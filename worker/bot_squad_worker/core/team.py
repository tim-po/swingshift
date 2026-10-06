"""Team / roster — the MANUAL Worker role model (multi-user foundation).

Workers are a CURATED roster: humans (and AI collaborators) are onboarded BY HAND
with a role, NOT auto-derived from contacts (see product-core OPEN-QUESTIONS Q7 —
"Human workers = added by hand into a role model"). This module is the thin
onboarding + roster + role layer over ``core.store``'s worker entity.

The role model underpins role-based approval routing: high-risk approvals go to
the ``approvers`` (active owners/admins), not to one hardcoded person — a step
toward a generic / multi-tenant system.

House style matches the rest of core: every function takes an explicit
``sqlite3.Connection``; workers are CONFIRMED on onboarding (a known collaborator,
not a proposed entity awaiting a trust gate).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import store
from .store import Entity

# The fixed vocabularies for the role model.
KINDS = ("human", "ai")
ROLES = ("owner", "admin", "member")
# Roles that high-risk approvals route to.
APPROVER_ROLES = ("owner", "admin")


def _validate(kind: str, role: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown worker kind: {kind!r}; expected one of {KINDS}")
    if role not in ROLES:
        raise ValueError(f"unknown worker role: {role!r}; expected one of {ROLES}")


def onboard_worker(conn: sqlite3.Connection, name: str, *, kind: str = "human",
                   role: str = "member", capabilities: Optional[str] = None,
                   channel: Optional[str] = None) -> Entity:
    """Onboard a worker BY HAND into the roster — a CONFIRMED role-model entity.

    Validates ``kind`` (human|ai) and ``role`` (owner|admin|member), then creates
    a ``trust='confirmed'`` worker with ``active=1``. ``name`` becomes the
    entity label. Keeps the existing capabilities/channel fields.
    """
    _validate(kind, role)
    return store.create_worker(
        conn, label=name, kind=kind, role=role, active=1,
        capabilities=capabilities, channel=channel, trust="confirmed")


def list_workers(conn: sqlite3.Connection, *, active_only: bool = False) -> list[Entity]:
    """The roster (all workers, oldest first). ``active_only`` drops deactivated."""
    workers = store.by_type(conn, "worker")
    if active_only:
        workers = [w for w in workers if int(w.get("active") or 0) == 1]
    return workers


def set_role(conn: sqlite3.Connection, worker_id: str, role: str) -> Entity:
    """Change a worker's role (validated against the enum)."""
    if role not in ROLES:
        raise ValueError(f"unknown worker role: {role!r}; expected one of {ROLES}")
    return store.update_payload(conn, worker_id, role=role)


def set_active(conn: sqlite3.Connection, worker_id: str, active: bool) -> Entity:
    """Activate / deactivate a worker (a soft on/off; the row is kept)."""
    return store.update_payload(conn, worker_id, active=1 if active else 0)


def remove_worker(conn: sqlite3.Connection, worker_id: str) -> None:
    """Delete a worker from the roster (payload/tags/edges cascade)."""
    store.delete(conn, worker_id)


def approvers(conn: sqlite3.Connection) -> list[Entity]:
    """Active workers who can APPROVE high-risk work: role in {owner, admin}.

    These are the people high-risk approvals route to. Humans are preferred
    (listed first) but any active owner/admin is included.
    """
    active = list_workers(conn, active_only=True)
    appr = [w for w in active if (w.get("role") or "member") in APPROVER_ROLES]
    # prefer humans, but include any owner/admin (stable within each group).
    appr.sort(key=lambda w: 0 if (w.get("kind") or "human") == "human" else 1)
    return appr
