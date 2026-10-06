"""iCloud calendar access over CalDAV (read existing events + write swarm-detected
meetings into a dedicated "Swarm" calendar).

Credentials come from ``config/secrets.toml`` ``[icloud]`` (apple_id + an
APP-SPECIFIC password — never the real Apple password; revocable from
appleid.apple.com). iCloud is on the direct route (not the DPI-blocked Telegram
path), so we strip any proxy env around the CalDAV calls for a clean direct
connection even when the worker is otherwise proxying Telegram egress.

Posture: personal calendar data stays on-host / in-context; never written to
shared memory. Writes target ONLY the dedicated "Swarm" calendar.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import os
import tomllib
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CALDAV_URL = "https://caldav.icloud.com/"
SWARM_CALENDAR_NAME = "Swarm"
_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy")


@contextlib.contextmanager
def _no_proxy():
    """Temporarily clear proxy env so CalDAV reaches iCloud on the direct route."""
    saved = {k: os.environ.pop(k, None) for k in _PROXY_ENV}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _creds(config_dir: Path) -> Optional[dict]:
    p = Path(config_dir) / "secrets.toml"
    if not p.exists():
        return None
    ic = tomllib.loads(p.read_text()).get("icloud") or {}
    if ic.get("apple_id") and ic.get("app_password"):
        return ic
    return None


def is_configured(config_dir: Path) -> bool:
    return _creds(config_dir) is not None


def _principal(config_dir: Path):
    import caldav
    ic = _creds(config_dir)
    if not ic:
        raise RuntimeError("iCloud not configured: secrets.toml [icloud] apple_id/app_password")
    client = caldav.DAVClient(url=CALDAV_URL, username=ic["apple_id"],
                              password=ic["app_password"])
    return client.principal()


def read_events(config_dir: Path, days_back: int = 1, days_forward: int = 30,
                limit: int = 200) -> list[dict]:
    """Return events across all calendars in the window, soonest first.

    Each event: {title, start, end, location, calendar, all_day}.
    """
    out: list[dict] = []
    with _no_proxy():
        principal = _principal(config_dir)
        start = _dt.datetime.now() - _dt.timedelta(days=days_back)
        end = _dt.datetime.now() + _dt.timedelta(days=days_forward)
        for cal in principal.calendars():
            try:
                name = cal.get_display_name() or "?"
            except Exception:  # noqa: BLE001
                name = "?"
            try:
                evs = cal.search(start=start, end=end, event=True, expand=True)
            except Exception:  # noqa: BLE001 — reminders lists etc. reject event search
                continue
            for e in evs:
                try:
                    v = e.icalendar_component
                    ds = v.get("dtstart")
                    de = v.get("dtend")
                    dval = ds.dt if ds else None
                    all_day = not hasattr(dval, "hour")
                    out.append({
                        "title": str(v.get("summary", "(no title)")),
                        "start": _iso(dval),
                        "end": _iso(de.dt if de else None),
                        "location": str(v.get("location", "")) or None,
                        "calendar": name,
                        "all_day": all_day,
                    })
                except Exception:  # noqa: BLE001
                    continue
    out.sort(key=lambda x: x["start"] or "")
    return out[:limit]


def _iso(d: Any) -> Optional[str]:
    if d is None:
        return None
    try:
        return d.isoformat()
    except Exception:  # noqa: BLE001
        return str(d)


def ensure_swarm_calendar(config_dir: Path):
    """Find or create the dedicated 'Swarm' calendar; return the calendar object.
    Caller should already be inside _no_proxy() (the write helpers handle that)."""
    principal = _principal(config_dir)
    for c in principal.calendars():
        try:
            if (c.get_display_name() or "").strip().lower() == SWARM_CALENDAR_NAME.lower():
                return c
        except Exception:  # noqa: BLE001
            continue
    return principal.make_calendar(name=SWARM_CALENDAR_NAME)


def add_meeting(config_dir: Path, title: str, start: str, end: Optional[str] = None,
                description: Optional[str] = None, location: Optional[str] = None) -> str:
    """Create an event in the Swarm calendar. ``start``/``end`` are ISO8601.
    Returns the event UID (store it to allow later delete/update)."""
    with _no_proxy():
        cal = ensure_swarm_calendar(config_dir)
        s = _parse(start)
        e = _parse(end) if end else (s + _dt.timedelta(hours=1))
        uid = str(uuid.uuid4())
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//bot-swarm//calendar//EN",
                 "BEGIN:VEVENT", f"UID:{uid}",
                 f"SUMMARY:{_esc(title)}",
                 f"DTSTART:{s.strftime('%Y%m%dT%H%M%S')}",
                 f"DTEND:{e.strftime('%Y%m%dT%H%M%S')}"]
        if location:
            lines.append(f"LOCATION:{_esc(location)}")
        if description:
            lines.append(f"DESCRIPTION:{_esc(description)}")
        lines += ["END:VEVENT", "END:VCALENDAR"]
        cal.save_event("\n".join(lines) + "\n")
        log.info("icloud_calendar: added Swarm event %s (%s)", title[:40], uid)
        return uid


ORGANIZER_CN = "Swarm"
ORGANIZER_MAILTO = "mailto:swarm@botswarm.local"


def add_meeting_invite(config_dir: Path, title: str, start: str,
                       end: Optional[str] = None, description: Optional[str] = None,
                       location: Optional[str] = None) -> str:
    """Create a VEVENT in the Swarm calendar as a CalDAV INVITATION.

    Writes ``METHOD:REQUEST`` with an ``ORGANIZER`` (the swarm) and a single
    ``ATTENDEE`` (Tim, ``PARTSTAT=NEEDS-ACTION;RSVP=TRUE``) so Apple Calendar
    surfaces Accept/Decline buttons. ``start``/``end`` are ISO8601. Returns the
    event UID — store it so the poll job can read back ``PARTSTAT`` and so the
    event can be deleted on decline. Runs inside ``_no_proxy()`` (direct route).
    """
    ic = _creds(config_dir)
    attendee = (ic.get("apple_id") if ic else None) or "tim@example.com"
    with _no_proxy():
        cal = ensure_swarm_calendar(config_dir)
        s = _parse(start)
        e = _parse(end) if end else (s + _dt.timedelta(hours=1))
        uid = str(uuid.uuid4())
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
                 "PRODID:-//bot-swarm//calendar//EN",
                 "METHOD:REQUEST",
                 "BEGIN:VEVENT", f"UID:{uid}",
                 f"DTSTAMP:{stamp}",
                 f"SUMMARY:{_esc(title)}",
                 f"DTSTART:{s.strftime('%Y%m%dT%H%M%S')}",
                 f"DTEND:{e.strftime('%Y%m%dT%H%M%S')}",
                 f"ORGANIZER;CN={ORGANIZER_CN}:{ORGANIZER_MAILTO}",
                 ("ATTENDEE;CN=Tim;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;"
                  f"ROLE=REQ-PARTICIPANT:mailto:{attendee}")]
        if location:
            lines.append(f"LOCATION:{_esc(location)}")
        if description:
            lines.append(f"DESCRIPTION:{_esc(description)}")
        lines += ["END:VEVENT", "END:VCALENDAR"]
        cal.save_event("\n".join(lines) + "\n")
        log.info("icloud_calendar: added Swarm INVITE %s (%s)", title[:40], uid)
        return uid


def _unfold(data: str) -> str:
    """Unfold iCal line continuations (CRLF/LF + leading space or tab)."""
    return data.replace("\r\n ", "").replace("\r\n\t", "").replace(
        "\n ", "").replace("\n\t", "")


def read_partstat(config_dir: Path, uid: str) -> Optional[str]:
    """Return Tim's ATTENDEE ``PARTSTAT`` for the Swarm event ``uid``.

    Finds the event by UID in the Swarm calendar and regex-matches
    ``PARTSTAT=...`` (e.g. ACCEPTED/DECLINED/NEEDS-ACTION) on the unfolded event
    data; returns None if the event (or a PARTSTAT) isn't found. Runs inside
    ``_no_proxy()``.
    """
    import re
    with _no_proxy():
        cal = ensure_swarm_calendar(config_dir)
        for ev in cal.events():
            try:
                data = ev.data or ""
            except Exception:  # noqa: BLE001
                continue
            if f"UID:{uid}" not in _unfold(data):
                continue
            m = re.search(r"PARTSTAT=([A-Z-]+)", _unfold(data))
            return m.group(1) if m else None
    return None


def _parse_ics_dt(v: str) -> _dt.datetime:
    """Parse an iCal DTSTART value (20260629T090000[Z] or all-day 20260629).

    Timezone is intentionally ignored (trailing Z stripped, TZID handled by the
    caller's regex): Swarm events are written as floating local wall time, so
    wall time is what we compare/store.
    """
    v = (v or "").strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable iCal datetime: {v!r}")


def _unesc(s: str) -> str:
    """Reverse ``_esc``: unescape iCal TEXT (\\n, \\, \\; \\\\)."""
    import re
    return re.sub(r"\\([\\,;nN])",
                  lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def _event_fields(u: str, uid: str) -> dict:
    """Extract ``{"start", "summary"}`` from an UNFOLDED VEVENT blob (shared by
    ``read_event`` / ``read_events_map``). DTSTART params (TZID/VALUE=DATE) are
    tolerated, the value is normalized to floating local ISO
    ``YYYY-MM-DDTHH:MM:SS``; SUMMARY is unescaped. ``uid`` is log context only.
    """
    import re
    start = None
    m = re.search(r"^DTSTART(?:;[^:\r\n]*)?:([0-9TZ]+)", u, re.MULTILINE)
    if m:
        try:
            start = _parse_ics_dt(m.group(1)).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            log.warning("icloud_calendar: bad DTSTART on %s: %s",
                        uid, m.group(1))
    m = re.search(r"^SUMMARY(?:;[^:\r\n]*)?:(.*)", u, re.MULTILINE)
    summary = _unesc(m.group(1).strip().rstrip("\r")) if m else None
    return {"start": start, "summary": summary}


def read_event(config_dir: Path, uid: str) -> Optional[dict]:
    """Return ``{"start": iso_str|None, "summary": str|None}`` for the Swarm
    event ``uid``, or None if the event is gone from the calendar.

    Used by callers that treat None as 'Tim deleted it', so 'gone' must mean a
    CLEAN not-found: when the uid isn't found but one or more events' data
    couldn't be fetched, the failing event may BE the target — that listing is
    inconclusive and raises RuntimeError instead of returning None. Runs inside
    ``_no_proxy()``.
    """
    data_errors = 0
    with _no_proxy():
        cal = ensure_swarm_calendar(config_dir)
        for ev in cal.events():
            try:
                data = ev.data or ""
            except Exception:  # noqa: BLE001
                data_errors += 1
                continue
            u = _unfold(data)
            if f"UID:{uid}" not in u:
                continue
            return _event_fields(u, uid)
    if data_errors:
        raise RuntimeError(
            f"read_event inconclusive for {uid}: {data_errors} event data "
            "fetch(es) failed and the uid was not found")
    return None


def read_events_map(config_dir: Path) -> dict:
    """ONE listing pass over the whole Swarm calendar.

    Returns ``{"events": {uid: {"start", "summary"}}, "data_errors": n}`` so the
    sync tick can reconcile every 'scheduled' row from a single O(events) fetch
    instead of a per-row ``cal.events()`` walk. ``data_errors`` counts events
    whose data couldn't be fetched: when non-zero, a uid missing from
    ``events`` is INCONCLUSIVE (the failed event may be it) — callers must
    skip, never treat it as deleted. Runs inside ``_no_proxy()``.
    """
    import re
    events: dict = {}
    data_errors = 0
    with _no_proxy():
        cal = ensure_swarm_calendar(config_dir)
        for ev in cal.events():
            try:
                data = ev.data or ""
            except Exception:  # noqa: BLE001
                data_errors += 1
                continue
            u = _unfold(data)
            m = re.search(r"^UID(?:;[^:\r\n]*)?:([^\r\n]+)", u, re.MULTILINE)
            if not m:
                continue
            events[m.group(1).strip()] = _event_fields(u, m.group(1).strip())
    return {"events": events, "data_errors": data_errors}


def delete_meeting(config_dir: Path, uid: str) -> bool:
    """Delete an event by UID from the Swarm calendar."""
    with _no_proxy():
        cal = ensure_swarm_calendar(config_dir)
        for e in cal.events():
            try:
                if f"UID:{uid}" in e.data:
                    e.delete()
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _parse(s: str) -> _dt.datetime:
    s = (s or "").strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    # last resort: fromisoformat
    return _dt.datetime.fromisoformat(s)


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
