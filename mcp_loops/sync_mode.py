"""sync_mode — the unified sync engine's kill-switch and rail flags (spec §7.2).

Every value is read from the environment AT CALL TIME, never at import, so a
process that reloads its env flips without a restart; otherwise the operator
restarts it (never a loop). Unknown values fail SAFE: an unrecognised mode is
``off`` and an unrecognised flag keeps its default, and :func:`sync_state`
reports what was ignored so a typo is visible instead of silent.

  LOOPS_SYNC_MODE       off (default) | shadow | primary | exclusive
  LOOPS_RAIL_MIRROR     on (default) | off — honoured only in ``primary``
  LOOPS_RAIL_EVENTS     on (default) | off — honoured only in ``primary``
  LOOPS_SYNC_WRITEBACK  off (default) | on  — S-6 config write-back gate
  LOOPS_S0_INVENTORY    on (default) | off  — S-0 loop.list inventory

The rails (R1 mirror, R2 events) always serve the read path in ``off`` and
``shadow``, and are never consulted in ``exclusive``. R3 (heartbeat) has no flag:
it is kept as presence (D9).

S-0 is phase 0 of the migration and the permanent v1-peer path (D10), so its
inventory polling is ON in every mode, ``off`` included — ``off`` restores
*post-S-0* behaviour (the G-1 golden is re-baselined once at S-0).
``LOOPS_S0_INVENTORY=off`` is its own operator rollback, independent of the
sync engine.

Until the engine exists (S-2), :func:`effective_mode` reports ``off`` whatever
is configured, so ``exclusive`` can never blank the read path by turning the
rails off with nothing behind them.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

ENV_MODE = "LOOPS_SYNC_MODE"
ENV_RAIL_MIRROR = "LOOPS_RAIL_MIRROR"
ENV_RAIL_EVENTS = "LOOPS_RAIL_EVENTS"
ENV_WRITEBACK = "LOOPS_SYNC_WRITEBACK"
ENV_S0_INVENTORY = "LOOPS_S0_INVENTORY"

OFF = "off"
SHADOW = "shadow"
PRIMARY = "primary"
EXCLUSIVE = "exclusive"
MODES = (OFF, SHADOW, PRIMARY, EXCLUSIVE)

RAIL_MIRROR = "mirror"
RAIL_EVENTS = "events"
_RAIL_ENV = {RAIL_MIRROR: ENV_RAIL_MIRROR, RAIL_EVENTS: ENV_RAIL_EVENTS}

_ON = ("on", "1", "true", "yes")
_OFF = ("off", "0", "false", "no")


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _raw(name: str, env: Optional[Mapping[str, str]]) -> str:
    return str(_env(env).get(name, "") or "").strip().lower()


def _flag(name: str, default: bool, env: Optional[Mapping[str, str]]) -> bool:
    v = _raw(name, env)
    if v in _ON:
        return True
    if v in _OFF:
        return False
    return default


def sync_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """The configured mode; empty or unknown → ``off`` (fail safe)."""
    v = _raw(ENV_MODE, env)
    return v if v in MODES else OFF


def effective_mode(*, engine_available: bool = False,
                   env: Optional[Mapping[str, str]] = None) -> str:
    """The mode the read path acts on: the configured mode, or ``off`` when no
    sync engine is running to serve it."""
    return sync_mode(env) if engine_available else OFF


def rail_enabled(rail: str, *, engine_available: bool = False,
                 env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether rail R1 (``mirror``) or R2 (``events``) feeds the read path."""
    if rail not in _RAIL_ENV:
        raise ValueError(f"unknown rail {rail!r}")
    mode = effective_mode(engine_available=engine_available, env=env)
    if mode == EXCLUSIVE:
        return False
    if mode == PRIMARY:
        return _flag(_RAIL_ENV[rail], True, env)
    return True


def writeback_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """S-6 gate: may the materializer write remote config edits into _loops/."""
    return _flag(ENV_WRITEBACK, False, env)


def s0_inventory_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """S-0: poll connected origins' loop.list and merge it into the read path."""
    return _flag(ENV_S0_INVENTORY, True, env)


def sync_state(*, engine_available: bool = False,
               env: Optional[Mapping[str, str]] = None) -> dict:
    """A plain-data view of every switch, plus the values that were ignored."""
    e = _env(env)
    ignored = {}
    if _raw(ENV_MODE, env) and _raw(ENV_MODE, env) not in MODES:
        ignored[ENV_MODE] = e.get(ENV_MODE)
    for name in (ENV_RAIL_MIRROR, ENV_RAIL_EVENTS, ENV_WRITEBACK,
                 ENV_S0_INVENTORY):
        v = _raw(name, env)
        if v and v not in _ON and v not in _OFF:
            ignored[name] = e.get(name)
    return {
        "mode": sync_mode(env),
        "effectiveMode": effective_mode(engine_available=engine_available,
                                        env=env),
        "engineAvailable": bool(engine_available),
        "rails": {r: rail_enabled(r, engine_available=engine_available, env=env)
                  for r in (RAIL_MIRROR, RAIL_EVENTS)},
        "writeback": writeback_enabled(env),
        "s0Inventory": s0_inventory_enabled(env),
        "ignored": ignored,
    }
