"""resolution — the closing **Resolution card** for a finished loop (REDESIGN-SPEC
§3 rd-results, build order §5.4).

A result is NOT a destination — it is the *terminal state a loop enters*, read in
the loop view. When a loop ends, the top of the same pane resolves into a card
that reads top-to-bottom the way a human would tell you how it went:

  1. a **computed, honest verdict** — one of
     {``SHIPPED·running``, ``RESOLVED``, ``PARTIAL``, ``FAILED``,
     ``ABANDONED—needs you``} — **green only on a positive resolution signal**;
  2. the **resolution in one line**, led by the real-world outcome (the crew's
     answer), not a filename;
  3. the **real handles** — commit, the deliverable, the proof strip.

Plus a **disposition** that is *gated on outcome*: "How did this land?"
good/ok/bad + a why, **offered only when there is an outcome to judge** — a crash
that produced nothing can never be rated "good".

THE HONESTY MANDATE (§5.4, unfakeable by construction). ``green`` is returned
``True`` from **exactly two** branches — :data:`RESOLVED` and :data:`SHIPPED`. A
guardian-stopped run persists ``run.state = "finished"`` (because its ``ended``
was not ``"error"``), so :func:`mcp_loops.envelope.external_status` reports it as
``completed`` — which would render **green**. This module refuses that: the
verdict keys off the raw ``result.ended`` (in ``verification.ended``), so a
``guardian_stopped`` / ``error`` / ``stopped`` outcome is caught **before** the
green branch and lands on FAILED / ABANDONED with a **Respawn / Take over**
action. There is no input that makes a stop or an error read green.

This module is PURE — it takes an already-built result envelope
(:func:`mcp_loops.envelope.build_envelope`) plus the loop's disposition rows and
returns plain dicts. It reads nothing off disk and never raises, so every verdict
rule is unit-testable without a live engine. ``mcp_loops.server.loop_resolution``
does the file I/O and calls :func:`build_resolution_card`.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from . import deliverables as _deliverables

# ── the five committed verdict values (§3 rd-results / §4 Q-resolution) ───────
SHIPPED = "SHIPPED·running"       # running, but real code has already landed
RESOLVED = "RESOLVED"             # terminal + a positive resolution signal (green)
PARTIAL = "PARTIAL"               # terminal, but incomplete — tests red / no deliverable
FAILED = "FAILED"                 # a crash / hard error — nothing trustworthy produced
ABANDONED = "ABANDONED—needs you" # stopped / guardian gave up — needs the owner

# ``result.ended`` raw outcomes (mcp_loops.runner) that are POSITIVE terminals —
# the only ones that may pass into the green RESOLVED branch.
_ENDED_GOOD = frozenset({"complete", "early_finish", "done", "finished"})
# raw outcomes that mean the engine gave up / the run was cut short → the owner.
_ENDED_ABANDONED = frozenset({"guardian_stopped", "stopped", "aborted"})

# Owner-action kinds the card hands the frontend (the "one human action owed").
ACT_MERGE = "merge"        # a positive resolution left a commit to land
ACT_REVIEW = "review"      # a partial — look at what's left
ACT_RESPAWN = "respawn"    # a failure/abandonment — start it again
ACT_TAKE_OVER = "take_over"  # a live pause — step in
ACT_WATCH = "watch"        # still running — nothing owed yet

# The outcome verbs a disposition can carry as a *quality* verdict (§4.2) — the
# ONLY manual rating (Better-UX #5). re-run / removed / deleted are behaviors the
# engine records itself; legacy ship/keep rows are behaviors too, never a rating.
_OUTCOME_VERBS = ("good", "ok", "bad")

_SHA_RE = re.compile(r"\b([0-9a-fA-F]{7,40})\b")


def _short(sha: Any) -> Optional[str]:
    """A 7-char short sha for display, or ``None``."""
    if not isinstance(sha, str) or not sha.strip():
        return None
    return sha.strip()[:7]


def _looks_red(tests: Any) -> bool:
    """True when a ``tests=`` marker reads as FAILING — an explicit ``red`` /
    ``fail`` token, or a ``passed/total`` fraction with passed < total (e.g.
    ``41/42``, ``passed:3/4``). A ``green`` / all-passing / unknown marker is not
    red. Never raises."""
    if not isinstance(tests, str) or not tests.strip():
        return False
    s = tests.strip().lower()
    if "red" in s or "fail" in s:
        return True
    if "green" in s or "pass" in s and "/" not in s:
        return False
    m = re.search(r"(\d+)\s*/\s*(\d+)", s)
    if m:
        passed, total = int(m.group(1)), int(m.group(2))
        return total > 0 and passed < total
    return False


def _one_line(text: Any, limit: int = 240) -> Optional[str]:
    """The crew's answer squeezed to a single resolution line — collapse
    whitespace, strip the opt-in ``commit=/tests=/artifact=`` markers so the line
    leads with the real-world outcome (not machinery), cap length. ``None`` for
    empty input."""
    if not isinstance(text, str):
        return None
    s = re.sub(r"\b(commit|tests|artifact)=\S+", "", text)
    s = " ".join(s.split()).strip(" .;—-")
    if not s:
        return None
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


# artifact kinds, in the order they read as a real "deliverable" — an agent's
# explicitly-declared ``artifact=`` path first, then the finish report, then the
# output tree; the engine's ``block_scheme`` (config.html) is machinery, last.
_DELIVERABLE_RANK = {"reported": 0, "finish_report": 1, "output_dir": 2,
                     "block_scheme": 9}


def _pick_deliverable(artifacts: list) -> Optional[str]:
    """The ONE deliverable path (a **string**, never the raw ``{kind,path,exists}``
    dict — the frontend renders it with string ops, so an object shows as
    ``[object Object]``). Prefers an agent-declared artifact over the engine's
    machinery files, prefers one that actually ``exists``, and returns its ``path``.
    ``None`` when there is nothing to point at. Never raises."""
    best: Optional[dict] = None
    best_key = None
    for a in artifacts or []:
        if not isinstance(a, dict) or not isinstance(a.get("path"), str):
            continue
        # sort key: declared-kind rank, then existing-before-claimed, then order
        key = (_DELIVERABLE_RANK.get(a.get("kind"), 5), 0 if a.get("exists") else 1)
        if best is None or key < best_key:
            best, best_key = a, key
    return best.get("path") if best else None


def _produced(env: dict) -> bool:
    """Did the run leave anything trustworthy behind — a commit, an artifact, or a
    substantive crew answer? Drives the FAILED "nothing was produced" wording and
    the PARTIAL "completed but empty" gate."""
    if env.get("git_commit"):
        return True
    if env.get("answer_note"):
        return True
    arts = env.get("artifacts")
    return bool(isinstance(arts, list) and arts)


def _owner_action(kind: str, *, commit: Optional[str] = None,
                  reason: Optional[str] = None) -> dict[str, Any]:
    """The single named owner-action the card offers (§ rd-results: the unlabeled
    ``ship``/``keep`` chips become an explicit, discoverable button). Green
    resolutions hand a *merge*; failures hand a *respawn*; a live pause hands a
    *take over*."""
    labels = {
        ACT_MERGE: f"Review & merge {commit}" if commit else "Review & merge",
        ACT_REVIEW: "Review what's left",
        ACT_RESPAWN: "Respawn the loop",
        ACT_TAKE_OVER: "Take over",
        ACT_WATCH: "Watch it finish",
    }
    act: dict[str, Any] = {"kind": kind, "label": labels.get(kind, kind)}
    if commit:
        act["commit"] = commit
    if reason:
        act["reason"] = reason
    return act


def compute_verdict(env: dict) -> dict[str, Any]:
    """The COMPUTED, honest verdict for a finished (or shipping) loop. Returns::

        {value,          # one of the five, or None when there's no resolution yet
         green,          # True ONLY on a positive resolution signal (unfakeable)
         resolved,       # whether to render the Resolution card at all
         running,        # is the loop still live
         reason,         # one honest line about the outcome state
         ownerAction,    # {kind,label,...} — the one human action owed (or None)
         canRate}        # is a quality disposition offered (gated on outcome)

    Precedence is HONESTY FIRST — a crash / guardian-stop / cancel is caught
    before any green branch, so ``green`` can never be True for them:

    1. **FAILED** — external ``error`` or a raw ``ended == "error"`` (a crash).
    2. **ABANDONED—needs you** — a terminal ``guardian_stopped`` / ``stopped``
       (the engine gave up, or the run was cut short). Never green; Respawn.
    3. **SHIPPED·running** — still running, but a real commit has already landed
       (a commit is an unfakeable positive signal). Green, but nothing owed yet.
    4. **RESOLVED** — terminal + positive (``completed`` with a good ``ended``),
       tests not red, something produced. The one green terminal.
    5. **PARTIAL** — terminal + positive but incomplete: tests red, or completed
       yet left no deliverable. Amber, never green.
    6. otherwise (saved / running-with-nothing / parked on the owner) — no
       resolution yet: ``resolved=False``, ``value=None`` (the team room speaks).

    Pure; reads only the passed envelope; never raises."""
    status = env.get("status")
    done = bool(env.get("done"))
    ver = env.get("verification") if isinstance(env.get("verification"), dict) else {}
    ended = ver.get("ended")
    err = ver.get("error") or (status == "error") or (ended == "error")
    commit = _short(env.get("git_commit"))
    running = status == "running"

    # 1 — FAILED (a crash / hard error). Never green.
    if err:
        why = ("runtime died — nothing was produced" if not _produced(env)
               else "runtime died mid-flight after partial work")
        return {"value": FAILED, "green": False, "resolved": True,
                "running": False, "reason": why, "canRate": False,
                "ownerAction": _owner_action(ACT_RESPAWN, reason=why)}

    # 2 — ABANDONED — needs you (guardian gave up / run cut short). Never green.
    #     `stopped` is an external terminal; `guardian_stopped` hides inside a
    #     run.state="finished" (ended != error) — caught HERE, before green.
    if status == "stopped" or ended in _ENDED_ABANDONED:
        why = ("the engine gave up re-nudging a stalled turn"
               if ended == "guardian_stopped"
               else "the run was stopped before it resolved")
        return {"value": ABANDONED, "green": False, "resolved": True,
                "running": False, "reason": why, "canRate": False,
                "ownerAction": _owner_action(ACT_RESPAWN, reason=why)}

    # 3 — SHIPPED·running (live, but real code already landed). Green.
    if running and commit:
        return {"value": SHIPPED, "green": True, "resolved": True,
                "running": True,
                "reason": f"real code has already landed ({commit}); loop still running",
                "canRate": False,   # premature to rate a loop that hasn't ended
                "ownerAction": _owner_action(ACT_WATCH)}

    # 4/5 — a positive terminal (completed with a good `ended`).
    if done and status == "completed" and ended in _ENDED_GOOD | {None}:
        if _looks_red(ver.get("tests")):
            return {"value": PARTIAL, "green": False, "resolved": True,
                    "running": False,
                    "reason": f"completed but tests are red ({ver.get('tests')})",
                    "canRate": True,
                    "ownerAction": _owner_action(ACT_REVIEW)}
        if not _produced(env):
            return {"value": PARTIAL, "green": False, "resolved": True,
                    "running": False,
                    "reason": "completed but left no deliverable",
                    "canRate": True, "ownerAction": _owner_action(ACT_REVIEW)}
        return {"value": RESOLVED, "green": True, "resolved": True,
                "running": False,
                "reason": "the goal was met" + (f" — {commit} to land" if commit else ""),
                "canRate": True,
                "ownerAction": _owner_action(ACT_MERGE, commit=commit) if commit
                else _owner_action(ACT_REVIEW)}

    # 6 — no resolution yet (saved / running-with-nothing / parked on the owner).
    if status in ("waiting_owner", "needs_owner"):
        return {"value": None, "green": False, "resolved": False,
                "running": True, "reason": "paused — waiting on you",
                "canRate": False,
                "ownerAction": _owner_action(ACT_TAKE_OVER)}
    return {"value": None, "green": False, "resolved": False,
            "running": running, "reason": "no resolution yet", "canRate": False,
            "ownerAction": None}


def current_disposition(rows: list[dict], *,
                        run_started: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The loop's CURRENT quality verdict — a single replaceable value, NOT the
    append log (§ rd-results: "you rated this **good** · change", re-rating
    overwrites). Reads the loop's ``dispositions.jsonl`` rows, keeps only the
    outcome verbs (good/ok/bad) for THIS run (matched on ``runStarted`` when
    given), and returns the LATEST one with its ``note`` (the "why") — or ``None``
    when the owner hasn't rated it. A ``bad`` verdict carries an honest
    ``nextAction`` prompt.

    Pure; tolerant of malformed rows; never raises."""
    best: Optional[dict] = None
    best_at = None
    for r in rows or []:
        if not isinstance(r, dict) or r.get("verb") not in _OUTCOME_VERBS:
            continue
        if run_started is not None:
            rs = r.get("runStarted")
            # only judge THIS run; rows with no runStarted are kept (defensive)
            if isinstance(rs, (int, float)) and abs(rs - run_started) > 0.5:
                continue
        at = r.get("at")
        at = at if isinstance(at, (int, float)) else 0
        if best is None or at >= best_at:
            best, best_at = r, at
    if best is None:
        return None
    verb = best.get("verb")
    out: dict[str, Any] = {
        "verb": verb,
        "note": (best.get("note") or "").strip() or None,
        "at": best.get("at"),
        "source": best.get("source"),
    }
    if verb == "bad":
        out["nextAction"] = "respawn or file an issue — this didn't land"
    return out


