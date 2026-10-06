"""Dual goodness score (Run observability & trust, slice 1).

Two INDEPENDENT judgements of one finished run, shown side by side and NEVER
blended into one number:

* **owner** — the human's good / ok / bad disposition (``dispositions.jsonl``,
  read via :func:`mcp_loops.resolution.current_disposition`). Recorded only by
  the owner; this module never writes it.
* **analyst** — a deterministic *loop-analyst* pass over the finished run's
  telemetry (the same :func:`mcp_loops.server.loop_analyze` payload) plus the
  computed Resolution verdict. The owner rating is NEVER an input to it — the
  analyst function does not even receive the disposition rows.

The analyst record is persisted per run (keyed on ``run.started``) in
``<loop data>/goodness.json`` so a re-run keeps the earlier run's score and a
re-read is free. Every helper here is pure except :func:`load` / :func:`save`;
none of them raise into a caller.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from mcp_loops import resolution as R

ANALYST_ID = "loop-analyst@heuristic-v1"
GRADES = ("good", "ok", "bad")
GOOD_AT = 70          # score >= GOOD_AT  → good
OK_AT = 40            # score >= OK_AT    → ok, else bad
RATIONALE_CAP = 200

# base score per computed verdict — the outcome dominates; telemetry adjusts.
_BASE = {
    R.RESOLVED: 80,
    R.SHIPPED: 70,
    R.PARTIAL: 50,
    R.ABANDONED: 20,
    R.FAILED: 5,
}
# a FINISHED run the Resolution verdict leaves unresolved (e.g. ended on the
# turn limit): judged, but from a low-middle base — it never met its goal.
UNRESOLVED = "UNRESOLVED"
_BASE_UNRESOLVED = 35


def goodness_path(status_dir: str) -> str:
    return os.path.join(status_dir, "goodness.json")


def run_key(run_started: Any) -> str:
    """The per-run key: ``run.started`` (epoch secs) as a stable string."""
    if isinstance(run_started, (int, float)):
        return f"{float(run_started):.3f}"
    return "unknown"


def _grade(score: int) -> str:
    return "good" if score >= GOOD_AT else "ok" if score >= OK_AT else "bad"


def _cap(text: str, limit: int = RATIONALE_CAP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def score_run(analysis: dict, card: Optional[dict] = None, *,
              now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The analyst score for ONE finished run, or ``None`` when there is
    nothing finished to judge (an ``error`` analysis, e.g. the run is live).
    A finished run with no Resolution verdict (turn-limit end) is scored as
    ``UNRESOLVED``.

    ``analysis`` is a :func:`loop_analyze` payload (``telemetry`` used);
    ``card`` is the Resolution card (``verdict`` / ``handles`` / ``proof``
    used). Deliberately takes NO disposition input — the owner rating can never
    leak into the analyst score. Deterministic: same inputs → same record
    (``at`` aside). Returns ``{grade, score, rationale, factors, verdict,
    analyst, at}``."""
    if not isinstance(analysis, dict) or analysis.get("error"):
        return None
    tele = analysis.get("telemetry") if isinstance(analysis.get("telemetry"), dict) else {}
    card = card if isinstance(card, dict) else {}
    verdict = (card.get("verdict") or {}).get("value")
    if verdict in _BASE:
        score = _BASE[verdict]
        label = f"verdict {verdict}"
    else:
        verdict, score = UNRESOLVED, _BASE_UNRESOLVED
        label = f"unresolved (ended {tele.get('ended') or analysis.get('state')})"
    factors: list[dict[str, Any]] = [{"factor": label, "delta": score}]

    def add(label: str, delta: int) -> None:
        nonlocal score
        if delta:
            score += delta
            factors.append({"factor": label, "delta": delta})

    per_agent = tele.get("per_agent") if isinstance(tele.get("per_agent"), dict) else {}
    timeouts = sum(int((pa.get("statuses") or {}).get("timeout", 0))
                   for pa in per_agent.values() if isinstance(pa, dict))
    add(f"{timeouts} timed-out turn(s)", -min(20, 5 * timeouts))
    retired = tele.get("retired") or []
    add(f"{len(retired)} retired agent(s)", -min(9, 3 * len(retired)))
    turns = tele.get("turns_used") or tele.get("report_count") or 0
    if not turns:
        add("no turns recorded", -20)

    proof = card.get("proof") if isinstance(card.get("proof"), dict) else {}
    tests = proof.get("tests")
    if isinstance(tests, str) and tests.strip():
        if not R._looks_red(tests):
            add("tests evidence green", 10)
    else:
        add("no test evidence", -5)
    handles = card.get("handles") if isinstance(card.get("handles"), dict) else {}
    if handles.get("commit"):
        add("commit landed", 5)

    score = max(0, min(100, int(score)))
    grade = _grade(score)
    adj = [f"{f['factor']} ({f['delta']:+d})" for f in factors[1:]]
    rationale = _cap(f"{label} → {score}/100" + (": " + "; ".join(adj) if adj else ""))
    return {"grade": grade, "score": score, "rationale": rationale,
            "factors": factors, "verdict": verdict, "analyst": ANALYST_ID,
            "at": time.time() if now is None else now}


def load(status_dir: str) -> dict[str, Any]:
    """The persisted ``{runs: {key: record}}`` (empty when absent/corrupt)."""
    try:
        with open(goodness_path(status_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("runs"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"runs": {}}


def save(status_dir: str, key: str, record: dict) -> None:
    """Persist one run's analyst record (atomic replace)."""
    data = load(status_dir)
    data["runs"][key] = record
    os.makedirs(status_dir, exist_ok=True)
    path = goodness_path(status_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def owner_cell(owner: Optional[dict]) -> str:
    return owner.get("verb", "—") if isinstance(owner, dict) else "_pending_"


def analyst_cell(analyst: Optional[dict]) -> str:
    if not isinstance(analyst, dict):
        return "_pending_"
    return f"{analyst.get('grade')} · {analyst.get('score')}/100"


def table_cells(owner: Optional[dict], analyst: Optional[dict]) -> dict[str, str]:
    """The two SEPARATE cells for the reserved result-markdown Goodness table."""
    return {"owner": owner_cell(owner), "analyst": analyst_cell(analyst)}


def view(name: str, *, run_started: Any, owner: Optional[dict],
         analyst: Optional[dict], analyst_reason: Optional[str] = None,
         rateable: bool = False) -> dict[str, Any]:
    """The ``loop_goodness`` payload: owner and analyst as separate fields with
    an honest per-side ``pending`` flag — there is no combined score."""
    out: dict[str, Any] = {
        "name": name,
        "runStarted": run_started,
        "owner": owner,
        "analyst": analyst,
        "pending": {"owner": owner is None, "analyst": analyst is None},
        "ownerRateable": bool(rateable),
        "table": table_cells(owner, analyst),
    }
    if analyst is None and analyst_reason:
        out["analystReason"] = analyst_reason
    return out
