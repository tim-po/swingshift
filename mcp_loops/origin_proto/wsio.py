"""wsio — the raw asyncio + wsproto WebSocket primitive.

A tiny, dependency-lean transport layer: it turns an asyncio stream pair into a
message-oriented WebSocket connection (:class:`WSConn`) for BOTH ends, plus a
client dialer (:func:`connect`) and a server acceptor (:func:`serve`). It knows
nothing about the origin protocol — it only moves UTF-8 text frames, answers
pings, and closes cleanly. The protocol logic (handshake, auth, RPC, events,
reconnect, idempotency) lives one layer up in :mod:`channel`.

Why wsproto: it is the sans-I/O WebSocket library already vendored for the
dashboard's terminal handshake (pure-python, only needs h11), so we add no new
transport dependency. wsproto parses/produces frames; we drive the byte I/O over
asyncio streams and serialize writes behind a lock so a background pong/close
never interleaves with an app send.

Transport crypto (§4.3, isolation rules): this dials/serves over PLAINTEXT
loopback for the in-process dogfood, and over TLS/mTLS when an `ssl.SSLContext`
is passed through to ``asyncio.open_connection`` / ``start_server`` (G2.5 — see
:mod:`mcp_loops.origin_proto.tls`, which mints those contexts from the device
keypair). Channel AUTHENTICATION is by the device keypair either way (replay-proof
challenge/response — see :mod:`channel`), which is transport-independent; TLS
wraps it for the network hop and does not change the auth model. ``WSConn`` also
exposes :meth:`WSConn.ssl_object` so the channel can read the TLS peer cert and
bind the encrypted tunnel to the device key.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection, BytesMessage, CloseConnection, Ping, Pong, Request,
    RejectConnection, TextMessage)
from wsproto.frame_protocol import CloseReason

_READ_CHUNK = 65536
WS_PATH = "/origin/ws"


class WSClosed(Exception):
    """The WebSocket is closed; no further sends/recvs will succeed."""


class WSConn:
    """A message-oriented WebSocket connection over an asyncio stream pair.

    Received text frames (reassembled across fragments) land on an internal
    queue; :meth:`recv` awaits the next one and returns ``None`` once the peer
    closes. :meth:`send` writes a text frame. A background pump task reads bytes,
    feeds wsproto, answers pings, and enqueues messages. Writes (app sends +
    protocol pongs/closes) are serialized behind a lock.
    """

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, ws: WSConnection):
        self._reader = reader
        self._writer = writer
        self._ws = ws
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._recv_buf = ""
        self._pump_task: Optional[asyncio.Task] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def _start(self) -> None:
        self._pump_task = asyncio.ensure_future(self._pump())

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def ssl_object(self):
        """The live ``ssl.SSLObject`` for this connection, or None on a plaintext
        socket. Lets the channel read the TLS peer cert post-handshake to bind the
        encrypted tunnel to the device key (:func:`tls.peer_pubkey_b64`)."""
        try:
            return self._writer.get_extra_info("ssl_object")
        except (AttributeError, KeyError):
            return None

    def peername(self):
        """The peer's ``(host, port)`` for this connection, or None when the
        transport can't report one. The channel uses the host to classify a live
        origin as loopback (may stay plaintext) vs. non-loopback (the §5.1(4)
        mTLS hard gate applies to its ``origin.run``)."""
        try:
            return self._writer.get_extra_info("peername")
        except (AttributeError, KeyError):
            return None

    async def _raw_write(self, data: bytes) -> None:
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def _pump(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    data = await self._reader.read(_READ_CHUNK)
                except (ConnectionError, OSError):
                    break
                if not data:
                    break  # EOF
                self._ws.receive_data(data)
                for ev in self._ws.events():
                    await self._handle_event(ev)
        finally:
            await self._teardown()

    async def _handle_event(self, ev) -> None:
        if isinstance(ev, TextMessage):
            self._recv_buf += ev.data
            if ev.message_finished:
                msg, self._recv_buf = self._recv_buf, ""
                await self._inbox.put(msg)
        elif isinstance(ev, BytesMessage):
            # protocol is text-JSON only; drain fragments but ignore binary.
            if ev.message_finished:
                self._recv_buf = ""
        elif isinstance(ev, Ping):
            try:
                await self._raw_write(self._ws.send(ev.response()))
            except (ConnectionError, OSError):
                await self._teardown()
        elif isinstance(ev, Pong):
            pass
        elif isinstance(ev, CloseConnection):
            # echo the close, then we're done reading.
            try:
                await self._raw_write(self._ws.send(ev.response()))
            except (ConnectionError, OSError):
                pass
            await self._teardown()

    async def _teardown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        # unblock any waiting recv()
        await self._inbox.put(None)
        try:
            self._writer.close()
        except OSError:
            pass

    # ── message I/O ───────────────────────────────────────────────────────────
    async def send(self, text: str) -> None:
        if self._closed.is_set():
            raise WSClosed("send on a closed WebSocket")
        try:
            from wsproto.events import Message
            await self._raw_write(self._ws.send(Message(data=text)))
        except (ConnectionError, OSError) as e:
            await self._teardown()
            raise WSClosed(str(e)) from e

    async def recv(self) -> Optional[str]:
        """Await the next text message, or ``None`` when the peer has closed."""
        if self._closed.is_set() and self._inbox.empty():
            return None
        msg = await self._inbox.get()
        return msg

    async def close(self, *, code: CloseReason = CloseReason.NORMAL_CLOSURE,
                    reason: str = "") -> None:
        if self._closed.is_set():
            return
        try:
            await self._raw_write(
                self._ws.send(CloseConnection(code=code.value, reason=reason)))
        except (ConnectionError, OSError, Exception):  # noqa: BLE001
            pass
        await self._teardown()
        if self._pump_task is not None:
            try:
                await asyncio.wait_for(self._pump_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._pump_task.cancel()

    async def wait_closed(self) -> None:
        await self._closed.wait()


# ── client dialer ─────────────────────────────────────────────────────────────
async def connect(host: str, port: int, *, path: str = WS_PATH,
                  ssl=None, tunnel_path: Optional[str] = None,
                  tunnel_ssl=None) -> WSConn:
    """Dial a WebSocket server (the ORIGIN dialing OUT — §4.3). Completes the
    HTTP upgrade handshake and returns a live :class:`WSConn`.

    ``tunnel_path`` (e.g. ``/hub``) reaches the Hub through a front door's byte
    tunnel instead: ``host:port`` is the front door (``tunnel_ssl`` its TLS), and
    ``ssl`` — the Hub's pinned TLS — runs INSIDE the tunnel, end to end."""
    if tunnel_path:
        from mcp_loops.origin_proto import hub_tunnel
        sock = await hub_tunnel.open_tunnel(host, port, tunnel_path,
                                            outer_ssl=tunnel_ssl)
        reader, writer = await asyncio.open_connection(
            sock=sock, ssl=ssl, server_hostname="" if ssl is not None else None)
    else:
        reader, writer = await asyncio.open_connection(host, port, ssl=ssl)
    ws = WSConnection(ConnectionType.CLIENT)
    writer.write(ws.send(Request(host=host, target=path)))
    await writer.drain()
    # read until the server accepts (or rejects) the upgrade
    while True:
        data = await reader.read(_READ_CHUNK)
        if not data:
            writer.close()
            raise WSClosed("connection closed during upgrade")
        ws.receive_data(data)
        accepted = False
        for ev in ws.events():
            if isinstance(ev, AcceptConnection):
                accepted = True
            elif isinstance(ev, RejectConnection):
                writer.close()
                raise WSClosed(f"server rejected upgrade ({ev.status_code})")
        if accepted:
            break
    conn = WSConn(reader, writer, ws)
    conn._start()
    return conn


