"""Interactive Loops panel for the self-hosted Loopyard dashboard.

Two data paths, deliberately kept separate:

* READ path (file-based, server-independent): reads the loop state files the
  runner already writes — ``data/_loops/<name>/{config.json,run.json,
  status.jsonl,live.json,input-queue.jsonl}`` — and renders states, the live
  in-flight marker (#5), parallel-group + sub-loop machinery events (#3,#4),
  pending owner steering notes, and per-turn logs. Fail-soft: a read error on
  one loop degrades that card, never the page. This path also works for the
  FLEET mirror dirs, where no local server manages the loop.

* ACTION path (client of the mcp-loops server over MCP-streamable-HTTP): the
  panel is no longer read-only. stop/start, mid-run input, answering a
  waiting_owner question, archive/unarchive, per-turn drill-in, the registry
  browser + clone, and on-demand analysis all proxy to the running mcp-loops
  server (``MCP_LOOPS_URL``, default the live 127.0.0.1:8771/mcp). These are
  offered ONLY for ``local`` loops — remote fleet mirrors stay read-only.

FLEET VIEW: besides this host's ``data/_loops`` (host label "local"), it also
reads any ``data/_loops_<host>`` MIRROR dir (e.g. ``data/_loops_anneke``, kept
fresh by a periodic rsync). Each loop card is tagged with its host.

Wire into gate.py (auto-gated by the cookie-auth wrapper) — static paths BEFORE
the ``{name}`` route so ``/registry`` and ``/clone`` are not eaten as a name:
    from tracking_ui import loops_panel
    Route("/loops", loops_panel.loops_page, methods=["GET"]),
    Route("/api/loops", loops_panel.loops_list_api, methods=["GET"]),
    Route("/api/loops/registry", loops_panel.loops_registry_api, methods=["GET"]),
    Route("/api/loops/clone", loops_panel.loop_clone_api, methods=["POST"]),
    Route("/api/loops/{name}", loops_panel.loop_detail_api, methods=["GET"]),  # ?host=
    Route("/api/loops/{name}/turn", loops_panel.loop_turn_api, methods=["GET"]),
    Route("/api/loops/{name}/action", loops_panel.loop_action_api, methods=["POST"]),
    Route("/api/loops/{name}/disposition", loops_panel.loop_disposition_api, methods=["POST"]),
    Route("/api/loops/{name}/brief", loops_panel.loop_brief_api, methods=["POST"]),
    Route("/api/loops/{name}/debrief", loops_panel.loop_debrief_api, methods=["POST"]),
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

# Loop attribution (REDESIGN-SPEC §3): a loop's project is the CONFIDENT signal
# resolved read-time by ``mcp_loops.schema.resolve_project`` — ONLY an explicit
# projectId/productId on the config, else None → honest "Unattributed" (inference
# is a suggestion, never a binding: loopyard-bug-1790177434). NEVER the checkout's git remote
# or the loop slug (both launder the whole fleet into one dead bucket, which §3
# forbids). ``is_single_agent`` marks a loop-of-1 for the single-agent chip (§Q1).
from mcp_loops import schema as _schema
from mcp_loops import teamroom as _teamroom
from mcp_loops import envelope as _envelope
from mcp_loops import resolution as _resolution
from mcp_loops import turn_identity as _turn_identity

# R17: the local loops dir is the one the engine resolves (LOOPS_DATA_DIR >
# LOOPYARD_HOME > install default); BOT_SQUAD_HOME is a deprecated alias for
# ``<home>/data/_loops`` when LOOPS_DATA_DIR is unset.
from mcp_loops import paths as _paths
_HOME = _paths.legacy_home_override("loops_panel")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# the "no project binding" scope id (frontend PROJECT_UNATTR, server UNATTRIBUTED_SCOPE)
_UNATTRIBUTED_SCOPE = "__unattributed__"

_log = logging.getLogger("tracking_ui.loops_panel")


def _error_ref(where: str, exc: BaseException) -> str:
    """Log an unexpected failure server-side (full detail + traceback) and return
    a short opaque id for the client. Raw exception text is never sent to the
    browser — an OSError string carries absolute on-disk paths
    (loopyard-bug-1790562070); the id lets an operator find the log line."""
    eid = secrets.token_hex(4)
    _log.error("%s failed [err %s]: %s: %s", where, eid, type(exc).__name__, exc,
               exc_info=exc)
    return eid


def _internal_error(where: str, exc: BaseException) -> str:
    """Generic client-facing message for an unexpected failure (see _error_ref)."""
    return f"internal error (id {_error_ref(where, exc)})"

# §4.1 result-view markers — mirror mcp_loops.envelope's opt-in note markers so
# the dashboard reader can lead with the ANSWER (non-manager note + commit +
# declared artifact) without importing the envelope module. Kept in sync by
# construction: same patterns, same meaning.
_COMMIT_RE = re.compile(r"\bcommit=([0-9a-fA-F]{7,40})\b", re.IGNORECASE)
_TESTS_RE = re.compile(r"\btests=(\S+)", re.IGNORECASE)
_ARTIFACT_RE = re.compile(r"\bartifact=(\S+)", re.IGNORECASE)
_BRANCH_RE = re.compile(r"\bbranch=(\S+)", re.IGNORECASE)
# a per-turn transcript file the engine drops into the output tree
# (report.transcript_name -> "<agent>-NNN.txt"): demoted below the deliverable.
_TRANSCRIPT_RE = re.compile(r"-\d+\.txt$")

# Where the interactive controls send commands. Default = the live server; the
# scratch test harness points this at a spare-port server (e.g. :8781).
_MCP_URL = os.environ.get("MCP_LOOPS_URL", "http://127.0.0.1:8771/mcp")


def _sources() -> list[tuple[str, Path]]:
    """(host_label, loops_dir) for every source: this host's live dir + any
    data/_loops_<host> mirror. Ordered local-first."""
    if os.environ.get("LOOPS_DATA_DIR") or _HOME is None:
        local = Path(_paths.resolve_data_dir())
    else:
        local = Path(_HOME) / "data" / "_loops"
    out: list[tuple[str, Path]] = [("local", local)]
    try:
        # mirrors are siblings of the RESOLVED dir (where sync scripts write)
        for d in sorted(local.parent.glob("_loops_*")):
            if d.is_dir():
                out.append((d.name[len("_loops_"):], d))
    except OSError:
        pass
    return out


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _project_binding(cfg: dict, name: str) -> dict:
    """The loop card's project binding — the CONFIDENT project the loop belongs to
    (REDESIGN-SPEC §3), resolved read-time by :func:`mcp_loops.schema.resolve_project`:
    ONLY an explicit ``projectId``/``productId``, else ``None`` → honest
    **"Unattributed"** (a goal target / git remote is merely a suggestion —
    loopyard-bug-1790177434). It NEVER
    derives from the checkout's git remote or the loop slug — both launder the whole
    fleet into one dead bucket, the §3 violation the round-1 inputs flagged. Emits
    ``project`` (canonical) + ``product`` (legacy alias) as the grouping/binding key
    and ``productName`` as the header label (the id doubles as its label). All keys
    ``None`` when unattributed. Pure, read-time, never raises."""
    pid = _schema.resolve_project(cfg, loop_name=name)
    return {"project": pid, "product": pid, "productName": pid}


def _team(cfg: dict) -> dict:
    steps = (cfg or {}).get("steps", {}) or {}
    manager, workers, inputs, loops = None, [], [], []
    for sid, s in steps.items():
        if s.get("type") == "loop":
            loops.append(sid)
        elif s.get("role") == "manager":
            manager = sid
        elif s.get("role") == "worker":
            workers.append(sid)
        elif s.get("role") == "input_provider":
            inputs.append(sid)
    return {"manager": manager, "workers": workers, "inputs": inputs, "subloops": loops}


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
    except (FileNotFoundError, OSError):
        pass
    return out


def _annotated_status(root: Path, name: str, n: int) -> list[dict]:
    """The last ``n`` status.jsonl events, each non-machinery report of the
    CURRENT run annotated with its 1-based per-run, per-agent turn index
    (``seq``) — the same index the engine keys the framed prompt file
    ``prompts/<agent>-NNN.txt`` on, so a log row can drill into its own turn
    (#6). H7: keyed on (runId, agent, turn) via the shared
    :mod:`mcp_loops.turn_identity` helper (legacy rows: the run's ts-window +
    position); a prior run's rows get ``seq: None`` — their prompt files were
    reused by this run, so they cannot be opened. Machinery events
    (parallel/sub-loop, #3/#4) are passed through untouched with
    ``kind:"machinery"``."""
    events = _read_jsonl(root / name / "status.jsonl")
    run = _read_json(root / name / "run.json") or {}
    in_run = _turn_identity.rows_for_run(events, run)
    for e, t in zip(in_run, _turn_identity.number_turns(in_run)):
        if t is not None:
            e["seq"] = t
    mine = {id(e) for e in in_run}
    for e in events:
        if (id(e) not in mine and e.get("kind") != "machinery"
                and isinstance(e.get("agent"), str)):
            e["seq"] = None
    return events[-n:] if n and n < len(events) else events


def _pending_input(root: Path, name: str) -> list[dict]:
    """Owner steering notes queued but not yet drained by the manager (#1)."""
    items = _read_jsonl(root / name / "input-queue.jsonl")
    cur = 0
    try:
        with open(root / name / "input-queue.cursor", encoding="utf-8") as fh:
            cur = int(fh.read().strip() or "0")
    except (FileNotFoundError, ValueError, OSError):
        cur = 0
    return items[cur:]


def _sub_agent_map(cfg: dict) -> tuple[dict, dict]:
    """Map every agent nested under a TOP-LEVEL sub-loop step to that step id,
    and each top sub-loop step to its display (child loop) name. Lets the UI
    attribute concurrently-interleaved turns to the right sibling sub-loop when
    sub-loops run in a parallel group (their status.jsonl events interleave)."""
    a2s: dict = {}
    names: dict = {}

    def collect(steps: dict, top: str) -> None:
        for sid, s in (steps or {}).items():
            if isinstance(s, dict) and s.get("type") == "loop":
                collect((s.get("loop") or {}).get("steps") or {}, top)
            else:
                a2s[sid] = top

    for sid, s in (cfg.get("steps") or {}).items():
        if isinstance(s, dict) and s.get("type") == "loop":
            names[sid] = (s.get("loop") or {}).get("name") or sid
            collect((s.get("loop") or {}).get("steps") or {}, sid)
    return a2s, names


def _external_status(run: dict) -> str | None:
    """A loop's terminal-aware status for the Runs feed + card — mirrors the CLI
    result-envelope vocab (running | completed | error | stopped | waiting_owner |
    needs_owner | saved). ``None`` only when the loop has never run. Fixes the Runs
    view showing ``status:None`` for a finished front-door loop."""
    state = (run or {}).get("state")
    if not state:
        return None
    if state in ("running", "stopping"):
        return "running"
    if state in ("finished", "complete"):
        ended = ((run or {}).get("result") or {}).get("ended")
        return "error" if ended == "error" else "completed"
    return state              # saved | stopped | error | waiting_owner | needs_owner


def _loop_summary_line(run: dict, recent: list[dict]) -> str | None:
    """A one-line human summary a finished loop leaves. Prefer the engine's
    finish-report headline (which now names what was produced + where), else the
    last agent note. ``None`` when the loop has produced no signal yet."""
    fr = ((run or {}).get("finish_report") or {}).get("text")
    if isinstance(fr, str) and fr.strip():
        for ln in fr.splitlines():
            head = ln.strip().lstrip("#").strip()
            if head:
                return head[:200]
    note = recent[-1].get("note") if recent else None
    return note[:200] if isinstance(note, str) and note.strip() else None


def _mtime_ns(path: Path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


# loop dir -> (file stamp, compact result). The list is polled; a finished
# loop's verdict only changes when its run/status/config files do.
_RESULT_CACHE: dict = {}


def _result_summary(root: Path, host: str, name: str, cfg: dict, run: dict) -> dict | None:
    """The compact ``result`` a FINISHED loop's list row carries (loopyard-bug-
    1790089836-2) — what Overview's "Recent results" renders without a per-loop
    round-trip::

        {ended, verdict, green, resolution, commit, turns_used, winddown_turns}

    ``verdict``/``green``/``resolution`` are the SAME computed, honest values the
    loop's Resolution card shows (``resolution.compute_verdict`` over the result
    envelope), so a row can never read greener than its card. No event arrays.
    ``None`` while the loop has not reached a terminal run state."""
    if run.get("state") not in _envelope.RUN_TERMINAL:
        return None
    d = root / name
    stamp = (_mtime_ns(d / "run.json"), _mtime_ns(d / "status.jsonl"),
             _mtime_ns(d / "config.json"))
    hit = _RESULT_CACHE.get(str(d))
    if hit and hit[0] == stamp:
        return dict(hit[1])
    reports = [e for e in _read_jsonl(d / "status.jsonl") if e.get("kind") != "machinery"]
    out_dir = None
    if host == "local":                   # a mirror holds status only, never the output tree
        od = Path(os.environ.get("LOOPS_OUTPUT_DIR") or (root / "_output")) / name
        out_dir = str(od) if od.is_dir() else None
    env = _envelope.build_envelope(name, run=run, config=cfg, reports=reports,
                                   status_dir=str(d), output_dir=out_dir,
                                   with_deliverables=False)
    verdict = _resolution.compute_verdict(env)
    res = run.get("result") or {}
    turns = _turns(root, name, run, (cfg.get("budget") or {}).get("turnLimit"))
    commit = env.get("git_commit")
    out = {
        "ended": res.get("ended"),
        "verdict": verdict.get("value"),
        "green": bool(verdict.get("green")),
        "resolution": _resolution._one_line(env.get("answer_note")) or verdict.get("reason"),
        "commit": commit[:7] if isinstance(commit, str) else None,
        # Same turn contract as the row (see _turns): main-phase turns capped at
        # turnLimit, so no field named turns_used ever reads over its limit.
        "turns_used": turns["budgeted"],
        "winddown_turns": turns["winddown"],
    }
    _RESULT_CACHE[str(d)] = (stamp, out)
    return dict(out)


_LIVE_STATES = ("running", "stopping", "waiting_owner", "needs_owner")


def _turns(root: Path, name: str, run: dict, limit) -> dict:
    """Turn accounting for a list row (loopyard-bug-1790089836). The contract:

    * ``main`` — MAIN-phase turns, the ones budgeted by ``turnLimit``. The
      runner checks the limit only between rounds, so the last round may finish
      a few turns past it; ``over`` = ``max(0, main - limit)`` says by how much.
    * ``winddown`` — the guaranteed wind-down tail, which runs BEYOND the budget
      and never counts against it.
    * ``budgeted`` — ``min(main, limit)``: the numerator for a ``x / turnLimit``
      fraction, so a progress display can never exceed its limit.

    A finished run reads its final ``result``; a live run reads the runner's
    per-turn ``progress.json`` (ignored when it predates this run's start)."""
    result = run.get("result") or {}
    main, wind = result.get("turns_used"), result.get("winddown_turns")
    if main is None and run.get("state") in _LIVE_STATES:
        live = _read_json(root / name / "progress.json") or {}
        started = run.get("started") or 0
        if isinstance(live, dict) and (live.get("updated") or 0) >= started:
            main, wind = live.get("turns_used"), live.get("winddown_turns")
    main = main if isinstance(main, int) else None
    wind = wind if isinstance(wind, int) else (0 if main is not None else None)
    lim = limit if isinstance(limit, int) and limit > 0 else None
    return {"main": main, "winddown": wind, "limit": lim,
            "budgeted": (min(main, lim) if (main is not None and lim) else main),
            "over": (max(0, main - lim) if (main is not None and lim) else 0)}


def _dispatched_origin(loop_dir: Path) -> str | None:
    """The remote origin the Hub dispatched this saved loop to (the engine's
    ``dispatched.json`` beside config.json), else None."""
    d = _read_json(loop_dir / "dispatched.json")
    o = d.get("origin") if isinstance(d, dict) else None
    return o if isinstance(o, str) and o and o != "local" else None


def _summary(root: Path, host: str, name: str) -> dict:
    d = root / name
    cfg = _read_json(d / "config.json") or {}
    run = _read_json(d / "run.json") or {}
    recent = _annotated_status(root, name, 1)
    result = run.get("result") or {}
    turn_limit = (cfg.get("budget") or {}).get("turnLimit")
    turns = _turns(root, name, run, turn_limit)
    return {
        "name": name, "host": host,
        "state": run.get("state", "saved"),
        "slug": run.get("slug"),
        "archived": bool(run.get("archived")),
        # E3: a first-class sub-loop child carries a link back to its parent loop
        # (kind=="subloop"), written by the runner's FirstClassSubloopDispatcher.
        # Surfaced so the loops view can label a `<parent>.<step>` row as a sub-loop
        # of its parent instead of an unexplained dotted sibling. Absent (None) for
        # every ordinary top-level loop — purely additive, no existing row changes.
        "kind": run.get("kind"),
        "parent": run.get("parent"),
        "parentStep": run.get("parentStep"),
        "started": run.get("started"), "updated": run.get("updated"),
        "question": run.get("question"), "guardian_alert": run.get("guardian_alert"),
        "goal": (cfg.get("goal") or "")[:220],
        # D-FE (read side): surface a loop's product + origin BINDINGS straight
        # from its raw config, for display only. Purely additive and defensive —
        # today no config carries these (they arrive with Phase C/D-schema + the
        # creator New-Team flow), so both are None for every existing loop and no
        # card changes. `origin` is the intended target box (distinct from
        # `host`, which is the mirror this record was read from); we accept the
        # `host_class` spelling too so whichever key D-schema settles on is shown.
        # Round A rename: canonical key is ``projectId`` (``productId`` still
        # accepted as a read alias — schema dual-writes both, so old configs
        # resolve unchanged). Emit under BOTH ``project`` (canonical) and
        # ``product`` (legacy) so the client and any older reader keep working.
        # An EXPLICIT config binding wins; else the CONFIDENT goal-target signal
        # (REDESIGN-SPEC §3, via schema.resolve_project) so the switcher has real
        # projects to discriminate; else honest None → "Unattributed". NEVER the
        # git remote / slug (the §3 violation the round-1 inputs flagged).
        # `productName` is the header label the grouped view shows; the id stays
        # the grouping/binding key.
        **_project_binding(cfg, name),
        # REDESIGN-SPEC §Q1 — a loop-of-1 (exactly one top-level agent step) is the
        # smallest primitive; the single-agent chip + solo tag key off this. Computed
        # the SAME way as mcp_loops.schema.is_single_agent (imported, not re-derived).
        "single_agent": _schema.is_single_agent(cfg),
        # gap #3: a Hub-saved loop dispatched to a REMOTE origin runs THERE —
        # attribute it to that origin (``dispatched.json``) so the
        # Machines view counts it on the right computer, not as a local save.
        "origin": (_dispatched_origin(d) or cfg.get("origin")
                   or cfg.get("host_class") or None),
        "team": _team(cfg),
        "turnLimit": turn_limit,
        # Turn contract (loopyard-bug-1790089836, see _turns): ``turns_used`` is
        # the MAIN-phase count against ``turnLimit``, capped at it, so
        # ``turns_used/turnLimit`` never reads over 100%; wind-down turns are
        # separate (``winddown_turns``) and ``turns`` carries the raw
        # {main, winddown, limit, budgeted, over}. Live runs read progress.json.
        "turns": turns,
        "ended": result.get("ended"), "turns_used": turns["budgeted"],
        "winddown_turns": turns["winddown"], "retired": result.get("retired"),
        # Compact finished-run result (loopyard-bug-1790089836-2): the Resolution
        # card's verdict/green/resolution, no event arrays. None while live.
        "result": _result_summary(root, host, name, cfg, run),
        "last_report": recent[-1] if recent else None,
        # Give-task→find-result at the UI layer: a short, honest summary a finished
        # loop leaves, so the Runs feed + loop card can show WHAT happened instead
        # of a blank. Prefer the engine's finish-report headline (which now names
        # what was produced + where), else the last agent note, else None.
        "summary": _loop_summary_line(run, recent),
        "status": _external_status(run),
    }


def list_loops() -> list[dict]:
    loops: list[dict] = []
    for host, root in _sources():
        try:
            entries = sorted(os.listdir(root)) if root.is_dir() else []
        except OSError:
            entries = []
        for name in entries:
            if name.startswith("_") or not _NAME_RE.match(name):
                continue                      # skip _registry and other private dirs
            if not (root / name / "config.json").is_file():
                continue
            try:
                loops.append(_summary(root, host, name))
            except Exception:  # noqa: BLE001
                loops.append({"name": name, "host": host, "state": "error"})
    loops.sort(key=lambda x: x.get("updated") or x.get("started") or 0, reverse=True)
    return loops


def _manager_ids(cfg: dict) -> set:
    """Step ids whose role is ``manager`` — their last note is a wind-down
    VERDICT, not the deliverable, so the result view excludes it (§4.1)."""
    steps = (cfg or {}).get("steps") or {}
    return {sid for sid, s in steps.items()
            if isinstance(s, dict) and s.get("role") == "manager"}


def _answer_note(recent: list[dict], manager_ids: set) -> str | None:
    """The last SUBSTANTIVE non-manager agent note — the ANSWER the result view
    leads with (§4.1). Mirrors ``envelope._answer_note``: managers excluded
    (their note is a status-line verdict), marker-only notes skipped in favor of
    real prose, falling back to the last non-manager note. ``None`` = no answer
    captured yet (the view then stays honest rather than faking a receipt)."""
    fallback = None
    for e in reversed(recent or []):
        note = e.get("note")
        if not isinstance(note, str) or not note.strip():
            continue
        if e.get("kind") == "machinery" or e.get("agent") in manager_ids:
            continue
        if fallback is None:
            fallback = note.strip()
        prose = _ARTIFACT_RE.sub("", _TESTS_RE.sub("", _COMMIT_RE.sub("", note)))
        if prose.strip():
            return note.strip()
    return fallback


def _git_pointer(run: dict, recent: list[dict]) -> dict | None:
    """A plain git-commit pointer for the result view (§4.1): "committed to
    ``<branch>`` @ ``<sha>``" when a run field or an agent note names a commit —
    instead of pretending the deliverable sits in the output tree. Mirrors the
    envelope's precedence (the engine-stamped run field wins over a note marker;
    else the last ``commit=`` marker); ``branch=`` is best-effort from a note."""
    rc = (run or {}).get("git_commit")
    marker_commit = None
    branch = None
    for e in recent or []:
        note = e.get("note")
        if not isinstance(note, str):
            continue
        m = _COMMIT_RE.search(note)
        if m:
            marker_commit = m.group(1)
        b = _BRANCH_RE.search(note)
        if b:
            branch = b.group(1)
    commit = rc if isinstance(rc, str) and rc.strip() else marker_commit
    if isinstance(commit, str) and commit.strip():
        return {"commit": commit.strip()[:12], "branch": branch}
    return None


_RESULT_PREVIEW_BYTES = 1600


def _result_files(name: str, recent: list[dict]) -> dict:
    """The result view's file surface (§4.1). Reuses the SAME sandboxed reader as
    the Files modal (``loop_files``/``_resolve_in_artifacts``) and returns:

      * ``curated`` — the ONE deliverable to preview at the top: a declared
        ``artifact=`` (override, if it resolves) wins; else ``INDEX.md``; else the
        first non-transcript, non-receipt ``.md``. Includes a short text preview.
      * ``files`` — the honest output-file list with transcripts + the
        finish-report receipt demoted OUT (they live one disclosure deeper).
      * ``count`` — total files on disk (for the "…N more" affordance).

    Read-only; never raises (a walk/read error degrades to an empty surface)."""
    try:
        listing = loop_files(name)
    except Exception:  # noqa: BLE001 — the result view must never break the detail
        return {"curated": None, "files": [], "count": 0}
    flat: list[dict] = []
    for g in listing.get("artifacts", []):
        for f in g.get("files", []):
            flat.append(f)

    def _is_transcript(p: str) -> bool:
        return bool(_TRANSCRIPT_RE.search(p))

    def _is_receipt(p: str) -> bool:
        return os.path.basename(p).lower() == "finish-report.md"

    # curated pick: declared artifact= override first (honor a real one only)
    curated_path = None
    override = False
    for e in reversed(recent or []):
        note = e.get("note")
        if isinstance(note, str):
            m = _ARTIFACT_RE.search(note)
            if m and _resolve_in_artifacts(name, m.group(1)):
                curated_path, override = m.group(1), True
                break
    if not curated_path:
        mds = [f["path"] for f in flat
               if f["path"].lower().endswith(".md") and not _is_receipt(f["path"])]
        idx = [p for p in mds if os.path.basename(p).lower() == "index.md"]
        picks = idx or mds
        curated_path = picks[0] if picks else None

    curated = None
    if curated_path:
        fp = _resolve_in_artifacts(name, curated_path)
        if fp is not None:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
                curated = {"path": curated_path,
                           "preview": text[:_RESULT_PREVIEW_BYTES],
                           "truncated": len(text) > _RESULT_PREVIEW_BYTES,
                           "override": override}
            except OSError:
                curated = None

    files = [f for f in flat
             if not _is_transcript(f["path"]) and not _is_receipt(f["path"])]
    return {"curated": curated, "files": files, "count": listing.get("count", 0)}


def loop_detail(name: str, host: str = "local", tail: int = 120) -> dict:
    if not _NAME_RE.match(name or ""):
        return {"error": "bad name"}

    def _build(h: str, root: Path) -> dict:
        summ = _summary(root, h, name)
        summ["recent"] = _annotated_status(root, name, tail)
        # the full finish-report text (the engine's result doc) so the detail view
        # can render a real result panel after a run, not just a log tail.
        run = _read_json(root / name / "run.json") or {}
        summ["finish_report"] = ((run.get("finish_report") or {}).get("text"))
        # §4.1 — the readable result LEADS WITH THE ANSWER, not the receipt. These
        # additive fields feed the inverted result block: the last substantive
        # non-manager agent note, the curated deliverable + honest output-file
        # list (transcripts/receipt demoted), and a plain git-commit pointer.
        # `finish_report`/`summary` stay for the demoted one-line status. Purely
        # additive: no existing field changes, so the old render path is intact.
        cfg = _read_json(root / name / "config.json") or {}
        mgr = _manager_ids(cfg)
        summ["answer_note"] = _answer_note(summ["recent"], mgr)
        summ["git_pointer"] = _git_pointer(run, summ["recent"])
        summ["result_files"] = _result_files(name, summ["recent"])
        live = _read_json(root / name / "live.json") or {}
        # a finished run's leftover live.json is not "Loop running · N in flight"
        # — the single STATE is the only status (Better-UX #1/#8)
        summ["live"] = [] if _teamroom.run_finished(run) else live.get("running", [])
        summ["live_updated"] = live.get("updated")
        summ["input_pending"] = _pending_input(root, name)
        a2s, snames = _sub_agent_map(_read_json(root / name / "config.json") or {})
        summ["sub_agents"] = a2s          # agent id -> its top-level sub-loop step id
        summ["sub_names"] = snames        # sub-loop step id -> display name
        return summ

    for h, root in _sources():
        if h == host and (root / name / "config.json").is_file():
            return _build(h, root)
    # fall back: search any host (host label may be stale)
    for h, root in _sources():
        if (root / name / "config.json").is_file():
            return _build(h, root)
    return {"error": "not found"}


# ── RESULTS / FILES path: browse + view the deliverables a loop produced ──
# A loop declares its output location(s) in a sidecar the coordinator writes:
#   data/_loops/<name>/artifacts.json  ->  {"dirs": ["/abs/path", ...], "label": "..."}
# Everything here is read-only and STRICTLY sandboxed to those declared dirs.
_TEXT_EXT = {".md", ".txt", ".json", ".csv", ".tsv", ".log", ".py", ".js", ".css",
             ".yaml", ".yml", ".toml", ".ini", ".sh", ".sql"}
_HTMLish = {".html", ".htm"}
_IMG_CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".cache"}
_MAX_FILES = 400
_MAX_BYTES = 12 * 1024 * 1024


def _kind(ext: str) -> str:
    if ext in _HTMLish:
        return "html"
    if ext == ".svg":
        return "svg"
    if ext in _IMG_CT:
        return "image"
    if ext in _TEXT_EXT:
        return "text"
    return "other"


def _artifacts_dirs(name: str) -> tuple[list[Path], str]:
    """Resolved artifact dirs for a loop + label.

    Primary source is the ``artifacts.json`` sidecar a coordinator writes for a
    PROJECT loop. FRONT-DOOR loops (created via the +Loop creator or a bare
    ``loop_save``) have NO sidecar — yet the engine still writes their real
    deliverable to ``_output/<name>/`` (finish-report + per-turn transcripts,
    exactly what ``get_loop_result`` surfaces). Without a fallback the dashboard
    Files browser told a UI-first user "No output files declared" while their
    result sat on disk — the give-task→find-result gap at the UI layer. So when
    there are no declared dirs we fall back to that output tree."""
    if not _NAME_RE.match(name or ""):
        return [], ""
    local = _sources()[0][1]                      # the local data/_loops dir
    meta = _read_json(local / name / "artifacts.json") or {}
    dirs: list[Path] = []
    for d in meta.get("dirs", []) or []:
        try:
            rp = Path(d).resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in dirs:
            dirs.append(rp)
    label = meta.get("label") or ""
    if not dirs:
        # Fallback: the engine's own output tree (_loops/_output/<name>/), which
        # sits beside the data dir — the same place envelope.py / get_loop_result
        # read. Surfaces the finish-report + transcripts for front-door loops.
        out_dir = local / "_output" / name
        try:
            if out_dir.is_dir():
                dirs.append(out_dir.resolve())
                label = label or "loop output"
        except OSError:
            pass
    return dirs, label


def loop_files(name: str) -> dict:
    """List the files under a loop's declared artifact dirs (sandboxed, capped)."""
    dirs, label = _artifacts_dirs(name)
    groups: list[dict] = []
    total = 0
    for d in dirs:
        files: list[dict] = []
        for root, subdirs, names in os.walk(d):
            subdirs[:] = [s for s in subdirs if s not in _SKIP_DIRS]
            for fn in sorted(names):
                if total >= _MAX_FILES:
                    break
                fp = Path(root) / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                ext = fp.suffix.lower()
                rel = str(fp.relative_to(d))
                files.append({"path": rel, "size": st.st_size, "kind": _kind(ext)})
                total += 1
        files.sort(key=lambda f: f["path"])
        groups.append({"dir": str(d), "label": label, "files": files})
    return {"name": name, "artifacts": groups, "count": total,
            "capped": total >= _MAX_FILES}


def _resolve_in_artifacts(name: str, rel: str) -> Path | None:
    """Resolve rel path against the loop's artifact dirs, refusing any escape."""
    dirs, _ = _artifacts_dirs(name)
    for d in dirs:
        try:
            cand = (d / rel).resolve()
        except OSError:
            continue
        if (cand == d or str(cand).startswith(str(d) + os.sep)) and cand.is_file():
            return cand
    return None


# Loop artifacts are UNTRUSTED (agent-written, may carry pulled-in web content):
# every artifact response is sandboxed so HTML/SVG still render in a new tab but
# run no script, submit no form and get an opaque origin — never the dashboard's
# (loopyard-bug-1790562066). nosniff stops a text/plain body being sniffed to HTML.
_ARTIFACT_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; "
                               "style-src 'unsafe-inline'; media-src 'self' data:",
    "X-Content-Type-Options": "nosniff",
}


def _serve_file(name: str, rel: str, download: bool = False) -> Response:
    fp = _resolve_in_artifacts(name, rel)
    if fp is None:
        return Response("not found", status_code=404, media_type="text/plain")
    try:
        if fp.stat().st_size > _MAX_BYTES:
            return Response("file too large to preview", status_code=413,
                            media_type="text/plain")
        data = fp.read_bytes()
    except OSError as e:
        return Response(f"read error (id {_error_ref('serve_file', e)})",
                        status_code=500, media_type="text/plain")
    ext = fp.suffix.lower()
    if download:
        safe = re.sub(r'[^\w.\- ]', "_", fp.name) or "file"
        return Response(data, media_type="application/octet-stream",
                        headers={**_ARTIFACT_HEADERS, "Content-Disposition":
                                 f'attachment; filename="{safe}"'})
    if ext in _HTMLish:
        ct = "text/html; charset=utf-8"          # renders (sandboxed) in a new tab
    elif ext == ".svg":
        ct = "image/svg+xml"
    elif ext in _IMG_CT:
        ct = _IMG_CT[ext]
    elif ext in _TEXT_EXT or ext == "":
        ct = "text/plain; charset=utf-8"          # readable in-browser
    else:
        ct = "application/octet-stream"
    return Response(data, media_type=ct, headers=dict(_ARTIFACT_HEADERS))


_MAX_ZIP_BYTES = 60 * 1024 * 1024


def _zip_artifacts(name: str):
    """Zip a loop's artifact dirs (sandboxed, capped) into an anonymous temp
    file, rewound, for streaming. None if it has none. Blocking (walk + DEFLATE):
    call it off the event loop (loopyard-bug-1790562060)."""
    dirs, _ = _artifacts_dirs(name)
    if not dirs:
        return None
    import tempfile
    import zipfile
    buf = tempfile.TemporaryFile()
    try:
        total = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in dirs:
                base = d.name or "files"
                for root, subdirs, names in os.walk(d):
                    subdirs[:] = [s for s in subdirs if s not in _SKIP_DIRS]
                    for fn in sorted(names):
                        fp = Path(root) / fn
                        try:
                            sz = fp.stat().st_size
                        except OSError:
                            continue
                        if sz > _MAX_BYTES or total + sz > _MAX_ZIP_BYTES:
                            continue
                        total += sz
                        z.write(fp, os.path.join(base, str(fp.relative_to(d))))
    except BaseException:
        buf.close()
        raise
    buf.seek(0)
    return buf


_ZIP_CHUNK = 256 * 1024


def _iter_and_close(fh):
    """Sync chunk iterator (Starlette pulls it in a threadpool); closes fh."""
    try:
        while chunk := fh.read(_ZIP_CHUNK):
            yield chunk
    finally:
        fh.close()


# ── MCP client: the ACTION path (turns the panel into a client of mcp-loops) ──
_MCP_TIMEOUT_S = 45.0


class _SharedMCP:
    """ONE long-lived MCP client session per (event loop, URL), shared by every
    request (loopyard-bug-1790089834: a fresh session per call cost 0.3s idle and
    2-22s under load).

    The transport + ClientSession live in a dedicated owner task (anyio contexts
    must be entered/exited in the same task); request tasks only call
    ``session.call_tool``, which ClientSession multiplexes by request id, so
    concurrent calls share the session without serialising. ``_lock`` guards
    only (re)connect, so N concurrent first calls still initialize once.

    Reconnect: any failure on a call invalidates the session; if that session
    was a REUSED one (stale after an mcp-loops restart, dropped connection) the
    call is retried once on a fresh session. Timeouts are not retried — the
    server may still be executing the tool.

    Dead transport: when mcp-loops goes away mid-session the POST's ConnectError
    is raised inside the transport's task group, which tears the owner task down
    but never answers the pending ``call_tool`` — it would sit out the full read
    timeout. So the owner sets a per-session ``dead`` event on exit and every
    call races its request against it."""

    def __init__(self) -> None:
        self._loop = None
        self._url: str | None = None
        self._lock = None
        self._session = None
        self._stop = None
        self._dead = None
        self._tasks: set = set()
        self.connects = 0  # sessions opened (tests + timing artifact read this)

    def _bind(self, url: str) -> None:
        import asyncio
        loop = asyncio.get_running_loop()
        if loop is self._loop and url == self._url:
            return
        # new event loop (tests) or a re-pointed URL: the old session belongs to
        # a different loop/server — drop it (close if its loop is still ours).
        if self._loop is loop and self._stop is not None:
            self._stop.set()
        self._loop, self._url = loop, url
        self._lock = asyncio.Lock()
        self._session = self._stop = self._dead = None

    async def _connect(self):
        import asyncio
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        stop = asyncio.Event()
        dead = asyncio.Event()
        url = self._url

        async def _owner():
            session = None
            try:
                http = create_mcp_http_client(
                    timeout=httpx.Timeout(_MCP_TIMEOUT_S, read=300.0))
                async with http:
                    async with streamable_http_client(url, http_client=http) as (r, w, _):
                        async with ClientSession(
                                r, w, read_timeout_seconds=timedelta(seconds=_MCP_TIMEOUT_S)) as s:
                            await s.initialize()
                            session = s
                            ready.set_result(s)
                            await stop.wait()
            except BaseException as e:  # noqa: BLE001 — surfaced via `ready`
                if not ready.done():
                    ready.set_exception(e if isinstance(e, Exception)
                                        else RuntimeError(f"mcp session task ended: {e!r}"))
                if not isinstance(e, Exception):
                    raise
            finally:
                dead.set()  # releases every call in flight on this session
                if session is not None and self._session is session:
                    self._session = self._stop = self._dead = None

        task = loop.create_task(_owner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.connects += 1
        try:
            session = await asyncio.wait_for(asyncio.shield(ready), _MCP_TIMEOUT_S)
        except BaseException:
            stop.set()
            task.cancel()
            raise
        if dead.is_set():  # owner exited between initialize and here
            raise ConnectionError("mcp session closed during connect")
        self._session, self._stop, self._dead = session, stop, dead
        return session

    async def _get(self, url: str):
        self._bind(url)
        async with self._lock:
            if self._session is None:
                session = await self._connect()
                return session, self._dead, True
            return self._session, self._dead, False

    def _invalidate(self, session) -> None:
        if self._session is session:
            stop, self._session, self._stop, self._dead = self._stop, None, None, None
            if stop is not None:
                stop.set()

    @staticmethod
    async def _call_or_dead(session, dead, tool: str, args: dict):
        import asyncio
        call = asyncio.ensure_future(session.call_tool(tool, args))
        died = asyncio.ensure_future(dead.wait())
        try:
            await asyncio.wait({call, died}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            died.cancel()
            if not call.done():
                call.cancel()
        if call.done() and not call.cancelled():
            return call.result()
        raise ConnectionError("mcp-loops session closed (transport failed)")

    async def call(self, url: str, tool: str, args: dict):
        from mcp.shared.exceptions import McpError
        import asyncio

        for attempt in (0, 1):
            session, dead, fresh = await self._get(url)
            try:
                return await self._call_or_dead(session, dead, tool, args)
            except Exception as e:  # noqa: BLE001
                self._invalidate(session)
                timed_out = isinstance(e, (TimeoutError, asyncio.TimeoutError)) or (
                    isinstance(e, McpError) and getattr(e.error, "code", None) == 408)  # httpx.codes.REQUEST_TIMEOUT
                if fresh or attempt or timed_out:
                    raise

    async def aclose(self) -> None:
        import asyncio
        if self._stop is not None:
            self._stop.set()
        self._session = self._stop = self._dead = None
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)


_SHARED_MCP = _SharedMCP()


async def aclose_shared_mcp() -> None:
    """Close the shared mcp-loops session (app shutdown hook)."""
    await _SHARED_MCP.aclose()


async def _mcp_call(tool: str, args: dict) -> dict:
    """Call one mcp-loops tool over the shared streamable-HTTP session and return
    its dict result. Never raises: transport failure / server error becomes
    {"error": ...} so the UI can surface it verbatim (U11)."""
    try:
        import mcp  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return {"error": f"mcp client unavailable (id {_error_ref('mcp import', e)})"}
    try:
        res = await _SHARED_MCP.call(_MCP_URL, tool, args)
        data = getattr(res, "structuredContent", None)
        if isinstance(data, dict):
            # FastMCP wraps a non-dict return under {"result": ...}; a
            # dict return may pass through directly OR be wrapped too.
            if set(data.keys()) == {"result"} and isinstance(data["result"], dict):
                return data["result"]
            return data
        for c in (getattr(res, "content", None) or []):
            txt = getattr(c, "text", None)
            if txt:
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    return {"raw": txt}
        return {"error": "empty response from mcp-loops"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"mcp-loops unreachable ({_MCP_URL}): {type(e).__name__} "
                         f"(id {_error_ref('mcp call ' + tool, e)})"}


_UPSTREAM_ERR = ("mcp-loops unreachable", "mcp client unavailable", "empty response from mcp-loops")
_NOT_FOUND_RE = re.compile(r"^(no (issue|doc|thread)\b|unknown\b)|\bnot found\b", re.IGNORECASE)
_CONFLICT_RE = re.compile(r"^illegal transition\b|\bmay not\b|\bvanished\b", re.IGNORECASE)


def _error_status(res) -> int:
    """The HTTP status for a proxied tool result (loopyard-bug-1790089840): 200 on
    success; on an ``{error}`` body, 502 when mcp-loops itself couldn't be reached,
    409 for a state conflict (an illegal triage move carries ``legal``; a read-only
    doc), 404 for an unknown id, else 400."""
    if not isinstance(res, dict) or not res.get("error"):
        return 200
    msg = str(res.get("error"))
    if msg.startswith(_UPSTREAM_ERR):
        return 502
    if "legal" in res or _CONFLICT_RE.search(msg):
        return 409
    if _NOT_FOUND_RE.search(msg):
        return 404
    return 400


def _mcp_json(res) -> JSONResponse:
    """A proxied write's JSON response: the tool's body unchanged, with a 4xx/502
    status when it is an ``{error}`` so generic clients see the failure."""
    return JSONResponse(res, status_code=_error_status(res))


async def _json_body(request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ── handlers (auto-gated by gate.py's auth wrapper) ──
async def loops_list_api(request):
    return JSONResponse({"loops": await run_in_threadpool(list_loops),
                         "hosts": [h for h, _ in _sources()]})


async def loop_detail_api(request):
    name = request.path_params.get("name", "")
    host = request.query_params.get("host", "local")
    try:
        tail = int(request.query_params.get("tail", "120"))
    except ValueError:
        tail = 120
    return JSONResponse(await run_in_threadpool(
        loop_detail, name, host, max(1, min(tail, 500))))


# Actions that operate on the loop's LIVE run and therefore route to the origin
# that runs it: local uses the in-process run tools, a remote origin rides the
# cross-origin dispatch channel (P2 §b — real dispatch, not a read-only mirror).
_REMOTE_ROUTED = {"start": "loop_dispatch_start", "stop": "loop_dispatch_stop"}


def _norm_dispatch(res: dict) -> dict:
    """Surface a dispatch refusal (``ok:False`` with a ``reason`` but no
    ``error`` key) as a plain ``error`` so the panel/SPA's existing error path
    shows WHY a cross-origin control didn't take — never a confusing "ok →
    unreachable"."""
    if isinstance(res, dict) and res.get("ok") is False and not res.get("error"):
        return {**res, "error": res.get("reason") or "dispatch refused"}
    return res


async def loop_brief_api(request):
    """Spawn a FRESH MANAGER-BRIEFING session preloaded with this loop's full
    context (force_new — retires any live prior briefing). Returns
    {ok, sid, attach, slug, window} — the owner attaches, reshapes the goal/crew,
    then Run adopts this exact session as the manager. Local only."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_brief_session",
                                        {"name": name, "force_new": True}))


async def loop_debrief_api(request):
    """DEBRIEF — resume the SAME manager (carry-over): attach a live briefing
    session, or resurrect the manager a previous run adopted so it continues with
    its whole prior context. Returns {ok, sid, attach, slug, window, resumed}. Local only."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_debrief_session", {"name": name}))


async def loop_action_api(request):
    """Command a loop via the mcp-loops server. Body: {action, host?, ...}.

    start/stop route to the loop's OWNING ORIGIN so the ONE dashboard controls a
    loop regardless of which origin runs it (P2 §b): ``host`` local (default)
    uses loop_start / loop_stop; a REMOTE origin rides the cross-origin dispatch
    channel (loop_dispatch_start / loop_dispatch_stop), which writes a request
    envelope for the origin's executor and honestly refuses an unreachable
    origin — never a silent success and never hitting the wrong box. The other
    write-actions (input / reply / archive / analyze / save_*) act on a LOCAL run
    only; on a remote origin they return a clear 'not available cross-origin'
    error instead of silently commanding a same-named local loop."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    body = await _json_body(request)
    action = (body.get("action") or "").strip()
    host = (body.get("host") or "local").strip() or "local"
    if host != "local" and not _NAME_RE.match(host):
        return JSONResponse({"error": "bad host"}, status_code=400)

    if host != "local":
        tool = _REMOTE_ROUTED.get(action)
        if tool is not None:
            return JSONResponse(_norm_dispatch(
                await _mcp_call(tool, {"name": name, "origin": host})))
        # Only run-lifecycle verbs route cross-origin today. Refuse the rest
        # clearly rather than acting on a local loop of the same name.
        return JSONResponse(
            {"error": f"action {action!r} is not available cross-origin yet — "
                      f"only start/stop route to a remote origin (host {host!r})"},
            status_code=409)

    tool_args = {
        "stop": ("loop_stop", {"name": name}),
        "start": ("loop_start", {"name": name,
                                 **({"slug": body["slug"]} if body.get("slug") else {})}),
        "input": ("loop_input", {"name": name, "text": body.get("text", "")}),
        "reply": ("loop_reply", {"name": name, "reply": body.get("reply", "")}),
        # Better-UX #6 — STEER mirrors Brief: gentle stop → manager hand-off →
        # restart from the same point (poll steer_status until phase=ready).
        "steer": ("loop_steer", {"name": name}),
        "steer_status": ("loop_steer_status", {"name": name}),
        "steer_resume": ("loop_steer_resume", {"name": name}),
        "archive": ("loop_archive", {"name": name}),
        "unarchive": ("loop_unarchive", {"name": name}),
        "analyze": ("loop_analyze", {"name": name}),
        "save_registry": ("loop_registry_save_loop",
                          {"name": name, "from_loop": name,
                           "note": body.get("note", "")}),
        "save_agent": ("loop_save_agent",
                       {"agent_id": body.get("agent", ""), "from_loop": name,
                        "note": body.get("note", "")}),
    }.get(action)
    if tool_args is None:
        return JSONResponse({"error": f"unknown action {action!r}"}, status_code=400)
    return JSONResponse(await _mcp_call(*tool_args))


async def loop_disposition_api(request):
    """Record an invisible disposition (SLICE-1 §4.2–4.4) from the result view:
    the inline ship/keep taps (``source="behavior"``) and the three quiet
    good/ok/bad buttons (``source="explicit"``). Body: ``{verb, source?}``.
    Local-only — a disposition is about THIS box's run store — so it mirrors
    loop_action_api's name-guard + _mcp_call without the cross-origin routing."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    body = await _json_body(request)
    verb = (body.get("verb") or "").strip()
    source = (body.get("source") or "behavior").strip() or "behavior"
    # §5.4 Resolution card — the good/ok/bad rating carries a "why" note (the honest
    # reason it landed the way it did); the backend stores it on the row so the card
    # can pre-fill "you rated this good · <why> · change".
    note = (body.get("note") or "").strip()
    args = {"name": name, "verb": verb, "source": source, "origin": "dashboard"}
    if note:
        args["note"] = note
    return JSONResponse(await _mcp_call("loop_disposition_record", args))


async def loop_resolution_api(request):
    """REDESIGN-SPEC §5.4 — the closing **Resolution card** for a loop. Proxies
    dev-1's ``loop_resolution`` (mcp_loops/resolution.py): a re-composition of the
    result envelope + the loop's disposition rows into a receipt led by a COMPUTED,
    honest verdict (``verdict.green`` True ONLY on a real positive resolution
    signal — a guardian-stop / error can never render green). The loop-detail
    already carries this card inside ``loop_team_room``; this standalone route lets
    the frontend refresh just the card and drives the acceptance tests. Read-only;
    origin-aware (a remote loop resolves off its synced mirror; disposition stays
    local-only, honestly)."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    host = request.query_params.get("host", "local")
    args = {"name": name}
    if host and host != "local":
        if not _NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await _mcp_call("loop_resolution", args))


async def loop_project_dispositions_api(request):
    """REDESIGN-SPEC §5.4 — disposition "shown back on loop cards + aggregated
    per-project." Proxies dev-1's ``loop_project_dispositions``: the honest tally
    (``good/ok_count/bad/rated/total/goodRate``, ``goodRate`` null until something
    is rated — never a fake 0%) plus each rated loop's current verb, so the Loops
    list can badge a card with how its owner said it landed and show the rollup.
    ``project`` empty ⇒ the whole origin. Read-only."""
    project = request.path_params.get("project", "")
    # the project id is a slug (or "" for the whole fleet); guard the same way the
    # other name-shaped params are guarded, but allow the empty all-projects case.
    if project and not _NAME_RE.match(project):
        return JSONResponse({"error": "bad project"}, status_code=400)
    host = request.query_params.get("host", "local")
    args = {"project": project}
    if host and host != "local":
        if not _NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await _mcp_call("loop_project_dispositions", args))


async def loop_creator_brief_api(request):
    """REDESIGN-SPEC §5.5 — the NATIVE conversational brief (Door B), and the
    per-loop **Brief / Debrief** buttons. Proxies dev-1's daemon-free
    ``loop_creator_brief`` (``mcp_loops.creator.brief_turn``): from a described
    ``goal`` (Door B) OR an existing loop's ``name`` (Brief/Debrief seed its saved
    goal/project), plus the ``answers`` gathered so far, it returns the next 1-2
    still-open MATERIAL questions AND a live, editable **team preview = the config**.
    ``phase='debrief'`` reframes it as the end-of-run capture. No worker daemon /
    tmux — so it works on the isolated redesign stack and never drops the user into
    a shell (this is what kills the old ``/brief``,``/debrief`` 404 + JSON toast)."""
    body = await _json_body(request)
    args: dict = {}
    for key in ("goal", "name", "projectId", "target", "phase"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            args[key] = val.strip()
    # a name-seeded brief/debrief points at a saved loop — guard it like every
    # other name-shaped param so a bad slug can't reach the tool.
    if args.get("name") and not _NAME_RE.match(args["name"]):
        return JSONResponse({"error": "bad name"}, status_code=400)
    if body.get("just_one"):
        args["just_one"] = True
    ans = body.get("answers")
    if isinstance(ans, dict):
        args["answers"] = ans
    return JSONResponse(await _mcp_call("loop_creator_brief", args))


async def loop_project_counts_api(request):
    """REDESIGN-SPEC §2 — the per-project **nav counts** the switcher rescopes the
    whole rail by. Proxies dev-1's ``loop_project_counts``: ``loops / ideahub /
    issues`` scoped to the active project, and ``origins / origins_reachable /
    sessions`` fleet-wide (compute is not project-owned, so the top bar never shows
    a contradictory ``0`` — §3 honesty). ``project`` empty ⇒ the whole fleet;
    ``__unattributed__`` ⇒ records with no project binding. The slug is guarded
    exactly like the dispositions route (empty all-projects allowed).
    Read-only; ``host`` selects a remote origin's mirror."""
    project = request.query_params.get("project", "")
    if project and project != _UNATTRIBUTED_SCOPE and not _NAME_RE.match(project):
        return JSONResponse({"error": "bad project"}, status_code=400)
    host = request.query_params.get("host", "local")
    args = {"project": project}
    if host and host != "local":
        if not _NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await _mcp_call("loop_project_counts", args))


async def loop_turn_api(request):
    name = request.path_params.get("name", "")
    agent = request.query_params.get("agent", "")
    host = request.query_params.get("host", "local")
    try:
        seq = int(request.query_params.get("seq", "0"))
    except ValueError:
        seq = 0
    args = {"name": name, "agent": agent, "seq": seq}
    # drill into a turn of a loop on ANY origin (loop_turn_detail is origin-aware);
    # default local keeps the existing call byte-identical.
    if host and host != "local":
        if not _NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await _mcp_call("loop_turn_detail", args))


