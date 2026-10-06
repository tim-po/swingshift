"""Project heartbeat — the task-board mechanic that keeps coordinators driving.

PROBLEM: coordinators are driven loops that only act when a message arrives and
keep their plans in chat memory — so they stall at arbitrary points (ship half a
thing, then wait forever for a sign-off nobody asked for). FIX: the plan lives
on a durable task board in core.db (tasks tagged ``project:<slug>``), and every
HEARTBEAT_INTERVAL the runner injects one self-drive turn: "here is your board,
here is your next task, do the next concrete step NOW, then update the board."

Three pieces, all in this module:

- **Board reads** — :func:`project_board` groups a project's tasks by status
  (with priority / blocked-via-edges / approval), :func:`render_board` renders
  the compact text injected into the prompt, :func:`pick_next` chooses the one
  task the heartbeat turn should push (P0>P1>P2>P3, then in_progress before
  backlog — finish before starting — then oldest; skips blocked tasks and
  ``approval='needed'`` tasks so the loop never nags a human twice).
- **Board writes** — the argparse CLI (``python -m bot_squad_worker.core
  .heartbeat``) is what the heartbeat TURN uses to update the board: ``board`` /
  ``add`` / ``set-status`` / ``set-approval`` / ``relabel`` / ``block`` /
  ``note``. Simple and
  deterministic — no MCP dependency (mcp-core-store comes later).
- **Ping-once** — :func:`should_ping_tim` tracks per-task ping timestamps in the
  runner's state dict so a task blocked on Tim messages him at most ONCE.

Runner wiring lives in ``coord_shared/heartbeat_hook.py`` (kept out of the
runner files, which are edited by another worker); the 4-line hook is documented
in ``coord_shared/HOOK-SNIPPET.md``.

House style matches ``store``: every function takes an explicit
``sqlite3.Connection``; the CLI opens the db itself (``--db`` > ``$CORE_DB`` >
``<repo>/data/_tg/core.db``).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import store, task_manager
from .store import Entity

# the install root (…/bot-swarm on the coordinator)
_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB = _ROOT / "data" / "_tg" / "core.db"

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
TASK_STATUSES = ("backlog", "in_progress", "done", "archived")
# Statuses that CLOSE a task. Nothing further happens to one, so any human gate
# still flagged on it is stranded — see closing_approval_verdict.
_TERMINAL_STATUSES = ("done", "archived")
_BOARD_ORDER = ("in_progress", "backlog", "done", "archived")  # render order
# The SHARED contract is core.lifecycle's (none|needed|approved|rejected); this
# CLI was missing 'rejected', so the one value only the approval centre can
# produce was the one value the CLI could neither set nor clear.
APPROVALS = ("none", "needed", "approved", "rejected")

# Approvals that mean "a human has NOT said go". A task in one of these is not
# actionable: 'needed' is still waiting, 'rejected' has been REFUSED, and doing
# a refused task is worse than doing nothing.
_NOT_ACTIONABLE = ("needed", "rejected")


def project_tag(project: str) -> str:
    """Normalize a project slug to its tag (accepts either form)."""
    return project if project.startswith("project:") else f"project:{project}"


def _slug(project: str) -> str:
    return project.split(":", 1)[1] if project.startswith("project:") else project


# --- board reads -------------------------------------------------------------
def project_board(conn, project: str) -> dict:
    """The project's task board: tasks tagged ``project:<slug>`` grouped by
    status, each row carrying priority / blocked (derived from ``blocks`` edges
    via task_manager.is_blocked) / approval / due date."""
    tag = project_tag(project)
    statuses: dict[str, list[dict[str, Any]]] = {s: [] for s in _BOARD_ORDER}
    for t in store.entities_by_tag(conn, tag, type_="task"):
        st = t.get("status") or "backlog"
        statuses.setdefault(st, []).append({
            "id": t.id,
            "label": t.label,
            "priority": t.get("priority") or "P2",
            "status": st,
            "approval": t.get("approval") or "none",
            "blocked": task_manager.is_blocked(conn, t.id),
            "due": t.get("due_date"),
            "created_at": t.created_at,
        })
    for rows in statuses.values():
        rows.sort(key=lambda r: (_PRIORITY_RANK.get(r["priority"], 99),
                                 r["created_at"]))
    return {"tag": tag, "statuses": statuses,
            "total": sum(len(v) for v in statuses.values())}


def render_board(board: dict) -> str:
    """Compact text rendering of :func:`project_board` for prompt injection."""
    st = board["statuses"]
    if board["total"] == 0:
        return f"{board['tag']} — board empty"
    counts = " | ".join(f"{len(st[s])} {s}" for s in _BOARD_ORDER if st.get(s))
    lines = [f"{board['tag']} — {counts}"]
    for s in _BOARD_ORDER:
        rows = st.get(s) or []
        if not rows:
            continue
        lines.append(f"{s.upper()}:")
        for r in rows:
            flags = []
            if r["blocked"]:
                flags.append("blocked")
            if r["approval"] != "none":
                flags.append(f"approval={r['approval']}")
            if r["due"]:
                flags.append(f"due {r['due']}")
            tail = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  [{r['priority']}] {r['id'][:8]} {r['label']}{tail}")
    return "\n".join(lines)


def pick_next(conn, project: str) -> Optional[Entity]:
    """The ONE task the heartbeat turn should push, or None (idle).

    Candidates: tasks tagged ``project:<slug>`` with status in
    {backlog, in_progress} that are NOT blocked (an unfinished dependency via
    ``blocks`` edges) and NOT awaiting-or-refused by a human — ``needed`` is
    still waiting (nagging again is the heartbeat's job to avoid) and
    ``rejected`` has been REFUSED. Excluding ``rejected`` is the whole point of
    it being a distinct value: :func:`lifecycle.reject` returns the task to the
    backlog, so a rejected task that read as merely undecided would be the top
    pick on the very next heartbeat and the swarm would immediately do the thing
    Tim just clicked Reject on. Ranking: priority
    P0>P1>P2>P3, then in_progress before backlog at equal priority (finish
    before starting), then oldest first.
    """
    candidates = []
    for t in store.entities_by_tag(conn, project_tag(project), type_="task"):
        if t.get("status") not in ("backlog", "in_progress"):
            continue
        if (t.get("approval") or "none") in _NOT_ACTIONABLE:
            continue
        if task_manager.is_blocked(conn, t.id):
            continue
        candidates.append(t)
    if not candidates:
        return None
    return min(candidates, key=lambda t: (
        _PRIORITY_RANK.get(t.get("priority"), 99),
        0 if t.get("status") == "in_progress" else 1,
        t.created_at))


def stalled_tasks(conn, project: str) -> list:
    """Live tasks that exist but CANNOT be picked — blocked, or awaiting a
    human, or REFUSED by one.

    A board with these and NO actionable task is STALLED, which is a different
    state from EMPTY and must not be treated the same. An empty board means
    nothing was ever started, so nothing of ours is running. A stalled board
    means work IS in flight — possibly detached, possibly BILLING — and merely
    waiting on someone. :func:`pick_next` returns None for both, because there
    is nothing to push forward; the caller decides what that silence means.
    """
    out = []
    for t in store.entities_by_tag(conn, project_tag(project), type_="task"):
        if t.get("status") not in ("backlog", "in_progress"):
            continue
        if (t.get("approval") or "none") in _NOT_ACTIONABLE or task_manager.is_blocked(conn, t.id):
            out.append(t)
    return out


# --- ping-once ---------------------------------------------------------------
def should_ping_tim(conn, project: str, state: dict) -> bool:
    """Ping-once gate: True iff some live ``approval='needed'`` task in the
    project has NOT yet pinged Tim. Records the ping timestamp per task in
    ``state['heartbeat_pings']`` (the runner persists ``state``), so each
    blocked task nags Tim at most once — a task must leave ``needed`` and
    re-enter it (a genuinely new blockage) to earn another ping."""
    pings = state.setdefault("heartbeat_pings", {})
    fresh = [
        t for t in store.entities_by_tag(conn, project_tag(project), type_="task")
        if (t.get("approval") or "none") == "needed"
        and t.get("status") in ("backlog", "in_progress")
        and t.id not in pings
    ]
    if not fresh:
        return False
    now = int(time.time())
    for t in fresh:
        pings[t.id] = now
    return True


# --- the heartbeat turn prompt -------------------------------------------------
def default_pybin() -> str:
    venv = _ROOT / "worker" / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def heartbeat_prompt(board_text: str, task: Entity, project_slug: str, *,
                     pybin: Optional[str] = None) -> str:
    """The instruction for ONE heartbeat turn: push ``task`` one concrete step,
    then update the board via the CLI."""
    slug = _slug(project_slug)
    py = pybin or default_pybin()
    cli = f"{py} -m bot_squad_worker.core.heartbeat"
    tid = task.id[:8]
    return (
        f"PROJECT HEARTBEAT — project:{slug}. Self-drive turn (nobody messaged "
        f"you); keep the project moving on your own.\n\n"
        f"Board:\n{board_text}\n\n"
        f"Next task: {task.label} (id {tid}, {task.get('priority')}, "
        f"{task.get('status')}).\n\n"
        f"Do the NEXT CONCRETE STEP now — build/write/verify, real progress not "
        f"planning. KEEP IT BOUNDED (~15 min): while you work your loop can't "
        f"hear Tim, so if the step is long, START it detached (nohup … & to a "
        f"log), note the pid/log on the board, END the turn, and check it next "
        f"heartbeat instead of grinding.\n"
        f"Then update the board — CLI `{cli} <sub>` from the repo root, ids take "
        f"prefixes like {tid}; subs: board {slug} / add / set-status "
        f"(in_progress→done) / set-approval / block / note. Note what you "
        f"shipped, add follow-ups you found, block a task on its prereq.\n"
        f"If GENUINELY blocked on Tim: message him ONCE, then `{cli} set-approval "
        f"{tid} needed` so the heartbeat stops nagging. Board empty or all "
        f"blocked: reply HEARTBEAT_IDLE."
    )


def pending_ping_prompt(board_text: str, pending: list, project_slug: str, *,
                        pybin: Optional[str] = None) -> str:
    """The instruction for ONE PENDING-DECISIONS turn — fired by the heartbeat
    when :func:`should_ping_tim` reports a decision waiting on Tim that has never
    been surfaced to him.

    This is the piece the module docstring promised ("ping-once") but the runner
    hook never called, so boards where EVERY open task was ``approval='needed'``
    went silent: :func:`pick_next` skips those tasks (rightly — no re-nag), the
    stall watch is forbidden to nag, and nothing was left to tell Tim a decision
    was waiting. The board self-stalled and only moved when Tim happened to
    message in. The whole point of this turn is to break that silence ONCE per
    decision: send Tim a single consolidated digest of what is waiting on him,
    each with a one-line recommendation, so he can decide.
    """
    slug = _slug(project_slug)
    lines = []
    for t in pending:
        lines.append(f"  - {t.label} ({t.get('priority')}, id {t.id[:8]})")
    listing = "\n".join(lines) if lines else "  (none resolved)"
    return (
        f"You are on your PROJECT HEARTBEAT for project:{slug}, and you have "
        f"DECISION(S) waiting on Tim that he has NOT been told about. Nobody "
        f"messaged you; a board that is all 'awaiting approval' cannot move "
        f"until Tim decides, and the heartbeat will not surface these again "
        f"(ping-once), so this is your one chance to put them in front of him.\n\n"
        f"Board:\n{board_text}\n\n"
        f"Decisions currently waiting on Tim:\n{listing}\n\n"
        f"Do exactly this:\n"
        f"1. Send Tim ONE consolidated message via your telegram tool — plain, "
        f"conversational English, no markdown. List each decision above in one "
        f"short line HE can understand (not the internal task label if it is "
        f"jargon), and for EACH give your one-line recommendation + what you'll "
        f"do by default if he doesn't answer. This is a digest, not one message "
        f"per item.\n"
        f"2. If any of these is no longer actually blocked on Tim (you can "
        f"resolve it yourself, or it is stale), clear it instead: "
        f"`set-approval <id> approved|none` or `set-status <id> ...`. Do NOT "
        f"list a decision to Tim that you could make yourself.\n"
        f"3. Say nothing else to Tim and start no long work — this turn exists "
        f"only to make the pending decisions visible. If, after review, NOTHING "
        f"is genuinely waiting on Tim, reply HEARTBEAT_IDLE and send no message.\n"
    )


def stall_prompt(board_text: str, stalled: list, project_slug: str, *,
                 pybin: Optional[str] = None, stalled_for: int = 0) -> str:
    """The instruction for ONE STALL WATCH turn — fired when the board has live
    tasks but NONE is actionable (all blocked / awaiting a human).

    Until 2026-07-13 this state produced NO turn at all: ``pick_next`` returned
    None and the runner just went back to sleep. That silence is what let
    coord-computation's three rented GPU nodes idle for six days and burn a $36
    balance to -$4.30 with zero output — its board was two tasks, one blocked and
    one awaiting Tim, so the heartbeat never fired once. Being blocked on a human
    excuses a coordinator from making PROGRESS; it does not excuse it from
    watching what it already set running. Nagging the human is still forbidden —
    the ping-once rule stands — but going blind is not the way to avoid nagging.
    """
    slug = _slug(project_slug)
    py = pybin or default_pybin()
    cli = f"{py} -m bot_squad_worker.core.heartbeat"
    waited = ""
    if stalled_for >= 3600:
        waited = (f" It has been stalled for at least {stalled_for // 3600}h "
                  f"(since I first noticed).")
    return (
        f"You are on a STALL WATCH for project:{slug} — a scheduled self-drive "
        f"turn. Nobody messaged you. Your board has live work but NOTHING "
        f"actionable: every open task is blocked or waiting on a human.{waited}\n\n"
        f"Board:\n{board_text}\n\n"
        f"This is NOT a 'do the next task' turn — there is no next task. It is a "
        f"CUSTODIAL turn. Being blocked excuses you from making PROGRESS. It does "
        f"not excuse you from watching what you already set running.\n\n"
        f"Do these, in order:\n"
        f"1. UNATTENDED RESOURCES — if this project has ANYTHING running that no "
        f"one is watching turn-by-turn (cloud instances, rented GPUs, detached "
        f"background jobs, paid APIs, cron), CHECK IT NOW with a real command, "
        f"not from memory. Is it doing work? Is it COSTING money while idle? "
        f"Idle-but-billing is exactly how this fleet burned a $36 balance to "
        f"-$4.30 across six silent days: three GPU nodes provisioned, nothing "
        f"watching, zero measurements produced. If something is burning, STOP IT "
        f"or escalate — that is the entire point of this turn.\n"
        f"2. STALE BLOCKERS — for each task waiting on a human, is the blocker "
        f"still real? You may NOT re-nag Tim merely because you are stalled: the "
        f"ping-once rule stands. Re-ping ONLY if the wait has started to COST "
        f"something (money burning, a deadline, a dependency rotting) — and then "
        f"send ONE message that states the cost, not a reminder.\n"
        f"3. REJECTED TASKS — `approval=rejected` means a HUMAN CLICKED REJECT on "
        f"the approval centre. That is a decision, not a blocker, and it is not "
        f"yours to overturn: do NOT set it back to approved/none and do NOT quietly "
        f"do the work anyway. Either `set-status <id> archived` (the answer was no) "
        f"or `add` a NEW task for a different approach and say what changed. If you "
        f"believe the rejection was a mistake, that is a message to Tim, not an "
        f"edit to the field.\n"
        f"4. ROUTE AROUND — is there work the blocker does not actually block? "
        f"Authoring, tests, tooling, a watchdog, a dry run, docs? If so, `add` it "
        f"to the board and do it. A blocked project is not a dead project.\n\n"
        f"Board CLI (run from the repo root):\n"
        f"  {cli} board {slug}\n"
        f"  {cli} add {slug} \"<label>\" [--priority P0|P1|P2|P3]\n"
        f"  {cli} set-status <task_id> backlog|in_progress|done|archived\n"
        f"  {cli} set-approval <task_id> none|needed|approved|rejected   "
        f"(approved/none = actionable again; rejected = a human said no)\n"
        f"  {cli} note {slug} \"<what you found / where things stand>\"\n\n"
        f"If everything is genuinely fine — nothing of yours is running, nothing "
        f"is burning, the blockers are still legitimately pending — reply "
        f"HEARTBEAT_IDLE and spend nothing further. A cheap turn that confirms "
        f"the lights are off is the POINT; silence that assumes it is not."
    )


# --- board CLI (what the heartbeat turn runs) --------------------------------
def _resolve_task_id(conn, prefix: str) -> str:
    """Resolve a full id or unique prefix to a task id (SystemExit on miss)."""
    rows = conn.execute(
        "SELECT id FROM metadata WHERE type='task' AND id LIKE ?",
        (prefix + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise SystemExit(f"no task matches id {prefix!r}")
    raise SystemExit(
        f"ambiguous task id {prefix!r} — matches "
        + ", ".join(r["id"][:8] for r in rows))


def closing_approval_verdict(approval: Optional[str], status: str) -> Optional[str]:
    """Guidance iff this move CLOSES a task that still carries ``approval='needed'``.

    Returns ``None`` when the move is fine, else the refusal text. Pure and
    shared, because there are TWO doors onto the same field (this CLI's
    ``set-status`` and mcp_core_store's ``core_task_set_status``) and a guard on
    one door is not a guard.

    The defect: status and approval are two commands, so a coordinator finishing
    a task types the one that shows on the board and forgets the other. The flag
    then survives its own task — the swarmdev sweep of 2026-07-22 found stale
    ``needed`` on tasks long done, one of them a decision Tim really had made
    eight days earlier. A flag on a closed task is worse than no flag: the
    ping-once gate reads the board as "still waiting on a human", so the queue
    that is supposed to show what Tim owes shows him work that is already over,
    and the items he DOES owe are camouflaged among them.

    It REFUSES rather than auto-clearing, and that is the whole point. 'needed'
    closes two different ways — 'approved' (a human decided, and the record must
    carry the decision) or 'none' (the question was overtaken and no decision was
    ever made). Only the closer knows which. Auto-clearing would have to pick one
    silently, and picking 'none' would erase real decisions — exactly the history
    coord-computation preserved by hand on 855bbbbb (STOP ALL / destroy node-3,
    irreversible, and now recorded as 'approved' instead of merely unflagged).
    Refusing costs one extra word on the command line and cannot lose a fact.
    """
    if status not in _TERMINAL_STATUSES or (approval or "none") != "needed":
        return None
    return (
        f"refusing: this task is approval='needed' and you are closing it "
        f"({status}). Say what happened to the decision, in the same command:\n"
        f"  --approval approved   a human DECIDED it (keeps the decision on the "
        f"record)\n"
        f"  --approval none       no decision was ever made; the question was "
        f"overtaken\n"
        f"Left as 'needed' the flag outlives the task and the approval queue "
        f"shows the human work that is already finished.")


def stranded_approvals(conn) -> tuple[list[dict], dict]:
    """Closed-but-still-flagged tasks, AND the denominator they were found in.

    Returns ``(findings, scope)`` where scope is ``{"tasks": N, "projects": P}``
    — the size of what was actually examined. Green has to carry its own
    evidence: coord-genui deleted all 185 task rows from a scratch copy of the
    db and got output byte-identical to a real clean run, same exit code. The
    all-clear was asserting "checked every project, every status" in a STRING
    while having checked nothing, so a wrong --db, an empty db or a JOIN that
    stopped matching would all read as health. The claim has to come out of the
    measurement, not out of the message.

    The net under :func:`closing_approval_verdict`, because that guard covers the
    two doors that can refuse (this CLI, mcp_core_store) and there is a third
    that cannot: the approval centre's board drag calls ``lifecycle.move`` /
    ``lifecycle.archive`` directly, and a UI drag has nowhere to put a refusal.
    So the guard stops the common case and this finds whatever still gets
    through — that residual door is stated here rather than left to be inferred
    from the guard's existence.

    Deliberately UNFILTERED: no project tag, no status filter. The sweep that
    prompted this looked only at open tasks, so an archived row carrying a live
    flag was structurally invisible to it — coord-computation found one by hand
    that the query could not have surfaced. A query that cannot see a state
    reports zero for it and reads exactly like clean.
    """
    rows = conn.execute(
        "SELECT m.id, m.label, t.status, t.approval FROM metadata m "
        "JOIN task t ON t.metadata_id=m.id").fetchall()
    out = []
    for r in rows:
        if closing_approval_verdict(r["approval"], r["status"]):
            tags = conn.execute(
                "SELECT g.name FROM metadata_tag mt JOIN tag g ON g.id=mt.tag_id "
                "WHERE mt.metadata_id=? AND g.name LIKE 'project:%'",
                (r["id"],)).fetchall()
            out.append({"id": r["id"], "label": r["label"],
                        "status": r["status"],
                        "project": tags[0]["name"] if tags else "(untagged)"})
    projects = conn.execute(
        "SELECT COUNT(DISTINCT g.name) n FROM tag g JOIN metadata_tag mt "
        "ON mt.tag_id=g.id JOIN task t ON t.metadata_id=mt.metadata_id "
        "WHERE g.name LIKE 'project:%'").fetchone()["n"]
    return out, {"tasks": len(rows), "projects": projects}


def task_id_as_slug(conn, slug: str) -> Optional[str]:
    """The task this 'project slug' actually names, or None if it names none.

    The PREDICATE behind :func:`_reject_task_id_as_slug`, split out because the
    CLI is no longer the only door: mcp-core-store's ``core_note_add`` takes the
    same slug and must refuse the same mistake. A guard that lives at one door
    is blind to every other door, and the MCP server cannot reuse the CLI's
    refusal directly because that one raises SystemExit — a BaseException, which
    the server's fail-soft wrapper deliberately does not catch. So the RULE
    lives here once and each door renders it in its own currency (SystemExit for
    the CLI, an {"error": ...} dict for the tool).
    """
    if len(slug) < 6 or not all(c in "0123456789abcdef" for c in slug.lower()):
        return None
    rows = conn.execute(
        "SELECT id FROM metadata WHERE type='task' AND id LIKE ?",
        (slug + "%",)).fetchall()
    return rows[0]["id"] if rows else None


def _reject_task_id_as_slug(conn, slug: str) -> None:
    """Refuse a PROJECT slug that is really a TASK id.

    ``note`` / ``add`` / ``board`` take a project slug, but the task-mutating
    commands next to them (``set-status``, ``set-approval``, ``block``) take a
    task id — so the natural slip is ``note <task-id> "..."``. That used to
    SUCCEED, silently minting a brand-new one-row project called
    ``project:<task-id>`` and filing the note there instead of anywhere near the
    task the author meant. 13 such tags were on the shared board when this guard
    was written (found by ops/module_drift.py), including several minted the same
    day by the coordinator that then went looking for its own note.

    The guard is deliberately narrow: it fires only when the slug actually
    RESOLVES to a task in this database. A real slug ("umem", "plancheck") never
    does, and a genuinely new project's first task is unaffected, so this cannot
    block legitimate work — it only refuses the one case that was always a
    mistake.
    """
    hit = task_id_as_slug(conn, slug)
    if not hit:
        return
    raise SystemExit(
        f"refusing: {slug!r} is a TASK id ({hit[:8]}), not a project "
        f"slug. This command takes a PROJECT slug (umem, plancheck, swarmdev …) "
        f"— passing a task id would create a junk project:{slug} tag and file "
        f"your text where nobody will look for it. If you meant to annotate the "
        f"task, note it under its project slug and name the task in the text.")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m bot_squad_worker.core.heartbeat",
        description="Project task-board CLI (heartbeat mechanic).")
    ap.add_argument("--db", default=None,
                    help="core db path (default: $CORE_DB or data/_tg/core.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("board", help="show the project board")
    p.add_argument("slug")

    p = sub.add_parser("add", help="add a task to the project board")
    p.add_argument("slug")
    p.add_argument("label")
    p.add_argument("--priority", default="P2", choices=list(_PRIORITY_RANK))
    p.add_argument("--status", default="backlog",
                   choices=["backlog", "in_progress"])

    p = sub.add_parser("set-status", help="move a task on the board")
    p.add_argument("task_id")
    p.add_argument("status", choices=list(TASK_STATUSES))
    p.add_argument("--approval", default=None, choices=list(APPROVALS),
                   help="settle the human gate in the SAME command (required "
                        "when closing a task that is approval='needed')")

    p = sub.add_parser("set-approval",
                       help="human gate: needed = waiting on Tim (heartbeat "
                            "skips it), approved/none = actionable again")
    p.add_argument("task_id")
    p.add_argument("approval", choices=list(APPROVALS))

    p = sub.add_parser("relabel",
                       help="rewrite a task's LABEL — the instruction the "
                            "heartbeat hands the next turn. A stale label is a "
                            "stale order; --append adds to the current one")
    p.add_argument("task_id")
    p.add_argument("text")
    p.add_argument("--append", action="store_true",
                   help="append ' — <text>' to the existing label instead of "
                        "replacing it (e.g. a correction/addendum without "
                        "losing the original instruction)")

    p = sub.add_parser("ask",
                       help="write the DECISION ROW a human reads on the "
                            "approval centre: the one-line question + what you "
                            "advise and will do by default if nobody answers")
    p.add_argument("task_id")
    p.add_argument("question", help="the ask, in the HUMAN's words, one line")
    p.add_argument("--recommend", default=None,
                   help="what you advise AND what you will do by DEFAULT if "
                        "the answer never comes — the default is what makes "
                        "not-deciding safe")

    sub.add_parser("audit-approvals",
                   help="fleet-wide: closed tasks still flagged approval=needed")

    p = sub.add_parser("block", help="task waits until blocker task is done")
    p.add_argument("task_id")
    p.add_argument("blocker_task_id")

    p = sub.add_parser("note", help="record a distilled project note artifact")
    p.add_argument("slug")
    p.add_argument("text")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    db = args.db or os.environ.get("CORE_DB") or str(_DEFAULT_DB)
    conn = store.connect(db)
    try:
        if args.cmd == "board":
            _reject_task_id_as_slug(conn, args.slug)
            print(render_board(project_board(conn, args.slug)))
        elif args.cmd == "add":
            _reject_task_id_as_slug(conn, args.slug)
            t = store.create_task(conn, label=args.label,
                                  priority=args.priority, status=args.status,
                                  trust="confirmed",
                                  tags=[project_tag(args.slug)])
            print(f"created {t.id} [{args.priority}] {args.label} "
                  f"({args.status})")
        elif args.cmd == "set-status":
            tid = _resolve_task_id(conn, args.task_id)
            if args.approval is None:
                cur = store.get(conn, tid)
                verdict = closing_approval_verdict(
                    (cur or {}).get("approval"), args.status)
                if verdict:
                    raise SystemExit(verdict)
            fields: dict[str, Any] = {"status": args.status}
            if args.approval is not None:
                fields["approval"] = args.approval
            store.update_payload(conn, tid, **fields)
            print(f"{tid[:8]} -> {args.status}"
                  + (f" approval={args.approval}" if args.approval else ""))
        elif args.cmd == "set-approval":
            tid = _resolve_task_id(conn, args.task_id)
            store.update_payload(conn, tid, approval=args.approval)
            print(f"{tid[:8]} approval={args.approval}")
        elif args.cmd == "relabel":
            tid = _resolve_task_id(conn, args.task_id)
            cur = store.get(conn, tid)
            if cur is None:
                raise SystemExit(f"no task {tid[:8]}")
            old = cur.label or ""
            text = args.text.strip()
            if not text:
                # A label IS the instruction the heartbeat reads out; a blank
                # one is not "no order", it is an EMPTY order the next turn
                # still gets handed. Same class as an empty `ask`: refuse it.
                raise SystemExit("the label is empty — say what the task IS, "
                                 "or leave it unchanged")
            if args.append:
                if not old.strip():
                    # --append onto nothing is just a set; but SAY so rather
                    # than silently producing a label that starts with ' — '.
                    raise SystemExit(
                        f"{tid[:8]} has no label to append to — relabel it "
                        f"without --append to set one")
                new = f"{old.rstrip()} — {text}"
            else:
                new = text
            store.set_label(conn, tid, new)
            # Print BOTH so the rewrite is auditable — the whole point of this
            # verb is that a wrong label silently mis-orders the next turn, so a
            # relabel that shows only the new text hides exactly what it changed.
            print(f"{tid[:8]} relabelled")
            print(f"  was: {old[:100] or '(empty)'}")
            print(f"  now: {new[:100]}")
        elif args.cmd == "ask":
            tid = _resolve_task_id(conn, args.task_id)
            question = args.question.strip()
            if not question:
                # An empty ask is WORSE than none: it renders as a blank row,
                # which reads as a question already answered. Refuse it.
                raise SystemExit("the ask is empty — say what you are asking, "
                                 "or leave the field unset")
            fields: dict[str, Any] = {"ask": question}
            if args.recommend is not None:
                fields["recommend"] = args.recommend.strip() or None
            store.update_payload(conn, tid, **fields)
            print(f"{tid[:8]} ask={question[:60]}"
                  + (f" recommend={args.recommend[:40]}" if args.recommend else ""))
            if not args.recommend:
                # Not refused — recording the QUESTION is still better than not
                # recording it, and a hard refusal here would push people to skip
                # `ask` entirely. But say what the omission costs: the page now
                # shows this row as having no stated default, because a decision
                # nobody makes is still an outcome and it should be a chosen one.
                print("  no --recommend: the approval centre will show this row "
                      "as having NO stated default, i.e. 'if you do not answer, "
                      "this waits indefinitely'. If that is not what happens, "
                      "say what does.")
        elif args.cmd == "audit-approvals":
            stranded, scope = stranded_approvals(conn)
            if not scope["tasks"]:
                # NOT clean — BLIND. A board with zero tasks is a reading
                # failure (wrong --db, empty db, a JOIN that stopped matching),
                # and it must never share an exit code with a real all-clear:
                # as a cron net, rc is the whole signal and nobody reads stdout.
                print(f"CANNOT SEE: 0 tasks in {db} — nothing was checked, so "
                      f"this is NOT an all-clear. Wrong --db/$CORE_DB, an empty "
                      f"database, or the metadata/task join no longer matches.")
                return 2
            if not stranded:
                print(f"checked {scope['tasks']} tasks across "
                      f"{scope['projects']} projects (every status) — no "
                      f"stranded approvals")
            for s in stranded:
                print(f"{s['id'][:8]} [{s['status']}] {s['project']} "
                      f"{s['label'][:60]}")
                print(f"  settle it: set-status {s['id'][:8]} {s['status']} "
                      f"--approval approved|none")
            if stranded:
                print(f"{len(stranded)} stranded of {scope['tasks']} tasks "
                      f"checked across {scope['projects']} projects")
            return 1 if stranded else 0
        elif args.cmd == "block":
            tid = _resolve_task_id(conn, args.task_id)
            blocker = _resolve_task_id(conn, args.blocker_task_id)
            store.add_edge(conn, blocker, tid, "blocks")
            print(f"{blocker[:8]} blocks {tid[:8]}")
        elif args.cmd == "note":
            _reject_task_id_as_slug(conn, args.slug)
            label = args.text if len(args.text) <= 72 else args.text[:69] + "…"
            a = store.create_artifact(conn, content=args.text,
                                      state="distilled", trust="confirmed",
                                      label=label,
                                      tags=[project_tag(args.slug)])
            print(f"noted {a.id[:8]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
