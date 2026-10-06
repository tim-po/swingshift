"""Tiny end-of-turn status reporter — every loop agent calls this to finish a turn.

    python -m mcp_loops.report <loop> <agent> <status> "<short note>" [--gist "<one-line reasoning>"]

It appends one JSON line to the loop's status log, which the headless substrate
tails to learn how a turn ended (and thus whether to repeat / retire / wind down).
Kept dependency-free and fail-soft: a bad status is reported back so the agent can
correct, but writing never raises into the agent's shell.

Each report also appends one capped line to the loop's thought-log
(``thoughtlog.jsonl``, see :mod:`mcp_loops.thoughtlog`): the optional ``--gist``
if given, else the note. Only that capped gist is stored — never turn I/O.

Status vocab by role (the engine validates against the agent's actual role):
  worker         → completed | work_remaining
  input_provider → satisfied | minor_only | needs_work
  manager        → continue | ask_owner | wind_down | complete
"""

from __future__ import annotations

import json
import os
import sys
import time

from mcp_loops import paths, turn_identity
from mcp_loops.schema import STATUS_VOCAB

ALL_STATUSES = frozenset(s for v in STATUS_VOCAB.values() for s in v)


def status_dir(loop: str) -> str:
    # explicit-arg > $LOOPS_DATA_DIR > <install>/data/_loops — one shared rule.
    return os.path.join(paths.resolve_data_dir(), loop)


def status_log(loop: str) -> str:
    return os.path.join(status_dir(loop), "status.jsonl")


def disposition_log(loop: str) -> str:
    """The invisible-disposition log (SLICE-1 §4.2): an append-only sibling of
    ``status.jsonl`` in the loop's LOCAL status_dir — NEVER a Project checkout.
    That one placement is what lets a disposition be captured with zero setup
    (no Project, no ``_resolve_checkout_for``) and survive a re-run (the run
    cannot wipe an append-only sibling). Mirrors :func:`status_log`."""
    return os.path.join(status_dir(loop), "dispositions.jsonl")


# ── output tree (persisted artefacts a caller browses; NOT the engine log) ──
# One shared rule so the server, the substrate, and any dashboard route resolve
# the SAME place: $LOOPS_OUTPUT_DIR > <data_dir sibling>/_output. The data dir is
# ``.../_loops``; the output tree sits beside it as ``.../_loops/_output`` (what
# server._output_base historically computed), keeping per-loop artefacts under
# ``_output/<loop>/`` for the app-connect / dashboard browse surface.
def output_base() -> str:
    return os.environ.get(
        "LOOPS_OUTPUT_DIR",
        os.path.join(os.path.dirname(status_dir("_")), "_output"))


def output_dir(loop: str) -> str:
    return os.path.join(output_base(), loop)


def transcript_name(agent: str, turn: int) -> str:
    """The basename of a persisted turn transcript (Q2): ``<agent>-<turn>.txt``
    with a filesystem-safe agent segment. Shared by the WRITER (the substrate)
    and every READER (loop_turn_detail, the issue writer) so the name has ONE
    definition and a reader never guesses a name the writer didn't use."""
    return f"{_safe_segment(agent)}-{int(turn):03d}.txt"


def transcript_path(loop: str, agent: str, turn: int) -> str:
    """Where a single agent turn's raw transcript is persisted (Q2 observability):
    ``_output/<loop>/<agent>-<turn>.txt`` with a filesystem-safe agent segment."""
    return os.path.join(output_dir(loop), transcript_name(agent, turn))


def _safe_segment(name: str) -> str:
    """Collapse a name to a safe filename segment (``[A-Za-z0-9._-]``) so a hostile
    or odd agent id can never escape the output dir or clobber a sibling path."""
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(name))
    return out.strip("._") or "agent"


# ── issue store + engine log (Q3 observability) ─────────────────────────────
# Terminal loop failures become durable local issues. One shared rule so the
# WRITER (the engine's terminal-fail hook) and the READER (the dashboard Issues
# view) resolve the same place: ``<data_dir>/_issues`` — beside the ``_output``
# tree, both under the resolved loops data root so a fresh/overridden install is
# self-contained.
def issues_dir() -> str:
    return os.path.join(paths.resolve_data_dir(), "_issues")


