"""channel — G3: the secure origin channel (protocol logic over :mod:`wsio`).

The origin DIALS OUT and holds one long-lived WebSocket (no inbound ports,
NAT-friendly — §4.3). Both ends run the wire protocol from :mod:`wire`:

  handshake   hello → challenge → auth → welcome. The origin proves identity by
              signing a fresh server nonce with its device key (replay-proof; no
              bearer token in a URL, ever). The plane verifies against the
              enrolled PUBLIC key and refuses a revoked/unknown device
              (fail-closed). Protocol version is negotiated, not assumed.
  steady      hub → origin: capability-scoped RPC (fixed allowlist, NEVER shell).
              origin → hub: pushed run/turn/status/result events + heartbeats
              (replaces the rsync mirror).
  reliability reconnect with exponential backoff; at-least-once delivery with a
              dispatch-id idempotency ledger so a redelivery RE-ATTACHES to the
              in-flight run instead of double-running (§7).
  revocation  the server registers a teardown hook on the EnrollmentStore, so a
              revoke DROPS the live socket server-side, not just next time (§6.3).

Two classes: :class:`ChannelServer` (hub side, accepts dials — the transport
under the other engineer's registry/router) and :class:`ChannelClient` (origin
side, dials out — the transport under the origin-agent core's RPC executor +
event pusher). Both take injected callables for the pieces the OTHER engineer
owns (an RPC executor on the client, an event sink on the server), so this
module stays pure transport + protocol + identity.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import ssl
import time
from typing import Any, Awaitable, Callable, Optional

from mcp_loops import audit as _daudit
from mcp_loops import origin_policy as _policy
from mcp_loops.origin_proto import identity, wire, wsio
from mcp_loops.origin_proto.enrollment import (
    A_DISPATCH, DEFAULT_OWNER, EnrollmentError, EnrollmentStore, device_owner)

# EnrollmentError.code → wire error code (the plane answers a refused handshake
# with a typed error frame, not a naked disconnect).
_AUTH_ERR_MAP = {
    "unknown_device": wire.E_UNKNOWN_DEVICE,
    "revoked": wire.E_REVOKED,
    "bad_signature": wire.E_BAD_SIGNATURE,
}

# reconnect backoff (§4.3, §8): start fast, cap, so a napping laptop or a flapped
# tunnel re-establishes quickly without hammering the plane.
BACKOFF_INITIAL = 0.2
BACKOFF_MAX = 30.0
BACKOFF_FACTOR = 2.0

RPC_TIMEOUT = 30.0
HEARTBEAT_INTERVAL = 15.0


class ChannelError(RuntimeError):
    """A channel-level failure (handshake botched, RPC timed out, no live
    origin).

    ``sent`` says whether the request frame MAY have left the Hub. It defaults to
    True (conservative: outcome unknown); ``call_rpc`` clears it for refusals it
    raises BEFORE anything crosses the wire (unknown method, revoked, not live,
    missing dispatchId), so an auditor can tell "never sent" from "sent, lost"."""

    sent: bool = True


def _unsent(exc: ChannelError) -> ChannelError:
    """Mark a pre-send refusal: nothing crossed the wire (P3b)."""
    exc.sent = False
    return exc


class ChannelDenied(ChannelError):
    """The plane refused this device's handshake (unknown/revoked/bad-sig). The
    client must STOP reconnecting — a revoked key will never be admitted, so
    retrying is pointless and noisy. Carries the wire error ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class RpcRefused(ChannelError):
    """The origin ANSWERED the rpc with a typed error (``rpc_result ok=false`` —
    e.g. RunNotAllowed / argv policy / RunBusy). Distinct from a transport failure
    (disconnect, timeout), whose outcome is unknown: a refusal is proof the origin
    did not complete the call. Carries the wire error ``code``."""

    def __init__(self, code: Optional[str], message: Optional[str]):
        super().__init__(f"rpc failed: {code}: {message}")
        self.code = code


