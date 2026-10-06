"""Loop runner — the engine that drives a validated config's state machine.

Design: the engine is **substrate-agnostic**. All it needs is something that can
(a) brief the manager, (b) run one agent turn and hand back that turn's structured
status, (c) compact an agent before its next turn, and (d) relay an owner ask.
That contract is :class:`Substrate`. The rules below are pure logic, unit-tested
against :class:`FakeSubstrate`; the real :class:`HeadlessSubstrate` (part c/wiring)
maps the same calls onto worker-daemon sessions (spawn_session / inject_input /
sleep_expert) — nothing in the state machine changes.

State machine (matches the agreed spec):

* **Briefing** — before any worker runs, the manager is briefed. ``briefingStart``
  (E1) selects how: ``auto`` (canned substrate handshake, default), ``interactive``
  (a real owner ↔ manager exchange — the manager may ``ask_owner`` and the owner
  replies before the first worker turn), or ``none`` (skipped entirely). Off-budget.
* **Main phase** — repeat rounds (one ``stepOrder`` pass) while ``turnLimit``
  collective turns remain; a round already in flight is allowed to finish past
  the limit. Per scheduled step:
    - **repeat-in-place**: run the agent again (same step) while its status is a
      REPEAT_STATUS for its role and ``repeatsUsed < maxRepeatTurns``
      (worker default 3, others 1). A worker finishes its todos before critics run.
    - **input retirement**: ``satisfied`` → retire now; ``minor_only`` → give it
      ``minorOnlyExtraSteps`` more scheduled runs then retire; when < ``minorSkipThreshold``
      of ``turnLimit`` remains, retire every ``minor_only`` at once.
    - **manager decisions**: ``ask_owner`` pauses (off-budget) and relays to the
      owner; ``wind_down`` / ``complete`` ends the main phase.
  When no input agent is still ``needs_work`` (and there was at least one), the
  main phase ends early.
* **Wind-down** (guaranteed, beyond ``turnLimit``) — MANAGER-GATED (E2): always
  begins with ONE manager operational-check turn; if the manager judges every
  deliverable operational (``complete``) the loop ends there (minimum = 1 turn).
  Only an explicit ``needs_work`` signal expands wind-down into a worker pass +
  manager re-check, repeated until operational, bounded by ``managerFinalizeSteps``.
  No input agents run here.

A ``loop`` step runs its inlined nested config to completion via the same engine
and counts as one turn in the parent.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from mcp_loops import deliverables, report
from mcp_loops.schema import (
    INPUT_PROVIDER,
    MANAGER,
    REPEAT_STATUS,
    WORKER,
    summarize,
    validate_config,
)


# ── the turn outcome + substrate contract ────────────────────────────────────
@dataclass
class TurnOutcome:
    """What one agent turn produced."""
    status: str
    notes: str = ""
    raw: str = ""


class Substrate(Protocol):
    """Everything the engine needs from the execution layer.

    Implementations: :class:`FakeSubstrate` (tests) and ``HeadlessSubstrate``
    (worker-daemon sessions). ``phase`` is one of 'briefing'|'round'|'winddown'
    and ``kind`` a free label (e.g. 'triage','consolidate','final') for logging.
    """

    def brief_manager(self, agent_id: str, goal: str, owner_ask: "OwnerChannel") -> None: ...

    def run_turn(self, agent_id: str, role: str, prompt: str, *,
                 context_cap: int, phase: str, kind: str = "") -> TurnOutcome: ...

    def compact(self, agent_id: str) -> None: ...

    def shutdown(self) -> None: ...


OwnerChannel = Callable[[str], str]
"""Relay a manager question to the owner and block for the reply (Telegram in prod)."""


# ── event log ────────────────────────────────────────────────────────────────
@dataclass
class Event:
    t: float
    phase: str
    agent: str
    role: str
    status: str
    note: str = ""


# ── persistence sink (structural + live events → disk, so the UI can see them) ─
# The engine's own event list (`self.events`) is in-memory; the read-only
# dashboard can only see what's on disk. This sink lets the runner ALSO persist
# machinery-emitted events — parallel-group dispatch, sub-loop start/finish
# (#3,#4) — to status.jsonl, and a LIVE in-progress marker (#5) to live.json.
# It is INJECTABLE and defaults to a no-op so pure-logic tests (and any direct
# LoopRunner use) never write to disk; the server wires the file-backed sink.
class LoopPersistence(Protocol):
    def machinery_event(self, agent: str, role: str, status: str, note: str = "",
                        phase: str = "") -> None: ...
    def set_live(self, entries: "list[dict]") -> None: ...
    def progress(self, turns_used: int, winddown_turns: int) -> None: ...


# ── sub-loop dispatcher (E3: nested loops as INDEPENDENT first-class loops) ────
# A `loop` step can run two ways:
#   * INLINE (default, no dispatcher) — the child shares the parent's substrate +
#     persistence sink + guardian and runs to completion in-process. This is the
#     original behavior, kept for pure-logic tests and any direct LoopRunner use;
#     existing nested configs still complete unchanged.
#   * FIRST-CLASS (a dispatcher is injected) — the child runs as its OWN loop with
#     its OWN run/status/state (own status dir + status.jsonl + run.json, shown
#     SEPARATELY in loop_list / the dashboard). The parent merely DISPATCHES a task
#     and RECEIVES the child's RESULT ENVELOPE + final status back; the parent's
#     status.jsonl records a single dispatch→result link event (by child name),
#     NOT the child's internal turns. The child keeps its own guardian recovery.
# The dispatcher is the server-owned seam (it knows about run.json / status dirs /
# HeadlessSubstrate — things the pure engine deliberately does not). See E3.
class SubloopDispatcher(Protocol):
    def dispatch(self, *, parent_loop: str, sid: str, child_config: dict,
                 owner: "OwnerChannel", should_stop: "Callable[[], bool]",
                 clock: "Callable[[], float]") -> dict:
        """Run ``child_config`` as its own first-class loop and return a result
        dict: ``{child_loop, status, ended, turns_used, winddown_turns,
        envelope}``. ``child_loop`` is the on-disk loop name the parent links to;
        ``envelope`` is the round-2 result envelope (envelope.build_envelope),
        turned inward."""
        ...


class NullPersistence:
    """Default sink: persists nothing (in-memory events only)."""
    def machinery_event(self, agent: str, role: str, status: str, note: str = "",
                        phase: str = "") -> None:
        pass

    def set_live(self, entries: "list[dict]") -> None:
        pass

    def progress(self, turns_used: int, winddown_turns: int) -> None:
        pass


class FilePersistence:
    """File-backed sink used by the live server.

    * ``machinery_event`` appends a line to the loop's ``status.jsonl`` marked
      ``kind="machinery"`` so it renders in the event log but is never mistaken
      for an agent's end-of-turn report (receipts.py + analysis skip these).
    * ``set_live`` rewrites ``live.json`` (atomic replace) with the steps/groups
      currently in flight, so the UI can show a pulsing "running now" marker.

    Fail-soft: a persistence hiccup must never take down a live run, so every
    write swallows its own errors (the in-memory event list is the source of
    truth for the run result).
    """

    def __init__(self, loop_name: str, clock: Callable[[], float] = time.time):
        from mcp_loops import report
        self.loop = loop_name
        self._status_path = report.status_log(loop_name)
        self._live_path = os.path.join(os.path.dirname(self._status_path), "live.json")
        self._progress_path = os.path.join(os.path.dirname(self._status_path), "progress.json")
        self.clock = clock
        self._lock = threading.Lock()

    def machinery_event(self, agent: str, role: str, status: str, note: str = "",
                        phase: str = "") -> None:
        entry = {"ts": self.clock(), "loop": self.loop, "agent": agent,
                 "role": role, "status": status, "note": note,
                 "kind": "machinery", "valid": True}
        if phase:
            entry["phase"] = phase          # Q1: guardian events carry their phase
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self._status_path), exist_ok=True)
                with open(self._status_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass

    def set_live(self, entries: "list[dict]") -> None:
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self._live_path), exist_ok=True)
                tmp = self._live_path + ".tmp"
                payload = {"updated": self.clock(), "running": list(entries)}
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, self._live_path)   # atomic: UI never reads a partial file
        except Exception:  # noqa: BLE001
            pass

    def progress(self, turns_used: int, winddown_turns: int) -> None:
        """A7 (live turn accounting): atomically publish the running turn counts so
        a mid-run poll shows real progress (turns.main was stuck at 0 until the run
        ENDED, because turns_used only ever landed in the final result). Written to
        a runner-owned progress.json — never run.json, so it can't race the server's
        authoritative run-state writes (A2)."""
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self._progress_path), exist_ok=True)
                tmp = self._progress_path + ".tmp"
                payload = {"turns_used": int(turns_used),
                           "winddown_turns": int(winddown_turns),
                           "updated": self.clock()}
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, self._progress_path)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class LoopResult:
    name: str
    ended: str                    # 'complete' | 'turn_limit' | 'early_finish' | 'error'
    turns_used: int               # main-phase turns
    winddown_turns: int
    retired: list[str]
    events: list[Event] = field(default_factory=list)
    error: Optional[str] = None
    # E3: result envelopes of any first-class sub-loops this loop dispatched
    # (task-in / results-out). Empty for the inline path and for loops with no
    # `loop` step. Each entry is a dispatcher result dict (see SubloopDispatcher).
    subloops: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ended": self.ended, "turns_used": self.turns_used,
            "winddown_turns": self.winddown_turns, "retired": self.retired,
            "error": self.error,
            "events": [vars(e) for e in self.events],
            "subloops": self.subloops,
        }


# ── guardian (loop-level recovery, present in every real run, not in config) ──
class Guardian:
    """Wakes when a loop turn fails (timeout / substrate error) and tries to
    recover so the failed step can be retried.

    Budget: ``max_turns`` recovery attempts total per loop. Attempt 1 is a cheap
    machinery re-nudge ("you haven't reported — run the report command now");
    attempts 2..N escalate to a service agent that diagnoses + acts. Each
    successful attempt tells the runner to RETRY the failed step. After
    ``max_turns`` with no recovery it pings the owner and returns ``give_up``,
    which ends the loop. Recovery scope is retry-the-step (not restart-the-loop).

    Needs two optional substrate hooks: ``guardian_renudge(agent_id) -> bool`` and
    ``guardian_service(problem, attempt, max) -> TurnOutcome`` (status
    ``retry``/``give_up``). Missing hooks degrade to an immediate give-up.

    The give-up clock is PER AGENT and is reset by progress: a fresh report from
    the agent (``note_progress``) or a service-granted ``EXTEND`` (bounded by
    ``max_extend_resets`` per stuck episode, so a service that keeps extending
    can't spin forever). A report that lands while recovery is under way
    (optional hook ``has_pending_report(agent_id) -> bool``) overrides a give-up.
    """

    def __init__(self, substrate: Any, loop_name: str,
                 owner_channel: Optional[OwnerChannel] = None, *,
                 max_turns: int = 5, max_extend_resets: int = 3,
                 log: Callable[[str], None] = lambda m: None,
                 emit: Callable[[str, str, str], None] = lambda agent, status, note: None):
        self.sub = substrate
        self.loop_name = loop_name
        self.owner = owner_channel or (lambda q: "")
        self.max_turns = max_turns
        self.log = log
        # emit(agent, status, note): surface each recovery attempt as a real loop
        # event (Q1 guardian visibility). Defaults to a no-op; the LoopRunner
        # wires this to its persistence sink so attempts land in status.jsonl.
        self.emit = emit
        self.max_extend_resets = max_extend_resets
        self.turns_used = 0                     # total recovery attempts (all agents)
        self._attempts: dict[str, int] = {}     # per-agent give-up clock
        self._extends: dict[str, int] = {}      # EXTEND resets this stuck episode
        self.gave_up = False
        self.pinged = False
        self._lock = threading.Lock()   # parallel agents may time out concurrently

    def handle(self, problem: dict) -> str:
        """Return 'retry' (runner retries the failed step) or 'give_up' (end).
        Serialized so concurrent timeouts share the budget correctly."""
        with self._lock:
            return self._handle(problem)

    def note_progress(self, agent: str) -> None:
        """A fresh report from ``agent``: it is alive and productive — restart its
        give-up clock (scattered, individually-recovered timeouts never add up)."""
        with self._lock:
            self._attempts.pop(agent, None)
            self._extends.pop(agent, None)

    def _handle(self, problem: dict) -> str:
        if self.gave_up:
            return "give_up"
        agent = problem.get("agent", "?")
        if self._attempts.get(agent, 0) >= self.max_turns:
            return self._give_up_unless_reported(problem)
        self.turns_used += 1
        n = self._attempts[agent] = self._attempts.get(agent, 0) + 1
        if n == 1:
            ok = self._safe(lambda: self.sub.guardian_renudge(agent))
            self.log(f"[guardian] attempt {n}/{self.max_turns}: re-nudge {agent} (ok={ok!r})")
            self._safe(lambda: self.emit(
                agent, "renudge", f"attempt {n}/{self.max_turns}: re-nudge (ok={ok!r})"))
            return "retry"
        outcome = self._safe(lambda: self.sub.guardian_service(problem, n, self.max_turns))
        status = getattr(outcome, "status", "give_up")
        note = getattr(outcome, "notes", str(outcome))
        self.log(f"[guardian] attempt {n}/{self.max_turns}: service → {status} ({note})")
        self._safe(lambda: self.emit(
            agent, "service", f"attempt {n}/{self.max_turns}: service → {status} ({note})"))
        # the service agent may grant a slow-but-productive agent a longer turn
        # budget via `EXTEND=<minutes>` in its note (mid-loop, per agent).
        m = re.search(r"EXTEND=(\d+)", note or "")
        if m:
            self._safe(lambda: self.sub.extend_agent_timeout(problem.get("agent"), int(m.group(1))))
            self.log(f"[guardian] extended {problem.get('agent')} → {m.group(1)}m per service")
            self._safe(lambda: self.emit(
                problem.get("agent", agent), "extend", f"→ {m.group(1)}m per service"))
            # a granted EXTEND restarts the give-up clock (bounded per episode)
            if status != "give_up" and self._extends.get(agent, 0) < self.max_extend_resets:
                self._extends[agent] = self._extends.get(agent, 0) + 1
                self._attempts[agent] = 0
        if status == "give_up":
            return self._give_up_unless_reported(problem)
        return "retry"

    def _give_up_unless_reported(self, problem: dict) -> str:
        """Give up — unless the agent's report landed while we were recovering
        (a fresh report is progress): then retry so the runner takes it."""
        agent = problem.get("agent", "?")
        hook = getattr(self.sub, "has_pending_report", None)
        if hook is not None and self._safe(lambda: hook(agent)) is True:
            self._attempts.pop(agent, None)
            self._extends.pop(agent, None)
            self.log(f"[guardian] {agent} reported during recovery — not giving up")
            self._safe(lambda: self.emit(agent, "recovered", "report landed during recovery"))
            return "retry"
        return self._give_up(problem)

    def _give_up(self, problem: dict) -> str:
        self.gave_up = True
        if not self.pinged:
            agent = problem.get("agent", "?")
            phase = problem.get("phase", "?")
            self._safe(lambda: self.owner(
                f"⚠ Loop '{self.loop_name}' is stuck on agent '{agent}' "
                f"({phase}/{problem.get('status')}) — the guardian could not recover "
                f"after {self.max_turns} attempts. It needs you."))
            self.pinged = True
        self.log(f"[guardian] GAVE UP on {problem.get('agent')} after "
                 f"{self.max_turns} attempts — owner pinged")
        self._safe(lambda: self.emit(
            problem.get("agent", "?"), "give_up",
            f"gave up after {self.max_turns} attempts — owner pinged"))
        return "give_up"

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — recovery hooks are best-effort
            return e


# ── the engine ────────────────────────────────────────────────────────────────
class LoopRunner:
    """Drive a validated loop config on a substrate."""

    # E1: safety cap on manager clarifying questions during an interactive briefing
    # (each is an off-budget owner round-trip) — bounds a manager that keeps asking.
    _MAX_BRIEFING_ASKS = 8

    def __init__(self, config: Any, substrate: Substrate,
                 owner_channel: Optional[OwnerChannel] = None, *,
                 guardian: "Optional[Guardian]" = None,
                 should_stop: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time,
                 persist: "Optional[LoopPersistence]" = None,
                 subloop_dispatcher: "Optional[SubloopDispatcher]" = None,
                 resume: Optional[dict] = None):
        res = validate_config(config)
        if not res["ok"]:
            raise ValueError("invalid loop config: " + "; ".join(res["errors"]))
        self.cfg = res["config"]
        self.sub = substrate
        # FIX B (sub-loop report routing): agents must report to the loop name the
        # SUBSTRATE tails, not our own cfg name. For a top-level loop these are equal;
        # for a child sub-loop sharing the parent's substrate they differ (child cfg
        # name vs parent substrate name) — reporting under the child name lands the
        # report where the substrate never reads → silent timeout. Follow the substrate.
        self._report_loop = getattr(substrate, "loop", None) or self.cfg["name"]
        self.owner = owner_channel or (lambda q: "")
        self.guardian = guardian
        if self.guardian is not None:
            # Q1: route the guardian's per-attempt activity into our persistence
            # sink so it lands in status.jsonl (as `guardian`-phase machinery
            # events) and renders in the dashboard, not only mcp_loops.log.
            self.guardian.emit = self._guardian_emit
        self.should_stop = should_stop
        self.clock = clock
        self.persist = persist or NullPersistence()
        # E3: when injected, `loop` steps run as first-class dispatched loops;
        # when absent, they run inline (backward-compatible). Child envelopes the
        # dispatcher returns accumulate here and land on the final LoopResult.
        self.subloop_dispatcher = subloop_dispatcher
        self._subloop_results: list[dict] = []
        self.s = summarize(self.cfg)
        self.events: list[Event] = []
        self._live: dict[str, dict] = {}          # step key -> in-flight marker (#5)

        # per-run state
        self.turns_used = 0
        self.winddown_turns = 0
        self.retired: set[str] = set()
        self.minor_left: dict[str, int] = {}     # input id -> scheduled runs left before retire
        self.input_status: dict[str, str] = {}   # latest status per input id
        self._guardian_stop = False              # guardian gave up → end the loop
        self._serial_agents: set[str] = set()    # agents the manager forced serial THIS round
        self._lock = threading.RLock()           # guards shared counters/log under parallel groups
        # stagger between concurrent starts in a parallel group (seconds) so
        # simultaneous cold-boots don't miss prompt delivery; sub-loops boot a
        # whole team so they get more spacing. Tests set these to 0.
        self.group_stagger_subloop = 8.0
        self.group_stagger_agent = 1.5
        # Better-UX #6 STEER: a run restarted after a Steer resumes FROM THE SAME
        # POINT — the turn budget continues where the stopped run left off, the
        # (already owner-steered) manager skips the briefing, and its first turn
        # carries the steer context once. ``{turnsUsed, context}``; None ⇒ fresh.
        self._resume = resume if isinstance(resume, dict) else None
        self._steer_context: Optional[str] = None
        if self._resume:
            try:
                self.turns_used = max(0, int(self._resume.get("turnsUsed") or 0))
            except (TypeError, ValueError):
                self.turns_used = 0
            ctx = self._resume.get("context")
            self._steer_context = ctx.strip() if isinstance(ctx, str) and ctx.strip() else None

    # -- helpers --
    def _cap(self, role: str) -> int:
        caps = self.cfg["contextCaps"]
        return caps["manager"] if role == MANAGER else caps["default"]

    def _log(self, phase: str, agent: str, role: str, status: str, note: str = "") -> None:
        self.events.append(Event(self.clock(), phase, agent, role, status, note))

    def _emit(self, phase: str, agent: str, role: str, status: str, note: str = "") -> None:
        """Log to the in-memory event list AND persist it to status.jsonl as a
        machinery event (so the read-only UI can see structural events #3/#4)."""
        self._log(phase, agent, role, status, note)
        self.persist.machinery_event(agent, role, status, note, phase=phase)

    # -- Q1 (guardian visibility): the guardian's recovery attempts (re-nudge,
    # service-agent turn, extend, give-up) are emitted as real loop events under a
    # `guardian` phase so EVERY attempt shows as a turn in the loops panel + turn
    # view / dashboard — not just in mcp_loops.log + the Telegram alert. The
    # Guardian calls this via its injected `emit` hook (wired in __init__).
    def _guardian_emit(self, agent: str, status: str, note: str = "") -> None:
        self._emit("guardian", agent or "?", "guardian", status, note)

    # -- live in-progress marker (#5): machinery-emitted, zero agent work --
    def _live_add(self, sid: str, role: str, phase: str, kind: str = "") -> None:
        with self._lock:
            self._live[sid] = {"agent": sid, "role": role, "phase": phase,
                               "kind": kind, "since": self.clock()}
            self.persist.set_live(list(self._live.values()))

    def _live_remove(self, sid: str) -> None:
        with self._lock:
            self._live.pop(sid, None)
            self.persist.set_live(list(self._live.values()))

    @property
    def _turn_limit(self) -> int:
        return self.cfg["budget"]["turnLimit"]

    @property
    def _remaining_frac(self) -> float:
        tl = self._turn_limit
        return (tl - self.turns_used) / tl if tl else 0.0

    def _all_inputs(self) -> list[str]:
        return list(self.s["inputs"])

    def _inputs_need_work(self) -> bool:
        """True while at least one non-retired input hasn't signalled done."""
        live = [i for i in self._all_inputs() if i not in self.retired]
        if not live:
            return False
        # only satisfied / minor_only count as "done enough"; never-run, needs_work,
        # or a substrate timeout all still need work (a timeout must not read as done).
        done = {"satisfied", "minor_only"}
        return any(self.input_status.get(i, "needs_work") not in done for i in live)

    # -- retirement policy for input providers --
    def _apply_input_status(self, sid: str, status: str) -> None:
        self.input_status[sid] = status
        if status == "satisfied":
            self.retired.add(sid)
            self._log("round", sid, INPUT_PROVIDER, "retired", "satisfied")
        elif status == "minor_only":
            extra = self.cfg["budget"]["minorOnlyExtraSteps"]
            if sid not in self.minor_left:
                self.minor_left[sid] = extra
            # this very run counts as one of the "more runs"? No: minor_only was
            # reported on a scheduled run; grant `extra` FUTURE runs then retire.
            if self.minor_left[sid] <= 0:
                self.retired.add(sid)
                self._log("round", sid, INPUT_PROVIDER, "retired", "minor_only exhausted")

    def _maybe_budget_skip_minors(self) -> None:
        """< threshold of turnLimit left → retire all minor_only inputs at once."""
        if self._remaining_frac >= self.cfg["budget"]["minorSkipThreshold"]:
            return
        for sid in self._all_inputs():
            if sid in self.retired:
                continue
            if self.input_status.get(sid) == "minor_only":
                self.retired.add(sid)
                self._log("round", sid, INPUT_PROVIDER, "retired", "budget<threshold: minor dropped")

    # -- briefing channel: the manager writes a board/briefing file the team reads --
    def _board_path(self) -> str:
        # Namespace the board by THIS loop's OWN name — not the shared substrate
        # report-loop. Under nesting, every sub-loop shares the parent's report
        # loop, so a single board.md let a sub-loop manager's board CLOBBER the
        # parent's — top-level agents (e.g. the CTO) then read a sub-loop's
        # "we're done, no action" board instead of their real integration orders.
        # One board file PER loop level keeps each manager's board isolated.
        return os.path.join(report.status_dir(self._report_loop),
                            f"board.{self.cfg['name']}.md")

    def _read_board(self) -> str:
        try:
            with open(self._board_path(), encoding="utf-8") as fh:
                return fh.read()[:9000]
        except (FileNotFoundError, OSError):
            return ""

    def _results_digest(self, n: int = 10) -> str:
        lines = []
        for e in self.events[-50:]:
            if e.agent == "__service" or e.status in ("parallel", "subloop_start"):
                continue
            note = (e.note or "").strip()
            if note:
                lines.append(f"- {e.agent} [{e.status}]: {note[:150]}")
        return "\n".join(lines[-n:])

    # -- prompt assembly (briefing-mode aware; the substrate owns delivery) --
    def _prompt(self, sid: str, step: dict, phase: str, kind: str, repeat_ix: int) -> str:
        role = step["role"]
        bmode = self.cfg.get("briefingMode", "baseline")
        imode = self.cfg.get("inputMode", "baseline")
        p = [f"# LOOP: {self.cfg['name']}"]
        # north star: the manager always holds it; the team only in baseline mode
        if role == MANAGER:
            p.append(f"## NORTH STAR (you hold this — do NOT paste it verbatim to your team)\n{self.cfg['goal']}")
        elif bmode == "baseline":
            p.append(f"## NORTH STAR\n{self.cfg['goal']}")
        else:
            p.append(f"## THIS LOOP (context only — your orders come from the manager below)\n{self.cfg['goal'][:220]}…")
        p += [f"## YOU: {sid} ({role})",
              f"### personality\n{step['personality']}",
              f"### your goal\n{step['goal']}"]
        # role operating instructions
        if role == WORKER:
            p.append("### how to work\nYou are a builder. Do REAL work in the codebase THIS turn — create/modify actual files and verify them. Do not merely plan or philosophise; produce working output and report exactly what you changed.")
            # P1 legibility ("give task → find result"): the app/CLI advertises the
            # loop's deliverable at this exact path, but agents run in the working
            # repo and write there by default — so a first-time user opens the
            # advertised folder, finds nothing, and thinks the loop produced nothing.
            # Tell the agent the real path and to land any USER-FACING deliverable
            # (report/summary/generated file) there. Code edits stay in the repo.
            outdir = report.output_dir(self.cfg["name"])
            p.append(
                "### WHERE YOUR RESULT GOES — the user looks HERE\n"
                f"This loop's deliverable folder is:\n  {outdir}\n"
                "That is exactly the path the app/CLI shows the user as this loop's "
                "output. When your turn produces a USER-FACING deliverable — a written "
                "report, a summary of what was accomplished, a generated file — save a "
                "copy INTO that folder (create it if missing), and name it in your "
                "end-of-turn note as `artifact=<path>`. Files you put in its "
                f"`{deliverables.FOLDER}/` sub-folder are shown to the user as the "
                "loop's RESULTS; you may mark SEVERAL files as results — repeat "
                "`artifact=<path>` once per file. Otherwise the user opens the "
                "advertised output folder, finds it empty, and concludes nothing was "
                "produced. Keep editing source in the working repo as normal (report "
                "the commit as `commit=<sha>`); the output folder is for the result "
                "the user asked for, where they will look for it.")
        elif role == INPUT_PROVIDER:
            if imode == "evidence":
                p.append("### how to evaluate — EVIDENCE REQUIRED\nDo NOT judge from reasoning alone. Gather evidence in your sphere: drive/inspect the ACTUAL product, read the REAL code, and research your domain (the web, comparable products) as needed. Your verdict MUST cite what you concretely saw or tested. If you cannot verify it genuinely works, report needs_work — plausible-looking is NOT 'satisfied'.\nIF THE WORK HAS A UI: you MUST actually TEST it, not read it. For every page/view the work touches: run the app, LOAD the page, and EXERCISE its real endpoints (curl/open each API route the page calls) — confirm the page renders REAL data and every call returns success with real content. A page that renders but whose backend endpoint 404s, errors, returns 'Unknown tool', or is empty because the endpoint doesn't exist is a FAILURE, not done. 'Tests pass' is NOT evidence the page works — only loading it and hitting its endpoints is.\nHOW (what your permissions allow): launch the server from the workspace on an unused HIGH port against a /tmp data dir (`python -m <workspace package> --port 18xxx …`, `npm run dev -- --port 18xxx`; one `PYTHONPATH=…` prefix is fine), probe it with `curl` at http://127.0.0.1:<port>/ or http://localhost:<port>/ only — flags first with glued values, the URL last and alone (`curl -sS -XPOST -H'Content-Type:application/json' -d'{\"a\":1}' http://127.0.0.1:18xxx/api/…`) — run tests with `python -m pytest` / `PYTHONPATH=… python -m pytest` (isolate with `--rootdir=<ws> --confcutdir=<ws>`; `-c` is denied), and do fails-before checks in `git worktree add /tmp/<name> <sha>` (then `git -C /tmp/<name> …`). You cannot edit repo files, push, restart services or kill processes — do not try; a DENIED command is a caps gap to report with its exact text, not a product failure.")
            else:
                p.append("### how to evaluate\nAssess whether the work meets the goal and report your verdict.")
        # briefing channel (board / briefing modes)
        if role == MANAGER and bmode in ("briefing", "board"):
            dig = self._results_digest()
            if dig:
                p.append(f"### what your team reported recently\n{dig}")
            if bmode == "board":
                p.append(f"### YOUR BOARD — {self._board_path()}\nThis is how you MANAGE. Your team does NOT see the north star; the board is their only instruction. READ the current board, INSPECT the current product state, then REWRITE the board this turn: a running record of what is done/decided, PLUS a concrete next directive for EACH agent, derived from the north star + recent results + the real product state. Save it to that exact path BEFORE you end your turn.\n\n--- CURRENT BOARD ---\n{self._read_board() or '(empty — create it now)'}")
            else:
                p.append(f"### BRIEF YOUR TEAM — write {self._board_path()}\nYour team does NOT see the north star. WRITE a fresh briefing to that exact path this turn: a concrete next directive for each agent, derived from the north star + recent results + the current product state. Save it before you end your turn.")
        elif role != MANAGER and bmode in ("briefing", "board"):
            label = "board" if bmode == "board" else "briefing"
            p.append(f"### YOUR MARCHING ORDERS (from the manager's {label})\n{self._read_board() or '(the manager has not briefed yet this loop — take the obvious first REAL step in your sphere, then report)'}")
        # E1: interactive loop-start briefing — invite REAL clarifying questions.
        if role == MANAGER and phase == "briefing":
            p.append("### BRIEFING — clarify BEFORE work starts\nThis is the loop-start briefing, before any worker runs. If anything about the goal, scope, or priorities is genuinely unclear, ASK the owner now: report `ask_owner` with your question (you get an answer and may ask again). When you hold the goal clearly, report `continue` to begin the main phase.")
        # E2: wind-down operational check — the manager GATES wind-down here.
        if role == MANAGER and phase == "winddown":
            p.append("### WIND-DOWN — OPERATIONAL CHECK\nThe main phase is over. INSPECT the actual deliverables. If EVERY deliverable is genuinely OPERATIONAL (works + verified), report `complete`: the loop ENDS now, no further worker turns. If something is NOT operational, report `needs_work` with a one-line note on exactly what remains — the workers get ONE pass to fix it, then you are asked again.")
        # Better-UX #6: the manager's FIRST turn after a Steer restart carries the
        # steer context once (the owner's new direction lives in its dialogue).
        if role == MANAGER and phase != "briefing" and self._steer_context:
            p.append("### STEERED — resume from the same point\n"
                     f"{self._steer_context}")
            self._steer_context = None
        if repeat_ix:
            p.append(f"(repeat turn {repeat_ix} — you reported work remaining last turn)")
        vocab = {
            WORKER: "completed | work_remaining",
            INPUT_PROVIDER: "satisfied | minor_only | needs_work",
            MANAGER: "continue | ask_owner | wind_down | complete",
        }[role]
        p.append(
            "### END YOUR TURN by reporting status:\n"
            f"  python -m mcp_loops.report {self._report_loop} {sid} <{vocab}> \"<short note>\"")
        return "\n\n".join(p)

    # -- run one agent step, honouring repeat-in-place; returns final outcome --
    def _raw_turn(self, sid: str, step: dict, phase: str, kind: str, r: int) -> TurnOutcome:
        """One real agent turn: run, count, log, compact. Substrate errors are
        turned into a 'timeout' outcome so the guardian can treat them uniformly."""
        role = step["role"]
        self._live_add(sid, role, phase, kind)         # #5: mark in-flight (cleared below)
        try:
            prompt = self._prompt(sid, step, phase, kind, r)
            outcome = self.sub.run_turn(sid, role, prompt,
                                        context_cap=self._cap(role), phase=phase, kind=kind)
        except Exception as e:  # noqa: BLE001
            if self.guardian is None:
                raise                                   # preserve fail-fast when unguarded (finally clears live)
            outcome = TurnOutcome("timeout", f"substrate error: {type(e).__name__}: {e}")
        finally:
            self._live_remove(sid)                     # report landed (or timed out): clear
        with self._lock:                               # thread-safe under parallel groups
            if phase == "winddown":
                self.winddown_turns += 1
            else:
                self.turns_used += 1
            self._log(phase, sid, role, outcome.status, outcome.notes)
            tu, wt = self.turns_used, self.winddown_turns
        self.persist.progress(tu, wt)                  # A7: live turn counter (outside lock: disk I/O)
        try:
            self.sub.compact(sid)                      # compact-before-next-turn
        except Exception:  # noqa: BLE001
            pass
        return outcome

    def _guarded_turn(self, sid: str, step: dict, phase: str, kind: str, r: int) -> TurnOutcome:
        """A turn wrapped in guardian recovery: on a timeout, ask the guardian to
        recover (re-nudge → service agent) and RETRY the same step, until it
        succeeds or the guardian gives up (which pings the owner)."""
        outcome = self._raw_turn(sid, step, phase, kind, r)
        while outcome.status == "timeout" and self.guardian and not self.guardian.gave_up:
            verdict = self.guardian.handle(
                {"agent": sid, "role": step["role"], "phase": phase,
                 "kind": kind, "status": outcome.status, "note": outcome.notes})
            if verdict == "retry":
                # a report that landed during recovery IS this turn's outcome —
                # take it instead of re-delivering the prompt
                late = self._take_late_report(sid, step, phase)
                outcome = late or self._raw_turn(sid, step, phase, kind, r)
            else:                                       # give_up
                self._guardian_stop = True
                break
        if self.guardian and outcome.status != "timeout":
            self.guardian.note_progress(sid)            # fresh report → reset give-up clock
        return outcome

    def _take_late_report(self, sid: str, step: dict, phase: str) -> "Optional[TurnOutcome]":
        take = getattr(self.sub, "take_pending_report", None)
        try:
            late = take(sid) if take is not None else None
        except Exception:  # noqa: BLE001 — optional hook, best-effort
            late = None
        if late is not None:
            self._log(phase, sid, step["role"], late.status, late.notes)
        return late

    def _run_step(self, sid: str, step: dict, phase: str, kind: str = "") -> TurnOutcome:
        role = step["role"]
        max_repeat = step.get("maxRepeatTurns", 1)
        outcome = TurnOutcome("continue")
        for r in range(max_repeat):
            outcome = self._guarded_turn(sid, step, phase, kind, r)
            if self._guardian_stop:
                break
            # decide whether to repeat this same agent
            if outcome.status not in REPEAT_STATUS.get(role, frozenset()):
                break
        return outcome

    # ── slot execution (a slot is one id, or a parallel group of ids) ──
    def _run_slot(self, sids: list[str], phase: str, parallel: bool) -> dict[str, TurnOutcome]:
        if parallel:
            return self._run_group_parallel(sids, phase)
        out: dict[str, TurnOutcome] = {}
        for sid in sids:
            step = self.cfg["steps"][sid]
            if step.get("type") == "loop":
                self._run_subloop(sid, step)
                out[sid] = TurnOutcome("subloop")
            else:
                out[sid] = self._run_step(sid, step, phase)
            if self._guardian_stop:
                break
        return out

    def _run_group_parallel(self, sids: list[str], phase: str) -> dict[str, TurnOutcome]:
        """Run a group's agents CONCURRENTLY (each is its own session; the wait is
        blocking I/O, so threads give real concurrency). Barrier: join all."""
        self._emit("round", "+".join(sids), "group", "parallel",
                   f"{len(sids)} agents concurrent")
        results: dict[str, TurnOutcome] = {}

        def work(sid: str) -> None:
            step = self.cfg["steps"][sid]
            if step.get("type") == "loop":
                self._run_subloop(sid, step)          # runs the whole child loop
                results[sid] = TurnOutcome("subloop")
            else:
                results[sid] = self._run_step(sid, step, phase)

        threads = [threading.Thread(target=work, args=(sid,), name=f"loop-{sid}")
                   for sid in sids]
        # Stagger starts so concurrent members don't cold-boot their sessions at
        # the exact same instant (simultaneous spawns can miss prompt delivery on
        # a busy daemon). Sub-loops boot a whole team, so give them more spacing.
        stagger = (self.group_stagger_subloop
                   if any(self.cfg["steps"][s].get("type") == "loop" for s in sids)
                   else self.group_stagger_agent)
        for i, t in enumerate(threads):
            if i and stagger:
                time.sleep(stagger)
            t.start()
        for t in threads:
            t.join()
        return results

    def _process_outcome(self, sid: str, step: dict, outcome) -> Optional[str]:
        """Post-turn bookkeeping for one agent (run after the slot's barrier, so
        single-threaded). Returns an 'ended' string (manager wind_down/complete)
        or None."""
        if outcome is None:
            return None
        role = step.get("role")
        if role == INPUT_PROVIDER:
            if self.input_status.get(sid) == "minor_only" and sid in self.minor_left:
                self.minor_left[sid] -= 1
            self._apply_input_status(sid, outcome.status)
        elif role == MANAGER:
            # the manager may force a parallel group SERIAL this round: SERIAL=id,id
            m = re.search(r"SERIAL=([A-Za-z0-9_,\-]+)", outcome.notes or "")
            if m:
                self._serial_agents.update(x for x in m.group(1).split(",") if x)
                self._log("round", sid, MANAGER, "serialized", m.group(1))
            if outcome.status == "ask_owner":
                reply = self.owner(outcome.notes or "The loop needs your input.")
                self._log("round", sid, MANAGER, "owner_reply", reply[:200])
            elif outcome.status in ("wind_down", "complete"):
                if self._inputs_need_work():
                    self._log("round", sid, MANAGER, "wind_down_denied",
                              "inputs still need work")
                else:
                    return outcome.status
        return None

    # ── entry point ──
    def run(self) -> LoopResult:
        try:
            return self._run()
        except Exception as e:  # noqa: BLE001
            return LoopResult(self.cfg["name"], "error", self.turns_used,
                              self.winddown_turns, sorted(self.retired),
                              self.events, error=f"{type(e).__name__}: {e}",
                              subloops=list(self._subloop_results))

    def _run(self) -> LoopResult:
        mgr = self.s["manager"]
        steps = self.cfg["steps"]

        # 1) briefing (owner ↔ manager) — off the turn budget. E1: `briefingStart`
        # selects WHETHER/HOW it happens: `none` skips it (straight to main phase),
        # `auto` is the canned substrate handshake (today's default), `interactive`
        # is a REAL owner↔manager exchange (manager may ask, owner replies) before
        # the first worker round.
        bstart = self.cfg.get("briefingStart", "auto")
        if self._resume:
            # STEER restart: the owner already talked to this manager — no re-brief.
            self._emit("steer", mgr or "-", MANAGER if mgr else "-", "resumed",
                       f"resumed after steer at turn {self.turns_used}")
        elif mgr and bstart != "none":
            if bstart == "interactive":
                self._interactive_briefing(mgr, self.cfg["steps"][mgr])
            else:  # auto
                self.sub.brief_manager(mgr, self.cfg["goal"], self.owner)
                self._log("briefing", mgr, MANAGER, "briefed")

        # 2) main phase — each stepOrder entry is a SLOT: a single id, or a list
        # (a PARALLEL group that runs concurrently unless the manager forced it
        # serial this round via a SERIAL=<ids> note).
        ended = "turn_limit"
        while self.turns_used < self._turn_limit:
            self._serial_agents = set()                # manager's serial calls reset per round
            broke = False
            for entry in self.cfg["stepOrder"]:
                if self.should_stop():                 # cooperative cancel (loop_stop)
                    ended = "stopped"; broke = True; break
                group = entry if isinstance(entry, list) else [entry]
                runnable = [g for g in group if not (
                    steps[g].get("role") == INPUT_PROVIDER and g in self.retired)]
                if not runnable:
                    continue
                # sub-loops CAN run in a parallel group now: _run_group_parallel
                # dispatches each loop member via _run_subloop in its own thread.
                # Isolation across concurrent sub-loops (e.g. separate git
                # worktrees) is the config author's responsibility via each
                # sub-loop's goal; the shared persistence sink is lock-guarded.
                parallel = (len(runnable) > 1
                            and not any(g in self._serial_agents for g in runnable))
                results = self._run_slot(runnable, "round", parallel)
                if self._guardian_stop:
                    ended = "guardian_stopped"; broke = True; break
                stop = None
                for g in runnable:                     # process in order (deterministic)
                    stop = self._process_outcome(g, steps[g], results.get(g))
                    if stop:
                        break
                self._maybe_budget_skip_minors()
                if stop:
                    ended = stop; broke = True; break
            if broke:
                break
            # round finished without a break → early finish check
            if self._all_inputs() and not self._inputs_need_work():
                ended = "early_finish"; break

        # 3) wind-down (guaranteed tail, beyond turnLimit) — skipped if the
        # guardian gave up (substrate likely broken; owner already pinged) or the
        # run was cooperatively stopped.
        # A wind-down crash (e.g. a dead session's bookkeeping raising) must not
        # erase the outcome the main phase already reached — a manager-completed
        # run stays `complete`, never `error`.
        if not self._guardian_stop and ended != "stopped":
            try:
                self._winddown(mgr)
            except Exception as e:  # noqa: BLE001
                self._log("winddown", mgr or "-", MANAGER, "winddown_error",
                          f"{type(e).__name__}: {e}")

        final = "complete" if ended == "complete" else ended
        return LoopResult(self.cfg["name"], final, self.turns_used,
                          self.winddown_turns, sorted(self.retired), self.events,
                          subloops=list(self._subloop_results))

    @staticmethod
    def _winddown_needs_work(outcome: "Optional[TurnOutcome]") -> bool:
        """E2 wind-down gate: expand into a worker pass ONLY when the manager
        EXPLICITLY signals more work is needed. Any other signal — ``complete`` /
        ``wind_down`` / ``operational`` / ``continue``, or an unrecognized/missing
        status — is treated as OPERATIONAL, so wind-down ends after the single
        check turn. This is what makes the minimum wind-down one manager turn, and
        it is backward-safe: a manager (or fake substrate) that never learned the
        new protocol simply ends wind-down in one turn instead of paying the old
        fixed 2-pass toll."""
        if outcome is None:
            return False
        status = (outcome.status or "").strip().lower()
        if status in ("needs_work", "needs-work", "more_work"):
            return True
        note = (outcome.notes or "").lower()
        return "needs_work" in note or "needs work" in note

    def _winddown(self, mgr: Optional[str]) -> None:
        """Guaranteed tail, but MANAGER-GATED (E2). Wind-down ALWAYS begins with
        exactly ONE manager operational-check turn (the safety-check). If the
        manager judges all deliverables OPERATIONAL, the loop ENDS THERE —
        minimum wind-down is a single manager turn, no forced worker passes, no
        re-doing finished work. Only an explicit ``needs_work`` signal expands
        wind-down into a worker pass (roster) + a manager consolidation/re-check,
        repeated until operational and bounded by ``managerFinalizeSteps`` so a
        stuck signal can't spin. The old unconditional 2-worker-pass toll — the
        end-of-run spin the owner flagged — is gone. Manager turns are drawn from
        ``managerFinalizeSteps`` (default 4)."""
        if not mgr:
            return
        steps = self.cfg["steps"]
        workers = self.s["workers"]
        mgr_budget = self.cfg["budget"]["managerFinalizeSteps"]
        if mgr_budget <= 0:
            return

        # ALWAYS: one manager operational-check turn (this doubles as the final
        # report when everything is operational, keeping the minimum at 1 turn).
        outcome = self._run_step(mgr, steps[mgr], "winddown", "check")
        mgr_budget -= 1
        if self._guardian_stop:
            return

        # Expand into worker passes ONLY while the manager signals needs_work; each
        # pass is followed by a manager consolidation/re-check. Bounded by the
        # remaining manager finalize budget.
        while workers and mgr_budget > 0 and self._winddown_needs_work(outcome):
            for wid in workers:
                self._run_step(wid, steps[wid], "winddown", "finalize")
                if self._guardian_stop:
                    return
            outcome = self._run_step(mgr, steps[mgr], "winddown", "consolidate")
            mgr_budget -= 1
            if self._guardian_stop:
                return

    def _interactive_briefing(self, mgr: str, step: dict) -> None:
        """E1 (briefingStart=interactive): a REAL owner↔manager exchange before the
        main phase. The manager runs a briefing turn and MAY ask the owner
        clarifying questions (``ask_owner``); each question is relayed to the owner
        via the existing OwnerChannel and the reply fed back, until the manager
        stops asking (any non-ask status) or a small safety cap is hit. Runs OFF
        the turn budget (like the auto briefing) — briefing turns do not go through
        the main turn counter."""
        for _ in range(self._MAX_BRIEFING_ASKS):
            try:
                prompt = self._prompt(mgr, step, "briefing", "brief", 0)
                outcome = self.sub.run_turn(mgr, MANAGER, prompt,
                                            context_cap=self._cap(MANAGER),
                                            phase="briefing", kind="brief")
            except Exception:  # noqa: BLE001 — briefing is best-effort; never crash the loop
                break
            self._log("briefing", mgr, MANAGER, outcome.status, outcome.notes)
            if outcome.status == "ask_owner":
                reply = self.owner(outcome.notes or "The loop needs your input.")
                self._log("briefing", mgr, MANAGER, "owner_reply", reply[:200])
                continue
            break
        self._log("briefing", mgr, MANAGER, "briefed")

    def _run_subloop(self, sid: str, step: dict) -> None:
        """Run a `loop` step. E3: FIRST-CLASS via the injected dispatcher (child is
        its own loop, parent gets a result envelope + a single link event), else
        INLINE (child shares this loop's substrate/persist/guardian — the original
        behavior). Either way a sub-loop step counts as ONE parent turn."""
        inner = step.get("loop") or {}
        if self.subloop_dispatcher is not None:
            self._run_subloop_firstclass(sid, inner)
        else:
            self._run_subloop_inline(sid, inner)

    def _run_subloop_inline(self, sid: str, inner: dict) -> None:
        self._emit("round", sid, "loop", "subloop_start", inner.get("name", ""))
        # the child inherits our persistence sink so its own structural events
        # land in THIS loop's status.jsonl too (nested state is visible in the UI).
        # FIX A: pass our guardian so a wedged sub-loop agent gets the same
        # recovery (re-nudge → service agent) as top-level agents; without it a
        # child sub-loop had zero recovery and a single stuck agent killed it.
        child = LoopRunner(inner, self.sub, self.owner, clock=self.clock,
                           persist=self.persist, guardian=self.guardian)
        res = child.run()
        with self._lock:            # thread-safe: sub-loops may run in a parallel group
            self.turns_used += 1    # a sub-loop step counts as one parent turn
        self._emit("round", sid, "loop", "subloop_" + res.ended,
                   f"{res.turns_used}+{res.winddown_turns} turns")

    def _run_subloop_firstclass(self, sid: str, inner: dict) -> None:
        """E3: dispatch the nested loop as an INDEPENDENT first-class loop and
        record ONLY the dispatch→result link (by child loop name) in THIS loop's
        status.jsonl — the child's own turns live in the child's status.jsonl."""
        child_hint = inner.get("name") or sid
        # liveness marker before the child starts (links the child by name; carries
        # no child-internal turns). The dispatcher may pick the final on-disk name.
        self._emit("round", sid, "loop", "subloop_dispatch",
                   f"dispatched sub-loop task → {child_hint}")
        try:
            result = self.subloop_dispatcher.dispatch(
                parent_loop=self._report_loop, sid=sid, child_config=inner,
                owner=self.owner, should_stop=self.should_stop, clock=self.clock)
        except Exception as e:  # noqa: BLE001 — a dispatcher failure ends this step, not the crash
            with self._lock:
                self.turns_used += 1
            self._emit("round", sid, "loop", "subloop_error",
                       f"{child_hint}: dispatch failed: {type(e).__name__}: {e}")
            return
        with self._lock:            # thread-safe: sub-loops may run in a parallel group
            self.turns_used += 1    # a sub-loop step counts as one parent turn
            self._subloop_results.append(result)
        child_loop = result.get("child_loop", child_hint)
        status = result.get("status", "?")
        ended = result.get("ended", "?")
        tu = result.get("turns_used", 0)
        wd = result.get("winddown_turns", 0)
        # the single dispatch→result link event: names the child loop + its final
        # status/outcome, NOT its internal turns (those are in the child's log).
        self._emit("round", sid, "loop", "subloop_result",
                   f"{child_loop} → {status} (ended {ended}; {tu}+{wd} turns)")


# ── fake substrate for tests / dry runs ───────────────────────────────────────
class FakeSubstrate:
    """Scriptable in-memory substrate.

    ``scripts`` maps an agent id to a list of ``TurnOutcome`` (or ``(status, note)``
    tuples) returned in order; once exhausted, ``defaults[role]`` is used. This
    lets tests drive exact status sequences through the engine.
    """

    def __init__(self, scripts: Optional[dict[str, list]] = None,
                 defaults: Optional[dict[str, TurnOutcome]] = None,
                 service: Optional[list] = None):
        self._scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self._defaults = defaults or {
            WORKER: TurnOutcome("completed"),
            INPUT_PROVIDER: TurnOutcome("satisfied"),
            MANAGER: TurnOutcome("continue"),
        }
        self._service = list(service or [])           # scripted guardian_service outcomes
        self.calls: list[tuple[str, str, str]] = []   # (agent, phase, kind)
        self.briefed: list[str] = []
        self.compacted: list[str] = []
        self.renudged: list[str] = []                 # guardian_renudge calls
        self.extended: list[tuple] = []               # (agent_id, minutes) extend calls

    def brief_manager(self, agent_id: str, goal: str, owner_ask: OwnerChannel) -> None:
        self.briefed.append(agent_id)

    def run_turn(self, agent_id, role, prompt, *, context_cap, phase, kind=""):
        self.calls.append((agent_id, phase, kind))
        script = self._scripts.get(agent_id)
        if script:
            item = script.pop(0)
            if isinstance(item, TurnOutcome):
                return item
            if isinstance(item, tuple):
                return TurnOutcome(item[0], item[1] if len(item) > 1 else "")
            return TurnOutcome(str(item))
        return self._defaults.get(role, TurnOutcome("continue"))

    def compact(self, agent_id: str) -> None:
        self.compacted.append(agent_id)

    def shutdown(self) -> None:
        pass

    # guardian hooks
    def guardian_renudge(self, agent_id: str) -> bool:
        self.renudged.append(agent_id)
        return True

    def extend_agent_timeout(self, agent_id: str, minutes: float) -> bool:
        self.extended.append((agent_id, minutes))
        return True

    def guardian_service(self, problem: dict, attempt: int, max_attempts: int) -> TurnOutcome:
        if self._service:
            item = self._service.pop(0)
            return item if isinstance(item, TurnOutcome) else TurnOutcome(str(item))
        return TurnOutcome("retry")
