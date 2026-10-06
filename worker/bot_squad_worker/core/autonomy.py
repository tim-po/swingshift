"""Autonomy gate — per ACTION-TYPE stakes (design: product-core/AUTONOMY.md).

The confirm gate keys off the **action type's stakes** (how outward-facing /
irreversible the act is), **not** primarily who is doing it. Every action type
carries a fixed autonomy level:

- ``auto``        — execute without asking (low stakes, reversible, internal).
- ``confirm``     — propose and wait for an explicit human OK before executing.
- ``human_only``  — never auto; a human must perform or explicitly direct it.

Worker/channel trust may only **TIGHTEN**, never loosen: a low-trust worker can
force a ``confirm`` even on an otherwise ``auto`` action, but a high-trust worker
can NEVER drop a ``confirm``/``human_only`` action down to auto. This keeps the
stakes of the *act* as the floor of the safety model.
"""
from __future__ import annotations

from typing import Optional

# action_type -> 'auto' | 'confirm' | 'human_only'
#
# High stakes (outward / irreversible) => confirm-first.
# Low stakes (internal / reversible)   => auto.
STAKES: dict[str, str] = {
    # --- confirm-first: outward-facing / hard to reverse -------------------
    "send_message_as_person": "confirm",   # e.g. send a chat/DM as a human
    "send_email": "confirm",
    "spend": "confirm",                     # spend money / purchase
    "deploy_prod": "confirm",               # deploy to prod / merge to main
    "delete": "confirm",                    # delete data
    "remind_human": "confirm",              # a subtle nudge still touches a person
    # --- auto: internal / reversible --------------------------------------
    "assign_ai_worker": "auto",             # dispatch an internal AI worker
    "draft": "auto",                        # compose but don't send
    "create_proposed": "auto",              # create a proposed entity / tag
    "research": "auto",                     # web search / read context
    "edit_dev_branch": "auto",              # edit code on a dev branch
}

# Fail-safe: an action type we have never classified is treated as high stakes.
DEFAULT_LEVEL = "confirm"

# Worker trust below this floor TIGHTENS an otherwise-auto action to confirm.
# (Only the store's "confirmed" trust counts as trusted; proposed/rejected do
# not.) Trust can never loosen a confirm/human_only action.
_TRUSTED = "confirmed"


def level_for(action_type: str) -> str:
    """The fixed autonomy level for an action type (fail-safe to confirm)."""
    return STAKES.get(action_type, DEFAULT_LEVEL)


def requires_confirmation(action_type: str,
                          worker_trust: Optional[str] = None) -> bool:
    """Does this action need an explicit human OK before executing?

    Returns ``True`` for ``confirm`` and ``human_only`` levels.

    ``worker_trust`` may only **tighten** the gate, never loosen it: a
    non-confirmed (low-trust) worker forces confirmation even on an ``auto``
    action. A trusted worker can never relax a confirm/human_only action.
    """
    level = level_for(action_type)
    if level in ("confirm", "human_only"):
        return True  # stakes floor: trust can never loosen this
    # level == 'auto' here: low-trust worker tightens it to confirm.
    if worker_trust is not None and worker_trust != _TRUSTED:
        return True
    return False
