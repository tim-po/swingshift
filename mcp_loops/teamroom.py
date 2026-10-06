"""teamroom — re-compose the data the loop engine already writes into a
**team-room** payload for the redesigned loop-detail (REDESIGN-SPEC §3 rd-engine,
build order §5.3).

The loop-detail *is* the product: you open it and read **a team, by role,
converging on a goal**. Today the same detail shows counts (``0W 8I 1M``), a
scheduler log, and raw prompt dumps. Every field below is a re-composition of
files the engine ALREADY writes — nothing new is asked of an agent:

- ``config.json``   → the roster (agent ids, roles, personalities → "owns: …")
- ``live.json``     → who is running RIGHT NOW (the live status dot + "doing now")
- ``status.jsonl``  → each agent's reports (last report, report history, the
                      manager's one-line read) + the guardian machinery that
                      signals a stall
- ``progress.json`` → the never-blank turn bar (turns used / wind-down)
- ``budget.turnLimit`` (config) → the turn-bar denominator

This module is PURE: it takes already-read data and returns plain dicts. It reads
nothing off disk and never raises, so the convergence rules and the roster shape
can be unit-tested without a live engine. ``mcp_loops.server`` does the file I/O
and calls :func:`build_team_room`.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp_loops import turn_identity
from mcp_loops.schema import MANAGER

# ── run-state vocab the convergence chip keys off ────────────────────────────
# A POSITIVE terminal state — the only states that may read "Delivered" (green).
# Mirrors the results honesty mandate (§5.4): a stop/error can never look green.
_TERMINAL_GOOD = frozenset({"completed", "early_finish", "done", "finished"})
# States that demand the owner — an error/guardian-stop lands here, never green.
_NEEDS_YOU_STATE = frozenset({
    "waiting_owner", "needs_owner", "error", "failed", "aborted",
    "guardian_stopped", "stopped"})
# Raw ``result.ended`` outcomes that mean the engine gave up / the run was cut
# short — a guardian-stop hides inside ``run.state == "finished"`` (its ``ended``
# was not ``"error"``), so keying the chip off ``state`` alone would read it green.
# MIRRORS ``mcp_loops.resolution._ENDED_ABANDONED`` (the results honesty source);
# a cross-module test locks the two together so they never drift.
_ENDED_ABANDONED = frozenset({"guardian_stopped", "stopped", "aborted"})
# Per-report statuses (mcp_loops.report vocab) that mean the owner is needed.
_NEEDS_YOU_REPORT = frozenset({"needs_owner", "blocked"})
# Per-report statuses that mean an agent finished its charge.
_DONE_REPORT = frozenset({"completed", "early_finish", "done", "finished"})
# Per-report statuses that want attention on the roster (amber dot), short of a
# hard owner-block.
_ATTENTION_REPORT = frozenset({"needs_work", "error", "failed"}) | _NEEDS_YOU_REPORT
# Run states that mean the loop is live (paired with live.json as a fallback).
_RUNNING_STATE = frozenset({"running", "starting", "resuming"})

# The five committed convergence values. Never blank — one is always returned.
ALIGNING = "Aligning"
CONVERGING = "Converging"
STALLED = "Stalled"
NEEDS_YOU = "Needs you"
DELIVERED = "Delivered"


def _first_sentence(text: Any, limit: int = 120) -> Optional[str]:
    """The lead clause of a personality/goal blob → a one-line responsibility.
    Splits on the first sentence end or newline, trims, and caps length. ``None``
    for empty/non-string input."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    # cut at the first hard break (newline) or sentence end, whichever is first
    cut = len(s)
    for mark in (". ", "\n", "! ", "? ", " — "):
        i = s.find(mark)
        if 0 <= i < cut:
            cut = i + (0 if mark == "\n" else 1)
    lead = s[:cut].strip().rstrip(".").strip()
    if len(lead) > limit:
        lead = lead[: limit - 1].rstrip() + "…"
    return lead or None


def display_role(role: Any) -> str:
    """Collapse the internal role vocab to what the roster shows: ``manager`` or
    ``worker``. ``input_provider`` — a worker that feeds the manager — reads as
    ``worker`` in the team room (the §3 sketch's ``MANAGER`` / ``WORKER``)."""
    return "manager" if role == MANAGER else "worker"