async def loop_teamroom_api(request):
    """REDESIGN-SPEC §5.3 — the loop-detail as a TEAM ROOM. Proxies dev-1's
    ``loop_team_room`` (mcp_loops/teamroom.py): a pure re-composition of data the
    engine already writes (config roster + live.json + progress.json +
    status.jsonl) into the payload the redesigned detail renders — the roster
    (manager first, with owns/status-dot/doing-now/last-report), the COMPUTED
    never-blank convergence chip, the never-blank turn/wind-down bar, and the
    manager's one-line read. Read-only; origin-aware (reads a synced mirror for a
    remote loop). No engine state is mutated."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    host = request.query_params.get("host", "local")
    args = {"name": name}
    if host and host != "local":
        if not _NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await _mcp_call("loop_team_room", args))


async def loop_agent_reports_api(request):
    """REDESIGN-SPEC §5.3 — clicking an agent card opens its REPORT HISTORY (not a
    prompt dump). Proxies dev-1's ``loop_agent_reports``: one agent's end-of-turn
    reports newest-first, each carrying the ``seq`` the agent-turn card
    (``loop_turn_detail``) opens."""
    name = request.path_params.get("name", "")
    agent = request.query_params.get("agent", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent"}, status_code=400)
    host = request.query_params.get("host", "local")
    args = {"name": name, "agent": agent}
    if host and host != "local":
        if not _NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await _mcp_call("loop_agent_reports", args))


async def loops_registry_api(request):
    return JSONResponse(await _mcp_call("loop_registry_list", {}))


async def loop_analytics_api(request):
    """Cross-loop telemetry hub: per-agent + per-loop rollups across the fleet."""
    return JSONResponse(await _mcp_call("loop_fleet_analytics", {}))


async def loop_agent_api(request):
    """Every turn one agent took across all loops (?id=<agent>)."""
    agent = request.query_params.get("id", "")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_agent_detail", {"agent_id": agent}))


async def loop_agent_record_api(request):
    """The v2 registry record for one saved agent (?id=<agent>): head identity
    (persona + generic goal + model) and the full immutable version history.
    Returns {"error": ...} for agents that aren't saved in the registry."""
    agent = request.query_params.get("id", "")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_registry_get",
                                        {"kind": "agent", "id": agent}))