def aggregate_dispositions(items: list[dict]) -> dict[str, Any]:
    """The per-project disposition aggregate (§ rd-results: "this team: 4 good /
    1 bad across 5 loops" — disposition fed back, not dropped into a JSONL no one
    sees). ``items`` is one entry per loop, ``{loop, current}`` where ``current``
    is that loop's :func:`current_disposition` (or ``None`` when unrated). Only the
    CURRENT verdict per loop counts — a loop rated many times contributes once, so
    the aggregate mirrors the "single replaceable verdict" model and can't be
    inflated by re-rating.

    Returns ``{good, ok, bad, rated, total, goodRate, loops:[{loop,verb,note}]}``
    — ``goodRate`` = good / rated (``None`` when nothing is rated yet, never a
    fake 0%). Pure; tolerant of malformed entries; never raises."""
    counts = {"good": 0, "ok": 0, "bad": 0}
    loops: list[dict[str, Any]] = []
    total = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        total += 1
        cur = it.get("current")
        if not isinstance(cur, dict):
            continue
        verb = cur.get("verb")
        if verb in counts:
            counts[verb] += 1
            loops.append({"loop": it.get("loop"), "verb": verb,
                          "note": cur.get("note")})
    rated = counts["good"] + counts["ok"] + counts["bad"]
    good_rate = round(counts["good"] / rated, 3) if rated else None
    return {**counts, "rated": rated, "total": total,
            "goodRate": good_rate, "loops": loops}