def _fmt_elapsed(seconds: Optional[float]) -> Optional[str]:
    """A compact ``1m48s`` / ``12s`` / ``1h03m`` elapsed string, or ``None``."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _agent_ids_in_order(config: dict) -> list[str]:
    """Every top-level agent step id, MANAGER FIRST, then in ``stepOrder`` (which
    may nest parallel groups as sub-lists), then any config-order remainder. Loop
    (sub-loop) steps are skipped — the roster is agents, not machinery."""
    steps = config.get("steps") if isinstance(config, dict) else None
    if not isinstance(steps, dict):
        return []
    agents = [sid for sid, s in steps.items()
              if isinstance(s, dict) and s.get("type", "agent") == "agent"]
    manager = [sid for sid in agents
               if steps[sid].get("role") == MANAGER]

    flat: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            flat.append(node)
        elif isinstance(node, list):
            for x in node:
                _walk(x)

    _walk(config.get("stepOrder") or [])
    ordered: list[str] = []
    seen: set[str] = set()
    for sid in manager + flat + agents:      # manager, then stepOrder, then rest
        if sid in agents and sid not in seen:
            seen.add(sid)
            ordered.append(sid)
    return ordered


def _reports_in_run(reports: list[dict], started: Optional[float] = None, *,
                    run: Optional[dict] = None) -> list[dict]:
    """Scope raw ``status.jsonl`` rows to the current run so a re-run of the same
    loop name never shows the prior run's turns. H7: with ``run`` (run.json) a v2
    row is kept iff its ``runId`` matches; unstamped rows fall back to the window
    (>= ``started``; no ``ts`` kept; no ``started`` drops nothing). The ONE shared
    rule is :func:`mcp_loops.turn_identity.rows_for_run`."""
    return turn_identity.rows_for_run(
        reports, run if run is not None else {"started": started})


def _is_renudge(entry: dict) -> bool:
    """True when a machinery row is the guardian re-nudging a stalled turn
    (``phase == 'guardian'`` and a ``renudge``/``service`` attempt) — the honest
    on-disk signal that a turn stopped moving and the engine is prodding it."""
    if entry.get("kind") != "machinery" or entry.get("phase") != "guardian":
        return False
    blob = f"{entry.get('status', '')} {entry.get('note', '')}".lower()
    return "renudge" in blob or "attempt" in blob or "service" in blob


def compute_convergence(*, run: Optional[dict], progress: Optional[dict],
                        reports: list[dict], live: Optional[dict],
                        turn_limit: Optional[int],
                        ended: Optional[str] = None) -> dict[str, str]:
    """The single COMMITTED convergence chip — one of {Aligning, Converging,
    Stalled, Needs you, Delivered} — plus a one-line ``reason``. Never blank.

    Rules, in strict precedence (honesty first: a stop/error can NEVER read as a
    positive "Delivered"):

    1. **Delivered** — the run reached a POSITIVE terminal state
       (``completed`` / ``early_finish`` / ``done`` / ``finished``) AND its raw
       ``ended`` is not an abandon/error outcome. A guardian-stop persists
       ``run.state = "finished"`` (its ``ended`` was not ``"error"``), so keying
       off ``state`` alone would read it green — ``ended`` is checked here so an
       ABANDONED loop can never show DELIVERED (mirrors the Resolution card).
    2. **Needs you** — the run is parked on the owner or ended badly
       (``waiting_owner`` / ``error`` / ``guardian_stopped`` / ``stopped`` / …, or
       a "finished" run whose raw ``ended`` is ``guardian_stopped`` / ``stopped`` /
       ``aborted`` / ``error``), OR the latest non-machinery report says
       ``needs_owner`` / ``blocked``.
    3. **Stalled** — the loop is live but the most recent activity is the guardian
       RE-NUDGING a stuck turn (no fresh agent report since) — the turn stopped
       moving on its own.
    4. **Converging** — live and closing in: in wind-down (turns used ≥ the turn
       limit), OR past 70% of the turn budget, OR a majority of the workers have
       already posted a done report.
    5. **Aligning** — live and healthy but still early (the default running read),
       and also the pre-run default for a saved loop.

    Pure: reads only the passed-in structures; never raises."""
    run = run or {}
    progress = progress or {}
    state = run.get("state")
    running = state in _RUNNING_STATE or bool((live or {}).get("running"))
    used = progress.get("turns_used")
    used = used if isinstance(used, (int, float)) else 0
    # Honesty: a guardian-stop / cut-short run persists state="finished" but its
    # raw `ended` betrays it. The chip must never read DELIVERED for those.
    bad_ended = ended in _ENDED_ABANDONED or ended == "error"

    # 1 — delivered (the only green) — a positive terminal AND not secretly abandoned
    if state in _TERMINAL_GOOD and not bad_ended:
        return {"value": DELIVERED,
                "reason": f"run reached a positive terminal state ({state})"}

    non_mach = [r for r in reports if r.get("kind") != "machinery"]
    last_real = non_mach[-1] if non_mach else None

    # 2 — needs you (owner-parked or a bad ending, incl. a "finished" run whose
    #     raw `ended` is a guardian-stop / cut-short / error — never green)
    if state in _NEEDS_YOU_STATE or bad_ended:
        why = (f"run is {state} — needs the owner" if state in _NEEDS_YOU_STATE
               else f"the run ended {ended} — needs the owner")
        return {"value": NEEDS_YOU, "reason": why}
    if last_real and last_real.get("status") in _NEEDS_YOU_REPORT:
        who = last_real.get("agent") or "an agent"
        return {"value": NEEDS_YOU,
                "reason": f"{who} reported {last_real.get('status')}"}

    # 3 — stalled (guardian is prodding a stuck live turn)
    if running and reports:
        last_any = reports[-1]
        if _is_renudge(last_any):
            return {"value": STALLED,
                    "reason": "guardian is re-nudging a stalled turn"}

    # 4 — converging (live and closing in)
    if running:
        if turn_limit and used >= turn_limit:
            return {"value": CONVERGING, "reason": "in wind-down — finalizing"}
        if turn_limit and used >= max(1, round(turn_limit * 0.7)):
            return {"value": CONVERGING,
                    "reason": f"past 70% of the turn budget ({used}/{turn_limit})"}
        if _worker_majority_done(reports):
            return {"value": CONVERGING,
                    "reason": "most of the team has posted a done report"}
        return {"value": ALIGNING, "reason": "team is aligning on the goal"}

    # 5 — not running, not terminal-good, not owner-blocked (saved / idle)
    return {"value": ALIGNING, "reason": "not started"}


# ── the single loop STATE (Better-UX #1) — replaces the standalone chip ──────
# One value, three phases. The old "Aligning" convergence chip is FOLDED IN as
# the running sub-states; the run result decides the finished ones.
STATE_CONFIG = "config"                      # saved / never run (was "saved")
STATE_ALIGNING = "aligning"
STATE_CONVERGING = "converging"
STATE_STALLED = "stalled"
STATE_NEEDS_ASSISTANCE = "needs-assistance"
STATE_DELIVERED = "delivered"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"
LOOP_STATES = {
    "config": (STATE_CONFIG,),
    "running": (STATE_ALIGNING, STATE_CONVERGING, STATE_STALLED,
                STATE_NEEDS_ASSISTANCE),
    "finished": (STATE_DELIVERED, STATE_ERROR, STATE_STOPPED),
}
# run.state values that park a LIVE run on the owner (still running, not ended)
_PARKED_STATE = frozenset({"waiting_owner", "needs_owner"})
_ERROR_STATE = frozenset({"error", "failed"})
_STOPPED_STATE = frozenset({"stopped", "stopping", "aborted", "guardian_stopped"})
_STEER_WAITING = frozenset({"stopping", "handoff", "ready"})
_STEER_REASON = {
    "stopping": "steering — finishing the current turn, then pausing",
    "handoff": "steering — preparing the manager for you",
    "ready": "steering — talk to the manager, then restart",
}
# convergence chip → running sub-state
_CHIP_TO_STATE = {ALIGNING: STATE_ALIGNING, CONVERGING: STATE_CONVERGING,
                  STALLED: STATE_STALLED, NEEDS_YOU: STATE_NEEDS_ASSISTANCE}


def compute_loop_state(*, run: Optional[dict], progress: Optional[dict],
                       reports: list[dict], live: Optional[dict],
                       turn_limit: Optional[int],
                       ended: Optional[str] = None) -> dict[str, str]:
    """The ONE loop state the loop-detail shows — ``{value, phase, reason}`` with
    ``phase`` ∈ {config, running, finished} and ``value`` from :data:`LOOP_STATES`.

    Precedence (honesty first — a stop/error can NEVER read ``delivered``):

    1. **error** — ``run.state`` is error/failed, or the raw ``ended`` is error.
    2. **stopped** — stopped/stopping/aborted/guardian-stopped, or a "finished"
       run whose raw ``ended`` betrays an abandon (guardian-stop hides there).
    3. **delivered** — a positive terminal state with a clean ``ended``.
    4. **running** — live (run state or live.json) or parked on the owner: the
       sub-state is the teamroom convergence read (aligning / converging /
       stalled / needs-assistance).
    5. **config** — saved, never run.

    Pure; never raises; never blank."""
    run = run or {}
    state = run.get("state")
    # Better-UX #6: a Steer in progress (gentle stop → manager hand-off → owner
    # talking to it) is NOT a finished/stopped loop — it needs the owner.
    steer = run.get("steer") if isinstance(run.get("steer"), dict) else {}
    if steer.get("phase") in _STEER_WAITING:
        return {"value": STATE_NEEDS_ASSISTANCE, "phase": "running",
                "reason": _STEER_REASON[steer["phase"]]}
    if state in _ERROR_STATE or ended == "error":
        return {"value": STATE_ERROR, "phase": "finished",
                "reason": "the run ended in an error"}
    if state in _STOPPED_STATE or ended in _ENDED_ABANDONED:
        why = ("stopping — finishing the current turn" if state == "stopping"
               else f"the run was stopped ({ended or state})")
        return {"value": STATE_STOPPED, "phase": "finished", "reason": why}
    if state in _TERMINAL_GOOD:
        return {"value": STATE_DELIVERED, "phase": "finished",
                "reason": f"run reached a positive terminal state ({state})"}
    running = (state in _RUNNING_STATE or state in _PARKED_STATE
               or bool((live or {}).get("running")))
    if not running:
        return {"value": STATE_CONFIG, "phase": "config", "reason": "not started"}
    chip = compute_convergence(run=run, progress=progress, reports=reports,
                               live=live, turn_limit=turn_limit, ended=ended)
    return {"value": _CHIP_TO_STATE.get(chip["value"], STATE_ALIGNING),
            "phase": "running", "reason": chip["reason"]}


def run_finished(run: Optional[dict]) -> bool:
    """True when the ONE loop STATE is finished (delivered/error/stopped) —
    then any live.json "in flight" rows are stale leftovers of a crashed or
    stopped run and must not be shown as a second, running status. Pure."""
    run = run if isinstance(run, dict) else {}
    res = run.get("result")
    ended = res.get("ended") if isinstance(res, dict) else None
    return compute_loop_state(run=run, progress=None, reports=[], live=None,
                              turn_limit=None, ended=ended)["phase"] == "finished"


def _worker_majority_done(reports: list[dict]) -> bool:
    """True when a majority of the distinct NON-manager agents that have reported
    this run have a done status as their latest report — a real convergence
    signal short of the run itself ending."""
    latest: dict[str, str] = {}
    for r in reports:
        if r.get("kind") == "machinery" or r.get("role") == MANAGER:
            continue
        agent = r.get("agent")
        if isinstance(agent, str) and r.get("status"):
            latest[agent] = r["status"]           # last write wins (chronological)
    if not latest:
        return False
    done = sum(1 for st in latest.values() if st in _DONE_REPORT)
    return done * 2 >= len(latest)


def turn_bar(*, progress: Optional[dict], turn_limit: Optional[int],
             running: bool) -> dict[str, Any]:
    """The never-blank ``turn N/M · wind-down in K`` bar (§5.3). ``used`` is ALWAYS
    a number (0, not blank, on a fresh run — uxui saw blank TURNS; fixed here at
    the data source). ``winddown_in`` = turns left before wind-down begins
    (``limit - used``); ``in_winddown`` once used has reached the limit."""
    progress = progress or {}
    used = progress.get("turns_used")
    used = int(used) if isinstance(used, (int, float)) else 0
    limit = int(turn_limit) if isinstance(turn_limit, (int, float)) else None
    winddown_in = max(0, limit - used) if limit is not None else None
    winddown_turns = progress.get("winddown_turns")
    return {
        "used": used,
        "limit": limit,
        "winddown_in": winddown_in,
        "winddown_turns": (int(winddown_turns)
                           if isinstance(winddown_turns, (int, float)) else None),
        "in_winddown": bool(limit is not None and used >= limit),
        "running": bool(running),
    }


def _status_dot(agent: str, live_by_agent: dict, last_report: Optional[dict]) -> str:
    """The live status dot for one agent card:
    ``active`` (running now) · ``done`` (last report finished) ·
    ``attention`` (last report needs work/owner) · ``idle`` (otherwise)."""
    if agent in live_by_agent:
        return "active"
    st = (last_report or {}).get("status")
    if st in _DONE_REPORT:
        return "done"
    if st in _ATTENTION_REPORT:
        return "attention"
    return "idle"


# The per-turn dot array is capped so a long run can't bloat the payload; the
# count of older, hidden turns is reported alongside.
TURN_DOTS_MAX = 40


def _turn_dot(status: Any) -> str:
    """One past turn's dot: ``done`` · ``attention`` · ``ok`` (a normal turn)."""
    if status in _DONE_REPORT:
        return "done"
    if status in _ATTENTION_REPORT:
        return "attention"
    return "ok"