async def loop_agent_versions_api(request):
    """Compact version timeline for a saved agent (?id=<agent>): per-version
    source/note/model, what changed vs the prior version, and which is head."""
    agent = request.query_params.get("id", "")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_agent_versions", {"id": agent}))


async def loop_agent_diff_api(request):
    """Readable per-version diff for a saved agent (?id=<agent>&a=<n>&b=<m>):
    proxies ``loop_registry_agent_diff`` (per-field from/to/changed + unified
    text diff of persona/genericGoal) for the version timeline."""
    agent = request.query_params.get("id", "")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    try:
        a = int(request.query_params.get("a", ""))
        b = int(request.query_params.get("b", ""))
    except ValueError:
        return JSONResponse({"error": "a and b must be version numbers"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_registry_agent_diff",
                                        {"agent_id": agent, "version_a": a, "version_b": b}))


# ── generic-goal distiller (registry v2) ──────────────────────────────────────
# Reuses the existing authoring machinery rather than inventing a new one: the
# authoring RULES' leakage patterns (todos / product+path detail) decide which
# sentences are loop-specific, and the creator's loop-agnostic ROLE DEFAULTS are
# the fallback when nothing reusable is left. Pure + deterministic; the result
# is only a SUGGESTION — nothing is stored until the human accepts it.
# The accepted version's note — the timeline keys its "goal made reusable" label
# off it (mcp_loops records the save as source=edit; we don't change mcp_loops).
DISTILL_NOTE = "distilled: loop-agnostic goal"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def distill_generic_goal(goal: str, role: str | None = None) -> dict:
    """Suggest a loop-agnostic form of an agent's ``goal``. Returns
    ``{current, suggested, basis, findings, changed}`` where ``basis`` is
    ``clean`` (no real leakage — suggestion == current, no offer), ``trimmed``
    (the loop-specific sentences were dropped), ``role-default`` (the goal was
    empty / the auto placeholder, so the creator's loop-agnostic default for the
    role is offered) or ``all-specific`` (every sentence is loop-specific; no
    offer — replacing a real goal with boilerplate would lose the agent).

    Only the RULES' real-leakage checks (``warn``: todos, product/path detail)
    trigger an offer; the soft ``info`` "long, never references the loop goal"
    hint does not — those are often specialised but already reusable goals."""
    from mcp_loops import agents as _agents
    from mcp_loops import authoring as _authoring
    from mcp_loops import creator as _creator

    current = (goal or "").strip()
    role_key = role if role in _creator._ROLE_DEFAULTS else _schema.WORKER
    fallback = _creator._ROLE_DEFAULTS[role_key]["goal"]
    def _leaks(text: str) -> list[dict]:
        return [f for f in _authoring._lint_identity("agent", "", text)
                if f["where"].endswith(".goal") and f["level"] == "warn"]

    findings = _leaks(current)

    def _leaky(text: str) -> bool:
        return any(p.search(text) for p in
                   (*_authoring._TODO_PATTERNS, *_authoring._PRODUCT_DETAIL_PATTERNS))

    if not current or current == _agents.AUTO_GENERIC_GOAL_PLACEHOLDER:
        suggested, basis = fallback, "role-default"
    elif not findings:
        suggested, basis = current, "clean"
    else:
        kept = [s.strip() for s in _SENTENCE_SPLIT.split(current)
                if s.strip() and not _leaky(s)]
        cand = " ".join(kept).strip()
        if cand and not _leaks(cand):
            suggested, basis = cand, "trimmed"
        else:
            suggested, basis = current, "all-specific"
    return {"current": current, "suggested": suggested, "basis": basis,
            "findings": findings, "changed": suggested != current}


