"""origin_service — G2.3: enroll THIS box (the VPS) as a first-class origin.

The owner's north star: the VPS is an origin like any other — it connects to the
hub through the SAME protocol, and its dashboard-serving role is incidental. This
module brings the box up as a LIVE enrolled origin over the origin protocol:

  * a stable device identity (Ed25519 keypair, 0600 keystore under the data dir —
    the box keeps ONE identity across restarts);
  * an in-process :class:`~mcp_loops.origin_proto.dispatch.LocalOriginService`
    (hub + local origin-agent) that dials the hub, enrolls as origin ``local``,
    reports capabilities (authed CLIs, cores, RAM, load), heartbeats, and pushes
    events — replacing the rsync-mirror freshness for this origin;
  * :func:`mcp_loops.server.install_origin_dispatch`, so ``loop_start`` now ROUTES
    through this enrolled origin's own engine (G2.2) instead of the hardcode.

Execution ALWAYS stays on the origin: the agent's engine is an in-process adapter
(:class:`InProcessEngine`) that drives the SAME server tools/launcher, so no work
is copied and no second socket hop is added; the origin-agent reaches the engine
directly. The private key never leaves the box; the hub holds only the PUBLIC key.

Bring-up is OPT-IN (env ``LOOPYARD_ORIGIN_AGENT``): default OFF keeps every
existing deploy byte-for-byte unchanged, and the agent can never wedge a server
that didn't opt in. When on, the server starts it at boot (inheriting the
server's own new-session detach from A2 — the agent lives in the server process,
so it survives the launching shell exactly as the server does) and records an
``origin-agent`` entry so ``yard status`` shows it alongside server/worker/poller.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """Whether the origin-agent should be brought up (opt-in, default OFF)."""
    return os.environ.get("LOOPYARD_ORIGIN_AGENT", "").strip().lower() in _TRUTHY


# ── M3: the three Hub modes, decided by env (default OFF = standalone §3) ─────
# The SINGLE place the split's standalone guarantee is expressed as a decision.
# The engine dials a Hub ONLY in the two non-OFF modes; default deploy is OFF, so
# loop_start fails OPEN to _loop_start_local and the engine runs loops with no Hub
# (HUB-ENGINE-SPLIT-SPEC §3, §4 M3). See :mod:`mcp_loops.hub_serve` for the Hub
# runnable the REMOTE mode dials.
HUB_OFF = "off"        # neither env set — standalone, no Hub, pre-split path
HUB_LOCAL = "local"    # LOOPYARD_ORIGIN_AGENT — in-process loopback Hub (M0–M2)
HUB_REMOTE = "remote"  # LOOPYARD_ORIGIN_HUB_URL — dial an external hub_serve daemon


def hub_url() -> Optional[str]:
    """The external Hub URL to dial, or None. Set → the engine reaches a Hub
    daemon (like :mod:`mcp_loops.hub_serve`) over the origin_wire ChannelClient
    (via :mod:`mcp_loops.origin_client`), instead of the in-process loopback Hub."""
    v = os.environ.get("LOOPYARD_ORIGIN_HUB_URL", "").strip()
    return v or None


def hub_mode() -> str:
    """Resolve which Hub bring-up (if any) this deploy opted into. An explicit
    external URL wins over the in-process agent flag; with neither set the mode is
    OFF and the engine is standalone. This is a pure function of the environment —
    the deterministic anchor the M3 acceptance and every caller share."""
    if hub_url():
        return HUB_REMOTE
    if enabled():
        return HUB_LOCAL
    return HUB_OFF


class InProcessEngine:
    """The origin-agent's engine, wired to THIS server's own tools/launcher.

    The origin-agent reaches the box's loop engine in-process (no LoopsMCP socket
    hop, no recursion): reads go to the read tools; the mutating loop.start goes
    straight to the raw launcher (``_loop_start_local``), NOT the routing tool, so
    a dispatched start executes locally instead of re-routing back through the hub.
    """

    def loop_list(self) -> dict:
        from mcp_loops import server
        return server.loop_list()

    def loop_get(self, name: str) -> dict:
        from mcp_loops import server
        return server.loop_get(name)

    def loop_save(self, config: dict) -> dict:
        from mcp_loops import server
        return server.loop_save(config)

    def loop_status(self, name: str, tail: Optional[int] = None) -> dict:
        from mcp_loops import server
        return server.loop_status(name, tail=tail if tail is not None else 10)

    def loop_start(self, name: str, slug: Optional[str] = None,
                   dispatch_id: Optional[str] = None) -> dict:
        from mcp_loops import server
        # the RAW local launcher — dispatched work runs HERE, keyed on the id.
        return server._loop_start_local(name, slug or "", dispatch_id=dispatch_id)

    def loop_stop(self, name: str) -> dict:
        from mcp_loops import server
        return server.loop_stop(name)

    def call(self, tool: str, params: Optional[dict] = None) -> dict:
        from mcp_loops import server
        if tool == "loop_product_list":
            return server.loop_product_list(**(params or {}))
        raise RuntimeError(f"origin-agent engine: unsupported tool {tool!r}")


def origin_state_dir(data_dir: Optional[str] = None) -> str:
    """Where this box's origin identity + enrollment live: a ``_origin`` dir under
    the local mirror (prefixed ``_`` so it is never mistaken for a loop)."""
    from mcp_loops import server
    local_mirror = server._local_mirror_path()
    return os.path.join(local_mirror, "_origin")


def build_service(*, data_dir: Optional[str] = None, origin_id: str = "local"):
    """Construct (but do not start) the box's LocalOriginService with a persistent
    device identity + enrollment store under the data dir. Separated from
    :func:`start` so tests can build against a tmp dir + fake engine."""
    from mcp_loops.origin_proto import identity
    from mcp_loops.origin_proto.agent_core import RunAllowlist
    from mcp_loops.origin_proto.dispatch import LocalOriginService
    from mcp_loops.origin_proto.enrollment import EnrollmentStore

    root = origin_state_dir(data_dir)
    os.makedirs(root, mode=0o700, exist_ok=True)
    ident = identity.DeviceIdentity.load_or_create(
        os.path.join(root, "device.json"), label=origin_id)
    store = EnrollmentStore(os.path.join(root, "enroll"))

    # §5/§P3 — arbitrary exec (origin.run + fs.pull/fs.push) is OPT-IN per origin.
    # The allowlist lives on THIS origin's OWN disk; the Hub can never widen it. A
    # fresh install is fail-closed deny-all (empty file) — the owner opts programs
    # in (e.g. cat/tee for fs, gh, …) by editing run-allowlist.json here.
    run_allowlist = RunAllowlist(os.path.join(root, "run-allowlist.json"))

    # G2.4 — the live status-tailer readers: a dispatched loop.start streams THIS
    # loop's turn/state progress to the hub off its on-disk status.jsonl + run.json
    # (the same files the engine writes), so a connected origin's progress shows in
    # the consolidated view instead of waiting on the rsync mirror.
    def _read_status(name: str) -> list:
        from mcp_loops import report, server
        return server._read_jsonl(report.status_log(name))

    def _read_run(name: str) -> dict:
        from mcp_loops import server
        return server._read_json(server._run_path(name)) or {}

    return LocalOriginService(
        store, ident, engine=InProcessEngine(), origin_id=origin_id,
        ledger_path=os.path.join(root, "dispatch-ledger.jsonl"),
        status_reader=_read_status, run_reader=_read_run,
        run_allowlist=run_allowlist)


# module-level singleton so a second boot call is a no-op + stop() has a handle.
_SERVICE: Any = None


def start(*, data_dir: Optional[str] = None, origin_id: str = "local",
          now: Optional[float] = None, timeout: float = 10.0) -> Any:
    """Bring the origin-agent up, install it as the loop_start dispatch route, and
    record it as a tracked service. Idempotent; fail-soft — a bring-up error is
    swallowed (the server keeps serving; loop_start just stays on the local path).
    Returns the live service, or None on failure."""
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    try:
        svc = build_service(data_dir=data_dir, origin_id=origin_id)
        svc.start(timeout=timeout)
    except Exception:  # noqa: BLE001 — never let the agent sink the server
        return None
    try:
        from mcp_loops import server, services
        server.install_origin_dispatch(svc)
        # tracked so `yard status` shows it. It lives IN the server process, so it
        # shares the server's pid + lifecycle (and A2 new-session survival).
        services.record("origin-agent", pid=os.getpid(),
                        now=now if now is not None else time.time(),
                        data_dir=data_dir)
    except Exception:  # noqa: BLE001
        pass
    _SERVICE = svc
    return svc


def stop(*, data_dir: Optional[str] = None) -> None:
    global _SERVICE
    svc, _SERVICE = _SERVICE, None
    try:
        from mcp_loops import server
        server.uninstall_origin_dispatch()
    except Exception:  # noqa: BLE001
        pass
    if svc is not None:
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass
    try:
        from mcp_loops import services
        services.mark_stopped("origin-agent", data_dir)
    except Exception:  # noqa: BLE001
        pass


def hub_control_sock() -> Optional[str]:
    """The hub_serve control socket to bridge to (``LOOPYARD_HUB_CONTROL_SOCK``),
    or None. Independent of :func:`hub_mode`: it lets THIS engine reach origins
    connected to a co-located standalone Hub (prod: the /hub → hub_serve one)."""
    v = os.environ.get("LOOPYARD_HUB_CONTROL_SOCK", "").strip()
    return v or None


def maybe_install_hub_bridge() -> Any:
    """Install the engine ↔ hub_serve bridge when opted in. Lazy (dials per
    call), so a Hub that is down at boot costs nothing and never blocks serving;
    remote ops then answer with the honest 'control socket is down' reason."""
    path = hub_control_sock()
    if not path:
        return None
    try:
        from mcp_loops import server
        from mcp_loops.origin_proto.hub_control import HubServeDispatch
        bridge = HubServeDispatch(path)
        server.install_hub_bridge(bridge)
        return bridge
    except Exception:  # noqa: BLE001 — never let the bridge sink the server
        return None


def maybe_start() -> Any:
    """Bring the Hub connector up according to :func:`hub_mode` (called from the
    server's boot path). Returns the live service, or None when the engine is
    standalone or a bring-up fails soft.

      * ``HUB_OFF``   → return None: no Hub is dialed and ``loop_start`` keeps the
        pre-split ``_loop_start_local`` path (§3 standalone default).
      * ``HUB_LOCAL`` → :func:`start`: the in-process loopback Hub + local
        origin-agent (the M0–M2 path, unchanged).
      * ``HUB_REMOTE``→ the engine dials the EXTERNAL Hub daemon named by
        ``LOOPYARD_ORIGIN_HUB_URL`` over the origin_wire ChannelClient. That
        bring-up is the ``loopyard origin up --hub <url>`` engine
        (:mod:`mcp_loops.origin_client`); it is a foreground/detached client, not
        this in-process route, so we do not stand a second one up here — we only
        record that the box is in remote-Hub mode and leave ``loop_start`` on the
        local path until that client installs its own dispatch route.
    """
    maybe_install_hub_bridge()
    mode = hub_mode()
    if mode == HUB_LOCAL:
        return start()
    # HUB_OFF and HUB_REMOTE both leave the in-process dispatch seam uninstalled:
    # OFF is standalone; REMOTE is served by the origin_client daemon dialing the
    # external hub_serve Hub, not by this in-process loopback bring-up.
    return None