TURN_NOTE_MAX = 600


def _turn_note(note: Any) -> Optional[str]:
    if not isinstance(note, str) or not note.strip():
        return None
    n = note.strip()
    return n if len(n) <= TURN_NOTE_MAX else n[:TURN_NOTE_MAX - 1] + "…"


def turn_dots(reports_for_agent: list[dict], *, live: bool) -> dict[str, Any]:
    """Better-UX #2: an agent's dots as a per-turn ARRAY, oldest → newest — one
    dot per finished turn (its report), plus a trailing ``active`` dot for the
    turn running right now. ``{dots:[{seq, turn, status, dot, current}], hidden}``
    where ``hidden`` counts older turns dropped past :data:`TURN_DOTS_MAX`."""
    mine = [r for r in reports_for_agent if r.get("kind") != "machinery"]
    seqs = turn_identity.number_turns(mine)      # H7: stamped per-run turn / legacy index
    dots = [{"seq": i, "turn": r.get("turn"), "status": r.get("status"),
             "dot": _turn_dot(r.get("status")), "current": False,
             # Better-UX #6: the agent's OWN note for that turn — its turn-by-turn
             # steering notes, shown in place of the retired owner input.
             "note": _turn_note(r.get("note")), "ts": r.get("ts")}
            for i, r in zip(seqs, mine)]
    if live:
        dots.append({"seq": max((s for s in seqs if s), default=0) + 1,
                     "turn": None, "status": None,
                     "dot": "active", "current": True})
    hidden = max(0, len(dots) - TURN_DOTS_MAX)
    return {"dots": dots[hidden:], "hidden": hidden}