def _head_version(rec: dict) -> dict | None:
    vs = rec.get("versions") if isinstance(rec, dict) else None
    if not isinstance(vs, list) or not vs:
        return None
    return next((v for v in vs if v.get("version") == rec.get("head")), vs[-1])


async def loop_agent_distill_api(request):
    """Offer a distilled, loop-agnostic generic goal for a saved agent
    (?id=<agent>). Read-only: nothing is written until the accept route."""
    agent = request.query_params.get("id", "")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    rec = await _mcp_call("loop_registry_get", {"kind": "agent", "id": agent})
    head = _head_version(rec)
    if rec.get("error") or head is None:
        return JSONResponse({"error": rec.get("error") or "agent is not saved in the registry"})
    out = distill_generic_goal(head.get("genericGoal") or "", rec.get("defaultRole"))
    out.update({"id": agent, "version": head.get("version")})
    return JSONResponse(out)


async def loop_agent_distill_accept_api(request):
    """Store an accepted distilled goal (body: {id, goal}) as a NEW immutable
    version via ``loop_registry_save_agent`` — the persona and model are carried
    over from head unchanged, so only the generic goal moves. Non-destructive:
    every prior version stays in the timeline."""
    body = await _json_body(request)
    agent = body.get("id", "")
    goal = body.get("goal")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    if not isinstance(goal, str) or not goal.strip():
        return JSONResponse({"error": "goal is required"}, status_code=400)
    rec = await _mcp_call("loop_registry_get", {"kind": "agent", "id": agent})
    head = _head_version(rec)
    if rec.get("error") or head is None:
        return JSONResponse({"error": rec.get("error") or "agent is not saved in the registry"})
    args = {"agent_id": agent, "persona": head.get("persona") or "",
            "generic_goal": goal.strip(),
            "note": DISTILL_NOTE}
    if head.get("model"):
        args["model"] = head["model"]
    if rec.get("defaultRole"):
        args["role"] = rec["defaultRole"]
    return JSONResponse(await _mcp_call("loop_registry_save_agent", args))


