"""Sessions — the attached collaborators (REDESIGN-SPEC §3 rd-sessions §7 build
order): a **first-class roster** read-model over the connect store.

A *session* is a live app/CLI **connector** (see :mod:`mcp_loops.connect`) that can
execute dispatched tasks on the user's own compute. This module RE-COMPOSES the
connector records + the task ledger the connect store already holds into one
**roster card** per session — identity, runtime + capability chips, **which origin
it runs on** (host, cwd), an honest heartbeat, **what it's doing now**, and **what
it has created** (loops / issues / objectives). It is the read-model behind the
Sessions page.

Pure + READ-ONLY this round (no switch verbs yet, per the build order): it reads
the connect records the server passes in, mutates nothing, and — the honesty
mandate — **never fabricates a fact it cannot attribute**. An origin it doesn't
know is ``None`` (the UI shows "—"), and created-counts reflect only the tasks
this session actually *returned* with a named artifact, never a guess.
"""
from __future__ import annotations

from typing import Optional

# A connector unseen for longer than this reads as a stale heartbeat — mirrors
# :data:`mcp_loops.connect.DEFAULT_STALE_AFTER` so the two never drift.
DEFAULT_STALE_AFTER = 90.0

# result-envelope keys a returned task uses to name what it created → the roster
# bucket it counts toward. A doc/objective are the same substrate (§rd-ideahub).
_CREATED_KEYS = (("loop", "loops"), ("issue", "issues"),
                 ("objective", "objectives"), ("doc", "objectives"))


def _doing_label(task: dict) -> str:
    """The roster card's "doing now" line for a CLAIMED task — task id (+ the
    loop that dispatched it) ONLY, never the prompt. A connect task's prompt can
    carry credentials / interactive instructions, and the roster is readable by
    any caller, so task content never leaves the connect store through it."""
    tid = task.get("id")
    label = f"task {tid}" if isinstance(tid, str) and tid else "a task"
    loop = task.get("originLoop")
    if isinstance(loop, str) and loop:
        label += f" · loop {loop}"
    return label


def _origin_of(conn: dict) -> Optional[dict]:
    """Which origin a session runs on — host + cwd (+ origin id when recorded) —
    read from the connector's free ``meta`` bag. Returns ``None`` when the session
    never told us (honest: the UI shows "—", it does not invent a box)."""
    meta = conn.get("meta") if isinstance(conn.get("meta"), dict) else {}
    host = meta.get("host") or meta.get("hostname")
    cwd = meta.get("cwd") or meta.get("dir")
    origin = meta.get("origin") or meta.get("originId")
    if host is None and cwd is None and origin is None:
        return None
    return {"host": host, "cwd": cwd, "origin": origin}


def _created_counts(tasks: list[dict]) -> dict:
    """What a session has CREATED — counted only from tasks it actually RETURNED
    whose result envelope NAMES an artifact (a loop / issue / objective). Never a
    guess: a session with no attributable output honestly reports zeros."""
    out = {"loops": 0, "issues": 0, "objectives": 0}
    for t in tasks:
        if t.get("status") != "returned":
            continue
        res = t.get("result") if isinstance(t.get("result"), dict) else {}
        for key, bucket in _CREATED_KEYS:
            if res.get(key):
                out[bucket] += 1
    return out


def build_roster(connectors: list[dict], tasks: list[dict], *, now: float,
                 stale_after: float = DEFAULT_STALE_AFTER) -> dict:
    """The Sessions ROSTER: one honest card per connected session, most-recently
    seen first. ``connectors``/``tasks`` are the connect-store records the server
    reads (:func:`connect.list_connectors` / :func:`connect.list_tasks`). Returns
    ``{sessions, count, live}``. Pure; tolerant of malformed rows; never raises."""
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    out: list = []
    for c in (connectors or []):
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not cid:
            continue
        seen = c.get("lastSeen") or 0.0
        idle = round(now - seen, 1) if seen else None
        live = (bool(c.get("live")) if "live" in c
                else (bool(seen) and (now - seen) <= stale_after))
        mine = [t for t in tasks
                if t.get("claimedBy") == cid or t.get("connector") == cid]
        # what it's doing NOW: the newest task it currently holds CLAIMED.
        claimed = sorted((t for t in mine if t.get("status") == "claimed"),
                         key=lambda t: t.get("claimedAt") or 0.0, reverse=True)
        doing = _doing_label(claimed[0]) if claimed else None
        out.append({
            "id": cid,
            "runtime": c.get("runtime") or "claude",
            "capabilities": list(c.get("capabilities") or []),
            "origin": _origin_of(c),
            "heartbeat": {
                "live": live,
                "state": "live" if live else "stale",
                "lastSeen": c.get("lastSeen"),
                "idleSeconds": idle,
            },
            "doingNow": doing,
            "created": _created_counts([t for t in mine
                                        if t.get("claimedBy") == cid]),
            "activity": {
                "claimed": sum(1 for t in mine if t.get("claimedBy") == cid),
                "returned": sum(1 for t in mine if t.get("status") == "returned"),
                "failed": sum(1 for t in mine if t.get("status") == "failed"),
            },
        })
    out.sort(key=lambda s: s["heartbeat"].get("lastSeen") or 0.0, reverse=True)
    return {"sessions": out, "count": len(out),
            "live": sum(1 for s in out if s["heartbeat"]["live"])}
