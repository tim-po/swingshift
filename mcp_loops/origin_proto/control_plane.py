"""control_plane — G5: the hub side that sits on top of the secure channel.

The control plane is SEPARATE from the origins (§3). It holds NO model keys and
NO origin secrets — only PUBLIC device keys + metadata (that half is the
EnrollmentStore) plus the *live* view this module builds:

  registry       :class:`OriginRegistry` — which enrolled devices are connected
                 right now, their reported capabilities, and coarse HEALTH
                 (online / busy / stale / offline) derived from HEARTBEAT recency
                 over the live socket — NOT from rsync-mirror mtime.
  consolidation  :class:`EventConsolidator` — ingests the pushed
                 run/turn/status/result events into a PER-ORIGIN namespace and
                 renders the unified loop view FROM the event stream (again, not
                 mirror mtime). Ordered + idempotent per (origin, seq).
  router         :class:`DispatchRouter` — sends a capability-scoped ``loop.start``
                 to the TARGET origin's agent over its live channel, generating
                 the client-side dispatch-id (§7). Execution stays on the origin;
                 the router never copies work anywhere.
  fusion         :func:`unified_origins` — fuses the LIVE-enrolled origins (which
                 SUPERSEDE their mirror) with the rsync-mirror fallback
                 (:mod:`mcp_loops.origins`) for not-yet-enrolled origins, and
                 turns the ``origins.LOCAL_ORIGIN_ID`` special-case into "the
                 first enrolled origin" while keeping back-compat.

:class:`OriginHub` ties the three to an :class:`~mcp_loops.origin_proto.channel.ChannelServer`
with the right hooks, so a caller gets the whole hub in one object. This module
NEVER edits the channel/enrollment layer — it only consumes the injected hooks
(``on_authenticated`` / ``on_event``) and the public server API those layers
expose, keeping the protocol/channel ownership boundary clean.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Optional

from mcp_loops import origins as origins_mod
from mcp_loops.devices import DEFAULT_OWNER
from mcp_loops import audit as _daudit
from mcp_loops import sync_mode as _sync_mode
from mcp_loops.origin_proto import wire
from mcp_loops.origin_proto.agent_core import onboarding_actions
from mcp_loops.origin_proto.enrollment import (
    A_ONBOARDING, A_RUN, A_RUN_DENIED, A_RUN_REATTACHED, A_RUN_UNCONFIRMED,
    device_owner)

# ── health vocabulary (heartbeat-derived; §4.2/§8) ────────────────────────────
ONLINE = "online"    # live socket, heartbeat within STALE_SEC
BUSY = "busy"        # live + the origin reported a dispatch in-flight
STALE = "stale"      # live socket but no heartbeat within STALE_SEC (wedged?)
OFFLINE = "offline"  # no live socket

# A heartbeat is every ~15s (channel.HEARTBEAT_INTERVAL). Three missed beats →
# "stale". We never invent liveness we don't have: OFFLINE is authoritative from
# the socket being gone; STALE only applies while a socket is still nominally up.
HEARTBEAT_STALE_SEC = 45.0

# Slack added to a streamed/long origin.run's channel wait budget (§2.4 seam 2):
# the origin's OWN timeout should fire first (returning a proper timedOut result),
# so the hub waits a little past the run budget before treating it as a channel
# failure — never the RPC_TIMEOUT 30 s default a streamed run would blow.
RUN_WAIT_MARGIN = 10.0


# ── origin registry (live devices + capabilities + health) ────────────────────
class OriginRegistry:
    """The plane's live-origin registry. It learns of an origin from the channel
    server's ``on_authenticated`` hook (register + first-seen), keeps last-seen
    fresh from pushed events, and derives HEALTH from heartbeat recency.

    Two liveness sources, both honest:

      * with a :class:`ChannelServer` handed in (:meth:`bind_server`), the socket
        itself is authoritative — no live session ⇒ OFFLINE, regardless of clocks
        (a dropped socket is a hard fact, not a timeout guess);
      * without one (pure event-driven tests / an ingest-only plane), health is
        derived from the last-seen timestamp vs an injected clock.

    ``busy`` is set from a heartbeat's ``busy`` flag or a dispatch the router
    marks in-flight. The store is consulted so a REVOKED device is never shown
    as a healthy origin (fail-closed)."""

    def __init__(self, store: Any = None, *, now_fn: Callable[[], float] = time.time):
        self.store = store
        self._now = now_fn
        self._server: Any = None
        # device_id -> {deviceId, label, firstSeen, lastSeen, capabilities, busy}
        self._origins: dict[str, dict] = {}

    def bind_server(self, server: Any) -> None:
        """Point the registry at the live :class:`ChannelServer` so OFFLINE is
        read from the socket, not inferred from a clock."""
        self._server = server

    # ── channel hooks (wired by OriginHub) ────────────────────────────────────
    def on_authenticated(self, device_id: str, rec: dict) -> None:
        """ChannelServer ``on_authenticated`` hook: a device just completed the
        handshake. Register (or re-activate) it and stamp first/last-seen."""
        now = self._now()
        cur = self._origins.get(device_id)
        if cur is None:
            cur = {"deviceId": device_id, "firstSeen": now,
                   "capabilities": {}, "busy": False}
            self._origins[device_id] = cur
        cur["label"] = rec.get("label")
        cur["lastSeen"] = now
        cur["revoked"] = False

    def on_frame(self, device_id: str, frame: dict) -> None:
        """ChannelServer ``on_event`` hook: any pushed frame bumps last-seen and,
        for a heartbeat, updates busy. (The server only forwards EVENT frames to
        ``on_event``; :class:`OriginHub` also feeds heartbeats here so health has
        the recency signal it needs.)"""
        cur = self._origins.get(device_id)
        if cur is None:
            return
        cur["lastSeen"] = self._now()
        if isinstance(frame, dict) and frame.get("t") == wire.T_HEARTBEAT \
                and "busy" in frame:
            cur["busy"] = bool(frame.get("busy"))

    def note_seen(self, device_id: str, *, busy: Optional[bool] = None) -> None:
        """Explicitly bump last-seen (used by the heartbeat feed + pure tests)."""
        cur = self._origins.get(device_id)
        if cur is None:
            return
        cur["lastSeen"] = self._now()
        if busy is not None:
            cur["busy"] = bool(busy)

    def set_capabilities(self, device_id: str, caps: dict) -> None:
        cur = self._origins.get(device_id)
        if cur is not None:
            cur["capabilities"] = caps or {}
            cur["lastSeen"] = self._now()

    def note_revoked(self, device_id: str) -> None:
        """Mark a device revoked (fail-closed): a revoked device is never a
        healthy origin. The socket teardown is the EnrollmentStore's job; this is
        the registry-view half."""
        cur = self._origins.get(device_id)
        if cur is not None:
            cur["revoked"] = True
            cur["busy"] = False

    # ── health ────────────────────────────────────────────────────────────────
    def _is_live(self, device_id: str) -> Optional[bool]:
        if self._server is None:
            return None
        try:
            return bool(self._server.is_live(device_id))
        except Exception:  # noqa: BLE001
            return None

    def health(self, device_id: str) -> str:
        cur = self._origins.get(device_id)
        if cur is None:
            return OFFLINE
        if cur.get("revoked"):
            return OFFLINE
        if self.store is not None:
            try:
                if self.store.is_revoked(device_id):
                    return OFFLINE
            except Exception:  # noqa: BLE001
                pass
        live = self._is_live(device_id)
        if live is False:
            return OFFLINE
        if live is True:
            # a live socket is authoritative: heartbeats flow over the channel
            # (the transport keeps it up or drops it), so "up" == online. We do
            # NOT downgrade to STALE off the registry's partial event-recency
            # view — that would misreport a healthy-but-quiet origin.
            return BUSY if cur.get("busy") else ONLINE
        # pure event-driven mode (no server bound): derive from heartbeat/event
        # recency vs the injected clock.
        last = cur.get("lastSeen")
        age = (self._now() - last) if last is not None else None
        if age is None or age > HEARTBEAT_STALE_SEC * 3:
            return OFFLINE
        if age > HEARTBEAT_STALE_SEC:
            return STALE
        return BUSY if cur.get("busy") else ONLINE

    def get(self, device_id: str) -> Optional[dict]:
        cur = self._origins.get(device_id)
        if cur is None:
            return None
        return {**cur, "health": self.health(device_id)}

    def list(self) -> list[dict]:
        return [self.get(d) for d in sorted(self._origins)]

    def live_ids(self) -> list[str]:
        return [d for d in sorted(self._origins)
                if self.health(d) in (ONLINE, BUSY, STALE)]


# ── event consolidation (per-origin namespace, rendered FROM events) ──────────
class EventConsolidator:
    """Ingests pushed run/turn/status/result events into a PER-ORIGIN namespace
    and renders the unified loop view from the EVENT STREAM — replacing the
    mirror-mtime freshness the rsync aggregation used.

    Ordering + idempotency (§7): events carry a per-origin ``seq``. A frame whose
    seq was already applied for that origin is dropped (at-least-once delivery may
    redeliver on reconnect); an out-of-order higher seq is applied and recorded.
    The rendered loop state is derived, never authoritative on the plane — the
    origin's engine remains the source of truth."""

    def __init__(self):
        # origin -> {"events": [...], "loops": {name: {...}}, "seq": int}
        self._ns: dict[str, dict] = {}

    def _bucket(self, origin: str) -> dict:
        return self._ns.setdefault(
            origin, {"events": [], "loops": {}, "seq": -1, "lastTs": None})

    def ingest(self, device_id: str, frame: dict) -> bool:
        """ChannelServer ``on_event`` sink. Returns True if the event was applied,
        False if it was a duplicate/ignored. ``device_id`` is the transport id;
        the event's own ``origin`` field is the namespace key (an origin keeps its
        logical id across re-enrollment)."""
        if not isinstance(frame, dict) or frame.get("t") != wire.T_EVENT:
            return False
        try:
            wire.validate_event(frame)
        except wire.ProtocolError:
            return False
        origin = frame.get("origin") or device_id
        seq = frame["seq"]
        bucket = self._bucket(origin)
        if seq <= bucket["seq"] and any(e["seq"] == seq for e in bucket["events"]):
            return False  # already applied (redelivery) — idempotent
        bucket["events"].append({"seq": seq, "kind": frame["kind"],
                                 "ts": frame["ts"], "data": frame.get("data", {})})
        bucket["seq"] = max(bucket["seq"], seq)
        bucket["lastTs"] = max(bucket["lastTs"] or 0.0, frame["ts"])
        self._apply(bucket, frame)
        return True

    def _apply(self, bucket: dict, frame: dict) -> None:
        """Fold one event into the derived per-origin loop state."""
        data = frame.get("data", {})
        name = data.get("loop") or data.get("name")
        if not name:
            return
        loop = bucket["loops"].setdefault(name, {"name": name})
        kind = frame["kind"]
        loop["lastTs"] = frame["ts"]
        if kind == "run":
            loop["state"] = data.get("state", loop.get("state", "running"))
        elif kind == "turn":
            if data.get("turn") is not None:
                loop["turn"] = data.get("turn")
            # a real report-line 'turn' event (G2.4) also carries the agent's
            # end-of-turn status + who reported it — fold both so the live view
            # shows WHICH agent last moved and HOW, not just the turn number.
            if data.get("status"):
                loop["status"] = data.get("status")
            if data.get("agent"):
                loop["agent"] = data.get("agent")
        elif kind == "status":
            loop["status"] = data.get("status", data.get("agent"))
        elif kind == "result":
            loop["state"] = data.get("state", "done")
            loop["result"] = data.get("result", data)

    def origins(self) -> list[str]:
        return sorted(self._ns)

    def events_for(self, origin: str) -> list[dict]:
        return list(self._bucket(origin)["events"])

    def loops_for(self, origin: str) -> list[dict]:
        return [self._ns[origin]["loops"][n]
                for n in sorted(self._ns.get(origin, {}).get("loops", {}))]

    def last_seen(self, origin: str) -> Optional[float]:
        return self._ns.get(origin, {}).get("lastTs")

    def unified_loops(self) -> list[dict]:
        """Every loop across every origin, tagged with its origin — the unified
        view rendered FROM events (not mirror mtime)."""
        out: list[dict] = []
        for origin in self.origins():
            for loop in self.loops_for(origin):
                out.append({**loop, "origin": origin})
        return out


