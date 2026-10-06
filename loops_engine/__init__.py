"""loops_engine — the per-origin engine: runs loops, single authority for state.

Phase A / **M1 logical packaging** (HUB-ENGINE-SPLIT-SPEC §1a, §4 M1). A *thin
re-export shim* over the in-place ``mcp_loops.*`` engine modules — the same
zero-import-churn pattern ``mcp_loops.projects`` already uses (``projects.py``).
**No code moves in Phase A** and **the engine still answers on ``:8771`` in one
process** (§4 M1): this package only draws the *logical* boundary so future code
can write ``from loops_engine import server`` / ``import loops_engine.workspaces``
instead of ``mcp_loops.*``.

Each name is aliased to the SAME module object as its ``mcp_loops`` source
(``loops_engine.server is mcp_loops.server`` → ``True``) — a re-export, not a
copy, so the in-memory run registry (``server._RUNS``, the origin's single state
authority) stays one object in one process.

The engine bucket is everything that runs / persists a loop locally: the FastMCP
server + registry, the runner core, session/team-room/thread/report/poll state,
the workspace primitive, config validate/render/authoring, and the origin's
local data authority (projects, issues, docs, capabilities). It also ships the
engine's own remote-facing adapter (``agent_core`` = ``OriginAgentExecutor``) and
the device ``identity`` half — *that is how an origin exposes itself*; the Hub
façade is NOT part of this package (see :mod:`hub`).
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys

# local shim name -> real backing module (all engine-local, single-authority)
_REEXPORTS = {
    # FastMCP server = the origin's local API + the in-memory run registry (_RUNS)
    "server": "mcp_loops.server",
    # loop execution core: LoopRunner, Guardian, FilePersistence, SubloopDispatcher
    "runner": "mcp_loops.runner",
    "headless": "mcp_loops.headless",          # HeadlessSubstrate — spawns worker-daemon sessions
    # local loop state
    "sessions": "mcp_loops.sessions",
    "teamroom": "mcp_loops.teamroom",
    "threads": "mcp_loops.threads",
    "report": "mcp_loops.report",
    "poller": "mcp_loops.poller",
    # per-origin workspace primitive (provision/reap worktrees) + yard supervision
    "workspaces": "mcp_loops.workspaces",
    "workspace": "mcp_loops.workspace",
    "workspace_agents": "mcp_loops.workspace_agents",
    "yard": "mcp_loops.yard",
    # config validate / render / authoring — local
    "schema": "mcp_loops.schema",
    "render": "mcp_loops.render",
    "authoring": "mcp_loops.authoring",
    "creator": "mcp_loops.creator",
    "creator_context": "mcp_loops.creator_context",
    "creator_launch": "mcp_loops.creator_launch",
    "agents": "mcp_loops.agents",
    "new": "mcp_loops.new",
    # the origin's local data authority
    "projects": "mcp_loops.projects",
    "products": "mcp_loops.products",
    "products_gather": "mcp_loops.products_gather",
    "issues": "mcp_loops.issues",
    "resolution": "mcp_loops.resolution",
    "receipts": "mcp_loops.receipts",
    "capabilities": "mcp_loops.capabilities",
    "ideahub": "mcp_loops.ideahub",
    "loopyard": "mcp_loops.loopyard",
    # data-dir resolution, runner binding, detach, diagnostics, local client
    "paths": "mcp_loops.paths",
    "runner_registry": "mcp_loops.runner_registry",
    "detach": "mcp_loops.detach",
    "standalone": "mcp_loops.standalone",
    "doctor": "mcp_loops.doctor",
    "services": "mcp_loops.services",
    "client": "mcp_loops.client",
    # the engine's OWN remote-facing adapter + device-key half (§1a): how an
    # origin exposes ITSELF to a Hub — engine side, sits on top of origin_wire.
    "agent_core": "mcp_loops.origin_proto.agent_core",
    "identity": "mcp_loops.origin_proto.identity",
}


def _install() -> None:
    """Alias each backing module under this package name — identity-preserving.

    See :mod:`origin_wire` for why registering in ``sys.modules`` makes every
    import form (``import loops_engine.server``, ``from loops_engine.server
    import ...``, ``loops_engine.server``) resolve to the one real module.
    """
    for _local, _target in _REEXPORTS.items():
        _mod = _importlib.import_module(_target)
        _sys.modules[f"{__name__}.{_local}"] = _mod
        globals()[_local] = _mod


_install()

__all__ = list(_REEXPORTS.keys())
