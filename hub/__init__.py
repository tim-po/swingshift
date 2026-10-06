"""hub — the per-user broker: registry + origin↔origin relay, runs NO loops.

Phase A / **M1 logical packaging** (HUB-ENGINE-SPLIT-SPEC §1b, §4 M1). A *thin
re-export shim* over the in-place Hub modules — same zero-import-churn pattern as
``mcp_loops.projects`` (``projects.py``). **No code moves and no process splits
in Phase A**: the Hub façade (``OriginHub``) still lives in one process with the
engine, and — critically — the §1d Hub-*facing tools* it logically owns
(``loop_origin_list``, ``loop_fleet_summary``, ``loop_sessions_list``,
``loop_dispatch_start`` / ``loop_dispatch_stop``, ``origin_run`` …) **STAY
registered on and answered by the engine's ``:8771``** because the live dashboard
calls them there directly (§1d, §4 M0). Their physical relocation off ``:8771``
is gated to **Phase C** — this package is the logical home only.

Each name is aliased to the SAME module object as its ``mcp_loops`` source
(``hub.control_plane is mcp_loops.origin_proto.control_plane`` → ``True``).

The Hub bucket brokers between origins and NEVER copies or runs work: the
``OriginHub`` façade (ChannelServer + OriginRegistry + EventConsolidator +
DispatchRouter), the unified read-model registry (``devices``), the legacy
cross-origin dispatch envelope/outbox (``dispatch``) and rsync-mirror registry
(``origins``), and the engine↔Hub wiring (``origin_service`` +
``local_service`` = the in-process ``LocalOriginService`` convenience), and the
standalone Hub daemon (``serve`` = :mod:`mcp_loops.hub_serve`, the M3 runnable an
external engine dials).
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys

# local shim name -> real backing module (all Hub-side broker/registry surface)
_REEXPORTS = {
    # the Hub façade: OriginHub, OriginRegistry, EventConsolidator, DispatchRouter
    "control_plane": "mcp_loops.origin_proto.control_plane",
    # unified read-only Device list over boxes/sessions/origins (routes THROUGH the hub)
    "devices": "mcp_loops.devices",
    # cross-origin dispatch request envelope + outbox (legacy mirror path)
    "dispatch": "mcp_loops.dispatch",
    # legacy rsync-mirror registry (pre-fabric cross-origin mechanism, kept as fallback rung)
    "origins": "mcp_loops.origins",
    # the engine↔Hub connector: enrolls THIS box, stands up the in-process hub + local origin-agent
    "origin_service": "mcp_loops.origin_service",
    # LocalOriginService — the convenience that collapses Hub+engine into one process today
    "local_service": "mcp_loops.origin_proto.dispatch",
    # M3: the Hub façade (OriginHub) as its OWN runnable daemon — the process an
    # engine dials via ChannelClient when LOOPYARD_ORIGIN_HUB_URL is set (§4 M3).
    "serve": "mcp_loops.hub_serve",
}


def _install() -> None:
    """Alias each backing module under this package name — identity-preserving.

    See :mod:`origin_wire` for why registering in ``sys.modules`` makes every
    import form resolve to the one real module. Note ``hub.dispatch`` and
    ``hub.local_service`` intentionally map to two DIFFERENT real modules
    (``mcp_loops.dispatch`` vs ``mcp_loops.origin_proto.dispatch``) to avoid the
    ``dispatch`` name collision while exposing both under the Hub façade.
    """
    for _local, _target in _REEXPORTS.items():
        _mod = _importlib.import_module(_target)
        _sys.modules[f"{__name__}.{_local}"] = _mod
        globals()[_local] = _mod


_install()

__all__ = list(_REEXPORTS.keys())
