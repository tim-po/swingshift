"""origin_wire — the shared, language-neutral origin protocol library.

Phase A / **M1 logical packaging** (HUB-ENGINE-SPLIT-SPEC §1c, §4 M1). This is a
*thin re-export shim* over the in-place ``mcp_loops.origin_proto`` transport
modules — the same zero-import-churn pattern ``mcp_loops.projects`` already uses
to re-export ``mcp_loops.products`` (``projects.py:1-18``). **No code moves in
Phase A**: nothing is relocated on disk, no process splits, ``:8771`` is
untouched. The shim only draws the *logical* package boundary so future code can
write ``from origin_wire import wire`` / ``import origin_wire.channel`` instead of
reaching into ``mcp_loops.origin_proto`` — and so the M1 static check has a
concrete package to assert the boundary on.

Each name below is aliased to the SAME module object as its ``mcp_loops`` source
(``origin_wire.wire is mcp_loops.origin_proto.wire`` → ``True``), so identity,
``isinstance`` and singleton state are all preserved — a re-export, not a copy.

BOUNDARY RULE (spec §1c, §4 M1 acceptance #4): ``origin_wire`` carries **no**
engine or Hub policy — pure transport/protocol. It must **never** import from
``loops_engine`` / ``mcp_loops.server`` or from the Hub façade
(``hub`` / ``mcp_loops.origin_proto.control_plane`` / ``devices`` / ``dispatch``
/ ``origins`` / ``origin_service``). ``mcp_loops.tests.test_package_boundaries``
statically enforces this. ``agent_core`` (engine side) and ``control_plane``
(Hub side) both sit ON TOP of this library and import IT, never the reverse.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys

# local shim name -> real backing module (all pure transport, no engine/hub edge)
_REEXPORTS = {
    "wire": "mcp_loops.origin_proto.wire",            # typed RPC + event frames, method vocab
    "channel": "mcp_loops.origin_proto.channel",      # ChannelClient (dial-out) + ChannelServer (accept/relay)
    "tls": "mcp_loops.origin_proto.tls",              # mTLS material / ssl contexts
    "wsio": "mcp_loops.origin_proto.wsio",            # raw asyncio WebSocket transport
    "enrollment": "mcp_loops.origin_proto.enrollment",  # enrollment store + action constants (public keys only)
    "identity": "mcp_loops.origin_proto.identity",    # device Ed25519 keypair primitive (pure crypto)
    "fs_ops": "mcp_loops.origin_proto.fs_ops",        # remote fs primitive over the one exec path
    "loopback": "mcp_loops.origin_proto.loopback",    # in-process loopback transport (single-process / tests)
}


def _install() -> None:
    """Alias each backing module under this package name — identity-preserving.

    Registering the real module in ``sys.modules`` under ``origin_wire.<name>``
    (in addition to binding it as a package attribute) makes every import form
    resolve to the same object: ``import origin_wire.wire``,
    ``from origin_wire.wire import Frame``, ``from origin_wire import wire`` and
    ``origin_wire.wire`` all reach ``mcp_loops.origin_proto.wire``.
    """
    for _local, _target in _REEXPORTS.items():
        _mod = _importlib.import_module(_target)
        _sys.modules[f"{__name__}.{_local}"] = _mod
        globals()[_local] = _mod


_install()

# convenience top-level constant, mirrored from the wire module
PROTOCOL_VERSION = globals()["wire"].PROTOCOL_VERSION

__all__ = [*_REEXPORTS.keys(), "PROTOCOL_VERSION"]