# ── idempotency ledger (§7) ───────────────────────────────────────────────────
class DispatchLedger:
    """Idempotency keyed by dispatch-id. An in-flight dispatch shares ONE future
    (a redelivery re-attaches to the running one — never a second run); a
    completed dispatch is recorded (optionally persisted) so a redelivery after
    reconnect/restart returns the recorded result instead of re-running.

    This is the concrete mechanism behind "never double-runs": the executor is
    invoked at most once per dispatch-id, ever.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._done: dict[str, dict] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        if path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("dispatchId"):
                        self._done[rec["dispatchId"]] = rec.get("result", {})
        except OSError:
            pass

    def _persist(self, dispatch_id: str, result: dict) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"dispatchId": dispatch_id,
                                     "result": result}) + "\n")
        except OSError:
            pass

    def seen(self, dispatch_id: str) -> bool:
        return dispatch_id in self._done

    async def run(self, dispatch_id: str,
                  factory: Callable[[], Awaitable[dict]]) -> dict:
        """Run ``factory()`` exactly once for ``dispatch_id``. A redelivery while
        it is in-flight re-attaches to the same future; a redelivery after it
        completed returns the recorded result. The returned dict carries
        ``_reattached: True`` on any redelivery so callers/tests can see dedupe
        happened."""
        if dispatch_id in self._done:
            return {**self._done[dispatch_id], "_reattached": True}
        inflight = self._inflight.get(dispatch_id)
        if inflight is not None:
            res = await asyncio.shield(inflight)
            return {**res, "_reattached": True}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        # if the run errors and no redelivery is awaiting this future, its
        # exception would otherwise log "never retrieved" — consume it in a
        # done-callback so a lone failed dispatch stays quiet (the error still
        # propagates to the caller via the `raise` below).
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[dispatch_id] = fut
        try:
            result = await factory()
        except Exception as e:
            self._inflight.pop(dispatch_id, None)
            if not fut.done():
                fut.set_exception(e)
            raise
        self._done[dispatch_id] = result
        self._persist(dispatch_id, result)
        self._inflight.pop(dispatch_id, None)
        if not fut.done():
            fut.set_result(result)
        return result


def _is_loopback_host(host: Optional[str]) -> bool:
    """True iff ``host`` is a loopback address (127.0.0.0/8, ::1) or absent.

    The §5.1(4) mTLS hard gate exempts loopback origins — the VPS's own ``local``
    origin dials over plaintext loopback, where there is no network to eavesdrop.
    A ``None`` host (an in-process/no-socket transport) is inherently local and
    treated as loopback; any routable peer is NOT loopback and its ``origin.run``
    must ride an mTLS-bound channel."""
    if host is None:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # not an IP literal (e.g. a hostname) — only the literal "localhost" is
        # safe to treat as loopback; anything else is conservatively NON-loopback.
        return host == "localhost"


# ── control-plane side: accepts origin dials ──────────────────────────────────
class _LiveOrigin:
    __slots__ = ("device_id", "conn", "session_id", "pending", "protocol_v",
                 "last_seen", "capabilities", "peer_host", "tls_bound",
                 "manifest", "manifest_status")

    def __init__(self, device_id: str, conn: wsio.WSConn, session_id: str,
                 protocol_v: int):
        self.device_id = device_id
        self.conn = conn
        self.session_id = session_id
        self.protocol_v = protocol_v
        self.pending: dict[str, asyncio.Future] = {}
        self.last_seen: Optional[float] = None
        self.capabilities: dict = {}
        # §5.1(4): the transport posture captured at handshake, so the exec gate
        # answers deterministically without re-reading the socket mid-run.
        self.peer_host: Optional[str] = None
        self.tls_bound: bool = False
        # P3: the capability manifest THIS session advertised (the Hub enforces it
        # on every unit); fail-closed describe-only until the handshake sets it.
        self.manifest: _policy.OriginManifest = _policy.hub_fallback()
        self.manifest_status: str = _policy.M_MISSING


class ChannelServer:
    """The hub endpoint: accepts origin dials, runs the device-key handshake, and
    holds the live sessions. The other engineer's registry/router layers on top
    via :meth:`call_rpc` (send a capability-scoped RPC to a live origin) and the
    ``on_event`` sink (ingest pushed events into the per-origin namespace).

    It registers a teardown hook on the :class:`EnrollmentStore` so a revoke
    drops the live socket server-side (§6.3)."""

    def __init__(self, store: EnrollmentStore, *,
                 on_event: Optional[Callable[[str, dict], Any]] = None,
                 on_authenticated: Optional[Callable[[str, dict], Any]] = None,
                 ssl_context: Any = None,
                 require_tls_binding: bool = False,
                 hub_cert_pem: Optional[bytes] = None,
                 is_loopback_fn: Callable[[Optional[str]], bool] = _is_loopback_host,
                 now_fn: Callable[[], float] = time.time):
        self.store = store
        # §6.2/§5.1(4)(b): the Hub's own cert (PEM), handed back to a box at the
        # end of a successful pre-auth pairing claim so the box can PIN it for
        # every subsequent mTLS dial. None → the box must obtain the pin out of
        # band (the pairing answer still enrolls the key + returns the device_id).
        self._hub_cert_pem = hub_cert_pem
        self._on_event = on_event
        self._on_authenticated = on_authenticated
        # §5.1(4): how the exec gate classifies a live origin as loopback (may
        # stay plaintext) vs. non-loopback (mTLS-bound required). Injectable so a
        # test can exercise the NON-loopback branch over a real mTLS channel that
        # is physically loopback, without a second host.
        self._is_loopback = is_loopback_fn
        # G2.5: TLS on the accept side. ``ssl_context`` (from tls.server_ssl_context)
        # encrypts the tunnel; ``require_tls_binding`` additionally demands that a
        # presented TLS client cert's device pubkey MATCH the pubkey proven in the
        # app-layer auth frame, binding the encrypted session to the device key.
        self._ssl_context = ssl_context
        self._require_tls_binding = require_tls_binding
        # P2.5: a context that ASKS for client certs without a fixed bundle
        # (tls.server_ssl_context(request_client_cert=True)) trusts exactly the
        # enrolled devices' certs — the ones on disk now, plus each new one as a
        # pairing claim delivers it (_handle_pair).
        self._trusts_enrolled_certs = bool(
            require_tls_binding and ssl_context is not None
            and getattr(ssl_context, "verify_mode", None) == ssl.CERT_OPTIONAL)
        if self._trusts_enrolled_certs:
            from mcp_loops.origin_proto import tls as _tls
            for pem in store.enrolled_device_certs():
                try:
                    _tls.trust_client_cert(ssl_context, pem)
                except (ssl.SSLError, ValueError):
                    pass
        self._now = now_fn
        self._live: dict[str, _LiveOrigin] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._rpc_seq = 0
        # §2.4 seam 2: the per-dispatchId run back-channel. A streamed origin.run's
        # run.chunk/run.end events carry a `dispatchId` (NOT a loop name), so the
        # loop-name EventConsolidator cannot correlate them to the in-flight call.
        # A caller (DispatchRouter.run) subscribes by dispatch-id; _serve forwards
        # matching run events to it as they arrive, while the terminal rpc_result
        # still resolves the call's future.
        # P3c: keyed by (device_id, dispatchId), not dispatchId alone — a run event
        # is only delivered when it arrived on the SAME origin's authenticated
        # session the run was sent to, so another live origin that learns (or
        # guesses) a dispatchId cannot inject chunks/run.end into that stream.
        self._run_subscribers: dict[tuple[str, str], Callable[[dict], Any]] = {}
        # wire revoke → active teardown (§6.3)
        store._on_revoke = self._on_revoke

    # ── lifecycle ──────────────────────────────────────────────────────────────
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Start accepting dials. Returns the bound port (pass 0 to get an
        ephemeral high port — the isolation-safe default for tests). When an
        ``ssl_context`` was supplied the listener serves wss/TLS (G2.5)."""
        self._loop = asyncio.get_event_loop()
        self._server = await wsio.serve(self._handle, host, port,
                                        ssl=self._ssl_context)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for live in list(self._live.values()):
            try:
                await live.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._live.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def live_origins(self) -> list[str]:
        return sorted(self._live.keys())

    def is_live(self, device_id: str) -> bool:
        return device_id in self._live

    def manifest_for(self, device_id: str) -> Optional[dict]:
        """P3: the manifest the Hub enforces for ``device_id``'s live session
        (with its receipt status), else None when not live."""
        live = self._live.get(device_id)
        if live is None:
            return None
        return {"manifest": live.manifest.to_dict(), "status": live.manifest_status,
                "digest": live.manifest.digest()}

    # ── handshake + steady-state receive ───────────────────────────────────────
    async def _handle(self, conn: wsio.WSConn) -> None:
        device_id: Optional[str] = None
        try:
            device_id = await self._handshake(conn)
            if device_id is None:
                return
            await self._serve(device_id, conn)
        except (wsio.WSClosed, ConnectionError, OSError):
            pass
        finally:
            if device_id is not None:
                live = self._live.get(device_id)
                if live is not None and live.conn is conn:
                    self._live.pop(device_id, None)
                    for fut in live.pending.values():
                        if not fut.done():
                            fut.set_exception(ChannelError("origin disconnected"))

    async def _handshake(self, conn: wsio.WSConn) -> Optional[str]:
        now = self._now()
        # 1. hello — OR a pre-auth `pair` claim from a box that is not yet enrolled
        # (§6.2). A pairing claim cannot proceed to a live session (the box has no
        # authenticated identity yet); it enrolls the key, hands back the Hub cert
        # to pin, and closes. The box then re-dials the mTLS handshake as enrolled.
        frame = await self._recv_frame(conn)
        if frame is not None and frame.get("t") == wire.T_PAIR:
            await self._handle_pair(conn, frame, now=now)
            return None
        if frame is None or frame.get("t") != wire.T_HELLO:
            await self._send(conn, wire.error(wire.E_BAD_REQUEST, "expected hello"))
            return None
        try:
            wire.validate_frame(frame)
            v = wire.negotiate_version(frame["v"], frame["minV"])
        except wire.ProtocolError as e:
            await self._send(conn, wire.error(e.code, e.message))
            return None
        device_id = frame["deviceId"]
        # P3: hold the advertised manifest; it is judged + stored only after auth.
        raw_manifest = frame.get("manifest")
        # 2. challenge (fresh random nonce — replay-proof)
        nonce = secrets.token_bytes(32)
        await self._send(conn, wire.challenge(identity.b64e(nonce), v))
        # 3. auth
        frame = await self._recv_frame(conn)
        if frame is None or frame.get("t") != wire.T_AUTH:
            await self._send(conn, wire.error(wire.E_BAD_REQUEST, "expected auth"))
            return None
        try:
            wire.validate_frame(frame)
            if frame["deviceId"] != device_id:
                raise EnrollmentError("bad_signature", "device id changed mid-handshake")
            rec = self.store.authenticate(device_id, nonce, frame["sig"], now=now)
        except (wire.ProtocolError, EnrollmentError) as e:
            code = _AUTH_ERR_MAP.get(getattr(e, "code", ""), wire.E_BAD_REQUEST)
            await self._send(conn, wire.error(code, str(getattr(e, "message", e))))
            await conn.close()
            return None
        # 3b. TLS device-key binding (G2.5): if we required it, the TLS client
        # cert the peer presented must carry the SAME device key it just proved in
        # the app-layer auth frame. This ties the encrypted tunnel to the device
        # identity, so a hijacked TLS session cannot be driven under another
        # identity. Fail-closed: no cert / mismatched key → refuse.
        if self._require_tls_binding:
            from mcp_loops.origin_proto import tls as _tls
            peer_pub = _tls.peer_pubkey_b64(conn.ssl_object())
            enrolled_pub = rec.get("publicKey") if isinstance(rec, dict) else None
            if peer_pub is None or enrolled_pub is None or peer_pub != enrolled_pub:
                await self._send(conn, wire.error(
                    wire.E_BAD_SIGNATURE,
                    "TLS client cert is not bound to the authenticated device key"))
                await conn.close()
                return None
        # 4. welcome
        session_id = "sess_" + secrets.token_hex(8)
        live = _LiveOrigin(device_id, conn, session_id, v)
        live.last_seen = now
        live.capabilities = {"label": rec.get("label")}
        # §5.1(4): record the transport posture proven by THIS handshake. A
        # session is `tls_bound` only when the server both required the TLS
        # device-key binding AND ran over an encrypted tunnel (both true here iff
        # we reached welcome under `_require_tls_binding` with a live ssl_object).
        peer = conn.peername()
        live.peer_host = peer[0] if peer else None
        live.tls_bound = bool(self._require_tls_binding and
                              conn.ssl_object() is not None)
        # P3: the manifest is accepted only now — the peer has proven its device
        # key (and TLS binding, if required). Missing/invalid/unsupported → the
        # describe-only fallback. The record's owner is NEVER taken from it.
        manifest, m_status, m_detail = _policy.receive(raw_manifest)
        live.manifest = manifest
        live.manifest_status = m_status
        live.capabilities["manifest"] = manifest.to_dict()
        live.capabilities["manifestStatus"] = m_status
        try:
            self.store.set_manifest(device_id, manifest.to_dict(), now=now,
                                    status=m_status, detail=m_detail,
                                    digest=manifest.digest())
        except Exception:  # noqa: BLE001 — enforcement uses live.manifest regardless
            pass
        # a fresh dial supersedes any stale live session for the same device
        old = self._live.get(device_id)
        if old is not None and old.conn is not conn:
            asyncio.ensure_future(old.conn.close())
        self._live[device_id] = live
        await self._send(conn, wire.welcome(session_id, v,
                                            owner=device_owner(rec)))
        if self._on_authenticated is not None:
            try:
                self._on_authenticated(device_id, rec)
            except Exception:  # noqa: BLE001
                pass
        return device_id

    async def _handle_pair(self, conn: wsio.WSConn, frame: dict, *,
                           now: float) -> None:
        """The Hub side of the pre-auth pairing claim (§6.2). Validate the frame,
        consume the pairing code + enroll the presented PUBLIC key (this is the
        exact `EnrollmentStore.claim_pairing_code` flow, now reachable over the
        wire instead of only in-process), and answer with the device_id + TOFU
        fingerprint + the Hub's cert to pin. Any refusal is a typed `pairing_failed`
        error — the box never falls through to an unpaired dial. Closes either way:
        a pairing connection is one-shot, the box re-dials to authenticate."""
        try:
            wire.validate_frame(frame)
            wire.negotiate_version(frame["v"], frame["minV"])
        except wire.ProtocolError as e:
            await self._send(conn, wire.error(e.code, e.message))
            await conn.close()
            return
        try:
            device = self.store.claim_pairing_code(
                frame["code"], frame["pubkey"], now=now, label=frame.get("label"),
                origin_id=frame.get("originId"))
        except EnrollmentError as e:
            # bad/expired/consumed code or bad pubkey — all map to one box-facing
            # code so a probe cannot distinguish *why* a code failed.
            await self._send(conn, wire.error(
                wire.E_PAIRING_FAILED, str(getattr(e, "message", e))))
            await conn.close()
            return
        self._accept_device_cert(device["deviceId"], frame.get("cert"))
        hub_cert_b64 = (identity.b64e(self._hub_cert_pem)
                        if self._hub_cert_pem is not None else None)
        await self._send(conn, wire.paired(
            device["deviceId"], device["fingerprint"], hub_cert_b64=hub_cert_b64))
        await conn.close()

    def _accept_device_cert(self, device_id: str, cert_b64: Any) -> None:
        """Store the device cert a pairing claim carried and, on a binding Hub,
        trust it for the box's later mTLS dials. Best-effort: a missing/foreign
        cert leaves the device enrolled but unbound (the binding check refuses
        its dial loudly), never half-enrolled."""
        if not isinstance(cert_b64, str) or not cert_b64:
            return
        try:
            pem = identity.b64d(cert_b64)
            self.store.set_device_cert(device_id, pem)
        except Exception:  # noqa: BLE001 — bad cert ⇒ simply not trusted
            return
        if self._trusts_enrolled_certs:
            from mcp_loops.origin_proto import tls as _tls
            try:
                _tls.trust_client_cert(self._ssl_context, pem)
            except (ssl.SSLError, ValueError):
                pass

    async def _serve(self, device_id: str, conn: wsio.WSConn) -> None:
        live = self._live[device_id]
        while True:
            frame = await self._recv_frame(conn)
            if frame is None:
                break
            # a revoke may have landed while this frame was in flight — enforce
            # per-frame (per-RPC-boundary) denylist (§6.3).
            if self.store.is_revoked(device_id):
                await self._send(conn, wire.error(wire.E_REVOKED, "device revoked"))
                await conn.close()
                break
            t = frame.get("t")
            if t == wire.T_RPC_RESULT:
                fut = live.pending.pop(frame.get("id"), None)
                if fut is not None and not fut.done():
                    if frame.get("ok"):
                        fut.set_result(frame.get("result", {}))
                    else:
                        err = frame.get("error") or {}
                        fut.set_exception(RpcRefused(
                            err.get("code"), err.get("message")))
            elif t == wire.T_EVENT:
                live.last_seen = self._now()
                # §2.4 seam 2: forward run.chunk/run.end to a dispatch-id subscriber
                # (the streamed-run back-channel) as well as the consolidator sink.
                self._route_run_event(device_id, frame)
                if self._on_event is not None:
                    try:
                        self._on_event(device_id, frame)
                    except Exception:  # noqa: BLE001
                        pass
            elif t == wire.T_HEARTBEAT:
                live.last_seen = self._now()

    # ── outbound RPC (hub → origin) ────────────────────────────────────────────
    def _audit_unit(self, decision: str, device_id: str, method: str,
                    params: Optional[dict], dispatch_id: Optional[str], *,
                    reason: Optional[str] = None, result: Any = None) -> None:
        """P3 dispatch audit: ONE record per unit at this choke point (fail-soft).
        Owner/source/verb come from ``audit.dispatch_context`` set up-stack."""
        _daudit.record_unit(getattr(self.store, "root", None), decision=decision,
                            target=device_id, method=method, args=params,
                            dispatch_id=dispatch_id, reason=reason, result=result)

    def _refuse(self, exc: ChannelError, device_id: str, method: str,
                params: Optional[dict], dispatch_id: Optional[str]) -> ChannelError:
        """Audit a pre-send refusal as ``deny`` and mark it unsent (nothing ran)."""
        self._audit_unit("deny", device_id, method, params, dispatch_id,
                         reason=str(exc))
        return _unsent(exc)

    async def call_rpc(self, device_id: str, method: str,
                       params: Optional[dict] = None, *,
                       dispatch_id: Optional[str] = None,
                       timeout: float = RPC_TIMEOUT,
                       owner: Optional[str] = None,
                       verb: Optional[str] = None) -> dict:
        """Send a capability-scoped RPC to a live origin and await its result.

        This is THE Hub choke point every hub→origin unit passes (P3). Before
        anything crosses the wire it refuses, fail-closed: an off-allowlist
        method; a revoked/unknown device; a caller ``owner`` that does not own the
        device per its ENROLLED record (never the manifest); a non-live origin; a
        dispatch-id method without a UUID dispatchId (§7); a ``verb`` tag that does
        not fit the method; and any unit the target's advertised capability
        manifest does not permit. The owner + verb are stamped into the frame so
        the origin re-checks both (defence in depth).

        ``owner`` / ``verb`` default to the ``audit.dispatch_context`` values set
        up-stack, then to the pilot owner / the method's derived verb; the
        effective values are pushed back into that context, so the audit record
        carries exactly what was enforced. Every refusal is one ``deny`` record;
        a sent unit is one ``allow`` (or ``deny`` if the origin refused it)."""
        ctx = _daudit.current_context()
        eff_owner = owner or ctx.get("owner") or DEFAULT_OWNER
        tag = verb or ctx.get("verb")
        known = method in wire.RPC_METHODS
        eff_verb = _policy.verb_for(method, tag) if known else None
        with _daudit.dispatch_context(owner=eff_owner, verb=eff_verb):
            return await self._call_rpc(device_id, method, params,
                                        dispatch_id=dispatch_id, timeout=timeout,
                                        owner=eff_owner, tag=tag, verb=eff_verb)

    async def _call_rpc(self, device_id: str, method: str,
                        params: Optional[dict], *, dispatch_id: Optional[str],
                        timeout: float, owner: str, tag: Optional[str],
                        verb: Optional[str]) -> dict:
        if method not in wire.RPC_METHODS:
            raise self._refuse(ChannelError(
                f"method {method!r} not on the capability allowlist"),
                device_id, method, params, dispatch_id)
        if self.store.is_revoked(device_id):
            raise self._refuse(ChannelDenied(wire.E_REVOKED, "device is revoked"),
                               device_id, method, params, dispatch_id)
        # P3/P6 at the choke point: ownership from the ENROLLED record only.
        rec = self.store.get_device(device_id) or {}
        if device_owner(rec) != owner:
            raise self._refuse(ChannelDenied(
                wire.E_NOT_OWNER,
                f"device {device_id!r} is not owned by {owner!r}"),
                device_id, method, params, dispatch_id)
        live = self._live.get(device_id)
        if live is None:
            raise self._refuse(ChannelError(f"origin {device_id} is not live"),
                               device_id, method, params, dispatch_id)
        if method in wire.RPC_METHODS_REQUIRING_DISPATCH_ID:
            if not wire.is_dispatch_id(dispatch_id):
                raise self._refuse(ChannelError(
                    f"{method} requires a UUID dispatchId (§7)"),
                    device_id, method, params, dispatch_id)
        # P3 Hub-side enforcement: the unit must be in what the target advertised.
        reason = _policy.verb_mismatch(method, tag) or live.manifest.check(
            method, verb, params or {})
        if reason is not None:
            raise self._refuse(ChannelDenied(wire.E_NOT_PERMITTED, reason),
                               device_id, method, params, dispatch_id)
        self._rpc_seq += 1
        rpc_id = f"rpc{self._rpc_seq}"
        frame = wire.rpc(rpc_id, method, params, dispatch_id=dispatch_id,
                         owner=owner, verb=verb)
        wire.validate_rpc_request(frame)  # conformance on the way out, too
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        live.pending[rpc_id] = fut
        self.store.audit(A_DISPATCH, now=self._now(), deviceId=device_id,
                         method=method, dispatchId=dispatch_id, rpcId=rpc_id)
        await live.conn.send(json.dumps(frame))
        try:
            result = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            live.pending.pop(rpc_id, None)
            self._audit_unit("allow", device_id, method, params, dispatch_id,
                             result={"timedOut": True, "error": "ChannelTimeout"})
            raise ChannelError(f"rpc {method} timed out after {timeout}s") from e
        except RpcRefused as e:
            # the origin's defence-in-depth re-check (or its RunAllowlist) refused:
            # nothing ran — a deny, visible Hub-side.
            self._audit_unit("deny", device_id, method, params, dispatch_id,
                             reason=f"origin {e}")
            raise
        except Exception as e:  # disconnect etc. — sent, outcome unknown
            self._audit_unit("allow", device_id, method, params, dispatch_id,
                             result={"error": type(e).__name__})
            raise
        self._audit_unit("allow", device_id, method, params, dispatch_id,
                         result=result if isinstance(result, dict) else None)
        return result

    # ── §5.1(4) exec transport gate (Hub side of the bidirectional mTLS gate) ──
    def exec_transport_ok(self, device_id: str) -> Optional[str]:
        """Return ``None`` if this origin's live channel may carry ``origin.run``,
        else a human-readable refusal reason. This is the **Hub-side** half of the
        §5.1(4) bidirectional gate: a NON-loopback origin's session must be
        mTLS-bound — the Hub required + verified the origin's device client cert
        (``require_tls_binding`` over a real ``ssl_context``), so a plaintext /
        ``CERT_NONE`` / unbound remote session is refused, fail-closed. A loopback
        origin (the VPS's own ``local``) is exempt and may stay plaintext.

        The peer half — the origin pinning the Hub's cert and REFUSING a wrong/
        unpinned Hub — is enforced on the origin's dial (``tls.client_ssl_context``
        with ``server_cert_pem``); the Hub cannot observe it, so it is asserted
        separately in acceptance."""
        live = self._live.get(device_id)
        if live is None:
            return f"origin {device_id} is not live"
        if self._is_loopback(live.peer_host):
            return None  # loopback is exempt — no network to eavesdrop
        if live.tls_bound:
            return None  # non-loopback but mTLS device-bound → allowed
        return ("non-loopback origin.run requires an mTLS device-bound channel "
                "(§5.1(4)); this session is plaintext/CERT_NONE/unbound")

    # ── streamed-run back-channel (§2.4 seam 2) ────────────────────────────────
    def subscribe_run(self, dispatch_id: str,
                      callback: Callable[[dict], Any], *, device_id: str) -> None:
        """Register a callback to receive this dispatch-id's run.chunk/run.end
        event frames as they arrive (the streamed-run back-channel). DispatchRouter.
        run subscribes before sending origin.run and unsubscribes when the terminal
        rpc_result resolves. The subscription is bound to ``(device_id,
        dispatch_id)`` (P3c): only events arriving on ``device_id``'s own session
        are delivered."""
        self._run_subscribers[(device_id, dispatch_id)] = callback

    def unsubscribe_run(self, dispatch_id: str, *, device_id: str) -> None:
        self._run_subscribers.pop((device_id, dispatch_id), None)

    def _route_run_event(self, device_id: str, frame: dict) -> None:
        """Deliver a run.chunk/run.end event to its subscriber, if any.
        Correlation is by ``data.dispatchId`` — NOT loop name — which is exactly the
        gap the loop-name EventConsolidator could not close (§2.4) — AND by the
        authenticated ``device_id`` of the session the frame arrived on (P3c), so a
        different origin echoing someone else's dispatchId is dropped."""
        if frame.get("kind") not in ("run.chunk", "run.end"):
            return
        data = frame.get("data") or {}
        did = data.get("dispatchId")
        if not did:
            return
        cb = self._run_subscribers.get((device_id, did))
        if cb is None:
            return
        try:
            cb(frame)
        except Exception:  # noqa: BLE001 — a bad subscriber must not sink the channel
            pass

    # ── revoke teardown (§6.3) ─────────────────────────────────────────────────
    def _on_revoke(self, device_id: str) -> None:
        """Sync hook fired by EnrollmentStore.revoke — schedule an async socket
        teardown on the loop so revoke ACTIVELY severs the live session, not just
        denies the next reconnect."""
        live = self._live.pop(device_id, None)
        if live is None:
            return

        async def _kill():
            try:
                await self._send(live.conn, wire.error(wire.E_REVOKED, "revoked"))
            except Exception:  # noqa: BLE001
                pass
            try:
                await live.conn.close()
            except Exception:  # noqa: BLE001
                pass
            for fut in live.pending.values():
                if not fut.done():
                    fut.set_exception(ChannelDenied(wire.E_REVOKED, "revoked mid-flight"))

        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.ensure_future(_kill())

    # ── frame I/O helpers ──────────────────────────────────────────────────────
    async def _recv_frame(self, conn: wsio.WSConn) -> Optional[dict]:
        raw = await conn.recv()
        if raw is None:
            return None
        try:
            frame = json.loads(raw)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) else None

    async def _send(self, conn: wsio.WSConn, frame: dict) -> None:
        await conn.send(json.dumps(frame))


