"""Thought-log (Run observability & trust, slice 2).

A capped, ONE-LINE-per-turn reasoning gist for every agent turn of a loop,
appended to ``<loop data>/thoughtlog.jsonl`` beside ``status.jsonl``. It is
deliberately NOT a transcript: no full I/O is ever stored — only the gist the
agent chose to report (an explicit ``--gist`` on :mod:`mcp_loops.report`, else
the turn's report note), collapsed to a single line and hard-capped at
:data:`GIST_CAP` characters.

Each row::

    {ts, loop, agent, turn, status, gist, source: "gist"|"note", truncated}

``turn`` is the agent's own 1-based turn sequence within this log (the CLI
report has no engine turn counter), so the dashboard can render the log
turn-by-turn per agent. Writing is fail-soft; reading tolerates torn lines.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

GIST_CAP = 240          # hard per-row cap (chars) — a gist, never a transcript
READ_CAP = 500          # max rows a single read returns
ELLIPSIS = "…"


def thoughtlog_path(status_dir: str) -> str:
    return os.path.join(status_dir, "thoughtlog.jsonl")


def cap_gist(text: Any, cap: int = GIST_CAP) -> tuple[str, bool]:
    """Collapse ``text`` to one whitespace-normalised line, hard-capped at
    ``cap`` chars (ellipsis included). Returns ``(gist, truncated)``."""
    line = " ".join(str(text or "").split())
    if len(line) <= cap:
        return line, False
    return line[: max(cap - len(ELLIPSIS), 0)].rstrip() + ELLIPSIS, True


def read(status_dir: str, *, agent: Optional[str] = None,
         limit: int = 100) -> list[dict]:
    """The newest ``limit`` rows (oldest-first), optionally for one agent.
    Torn / non-JSON lines are skipped, never raised."""
    p = thoughtlog_path(status_dir)
    rows: list[dict] = []
    try:
        with open(p, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if agent and row.get("agent") != agent:
                    continue
                rows.append(row)
    except OSError:
        return []
    limit = max(1, min(int(limit or 1), READ_CAP))
    return rows[-limit:]


def _next_turn(status_dir: str, agent: str) -> int:
    n = 0
    try:
        with open(thoughtlog_path(status_dir), encoding="utf-8") as fh:
            for raw in fh:
                if f'"agent": {json.dumps(agent)}' in raw:
                    n += 1
    except OSError:
        pass
    return n + 1


def append(status_dir: str, loop: str, agent: str, status: str, *,
           note: str = "", gist: Optional[str] = None,
           now: Optional[float] = None) -> dict:
    """Append one capped gist row for a finished turn. An explicit non-empty
    ``gist`` wins; otherwise the report ``note`` is the gist."""
    source = "gist" if (gist or "").strip() else "note"
    text, truncated = cap_gist(gist if source == "gist" else note)
    row = {
        "ts": time.time() if now is None else now,
        "loop": loop, "agent": agent,
        "turn": _next_turn(status_dir, agent),
        "status": status, "gist": text, "source": source,
        "truncated": truncated,
    }
    os.makedirs(status_dir, exist_ok=True)
    with open(thoughtlog_path(status_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def view(name: str, rows: list[dict]) -> dict:
    """The API payload: rows oldest-first plus a per-agent turn index."""
    agents: dict[str, int] = {}
    for r in rows:
        a = str(r.get("agent") or "")
        agents[a] = agents.get(a, 0) + 1
    return {"name": name, "cap": GIST_CAP, "count": len(rows),
            "agents": agents, "entries": rows}