# ── S-0 live inventory (spec §7.3 a–e: loop.list polling, no new frames) ─────
# A connected origin's SAVED loops never produce an event, so the event
# consolidator alone shows them as ``loops: 0``. The hub therefore asks each
# connected origin for its inventory over the EXISTING ``loop.list`` RPC — on
# connect, on each ``run`` event and every INVENTORY_POLL_SEC — and caches the
# rows with ``fetchedAt`` so a read never waits on an origin. Kept until
# MIN_SUPPORTED=2 (S-8) as the v1-peer path and the shadow comparator's oracle.
INVENTORY_POLL_SEC = 60.0
INVENTORY_RPC_TIMEOUT = 15.0
INVENTORY_MAX_ROWS = 5000
INV_OK = "ok"          # last loop.list succeeded
INV_STALE = "stale"    # last loop.list failed; rows are the last good list
INV_HIDDEN = "hidden"  # the origin's manifest does not permit loop.list (8.9-2)
# the row facets an origin may report; anything else it sends is dropped
_INV_FIELDS = ("name", "state", "updated", "archived", "single_agent", "project")
_INV_HIDDEN_CODES = frozenset({wire.E_NOT_PERMITTED, wire.E_UNKNOWN_METHOD})


class InventoryCache:
    """Per-device cache of the last ``loop.list`` answer (§7.3 b, c).

    Fail-soft: an error after a good fetch keeps the rows and marks the entry
    ``stale`` — never ``[]``, because an emptied list is exactly how ``loops: 0``
    shows up. A manifest without ``loop.list`` is ``hidden`` (no rows, no count),
    never an empty inventory."""

    def __init__(self, *, now_fn: Callable[[], float] = time.time):
        self._now = now_fn
        # device_id -> {rows, fetchedAt, status, error}
        self._inv: dict[str, dict] = {}

    @staticmethod
    def _rows(result: Any) -> list[dict]:
        loops = result.get("loops") if isinstance(result, dict) else None
        if not isinstance(loops, list):
            raise ValueError("loop.list answer carries no loops list")
        out: list[dict] = []
        for r in loops[:INVENTORY_MAX_ROWS]:
            if not isinstance(r, dict):
                continue
            name = r.get("name")
            if not isinstance(name, str) or not name or name.startswith("_") \
                    or "/" in name or "\\" in name:
                continue
            row = {k: r[k] for k in _INV_FIELDS if k in r}
            row["state"] = row.get("state") or "saved"
            out.append(row)
        return out

    def record(self, device_id: str, result: Any) -> None:
        """A ``loop.list`` answer arrived. A malformed one counts as an error."""
        try:
            rows = self._rows(result)
        except ValueError as e:
            self.record_error(device_id, e)
            return
        self._inv[device_id] = {"rows": rows, "fetchedAt": self._now(),
                                "status": INV_OK, "error": None}

    def record_hidden(self, device_id: str, reason: str) -> None:
        self._inv[device_id] = {"rows": [], "fetchedAt": self._now(),
                                "status": INV_HIDDEN, "error": reason}

    def record_error(self, device_id: str, exc: BaseException) -> None:
        if getattr(exc, "code", None) in _INV_HIDDEN_CODES:
            self.record_hidden(device_id, str(exc))
            return
        cur = self._inv.get(device_id)
        if cur is None or cur["status"] == INV_HIDDEN:
            # never had a list: unknown, not empty (fetchedAt None)
            self._inv[device_id] = {"rows": [], "fetchedAt": None,
                                    "status": INV_STALE, "error": str(exc)}
            return
        cur["status"] = INV_STALE
        cur["error"] = str(exc)

    def mark_stale(self, device_id: str, reason: str) -> None:
        cur = self._inv.get(device_id)
        if cur is not None and cur["status"] == INV_OK:
            cur["status"] = INV_STALE
            cur["error"] = reason

    def forget(self, device_id: str) -> None:
        self._inv.pop(device_id, None)

    def get(self, device_id: str) -> Optional[dict]:
        cur = self._inv.get(device_id)
        if cur is None:
            return None
        return {**cur, "rows": [dict(r) for r in cur["rows"]]}

    def known(self, device_id: str) -> bool:
        """True iff the entry carries a real list (a fetch succeeded at least
        once), so ``len(rows)`` is a count rather than a guess."""
        cur = self._inv.get(device_id)
        return bool(cur and cur["status"] != INV_HIDDEN
                    and cur["fetchedAt"] is not None)


