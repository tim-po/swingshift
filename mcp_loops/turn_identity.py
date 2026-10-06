"""Status-row identity (H7): which RUN and which TURN a ``status.jsonl`` row is.

``status.jsonl`` accumulates across every run of a loop name, and the engine
restarts each agent's turn counter (the ``prompts/<agent>-NNN.txt`` index) on
every run. v1 rows carried neither a run id nor a turn, so every reader
re-derived "which turn of which run" its own way and, on a loop run more than
once, paired the wrong prompt with the wrong report.

v2 rows (``"v": 2``) carry:

* ``runId`` — minted once per run at start (``server._loop_start_local_locked``)
  and stored in ``run.json``;
* ``turn`` — the substrate's per-run, per-agent 1-based turn index, the SAME
  number the framed prompt file is named with. The substrate records it in a
  small cursor file (``turns.json``) when it delivers a turn; the reporter reads
  it back when the agent reports, so the agent's report command is unchanged.

Every reader goes through :func:`rows_for_run` / :func:`number_turns` /
:func:`find_turn`. Old rows keep working with NO migration: a row without a
``runId`` falls back to the ``ts >= run.started`` window, and a row without a
``turn`` to its position among the agent's in-run rows — exactly the legacy
behavior. Stdlib-only so ``envelope``/``teamroom``/the dashboard can import it.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any, Iterable, Optional

ROW_VERSION = 2
TURNS_FILE = "turns.json"
_LOCK = threading.Lock()   # parallel-group agents share one engine process


def mint_run_id() -> str:
    """A fresh, unique run id (stored as ``run.json`` ``runId``)."""
    return uuid.uuid4().hex


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _int_turn(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


# ── writer side ──────────────────────────────────────────────────────────────
def note_turn(status_dir: str, agent: str, turn: int) -> None:
    """Substrate hook: ``agent`` is now on per-run turn ``turn`` of the run in
    ``status_dir/run.json``. Fail-soft — identity must never cost a turn."""
    try:
        run_id = _read(os.path.join(status_dir, "run.json")).get("runId")
        if not isinstance(run_id, str) or not run_id:
            return
        path = os.path.join(status_dir, TURNS_FILE)
        with _LOCK:
            cur = _read(path)
            agents = cur.get("agents") if cur.get("runId") == run_id else None
            agents = dict(agents) if isinstance(agents, dict) else {}
            agents[agent] = int(turn)
            tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"runId": run_id, "agents": agents}, fh)
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def stamp(status_dir: str, agent: str) -> dict:
    """The identity fields for a row ``agent`` reports now: ``{v, runId, turn}``
    for the run in ``run.json`` (``turn`` None when the substrate recorded none
    for this run), or ``{}`` for a run that predates run ids (a v1 row)."""
    run_id = _read(os.path.join(status_dir, "run.json")).get("runId")
    if not isinstance(run_id, str) or not run_id:
        return {}
    cur = _read(os.path.join(status_dir, TURNS_FILE))
    turn = None
    if cur.get("runId") == run_id and isinstance(cur.get("agents"), dict):
        turn = _int_turn(cur["agents"].get(agent))
    return {"v": ROW_VERSION, "runId": run_id, "turn": turn}


# ── reader side (the ONE shared helper; legacy fallback built in) ─────────────
def rows_for_run(rows: Iterable[dict], run: Optional[dict]) -> list[dict]:
    """Scope ``status.jsonl`` rows to the run described by ``run`` (run.json).

    A row stamped with a ``runId`` belongs to the run iff the ids match — even a
    late report from the prior run that lands after the restart is excluded. A
    row without one (v1 rows, machinery rows) falls back to the legacy window:
    kept when its ``ts`` is at/after ``run.started`` or it has no ``ts``. With no
    ``started`` (never-run / saved) nothing is dropped by the window."""
    run = run or {}
    run_id = run.get("runId") if isinstance(run.get("runId"), str) else None
    started = run.get("started")
    out = []
    for r in rows:
        rid = r.get("runId")
        if run_id and isinstance(rid, str) and rid:
            if rid == run_id:
                out.append(r)
            continue
        ts = r.get("ts")
        if (not isinstance(started, (int, float)) or not isinstance(ts, (int, float))
                or ts >= started):
            out.append(r)
    return out


def _is_report(r: dict) -> bool:
    return r.get("kind") != "machinery" and isinstance(r.get("agent"), str)


def number_turns(rows: list[dict]) -> list[Optional[int]]:
    """The per-agent turn of each row of ONE run (already :func:`rows_for_run`
    scoped): a v2 row's stamped ``turn``; else (legacy) the next index after the
    agent's highest turn so far. Machinery / agentless rows get None."""
    hi: dict[str, int] = {}
    out: list[Optional[int]] = []
    for r in rows:
        if not _is_report(r):
            out.append(None)
            continue
        a = r["agent"]
        t = _int_turn(r.get("turn")) if r.get("v") == ROW_VERSION else None
        if t is None:
            t = hi.get(a, 0) + 1
        hi[a] = max(hi.get(a, 0), t)
        out.append(t)
    return out


def find_turn(rows: Iterable[dict], run: Optional[dict], agent: str,
              turn: int) -> Optional[dict]:
    """The report answering ``agent``'s per-run turn ``turn`` of ``run`` — keyed
    on (runId, agent, turn) — or None. The first report of that turn wins (the
    one the substrate consumed)."""
    mine = [r for r in rows_for_run(rows, run) if _is_report(r) and r["agent"] == agent]
    for r, t in zip(mine, number_turns(mine)):
        if t == turn:
            return r
    return None