async def loop_projects_api(request):
    """Browse PROJECTS — first-class targets loops & standalone runs point at.
    (Round A rename of the former Products catalog; the ``loop_product_list``
    MCP tool stays a live alias, so the legacy ``products/`` registry records
    resolve unchanged.)"""
    return JSONResponse(await _mcp_call("loop_project_list", {}))


async def loop_project_save_api(request):
    """Create/update a project (body: {project:{...}}, or legacy {product:{...}}).
    Portable-relative paths only — the backend rejects host-absolute paths."""
    body = await _json_body(request)
    proj = body.get("project")
    if not isinstance(proj, dict):
        proj = body.get("product")  # legacy key still accepted
    if not isinstance(proj, dict):
        return JSONResponse({"error": "missing project object"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_project_save", {"project": proj}))


async def loop_project_add_api(request):
    """TRIVIAL ADD — a project is NOTHING more than a git repo URL OR a directory
    path. Body: {source:"<git url or path>"}. id + display name are derived on the
    backend (loop_project_add); no hand-typed metadata. Returns loop_project_add's
    {ok, id, derived, warnings} | {error, errors}."""
    body = await _json_body(request)
    source = body.get("source", "")
    if not isinstance(source, str) or not source.strip():
        return JSONResponse({"error": "paste a git URL or a directory path"},
                            status_code=400)
    return JSONResponse(await _mcp_call("loop_project_add", {"source": source.strip()}))


async def loop_projects_assign_api(request):
    """BULK-ASSIGN a project to N loops (body: {project, loops:[names]}) — also the
    one-click "accept suggestion" for a single unbound loop. Proxies to
    ``loop_projects_assign`` (validated ``loop_save`` per loop). Returns
    {ok, project, assigned, errors}."""
    body = await _json_body(request)
    pid = body.get("project", "")
    names = body.get("loops")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    if (not isinstance(names, list) or not names
            or not all(isinstance(n, str) and _NAME_RE.match(n) for n in names)):
        return JSONResponse({"error": "loops must be a non-empty list of loop names"},
                            status_code=400)
    return JSONResponse(await _mcp_call("loop_projects_assign",
                                        {"project_id": pid, "names": names}))


async def loop_project_delete_api(request):
    """Delete a project (body: {id}). POST to match the app's action pattern."""
    body = await _json_body(request)
    pid = body.get("id", "")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_project_delete", {"id": pid}))


# ── the hub — objectives queue + issue log, zero-setup local store ───────────
# SLICE-1-SPEC §4.5 as amended by BRIEF-ADDENDUM: the hub WRITES from the first
# click, no Project. Unlike almost every other action here, these do NOT proxy to
# the mcp-loops server — they call the pure-local ``hub_store`` directly (append to
# a JSONL under the resolved data dir), so a first-run user's writes persist even
# with no runner/MCP server attached. This is the same file-based path the READ
# side uses; there is no capabilities mechanism and no checkout on it.
async def hub_view_api(request):
    """Everything the hub surface renders — objectives + issues + counts — in one
    local read. No Project required."""
    from mcp_loops import hub_store
    try:
        return JSONResponse(hub_store.view())
    except Exception as e:  # noqa: BLE001 — a read error degrades the panel, not the page
        return JSONResponse({"error": _internal_error("hub_view", e),
                             "objectives": [], "issues": []})


async def hub_objective_api(request):
    """Mutate the objectives queue. Body: {op:"add",text} | {op:"status",id,status}
    | {op:"remove",id}. Writes immediately to the local store."""
    from mcp_loops import hub_store
    body = await _json_body(request)
    op = body.get("op", "add")
    try:
        if op == "add":
            return JSONResponse({"ok": True, "objective": hub_store.add_objective(body.get("text", ""))})
        if op == "status":
            return JSONResponse({"ok": True, "objective": hub_store.set_objective_status(body.get("id", ""), body.get("status", ""))})
        if op == "remove":
            return JSONResponse({"ok": True, "objective": hub_store.remove_objective(body.get("id", ""))})
        return JSONResponse({"error": f"unknown op {op!r}"}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": _internal_error(f"hub op {op!r}", e)}, status_code=500)


async def hub_issue_api(request):
    """Append/resolve/remove a hub issue (the folded-in standalone Issues surface).
    Body: {op:"add",text} | {op:"status",id,status} | {op:"remove",id}."""
    from mcp_loops import hub_store
    body = await _json_body(request)
    op = body.get("op", "add")
    try:
        if op == "add":
            return JSONResponse({"ok": True, "issue": hub_store.add_issue(body.get("text", ""))})
        if op == "status":
            return JSONResponse({"ok": True, "issue": hub_store.set_issue_status(body.get("id", ""), body.get("status", ""))})
        if op == "remove":
            return JSONResponse({"ok": True, "issue": hub_store.remove_issue(body.get("id", ""))})
        return JSONResponse({"error": f"unknown op {op!r}"}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": _internal_error(f"hub op {op!r}", e)}, status_code=500)


# ── Hub-as-Workspace (HUB-WORKSPACE-SPEC, LOCKED) ─────────────────────────────
# The /api/loops/workspace/* surface the React SPA (frontend/packages/api
# workspace.ts) reads. Backed IN-PROCESS by mcp_loops.workspace.Workspace — an
# objective is a FOLDER of statused docs; the headline status ROLLS UP from the
# docs and a human can OVERRIDE it; the suggestion store carries the additive-only
# Accept/Decline/Discuss transitions. The two origin agents (Sweep / Reconcile)
# dispatch through mcp_loops.workspace_agents' origin.run bridge.
#
# SAFETY SPINE (additive-only): this HTTP surface NEVER accepts an ``actor``
# parameter. Every doc write is hardcoded ``actor=workspace.HUMAN`` — so no client
# can smuggle an agent into ``docs/``; the human's Accept remains the sole writer
# of accepted content, exactly as Workspace.write_doc's choke point enforces.
def _workspace():
    # ONE process-level store (loopyard-follow-up-1790338898): its per-base lock
    # serializes writes across requests, not just within one request's instance.
    from mcp_loops import workspace
    return workspace.shared()


async def ws_objectives_api(request):
    """GET — every objective with its rolled-up + effective (override-aware) status
    + doc_count. Fail-soft to an empty list with an ``error`` note (a read error
    degrades the panel, not the page)."""
    try:
        return JSONResponse({"objectives": _workspace().list_objectives()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"objectives": [], "error": _internal_error("ws_objectives", e)})


async def ws_objective_get_api(request):
    """GET one objective: its record (rollup + effective status) + its docs (each
    with status + provenance; bodies read via the doc endpoint)."""
    oid = (request.path_params.get("id") or "").strip("/")
    obj = _workspace().get_objective(oid)
    if obj is None:
        return JSONResponse({"error": f"unknown objective {oid!r}"}, status_code=404)
    return JSONResponse({"objective": obj})


async def ws_objective_create_api(request):
    """POST {title, project?} — create an empty objective FOLDER (optionally filed
    under a project); docs are added later via the human save path or an accepted
    suggestion."""
    body = await _json_body(request)
    ws = _workspace()
    project = body.get("project")
    try:
        rec = ws.create_objective(body.get("title", ""),
                                  project=project if isinstance(project, str) else None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"objective": ws.get_objective(rec["id"])})


async def ws_objective_override_api(request):
    """POST {id, status} — pin the objective's headline status (``status=null``
    clears the pin and the rollup is honest again)."""
    body = await _json_body(request)
    oid = (body.get("id") or "").strip()
    obj = _workspace().override_objective_status(oid, body.get("status"))
    if obj is None:
        return JSONResponse({"error": f"unknown objective {oid!r}"}, status_code=404)
    return JSONResponse({"objective": obj})


async def ws_objective_build_loop_api(request):
    """POST {id, project?} — RUN A PLAN AS A LOOP (the Plans view's ▶ Run as loop,
    after the human confirmed it). Builds a real loop config FROM the objective,
    saves it and records the plan↔loop link — nothing is started here; the client
    starts the saved loop through the normal action seam, exactly like the Hub's
    ``docs/point`` gesture. An adapted ideahub objective reuses that gesture
    verbatim (``loop_doc_build_loop``: the loop is recorded on the doc). A native
    one is seeded from its docs (:meth:`Workspace.loop_seed`), bound to
    ``project`` (or the objective's own), saved via ``loop_save`` and linked with
    :meth:`Workspace.point_loop`. Returns ``{ok, loop, objective}``."""
    from mcp_loops import hub_capabilities as _hc, workspace as _ws
    body = await _json_body(request)
    oid = (body.get("id") or "").strip()
    project = (body.get("project") or "").strip()
    if not oid or oid == _ws.INBOX_ID:
        return JSONResponse({"ok": False, "error": "bad plan id"}, status_code=400)
    if project and not _NAME_RE.match(project):
        return JSONResponse({"ok": False, "error": "bad project id"}, status_code=400)
    ws = _workspace()
    if _ws.is_ideahub_oid(oid):
        iid = oid[len(_ws.IH_PREFIX):]
        if not _doc_id_ok(iid):
            return JSONResponse({"ok": False, "error": "bad plan id"}, status_code=400)
        res = await _mcp_call("loop_doc_build_loop", {"id": iid})
        if not isinstance(res, dict) or not res.get("ok") or not res.get("loop"):
            return JSONResponse({"ok": False, "error": (res or {}).get("error")
                                 or "could not build a loop from this plan"}, status_code=502)
        return JSONResponse({"ok": True, "loop": res["loop"],
                             "objective": ws.get_objective(oid)})
    seed = ws.loop_seed(oid)
    if seed is None:
        return JSONResponse({"ok": False, "error": f"unknown plan {oid!r}"}, status_code=404)
    project = project or seed["project"]
    cfg = _hc.build_loop_from_objective(project, seed["title"], seed["detail"], cap_id="plans")
    if not project:
        cfg.pop("projectId", None)
        cfg["name"] = cfg["name"].replace("build--", "plan-", 1)
    saved = await _mcp_call("loop_save", {"config": cfg})
    if not isinstance(saved, dict) or saved.get("error") or not saved.get("name"):
        errs = (saved or {}).get("errors") or []
        msg = (saved or {}).get("error") or "could not save the loop"
        return JSONResponse({"ok": False, "error": msg + (": " + "; ".join(map(str, errs)) if errs else "")},
                            status_code=502)
    obj = ws.point_loop(oid, saved["name"], project=project or None)
    return JSONResponse({"ok": True, "loop": saved["name"], "objective": obj})


async def ws_doc_get_api(request):
    """GET a doc's meta + markdown body, for the pretty-render / Edit view."""
    oid = (request.path_params.get("oid") or "").strip("/")
    slug = (request.path_params.get("slug") or "").strip("/")
    doc = _workspace().get_doc(oid, slug)
    if doc is None:
        return JSONResponse({"error": "unknown doc"}, status_code=404)
    return JSONResponse({"doc": doc})


async def ws_doc_save_api(request):
    """POST {oid, title, body, slug?, status?} — THE human doc-write path (the sole
    writer of doc content). A save carrying a ``slug`` edits that doc in place (a
    manual edit of the human's OWN doc); a save without one creates a NEW doc.
    ``actor`` is hardcoded HUMAN — an agent can never reach here."""
    from mcp_loops import workspace as _ws
    body = await _json_body(request)
    oid = (body.get("oid") or "").strip()
    slug = body.get("slug") or None
    ws = _workspace()
    try:
        doc = ws.write_doc(oid, body.get("title", ""), body.get("body", ""),
                           actor=_ws.HUMAN, slug=slug,
                           status=body.get("status"),
                           overwrite=bool(slug))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"doc": ws.get_doc(oid, doc["slug"])})


async def ws_doc_status_api(request):
    """POST {oid, slug, status} — set a doc's status (touches meta only, never the
    body). The applied side of an accepted Reconcile status, or a human gesture."""
    body = await _json_body(request)
    oid = (body.get("oid") or "").strip()
    slug = (body.get("slug") or "").strip()
    doc = _workspace().set_doc_status(oid, slug, body.get("status") or "draft")
    if doc is None:
        return JSONResponse({"error": "unknown doc"}, status_code=404)
    return JSONResponse({"doc": doc})


async def ws_suggestions_api(request):
    """GET the objective's suggestions (``?state=pending|accepted|declined|discussing``
    filters). Each carries its kind, drafted body, and Reconcile's commit evidence."""
    oid = (request.path_params.get("oid") or "").strip("/")
    state = request.query_params.get("state") or None
    return JSONResponse({"suggestions": _workspace().list_suggestions(oid, state=state)})


def _resolved_suggestion(ws, oid: str, sid: str):
    """The folded suggestion after a transition (with its ``result``), for the card."""
    return next((s for s in ws.list_suggestions(oid) if s.get("id") == sid), None)


async def ws_suggestion_accept_api(request):
    """POST {oid, sid} — Accept: the human's click is the SOLE writer of accepted
    content. new_objective ⇒ a real objective; new_doc/enrichment ⇒ a NEW file
    (never overwrites); status ⇒ applies the proposed doc status."""
    body = await _json_body(request)
    oid = (body.get("oid") or "").strip()
    sid = (body.get("sid") or "").strip()
    from mcp_loops import workspace as _ws
    ws = _workspace()
    res = ws.accept_suggestion(oid, sid, actor=_ws.HUMAN)  # server-side HUMAN — no client actor
    if res is None:
        return JSONResponse({"error": "suggestion not pending"}, status_code=409)
    return JSONResponse({"suggestion": _resolved_suggestion(ws, oid, sid) or res})


async def ws_suggestion_decline_api(request):
    """POST {oid, sid} — Decline: discard the suggestion; nothing is written."""
    body = await _json_body(request)
    oid = (body.get("oid") or "").strip()
    sid = (body.get("sid") or "").strip()
    ws = _workspace()
    res = ws.decline_suggestion(oid, sid)
    if res is None:
        return JSONResponse({"error": "suggestion not pending"}, status_code=409)
    return JSONResponse({"suggestion": _resolved_suggestion(ws, oid, sid) or res})


async def ws_suggestion_discuss_api(request):
    """POST {oid, sid} — Discuss: move the suggested edits into the to-discuss
    FOLDER a human can later point a loop/session at. Still writes no doc."""
    body = await _json_body(request)
    oid = (body.get("oid") or "").strip()
    sid = (body.get("sid") or "").strip()
    ws = _workspace()
    res = ws.discuss_suggestion(oid, sid)
    if res is None:
        return JSONResponse({"error": "suggestion not pending"}, status_code=409)
    return JSONResponse({"suggestion": _resolved_suggestion(ws, oid, sid) or res})


async def ws_to_discuss_api(request):
    """GET the objective's to-discuss FOLDER — bundles of suggested edits parked for
    a human to later point a loop/session at."""
    oid = (request.path_params.get("oid") or "").strip("/")
    return JSONResponse({"items": _workspace().list_to_discuss(oid)})


async def ws_run_api(request):
    """POST {agent, origin, scope?, objective?} — launch an origin agent as an
    origin.run job on the Hub Fabric and FOLD its read-only collection into the
    suggestion store. Sweep mines candidate objectives from an origin's files;
    Reconcile scans an objective against the origin's git evidence. ADDITIVE-ONLY:
    both fold ONLY through Workspace.add_suggestion — never a human doc."""
    body = await _json_body(request)
    agent = (body.get("agent") or "").strip().lower()
    origin = (body.get("origin") or "").strip()
    if agent not in ("sweep", "reconcile"):
        return JSONResponse({"ok": False, "error": f"unknown agent {agent!r}"}, status_code=400)
    if not origin:
        return JSONResponse({"ok": False, "agent": agent, "error": "origin is required"},
                            status_code=400)
    from mcp_loops import server as _srv, workspace_agents as _wa
    ws = _workspace()

    def run_fn(argv, origin="", timeout=0.0):
        return _srv.origin_run(argv, origin=origin)

    try:
        if agent == "sweep":
            out = _wa.sweep_via_origin(ws, origin, run_fn, scope=body.get("scope") or None)
        else:
            oid = (body.get("objective") or "").strip()
            if not oid:
                return JSONResponse({"ok": False, "agent": agent,
                                     "error": "reconcile needs an objective"},
                                    status_code=400)
            out = _wa.reconcile_via_origin(ws, oid, origin, run_fn)
    except _wa.OriginRunError as e:
        return JSONResponse({"ok": False, "agent": agent, "error": str(e)},
                            status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "agent": agent,
                             "error": _internal_error(f"workspace agent {agent}", e)},
                            status_code=500)
    return JSONResponse({"ok": True, "agent": agent,
                         "job": out.get("objective") or origin, **out})


