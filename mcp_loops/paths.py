"""Install-root & data-dir resolution — the single source of truth for WHERE a
Loopyard install keeps its state.

Round-3 A1 (pilot BLOCKER): a fresh user who had never heard of *bot-swarm* ran
``yard up`` and it wrote ``~/bot-swarm/data/_loops/...`` and leaked that path in
errors. The hardcoded ``~/bot-swarm`` default is a relic of the ONE original box
the package used to live on; on any other machine it is both wrong and baffling.

The install is relocatable — ``import mcp_loops`` resolves by CWD/path — so the
one location we can always trust is *this file's own path*. ``install_root()``
derives the directory that CONTAINS the ``mcp_loops`` package (the clone/venv
root); the default data dir is ``<install_root>/data/_loops``. A user override is
still honoured first via an explicit argument, then ``$LOOPS_DATA_DIR``.

Why this is regression-free on the original box: there ``mcp_loops`` lives under
``/home/.../bot-swarm/``, so ``install_root()`` resolves to that same directory
and the default data dir is byte-identical to the old hardcoded one. A fresh
clone elsewhere simply gets its own self-contained root instead of reaching into
a ``~/bot-swarm`` that does not exist.
"""

from __future__ import annotations

import os
from pathlib import Path

# ``<install_root>/mcp_loops/paths.py`` → ``parents[1]`` is ``<install_root>``,
# the directory that contains the importable ``mcp_loops`` package.
INSTALL_ROOT = Path(__file__).resolve().parents[1]

ENV_DATA_DIR = "LOOPS_DATA_DIR"
# Phase-B D12: the STATE root, separate from the (read-only) code tree. Unset ⇒
# the install root, so every resolver below returns exactly its old value.
ENV_HOME = "LOOPYARD_HOME"
ENV_CONFIG_DIR = "LOOPYARD_CONFIG_DIR"
ENV_INSTALL = "LOOPYARD_INSTALL"
# The complete set of names ``yard start`` may create at the state root (and the
# set ``install.sh`` preserves across an upgrade).
STATE_DIRS = ("config", "data", "workspace")


def install_root() -> Path:
    """The directory that contains the ``mcp_loops`` package — the clone/venv
    root this install was started from. Derived from ``__file__`` so it is
    correct wherever the install was unpacked, with no ``~/bot-swarm`` guess."""
    return INSTALL_ROOT


def web_dist() -> Path:
    """The built web app (/app/) the dashboard serves: ``$LOOPYARD_WEB_DIST``,
    else a source checkout's ``frontend/apps/web/dist`` when built, else a
    tarball install's ``<install_root>/web`` (B-3, where ``--web-dist`` puts it)."""
    env = os.environ.get("LOOPYARD_WEB_DIST")
    if env:
        return Path(env)
    root = install_root()
    repo = root / "frontend" / "apps" / "web" / "dist"
    return repo if (repo / "index.html").is_file() else root / "web"


def state_root() -> Path:
    """Where mutable state (config/, data/, workspace/) lives:
    ``$LOOPYARD_HOME`` when set, else the install root (D12)."""
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    return INSTALL_ROOT


def config_dir(explicit: str | os.PathLike | None = None) -> Path:
    """The ONE config resolver (R14/R24), feeding every ``projects.toml`` reader:

        explicit > $LOOPYARD_CONFIG_DIR > $LOOPYARD_HOME/config
                 > $LOOPYARD_INSTALL/config > <install>/config
    """
    if explicit:
        return Path(explicit).expanduser()
    for env, suffix in ((ENV_CONFIG_DIR, None), (ENV_HOME, "config"),
                        (ENV_INSTALL, "config")):
        val = os.environ.get(env)
        if val:
            base = Path(val).expanduser()
            return base / suffix if suffix else base
    return INSTALL_ROOT / "config"


_WARNED_LEGACY_HOME: set[str] = set()


def legacy_home_override(who: str) -> Path | None:
    """R6/R17: ``$BOT_SQUAD_HOME`` as a deprecated state-root alias. Returns
    it (and prints one stderr line per process per caller) when set, else None
    — callers then resolve state through :func:`resolve_data_dir`."""
    val = os.environ.get("BOT_SQUAD_HOME")
    if not val:
        return None
    if who not in _WARNED_LEGACY_HOME:
        _WARNED_LEGACY_HOME.add(who)
        import sys
        print(f"[{who}] BOT_SQUAD_HOME is deprecated — set LOOPS_DATA_DIR or "
              "LOOPYARD_HOME instead", file=sys.stderr)
    return Path(val)


def default_data_dir() -> str:
    """The loops data root when the user set no override:
    ``<state_root>/data/_loops`` (== ``<install>/data/_loops`` unless
    ``$LOOPYARD_HOME`` is set). Install-relative so a fresh clone is
    self-contained and never touches a ``~/bot-swarm`` it doesn't own."""
    return str(state_root() / "data" / "_loops")


def resolve_data_dir(data_dir: str | None = None) -> str:
    """Canonical data-root precedence used across the whole stack:

        explicit arg  >  $LOOPS_DATA_DIR  >  <state_root>/data/_loops

    Always returns an absolute path. This is the ONE place the precedence lives;
    every module (server, report, yard, runner_registry, doctor, receipts) reads
    through it so a fresh install has exactly one place its state can land."""
    if data_dir:
        return os.path.abspath(data_dir)
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return os.path.abspath(env)
    return os.path.abspath(default_data_dir())


def sock_dir(data_dir: str | None = None) -> str:
    """The ``_sock`` dir (holds the worker-daemon socket) — a sibling of the
    ``_loops`` data root, resolved through the same precedence."""
    return os.path.join(os.path.dirname(resolve_data_dir(data_dir)), "_sock")
