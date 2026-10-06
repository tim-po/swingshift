"""hub_control — the engine ↔ standalone Hub bridge (gap #3, prod topology).

In production the Hub that REMOTE origins (a Mac that ran ``yard origin up``)
hold their live channel to is :mod:`mcp_loops.hub_serve`, a SEPARATE process
behind the dashboard gate's ``/hub`` tunnel. The engine (``mcp_loops.server``)
only ever dispatched through its own in-process ``LocalOriginService`` hub, so a
``loop_start(origin=<mac>)`` / capabilities probe could never reach a
hub_serve-enrolled origin. This module closes that hop without a new wire:

  * :func:`serve_control` — run BY hub_serve on its own event loop: a Unix-domain
    control socket (0600, peer uid checked with ``SO_PEERCRED``) that answers a
    SMALL fixed op set by calling the SAME :class:`OriginHub` router/registry the
    channel already uses. Protocol v1 ops: ``ping``, ``resolve``, ``start``
    (``loop.start`` — the manifest's ``agent`` verb + the origin's own Project
    gate still apply), ``stop`` (``loop.stop`` — de-escalation, never gated),
    ``status`` (read-only ``loop.status``), ``probe`` (read-only
    ``capabilities.probe``), ``events`` and ``snapshot``.
    Protocol v2 (H2 + H1 step 1) adds ``hello`` (version/op negotiation), the
    IDENTITY reads ``device.get`` / ``policy.view`` / ``audit.read`` — answered
    from the Hub's OWN EnrollmentStore + dispatch audit, the one authoritative
    store for the origins enrolled here — and the exec ops ``run`` /
    ``fs.pull`` / ``fs.push``. Exec rides the SAME ``router.run`` choke point
    the in-process hub uses: the Hub's manifest pre-send gate, the §5.1(4)
    binding gate, the origin's own fail-closed RunAllowlist and the Hub audit
    all still apply; the socket itself stays 0600 + same-uid.
  * :class:`HubServeDispatch` — the engine-side client, duck-typed to the
    remote half of ``server.OriginDispatch`` (``dispatch_start_remote``,
    ``dispatch_stop_remote``, ``dispatch_status_remote``, ``dispatch_capabilities_probe``,
    ``resolve_device``, ``remote_events``, ``consolidated_snapshot``) plus the v2
    identity reads (``device_get``, ``policy_view``, ``audit_read``) and exec
    (``dispatch_run``, ``dispatch_fs_pull``, ``dispatch_fs_push``). Its
    local-route ``dispatch_start`` always raises :class:`DispatchUnavailable`, so
    installing the bridge never re-routes an ordinary local start.

Opt-in on both sides: hub_serve ``--control-sock PATH`` and the engine's
``LOOPYARD_HUB_CONTROL_SOCK=PATH`` (:func:`mcp_loops.origin_service.
maybe_install_hub_bridge`). Neither set ⇒ nothing changes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import struct
import uuid
from typing import Any, Optional

from mcp_loops.origin_proto.dispatch import DispatchUnavailable

MAX_LINE = 4 * 1024 * 1024
# v1 = the original 8 unversioned ops (a request/reply with no ``v``). v2 adds
# ``hello`` + identity reads + exec. A request names the version it speaks in
# ``v``; the Hub refuses a NEWER one it cannot honour, and every reply carries
# the Hub's own ``v`` so a client can tell an old Hub from a bad request.
PROTOCOL_VERSION = 2
OPS_V1 = ("ping", "resolve", "start", "stop", "status", "probe", "events",
          "snapshot")
OPS_V2 = ("hello", "device.get", "policy.view", "audit.read", "run", "fs.pull",
          "fs.push")
OPS = OPS_V1 + OPS_V2
ENV_SOCK = "LOOPYARD_HUB_CONTROL_SOCK"


class HubControlError(RuntimeError):
    """A non-transport error the Hub reported for an op (a bug / bad request)."""


class HubProtocolError(DispatchUnavailable):
    """The Hub on the other end speaks an OLDER control protocol than the op
    needs (a hub_serve started before the upgrade) — a clear, fall-back-able
    reason rather than a bare 'unknown op'."""


# ── origin resolution (shared with LocalOriginService) ────────────────────────
def resolve_live_device(hub: Any, origin: str) -> str:
    """Map a REMOTE ``origin`` id/label/logical id to a LIVE device id on
    ``hub``. Raises :class:`DispatchUnavailable` with a clear reason when the
    origin is unknown or enrolled-but-not-connected."""
    reg = hub.registry
    live = set(reg.live_ids())
    for entry in reg.list():
        did = entry.get("deviceId")
        logical = (entry.get("capabilities") or {}).get("origin")
        if origin in (did, entry.get("label"), logical):
            if did in live:
                return did
            raise DispatchUnavailable(
                f"origin {origin!r} is enrolled but not connected")
    raise DispatchUnavailable(f"origin {origin!r} is not a connected origin")


# ── Hub side: the control socket ──────────────────────────────────────────────
def _peer_uid(writer: asyncio.StreamWriter) -> Optional[int]:
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    except OSError:
        return None


def _need_origin(params: dict) -> str:
    origin = params.get("origin")
    if not isinstance(origin, str) or not origin.strip():
        raise HubControlError("params.origin is required")
    return origin


def enrolled_device_id(hub: Any, origin: str) -> str:
    """Map ``origin`` (deviceId / label / logical origin id) to an ENROLLED
    device id — connected (registry) first, then any device in the Hub's
    EnrollmentStore, so an enrolled-but-offline origin still resolves (unlike
    :func:`resolve_live_device`). Raises :class:`DispatchUnavailable`."""
    for entry in list(hub.registry.list()) + list(hub.store.list_devices()):
        logical = (entry.get("capabilities") or {}).get("origin")
        if origin in (entry.get("deviceId"), entry.get("label"), logical):
            return entry.get("deviceId")
    raise DispatchUnavailable(f"origin {origin!r} is not an enrolled origin")


def policy_view(hub: Any, device_id: str, origin: str) -> dict:
    """P3: what ``hub`` ENFORCES for ``device_id`` — read-only, from the live
    session's manifest when connected, else the post-auth EnrollmentStore
    sidecar, else the describe-only fallback. ``owner`` is the ENROLLED owner
    (never the manifest's claim). Shared by the in-process hub
    (``LocalOriginService.policy_view``) and the ``policy.view`` op."""
    from mcp_loops import origin_policy as _pol
    from mcp_loops.origin_proto.enrollment import device_owner
    store = hub.store
    rec = store.get_device(device_id)
    live = hub.server.manifest_for(device_id)
    stored = store.get_manifest(device_id)
    if live is not None:
        source, m_dict, status = "live", live["manifest"], live["status"]
    elif stored is not None:
        source, m_dict, status = "stored", stored.get("manifest"), stored.get("status")
    else:
        source, m_dict, status = "none", _pol.hub_fallback().to_dict(), _pol.M_MISSING
    try:
        m = _pol.OriginManifest.from_dict(m_dict)
    except ValueError:  # a corrupt sidecar is shown as what the Hub enforces
        m, status = _pol.hub_fallback(), _pol.M_INVALID
    allowed = [v for v, ok in ((_pol.V_EXEC, m.exec), (_pol.V_FS_READ, m.fsRead),
                               (_pol.V_FS_WRITE, m.fsWrite),
                               (_pol.V_AGENT_START, m.agent),
                               (_pol.V_AGENT_STOP, m.agent)) if ok]
    allowed += [_pol.RPC_PREFIX + r for r in sorted(set(m.rpcs) | _pol.ALWAYS_RPCS)]
    return {"origin": origin, "deviceId": device_id,
            "owner": device_owner(rec) if rec else None,
            "live": live is not None, "source": source,
            "manifest": m.to_dict(), "manifestStatus": status,
            "digest": m.digest(),
            "receivedAt": (stored or {}).get("receivedAt"),
            "allowedVerbs": allowed}


def _opt_timeout(params: dict) -> Optional[float]:
    t = params.get("timeout")
    return float(t) if isinstance(t, (int, float)) and t > 0 else None


async def _handle_identity_or_exec(hub: Any, op: str, params: dict) -> Any:
    """The v2 ops. Identity reads answer from ``hub.store`` (the Hub's own
    EnrollmentStore); exec goes through ``hub.router.run`` / :mod:`fs_ops`."""
    from mcp_loops import audit
    from mcp_loops.origin_proto.enrollment import device_owner
    if op == "hello":
        return {"protocol": PROTOCOL_VERSION, "ops": list(OPS),
                "enrollRoot": getattr(hub.store, "root", None)}
    if op == "audit.read":
        log = audit.DispatchAudit.for_store(hub.store.root)
        recs = log.read(limit=int(params.get("limit") or 100),
                        origin=str(params.get("origin") or ""),
                        owner=str(params.get("owner") or ""),
                        verb=str(params.get("verb") or ""),
                        decision=str(params.get("decision") or ""),
                        since=float(params.get("since") or 0.0))
        return {"records": recs, "summary": log.summary()}
    if op == "device.get":
        did = params.get("deviceId")
        if not did:
            did = enrolled_device_id(hub, _need_origin(params))
        rec = hub.store.get_device(str(did))
        if not isinstance(rec, dict):
            return {"deviceId": did, "device": None, "owner": None}
        return {"deviceId": did, "device": rec, "owner": device_owner(rec)}
    origin = _need_origin(params)
    if op == "policy.view":
        return policy_view(hub, enrolled_device_id(hub, origin), origin)
    device_id = resolve_live_device(hub, origin)
    timeout = _opt_timeout(params)
    cap = params.get("capToken") or None
    if op == "run":
        argv = params.get("argv")
        if not isinstance(argv, list) or not argv:
            raise HubControlError("params.argv must be a non-empty list")
        env = params.get("env")
        res = await hub.router.run(
            device_id, [str(a) for a in argv], stdin=params.get("stdin") or None,
            cwd=params.get("cwd") or None, env=env if isinstance(env, dict) else None,
            timeout=timeout, cap_token=cap)
        out = dict(res) if isinstance(res, dict) else {"result": res}
    else:
        from mcp_loops.origin_proto import fs_ops
        path = params.get("path")
        if not isinstance(path, str) or not path:
            raise HubControlError("params.path is required")
        if op == "fs.pull":
            out = dict(await fs_ops.fs_pull(hub.router, device_id, path,
                                            timeout=timeout, cap_token=cap))
            out["dataB64"] = base64.b64encode(out.pop("data", b"") or b"").decode("ascii")
        else:  # fs.push
            data = base64.b64decode(params.get("dataB64") or "")
            out = dict(await fs_ops.fs_push(hub.router, device_id, path, data,
                                            timeout=timeout, cap_token=cap))
    out.update(routed=True, origin=origin, deviceId=device_id, via="origin-channel")
    return out


async def _handle_op(hub: Any, op: str, params: dict) -> Any:
    from mcp_loops.origin_proto import control_plane
    if op in OPS_V2:
        return await _handle_identity_or_exec(hub, op, params)
    if op == "ping":
        return {"ok": True, "live": list(hub.registry.live_ids())}
    if op == "snapshot":
        return control_plane.hub_snapshot(hub, mirror_root=None)
    origin = _need_origin(params)
    if op == "events":
        return list(hub.consolidator.events_for(origin))
    device_id = resolve_live_device(hub, origin)
    if op == "resolve":
        return {"deviceId": device_id}
    if op == "probe":
        res = await hub.router.call(device_id, "capabilities.probe")
        out = dict(res) if isinstance(res, dict) else {"ok": False, "result": res}
        out.update(origin=origin, deviceId=device_id, via="origin-channel")
        return out
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise HubControlError("params.name is required")
    if op == "status":
        res = await hub.router.call(device_id, "loop.status",
                                    {"name": name, "tail": int(params.get("tail", 10))})
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.update(origin=origin, via="origin-channel")
        return out
    if op == "stop":
        res = await hub.router.stop(device_id, name)
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("name", name)
        out.update(routed=True, origin=origin, deviceId=device_id,
                   via="origin-channel")
        return out
    if op == "start":
        config = params.get("config")
        did = str(uuid.uuid4())
        res = await hub.router.start(
            device_id, name, slug=params.get("slug") or None, dispatch_id=did,
            config=config if isinstance(config, dict) else None)
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("name", name)
        out.update(routed=True, dispatchId=did, origin=origin, deviceId=device_id)
        return out
    raise HubControlError(f"unknown op {op!r}")


def _error_reply(rid: Any, exc: BaseException) -> dict:
    return {"id": rid, "v": PROTOCOL_VERSION, "ok": False,
            "type": type(exc).__name__, "code": getattr(exc, "code", None),
            "error": str(exc)}


async def serve_control(hub: Any, path: str) -> asyncio.AbstractServer:
    """Bind the control socket at ``path`` (0600; a stale SOCKET there is
    replaced, any other file is refused) and serve it on the running loop.
    Only the Hub's own uid may talk to it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeError(f"refusing to replace non-socket {path!r}")
        os.unlink(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    me = os.getuid()

    async def on_client(reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
        try:
            uid = _peer_uid(writer)
            if uid is not None and uid != me:
                writer.write(json.dumps({"ok": False, "type": "PermissionError",
                                         "error": "peer uid refused"}).encode() + b"\n")
                return
            while True:
                line = await reader.readline()
                if not line:
                    return
                rid = None
                try:
                    req = json.loads(line)
                    rid = req.get("id")
                    op = req.get("op")
                    ver = req.get("v", 1)
                    if not isinstance(ver, int) or ver > PROTOCOL_VERSION:
                        raise HubProtocolError(
                            f"control protocol v{ver!r} not supported "
                            f"(this Hub speaks v{PROTOCOL_VERSION})")
                    if op not in OPS:
                        raise HubControlError(f"unknown op {op!r}")
                    params = req.get("params") or {}
                    if not isinstance(params, dict):
                        raise HubControlError("params must be an object")
                    reply = {"id": rid, "v": PROTOCOL_VERSION, "ok": True,
                             "result": await _handle_op(hub, op, params)}
                except Exception as exc:  # noqa: BLE001 — every error is a reply
                    reply = _error_reply(rid, exc)
                writer.write(json.dumps(reply, default=str).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    old = os.umask(0o177)
    try:
        srv = await asyncio.start_unix_server(on_client, path=path, limit=MAX_LINE)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return srv


# ── engine side: the client ──────────────────────────────────────────────────
def _raise_remote(reply: dict, op: str = "") -> None:
    from mcp_loops.origin_proto.channel import ChannelError, RpcRefused
    typ, msg, code = reply.get("type"), reply.get("error") or "", reply.get("code")
    if typ == "HubProtocolError" or (
            op in OPS_V2 and "v" not in reply and "unknown op" in msg):
        # a pre-v2 hub_serve (no ``v`` in its replies) does not know this op
        raise HubProtocolError(
            f"origin hub speaks an older control protocol than {op!r} needs "
            f"(restart hub_serve to pick up protocol v{PROTOCOL_VERSION}): {msg}")
    if typ == "DispatchUnavailable":
        raise DispatchUnavailable(msg)
    if typ == "RpcRefused":
        # re-raise with the SAME message so ProjectNotAllowed classifies as policy
        exc = RpcRefused(code, None)
        exc.args = (msg,)
        raise exc
    if typ in ("ChannelError", "TimeoutError", "ConnectionError"):
        raise ChannelError(msg)
    if typ in ("PermissionError", "PolicyDenied", "ProjectNotAllowed"):
        raise PermissionError(msg)
    raise HubControlError(f"{typ}: {msg}")


class HubServeDispatch:
    """Engine-side client of a hub_serve control socket (sync, one connection per
    call — the MCP tool thread is synchronous). Remote-origin ops only."""

    origin_id = "local"      # this engine is never one of the Hub's remote origins
    # the enrollment store lives in the Hub process and is NEVER opened here —
    # identity/audit/policy reads go over the v2 ops (device_get / policy_view /
    # audit_read) so the engine answers from the ONE authoritative store (H2).
    store = None

    def __init__(self, path: str, *, rpc_timeout: float = 30.0):
        self.path = path
        self._rpc_timeout = float(rpc_timeout)

    def _call(self, op: str, params: Optional[dict] = None, *,
              timeout: Optional[float] = None) -> Any:
        wait = timeout if timeout is not None else self._rpc_timeout + 5.0
        req = {"id": str(uuid.uuid4()), "v": PROTOCOL_VERSION, "op": op,
               "params": params or {}}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(wait)
                s.connect(self.path)
                s.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > MAX_LINE:
                        raise HubControlError("control reply too large")
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise DispatchUnavailable(f"origin hub control socket is down: {exc}")
        if not buf:
            raise DispatchUnavailable("origin hub closed the control socket")
        reply = json.loads(buf)
        if not reply.get("ok"):
            _raise_remote(reply, op)
        return reply.get("result")

    def _exec_block(self, timeout: Optional[float]) -> Optional[float]:
        return None if timeout is None else float(timeout) + self._rpc_timeout + 5.0

    # ── v2: protocol + identity reads (the Hub's own EnrollmentStore) ─────────
    def hello(self) -> dict:
        """``{protocol, ops, enrollRoot}`` of the Hub; raises
        :class:`HubProtocolError` against a pre-v2 hub_serve."""
        return self._call("hello", timeout=5.0)

    def device_get(self, device_id: str) -> dict:
        """``{deviceId, device, owner}`` from the Hub's EnrollmentStore
        (``device``/``owner`` None for a device the Hub never enrolled)."""
        return self._call("device.get", {"deviceId": device_id}, timeout=5.0)

    def policy_view(self, origin: Optional[str] = None) -> dict:
        if not origin or origin in ("local", "LOCAL"):
            raise DispatchUnavailable("the hub_serve bridge carries remote origins only")
        return self._call("policy.view", {"origin": origin}, timeout=10.0)

    def audit_read(self, *, limit: int = 100, origin: str = "", owner: str = "",
                   verb: str = "", decision: str = "", since: float = 0.0) -> dict:
        """``{records, summary}`` from the Hub's ``dispatch-audit.jsonl``."""
        return self._call("audit.read", {
            "limit": limit, "origin": origin, "owner": owner, "verb": verb,
            "decision": decision, "since": since}, timeout=10.0)

    # ── v2: exec (H1 step 1) — same router.run choke point as the in-process hub
    def dispatch_run(self, argv: Any, *, origin: Optional[str] = None,
                     stdin: Optional[str] = None, cwd: Optional[str] = None,
                     env: Optional[dict] = None, timeout: Optional[float] = None,
                     cap_token: Optional[str] = None, **_: Any) -> dict:
        return self._call("run", {
            "origin": origin or "", "argv": list(argv), "stdin": stdin,
            "cwd": cwd, "env": env, "timeout": timeout, "capToken": cap_token},
            timeout=self._exec_block(timeout))

    def dispatch_fs_pull(self, remote_path: str, *, origin: Optional[str] = None,
                         timeout: Optional[float] = None,
                         cap_token: Optional[str] = None) -> dict:
        return self._call("fs.pull", {
            "origin": origin or "", "path": remote_path, "timeout": timeout,
            "capToken": cap_token}, timeout=self._exec_block(timeout))

    def dispatch_fs_push(self, remote_path: str, data: Any, *,
                         origin: Optional[str] = None,
                         timeout: Optional[float] = None,
                         cap_token: Optional[str] = None) -> dict:
        raw = data.encode() if isinstance(data, str) else bytes(data or b"")
        return self._call("fs.push", {
            "origin": origin or "", "path": remote_path,
            "dataB64": base64.b64encode(raw).decode("ascii"),
            "timeout": timeout, "capToken": cap_token},
            timeout=self._exec_block(timeout))

    def is_live(self) -> bool:
        try:
            return bool(self._call("ping", timeout=5.0).get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def dispatch_start(self, name: str, slug: str = "") -> dict:
        raise DispatchUnavailable("the hub_serve bridge carries remote origins only")

    def resolve_device(self, origin: Optional[str] = None) -> str:
        return self._call("resolve", {"origin": origin or ""})["deviceId"]

    def dispatch_start_remote(self, name: str, origin: str, *, slug: str = "",
                              config: Optional[dict] = None) -> dict:
        return self._call("start", {"name": name, "origin": origin,
                                    "slug": slug, "config": config})

    def dispatch_stop_remote(self, name: str, origin: str) -> dict:
        return self._call("stop", {"name": name, "origin": origin})

    def dispatch_status_remote(self, name: str, origin: str, *,
                               tail: int = 10) -> dict:
        return self._call("status", {"name": name, "origin": origin, "tail": tail})

    def dispatch_capabilities_probe(self, origin: Optional[str] = None, *,
                                    timeout: Optional[float] = None) -> dict:
        return self._call("probe", {"origin": origin or ""},
                          timeout=(timeout + 5.0) if timeout else None)

    def remote_events(self, origin: str) -> list:
        try:
            return list(self._call("events", {"origin": origin}, timeout=10.0))
        except Exception:  # noqa: BLE001
            return []

    def consolidated_snapshot(self, *, mirror_root: Optional[str] = None) -> dict:
        try:
            snap = self._call("snapshot", timeout=10.0)
        except Exception:  # noqa: BLE001 — Hub down ⇒ no live rows from it
            return {"origins": [], "loops": []}
        return snap if isinstance(snap, dict) else {"origins": [], "loops": []}