# ── origin side: dials out, serves RPC, pushes events ─────────────────────────
class ChannelClient:
    """The origin endpoint: dials the hub, authenticates by device key, then
    serves inbound capability-scoped RPCs (via the injected ``executor``) and
    pushes events/heartbeats. Reconnects with exponential backoff; a redelivered
    loop.start re-attaches through the :class:`DispatchLedger` instead of
    double-running.

    ``executor(method, params) -> awaitable[dict]`` is the origin-agent core's
    RPC executor (the other engineer's G2) — the channel calls it; it never
    interprets shell (there is no shell method on the allowlist).
    """

    def __init__(self, ident: identity.DeviceIdentity, host: str, port: int, *,
                 origin_id: str, executor: Callable[[str, dict], Awaitable[dict]],
                 ledger: Optional[DispatchLedger] = None,
                 capabilities_fn: Optional[Callable[[], dict]] = None,
                 manifest_fn: Optional[Callable[[], dict]] = None,
                 owner: Optional[str] = None,
                 path: str = wsio.WS_PATH,
                 ssl_context: Any = None,
                 tunnel_path: Optional[str] = None,
                 tunnel_ssl: Any = None,
                 backoff_initial: float = BACKOFF_INITIAL,
                 backoff_max: float = BACKOFF_MAX,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL):
        self.identity = ident
        self.host = host
        self.port = port
        self.origin_id = origin_id
        self._executor = executor
        self.ledger = ledger or DispatchLedger()
        self._capabilities_fn = capabilities_fn
        # P3: this origin's OWN capability manifest. Default: the executor's
        # (OriginAgentExecutor reads it from the origin's disk), else the no-exec
        # default. Re-read on every connect; advertised in hello AND enforced here.
        if manifest_fn is None and callable(getattr(executor, "manifest", None)):
            manifest_fn = executor.manifest
        self._manifest_fn = manifest_fn
        self.policy: _policy.OriginManifest = _policy.OriginManifest.default()
        # P3 owner pin: explicit ``owner`` > manifest.owner > the Hub's welcome
        # (per session). A unit stamped for any other owner is refused.
        self._owner_pin = owner
        self.session_owner: Optional[str] = None
        self._path = path
        # G2.5: TLS context (from tls.client_ssl_context) for dialing wss/mTLS.
        # None keeps the plaintext-loopback behaviour used by the in-process
        # dogfood; a real remote origin passes one that pins the hub cert and
        # presents the device cert.
        self._ssl_context = ssl_context
        # a Hub behind a front door's /hub byte tunnel: host:port is the front
        # door, the pinned ssl_context still runs end to end inside the tunnel
        self._tunnel_path = tunnel_path
        self._tunnel_ssl = tunnel_ssl
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._heartbeat_interval = heartbeat_interval
        self._conn: Optional[wsio.WSConn] = None
        self._seq = 0
        self._stop = False
        self._connected = asyncio.Event()
        self._session_v: Optional[int] = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    # ── one connection: handshake + serve until it drops ───────────────────────
    async def run_once(self) -> None:
        conn = await wsio.connect(self.host, self.port, path=self._path,
                                  ssl=self._ssl_context,
                                  tunnel_path=self._tunnel_path,
                                  tunnel_ssl=self._tunnel_ssl)
        self._conn = conn
        try:
            await self._handshake(conn)
            self._connected.set()
            hb = asyncio.ensure_future(self._heartbeat_loop(conn))
            try:
                await self._serve(conn)
            finally:
                hb.cancel()
        finally:
            self._connected.clear()
            self._conn = None
            await conn.close()

    def _load_policy(self) -> _policy.OriginManifest:
        if self._manifest_fn is None:
            return _policy.OriginManifest.default()
        try:
            return _policy.OriginManifest.from_dict(self._manifest_fn())
        except Exception:  # noqa: BLE001 — a broken manifest fails CLOSED
            return _policy.deny_all()

    async def _handshake(self, conn: wsio.WSConn) -> None:
        self.policy = self._load_policy()
        self.session_owner = None
        await self._send(conn, wire.hello(
            self.identity.device_id, self.identity.public_key_b64,
            label=self.identity.label, manifest=self.policy.to_dict()))
        frame = await self._recv_frame(conn)
        if frame is None:
            raise ChannelError("connection closed during handshake")
        if frame.get("t") == wire.T_ERROR:
            raise ChannelDenied(frame.get("code", "error"), frame.get("message", ""))
        if frame.get("t") != wire.T_CHALLENGE:
            raise ChannelError(f"expected challenge, got {frame.get('t')}")
        wire.validate_frame(frame)
        self._session_v = frame["v"]
        nonce = identity.b64d(frame["nonce"])
        sig = self.identity.sign(nonce)
        await self._send(conn, wire.auth(self.identity.device_id, sig))
        frame = await self._recv_frame(conn)
        if frame is None:
            raise ChannelError("connection closed awaiting welcome")
        if frame.get("t") == wire.T_ERROR:
            # unknown/revoked/bad-sig — do not retry a revoked key
            raise ChannelDenied(frame.get("code", "error"), frame.get("message", ""))
        if frame.get("t") != wire.T_WELCOME:
            raise ChannelError(f"expected welcome, got {frame.get('t')}")
        wire.validate_frame(frame)
        self.session_owner = (self._owner_pin or self.policy.owner
                              or frame.get("owner"))

    async def _serve(self, conn: wsio.WSConn) -> None:
        while True:
            frame = await self._recv_frame(conn)
            if frame is None:
                break
            if frame.get("t") == wire.T_RPC:
                await self._handle_rpc(conn, frame)
            elif frame.get("t") == wire.T_ERROR:
                # server-initiated error mid-session (e.g. revoked teardown)
                raise ChannelDenied(frame.get("code", "error"),
                                    frame.get("message", ""))

    async def _handle_rpc(self, conn: wsio.WSConn, frame: dict) -> None:
        rpc_id = frame.get("id")
        try:
            wire.validate_rpc_request(frame)
        except wire.ProtocolError as e:
            await self._send(conn, wire.rpc_result(
                rpc_id, ok=False, err={"code": e.code, "message": e.message}))
            return
        method = frame["method"]
        params = frame.get("params", {})
        dispatch_id = frame.get("dispatchId")
        # P3 origin-side re-check (defence in depth): the unit must be for THIS
        # origin's owner and inside ITS OWN manifest — whatever the Hub decided.
        refusal = self._refusal(frame, method, params)
        if refusal is not None:
            await self._send(conn, wire.rpc_result(
                rpc_id, ok=False, err={"code": refusal[0], "message": refusal[1]}))
            return
        try:
            if method in wire.RPC_METHODS_REQUIRING_DISPATCH_ID:
                # idempotent: at most ONE run per dispatch-id, ever (§7). Pass the
                # dispatch-id INTO the executor's params (reserved `_dispatchId`)
                # so a cross-process executor can forward it to the engine's
                # loop_start — telling the engine "run locally, keyed on this id"
                # instead of re-routing through the hub (breaks the recursion when
                # the agent reaches the engine over the MCP socket).
                exec_params = {**params, "_dispatchId": dispatch_id}
                # §2.4 seam 1: origin.run gets a push callback so a STREAMED exec
                # can emit run.chunk/run.end mid-run. The exec body runs in a pool
                # THREAD; this callback marshals each push back onto the event loop.
                if method == "origin.run":
                    exec_params["_push"] = self._make_run_push(dispatch_id)
                    # the checked verb, so the executor applies the matching gate
                    exec_params["_verb"] = _policy.verb_for(method, frame.get("verb"))
                result = await self.ledger.run(
                    dispatch_id, lambda: self._executor(method, exec_params))
            else:
                result = await self._executor(method, params)
            await self._send(conn, wire.rpc_result(rpc_id, ok=True, result=result))
        except Exception as e:  # noqa: BLE001 — a bad executor must not kill the channel
            await self._send(conn, wire.rpc_result(
                rpc_id, ok=False,
                err={"code": wire.E_INTERNAL, "message": f"{type(e).__name__}: {e}"}))

    def _refusal(self, frame: dict, method: str,
                 params: dict) -> Optional[tuple]:
        """``(code, message)`` if this origin must refuse the unit, else None."""
        owner = frame.get("owner")
        if self.session_owner is None or owner != self.session_owner:
            return (wire.E_NOT_OWNER,
                    f"origin {self.origin_id!r} accepts units for owner "
                    f"{self.session_owner!r} only, not {owner!r}")
        reason = self.policy.check(method, frame.get("verb"), params)
        if reason is not None:
            return (wire.E_NOT_PERMITTED,
                    f"origin {self.origin_id!r} refused {method}: {reason}")
        return None

    # ── streamed origin.run push callback (§2.4 seam 1) ─────────────────────────
    def _make_run_push(self, dispatch_id: Optional[str]) -> Callable[[str, dict], Any]:
        """Build the thread-safe ``push(kind, data)`` handed to a streamed exec.
        The exec runs on a pool thread; this marshals each push onto THIS channel's
        event loop via :func:`asyncio.run_coroutine_threadsafe`, blocking the
        pool thread on the send so chunks are wire-ordered and back-pressured. The
        dispatch-id is stamped here (single source), so the executor need only send
        ``{fd, b64}`` / ``{exitCode, durationMs}``."""
        loop = asyncio.get_event_loop()

        def push(kind: str, data: dict) -> None:
            payload = {**data, "dispatchId": dispatch_id}
            fut = asyncio.run_coroutine_threadsafe(
                self.push_event(kind, payload), loop)
            fut.result()  # propagate a send error to the caller (best-effort wraps)

        return push

    # ── pushed telemetry (origin → hub) ────────────────────────────────────────
    async def push_event(self, kind: str, data: dict, *,
                         ts: Optional[float] = None) -> None:
        """Push a run/turn/status/result event to the hub. No-op-raises if not
        connected (the caller buffers/retries — §7 offline tolerance)."""
        conn = self._conn
        if conn is None or not self._connected.is_set():
            raise ChannelError("not connected — buffer and flush on reconnect")
        self._seq += 1
        frame = wire.event(kind, self._seq, ts if ts is not None else time.time(),
                           self.origin_id, data)
        wire.validate_event(frame)
        await self._send(conn, json.dumps(frame))

    async def _heartbeat_loop(self, conn: wsio.WSConn) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                caps = self._capabilities_fn() if self._capabilities_fn else {}
                await self._send(conn, wire.heartbeat(
                    time.time(), load=caps.get("load"), busy=caps.get("busy")))
        except (asyncio.CancelledError, wsio.WSClosed, ConnectionError, OSError):
            pass

    # ── reconnect loop with exponential backoff (§4.3, §8) ─────────────────────
    async def run_forever(self, *, max_reconnects: Optional[int] = None) -> None:
        """Dial, serve, and reconnect with exponential backoff across drops. A
        clean session resets the backoff. A ChannelDenied (revoked/unknown key)
        STOPS the loop — a revoked key will never be admitted. ``max_reconnects``
        bounds it for tests; None runs until :meth:`stop`."""
        backoff = self._backoff_initial
        attempts = 0
        while not self._stop:
            try:
                await self.run_once()
                backoff = self._backoff_initial  # clean session → reset
            except ChannelDenied:
                raise
            except (ChannelError, wsio.WSClosed, ConnectionError, OSError):
                pass
            if self._stop:
                break
            attempts += 1
            if max_reconnects is not None and attempts >= max_reconnects:
                break
            await asyncio.sleep(backoff)
            backoff = min(self._backoff_max, backoff * BACKOFF_FACTOR)

    def stop(self) -> None:
        self._stop = True

    # ── frame I/O helpers ──────────────────────────────────────────────────────
    async def _recv_frame(self, conn: wsio.WSConn) -> Optional[dict]:
        raw = await conn.recv()
        if raw is None:
            return None
        try:
            frame = json.loads(raw)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) else None

    async def _send(self, conn: wsio.WSConn, frame) -> None:
        await conn.send(frame if isinstance(frame, str) else json.dumps(frame))