def origin_display_id(rec: dict) -> str:
    """The origin card key (``caps.origin or label or deviceId``)."""
    caps = rec.get("capabilities") or {}
    return caps.get("origin") or rec.get("label") or rec["deviceId"]


def live_loops(registry: "OriginRegistry", consolidator: "EventConsolidator",
               inventory: Optional[InventoryCache] = None) -> list[dict]:
    """Every loop a connected origin reports, as ``source:"live"`` rows: the
    cached ``loop.list`` inventory (§7.3) with the event stream folded over it.

    Each inventory row carries ``deviceId`` (§7.3 a), the display origin id, the
    ENROLLED owner (scope comes from the session, never the origin's own claim —
    §8.2 SC-1), ``fetchedAt`` and ``stale``. An event-derived row for the same
    ``(origin, name)`` contributes its turn/status/agent and — when its event is
    at least as new as the inventory row — its state. Event-only loops pass
    through exactly as :meth:`EventConsolidator.unified_loops` renders them."""
    events = {(l["origin"], l["name"]): l for l in consolidator.unified_loops()}
    out: dict[tuple, dict] = {}
    if inventory is not None:
        for rec in registry.list():
            inv = inventory.get(rec["deviceId"])
            if not inv or inv["status"] == INV_HIDDEN:
                continue
            origin_id = origin_display_id(rec)
            if origin_id == origins_mod.LOCAL_ORIGIN_ID:
                continue    # a remote device never speaks for this box's disk
            owner = None
            if registry.store is not None:
                try:
                    dev = registry.store.get_device(rec["deviceId"])
                    owner = device_owner(dev) if dev is not None else None
                except Exception:  # noqa: BLE001
                    owner = None
            for row in inv["rows"]:
                key = (origin_id, row["name"])
                merged = {**row, "origin": origin_id, "deviceId": rec["deviceId"],
                          "fetchedAt": inv["fetchedAt"],
                          "stale": inv["status"] == INV_STALE,
                          "inventory": True}
                if owner:
                    merged["owner"] = owner
                ev = events.pop(key, None)
                if ev is not None:
                    upd = row.get("updated")
                    ev_newer = not isinstance(upd, (int, float)) or \
                        (ev.get("lastTs") or 0) >= upd
                    for k in ("turn", "status", "agent", "result", "lastTs"):
                        if k in ev:
                            merged[k] = ev[k]
                    if ev_newer and ev.get("state"):
                        merged["state"] = ev["state"]
                out[key] = merged
    for key, ev in events.items():
        out[key] = ev
    return [out[k] for k in sorted(out)]


