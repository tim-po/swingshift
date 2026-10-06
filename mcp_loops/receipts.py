"""Dispatch receipts (detection half) — find framed-but-unanswered loop turns.

The worker's inject path can silently fail (prompt typed, Enter never lands) or
mis-deliver, leaving the orchestrator waiting on a turn no session is running.
The evidence is already on disk: a framed prompt at
``data/_loops/<name>/prompts/<agent>-NNN.txt`` with **no matching report line**
in ``status.jsonl`` after it. This module audits that gap::

    python -m mcp_loops.receipts <loop-name> [--json] [--grace MIN]

Matching is FIFO per agent: each report answers that agent's oldest prompt
framed at-or-before the report's timestamp. Leftover prompts are ``pending``
while younger than the agent's own turn budget (config ``maxTurnMinutes`` —
the substrate's timeout, so anything younger is legitimately in flight) and
``stalled`` once older. Pure file reads — safe to run against a live loop.

The recovery half (substrate acks the injection and re-injects on a missing
receipt) belongs in headless.py; see the discovery backlog.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from mcp_loops import paths

# R6: the loops data dir is ``paths.resolve_data_dir()`` (LOOPS_DATA_DIR >
# LOOPYARD_HOME > install default). ``_HOME`` survives only as a legacy/test
# override (``<_HOME>/data/_loops``) via the deprecated BOT_SQUAD_HOME.
_HOME = paths.legacy_home_override("receipts")
_PROMPT_RE = re.compile(r"^(?P<agent>.+)-(?P<seq>\d{3})\.txt$")
DEFAULT_GRACE_MIN = 30


def _now() -> float:
    """Wall-clock seam — the CLI path reads through this so tests can pin it
    (scan() also takes an explicit ``now=`` for its own unit tests)."""
    return time.time()


def _agent_budgets(loop_dir: Path) -> dict[str, int]:
    """agent id -> maxTurnMinutes from the loop's (normalized) config."""
    try:
        cfg = json.loads((loop_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {sid: s["maxTurnMinutes"]
            for sid, s in (cfg.get("steps") or {}).items()
            if s.get("type") == "agent" and isinstance(s.get("maxTurnMinutes"), int)}


def _reports_by_agent(loop_dir: Path) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    try:
        lines = (loop_dir / "status.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("kind") == "machinery":
            continue                       # structural/live events aren't turn reports
        agent, ts = e.get("agent"), e.get("ts")
        if isinstance(agent, str) and isinstance(ts, (int, float)):
            out.setdefault(agent, []).append(float(ts))
    for v in out.values():
        v.sort()
    return out


def scan(loop_dir: Path | str, *, now: float | None = None,
         grace_min: int | None = None) -> dict:
    """Audit one loop dir. Returns {ok, state, dispatches, stalled, pending}.

    ``ok`` is False when at least one dispatch is stalled. ``now`` and
    ``grace_min`` (a global override for per-agent budgets) are injectable for
    tests.
    """
    loop_dir = Path(loop_dir)
    now = _now() if now is None else now
    budgets = _agent_budgets(loop_dir)
    reports = _reports_by_agent(loop_dir)
    try:
        run_state = json.loads((loop_dir / "run.json").read_text(encoding="utf-8")).get("state")
    except (OSError, json.JSONDecodeError):
        run_state = None

    prompts: list[tuple[str, int, float]] = []          # (agent, seq, mtime)
    pdir = loop_dir / "prompts"
    if pdir.is_dir():
        for p in pdir.iterdir():
            m = _PROMPT_RE.match(p.name)
            if m:
                try:
                    prompts.append((m.group("agent"), int(m.group("seq")), p.stat().st_mtime))
                except OSError:
                    continue
    prompts.sort(key=lambda x: (x[0], x[2]))

    # FIFO match per agent: each report answers the oldest prompt framed <= its ts
    unmatched_reports = {a: list(ts) for a, ts in reports.items()}
    dispatches: list[dict] = []
    for agent, seq, mtime in prompts:
        answered_ts = None
        pool = unmatched_reports.get(agent, [])
        for i, ts in enumerate(pool):
            if ts >= mtime:
                answered_ts = pool.pop(i)
                break
        if answered_ts is not None:
            status = "answered"
        else:
            budget_min = grace_min if grace_min is not None else budgets.get(agent, DEFAULT_GRACE_MIN)
            age_min = (now - mtime) / 60.0
            status = "stalled" if age_min > budget_min else "pending"
        dispatches.append({
            "agent": agent, "seq": seq, "prompt_mtime": mtime,
            "status": status,
            "age_min": round((now - mtime) / 60.0, 1),
            "answered_ts": answered_ts,
        })

    stalled = [d for d in dispatches if d["status"] == "stalled"]
    pending = [d for d in dispatches if d["status"] == "pending"]
    return {"ok": not stalled, "state": run_state, "dispatches": dispatches,
            "stalled": stalled, "pending": pending}


def format_report(name: str, res: dict) -> str:
    lines = [f"── dispatch receipts: {name} (run state: {res['state'] or 'unknown'}) ──"]
    for d in res["dispatches"]:
        mark = {"answered": "✓", "pending": "◷", "stalled": "✗"}[d["status"]]
        lines.append(f"  {mark} {d['agent']}-{d['seq']:03d}  {d['status']:<8} "
                     f"framed {d['age_min']}m ago")
    if res["stalled"]:
        agents = ", ".join(sorted({d["agent"] for d in res["stalled"]}))
        lines.append(f"  → {len(res['stalled'])} STALLED dispatch(es) ({agents}): the prompt was "
                     "framed but no report ever landed.")
        lines.append("    Recover: tmux capture-pane the agent's session — if the prompt sits "
                     "unsubmitted, tmux send-keys Enter; then check the session actually got "
                     "THIS agent's prompt (cross-delivery happens).")
    elif res["pending"]:
        lines.append(f"  → {len(res['pending'])} dispatch(es) in flight, within budget.")
    else:
        lines.append("  → all dispatches answered.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    grace = None
    if "--grace" in argv:
        i = argv.index("--grace")
        try:
            grace = int(argv[i + 1])
        except (IndexError, ValueError):
            print("--grace needs an integer (minutes)", file=sys.stderr)
            return 2
        argv = argv[:i] + argv[i + 2:]
    names = [a for a in argv if not a.startswith("--")]
    if not names:
        print("usage: python -m mcp_loops.receipts <loop-name> [--json] [--grace MIN]",
              file=sys.stderr)
        return 2
    loops_root = (Path(_HOME) / "data" / "_loops" if _HOME is not None
                  else Path(paths.resolve_data_dir()))
    loop_dir = loops_root / names[0]
    if not loop_dir.is_dir():
        print(f"no such loop dir: {loop_dir}", file=sys.stderr)
        return 2
    res = scan(loop_dir, grace_min=grace)
    print(json.dumps(res, indent=2) if as_json else format_report(names[0], res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