def _doing_now(entry: Optional[dict], now: Optional[float]) -> Optional[str]:
    """What an agent is doing RIGHT NOW from its live.json row — ``phase`` + live
    elapsed since it started this turn (``round · 1m48s``). ``None`` when the agent
    is not live."""
    if not isinstance(entry, dict):
        return None
    phase = entry.get("phase") or entry.get("kind") or "working"
    since = entry.get("since")
    elapsed = None
    if isinstance(since, (int, float)) and isinstance(now, (int, float)):
        elapsed = _fmt_elapsed(now - since)
    return f"{phase} · {elapsed}" if elapsed else str(phase)


def _last_report(reports_for_agent: list[dict]) -> Optional[dict]:
    """The most recent NON-machinery report for an agent → a compact card field."""
    for r in reversed(reports_for_agent):
        if r.get("kind") != "machinery":
            return {"status": r.get("status"), "note": r.get("note"),
                    "turn": r.get("turn"), "ts": r.get("ts")}
    return None


def roster(*, config: dict, reports: list[dict], live: Optional[dict],
           now: Optional[float]) -> list[dict[str, Any]]:
    """The team spine (§3.B): one card per agent, MANAGER FIRST, replacing the
    ``0W 8I 1M`` counts. Each card = ``{agent, role, displayRole, owns,
    statusDot, doingNow, lastReport, isManager}``, all re-composed from config +
    live.json + status.jsonl."""
    steps = config.get("steps") if isinstance(config, dict) else {}
    steps = steps if isinstance(steps, dict) else {}
    live_by_agent = {}
    for e in (live or {}).get("running") or []:
        if isinstance(e, dict) and e.get("agent"):
            live_by_agent[e["agent"]] = e
    by_agent: dict[str, list[dict]] = {}
    for r in reports:
        a = r.get("agent")
        if isinstance(a, str):
            by_agent.setdefault(a, []).append(r)

    cards = []
    for sid in _agent_ids_in_order(config):
        step = steps.get(sid) or {}
        role = step.get("role")
        last = _last_report(by_agent.get(sid, []))
        owns = _first_sentence(step.get("personality")) \
            or _first_sentence(step.get("goal"))
        personality = step.get("personality")
        goal = step.get("goal")
        cards.append({
            "agent": sid,
            "role": role,
            # Better-UX #7: opening an agent shows its role + FULL personality
            "personality": personality if isinstance(personality, str) else None,
            "goal": goal if isinstance(goal, str) else None,
            "turnDots": turn_dots(by_agent.get(sid, []),
                                  live=sid in live_by_agent),
            "displayRole": display_role(role),
            "isManager": role == MANAGER,
            "owns": owns,
            "statusDot": _status_dot(sid, live_by_agent, last),
            "doingNow": _doing_now(live_by_agent.get(sid), now),
            "lastReport": last,
        })
    return cards


