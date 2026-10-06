"""Shared coordinator working-memory blackboard (volatile, per-project).

A tiny append-only JSONL buffer that lets the TG conversational coordinator
brain and the terminal coordinator share SHORT-TERM context. Each coordinator
instance appends its turns; each reads the recent tail so they stay roughly in
sync on "what Tim just discussed" — without sharing a literal session.

State model (load-bearing):
  - File: ``data/_worker/coord-context/<slug>.jsonl`` — VOLATILE, NOT git memory.
  - Append-only JSONL; one entry per line:
      {"ts": int, "instance": str, "speaker": "tim"|"coord", "text": str}
    ``instance`` identifies the writer ("terminal", "tg-brain"); ``speaker`` is
    whose utterance it is.
  - Truncated to the last ~200 lines once it exceeds ~500 (bounded volatile log).

ROBUSTNESS CONTRACT: every function SWALLOWS IO errors and never raises into a
brain turn. ``read_recent`` returns ``[]`` on any problem; ``append`` is
best-effort. A corrupt line is skipped, not fatal.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Bounded-log thresholds: when the file grows past TRUNCATE_AT lines, rewrite it
# keeping only the last KEEP_LINES. Volatile state — no migration concern.
TRUNCATE_AT = 500
KEEP_LINES = 200

# Per-entry text cap so one giant message can't bloat the shared buffer.
_TEXT_MAX = 2000


def _path(data_dir: Path, slug: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(slug))
    return Path(data_dir) / "_worker" / "coord-context" / f"{safe}.jsonl"


def append(data_dir: Path, slug: str, instance: str, speaker: str,
           text: str) -> None:
    """Append one entry to the blackboard for ``slug`` (best-effort, never raises).

    Truncates the file to the last ``KEEP_LINES`` lines when it grows past
    ``TRUNCATE_AT`` (checked cheaply after the append).
    """
    try:
        text = (text or "").strip()
        if not text:
            return
        if len(text) > _TEXT_MAX:
            text = text[:_TEXT_MAX] + "…"
        p = _path(data_dir, slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": int(time.time()),
            "instance": str(instance),
            "speaker": str(speaker),
            "text": text,
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _maybe_truncate(p)
    except OSError as e:  # noqa: BLE001
        log.warning("coord_context append failed for %s: %s", slug, e)
    except Exception as e:  # noqa: BLE001 — never poison a brain turn
        log.warning("coord_context append error for %s: %s", slug, e)


def _maybe_truncate(p: Path) -> None:
    """If the file exceeds TRUNCATE_AT lines, rewrite keeping the last KEEP_LINES."""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) <= TRUNCATE_AT:
            return
        tail = lines[-KEEP_LINES:]
        p.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_recent(data_dir: Path, slug: str, n: int = 25) -> list[dict[str, Any]]:
    """Return up to the last ``n`` blackboard entries for ``slug`` (never raises).

    Corrupt/partial lines are skipped. Returns ``[]`` if the file is missing or
    unreadable. Entries are returned oldest→newest within the tail window.
    """
    try:
        p = _path(data_dir, slug)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and "text" in d:
            out.append(d)
    return out