# ── /loopyard project-state manager (#5) + capability mechanism (#6) ──────────
# All reach mcp-loops by tool name via _mcp_call; project ids validated with the
# shared _NAME_RE before any tool call, exactly like the project handlers above.
async def loopyard_list_api(request):
    """List one level of a Project's /loopyard/ dir. ?subpath= to descend."""
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    subpath = request.query_params.get("subpath", "")
    return JSONResponse(await _mcp_call("loopyard_list",
                                        {"project_id": pid, "subpath": subpath}))


async def loopyard_file_get_api(request):
    """Read one file under a Project's /loopyard/ (?path=<relpath>)."""
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    return JSONResponse(await _mcp_call(
        "loopyard_read", {"project_id": pid, "path": request.query_params.get("path", "")}))


async def loopyard_file_write_api(request):
    """Create/edit a file under a Project's /loopyard/ (body: {path, content})."""
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    body = await _json_body(request)
    return JSONResponse(await _mcp_call("loopyard_write", {
        "project_id": pid, "path": body.get("path", ""),
        "content": body.get("content", "")}))


async def capabilities_list_api(request):
    """Discover the bundled capabilities the dashboard mounts as sections."""
    return JSONResponse(await _mcp_call("capabilities_list", {}))


async def capability_page_api(request):
    """Serve a capability's DECLARATIVE page as HTML — the shell mounts it in an
    iframe (?project=<id> reaches the page as a query param). The page's own JS
    calls the /api/loops/capability/... data + trigger endpoints below."""
    cap = request.path_params.get("cap", "")
    if not _NAME_RE.match(cap or ""):
        return HTMLResponse("<p>bad capability id</p>", status_code=400)
    res = await _mcp_call("capability_page", {"cap_id": cap})
    if res.get("error") or not res.get("html"):
        return HTMLResponse(f"<p>{res.get('error', 'no page')}</p>", status_code=404)
    return HTMLResponse(res["html"])


async def capability_data_api(request):
    """GET → list a capability's data; POST {path, content} → write one file.
    Both are scoped under the project's loopyard/<capability dataDir>/."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    if request.method == "POST":
        body = await _json_body(request)
        return JSONResponse(await _mcp_call("capability_data_write", {
            "cap_id": cap, "project_id": pid,
            "path": body.get("path", ""), "content": body.get("content", "")}))
    return JSONResponse(await _mcp_call(
        "capability_data_list", {"cap_id": cap, "project_id": pid}))


async def capability_trigger_api(request):
    """Trigger a capability's attached loop; returns {ok, loop, envelope}."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    return JSONResponse(await _mcp_call(
        "capability_trigger", {"cap_id": cap, "project_id": pid}))


async def capability_result_api(request):
    """The attached loop's result envelope for this (capability, project)."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    return JSONResponse(await _mcp_call(
        "capability_result", {"cap_id": cap, "project_id": pid}))


# ── B2 first-party hub capabilities: extra read/action endpoints ──────────────
# ADDITIVE (engineer_a, Round B2). Thin pass-throughs to the two new MCP tools;
# same _NAME_RE id-validation + _mcp_call delegation as the generic capability
# endpoints above. engineer_b's C2/C3 handlers append AFTER this block.
async def capability_items_api(request):
    """GET → a capability's unified item list (stored JSON + imported md +
    auto-filed issues), for C1 Objectives / C4 Known-Issues."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    return JSONResponse(await _mcp_call(
        "capability_items", {"cap_id": cap, "project_id": pid}))


async def capability_build_api(request):
    """POST {title, detail?, id?} → seed a loop from an objective bound to the
    Project (C1 → Build). Returns {ok, loop, editUrl, goal}."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    body = await _json_body(request)
    return JSONResponse(await _mcp_call("capability_build_loop", {
        "cap_id": cap, "project_id": pid,
        "title": body.get("title", ""), "detail": body.get("detail", ""),
        "item_id": body.get("id", "") or body.get("item_id", "")}))


# ── B2 doc-shaped hub capabilities: C2 Specs + C3 Liked-Results (engineer_b) ──
# ADDITIVE. Thin pass-throughs to the C2/C3 MCP tools; same _NAME_RE id-guard +
# _mcp_call delegation as the endpoints above. render/list are generic across the
# two doc capabilities; use-context is Specs-only, pin is Liked-Results-only.
async def capability_render_api(request):
    """GET ?path=<loopyard-rel> → render one doc under the capability's dataDir
    to a complete CSP-safe HTML document (for a sandboxed iframe srcdoc)."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    return JSONResponse(await _mcp_call("capability_render", {
        "cap_id": cap, "project_id": pid,
        "path": request.query_params.get("path", "")}))


async def capability_docs_list_api(request):
    """GET → the doc list for a doc capability: Specs' attached specs
    (``capability_specs_list``) or Liked-Results' pinned gallery
    (``capability_results_list``), dispatched on the capability id."""
    cap = request.path_params.get("cap", "")
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(cap or "") or not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad capability or project id"}, status_code=400)
    if cap == "liked-results":
        return JSONResponse(await _mcp_call("capability_results_list", {"project_id": pid}))
    if cap == "specs":
        return JSONResponse(await _mcp_call("capability_specs_list", {"project_id": pid}))
    return JSONResponse({"error": f"no doc list for capability {cap!r}"}, status_code=400)


async def capability_spec_context_api(request):
    """POST {path} → hand a Specs doc to the loop-creator as trusted context
    (C2). Returns {ok, contextDoc, bytes}."""
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    body = await _json_body(request)
    return JSONResponse(await _mcp_call("capability_spec_use_context", {
        "project_id": pid, "path": body.get("path", ""), "title": body.get("title", "")}))


async def capability_result_pin_api(request):
    """POST {loop, src, note?} | {name, content, note?} → pin an output to the
    Liked-Results gallery with provenance (C3). Returns {ok, entry, meta}."""
    pid = request.path_params.get("project", "")
    if not _NAME_RE.match(pid or ""):
        return JSONResponse({"error": "bad project id"}, status_code=400)
    body = await _json_body(request)
    return JSONResponse(await _mcp_call("capability_result_pin", {
        "project_id": pid, "name": body.get("name", ""), "note": body.get("note", ""),
        "loop": body.get("loop", ""), "src": body.get("src", ""),
        "content": body.get("content", "")}))


# --- DEPRECATED back-compat ALIASES (noun rename Product → Project, Round A) ---
# Same handlers under the old names so /api/loops/products* keeps serving for
# bookmarks, the Mac launcher, and any older client. Delegate 1:1.
async def loop_products_api(request):
    """DEPRECATED alias of :func:`loop_projects_api`."""
    return await loop_projects_api(request)


async def loop_product_save_api(request):
    """DEPRECATED alias of :func:`loop_project_save_api`."""
    return await loop_project_save_api(request)


async def loop_product_add_api(request):
    """DEPRECATED alias of :func:`loop_project_add_api`."""
    return await loop_project_add_api(request)


async def loop_product_delete_api(request):
    """DEPRECATED alias of :func:`loop_project_delete_api`."""
    return await loop_project_delete_api(request)