def manager_read(*, config: dict, reports: list[dict]) -> Optional[str]:
    """The manager's one-line read of the goal (§3.A) — the latest non-machinery
    note from any manager-role agent. ``None`` when the manager hasn't reported."""
    steps = config.get("steps") if isinstance(config, dict) else {}
    steps = steps if isinstance(steps, dict) else {}
    managers = {sid for sid, s in steps.items()
                if isinstance(s, dict) and s.get("role") == MANAGER}
    for r in reversed(reports):
        if r.get("kind") == "machinery":
            continue
        if r.get("agent") in managers and r.get("note"):
            return r["note"]
    return None


def agent_report_history(reports: list[dict], agent: str) -> list[dict[str, Any]]:
    """One agent's report history, NEWEST FIRST (§3.B: click a card → report
    history, not a prompt dump). Each entry carries a 1-based ``seq`` matching the
    agent's nth non-machinery turn (the same index ``loop_turn_detail`` opens),
    so the frontend can drill into the agent-turn card."""
    mine = [r for r in reports
            if r.get("agent") == agent and r.get("kind") != "machinery"]
    out = []
    # 1-based, chronological → seq (H7: the stamped per-run turn when present)
    for i, r in zip(turn_identity.number_turns(mine), mine):
        out.append({"seq": i, "status": r.get("status"), "note": r.get("note"),
                    "turn": r.get("turn"), "ts": r.get("ts")})
    out.reverse()                                 # newest first for the UI
    return out


