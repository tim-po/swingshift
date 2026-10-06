"""Library quality rollup — the pure half of :func:`server.loop_quality`.

Two SEPARATE signals per run, never averaged together:

* the owner's rating (good / ok / bad, from ``dispositions.jsonl``), and
* the analyst's score (0–100 + grade, from ``goodness.json``, heuristic-v1).

:func:`loop_row` folds one loop's raw on-disk facts into a row with its run
history; :func:`agent_rows` joins those rows through each loop's agents into a
per-agent rollup (loops, avg analyst score, owner tallies, retry rate, trend,
best / worst loop, pinned versions seen). Pure: no I/O, deterministic.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from mcp_loops import goodness as G

UNATTRIBUTED = "__unattributed__"   # the web switcher's "no project" scope id
TREND_STEP = 5                       # |recent - older| ≥ this → up / down
LOW_SCORE = G.OK_AT                  # avg analyst score below this → attention
BAD_RATINGS = 2                      # this many owner "bad" (and more bad than good)


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _mean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def in_scope(project: Optional[str], want: str) -> bool:
    """'' = every project; ``UNATTRIBUTED`` = loops bound to none."""
    if not want:
        return True
    if want == UNATTRIBUTED:
        return not project
    return project == want


def run_history(goodness_runs: dict, verdicts: dict[str, dict]) -> list[dict[str, Any]]:
    """One row per run that has EITHER signal, oldest first:
    ``{runStarted, score, grade, owner}`` (a missing side is ``None``).

    ``goodness_runs`` is ``goodness.json``'s ``runs`` (``run_key → record``);
    ``verdicts`` maps the same ``run_key`` → the owner's current verdict."""
    keys = set(goodness_runs or {}) | set(verdicts or {})
    out = []
    for k in keys:
        rec = (goodness_runs or {}).get(k) if isinstance((goodness_runs or {}).get(k), dict) else None
        ver = (verdicts or {}).get(k)
        started = _num((rec or {}).get("runStarted"))
        if started is None:
            try:
                started = float(k)
            except (TypeError, ValueError):
                started = None
        out.append({
            "runStarted": started,
            "score": (rec or {}).get("score"),
            "grade": (rec or {}).get("grade"),
            "owner": (ver or {}).get("verb") if isinstance(ver, dict) else None,
        })
    out.sort(key=lambda r: r["runStarted"] or 0)
    return out


def loop_attention(owner_verb: Optional[str], grade: Optional[str]) -> Optional[str]:
    if owner_verb == "bad":
        return "You rated the last run bad"
    if grade == "bad":
        return "The analyst scored the last run low"
    return None


def loop_row(*, name: str, project: Optional[str], state: str, archived: bool,
             started: Any, updated: Any, goal: str, agents: Iterable[str],
             turns: int, retries: int, per_agent: dict[str, dict],
             pins: dict[str, list[int]], owner: Optional[dict],
             analyst: Optional[dict], analyst_pending: bool,
             analyst_reason: Optional[str], history: list[dict]) -> dict[str, Any]:
    """One loop's Library row. ``per_agent`` is ``{agent: {turns, retries}}``;
    ``pins`` is ``{agent: [registry versions frozen into this loop]}``."""
    started_f = _num(started)
    last = _num(updated) or started_f
    verb = owner.get("verb") if isinstance(owner, dict) else None
    grade = analyst.get("grade") if isinstance(analyst, dict) else None
    attention = loop_attention(verb, grade)
    return {
        "name": name, "project": project, "state": state, "archived": bool(archived),
        "startedTs": started_f, "lastTs": last, "goal": (goal or "")[:160],
        "agents": sorted(set(agents)), "turns": int(turns), "retries": int(retries),
        "perAgent": per_agent, "pins": pins,
        "owner": ({"verb": verb, "note": owner.get("note"), "at": owner.get("at")}
                  if verb else None),
        "analyst": ({"score": analyst.get("score"), "grade": grade,
                     "verdict": analyst.get("verdict"),
                     "rationale": analyst.get("rationale"), "at": analyst.get("at")}
                    if isinstance(analyst, dict) else None),
        "analystPending": bool(analyst_pending),
        **({"analystReason": analyst_reason} if analyst_reason else {}),
        "runs": history,
        "needsAttention": attention is not None,
        **({"attentionReason": attention} if attention else {}),
    }