async def loop_standalone_api(request):
    """Prepare a STANDALONE run — agent@version × product × model. Body:
    {agent_id, product_id, model?, version?}. Returns the prepared run (output
    folder, resolved model + honest modelNote, framed prompt). Does NOT execute
    the agent — live dispatch awaits the worker-daemon model-passthrough patch."""
    body = await _json_body(request)
    agent = body.get("agent_id", "")
    product = body.get("product_id", "")
    if not _NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    if not _NAME_RE.match(product or ""):
        return JSONResponse({"error": "bad product id"}, status_code=400)
    args = {"agent_id": agent, "product_id": product}
    if body.get("model"):
        args["model"] = body["model"]
    if body.get("version") is not None:
        try:
            args["version"] = int(body["version"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad version"}, status_code=400)
    # Forward the launcher's picked origin so the MCP tool's preflight runs
    # against the ACTUAL target — without this the origin selector is
    # decorative and the honesty chip disagrees with what the server checks.
    origin = body.get("origin")
    if origin:
        if not _NAME_RE.match(origin):
            return JSONResponse({"error": "bad origin id"}, status_code=400)
        args["origin"] = origin
    return JSONResponse(await _mcp_call("loop_run_agent_standalone", args))


def _registry_agents() -> list[dict]:
    """Saved registry agents WITH their persona/goal, read straight off disk
    (local root) so the loop editor's picker can prefill in one request."""
    local = _sources()[0][1]              # the local data/_loops dir
    d = local / "_registry" / "agents"
    out = []
    for fn in sorted(os.listdir(d)) if d.is_dir() else []:
        if not fn.endswith(".json"):
            continue
        rec = _read_json(d / fn) or {}
        step = rec.get("step") or {}
        out.append({"id": rec.get("id", fn[:-5]), "note": rec.get("note", ""),
                    "role": step.get("role", "worker"),
                    "personality": step.get("personality", ""),
                    "goal": step.get("goal", "")})
    return out


async def loop_agents_api(request):
    """Registry agents (id, role, persona, goal) for the editor's agent picker."""
    return JSONResponse({"agents": _registry_agents()})


async def loop_config_api(request):
    """Full normalized config of a loop, for editing in the in-dashboard form."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_get", {"name": name}))


async def loop_save_api(request):
    """Validate + persist a loop config authored in the dashboard form.
    Body: {config:{...}}. Returns loop_save's {ok,name,warnings} | {error,errors}."""
    body = await _json_body(request)
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        return JSONResponse({"error": "missing config object"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_save", {"config": cfg}))


async def loop_creator_validate_api(request):
    """Validate a config authored/pasted in the '+loop' editor against the REAL
    schema + linter (QoL-R5 / C2). Body: {config:{...}} OR {text:"…"} (raw editor
    text, incl. a creator's fenced ```json block). Returns loop_creator_validate's
    {ok, config, errors, warnings, lint, source} so the pane can show clear errors
    before Save. Never the client's own re-implementation — the server's gate."""
    body = await _json_body(request)
    args: dict = {}
    if isinstance(body.get("config"), dict):
        args["config"] = body["config"]
    elif isinstance(body.get("text"), dict):
        # a JSON object arriving under `text` (e.g. a client that JSON-encoded the
        # editor) is a config — route it through the config param so the tool never
        # sees a dict where it expects a string.
        args["config"] = body["text"]
    elif isinstance(body.get("text"), str):
        args["text"] = body["text"]
    else:
        return JSONResponse({"ok": False,
                             "errors": ["provide `config` (object) or `text`"]},
                            status_code=400)
    return JSONResponse(await _mcp_call("loop_creator_validate", args))


async def loop_creator_prompt_api(request):
    """Build the seeded loop-creator preprompt for the '+loop' RIGHT terminal +
    the LEFT pane's context summary (QoL-R5 / C1+C2). Returns loop_creator_prompt's
    {prompt, open_questions, context_docs} — the prompt already carries the C1
    context store framed as trusted background."""
    return JSONResponse(await _mcp_call("loop_creator_prompt", {}))


async def loop_creator_suggest_api(request):
    """Suggest a rule-checked, EDITABLE team shape from a described goal (I2), so
    the '+loop' LEFT pane can offer a good starting team the user then edits +
    Validates + Saves. Body: {goal:"…", name?:"…"}. Returns loop_creator_suggest's
    {ok, editable, goal, signals, roles:[{id, role, why}], spec, config, warnings,
    lint, rules, note}. NOT a template — the shape is derived from the goal."""
    body = await _json_body(request)
    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return JSONResponse({"ok": False,
                             "errors": ["provide a `goal` to derive a team from"]},
                            status_code=400)
    args = {"goal": goal}
    if isinstance(body.get("name"), str) and body["name"].strip():
        args["name"] = body["name"]
    return JSONResponse(await _mcp_call("loop_creator_suggest", args))


async def loop_creator_scaffold_api(request):
    """AI-AUTHORING — "＋ Add an agent" in the New-Loop draft editor. Body:
    {spec:{name?, goal, roles:[{id?, role?, personality?, goal?}]}}. Calls the REAL
    ``loop_creator_scaffold`` (deterministic, no AI) so a newly added agent gets the
    same on-model role defaults the creator gives every team. Pure — never saves.
    Returns loop_creator_scaffold's {ok, config, warnings, lint} or {ok:false, errors}."""
    body = await _json_body(request)
    spec = body.get("spec")
    if not isinstance(spec, dict):
        return JSONResponse({"ok": False, "errors": ["provide a `spec` object"]},
                            status_code=400)
    return JSONResponse(await _mcp_call("loop_creator_scaffold", {"spec": spec}))


async def loop_creator_draft_check_api(request):
    """AI-AUTHORING — the live check behind the New-Loop draft editor. Body:
    {config:{...}} (an UNSAVED draft). Calls the REAL ``loop_lint`` tool for the
    authoring findings, and builds the draft's schematic with the SAME
    ``authoring.schematic`` that ``loop_schematic`` renders for a saved loop (that
    tool is name-keyed; a draft has no name on disk yet). Pure — never saves.
    Returns {ok, errors, warnings, lints:[{level,rule,where,message}], schematic,
    offline} (``schematic`` is null while the draft doesn't pass the schema yet;
    ``offline`` is true when the lint tool couldn't be reached)."""
    body = await _json_body(request)
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "errors": ["provide a `config` object"]},
                            status_code=400)
    lint = await _mcp_call("loop_lint", {"config": cfg})
    if not isinstance(lint, dict):
        lint = {}
    out = {"ok": bool(lint.get("ok")),
           "errors": list(lint.get("errors") or []),
           "warnings": list(lint.get("warnings") or []),
           "lints": list(lint.get("lints") or [])}
    # a transport hiccup is not a problem with the draft: flag it (the UI says so
    # calmly) instead of passing a raw connection string through as a draft error.
    out["offline"] = bool(lint.get("error")) and not out["errors"]
    schematic = None
    try:
        from mcp_loops import authoring as _authoring
        res = _schema.validate_config(cfg)
        if res.get("ok"):
            schematic = _authoring.schematic(res["config"])
    except Exception:  # noqa: BLE001 — a preview never breaks the editor
        schematic = None
    out["schematic"] = schematic
    return JSONResponse(out)


async def loop_clone_api(request):
    body = await _json_body(request)
    return JSONResponse(await _mcp_call("loop_clone",
                                        {"from_id": body.get("from_id", ""),
                                         "new_name": body.get("new_name", "")}))


async def loop_files_api(request):
    """List the deliverable files a loop produced (from its artifacts.json)."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    return JSONResponse(await run_in_threadpool(loop_files, name))


async def loop_file_api(request):
    """Serve ONE artifact file, strictly sandboxed to the loop's artifact dirs.
    HTML/SVG/images render in the browser; text is shown as plain text; add
    ?download=1 to force a download."""
    name = request.path_params.get("name", "")
    rel = request.query_params.get("path", "")
    if not _NAME_RE.match(name or "") or not rel:
        return Response("bad request", status_code=400, media_type="text/plain")
    return await run_in_threadpool(
        _serve_file, name, rel, request.query_params.get("download") == "1")


async def loop_download_api(request):
    """Download ALL of a loop's artifacts as a single .zip (sandboxed, capped)."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return Response("bad name", status_code=400, media_type="text/plain")
    try:
        fh = await run_in_threadpool(_zip_artifacts, name)
    except OSError as e:
        return Response(f"zip error (id {_error_ref('zip_artifacts', e)})",
                        status_code=500, media_type="text/plain")
    if fh is None:
        return Response("no artifacts for this loop", status_code=404,
                        media_type="text/plain")
    return StreamingResponse(_iter_and_close(fh), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}-files.zip"'})


async def loops_origins_api(request):
    """List every ORIGIN visible from this box (local + fleet mirrors), with the
    honest CLI-capability probe INLINED per origin (D5 front-door proof).

    ``loop_origin_list`` on a worktree daemon already embeds ``capabilities``;
    but the dashboard proxies to whatever mcp-loops daemon is running, which may
    serve an older ``server.py`` whose list has no inline caps. So we enrich
    here from the long-standing per-origin ``loop_origin_capabilities`` tool —
    making ``/api/loops/origins`` carry inline ``capabilities`` (real ``authed``
    values, never ``"not yet probed"``) regardless of the daemon's version. If
    the engine already inlined them, we leave them untouched (no double probe).
    """
    res = await _mcp_call("loop_origin_list", {})
    origins_list = res.get("origins") if isinstance(res, dict) else None
    if isinstance(origins_list, list):
        for o in origins_list:
            if not isinstance(o, dict) or "capabilities" in o:
                continue                      # already inlined by the engine
            oid = o.get("id")
            if not (isinstance(oid, str) and _NAME_RE.match(oid)):
                continue
            cap = await _mcp_call("loop_origin_capabilities", {"id": oid})
            if not isinstance(cap, dict):
                continue
            o["capabilities"] = cap.get("cliCapabilities", [])
            o["capabilitiesOk"] = cap.get("ok", False)
            if cap.get("lastProbed") is not None:
                o["lastProbed"] = cap.get("lastProbed")
            if not cap.get("ok", False) and cap.get("reason"):
                o["capabilitiesReason"] = cap.get("reason")
    return JSONResponse(res)


async def loop_origin_capabilities_api(request):
    """Report which authed subscription CLIs an origin can run — the honest
    per-origin capability record the run launcher uses to disable models the
    target can't execute. For LOCAL, this triggers a live probe of PATH +
    credential files; for MIRROR origins it returns an honest 'not wired'
    signal with an empty capability list (see suggestions/CROSS-ORIGIN-
    DISPATCH.md). Never fabricates ``authed: true`` and never invents chips.
    """
    oid = (request.path_params.get("id") or "").strip()
    if not _NAME_RE.match(oid or ""):
        return JSONResponse({"error": "bad origin id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_origin_capabilities", {"id": oid}))


async def loop_agent_favorite_api(request):
    """Toggle the favorites flag on an agent (body: {id, favorite:bool})."""
    body = await _json_body(request)
    aid = body.get("id", "")
    if not _NAME_RE.match(aid or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    fav = bool(body.get("favorite", True))
    return JSONResponse(await _mcp_call(
        "loop_agent_favorite", {"agent_id": aid, "favorite": fav}))


# ── Q3: Issues view (terminal loop failures → durable local issues) ──────────
# The engine files a structured issue on a terminal failure (guardian give-up /
# error) via mcp_loops.issues.LocalIssueSink. The dashboard READS through the SAME
# sink (its `list_issues`/`get_issue`), so the on-disk format has one owner. Read
# is fail-soft: a missing/empty _issues dir yields an empty list, never a 500.
def _issue_sink():
    from mcp_loops import issues as _issues
    return _issues.LocalIssueSink()


def _issue_transcript_tail(path, limit: int = 8000):
    """Safely read the tail of an issue's persisted transcript (Q2). Guards that
    the path resolves INSIDE the loops output tree so a tampered issue record can
    never make the dashboard read an arbitrary file. Returns (tail, exists)."""
    if not path:
        return None, False
    try:
        from mcp_loops import report as _report
        root = os.path.realpath(_report.output_base())
        real = os.path.realpath(str(path))
        if real != root and not real.startswith(root + os.sep):
            return None, False           # outside the output tree — refuse
        with open(real, encoding="utf-8") as fh:
            data = fh.read()
        return data[-limit:], True
    except (OSError, ValueError):
        return None, False


async def issues_list_api(request):
    """REDESIGN-SPEC §3 (rd-issues) — the project-scoped reactive INBOX stream.
    Proxies dev-1's ``loop_issues_list`` so the ONE stream is served from the
    authored model (NOT the legacy crash diary): a crash is just the
    ``kind=crash, source=engine`` slice, joined ``loop→project`` for the required
    project. ``?project=`` scopes it (empty ⇒ the cross-project fleet stream the
    switcher filters client-side); the full stream (incl. resolved/dismissed) is
    returned so the client toggles open-work vs all, and every card carries
    ``project/kind/source/severity/status/title``. Fail-soft: a proxy hiccup
    degrades to an empty stream with an ``error`` note, never a 500 — the same
    discipline the legacy local read had."""
    project = request.query_params.get("project", "")
    if project and not _NAME_RE.match(project):
        return JSONResponse({"error": "bad project"}, status_code=400)
    res = await _mcp_call("loop_issues_list",
                          {"project": project, "include_resolved": True})
    if not isinstance(res, dict) or res.get("error") or res.get("ok") is False:
        return JSONResponse({"issues": [], "count": 0, "counts": {},
                             "error": (res or {}).get("error", "issues unavailable")})
    rows = res.get("issues", []) or []
    rows = sorted(rows, key=lambda r: (r.get("ts") is None, -(r.get("ts") or 0.0)))
    return JSONResponse({"issues": rows, "count": len(rows),
                         "counts": res.get("counts", {}), "project": res.get("project")})


async def issue_file_api(request):
    """REDESIGN-SPEC §3.2 — FILE an issue (＋ File an issue), first-class for a
    human. Proxies dev-1's ``loop_issue_file`` (``source="you"``); ``project`` may
    be empty (stored as no project; the UI labels it "Unattributed"), ``kind`` snaps to a safe default
    on a typo so the gesture never fails. Returns ``{ok, issue}``."""
    body = await _json_body(request)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "a title is required"}, status_code=400)
    args = {"project": (body.get("project") or "").strip(), "title": title,
            "kind": (body.get("kind") or "bug").strip(),
            "body": body.get("body") or "", "source": "you",
            "severity": (body.get("severity") or "normal").strip()}
    return _mcp_json(await _mcp_call("loop_issue_file", args))


async def issue_triage_api(request):
    """REDESIGN-SPEC §3.3 — inline TRIAGE (Snooze / Dismiss / Resolve / reopen /
    Point-a-loop's ``resolving``). Proxies dev-1's ``loop_issue_triage``, which
    enforces the legal-transition state machine so a card can never fake a
    resolution; an illegal move returns an honest ``{error, from, legal}``."""
    body = await _json_body(request)
    iid = (body.get("id") or "").strip()
    status = (body.get("status") or "").strip()
    if not iid:
        return JSONResponse({"error": "missing issue id"}, status_code=400)
    if not status:
        return JSONResponse({"error": "missing status"}, status_code=400)
    args = {"id": iid, "status": status, "note": body.get("note") or "",
            "actor": (body.get("actor") or "you").strip() or "you"}
    return _mcp_json(await _mcp_call("loop_issue_triage", args))


# ── §6 targets (a): the Idea Hub two-pane living-document workspace ───────────
# REDESIGN-SPEC §3 (rd-ideahub) + §6: the Hub UI reads/writes dev-1's living-doc
# model through these proxies (loop_docs_list / loop_doc_get / loop_doc_create /
# loop_doc_update), and fires the CORE gesture — point-a-loop-at-a-doc — through
# ``loop_doc_build_loop`` (build → promote → invite-rewrite in one call). Zero
# model logic here: every handler forwards to the tool dev-1 owns.
def _doc_id_ok(did: str) -> bool:
    """A Hub doc id is a sanitized slug (alnum + ``._-``); reject anything with a
    path separator or traversal before it reaches the store."""
    return bool(did) and "/" not in did and ".." not in did and "\x00" not in did


async def docs_list_api(request):
    """The project-scoped Hub RAIL — proxies ``loop_docs_list``. ``?project=``
    scopes (empty ⇒ the cross-project view the switcher filters client-side); each
    row carries the derived ``state``/``glyph`` + ``loop_count`` for the ●LOOP
    badge. Fail-soft to an empty rail with an ``error`` note."""
    project = request.query_params.get("project", "")
    if project and not _NAME_RE.match(project):
        return JSONResponse({"error": "bad project"}, status_code=400)
    res = await _mcp_call("loop_docs_list", {"project": project})
    if not isinstance(res, dict) or res.get("error") or res.get("ok") is False:
        return JSONResponse({"docs": [], "count": 0, "counts": {},
                             "error": (res or {}).get("error", "hub unavailable")})
    return JSONResponse({"docs": res.get("docs", []), "count": res.get("count", 0),
                         "counts": res.get("counts", {}), "project": res.get("project")})


async def doc_get_api(request):
    """One doc's full record (living body + loops + result ribbon + history +
    derived state/glyph) — proxies ``loop_doc_get``."""
    did = (request.path_params.get("id") or "").strip("/")
    if not _doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_doc_get", {"id": did}))


async def doc_create_api(request):
    """＋ New doc — zero-friction capture (§2.2). Proxies ``loop_doc_create``;
    ``project`` optional (an empty one shows in the cross-project view, never a fake
    bucket)."""
    body = await _json_body(request)
    args = {"project": (body.get("project") or "").strip(),
            "title": (body.get("title") or "").strip(),
            "body": body.get("body") or "",
            "loop_rewrite": bool(body.get("loop_rewrite"))}
    return _mcp_json(await _mcp_call("loop_doc_create", args))


async def doc_update_api(request):
    """Edit a doc in place (§2.4) — proxies ``loop_doc_update``. Only the passed
    fields change; ``ready``/``loop_rewrite`` accept booleans (the read-only vs
    loop-may-rewrite toggle); ``result`` = ``"set"`` (Mark done) / ``"clear"``
    (Reopen). Actor is ``you`` (a human editing in the pane) — the owner, the only
    actor the tool lets set/clear a result."""
    body = await _json_body(request)
    did = (body.get("id") or "").strip()
    if not _doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    args = {"id": did}
    if body.get("title") is not None:
        args["title"] = str(body.get("title"))
    if body.get("body") is not None:
        args["body"] = str(body.get("body"))
    for flag in ("ready", "loop_rewrite"):
        v = body.get(flag)
        if v is not None and v != "":
            args[flag] = "true" if v in (True, "true", "1", "yes", "on") else "false"
    res = body.get("result")
    if res is not None and res != "":
        if res not in ("set", "clear"):
            return JSONResponse({"error": "result must be 'set' or 'clear'"}, status_code=400)
        args["result"] = res
    return _mcp_json(await _mcp_call("loop_doc_update", args))


async def doc_move_api(request):
    """Move loops into a plan (their workstream) — proxies ``loop_doc_move``.
    Body ``{loops: [name, ...], to: doc id | ""}``; ``to`` empty takes them out of
    every plan. A loop sits in at most one plan."""
    body = await _json_body(request)
    to = (body.get("to") or "").strip()
    if to and not _doc_id_ok(to):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    loops = body.get("loops")
    if isinstance(loops, str):
        loops = [loops]
    if not isinstance(loops, list) or not loops or len(loops) > 500 or not all(isinstance(n, str) and _NAME_RE.match(n) for n in loops):
        return JSONResponse({"error": "loops must be a list of loop names"}, status_code=400)
    return _mcp_json(await _mcp_call("loop_doc_move", {"loops": loops, "to": to}))


async def doc_build_loop_api(request):
    """THE HUB CORE GESTURE — ▶ point a loop at this doc. Proxies dev-1's
    ``loop_doc_build_loop``: in one call it builds a real loop config FROM the doc,
    saves it, flips the doc to loop-may-rewrite, and points the loop — which
    PROMOTES the doc to an objective (◔ + ●LOOP). Returns ``{ok, loop, config, doc}``;
    the client then starts the saved loop with the normal action seam (§7)."""
    body = await _json_body(request)
    did = (body.get("id") or "").strip()
    if not _doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    return _mcp_json(await _mcp_call("loop_doc_build_loop", {"id": did}))


async def issue_detail_api(request):
    """One issue's full context + its Q2 transcript tail (drill-in). Unknown id →
    404 so the view shows an honest "not found", not an empty shell."""
    iid = (request.path_params.get("id") or "").strip("/")
    if not iid or ".." in iid.split("/") or "/" in iid:
        return JSONResponse({"error": "bad issue id"}, status_code=400)
    try:
        rec = _issue_sink().get_issue(iid)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": _internal_error("issue_detail", e)}, status_code=500)
    if not rec:
        return JSONResponse({"error": f"no issue {iid!r}"}, status_code=404)
    tail, exists = _issue_transcript_tail(rec.get("transcript_path"))
    return JSONResponse({"issue": rec, "transcript_tail": tail,
                         "transcript_exists": exists})


# ── §7 (rd-origins / rd-sessions) read-models — compute felt ambiently ──
# Three thin proxies onto dev-1's fresh read-models. Each fail-softs to an honest
# shape the SPA can render without a special case (never a fake row / faked count).
async def fleet_summary_api(request):
    """The **Fleet pill** data (REDESIGN-SPEC §2 / rd-origins) — proxies
    ``loop_fleet_summary``. Powers the top-bar ``◉ Fleet ●●○ N · M reachable`` pill
    and its popover: the honest counts, per-origin dot rows, and the drop signal
    (an origin *running a loop* that went unreachable). Fail-soft to zeros."""
    res = await _mcp_call("loop_fleet_summary", {})
    if not isinstance(res, dict) or res.get("error"):
        return JSONResponse({"origins": 0, "reachable": 0, "items": [], "dropped": [],
                             "dropCount": 0, "label": "0 origins · 0 reachable",
                             "error": (res or {}).get("error", "fleet unavailable")})
    return JSONResponse(res)


async def sessions_list_api(request):
    """The **Sessions roster** (REDESIGN-SPEC §3 rd-sessions) — proxies
    ``loop_sessions_list``. One card per living collaborator: identity, runtime +
    capability chips, which origin (host, cwd), an honest heartbeat, what it's doing
    now, and what it has created. READ-ONLY this round. Fail-soft to an empty
    roster with an ``error`` note the view surfaces verbatim (U11)."""
    res = await _mcp_call("loop_sessions_list", {})
    if not isinstance(res, dict) or res.get("error"):
        return JSONResponse({"sessions": [], "count": 0, "live": 0,
                             "error": (res or {}).get("error", "sessions unavailable")})
    return JSONResponse(res)


async def devices_list_api(request):
    """The **unified Device Registry** (DEVICE-REGISTRY-SPEC Phase 2) — the ONE
    list any frontend renders. Folds boxes (origins/mirrors), the LOCAL dial-out
    origin-agent, and connected sessions into a single ``Device`` shape
    (``{id, owner, kind, name, capabilities, address_free, last_seen, live,
    channel_ref}``) via :func:`mcp_loops.devices.devices_response`.

    Additive: it READS the same ``loop_origin_list`` + ``loop_sessions_list`` the
    Origins/Sessions pages use and reshapes them — those endpoints are untouched.
    ``?owner=<id>`` scopes the roster (Phase 5 seam). Fail-soft: an upstream outage
    yields an empty roster with an ``error`` note the view surfaces verbatim, never
    a 500. Routing to a device goes THROUGH the hub — ``address_free`` is a label,
    never a routable endpoint."""
    from mcp_loops import devices as _devices

    owner = (request.query_params.get("owner") or "").strip() or None
    origins_res = await _mcp_call("loop_origin_list", {})
    origins = origins_res.get("origins") if isinstance(origins_res, dict) else None
    sessions_res = await _mcp_call("loop_sessions_list", {})
    err = None
    if isinstance(origins_res, dict) and origins_res.get("error"):
        err = origins_res.get("error")
    elif not isinstance(origins, list):
        err = "origins unavailable"
    elif isinstance(sessions_res, dict) and sessions_res.get("error"):
        err = sessions_res.get("error")
    return JSONResponse(_devices.devices_response(
        origins if isinstance(origins, list) else [],
        sessions_res if isinstance(sessions_res, dict) else {},
        owner_filter=owner, error=err))


async def origin_picker_api(request):
    """The **one shared health-aware Origin picker** source (rd-origins) — proxies
    ``loop_origin_picker`` so every loop-birth surface offers the *full reachable
    fleet*, not just ``local``. Returns ``{options}`` (local first, reachable next,
    unreachable shown-but-``disabled`` with a reason, sessions as ``(session)``
    compute). Fail-soft to a local-only option so the composer always works."""
    res = await _mcp_call("loop_origin_picker", {})
    if not isinstance(res, dict) or res.get("error") or not res.get("options"):
        return JSONResponse({"options": [{"id": "local", "label": "local · this box",
                                          "reachable": True, "disabled": False}],
                             "error": (res or {}).get("error") if isinstance(res, dict) else "picker unavailable"})
    return JSONResponse(res)


# ── §8 (rd-ideahub / Q2): the objective-manager THREAD docked to a Hub doc ──
# Three thin proxies onto dev-1's durable, session-backed thread tools
# (loop_thread_get / loop_thread_attach / loop_thread_post). Zero model logic
# here — the honesty discipline (never a faked reply; an honest unattached/offline
# state when no live session is backing the thread) lives in the backend; the
# right pane just paints what these return. The thread's actions — rewrite the
# doc in place, file an issue, point a loop — are executed by the backing session,
# fold back from its REAL returned envelope, and show inline in the doc timeline
# the Hub editor already renders.
async def thread_get_api(request):
    """The objective-manager thread for a Hub doc — proxies ``loop_thread_get``.
    Returns ``{ok, doc_id, thread, doc, session_state}``: the durable conversation,
    the docked doc (derived state/glyph), and the HONEST session_state
    (unattached / offline / awaiting / ready). Read-only apart from dev-1's lazy
    create + fold-of-returned-work; never fabricates a reply."""
    did = (request.path_params.get("id") or "").strip("/")
    if not _doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    return JSONResponse(await _mcp_call("loop_thread_get", {"doc_id": did}))


async def _unknown_doc(did: str) -> JSONResponse | None:
    """The 404 a thread WRITE answers for a doc id that doesn't exist
    (loopyard-bug-1790089840): ``loop_thread_attach``/``loop_thread_post`` would
    lazily create an orphan ``_threads/<id>.json`` and answer 200, so check the
    doc first and create nothing. A failed lookup (mcp-loops down) answers with
    its own status (502); ``None`` means the doc exists — proceed."""
    res = await _mcp_call("loop_doc_get", {"id": did})
    if isinstance(res, dict) and res.get("ok") is not False and not res.get("error"):
        return None
    if not isinstance(res, dict) or not res.get("error"):
        res = {"error": f"no doc {did!r}"}
    return _mcp_json(res)


async def thread_attach_api(request):
    """ATTACH (or detach) the backing SESSION a thread's agent turns run on — Q2's
    "the runtime is a session." Proxies ``loop_thread_attach``. Pass a connector id
    to attach, or an empty ``session`` to detach back to the honest unattached
    state. Naming an unknown/offline id is not an error — the thread simply reads
    ``offline`` until it reconnects. Returns the refreshed thread view."""
    body = await _json_body(request)
    did = (body.get("id") or "").strip()
    if not _doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    session = (body.get("session") or "").strip()
    if (missing := await _unknown_doc(did)) is not None:
        return missing
    return _mcp_json(await _mcp_call("loop_thread_attach",
                                      {"doc_id": did, "session": session}))


async def thread_post_api(request):
    """POST a message to the objective-manager thread — proxies ``loop_thread_post``.
    With a LIVE backing session it records your words, dispatches a real task framed
    on the live doc + conversation (naming the gestures it may make: rewrite the doc
    / file an issue / point a loop), and records a PENDING agent turn the reply folds
    into later from the session's REAL envelope. With no live session your words are
    kept but nothing is faked (honest unattached/offline). Returns the refreshed
    thread view (+ ``dispatched``/``task_id``)."""
    body = await _json_body(request)
    did = (body.get("id") or "").strip()
    if not _doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "message text is required"}, status_code=400)
    if (missing := await _unknown_doc(did)) is not None:
        return missing
    return _mcp_json(await _mcp_call("loop_thread_post",
                                      {"doc_id": did, "text": text}))


