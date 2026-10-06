"""hub_tunnel — dial the Hub THROUGH an HTTPS front door's ``/hub`` tunnel.

The dashboard gate exposes the local Hub as a WebSocket at ``wss://<public>/hub``
that carries raw bytes (binary frames) to the Hub's TCP port. Cloudflare ends the
OUTER TLS, so the Hub's own pinned-cert/device-cert TLS must run INSIDE those
frames, end to end between the origin and the Hub.

:func:`open_tunnel` does the outer half: TLS to the front door (CA-verified, the
normal web posture), the WebSocket upgrade to ``path``, then a socketpair whose
far end is pumped to/from binary frames. The caller gets a plain socket and runs
the inner connection over it exactly as if it had dialed the Hub directly — so
the pinned inner TLS (``wsio.connect(sock=…)``) is what authenticates the Hub,
and the gate / Cloudflare only ever see ciphertext.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Optional

from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection, BytesMessage, CloseConnection, Message, Ping,
    RejectConnection, Request, TextMessage)

_CHUNK = 65536
CONNECT_TIMEOUT = 10.0
_PUMPS: "set[asyncio.Task]" = set()


class TunnelError(OSError):
    """The front door refused or dropped the /hub upgrade (an OSError so callers
    that already map transport failures to ``hub_unreachable`` keep doing so)."""


def _host_header(host: str, port: int, secure: bool) -> str:
    default = 443 if secure else 80
    h = f"[{host}]" if ":" in host else host
    return h if port == default else f"{h}:{port}"


async def open_tunnel(host: str, port: int, path: str, *, outer_ssl: Any = None,
                      timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """Open the outer ``/hub`` WebSocket and return the local end of a socketpair
    that is piped through it. ``outer_ssl`` is the front door's TLS context (None
    = plain ``ws://``, used by tests). Closing the returned socket (or the inner
    connection built on it) tears the outer WebSocket down; the front door
    hanging up closes the socket. Raises :class:`TunnelError` / OSError."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=outer_ssl,
                                server_hostname=host if outer_ssl else None),
        timeout)
    ws = WSConnection(ConnectionType.CLIENT)
    try:
        writer.write(ws.send(Request(
            host=_host_header(host, port, outer_ssl is not None), target=path)))
        await writer.drain()
        pending = b""   # bytes past the 101 that already belong to the pipe
        accepted = False
        while not accepted:
            data = await asyncio.wait_for(reader.read(_CHUNK), timeout)
            if not data:
                raise TunnelError("front door closed during the /hub upgrade")
            ws.receive_data(data)
            for ev in ws.events():
                if isinstance(ev, AcceptConnection):
                    accepted = True
                elif isinstance(ev, RejectConnection):
                    raise TunnelError(
                        f"front door rejected the /hub upgrade ({ev.status_code})")
                elif isinstance(ev, BytesMessage):
                    pending += ev.data
    except BaseException:
        writer.close()
        raise

    near, far = socket.socketpair()
    near.setblocking(False)
    far.setblocking(False)
    lr, lw = await asyncio.open_connection(sock=near)
    if pending:
        lw.write(pending)
    # hold the pump: the loop keeps only weak refs to tasks, so an unreferenced
    # pump can be garbage-collected mid-connection and drop the tunnel
    task = asyncio.ensure_future(_pump(ws, reader, writer, lr, lw))
    _PUMPS.add(task)
    task.add_done_callback(_PUMPS.discard)
    return far


async def _pump(ws: WSConnection, reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter, lr: asyncio.StreamReader,
                lw: asyncio.StreamWriter) -> None:
    """Shovel bytes between the socketpair and the outer WebSocket until either
    side closes, then close both."""
    lock = asyncio.Lock()

    async def out_write(data: bytes) -> None:
        async with lock:
            writer.write(data)
            await writer.drain()

    async def outer_to_local() -> None:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                return
            ws.receive_data(data)
            for ev in ws.events():
                if isinstance(ev, BytesMessage):
                    lw.write(ev.data)
                    await lw.drain()
                elif isinstance(ev, TextMessage):
                    return   # the tunnel is binary-only; text is a protocol fault
                elif isinstance(ev, Ping):
                    await out_write(ws.send(ev.response()))
                elif isinstance(ev, CloseConnection):
                    try:
                        await out_write(ws.send(ev.response()))
                    except (ConnectionError, OSError):
                        pass
                    return

    async def local_to_outer() -> None:
        while True:
            data = await lr.read(_CHUNK)
            if not data:
                return
            await out_write(ws.send(Message(data=data)))

    tasks = [asyncio.ensure_future(outer_to_local()),
             asyncio.ensure_future(local_to_outer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for w in (lw, writer):
            try:
                w.close()
            except Exception:  # noqa: BLE001 — already gone
                pass


async def peek_cert_der(host: str, port: int, path: str, *,
                        outer_ssl: Any = None,
                        timeout: float = CONNECT_TIMEOUT) -> Optional[bytes]:
    """The DER cert the Hub presents on the INNER TLS handshake through the
    tunnel, read without sending any application data (the tunneled twin of
    ``origin_client.peek_hub_cert_fingerprint``)."""
    import ssl as _ssl
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False   # identity is the fingerprint, checked by the caller
    ctx.verify_mode = _ssl.CERT_NONE
    sock = await open_tunnel(host, port, path, outer_ssl=outer_ssl, timeout=timeout)
    _, w = await asyncio.wait_for(
        asyncio.open_connection(sock=sock, ssl=ctx, server_hostname=""), timeout)
    try:
        obj = w.get_extra_info("ssl_object")
        return obj.getpeercert(binary_form=True) if obj is not None else None
    finally:
        w.close()
