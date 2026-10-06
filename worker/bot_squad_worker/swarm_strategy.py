"""Per-project swarm strategy — runtime knobs the operator/dashboard
can tune without touching code.

Stored as ``data/<slug>/strategy.toml``. Values not present in the file
fall through to ``DEFAULT_STRATEGY``. Unknown keys are rejected on
write so typos surface immediately instead of silently doing nothing.

For now the strategy is a flat dict. As more knobs accumulate, group
them into TOML sub-tables — but resist that until the count justifies
it.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


DEFAULT_STRATEGY: dict[str, Any] = {
    # Auto-sleep: when a live expert's recent transcript bytes (jsonl
    # size past sleep_mark) exceeds the threshold, the scheduled
    # autosleep tick fires sleep_and_respawn. Off by default — opt in
    # once you trust the scribe output for this project.
    "auto_sleep_enabled": False,
    "auto_sleep_threshold_bytes": 200_000,
    "auto_sleep_min_interval_sec": 600,
    # When the autosleep tick fires sleep, follow with a respawn to
    # actually free the pane's live context. If False, only memory is
    # written; pane keeps its bloated context until manually respawned.
    "auto_sleep_respawn": True,
    # Whether the scheduled autosleep tick covers the coordinator
    # itself. Coordinator sleep is more disruptive (it's the user's
    # main surface) so default False.
    "auto_sleep_includes_coordinator": False,
}

# Allowed keys + their python types. Used for both load-coercion and
# write-validation. Floats are accepted where int is expected (TOML
# numbers).
_SCHEMA: dict[str, type | tuple[type, ...]] = {
    "auto_sleep_enabled": bool,
    "auto_sleep_threshold_bytes": int,
    "auto_sleep_min_interval_sec": int,
    "auto_sleep_respawn": bool,
    "auto_sleep_includes_coordinator": bool,
}


def _strategy_path(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "strategy.toml"


def _validate(partial: dict, *, allow_missing: bool = True) -> dict:
    """Validate a partial strategy dict. Returns the (possibly coerced)
    valid dict. Raises ActionError on unknown keys or wrong types.
    """
    from bot_squad_worker.actions import ActionError
    out: dict[str, Any] = {}
    for k, v in partial.items():
        if k not in _SCHEMA:
            raise ActionError(f"strategy: unknown key {k!r}; allowed: {sorted(_SCHEMA)}")
        expected = _SCHEMA[k]
        if expected is bool:
            if not isinstance(v, bool):
                raise ActionError(f"strategy: {k!r} must be bool, got {type(v).__name__}")
            out[k] = v
        elif expected is int:
            # TOML emits ints; accept python bools cast accidentally? No
            # — bool is a subtype of int but we explicitly reject it for
            # int fields to avoid silently storing True as 1.
            if isinstance(v, bool) or not isinstance(v, int):
                raise ActionError(f"strategy: {k!r} must be int, got {type(v).__name__}")
            if v < 0:
                raise ActionError(f"strategy: {k!r} must be >= 0, got {v}")
            out[k] = v
        else:  # pragma: no cover - schema only has bool/int today
            if not isinstance(v, expected):
                raise ActionError(f"strategy: {k!r} type mismatch")
            out[k] = v
    return out


def read_strategy(cfg: Any, slug: str) -> dict:
    """Return the effective strategy for ``slug`` — DEFAULT_STRATEGY
    merged with whatever's in strategy.toml (file wins for present
    keys). Missing file returns the defaults.
    """
    if slug not in cfg.projects:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"read_strategy: unknown slug {slug!r}")
    path = _strategy_path(cfg, slug)
    merged = dict(DEFAULT_STRATEGY)
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, OSError) as e:
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"read_strategy: parse error: {e}")
        # Only honour keys we know; ignore unknown to be forgiving on
        # read (write rejects unknowns; this lets a future-old worker
        # read a future-new strategy.toml without erroring).
        for k, v in raw.items():
            if k in _SCHEMA:
                merged[k] = v
    return merged


def write_strategy(cfg: Any, slug: str, partial: dict) -> dict:
    """Update strategy.toml with ``partial`` (keys merged onto current
    effective strategy). Returns the new effective strategy.

    Empty dict is a valid no-op. Pass a single key to update one knob
    without touching others.
    """
    from bot_squad_worker.actions import ActionError
    if slug not in cfg.projects:
        raise ActionError(f"write_strategy: unknown slug {slug!r}")
    validated = _validate(partial)
    current = read_strategy(cfg, slug)
    current.update(validated)
    # Persist only the keys that differ from defaults so the file stays
    # readable. (No "everything defaulted" lines.)
    persisted = {k: v for k, v in current.items() if v != DEFAULT_STRATEGY[k]}
    path = _strategy_path(cfg, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_to_toml(persisted))
    return current


def _to_toml(d: dict) -> str:
    """Tiny TOML emitter for our flat bool/int dict. Avoids pulling in a
    write library; tomllib in stdlib is read-only.
    """
    lines: list[str] = []
    for k in sorted(d):
        v = d[k]
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        else:
            lines.append(f"{k} = {v}")
    return "\n".join(lines) + ("\n" if lines else "")