# ── the pre-auth pairing claim, box side (§6.2 remote pairing-claim transport) ──
async def pair_with_hub(host: str, port: int, *, code: str, pubkey_b64: str,
                        label: Optional[str] = None, ssl_context: Any = None,
                        cert_b64: Optional[str] = None,
                        origin_id: Optional[str] = None,
                        path: str = wsio.WS_PATH,
                        tunnel_path: Optional[str] = None,
                        tunnel_ssl: Any = None,
                        timeout: float = RPC_TIMEOUT) -> dict:
    """Dial the Hub ONCE, present a pairing ``code`` + this box's PUBLIC key, and
    return the Hub's answer: ``{deviceId, fingerprint, hubCert?}``. This is the box
    half of enrollment-over-the-wire — it needs NO enrolled identity yet (that is
    the whole point), so it runs before the device-key handshake can. ``ssl_context``
    is the TOFU dial context (encrypted, server cert captured but not yet pinned);
    the returned ``hubCert`` is what the box then pins for every subsequent mTLS
    dial. Raises :class:`ChannelDenied` (code ``pairing_failed``) on a refused claim
    and :class:`ChannelError` on a protocol/transport fault."""
    conn = await wsio.connect(host, port, path=path, ssl=ssl_context,
                              tunnel_path=tunnel_path, tunnel_ssl=tunnel_ssl)
    try:
        await conn.send(json.dumps(wire.pair(code, pubkey_b64, label=label,
                                               cert_b64=cert_b64,
                                               origin_id=origin_id)))
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout)
        except asyncio.TimeoutError as e:
            raise ChannelError("timed out awaiting pairing answer") from e
        if raw is None:
            raise ChannelError("connection closed during pairing")
        try:
            frame = json.loads(raw)
        except ValueError as e:
            raise ChannelError("malformed pairing answer") from e
        if not isinstance(frame, dict):
            raise ChannelError("malformed pairing answer")
        if frame.get("t") == wire.T_ERROR:
            raise ChannelDenied(frame.get("code", "error"), frame.get("message", ""))
        if frame.get("t") != wire.T_PAIRED:
            raise ChannelError(f"expected paired, got {frame.get('t')!r}")
        wire.validate_frame(frame)
        return frame
    finally:
        await conn.close()