def hub_snapshot(hub: "OriginHub", *, mirror_root: Optional[str] = None) -> dict:
    """The consolidated ``{origins, loops}`` snapshot BOTH read paths serve —
    the in-process ``LocalOriginService`` and the hub_serve control socket —
    so the S-0 inventory reaches the dashboard either way."""
    inv = getattr(hub, "inventory", None)
    if not _sync_mode.s0_inventory_enabled():
        inv = None                       # operator rollback: pre-S-0 view
    return {"origins": unified_origins(hub.registry, hub.consolidator,
                                       mirror_root=mirror_root, inventory=inv),
            "loops": live_loops(hub.registry, hub.consolidator, inv)}


# ── dispatch router (scoped loop.start to the owning origin) ──────────────────
class DispatchRouter:
    """Routes a capability-scoped RPC to the TARGET origin's agent over its live
    channel. Execution stays on the origin — the router only sends the scoped
    call. It generates the client-side dispatch-id for ``loop.start`` (§7), so a
    redelivery re-attaches instead of double-running, and marks the origin busy
    around a start so the registry health reflects it.

    P6 owner scoping: every hub→origin call is made ON BEHALF of an owner — the
    router's ``owner`` (default the single pilot owner), or a per-call ``owner=``
    override once a shared hub threads the caller through. Before any frame is
    sent the target's enrolled ``owner`` must equal the caller's; otherwise the
    call is refused (``E_NOT_OWNER``, fail-closed)."""

    def __init__(self, server: Any, *, registry: Optional[OriginRegistry] = None,
                 id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
                 owner: str = DEFAULT_OWNER):
        self._server = server
        self._registry = registry
        self._id_fn = id_fn
        self._owner = owner
        # loop-key -> dispatch-id, so a repeat start of the same target reuses the
        # id and rides the origin's idempotency ledger instead of minting a second.
        self._dispatch_ids: dict[str, str] = {}

    def _audit(self, kind: str, **fields: Any) -> None:
        """Write one Hub-side exec-audit line via the enrollment store, fail-soft.

        The generic transport line (``A_DISPATCH``) is written by the channel on
        send; this is the EXEC-semantics record — the real argv + outcome (§5.1(3),
        §6.5) — and the ONE place a fail-closed refusal (transport gate or the
        origin's typed ``RunNotAllowed``) leaves a trail. A missing store or a
        broken audit write must never abort a dispatch, so we swallow everything."""
        store = getattr(self._server, "store", None)
        if store is None:
            return
        try:
            store.audit(kind, now=time.time(), **fields)
        except Exception:  # noqa: BLE001 — a lost audit line beats a crashed dispatch
            pass

    def _owner_gate(self, device_id: str, owner: Optional[str]) -> Optional[str]:
        """P6: return a refusal reason if ``owner`` (else the router's owner) may
        not address ``device_id``, else None. The truth is the ENROLLED record
        (``store.get_device``), never anything the origin reports about itself. A
        server with no store (pure unit fakes) has no ownership to check."""
        store = getattr(self._server, "store", None)
        if store is None:
            return None
        caller = owner or self._owner
        rec = store.get_device(device_id)
        # An unknown device is left to the channel: it can never hold a live
        # socket (authenticate fails closed on a missing record), so call_rpc
        # refuses it anyway. What P6 closes is a KNOWN device of another owner.
        if rec is not None and device_owner(rec) != caller:
            return f"device {device_id!r} is not owned by {caller!r}"
        return None

    def _ctx(self, owner: Optional[str]):
        """P3 audit context for a unit sent on behalf of ``owner`` (else the
        router's owner) — read by the channel choke point's audit record."""
        return _daudit.dispatch_context(owner=owner or self._owner)

    def _deny_unit(self, device_id: str, method: str, params: Any,
                   reason: str, *, owner: Optional[str],
                   dispatch_id: Optional[str] = None) -> None:
        """P3 audit: a unit the ROUTER refused before it reached ``call_rpc``
        (owner gate / §5.1(4) transport gate) — one ``deny`` record, fail-soft."""
        store = getattr(self._server, "store", None)
        with self._ctx(owner):
            _daudit.record_unit(getattr(store, "root", None), decision="deny",
                                target=device_id, method=method, args=params,
                                dispatch_id=dispatch_id, reason=reason)

    def _require_owner(self, device_id: str, owner: Optional[str],
                       method: str = "", params: Any = None) -> None:
        reason = self._owner_gate(device_id, owner)
        if reason is not None:
            self._deny_unit(device_id, method, params,
                            f"{wire.E_NOT_OWNER}: {reason}", owner=owner)
            from mcp_loops.origin_proto.channel import ChannelDenied
            raise ChannelDenied(wire.E_NOT_OWNER, reason)

    async def start(self, device_id: str, name: str, *,
                    slug: Optional[str] = None,
                    dispatch_id: Optional[str] = None,
                    owner: Optional[str] = None,
                    config: Optional[dict] = None) -> dict:
        """Send ``loop.start`` to ``device_id``'s agent. The origin-side Product
        allowlist may still refuse it (§6.4) — that comes back as a typed RPC
        error, which we surface rather than swallow. ``config`` (gap #3) ships the
        Hub's saved loop config so an origin that lacks the loop can prepare it
        (the origin saves it only when absent, after its own Project gate)."""
        self._require_owner(device_id, owner, "loop.start",
                            {"name": name, "slug": slug})
        key = f"{device_id}:{name}:{slug or ''}"
        did = dispatch_id or self._dispatch_ids.get(key) or self._id_fn()
        self._dispatch_ids[key] = did
        if self._registry is not None:
            self._registry.note_seen(device_id, busy=True)
        try:
            with self._ctx(owner):
                params = {"name": name, "slug": slug}
                if config is not None:
                    params["config"] = config
                return await self._server.call_rpc(
                    device_id, "loop.start", params,
                    dispatch_id=did, owner=owner or self._owner)
        finally:
            if self._registry is not None:
                self._registry.note_seen(device_id, busy=False)

    async def run(self, device_id: str, argv: Any, *,
                  stdin: Optional[str] = None, cwd: Optional[str] = None,
                  env: Optional[dict] = None, timeout: Optional[float] = None,
                  stream: bool = False, cap_token: Optional[str] = None,
                  dispatch_id: Optional[str] = None,
                  on_chunk: Optional[Callable[[dict], Any]] = None,
                  max_bytes: Optional[int] = None,
                  owner: Optional[str] = None,
                  verb: Optional[str] = None) -> dict:
        """Route ``origin.run`` to ``device_id``'s agent (§2.4 seam 3). This is the
        ONLY hub→origin path for exec: ``.call`` refuses every dispatch-id method
        and ``.start`` is hardcoded to ``loop.start``, so neither can carry it.

        Two lifecycle facts §2.4 forces, both handled here:

          * The channel's single-future ``call_rpc`` defaults to ``RPC_TIMEOUT``
            (30 s). A streamed/long run cannot inherit that — the ``timeout`` param
            IS the run budget, so we wait on the future for ``timeout + margin``
            (the origin's own timeout fires first and returns a proper result), or
            wait UNBOUNDED when no budget is given. Never the silent 30 s cap.
          * When ``stream`` and ``on_chunk`` are set we subscribe to the run
            back-channel by dispatch-id BEFORE sending, so run.chunk/run.end frames
            reach the caller as they arrive (correlated by dispatchId, not loop
            name); the terminal rpc_result still resolves this call. Unsubscribe in
            ``finally`` so a subscriber never leaks past its run.

        A fresh dispatch-id is minted per call (exec is not naturally idempotent by
        target the way ``loop.start`` is); pass ``dispatch_id`` explicitly to make a
        retry ride the origin's idempotency ledger instead of re-executing.

        §5.1(4) HARD GATE: before anything is sent, the Hub refuses ``origin.run``
        to a NON-loopback origin whose channel is not mTLS device-bound — fail
        closed, so a plaintext/CERT_NONE/unbound remote exec never leaves the Hub.
        Loopback origins (the VPS's own ``local``) are exempt."""
        argv_list = list(argv)
        # P6: owner scoping first — a device the caller does not own is refused
        # (and audited with the real argv) before the transport gate even runs.
        not_owner = self._owner_gate(device_id, owner)
        if not_owner is not None:
            self._audit(A_RUN_DENIED, deviceId=device_id, argv=argv_list, cwd=cwd,
                        reason="owner", detail=not_owner,
                        owner=owner or self._owner)
            self._deny_unit(device_id, "origin.run", {"argv": argv_list, "cwd": cwd},
                            f"{wire.E_NOT_OWNER}: {not_owner}", owner=owner)
            from mcp_loops.origin_proto.channel import ChannelDenied
            raise ChannelDenied(wire.E_NOT_OWNER, not_owner)
        gate = self._server.exec_transport_ok(device_id)
        if gate is not None:
            # §5.1(4) fail-closed refusal — audit it (with the real argv) BEFORE we
            # raise, so a remote exec the Hub refused to send is never silent (§6.5).
            self._audit(A_RUN_DENIED, deviceId=device_id, argv=argv_list, cwd=cwd,
                        reason="transport", detail=gate)
            self._deny_unit(device_id, "origin.run", {"argv": argv_list, "cwd": cwd},
                            f"{wire.E_TLS_REQUIRED}: {gate}", owner=owner)
            from mcp_loops.origin_proto.channel import ChannelDenied
            raise ChannelDenied(wire.E_TLS_REQUIRED, gate)
        did = dispatch_id or self._id_fn()
        params: dict = {"argv": argv_list}
        if stdin is not None:
            params["stdin"] = stdin
        if cwd is not None:
            params["cwd"] = cwd
        if env is not None:
            params["env"] = env
        if timeout is not None:
            params["timeout"] = timeout
        if stream:
            params["stream"] = True
        if max_bytes is not None:
            params["maxBytes"] = int(max_bytes)  # P4c: capped at capture, origin-side
        if cap_token is not None:
            params["capToken"] = cap_token
        # the channel wait budget — NOT RPC_TIMEOUT (§2.4 seam 2). None → wait
        # forever for an unbounded run; else the run budget plus a margin so the
        # origin-side timeout wins the race and returns a proper timedOut envelope.
        wait_timeout = None if timeout is None else float(timeout) + RUN_WAIT_MARGIN
        subscribed = False
        if stream and on_chunk is not None:
            self._server.subscribe_run(did, on_chunk, device_id=device_id)
            subscribed = True
        if self._registry is not None:
            self._registry.note_seen(device_id, busy=True)
        try:
            with self._ctx(owner):
                result = await self._server.call_rpc(
                    device_id, "origin.run", params,
                    dispatch_id=did, timeout=wait_timeout,
                    owner=owner or self._owner, verb=verb)
        except Exception as e:  # noqa: BLE001 — audit the refusal/failure, then re-raise
            # Every refused OR failed run is audited with its real argv (§6.5), but
            # the KIND must tell the truth (P3b): only an origin-side typed refusal
            # (RunNotAllowed / argv policy, §5.3 → RpcRefused) proves nothing ran.
            # A disconnect / timeout / mid-flight revoke leaves the outcome UNKNOWN
            # — the run may well have completed — so it is never logged as denied.
            # A channel refusal raised BEFORE the frame left (revoked / not live /
            # no dispatchId → ``sent=False``) is equally proof nothing ran: denied.
            from mcp_loops.origin_proto.channel import ChannelError, RpcRefused
            if isinstance(e, RpcRefused):
                self._audit(A_RUN_DENIED, deviceId=device_id, dispatchId=did,
                            argv=argv_list, cwd=cwd, reason=str(e), code=e.code)
            elif isinstance(e, ChannelError) and not e.sent:
                self._audit(A_RUN_DENIED, deviceId=device_id, dispatchId=did,
                            argv=argv_list, cwd=cwd, reason=str(e),
                            code=getattr(e, "code", None), sent=False)
            else:
                self._audit(A_RUN_UNCONFIRMED, deviceId=device_id, dispatchId=did,
                            argv=argv_list, cwd=cwd, reason=str(e))
            raise
        else:
            if result.get("_reattached"):
                # P3a: a reused dispatchId re-attached to the origin's ledger entry
                # — NOTHING executed this time, and the cached exitCode belongs to
                # the ORIGINAL argv. Record an idempotent hit (with the argv that was
                # asked for, so a mismatched replay is visible) — never a fresh A_RUN.
                self._audit(A_RUN_REATTACHED, deviceId=device_id, dispatchId=did,
                            argv=argv_list, cwd=cwd)
                return result
            # exec-semantics record: the REAL argv + outcome, correlatable to the
            # send-time A_DISPATCH line by dispatchId (§5.1(3), §6.5).
            self._audit(A_RUN, deviceId=device_id, dispatchId=did, argv=argv_list,
                        cwd=cwd, exitCode=result.get("exitCode"),
                        timedOut=result.get("timedOut"),
                        durationMs=result.get("durationMs"))
            return result
        finally:
            if subscribed:
                self._server.unsubscribe_run(did, device_id=device_id)
            if self._registry is not None:
                self._registry.note_seen(device_id, busy=False)

    async def stop(self, device_id: str, name: str, *,
                   owner: Optional[str] = None) -> dict:
        self._require_owner(device_id, owner, "loop.stop", {"name": name})
        with self._ctx(owner):
            return await self._server.call_rpc(device_id, "loop.stop",
                                               {"name": name},
                                               owner=owner or self._owner)

    async def call(self, device_id: str, method: str,
                   params: Optional[dict] = None, *,
                   owner: Optional[str] = None) -> dict:
        """Pass-through for the read RPCs (loop.list/get/status, products.list,
        origin.describe) — the router is the single hub→origin call site."""
        if method in wire.RPC_METHODS_REQUIRING_DISPATCH_ID:
            raise ValueError(f"{method} must go through DispatchRouter.start")
        self._require_owner(device_id, owner, method, params)
        with self._ctx(owner):
            return await self._server.call_rpc(device_id, method, params or {},
                                               owner=owner or self._owner)

    # ── §6.4: the LIVE onboarding check (make the pure differ actually live) ──
    async def onboarding_check(self, device_id: str, required: Any, *,
                               owner: Optional[str] = None) -> dict:
        """Probe ``device_id``'s REAL, fresh capability picture over the wire and
        diff it against the CLIs a set of loops NEED (``required``) — the live half
        of §6.4. :func:`onboarding_actions` is the pure differ; this is the I/O that
        feeds it a *current* ``origin.describe`` (``probeAuth=True`` so ``authState``
        is freshly observed, ``candidates=required`` so only the needed CLIs are
        probed — GAP-1/GAP-2), then AUDITS the readiness verdict (``A_ONBOARDING``,
        fail-soft) so the owner has a trail of every readiness check, not just execs.

        Returns ``{origin, checkedAt, required, actions, blockers, ready}`` — ``ready``
        is True iff nothing is missing/unauthenticated (no actions). The describe RPC
        is a plain read (no dispatch-id), so a network failure surfaces as the caller's
        exception rather than a silent 'ready'."""
        req = [str(x) for x in (required or [])]
        self._require_owner(device_id, owner, "origin.describe",
                            {"probeAuth": True, "candidates": req})
        with self._ctx(owner):
            desc = await self._server.call_rpc(
                device_id, "origin.describe",
                {"probeAuth": True, "candidates": req},
                owner=owner or self._owner)
        actions = onboarding_actions(desc, req)
        origin = (desc.get("origin") if isinstance(desc, dict) else None) or device_id
        blockers = [a for a in actions if a.get("severity") == "blocker"]
        out = {
            "origin": origin,
            "checkedAt": time.time(),
            "required": req,
            "actions": actions,
            "blockers": len(blockers),
            "ready": not actions,
        }
        self._audit(A_ONBOARDING, deviceId=device_id, origin=origin,
                    required=req, actionCount=len(actions),
                    blockerCount=len(blockers), ready=out["ready"])
        return out

    async def onboarding_refresh_loop(
            self, device_id: str, required: Any, *, interval: float = 300.0,
            max_rounds: Optional[int] = None,
            on_result: Optional[Callable[[dict], Any]] = None,
            owner: Optional[str] = None) -> int:
        """The RECURRING driver §6.4 wants: re-run :meth:`onboarding_check` every
        ``interval`` seconds so a login that lapses (or a CLI that gets installed) is
        reflected in the audit trail without a human poking it. Each round is
        fail-soft — a transient describe failure logs nothing new and the loop keeps
        going. ``max_rounds`` bounds it (tests pin it; production leaves it None for
        an endless refresh until cancelled). Returns the number of checks that
        completed. ``on_result`` (optional) receives each round's verdict."""
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            try:
                res = await self.onboarding_check(device_id, required, owner=owner)
                if on_result is not None:
                    on_result(res)
            except Exception:  # noqa: BLE001 — a flaky round must not kill the refresh
                pass
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            await asyncio.sleep(interval)
        return rounds


