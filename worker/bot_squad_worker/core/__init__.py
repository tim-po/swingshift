"""Product core — the stable, domain-agnostic AI-management kernel.

Design: ``claude-memory/product-core/``. Built to be built *upon*, not changed.

Layers:
- ``store``              — the data layer: metadata envelope (CTI) + 4 entities +
                           first-class tags + the edge graph, over SQLite.
- ``context_processor``  — intake -> extract -> reconcile (new/update/garbage).
- ``task_manager`` /
  ``event_manager``      — the goal-driven engines (assign/ask, remind/reschedule).
- ``autonomy``           — per-action-type stakes gate (confirm vs auto).
- ``librarian``          — dedup / consolidate / per-tag meta-artifacts.
- ``migrate``            — import the live chat-tracking db onto the core.
- ``inspect``            — read-only review snapshot of the whole system.
- ``system.CoreSystem``  — the heartbeat loop, wired into one facade.
"""
from . import (  # noqa: F401
    autonomy,
    context_processor,
    event_manager,
    inspect,
    librarian,
    migrate,
    store,
    task_manager,
)
from .system import CoreSystem  # noqa: F401

__all__ = [
    "store", "context_processor", "task_manager", "event_manager",
    "autonomy", "librarian", "migrate", "inspect", "CoreSystem",
]
