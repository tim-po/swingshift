"""Device Registry — the ONE unified read-model over boxes, sessions and the
dial-out origin-agent (DEVICE-REGISTRY-SPEC Phase 2).

The objective: *any* frontend reads a single device list and routes to a device
THROUGH the hub, with no tunnels and no CLI reachability puzzle. Three registries
already exist and each has its own shape:

* **origins** (:mod:`mcp_loops.origins`) — the boxes/mirrors visible from this
  hub, plus the LOCAL origin (the box the hub runs on, which the dial-out
  origin-agent enrolls as a device).
* **sessions** (:mod:`mcp_loops.sessions`) — the live connectors/collaborators
  that execute dispatched tasks on the user's own compute.

This module folds those into ONE ``Device`` record::

    {id, owner, kind: box|session|origin, name, capabilities,
     address_free, last_seen, live, channel_ref}

It is **pure and READ-ONLY** — it takes the records the callers already read
(``loop_origin_list`` output + a ``build_roster`` result) and reshapes them. It
mutates nothing, never raises on malformed rows, and — the honesty mandate —
``live`` comes STRICTLY from the source record, never inferred. ``owner`` is
the record's own owner, defaulting (read-time) to the single pilot owner, so
``?owner=`` scoping (Phase 5) is a real filter, not a reshape. ``address_free`` is a *label*, never a routable
endpoint: routing always goes through the hub's outbound channel.
"""
from __future__ import annotations

import os
from typing import Optional

# Default owner until real multi-tenant lands (Phase 5). A single-tenant hub owns
# every device it sees; the field exists so ``?owner=`` filtering is a no-reshape
# seam. Override with LOOPYARD_PILOT_OWNER on a differently-named pilot.
DEFAULT_OWNER = os.environ.get("LOOPYARD_PILOT_OWNER", "local")


def _cap_names(caps: object) -> list[str]:
    """Normalise a capability list to plain CLI-name strings — the frontend
    contract is ``string[]``. Origins may inline caps either as bare names
    (``["claude", "codex"]``) or as the engine's rich probe records
    (``{"cli": "claude", "authed": false, …}``); a session declares plain names.
    Drop anything nameless. Never raises."""
    out: list[str] = []
    for c in caps or []:
        if isinstance(c, str):
            name = c
        elif isinstance(c, dict):
            name = c.get("cli") or c.get("name") or c.get("id")
        else:
            name = None
        if name:
            out.append(str(name))
    return out


def _origin_device(o: dict, *, owner: str) -> Optional[dict]:
    """One origin record → one unified device, or ``None`` for a row that is not a
    box/origin.

    The LOCAL origin is the box the hub runs on — the one the dial-out
    origin-agent enrolls — so it reads as kind ``origin`` (the "dial-out
    device"). A ``mirror`` origin is a ``box``. Any other origin ``kind`` (notably
    ``connected-session`` — a session that also appears as an origin) is SKIPPED
    here: sessions are folded from the authoritative Sessions roster, so counting
    them again as boxes would double-list one device. ``address_free`` is a human
    label only: the origin-agent is reached via the hub's channel, a mirror by its
    recorded address or sync path — never dialled directly.
    """
    oid = o.get("id")
    if not oid:
        return None
    okind = o.get("kind")
    if okind in ("local", "origin"):
        # "origin" = a live-enrolled dial-out device with no mirror on disk yet
        # (loop_origin_list appends it) — the same dial-out device kind.
        kind, address_free = "origin", "dial-out · via hub"
    elif okind == "mirror":
        kind = "box"
        address_free = o.get("address") or o.get("path") or None
    else:
        return None                       # connected-session/unknown → not a box
    return {
        "id": f"{kind}:{oid}",
        # Phase 5: the record's own (P3 enrolled) owner; an owner-less record
        # falls back to the hub default — same rule as enrollment.device_owner.
        "owner": str(o.get("owner") or owner),
        "kind": kind,
        "name": o.get("name") or str(oid),
        "capabilities": _cap_names(o.get("capabilities")),
        "address_free": address_free,
        "last_seen": o.get("last_seen"),
        # honest: strictly the record's reachability, never a guess.
        "live": bool(o.get("reachable")),
        "channel_ref": None,
    }


def _session_device(s: dict, *, owner: str) -> Optional[dict]:
    """One Sessions-roster card → one unified device, or ``None`` for a junk row."""
    sid = s.get("id")
    if not sid:
        return None
    hb = s.get("heartbeat") if isinstance(s.get("heartbeat"), dict) else {}
    origin = s.get("origin") if isinstance(s.get("origin"), dict) else None
    address_free: Optional[str] = None
    if origin:
        parts = [origin.get("host"), origin.get("cwd")]
        address_free = " · ".join(str(p) for p in parts if p) or None
    return {
        "id": f"session:{sid}",
        "owner": str(s.get("owner") or owner),
        "kind": "session",
        "name": str(sid),
        "capabilities": _cap_names(s.get("capabilities")),
        "address_free": address_free,
        "last_seen": hb.get("lastSeen"),
        "live": bool(hb.get("live")),
        # a session's task channel is the connect store, not a held hub channel.
        "channel_ref": None,
    }


def build_devices(origins: Optional[list], sessions_roster: Optional[dict], *,
                  owner: str = DEFAULT_OWNER) -> list[dict]:
    """Fold origins + the sessions roster into the unified device list.

    ``origins`` is the ``loop_origin_list`` ``origins`` array; ``sessions_roster``
    is a ``build_roster`` result (``{sessions, ...}``). Pure; tolerant of malformed
    rows and of either input being ``None``. Order: boxes/origins (as the source
    ordered them, local first) then sessions."""
    out: list[dict] = []
    for o in (origins or []):
        if isinstance(o, dict):
            dev = _origin_device(o, owner=owner)
            if dev is not None:
                out.append(dev)
    roster = sessions_roster if isinstance(sessions_roster, dict) else {}
    for s in (roster.get("sessions") or []):
        if isinstance(s, dict):
            dev = _session_device(s, owner=owner)
            if dev is not None:
                out.append(dev)
    return out


def devices_response(origins: Optional[list], sessions_roster: Optional[dict], *,
                     owner_filter: Optional[str] = None,
                     owner: str = DEFAULT_OWNER,
                     error: Optional[str] = None) -> dict:
    """The ``GET /api/loops/devices`` read model — the ONE list any frontend
    renders. Builds the unified list, applies the optional ``owner_filter``
    (Phase 5 seam), and reports honest ``count``/``live`` totals over the
    *returned* rows. ``error`` is surfaced verbatim in the empty-state (fail-soft:
    an upstream outage yields an empty roster + a note, never a 500)."""
    devices = build_devices(origins, sessions_roster, owner=owner)
    if owner_filter:
        devices = [d for d in devices if d.get("owner") == owner_filter]
    resp: dict = {
        "devices": devices,
        "owner": owner_filter,
        "count": len(devices),
        "live": sum(1 for d in devices if d.get("live")),
    }
    if error:
        resp["error"] = error
    return resp