# ── the hub façade ────────────────────────────────────────────────────────────
def _never_loopback(host: Optional[str]) -> bool:
    return False


class OriginHub:
    """Ties a :class:`ChannelServer` to the registry + consolidator + router with
    the right hooks. Construct with an :class:`EnrollmentStore`; ``await start()``
    to accept dials. The channel/enrollment layer is untouched — the hub only
    passes the hooks those layers already expose.
    """

    def __init__(self, store: Any, *, now_fn: Callable[[], float] = time.time,
                 ssl_context: Any = None, require_tls_binding: bool = False,
                 hub_cert_pem: Optional[bytes] = None,
                 loopback_exempt: bool = True,
                 inventory: bool = False,
                 inventory_poll_sec: float = INVENTORY_POLL_SEC,
                 inventory_exclude: tuple = ()):
        # imported here so this module doesn't hard-depend on the asyncio channel
        # at import time (keeps pure-logic imports cheap + circular-free).
        from mcp_loops.origin_proto.channel import ChannelServer
        self.store = store
        self.registry = OriginRegistry(store, now_fn=now_fn)
        self.consolidator = EventConsolidator()
        # S-0 (§7.3): the connected origins' loop.list inventory. Opt-in per
        # hub (``inventory=True``): the two serving hubs — hub_serve and the
        # in-process LocalOriginService — turn it on; a bare hub (tests, tools)
        # sends nothing it did not before. Devices in ``inventory_exclude`` are
        # never polled — the in-process service's own device IS this box, whose
        # loops the read path already scans on disk.
        self.inventory = InventoryCache(now_fn=now_fn)
        self._inventory_on = bool(inventory)
        self._inventory_poll_sec = float(inventory_poll_sec)
        self._inventory_exclude = set(inventory_exclude)
        self._inv_tasks: dict[str, asyncio.Task] = {}
        self._inv_again: set = set()
        self._inv_poller: Optional[asyncio.Task] = None
        # §5.1(4): pass the TLS material through so a remote-serving deployment can
        # turn the Hub-side mTLS hard gate ON (real ssl_context from
        # tls.server_ssl_context + require_tls_binding). The default stays plaintext
        # loopback — the VPS's own `local` origin, which the gate exempts.
        self.server = ChannelServer(
            store,
            on_event=self._on_event,
            on_authenticated=self._on_authenticated,
            ssl_context=ssl_context,
            require_tls_binding=require_tls_binding,
            # P2.5 G5: the cert a TLS Hub presents, handed back in the `paired`
            # answer so a remote box can verify it against the out-of-band
            # fingerprint and PIN it. None (plaintext loopback) = unchanged.
            hub_cert_pem=hub_cert_pem,
            # a Hub reached through the dashboard's /hub tunnel sees EVERY peer
            # as 127.0.0.1 (the gate), so it must not grant the loopback
            # exemption: each origin.run then needs an mTLS device-bound channel
            **({} if loopback_exempt else {"is_loopback_fn": _never_loopback}),
            now_fn=now_fn)
        self.registry.bind_server(self.server)
        self.router = DispatchRouter(self.server, registry=self.registry)
        # a revoke should drop the registry view too — chain onto the store hook
        # the ChannelServer already installed (channel does the socket teardown;
        # we add the registry-view update without disturbing it).
        self._chain_revoke_hook()

    def _chain_revoke_hook(self) -> None:
        prior = getattr(self.store, "_on_revoke", None)

        def _combined(device_id: str) -> None:
            if prior is not None:
                prior(device_id)
            self.registry.note_revoked(device_id)
            self.inventory.forget(device_id)

        self.store._on_revoke = _combined

    def _on_authenticated(self, device_id: str, rec: dict) -> None:
        self.registry.on_authenticated(device_id, rec)
        self.refresh_inventory_soon(device_id)          # §7.3 b: on connect

    def _on_event(self, device_id: str, frame: dict) -> None:
        # both the registry (recency/busy) and the consolidator (per-origin
        # namespace) see every pushed event.
        self.registry.on_frame(device_id, frame)
        self.consolidator.ingest(device_id, frame)
        if isinstance(frame, dict) and frame.get("kind") == "run":
            self.refresh_inventory_soon(device_id)      # §7.3 b: on each run

    # ── S-0 inventory refresh (§7.3 b–e) ──────────────────────────────────────
    async def refresh_inventory(self, device_id: str) -> Optional[dict]:
        """Fetch ``device_id``'s inventory over the existing ``loop.list`` RPC
        and record it (fail-soft). The call is made on behalf of the device's
        ENROLLED owner, so a non-default-owner origin is not refused by the
        owner gate. A manifest without ``loop.list`` is recorded ``hidden``
        without sending (no deny-audit line every poll)."""
        if device_id in self._inventory_exclude:
            return None
        m = None
        try:
            m = self.server.manifest_for(device_id)
        except Exception:  # noqa: BLE001
            m = None
        if m is not None:
            rpcs = (m.get("manifest") or {}).get("rpcs") or ()
            if "loop.list" not in rpcs:
                self.inventory.record_hidden(
                    device_id, "origin did not advertise rpc 'loop.list'")
                return self.inventory.get(device_id)
        owner = None
        try:
            dev = self.store.get_device(device_id) if self.store is not None else None
            owner = device_owner(dev) if dev is not None else None
        except Exception:  # noqa: BLE001
            owner = None
        try:
            with _daudit.dispatch_context(owner=owner or DEFAULT_OWNER):
                res = await self.server.call_rpc(
                    device_id, "loop.list", {}, timeout=INVENTORY_RPC_TIMEOUT,
                    owner=owner or DEFAULT_OWNER)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — §7.3 c: keep the last good list
            self.inventory.record_error(device_id, e)
        else:
            self.inventory.record(device_id, res)
        return self.inventory.get(device_id)

    def refresh_inventory_soon(self, device_id: str) -> None:
        """Schedule a refresh on the running loop; coalesces a burst of triggers
        into at most one in-flight fetch plus one follow-up. No running loop
        (pure unit fakes) → no-op."""
        if not self._inventory_on or device_id in self._inventory_exclude \
                or not _sync_mode.s0_inventory_enabled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if device_id in self._inv_tasks:
            self._inv_again.add(device_id)
            return

        async def _go() -> None:
            try:
                while True:
                    self._inv_again.discard(device_id)
                    await self.refresh_inventory(device_id)
                    if device_id not in self._inv_again:
                        break
            finally:
                self._inv_tasks.pop(device_id, None)

        self._inv_tasks[device_id] = loop.create_task(_go())

    async def poll_inventory_once(self) -> None:
        """One poll tick: refresh every live origin; an origin that dropped
        keeps its last list, marked stale."""
        live = set(self.server.live_origins())
        for rec in self.registry.list():
            did = rec["deviceId"]
            if did not in live:
                self.inventory.mark_stale(did, "origin not connected")
        for did in sorted(live):
            self.refresh_inventory_soon(did)

    async def _poll_inventory(self) -> None:
        while True:
            await asyncio.sleep(self._inventory_poll_sec)
            try:
                await self.poll_inventory_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick hiccup never stops polling
                pass

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        port = await self.server.start(host, port)
        if self._inventory_on and self._inventory_poll_sec > 0 \
                and self._inv_poller is None:
            self._inv_poller = asyncio.ensure_future(self._poll_inventory())
        return port

    async def stop(self) -> None:
        tasks = list(self._inv_tasks.values())
        if self._inv_poller is not None:
            tasks.append(self._inv_poller)
            self._inv_poller = None
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.server.stop()


