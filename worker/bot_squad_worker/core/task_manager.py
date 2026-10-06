"""Task Manager — drive Tasks toward completion (design: product-core/SYSTEMS.md).

Goal: move Tasks ``backlog -> todo -> done``. Secondary: gather more info about
under-specified Tasks. Worker management is folded in here (per SYSTEMS.md): the
manager knows the Workers (capabilities / channel / interaction-style / trust),
picks *who* by capability, routes by *channel*, and gates every act through
``autonomy``.

Two action options (same goal, different worker-nature):
  - **assign** — a *direct* dispatch for an AI worker, a *subtle* reminder for a
    human (``interaction_style``). AI assignment is the low-stakes
    ``assign_ai_worker`` action; a human nudge is the ``remind_human`` action,
    which the autonomy gate treats as confirm-first.
  - **ask** — ask a worker for more context to resolve an under-specified Task.

Only the 3 workflow statuses exist (``backlog``/``todo``/``done``) — resist more.
"""
from __future__ import annotations

import re
from typing import Optional

from . import autonomy, store
from .store import Entity

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _tokens(text: Optional[str]) -> set[str]:
    """Lowercase word tokens from free text (label / capabilities csv)."""
    if not text:
        return set()
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _task_terms(conn, task: Entity) -> set[str]:
    """The keywords describing a task: its label plus its tags."""
    terms = _tokens(task.label)
    for tag in store.tags_of(conn, task.id):
        terms |= _tokens(tag)
    return terms


def _assignment_count(conn, worker_id: str) -> int:
    """How many tasks a worker is currently assigned to (load, for tie-break)."""
    return len(store.edges_out(conn, worker_id, "assigned_to"))


def select_worker(conn, task: Entity) -> Optional[Entity]:
    """Pick the best worker for ``task``.

    Scoring = keyword overlap between the task's terms (label + tags) and the
    worker's ``capabilities``. The highest overlap wins; ties break toward the
    worker with the fewest current ``assigned_to`` edges (load balancing).
    Workers with zero overlap are not selected. Returns ``None`` if no worker
    matches.
    """
    terms = _task_terms(conn, task)
    if not terms:
        return None
    best: Optional[Entity] = None
    best_key: tuple[int, int] = (0, 0)  # (overlap, -load) — higher is better
    for w in store.by_type(conn, "worker"):
        overlap = len(terms & _tokens(w.get("capabilities")))
        if overlap == 0:
            continue
        key = (overlap, -_assignment_count(conn, w.id))
        if key > best_key:
            best_key = key
            best = w
    return best


def _assign_action_type(worker: Entity) -> str:
    """Direct dispatch for AI workers, subtle reminder for humans.

    ``interaction_style == 'subtle'`` (a human) routes through ``remind_human``
    (a gentle nudge that still touches a person => confirm-first). Everything
    else is treated as a direct AI dispatch (``assign_ai_worker`` => auto).
    """
    style = (worker.get("interaction_style") or "").lower()
    return "remind_human" if style == "subtle" else "assign_ai_worker"


def assign(conn, task_id: str, worker_id: str) -> dict:
    """Assign a task to a worker.

    Adds a ``worker -assigned_to-> task`` edge and advances the task
    ``backlog -> todo`` (never touches ``done``). Returns an action descriptor
    whose ``requires_confirmation`` is decided by the worker's nature (AI dispatch
    vs human reminder) AND the worker's trust (low trust tightens).
    """
    worker = store.get(conn, worker_id)
    if worker is None:
        raise KeyError(worker_id)
    store.add_edge(conn, worker_id, task_id, "assigned_to")
    task = store.get(conn, task_id)
    if task is not None and task.get("status") == "backlog":
        store.update_payload(conn, task_id, status="todo")
    action_type = _assign_action_type(worker)
    return {
        "action": "assign",
        "action_type": action_type,
        "task_id": task_id,
        "worker_id": worker_id,
        "channel": worker.get("channel"),
        "requires_confirmation": autonomy.requires_confirmation(
            action_type, worker_trust=worker.trust),
    }


def ask_for_context(conn, task_id: str, worker_id: str, question: str) -> dict:
    """Compose the intent to ask a worker for missing context.

    Does NOT actually message anyone — returns an action descriptor with the
    target channel from the worker so a caller (adapter) can realize it.
    """
    worker = store.get(conn, worker_id)
    if worker is None:
        raise KeyError(worker_id)
    action_type = "send_message_as_person" if \
        (worker.get("interaction_style") or "").lower() == "subtle" \
        else "draft"
    return {
        "action": "ask",
        "action_type": action_type,
        "task_id": task_id,
        "worker_id": worker_id,
        "channel": worker.get("channel"),
        "question": question,
        "requires_confirmation": autonomy.requires_confirmation(
            action_type, worker_trust=worker.trust),
    }


def _is_done(conn, eid: str) -> bool:
    e = store.get(conn, eid)
    return e is not None and e.type == "task" and e.get("status") == "done"


def is_blocked(conn, task_id: str) -> bool:
    """A task is blocked if any task it depends on is not yet done.

    ``depends_on`` is derived from incoming ``blocks`` edges (single source of
    truth). A task whose dependencies are all done (or has none) is actionable.
    """
    return any(not _is_done(conn, dep) for dep in store.depends_on(conn, task_id))


def next_actions(conn, slug_tag: Optional[str] = None) -> list[dict]:
    """Propose the next action for each actionable Task, ranked by priority.

    Considers confirmed, not-done tasks (optionally scoped to a tag). A task
    blocked by an unfinished dependency is skipped (not actionable yet). For each
    actionable task: ``assign`` if a capable worker exists, else ``ask`` for more
    context. Results are ordered P0 > P1 > P2 > P3.
    """
    if slug_tag:
        tasks = [e for e in store.entities_by_tag(conn, slug_tag, type_="task")]
    else:
        tasks = store.by_type(conn, "task", trust="confirmed")
    # entities_by_tag ignores trust, so enforce confirmed uniformly here.
    tasks = [t for t in tasks if t.trust == "confirmed"]

    proposals: list[dict] = []
    for task in tasks:
        if task.get("status") == "done":
            continue
        if is_blocked(conn, task.id):
            continue  # a dependency isn't done — not actionable yet
        worker = select_worker(conn, task)
        if worker is not None:
            action_type = _assign_action_type(worker)
            proposals.append({
                "action": "assign",
                "task_id": task.id,
                "worker_id": worker.id,
                "priority": task.get("priority"),
                "action_type": action_type,
                "requires_confirmation": autonomy.requires_confirmation(
                    action_type, worker_trust=worker.trust),
            })
        else:
            proposals.append({
                "action": "ask",
                "task_id": task.id,
                "worker_id": None,
                "priority": task.get("priority"),
                "action_type": "research",
                "requires_confirmation": autonomy.requires_confirmation(
                    "research"),
            })
    proposals.sort(key=lambda p: _PRIORITY_RANK.get(p.get("priority"), 99))
    return proposals
