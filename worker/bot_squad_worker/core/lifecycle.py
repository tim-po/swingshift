"""Task-autonomy lifecycle — the agile-board state machine (SHARED CONTRACT).

PRINCIPLE (Tim, 2026-07-01): entity **existence is never human-gated**. The only
human gate is approving **HIGH-RISK AI-task EXECUTION**. So:

- Artifacts + Events + Tasks are AUTO-CONFIRMED (``trust='confirmed'``) on the
  ingest/migration path — see ``auto_confirm`` (``store.create_*`` keeps its
  ``trust='proposed'`` default for the general model + existing tests).
- A task carries three autonomy fields (columns on ``task``): ``assignee``
  (unassigned|ai|human), ``risk`` (low|high|NULL), ``approval``
  (none|needed|approved|rejected). Workflow ``status`` is
  ``backlog|in_progress|done``.

Lifecycle transitions (this module):

- ``assign_human``  → a person owns it; nothing auto-executes.
- ``assign_ai``     → the swarm owns it; LOW risk auto-starts, HIGH risk parks in
  backlog needing approval (and pings the board owner).
- ``approve`` / ``reject`` → the human gate on a HIGH-risk AI task.
- ``move``          → board drag between the three workflow columns.

The risk call is a cheap keyword HEURISTIC by default, with a pluggable
``assessor`` (e.g. an LLM) as an override. Tests use the heuristic / a fake — no
network, no LLM, no real Telegram.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import store

log = logging.getLogger(__name__)

# The valid task workflow statuses: the board's three live columns plus the
# terminal 'archived' filing bucket (kept off the board — see archive/unarchive).
STATUSES = ("backlog", "in_progress", "done", "archived")

# Why a task was archived (filed away). 'not_relevant' = won't do; 'done' =
# already done, then filed away.
ARCHIVE_REASONS = ("not_relevant", "done")

# High-risk keywords (RU + EN), matched case-insensitively as substrings of the
# task label. Each implies an OUTWARD / IRREVERSIBLE / COSTLY / DESTRUCTIVE act:
# send/write-out, deploy/prod, merge, delete, pay/spend/buy, publish.
_HIGH_RISK_KEYWORDS = (
    # RU
    "отправ", "напиш", "деплой", "задеплой", "прод", "продакшн",
    "слить", "влить", "удал", "оплат", "заплат", "купи", "потрат",
    "опубликов", "выложи",
    # EN
    "merge", "spend", "publish", "deploy", "delete", "pay",
)

_TG_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


# --- risk ------------------------------------------------------------------
def _label_of(task: Any) -> str:
    """The label of a task (a store.Entity, or a plain dict/str)."""
    if isinstance(task, str):
        return task
    label = getattr(task, "label", None)
    if label is None and isinstance(task, dict):
        label = task.get("label")
    return label or ""


def assess_risk(task: Any, assessor: Optional[Callable[[Any], str]] = None) -> str:
    """Classify a task's execution risk → ``'low'`` | ``'high'``.

    Default is a HEURISTIC: HIGH if the label implies an outward / irreversible /
    costly / destructive act (see ``_HIGH_RISK_KEYWORDS``), else LOW. A pluggable
    ``assessor`` callable (e.g. an LLM) OVERRIDES the heuristic when supplied; its
    return is normalised so only an explicit ``'high'`` means high.
    """
    if assessor is not None:
        return "high" if assessor(task) == "high" else "low"
    low = _label_of(task).lower()
    return "high" if any(kw in low for kw in _HIGH_RISK_KEYWORDS) else "low"


# --- assignment / approval / board moves -----------------------------------
def assign_human(conn, task_id: str) -> store.Entity:
    """A person owns the task: ``assignee='human'``, parked in backlog, no gate."""
    return store.update_payload(
        conn, task_id, assignee="human", status="backlog", approval="none")


def assign_ai(conn, task_id: str,
              tg_notify: Optional[Callable[[Any], Any]] = None) -> dict:
    """Hand a task to the AI swarm and gate its EXECUTION by risk.

    LOW risk  → auto-start: ``status='in_progress'``, ``approval='none'``.
    HIGH risk → hold for the human gate: ``approval='needed'``, stays in
    ``backlog``, and (if ``tg_notify`` is given) ping the board owner.

    Returns ``{risk, auto_started, needs_approval}``.
    """
    task = store.get(conn, task_id)
    if task is None:
        raise KeyError(task_id)
    risk = assess_risk(task)
    if risk == "high":
        store.update_payload(conn, task_id, assignee="ai", risk="high",
                             approval="needed", status="backlog")
        if tg_notify is not None:
            tg_notify(store.get(conn, task_id))
        return {"risk": risk, "auto_started": False, "needs_approval": True}
    store.update_payload(conn, task_id, assignee="ai", risk="low",
                         approval="none", status="in_progress")
    return {"risk": risk, "auto_started": True, "needs_approval": False}


def _decide(conn, task_id: str, **fields: Any) -> store.Entity:
    """Apply a human decision ONLY to a task that is actually awaiting one.

    IDEMPOTENT BY CONSTRUCTION. These transitions are driven by BUTTONS on the
    approval centre, and a button is clicked twice, clicked from a tab that has
    been open for an hour, and clicked by a retrying browser. Without this guard
    a second Approve re-drags a finished task back to ``in_progress``, and a
    stale tab silently overturns a decision the owner made after that tab was
    rendered — the tap would not be recording a decision, it would be replaying
    whatever the page happened to be showing.

    A task not in ``approval='needed'`` is returned UNCHANGED rather than
    raising: re-clicking is not an error, it is the same decision arriving
    twice, and the caller can see the current state in the entity it gets back.
    """
    t = store.get(conn, task_id)
    if t is None or (t.get("approval") or "none") != "needed":
        return t
    return store.update_payload(conn, task_id, **fields)


def approve(conn, task_id: str) -> store.Entity:
    """Human approves a HIGH-risk AI task → it starts executing."""
    return _decide(conn, task_id, approval="approved", status="in_progress")


def reject(conn, task_id: str) -> store.Entity:
    """Human rejects a HIGH-risk AI task → back to backlog, NOT executed.

    ``rejected`` is a THIRD state, never a return to ``none``. A rejected task
    that read as undecided would be picked up by the very next heartbeat and the
    swarm would do the thing the human just refused — see
    :func:`heartbeat.pick_next`, which excludes it, and
    :func:`heartbeat.stalled_tasks`, which surfaces it for a custodial turn so
    the rejection is acted on instead of quietly rotting in the backlog.
    """
    return _decide(conn, task_id, approval="rejected", status="backlog")


def move(conn, task_id: str, status: str) -> store.Entity:
    """Board drag: set the workflow ``status`` (validated against the enum)."""
    if status not in STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {STATUSES}")
    return store.update_payload(conn, task_id, status=status)


def archive(conn, task_id: str, reason: str) -> store.Entity:
    """File a task away off the board: ``status='archived'`` + ``archive_reason``.

    ``reason`` is one of ``'not_relevant'`` (won't do) or ``'done'`` (it was
    already done, then filed away). Archived tasks are excluded from the board
    columns; use ``unarchive`` to bring one back to backlog.
    """
    if reason not in ARCHIVE_REASONS:
        raise ValueError(
            f"invalid archive reason {reason!r}; expected one of {ARCHIVE_REASONS}")
    return store.update_payload(
        conn, task_id, status="archived", archive_reason=reason)


def unarchive(conn, task_id: str) -> store.Entity:
    """Restore an archived task to the board: ``status='backlog'``, clear reason."""
    return store.update_payload(
        conn, task_id, status="backlog", archive_reason=None)


# --- auto-confirm (ingest / migration) -------------------------------------
def auto_confirm(conn, eid: str) -> None:
    """Promote a freshly ingested/migrated entity out of the proposed limbo.

    Existence is never human-gated, so artifacts/events/tasks become
    ``confirmed`` on this system's ingest/migration path. An explicitly
    ``rejected`` entity is left alone (a human decline still means something).
    For tasks, also normalise onto the board: any non-board status (e.g. the
    retired ``'todo'``) → ``'backlog'`` and ensure ``assignee='unassigned'``.
    """
    e = store.get(conn, eid)
    if e is None or e.trust == "rejected":
        return
    if e.trust != "confirmed":
        store.set_trust(conn, eid, "confirmed")
    if e.type == "task":
        upd: dict[str, Any] = {}
        if e.get("status") not in STATUSES:
            upd["status"] = "backlog"
        if not e.get("assignee"):
            upd["assignee"] = "unassigned"
        if upd:
            store.update_payload(conn, eid, **upd)


def auto_confirm_all(conn, ids) -> None:
    """``auto_confirm`` a batch of entity ids (ingest reconcile output)."""
    for eid in ids:
        auto_confirm(conn, eid)


# --- HIGH-risk approval ping -----------------------------------------------
# Org/host specifics (the fallback owner chat, the TG egress proxy) are NOT
# hardcoded here anymore — they come from config (config/org.toml), so the core
# is org-agnostic. See config.Config.org_owner_channel / .tg_egress_proxy, whose
# defaults preserve TODAY's swarmdev values.
def _owner_channel(cfg: Any) -> Optional[str]:
    """The config fallback approval-ping target (the org owner's TG chat)."""
    return getattr(cfg, "org_owner_channel", None)


def _egress_proxy(cfg: Any) -> Optional[str]:
    """The config HTTP proxy for TG bot sends (routes past the DPI block)."""
    return getattr(cfg, "tg_egress_proxy", None)


def _egress_note(cfg: Any) -> str:
    """WHICH KIND of failure the caller is looking at — a proxy that did not
    answer, or no proxy at all.

    This whole path is guarded to never raise, which is right (an approval ping
    must not take a lifecycle transition down with it) and is exactly why the log
    line has to carry the diagnosis: a suppressed failure that does not say what
    was tried is a silence with a timestamp. Until 2026-07-24 an absent
    [network].tg_egress_proxy fell back to a hardcoded container IP that had
    rotated away, so this warning fired with a transport error that read as "the
    proxy is down" when the truth was "there is no proxy and nobody said so"
    (coord-computation). TgClient._post already says this at its own send site;
    the approval ping is the one egress path that did not.
    """
    proxy = _egress_proxy(cfg)
    return (f"via proxy {proxy}" if proxy else
            "DIRECT — no [network].tg_egress_proxy in config/org.toml, and direct "
            "egress is blocked on this host, so no ping from this process can land")


def _swarmdev_token(cfg: Any) -> Optional[str]:
    """The swarmdev bot token from cfg (``[telegram_bots].swarmdev``)."""
    bots = getattr(cfg, "telegram_bots", None)
    if isinstance(bots, dict) and bots.get("swarmdev"):
        return bots["swarmdev"]
    fn = getattr(cfg, "bot_token_for", None)
    if callable(fn):
        try:
            return fn("swarmdev")
        except Exception:  # noqa: BLE001
            return None
    return None


def _tg_chat_id(channel: Any) -> Optional[str]:
    """Return a telegram chat id (numeric string) from a worker ``channel``.

    Accepts a bare numeric id (``"123456789"`` / negative group ids ``"-100…"``)
    or a ``telegram:``/``tg:`` prefixed one. Anything else (``@handle``, an email,
    None) → ``None`` (not a routable telegram chat id).
    """
    if channel is None:
        return None
    s = str(channel).strip()
    for pre in ("telegram:", "tg:"):
        if s.lower().startswith(pre):
            s = s[len(pre):].strip()
            break
    if s and (s.lstrip("-").isdigit()):
        return s
    return None


def _send_tg_message(cfg: Any, token: str, chat_id: str, text: str) -> bool:
    """POST one ``sendMessage`` through the configured TG egress proxy. Raises on
    transport/API error (callers guard). Returns the API ``ok`` flag."""
    import httpx  # lazy — not available in every env
    with httpx.Client(proxy=_egress_proxy(cfg), timeout=10) as client:
        resp = client.post(
            _TG_SEND_URL.format(token=token),
            json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()
        return bool(resp.json().get("ok"))


def _approval_text(task: Any) -> str:
    label = _label_of(task) or "task"
    return f"⚠️ High-risk task needs approval: {label} — approve in the board"


def notify_tg_high_risk(cfg: Any, task: Any) -> bool:
    """Ping the org's board owner that a HIGH-risk task needs approval.

    POSTs a ``sendMessage`` to the swarm bot API (token from
    ``[telegram_bots].swarmdev``) for the configured owner channel
    (``cfg.org_owner_channel``), routed through the configured TG egress proxy
    (``cfg.tg_egress_proxy``) like the rest of the worker's TG egress. Fully
    guarded: any failure (no token, network, API error) → ``False``; it NEVER
    raises. Tests monkeypatch this — nothing here sends for real.

    This is the CONFIG-level fallback ping. For ROLE-based routing (send to each
    active owner/admin approver's channel) use ``notify_approval`` with a conn.
    """
    try:
        token = _swarmdev_token(cfg)
        if not token:
            return False
        return _send_tg_message(
            cfg, token, _owner_channel(cfg), _approval_text(task))
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_tg_high_risk: suppressed failure (%s): %s",
                    _egress_note(cfg), exc)
        return False


def notify_approval(cfg: Any, conn: Any, task: Any) -> bool:
    """Role-based HIGH-risk approval routing.

    Looks up ``core.team.approvers(conn)`` (active workers with role owner/admin)
    and sends the approval ping to each approver whose ``channel`` looks like a
    telegram chat id (numeric). If NO approver has a telegram channel, FALLS BACK
    to the config-level owner channel (``cfg.org_owner_channel`` via the swarmdev
    bot). Fully guarded: never raises; returns ``True`` if at least one message
    was sent (``ok``).

    Tests monkeypatch this / ``notify_tg_high_risk`` — nothing sends for real.
    """
    try:
        token = _swarmdev_token(cfg)
        if not token:
            return False
        text = _approval_text(task)
        chat_ids: list[str] = []
        try:
            from . import team  # local import — avoids a hard import cycle
            for w in team.approvers(conn):
                cid = _tg_chat_id(w.get("channel"))
                if cid and cid not in chat_ids:
                    chat_ids.append(cid)
        except Exception as exc:  # noqa: BLE001 — degrade to the fallback chat
            log.warning("notify_approval: approver lookup failed: %s", exc)
        if not chat_ids:
            owner = _owner_channel(cfg)  # fall back to the configured owner chat
            if owner:
                chat_ids = [str(owner)]
        sent_any = False
        for cid in chat_ids:
            try:
                if _send_tg_message(cfg, token, cid, text):
                    sent_any = True
            except Exception as exc:  # noqa: BLE001 — one bad channel isn't fatal
                log.warning("notify_approval: send to %s failed (%s): %s",
                            cid, _egress_note(cfg), exc)
        return sent_any
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_approval: suppressed failure (%s): %s",
                    _egress_note(cfg), exc)
        return False