def ideahub_dir() -> str:
    """The Idea Hub living-document store (REDESIGN-SPEC §6 rd-ideahub): one file
    per doc + an ``index.jsonl`` under ``<data_root>/_ideahub`` — a sibling of the
    ``_issues`` and ``_output`` trees, all under the resolved loops data root so a
    fresh/overridden install is self-contained. One shared rule so the WRITER (the
    doc tools) and every READER (the Hub list/get) resolve the SAME place."""
    return os.path.join(paths.resolve_data_dir(), "_ideahub")


def threads_dir() -> str:
    """The objective-manager THREAD store (REDESIGN-SPEC §8 / Q2): the durable,
    doc-anchored conversation that steers a Hub objective — one file per doc-thread
    under ``<data_root>/_threads``, a sibling of the ``_ideahub``/``_issues``/
    ``_output`` trees so a fresh/overridden install is self-contained. One shared
    rule so the WRITER (the thread tools) and every READER resolve the SAME place;
    the thread is keyed by the doc it is docked to so closing the tab and returning
    tomorrow lands on the same conversation."""
    return os.path.join(paths.resolve_data_dir(), "_threads")


def engine_log_path() -> str:
    """The engine's stdout log (``mcp_loops.log``) — the multi-MB firehose an
    issue quotes a *slice* of. Overridable via ``$MCP_LOOPS_LOG``; defaults to the
    data root's parent (``.../data/mcp_loops.log`` on the live box)."""
    return os.environ.get(
        "MCP_LOOPS_LOG",
        os.path.join(os.path.dirname(paths.resolve_data_dir()), "mcp_loops.log"))


def latest_transcript(loop: str, agent: str) -> "str | None":
    """Newest persisted transcript (Q2) for an agent, or None. Turn numbers are
    zero-padded (``-NNN``) so the lexical max is the latest turn — this decouples
    the issue writer from the substrate's internal turn counter."""
    import glob
    hits = sorted(glob.glob(os.path.join(output_dir(loop), f"{_safe_segment(agent)}-*.txt")))
    return hits[-1] if hits else None


def record(loop: str, agent: str, status: str, note: str = "",
           *, turn: int | None = None) -> dict:
    """Append one status row. H7: a run started with a ``runId`` gets a v2 row
    ``{v:2, runId, turn}`` — ``turn`` from the substrate's per-run cursor unless
    passed explicitly; a run without one gets the unchanged v1 row."""
    d = status_dir(loop)
    ident: dict = {}
    try:
        ident = turn_identity.stamp(d, agent)
    except Exception:  # noqa: BLE001 — identity is best-effort, the report is not
        ident = {}
    if turn is None:
        turn = ident.get("turn")
    entry = {
        "ts": time.time(), "loop": loop, "agent": agent,
        "status": status, "note": note, "turn": turn,
        "valid": status in ALL_STATUSES,
    }
    if ident:
        entry["v"], entry["runId"] = ident["v"], ident["runId"]
    os.makedirs(d, exist_ok=True)
    with open(status_log(loop), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def record_thought(loop: str, agent: str, status: str, note: str = "",
                   gist: str | None = None) -> dict | None:
    """Append the turn's capped one-line gist to the thought-log; fail-soft
    (a thought-log hiccup must never cost the status report)."""
    try:
        from mcp_loops import thoughtlog
        return thoughtlog.append(status_dir(loop), loop, agent, status,
                                 note=note, gist=gist)
    except Exception:  # noqa: BLE001
        return None


def _pop_gist(argv: list[str]) -> str | None:
    """Remove an optional ``--gist <text>`` / ``--gist=<text>`` from argv."""
    for i, a in enumerate(argv):
        if a == "--gist":
            val = argv[i + 1] if i + 1 < len(argv) else ""
            del argv[i:i + 2]
            return val
        if a.startswith("--gist="):
            del argv[i]
            return a[len("--gist="):]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    gist = _pop_gist(argv)
    if len(argv) < 3:
        print(__doc__)
        return 2
    loop, agent, status = argv[0], argv[1], argv[2]
    note = argv[3] if len(argv) > 3 else ""
    try:
        entry = record(loop, agent, status, note)
    except Exception as e:  # noqa: BLE001 — never raise into the agent shell
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 0
    record_thought(loop, agent, status, note, gist)
    if not entry["valid"]:
        print(json.dumps({
            "warning": f"unknown status {status!r}",
            "expected_one_of": sorted(ALL_STATUSES),
            "recorded": True,
        }))
    else:
        print(json.dumps({"recorded": True, "status": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