# ── server acceptor ───────────────────────────────────────────────────────────
async def serve(handler: Callable[[WSConn], Awaitable[None]], host: str,
                port: int, *, path: str = WS_PATH, ssl=None):
    """Start a WebSocket server. For each accepted upgrade, spawns ``handler``
    with a live :class:`WSConn`. Returns the ``asyncio.Server`` (call
    ``.close()`` + ``await .wait_closed()`` to stop). Binds the exact host/port
    given — callers pass a UNIQUE high port and 127.0.0.1 (isolation rules)."""

    async def _on_client(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        ws = WSConnection(ConnectionType.SERVER)
        conn: Optional[WSConn] = None
        try:
            # complete the upgrade handshake
            while conn is None:
                data = await reader.read(_READ_CHUNK)
                if not data:
                    writer.close()
                    return
                ws.receive_data(data)
                for ev in ws.events():
                    if isinstance(ev, Request):
                        if path and ev.target != path:
                            writer.write(ws.send(RejectConnection(status_code=404)))
                            await writer.drain()
                            writer.close()
                            return
                        writer.write(ws.send(AcceptConnection()))
                        await writer.drain()
                        conn = WSConn(reader, writer, ws)
                        conn._start()
                        break
            await handler(conn)
        except (ConnectionError, OSError):
            pass
        finally:
            if conn is not None and not conn.closed:
                await conn.close()

    return await asyncio.start_server(_on_client, host, port, ssl=ssl)