# ── fusion: live-enrolled origins SUPERSEDE the rsync-mirror fallback ─────────
def unified_origins(registry: OriginRegistry, consolidator: EventConsolidator,
                    *, mirror_root: Optional[str] = None,
                    now: Optional[float] = None,
                    inventory: Optional[InventoryCache] = None) -> list[dict]:
    """Fuse the LIVE-enrolled origins (from the registry + event consolidation)
    with the rsync-mirror fallback (:func:`mcp_loops.origins.list_origins`) for
    origins that are NOT yet enrolled. A live origin SUPERSEDES its mirror (same
    logical id); mirror-only origins remain visible so nothing regresses while a
    box is still on the old rsync path.

    The ``origins.LOCAL_ORIGIN_ID`` ("local") special-case becomes "the first
    enrolled origin": if any origin is live-enrolled, that is the authoritative
    'local'-class view; the mirror 'local' only shows through when nothing is
    enrolled yet — keeping full back-compat with the pre-Origins dashboard.
    """
    now = now if now is not None else time.time()
    out: dict[str, dict] = {}

    # 1) mirror fallback first (lowest precedence)
    if mirror_root:
        try:
            for mo in origins_mod.list_origins(mirror_root, now=now):
                out[mo["id"]] = {**mo, "source": "mirror", "live": False}
        except Exception:  # noqa: BLE001 — a mirror scan error must not sink the
            pass          # live view; live origins still render.

    # 2) live-enrolled origins SUPERSEDE (highest precedence). An enrolled origin
    #    reports its logical id via origin.describe / its event stream; we key on
    #    the capability-reported origin id, falling back to the device label/id.
    for rec in registry.list():
        caps = rec.get("capabilities") or {}
        origin_id = origin_display_id(rec)
        loops = consolidator.loops_for(origin_id)
        inv = inventory.get(rec["deviceId"]) if inventory is not None else None
        # §7.3 d: the card counts the fetched inventory — the event namespace
        # only knows dispatched loops (the literal ``loops: 0``). No list yet →
        # the event count (today's behaviour); hidden → no count at all.
        if inv is not None and inv["status"] == INV_HIDDEN:
            count: Optional[int] = None
        elif inv is not None and inventory.known(rec["deviceId"]):
            names = {r["name"] for r in inv["rows"]} | {l["name"] for l in loops}
            count = len(names)
        else:
            count = len(loops)
        out[origin_id] = {
            "id": origin_id,
            "name": rec.get("label") or origin_id,
            "source": "live",
            "live": rec.get("health") in (ONLINE, BUSY, STALE),
            "health": rec.get("health"),
            "deviceId": rec["deviceId"],
            "capabilities": caps,
            "loops": count,
            "last_seen": consolidator.last_seen(origin_id) or rec.get("lastSeen"),
            # a live-enrolled origin is reachable over the channel, superseding
            # whatever the mirror mtime claimed.
            "reachable": rec.get("health") in (ONLINE, BUSY),
            "stale": rec.get("health") == STALE,
        }
        if inv is not None:
            out[origin_id]["inventory"] = inv["status"]
            out[origin_id]["inventoryFetchedAt"] = inv["fetchedAt"]
    return [out[k] for k in sorted(out)]


def first_enrolled_origin(registry: OriginRegistry) -> Optional[str]:
    """The origin that now plays the role the ``origins.LOCAL_ORIGIN_ID`` static
    'local' used to: the first live-enrolled origin (stable order by device id),
    or None when nothing is enrolled yet (mirror 'local' still applies)."""
    live = registry.live_ids()
    return live[0] if live else None
