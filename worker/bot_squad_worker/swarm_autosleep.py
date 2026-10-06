"""Auto-sleep tick — periodic background job that compacts experts
whose live transcript has grown past the project's
``auto_sleep_threshold_bytes``.

Wired into the scheduler in scheduler.py. Gated by
``strategy.auto_sleep_enabled`` per project (off by default — opt in
once the scribe output is trusted for that project).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bot_squad_worker import (
    sessions as _sessions,
    sleep as _sleep,
    swarm_strategy as _strategy,
)

log = logging.getLogger("bot-squad-worker.autosleep")


def _unsleeped_bytes(cfg: Any, slug: str, sid_meta: dict) -> int | None:
    """Bytes in the session's jsonl past its role's ``sleep_mark``.
    Returns None if we can't resolve the jsonl. ``sid_meta`` is one
    record from sessions.list_sessions output.
    """
    role = sid_meta.get("role")
    if not role:
        return None
    # We need cwd + claude_uuid from the live session md. list_sessions
    # already merged that in.
    cwd = sid_meta.get("cwd")
    claude_uuid = sid_meta.get("claude_uuid")
    if not cwd or not claude_uuid:
        return None
    jsonl = _sleep._resolve_jsonl_path(
        {"cwd": cwd, "claude_uuid": claude_uuid},
        _sleep._user_home(),
    )
    if jsonl is None:
        return None
    try:
        size = jsonl.stat().st_size
    except FileNotFoundError:
        return None
    mark_path = Path(cfg.data_dir) / slug / "experts" / role / "sleep_mark"
    mark = _sleep._read_mark(mark_path)
    return max(0, size - mark)


def _last_sleep_age_sec(cfg: Any, slug: str, role: str, now: float) -> float:
    """Seconds since the role's sleep_mark was last written. Infinity if
    no mark file exists yet (never slept)."""
    import math
    mark_path = Path(cfg.data_dir) / slug / "experts" / role / "sleep_mark"
    if not mark_path.is_file():
        return math.inf
    try:
        return now - mark_path.stat().st_mtime
    except FileNotFoundError:
        return math.inf


def autosleep_tick(cfg: Any) -> dict:
    """Per-project scan; fire sleep_and_respawn (or just sleep) on every
    over-threshold session, respecting the strategy knobs.

    Returns a summary dict ``{"projects_scanned": int, "fired":
    [{slug, sid, role, ...}], "skipped": [...]}``. Useful for tests and
    operator visibility via scheduler_state.
    """
    import time

    fired: list[dict] = []
    skipped: list[dict] = []
    scanned = 0
    now = time.time()

    for slug in cfg.projects:
        scanned += 1
        try:
            strategy = _strategy.read_strategy(cfg, slug)
        except Exception as e:  # noqa: BLE001
            log.warning("autosleep: read_strategy(%s) failed: %s", slug, e)
            skipped.append({"slug": slug, "reason": f"strategy_read: {e}"})
            continue
        if not strategy.get("auto_sleep_enabled"):
            continue

        threshold = int(strategy.get("auto_sleep_threshold_bytes", 0))
        min_interval = int(strategy.get("auto_sleep_min_interval_sec", 0))
        respawn = bool(strategy.get("auto_sleep_respawn", True))
        include_coord = bool(strategy.get("auto_sleep_includes_coordinator", False))

        try:
            rows = _sessions.list_sessions(cfg, slug)
        except Exception as e:  # noqa: BLE001
            log.warning("autosleep: list_sessions(%s) failed: %s", slug, e)
            skipped.append({"slug": slug, "reason": f"list_sessions: {e}"})
            continue

        for row in rows:
            role = row.get("role")
            if not role:
                continue  # bot-squad session, ignore
            if row.get("status") != "active":
                continue  # only live panes
            if role == "coordinator" and not include_coord:
                continue
            sid = row["sid"]
            unsleeped = _unsleeped_bytes(cfg, slug, row)
            if unsleeped is None or unsleeped < threshold:
                continue
            age = _last_sleep_age_sec(cfg, slug, role, now)
            if age < min_interval:
                skipped.append({"slug": slug, "sid": sid, "role": role,
                                "reason": "min_interval", "age_sec": age})
                continue
            try:
                sleep_res = _sleep.sleep_expert(cfg, slug, sid)
            except Exception as e:  # noqa: BLE001
                log.warning("autosleep: sleep_expert(%s, %s) failed: %s", slug, sid, e)
                skipped.append({"slug": slug, "sid": sid, "role": role,
                                "reason": f"sleep_failed: {e}"})
                continue
            event = {
                "slug": slug, "sid": sid, "role": role,
                "compacted_bytes": sleep_res["compacted_bytes"],
                "respawned": False,
            }
            if respawn:
                try:
                    rs = _sessions.respawn_expert(cfg, slug, sid)
                    event["respawned"] = True
                    event["new_sid"] = rs["new_sid"]
                    event["drained_lines"] = rs["drained_lines"]
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "autosleep: respawn_expert(%s, %s) failed: %s", slug, sid, e
                    )
                    event["respawn_error"] = str(e)
            fired.append(event)

    return {"projects_scanned": scanned, "fired": fired, "skipped": skipped}