def trend(points: list[float]) -> tuple[Optional[str], Optional[float]]:
    """Recent half vs older half of a time-ordered score series:
    ``('up'|'down'|'flat', delta)`` or ``(None, None)`` under two points."""
    if len(points) < 2:
        return None, None
    mid = len(points) // 2
    older, recent = points[:mid], points[mid:]
    delta = round(sum(recent) / len(recent) - sum(older) / len(older), 1)
    return ("up" if delta >= TREND_STEP else "down" if delta <= -TREND_STEP else "flat"), delta


def agent_attention(avg: Optional[float], owner: dict[str, int]) -> Optional[str]:
    if owner.get("bad", 0) >= BAD_RATINGS and owner["bad"] > owner.get("good", 0):
        return f"You rated {owner['bad']} of its runs bad"
    if avg is not None and avg < LOW_SCORE:
        return "Low analyst score on average"
    return None


def agent_rows(loops: list[dict], *, registry: dict[str, dict],
               since: Optional[float] = None) -> list[dict[str, Any]]:
    """Per-agent rollup joined through each loop row's ``agents``.

    Scores / ratings count every run in the loop's history (within ``since``
    when given); best / worst use each loop's latest analyst score.
    ``registry`` is ``{id: {favorite, saved}}``."""
    acc: dict[str, dict] = {}
    for lp in loops:
        runs = [r for r in lp.get("runs") or []
                if since is None or (r.get("runStarted") or 0) >= since]
        for a in lp.get("agents") or []:
            d = acc.setdefault(a, {"loops": [], "turns": 0, "retries": 0,
                                   "points": [], "owner": {"good": 0, "ok": 0, "bad": 0},
                                   "latest": [], "versions": set(), "lastTs": None})
            d["loops"].append(lp["name"])
            pa = (lp.get("perAgent") or {}).get(a) or {}
            d["turns"] += int(pa.get("turns") or 0)
            d["retries"] += int(pa.get("retries") or 0)
            d["versions"].update(v for v in (lp.get("pins") or {}).get(a, [])
                                 if isinstance(v, int))
            if lp.get("lastTs") and (d["lastTs"] is None or lp["lastTs"] > d["lastTs"]):
                d["lastTs"] = lp["lastTs"]
            for r in runs:
                s = _num(r.get("score"))
                if s is not None:
                    d["points"].append({"ts": r.get("runStarted"), "score": s,
                                        "loop": lp["name"]})
                if r.get("owner") in d["owner"]:
                    d["owner"][r["owner"]] += 1
            s = _num((lp.get("analyst") or {}).get("score"))
            if s is not None:
                d["latest"].append((s, lp["name"]))
    out = []
    for a, d in acc.items():
        pts = sorted(d["points"], key=lambda p: p["ts"] or 0)
        scores = [p["score"] for p in pts]
        avg = _mean(scores)
        tr, delta = trend(scores)
        latest = sorted(d["latest"])
        reg = registry.get(a) or {}
        attention = agent_attention(avg, d["owner"])
        out.append({
            "agent": a, "loops": sorted(set(d["loops"])), "loopCount": len(set(d["loops"])),
            "turns": d["turns"], "retries": d["retries"],
            "retryRate": round(d["retries"] / d["turns"], 3) if d["turns"] else 0,
            "avgScore": avg, "scored": len(scores), "owner": d["owner"],
            "trend": tr, "trendDelta": delta, "history": pts,
            "best": {"loop": latest[-1][1], "score": latest[-1][0]} if latest else None,
            "worst": ({"loop": latest[0][1], "score": latest[0][0]}
                      if len(latest) > 1 else None),
            "saved": a in registry, "favorite": bool(reg.get("favorite")),
            "versions": sorted(d["versions"]), "lastTs": d["lastTs"],
            "needsAttention": attention is not None,
            **({"attentionReason": attention} if attention else {}),
        })
    out.sort(key=lambda r: (-(r["avgScore"] if r["avgScore"] is not None else -1),
                            -r["loopCount"], r["agent"]))
    return out
