"""Event Manager — maximize successfully ATTENDED Events (product-core/SYSTEMS.md).

Goal: get workers to actually show up. Actions:
  - **remind** of an upcoming event,
  - **reschedule** an event (move its start/end),
  - **record attendance** (close the loop: ``attended`` | ``missed``), which feeds
    future reminders + worker patterns.

In the personal-life instance, "attend" is realized as the iCloud Accept/Decline
invite + PARTSTAT poll-sync via ``bot_squad_worker.icloud_calendar``. This module
NEVER calls iCloud/network itself — it only produces intents and updates entities;
an adapter realizes them. (See ``ICLOUD_REALIZES_ATTEND`` below.)

Only the 3 workflow statuses exist (``scheduled``/``attended``/``missed``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import store
from .store import Entity

# Where the outward "attend" act would be realized (NOT imported/called here, so
# tests never touch iCloud/network). An adapter wires this up.
ICLOUD_REALIZES_ATTEND = "bot_squad_worker.icloud_calendar"


def _parse(dt: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string; naive values are assumed UTC."""
    if not dt:
        return None
    try:
        parsed = datetime.fromisoformat(dt)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def upcoming(conn, within_hours: int = 48,
             now: Optional[datetime] = None) -> list[Entity]:
    """Confirmed events whose ``start_dt`` falls in ``(now, now+within_hours]``.

    Past events and events beyond the window are excluded; ordered soonest-first.
    ``now`` is injectable for deterministic tests (defaults to wall-clock UTC).
    """
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    horizon = ref.timestamp() + within_hours * 3600
    out: list[tuple[float, Entity]] = []
    for e in store.by_type(conn, "event", trust="confirmed"):
        if e.get("status") != "scheduled":
            continue
        start = _parse(e.get("start_dt"))
        if start is None:
            continue
        ts = start.timestamp()
        if ref.timestamp() < ts <= horizon:
            out.append((ts, e))
    out.sort(key=lambda pair: pair[0])
    return [e for _, e in out]


def remind(conn, event_id: str) -> dict:
    """Build a reminder intent for an event (does NOT send anything).

    Returns an action descriptor a caller/adapter can realize over the channels
    of the workers attending the event (``attends`` edges).
    """
    event = store.get(conn, event_id)
    if event is None:
        raise KeyError(event_id)
    attendees = store.edges_in(conn, event_id, "attends")
    channels = []
    for wid in attendees:
        w = store.get(conn, wid)
        if w is not None:
            channels.append({"worker_id": wid, "channel": w.get("channel")})
    return {
        "action": "remind",
        "event_id": event_id,
        "label": event.label,
        "start_dt": event.get("start_dt"),
        "location": event.get("location"),
        "attendees": channels,
    }


def reschedule(conn, event_id: str, new_start: str,
               new_end: Optional[str] = None) -> Entity:
    """Move an event's start (and optionally end). Returns the updated entity."""
    fields: dict[str, str] = {"start_dt": new_start}
    if new_end is not None:
        fields["end_dt"] = new_end
    return store.update_payload(conn, event_id, **fields)


def record_attendance(conn, event_id: str, attended: bool) -> Entity:
    """Close the loop: set event status to ``attended`` or ``missed``."""
    status = "attended" if attended else "missed"
    return store.update_payload(conn, event_id, status=status)