def build_resolution_card(name: str, *, env: dict,
                          disposition_rows: Optional[list[dict]] = None,
                          project: Optional[str] = None,
                          single_agent: bool = False,
                          archived: bool = False) -> dict[str, Any]:
    """Assemble the full Resolution card from a result envelope + the loop's
    disposition rows. The ONE call ``mcp_loops.server.loop_resolution`` makes.

    Returns ``{name, project, single_agent, verdict:{...}, resolution,
    handles:{commit,commitShort,artifacts,deliverable,deliverables}, proof:{...},
    disposition:{offered, current, autoStatus}, status}`` — a receipt that leads with the
    verdict, then the one-line resolution, then the real handles and the compact
    proof strip. ``offered`` gates the good/ok/bad chips on there being an outcome
    to judge (a crash can't be rated). ``deliverables`` is the Better-UX #3 folder
    view (multi-mark); ``autoStatus`` is the AUTOMATIC removed/deleted negative
    status that replaced the ship/keep clicks (Better-UX #5) — ``None`` when
    neither applies."""
    env = env if isinstance(env, dict) else {}
    verdict = compute_verdict(env)
    ver = env.get("verification") if isinstance(env.get("verification"), dict) else {}
    commit = env.get("git_commit") if isinstance(env.get("git_commit"), str) else None

    # the resolution line leads with the crew's real answer; fall back to the
    # honest verdict reason so the line is never blank.
    resolution = _one_line(env.get("answer_note")) or verdict.get("reason")

    artifacts = env.get("artifacts") if isinstance(env.get("artifacts"), list) else []
    duration = env.get("duration") if isinstance(env.get("duration"), dict) else {}
    turns = env.get("turns") if isinstance(env.get("turns"), dict) else {}
    run_started = duration.get("started")

    current = current_disposition(disposition_rows or [], run_started=run_started)
    folder = env.get("deliverables") if isinstance(env.get("deliverables"), dict) \
        else _deliverables.build_folder(None)

    return {
        "name": name,
        "project": project,
        "single_agent": bool(single_agent),
        "status": env.get("status"),
        "verdict": verdict,
        "resolution": resolution,
        "handles": {
            "commit": commit,
            "commitShort": _short(commit),
            # a STRING path (the frontend renders it with string ops); the full
            # ``{kind,path,exists}`` list stays under ``artifacts`` for the receipt.
            "deliverable": _pick_deliverable(artifacts),
            "deliverables": folder,
            "artifacts": artifacts,
        },
        "proof": {
            "tests": ver.get("tests"),
            "testsBy": ver.get("tests_by"),
            "turns": turns.get("main"),
            "winddown": turns.get("winddown"),
            "seconds": duration.get("seconds"),
            "signals": ver.get("signals") or [],
        },
        "disposition": {
            # GATED: a quality rating is offered ONLY when the verdict says there
            # is an outcome to judge (§ rd-results: never rate a crash "good").
            "offered": bool(verdict.get("canRate")),
            "current": current,
            "autoStatus": _deliverables.auto_status(archived=bool(archived),
                                                    folder=folder),
        },
    }
