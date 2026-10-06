"""Result envelope — fold a loop's on-disk run state into ONE structured
result an external app (Codex App / Claude Code session) can dispatch against
and pull back (CAP-2 INBOUND, north star §2a).

The envelope is the stable contract at the app-connect boundary. It is built
PURELY from what already exists on disk — the normalized ``config.json``, the
``run.json`` run state (which carries ``result`` = ``LoopResult.as_dict()`` once
the run ends), and the per-turn agent reports in ``status.jsonl``. Nothing here
spawns work, touches the network, or invents a fact it can't see. Missing
signals degrade to ``None``/``[]`` with an honest note — never a fabricated
commit or a green that didn't happen.

Envelope shape (what :func:`build_envelope` returns)::

    {
      "task_id":   "build_review_fix#1789474210",  # stable per RUN (name#started)
      "loop":      "build_review_fix",
      "status":    "completed",     # external vocab (see STATUS_* below)
      "done":      True,            # terminal? (completed | error | stopped)
      "summary":   "Loop … completed; 5 turns; ended complete. …",
      "answer_note": "Shipped the readable result view …" | None,  # §4.1 lead
      "artifacts": [{"kind": "block_scheme", "path": "…/config.html",
                     "exists": True}, …],
      "git_commit": "a1b2c3d" | None,
      "verification": {"ended": "complete", "error": None,
                       "tests": "passed:42/42" | None, "tests_note": "…",
                       "signals": ["reviewer", "tester"]},
      "duration":  {"seconds": 812.4, "started": 1789.., "ended_at": 1789..},
      "turns":     {"main": 5, "winddown": 1},
      "retired":   ["reviewer"],
      "waiting_question": None,      # set while parked on ask_owner
    }

RESULT MARKERS — how a free-text agent turn feeds the structured envelope
without new plumbing. Agents already write a one-line ``note`` via
``mcp_loops.report``; the envelope parses opt-in ``key=value`` tokens embedded
anywhere in any note (case-insensitive keys). This is the honest bridge from an
LLM turn to a machine-readable fact:

    commit=<7-40 hex>          -> git_commit         (last one wins)
    tests=passed:42/42         -> verification.tests  (any non-space token)
    tests=green | tests=red    -> verification.tests
    artifact=relpath/or/abs    -> an artifact (kind="reported"; may repeat)

A note with no markers contributes nothing — the envelope simply reports the
absence honestly (``tests_note``), so a run that never proved anything can't
masquerade as verified.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from . import deliverables, turn_identity

# ── external status vocabulary (stable at the app-connect boundary) ───────────
NOT_FOUND = "not_found"
SAVED = "saved"
RUNNING = "running"
WAITING_OWNER = "waiting_owner"
NEEDS_OWNER = "needs_owner"
COMPLETED = "completed"
ERROR = "error"
STOPPED = "stopped"

# terminal states — a caller can stop polling once `done` is True
TERMINAL = frozenset({COMPLETED, ERROR, STOPPED})

# B4: the INTERNAL run.json ``state`` terminal set — the raw on-disk vocabulary
# the server writes, distinct from the external ``status`` above. This is the
# SINGLE SOURCE OF TRUTH (server.py imports it) so the two can't drift: a
# consumer polling run.json can reliably detect "the run is over" regardless of
# OUTCOME by testing membership here. Mapping to external status:
#   finished/complete -> completed (or error, if result.ended == "error")
#   error             -> error
#   stopped           -> stopped   (what cancel_loop / loop_stop produce)
# All of them set envelope ``done`` = True. See docs/CONNECT.md.
RUN_TERMINAL = frozenset({"finished", "complete", "stopped", "error"})


def is_terminal_run_state(state: Optional[str]) -> bool:
    """True when a run.json ``state`` is terminal (run is over, any outcome).
    The reliable check for a consumer polling run.json directly."""
    return state in RUN_TERMINAL

# ── result markers agents embed in their end-of-turn note ─────────────────────
_COMMIT_RE = re.compile(r"\bcommit=([0-9a-fA-F]{7,40})\b", re.IGNORECASE)
_TESTS_RE = re.compile(r"\btests=(\S+)", re.IGNORECASE)
_ARTIFACT_RE = re.compile(r"\bartifact=(\S+)", re.IGNORECASE)


def external_status(run: Optional[dict], config: Optional[dict]) -> str:
    """Map the internal run state (+ result.ended) onto the stable external
    vocabulary. A finished run whose runner ``ended`` was an error surfaces as
    ``error``, not ``completed`` — honesty over optimism."""
    if not run and not config:
        return NOT_FOUND
    state = (run or {}).get("state", SAVED)
    if state in ("running", "stopping"):
        return RUNNING
    if state == "waiting_owner":
        return WAITING_OWNER
    if state == "needs_owner":
        return NEEDS_OWNER
    if state == "stopped":
        return STOPPED
    if state == "error":
        return ERROR
    if state in ("finished", "complete"):
        # B4: both are terminal-success run.json states; a finished run whose
        # runner ``ended`` was an error surfaces as error, not completed.
        ended = ((run or {}).get("result") or {}).get("ended")
        return ERROR if ended == "error" else COMPLETED
    # "saved" or any unknown state with a config present
    return SAVED if config else state


def _scan_markers(reports: list[dict], *,
                  manager_ids: "frozenset[str]" = frozenset(),
                  tester_ids: "frozenset[str]" = frozenset()) -> dict:
    """Parse opt-in result markers out of the agents' end-of-turn notes.
    Returns ``{commit, tests, tests_by, artifacts, signals}`` — ``signals`` names
    the agents whose notes carried a *counted* marker (provenance). Later reports
    win for scalar fields (commit/tests); artifacts accumulate.

    A5 (envelope hygiene, no lies): a ``tests=`` marker is only counted from a
    NON-manager agent. The manager gives DECISIONS (continue / wind_down /
    complete), not test results — so a manager note that happens to contain
    ``tests=green`` must NOT flip verification.tests before an actual tester ran.
    ``commit=`` and ``artifact=`` are still harvested from ANY agent.

    P3b (authoritative test attribution): ``tests_by`` records WHO actually ran
    the suite, so an independent tester's attribution must not be silently stolen
    by a later worker that merely *re-echoes* ``tests=`` in its own note. We rank
    the reporter's authority — a dedicated tester (``input_provider``, in
    ``tester_ids``) ranks ABOVE a builder (worker) — and only let a NEW ``tests=``
    marker overwrite the value+attribution when its authority is >= the standing
    one. So: tester green then writer re-emits → ``tests_by`` stays the tester;
    but a lone writer (no tester in the loop) still attributes to itself, and a
    tester can always update its OWN prior result. Managers are excluded upstream."""
    commit: Optional[str] = None
    tests: Optional[str] = None
    tests_by: Optional[str] = None
    tests_rank: int = -1                          # authority of whoever set tests
    artifacts: list[str] = []
    signals: list[str] = []
    for e in reports or []:
        note = e.get("note")
        if not isinstance(note, str) or not note:
            continue
        agent = e.get("agent")
        hit = False
        m = _COMMIT_RE.search(note)
        if m:
            commit = m.group(1)
            hit = True
        m = _TESTS_RE.search(note)
        if m and agent not in manager_ids:      # A5: never a manager-sourced green
            rank = 2 if agent in tester_ids else 1   # tester outranks writer
            if rank >= tests_rank:               # P3b: no lower-authority override
                tests = m.group(1)
                tests_by = agent if isinstance(agent, str) else None
                tests_rank = rank
            hit = True                           # the note still carried a signal
        for am in _ARTIFACT_RE.finditer(note):
            # artifact=none -> no file; a/{x,y}.md -> both files
            for path in deliverables.marker_paths(am.group(1)):
                if path not in artifacts:
                    artifacts.append(path)
            hit = True
        if hit and isinstance(agent, str) and agent not in signals:
            signals.append(agent)
    return {"commit": commit, "tests": tests, "tests_by": tests_by,
            "artifacts": artifacts, "signals": signals}


def _reports_for_run(reports: list[dict], started: Optional[float] = None, *,
                     run: Optional[dict] = None) -> list[dict]:
    """P3a (no cross-run leak): scope the per-turn reports to the CURRENT run's
    window. ``status.jsonl`` accumulates across every run of a loop name, and the
    envelope harvests its markers (commit / tests) and last-note summary from it —
    so a fresh ``start_loop`` over a prior run of the SAME name would otherwise
    surface the OLD run's ``commit=``/summary until this run republishes them.

    Each report carries a ``ts`` (mcp_loops.report stamps ``time.time()``); the run
    records its ``started``. Keep only reports at or after ``started``. A report
    with no ``ts`` is kept (defensive — hand-built/legacy entries), and with no
    ``started`` (never-run / saved) nothing is dropped.

    H7: pass ``run`` (run.json) to key on its ``runId`` — a v2 row belongs to the
    run iff its ``runId`` matches; unstamped rows keep the ts-window fallback
    (:func:`mcp_loops.turn_identity.rows_for_run`, the ONE shared rule)."""
    return turn_identity.rows_for_run(
        reports, run if run is not None else {"started": started})


def _artifact(kind: str, path: str) -> dict:
    return {"kind": kind, "path": path, "exists": bool(path) and os.path.exists(path)}


def _collect_artifacts(name: str, status_dir: Optional[str],
                       output_dir: Optional[str], marker_artifacts: list[str]) -> list[dict]:
    """The honest artifact list: known engine-produced files that ACTUALLY
    exist (block-scheme, finish report, output folder) plus any paths agents
    reported via ``artifact=`` markers. A reported path is resolved relative to
    the output folder when it isn't absolute; ``exists`` is checked so a caller
    never chases a path the loop only claimed."""
    out: list[dict] = []
    if status_dir:
        html = os.path.join(status_dir, "config.html")
        if os.path.exists(html):
            out.append(_artifact("block_scheme", html))
        finish = os.path.join(status_dir, "finish-report.md")
        if os.path.exists(finish):
            out.append(_artifact("finish_report", finish))
    if output_dir and os.path.isdir(output_dir):
        out.append(_artifact("output_dir", output_dir))
        # A2: the engine also drops finish-report.md INTO the output tree (what an
        # app-connect caller browses as output_dir). Surface it explicitly so the
        # caller doesn't have to know the filename — honest `exists` either way.
        fin_out = os.path.join(output_dir, "finish-report.md")
        if os.path.exists(fin_out):
            out.append(_artifact("finish_report", fin_out))
    for rel in marker_artifacts:
        # same resolution as the deliverables folder (data-root/workspace-relative
        # markers find their real file instead of reading as missing)
        out.append(_artifact("reported",
                             os.path.normpath(deliverables._resolve(rel, output_dir))))
    return out


def _summary(name: str, status: str, run: Optional[dict],
             result: dict, reports: list[dict]) -> str:
    """A one-paragraph human summary. Prefer the finish-report the engine wrote
    on loop end; else compose from result state + the last agent note. Never
    fabricates an outcome — it states what the run state says."""
    fin = (run or {}).get("finish_report") or {}
    if isinstance(fin, dict) and fin.get("text"):
        return str(fin["text"]).strip()
    turns = result.get("turns_used")
    ended = result.get("ended")
    parts = [f"Loop {name!r} {status}"]
    if isinstance(turns, int):
        parts.append(f"{turns} turn(s)")
    if ended:
        parts.append(f"ended {ended}")
    line = "; ".join(parts) + "."
    last = next((e.get("note") for e in reversed(reports or [])
                 if e.get("note")), None)
    if last:
        line += f" Last note: {str(last)[:200]}"
    if result.get("error"):
        line += f" Error: {result['error']}"
    return line


def _answer_note(reports: list[dict],
                 manager_ids: "frozenset[str]" = frozenset()) -> Optional[str]:
    """The last SUBSTANTIVE non-manager agent note — the ANSWER a finished loop
    leads with (§4.1). Exposed as its own field (not baked into the receipt
    string) so the result view can lead with the product, not the machinery.

    Managers are EXCLUDED: a manager's last note is a wind-down VERDICT
    (continue / wind_down / complete), which is a status line, not the deliverable
    (R1). A note that is only result markers (``commit=``/``tests=``/``artifact=``)
    carries no prose answer, so we prefer the last note that still has real text;
    if none does, fall back to the last non-manager note as-is. ``None`` when the
    loop has produced no non-manager note yet."""
    fallback: Optional[str] = None
    for e in reversed(reports or []):
        note = e.get("note")
        if not isinstance(note, str) or not note.strip():
            continue
        if e.get("agent") in manager_ids:
            continue
        if fallback is None:
            fallback = note.strip()
        prose = _ARTIFACT_RE.sub("", _TESTS_RE.sub("", _COMMIT_RE.sub("", note)))
        if prose.strip():
            return note.strip()
    return fallback


def _turns(result: dict, live_turns: Optional[dict]) -> dict:
    """A7 (turn accounting). Prefer the terminal result's counts (authoritative);
    while the run is still going the result is empty, so fall back to the live
    ``progress.json`` counts the runner publishes each turn — so ``turns.main`` is
    a REAL running count, not stuck at 0 until the loop ends."""
    main = result.get("turns_used")
    wind = result.get("winddown_turns")
    if main is None and isinstance(live_turns, dict):
        main = live_turns.get("turns_used")
        wind = live_turns.get("winddown_turns")
    return {"main": main or 0, "winddown": wind or 0}


def build_envelope(name: str, *, run: Optional[dict], config: Optional[dict],
                   reports: Optional[list[dict]] = None,
                   status_dir: Optional[str] = None,
                   output_dir: Optional[str] = None,
                   waiting_question: Optional[str] = None,
                   live_turns: Optional[dict] = None,
                   now: Optional[float] = None,
                   with_deliverables: bool = True) -> dict[str, Any]:
    """Fold a loop's on-disk state into the app-connect result envelope (pure).

    ``run``/``config`` are the parsed ``run.json``/``config.json`` (either may
    be ``None``). ``reports`` are the non-machinery ``status.jsonl`` entries.
    ``status_dir``/``output_dir`` locate engine artifacts. ``now`` is used only
    to compute elapsed time for a still-running loop. Returns the envelope dict
    described in the module docstring — a ``not_found`` envelope when neither a
    run nor a config exists for ``name``. ``with_deliverables=False`` skips the
    deliverables-folder walk (``deliverables`` is then ``None``) for callers that
    only need the verdict inputs, e.g. the dashboard's polled loop list."""
    reports = reports or []
    status = external_status(run, config)
    result = (run or {}).get("result") or {}
    started = (run or {}).get("started")
    # P3a: only THIS run's reports feed the markers/summary (no prior-run leak).
    reports = _reports_for_run(reports, run=run or {})
    # A5/P3b: role-based provenance. Managers' tests= markers are never counted
    # (a manager signals decisions, not outcomes); a dedicated tester
    # (input_provider) is the authoritative test source and outranks a worker
    # that merely re-echoes tests= (P3b). The role literals mirror schema.MANAGER /
    # schema.INPUT_PROVIDER — kept as literals so envelope.py stays import-free of
    # schema.
    steps = (config or {}).get("steps") or {}
    manager_ids = frozenset(
        sid for sid, s in steps.items()
        if isinstance(s, dict) and s.get("role") == "manager")
    tester_ids = frozenset(
        sid for sid, s in steps.items()
        if isinstance(s, dict) and s.get("role") == "input_provider")
    markers = _scan_markers(reports, manager_ids=manager_ids, tester_ids=tester_ids)

    # git commit: an explicit run field wins over a marker (the run field is the
    # engine/harness-stamped truth); both may be absent -> null, never invented.
    run_commit = (run or {}).get("git_commit")
    git_commit = run_commit if isinstance(run_commit, str) and run_commit.strip() \
        else markers["commit"]

    ended_at = (run or {}).get("updated") if status in TERMINAL else None
    seconds: Optional[float] = None
    if isinstance(started, (int, float)):
        end = ended_at if isinstance(ended_at, (int, float)) else now
        if isinstance(end, (int, float)):
            seconds = round(float(end) - float(started), 1)

    task_id = f"{name}#{int(started)}" if isinstance(started, (int, float)) else name

    tests = markers["tests"]
    verification = {
        "ended": result.get("ended"),
        "error": result.get("error"),
        "tests": tests,
        "tests_by": markers["tests_by"],           # A5: which agent flipped it (provenance)
        "tests_note": None if tests else
        "no test signal reported (a non-manager agent emits tests=<result> in its note)",
        "signals": markers["signals"],
    }

    return {
        "task_id": task_id,
        "loop": name,
        "status": status,
        "done": status in TERMINAL,
        "summary": _summary(name, status, run, result, reports),
        # §4.1: the ANSWER as its own field — the last substantive NON-manager
        # agent note. The result view leads with THIS (+ output files + commit),
        # not `summary` (the finish-report receipt, R1). `None` -> no answer yet.
        "answer_note": _answer_note(reports, manager_ids),
        "artifacts": _collect_artifacts(name, status_dir, output_dir,
                                        markers["artifacts"]),
        # Better-UX #3: the deliverables FOLDER (<output>/deliverables/ + every
        # artifact=-marked file, multi-mark, who marked it).
        "deliverables": (deliverables.build_folder(output_dir, reports)
                         if with_deliverables else None),
        "git_commit": git_commit if git_commit else None,
        "verification": verification,
        "duration": {"seconds": seconds, "started": started, "ended_at": ended_at},
        "turns": _turns(result, live_turns),
        "retired": result.get("retired") or [],
        "waiting_question": waiting_question,
    }