# ── §7 (rd-sessions): a capability-honest SEND to an app session ──
# The "Send" switch verb — a real dispatched task to a live connected session, no
# fake takeover. Proxies dev-1's ``dispatch_task`` targeting the session's
# connector id; the session claims and works it on its own compute. The card then
# reflects the returned work on the roster (Follow), never an invented reply.
async def session_dispatch_api(request):
    """Dispatch a real task to a connected app SESSION (REDESIGN-SPEC §rd-sessions
    "Send") — proxies ``dispatch_task`` with the session as the target connector.
    Honest: this hands the message to the session's own compute to claim and run;
    it fabricates no reply. Returns ``{ok, task_id, task}`` or an honest error."""
    body = await _json_body(request)
    sid = (body.get("session") or "").strip()
    text = (body.get("text") or "").strip()
    if not sid or not _NAME_RE.match(sid):
        return JSONResponse({"error": "a valid session id is required"}, status_code=400)
    if not text:
        return JSONResponse({"error": "message text is required"}, status_code=400)
    return JSONResponse(await _mcp_call(
        "dispatch_task",
        {"prompt": text, "connector": sid,
         "spec": {"kind": "session-send", "via": "dashboard"}}))


async def loops_page(request):
    return HTMLResponse(LOOPS_HTML)


# ── the self-contained interactive page ──
# The full self-contained interactive page (Loopyard surface). Lives in a
# sibling module so this file stays focused on route handlers.
from tracking_ui.loops_html import LOOPS_HTML  # noqa: E402