def build_team_room(name: str, *, config: Optional[dict], run: Optional[dict],
                    progress: Optional[dict], live: Optional[dict],
                    reports: list[dict], project: Optional[str],
                    single_agent: bool, now: Optional[float]) -> dict[str, Any]:
    """Assemble the full team-room payload from the raw on-disk structures. The
    ONE call ``mcp_loops.server.loop_team_room`` makes after reading the files.

    ``reports`` is the raw ``status.jsonl`` (machinery included — the Stalled
    signal needs it); this scopes them to the current run internally. Returns
    ``{name, goal, project, single_agent, state:{value,phase,reason},
    convergence:{value,reason},
    turn:{...}, managerRead, roster:[...], run:{state}}``."""
    config = config if isinstance(config, dict) else {}
    run = run or {}
    scoped = _reports_in_run(reports, run=run)
    budget = config.get("budget") if isinstance(config.get("budget"), dict) else {}
    turn_limit = budget.get("turnLimit")
    turn_limit = turn_limit if isinstance(turn_limit, (int, float)) else None
    # the raw terminal outcome (guardian-stop hides inside state="finished") — the
    # same honest signal the Resolution card reads, so the two verdicts agree.
    ended = (run.get("result") or {}).get("ended") if isinstance(run.get("result"), dict) else None
    state = compute_loop_state(run=run, progress=progress, reports=scoped, live=live,
                               turn_limit=turn_limit, ended=ended)
    if state["phase"] == "finished":
        # A crashed/stopped run can leave live.json claiming agents in flight
        # ("active · round · 228h"). The finished STATE is the only status — a
        # stale live file must not paint a second, running one.
        live = {}
    running = run.get("state") in _RUNNING_STATE or bool((live or {}).get("running"))
    return {
        "name": name,
        "goal": config.get("goal"),
        "project": project,
        "single_agent": bool(single_agent),
        # Better-UX #1: the ONE loop state (the old chip folded in). The legacy
        # ``convergence`` key stays for the tracking_ui panel until it migrates.
        "state": state,
        "convergence": compute_convergence(
            run=run, progress=progress, reports=scoped, live=live,
            turn_limit=turn_limit, ended=ended),
        "turn": turn_bar(progress=progress, turn_limit=turn_limit, running=running),
        "managerRead": manager_read(config=config, reports=scoped),
        "roster": roster(config=config, reports=scoped, live=live, now=now),
        "run": {"state": run.get("state", "saved"),
                # Better-UX #6: the Steer phase/attach rides along so the loop
                # view can show the hand-off without a second poll.
                **({"steer": run["steer"]} if isinstance(run.get("steer"), dict)
                   and run["steer"] else {})},
    }
