"""Origins — where loops actually RUN. First-class objects on top of the
existing multi-host mirror layout.

An **origin** is a machine (VPS or local box) running this same tool. The web
app is a MIRROR that CONSOLIDATES multiple origins. The layout on disk that
already exists in this repo is::

    data/_loops/                # the LOCAL origin (this host)
    data/_loops_<hostname>/     # mirror of a remote origin (e.g. _loops_anneke)

This module is *lean*: it enumerates that layout, tags every loop with the
origin that owns it, and surfaces health (last-seen ts from status.jsonl mtime).
It does NOT invent new remote-exec — that's a later increment. It is also pure:
no env reads, no logging; callers pass in the local mirror path (usually
``LOOPS_DATA_DIR``) and the module scans its parent for ``_loops_<host>`` mirror
siblings.

Record shape returned by :func:`list_origins`::

    {
      "id": "local",                 # 'local' for the local mirror, else the host suffix
      "name": "local",               # display name (same as id for now)
      "path": "/…/data/_loops",      # absolute mirror path on THIS box
      "kind": "local" | "mirror",    # local = the tool's own data dir, mirror = _loops_<host>
      "loops": 5,                    # non-archived loops (config.json, run not archived)
      "archived": 1,                 # archived loops, excluded from ``loops``
      "last_seen": 1789347501.4,     # max status.jsonl mtime across its loops (or None)
      "health": "fresh" | "stale" | "cold" | "empty",
      "reachable": True,             # local: always True; mirror: True iff health=='fresh'
      "stale": False,                # mirror: True when path exists but health != fresh
    }

Health is a coarse cue for the surface:

  fresh — activity within the last hour
  stale — activity within the last day
  cold  — activity older than a day
  empty — no loop activity found (no status.jsonl / run.json anywhere)

Connection state derives from health without inventing new signals:

  reachable — local is always True. A mirror is reachable iff its most recent
              status/run write is within ``FRESH_SEC``. We do NOT do live
              SSH/HTTP probes here; the ONLY evidence we have is the
              rsync/mirror mtime already on disk. Anything else would be a lie.

  stale     — a mirror we can *see* on disk (path exists) but whose last write
              is older than ``FRESH_SEC``. ``stale`` and ``reachable`` are
              mutually exclusive for mirrors; local is neither stale nor
              unreachable by construction.

"""

from __future__ import annotations

import json
import os
from typing import Optional

LOCAL_ORIGIN_ID = "local"
MIRROR_PREFIX = "_loops_"

# Marker file dropped in a mirror dir by ``yard connect <host>`` (see
# mcp_loops.yard). Records that the box was told about this origin BEFORE any
# loop has synced onto it, so the surface can say "connected · awaiting first
# sync" instead of the misleading "unreachable · last seen never" it would show
# for an empty dir. Hidden + a file (not a dir), so it never counts as a loop.
ORIGIN_MARKER = ".origin.json"

# health thresholds (seconds)
FRESH_SEC = 60 * 60           # 1 hour
STALE_SEC = 24 * 60 * 60      # 1 day


def _abs(path: str) -> str:
    return os.path.abspath(path)


def _mirror_dirs(local_path: str) -> list[tuple[str, str, str]]:
    """Yield ``(origin_id, kind, absolute_path)`` for every mirror visible from
    ``local_path``: the local mirror first (id ``local``), then every
    ``_loops_<host>`` sibling under ``dirname(local_path)``. Missing paths are
    skipped so a fresh box (no siblings yet) still shows the local origin."""
    out: list[tuple[str, str, str]] = []
    local_abs = _abs(local_path)
    if os.path.isdir(local_abs):
        out.append((LOCAL_ORIGIN_ID, "local", local_abs))
    parent = os.path.dirname(local_abs)
    if not parent or not os.path.isdir(parent):
        return out
    for name in sorted(os.listdir(parent)):
        if not name.startswith(MIRROR_PREFIX):
            continue
        p = os.path.join(parent, name)
        if not os.path.isdir(p) or _abs(p) == local_abs:
            continue
        host = name[len(MIRROR_PREFIX):]
        if not host:
            continue
        out.append((host, "mirror", p))
    return out


def _loop_names(mirror_path: str) -> list[str]:
    """Loop subdirs under a mirror path (ignores hidden entries and top-level
    ``_*`` folders like ``_registry`` / ``_output``)."""
    if not os.path.isdir(mirror_path):
        return []
    out: list[str] = []
    for name in sorted(os.listdir(mirror_path)):
        if name.startswith(".") or name.startswith("_"):
            continue
        if os.path.isdir(os.path.join(mirror_path, name)):
            out.append(name)
    return out


def _visible_counts(mirror_path: str, names: list[str]) -> tuple[int, int]:
    """``(visible, archived)`` loop counts for an origin, matching the loops feed
    (loopyard-bug-1790089837-2): a loop is a dir with a ``config.json``; it is
    archived when its ``run.json`` says so. Non-loop dirs count as neither."""
    visible = archived = 0
    for name in names:
        d = os.path.join(mirror_path, name)
        if not os.path.isfile(os.path.join(d, "config.json")):
            continue
        try:
            with open(os.path.join(d, "run.json"), encoding="utf-8") as fh:
                run = json.load(fh)
        except (OSError, ValueError):
            run = None
        if isinstance(run, dict) and run.get("archived"):
            archived += 1
        else:
            visible += 1
    return visible, archived


def _mtime_of(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _last_seen_for(mirror_path: str) -> Optional[float]:
    """Max mtime of any loop's ``status.jsonl`` / ``run.json`` under
    ``mirror_path`` — a coarse "last activity" for an origin. None = no activity."""
    latest: Optional[float] = None
    for loop in _loop_names(mirror_path):
        for probe in ("status.jsonl", "run.json"):
            m = _mtime_of(os.path.join(mirror_path, loop, probe))
            if m is None:
                continue
            if latest is None or m > latest:
                latest = m
    return latest


def _health_of(last_seen: Optional[float], now: float) -> str:
    if last_seen is None:
        return "empty"
    age = max(0.0, now - float(last_seen))
    if age <= FRESH_SEC:
        return "fresh"
    if age <= STALE_SEC:
        return "stale"
    return "cold"


def _reachable_of(kind: str, health: str) -> bool:
    """Honest reachability: local is always reachable (we ARE the box); a mirror
    is only reachable if its rsync/status mtime is fresh. No live probing —
    without that, the last-seen mtime is our only evidence."""
    if kind == "local":
        return True
    return health == "fresh"


def _stale_of(kind: str, health: str) -> bool:
    """A mirror we can see but whose activity is older than ``FRESH_SEC``.
    Local is never stale by construction."""
    if kind == "local":
        return False
    return health in ("stale", "cold", "empty")


def _read_marker(mirror_path: str) -> Optional[dict]:
    """The ``yard connect`` marker for a mirror, or None if absent/corrupt.
    Fail-soft: a garbled marker is treated as no marker (never raises)."""
    try:
        with open(os.path.join(mirror_path, ORIGIN_MARKER), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _build_origin(origin_id: str, kind: str, path: str, *, now: float,
                  with_loop_list: bool = False) -> dict:
    """Assemble one origin record — the single source of truth for both
    :func:`list_origins` and :func:`get_origin`, so health/reachability and the
    ``yard connect`` marker fields can never drift between the two."""
    loops = _loop_names(path)
    visible, archived = _visible_counts(path, loops)
    last_seen = _last_seen_for(path)
    health = _health_of(last_seen, now)
    reachable = _reachable_of(kind, health)
    stale = _stale_of(kind, health)
    marker = _read_marker(path)
    # A mirror we were told about (marker present) but that has never synced a
    # loop (no status.jsonl/run.json anywhere) is PENDING, not empty/unreachable:
    # the box knows it exists, it just hasn't heard from it yet. That's the
    # honest cause line the surface should show (P2 — never lie about state).
    if marker is not None and kind != "local" and last_seen is None:
        health = "pending"
        reachable = False
        stale = False
    rec: dict = {
        "id": origin_id,
        "name": origin_id,
        "path": path,
        "kind": kind,
        # the count matches the loops feed: non-archived loops only, with the
        # archived ones counted separately (the detail view keeps the dir list)
        "loops": (loops if with_loop_list else visible),
        "archived": archived,
        "last_seen": last_seen,
        "health": health,
        "reachable": reachable,
        "stale": stale,
    }
    if with_loop_list:
        rec["loop_count"] = len(loops)
    if marker is not None:
        # Surface only the honest, non-secret facts the box recorded at connect
        # time. There is never a key/token here — `yard connect` refuses them.
        if marker.get("address"):
            rec["address"] = str(marker["address"])
        if marker.get("connectedAt") is not None:
            rec["connectedAt"] = marker["connectedAt"]
        rec["source"] = str(marker.get("source") or "yard")
    return rec


def list_origins(local_path: str, *, now: float) -> list[dict]:
    """List every origin visible from ``local_path`` — local first, then mirror
    siblings sorted by id. Each entry carries ``loops`` count, ``last_seen`` mtime,
    and a coarse ``health`` cue for the surface. Mirrors added via ``yard
    connect`` but not yet synced read ``health:"pending"`` with their recorded
    ``connectedAt``/``address`` so the surface names the exact reason."""
    return [
        _build_origin(origin_id, kind, path, now=now)
        for origin_id, kind, path in _mirror_dirs(local_path)
    ]


def get_origin(local_path: str, origin_id: str, *, now: float) -> Optional[dict]:
    """One origin with its full loop list. Returns None if not visible."""
    for oid, kind, path in _mirror_dirs(local_path):
        if oid == origin_id:
            return _build_origin(oid, kind, path, now=now, with_loop_list=True)
    return None


def origin_for_loop(local_path: str, loop_name: str) -> Optional[str]:
    """Which origin owns a given loop name. LOCAL wins if the same name appears
    under multiple mirrors (an unusual case worth being explicit about)."""
    if not loop_name:
        return None
    for origin_id, _kind, path in _mirror_dirs(local_path):
        if os.path.isdir(os.path.join(path, loop_name)):
            return origin_id
    return None


def _loop_count(o: dict) -> int:
    """Loops on an origin however the record spells it — ``loops`` may be an int
    (list view) or a list (detail view); ``loop_count`` is the detail alias."""
    lo = o.get("loops")
    if isinstance(lo, int):
        return lo
    if isinstance(lo, list):
        return len(lo)
    lc = o.get("loop_count")
    return lc if isinstance(lc, int) else 0


def _unreachable_note(health: Optional[str]) -> str:
    """The honest one-line reason an origin is not reachable (never invented)."""
    return {
        "pending": "connected, waiting for its first sync",
        "stale": "last sync is stale",
        "cold": "no recent sync",
        "empty": "no activity seen yet",
    }.get(health or "", "not reachable from this box")


def fleet_summary(origins_list: list[dict]) -> dict:
    """The **Fleet-pill** summary (REDESIGN-SPEC §2 / rd-origins) over an already
    assembled origin list — local + mirror + live-enrolled origins, **NOT**
    connected-sessions (a session's home is the Sessions page, so it is never
    counted in the compute fleet). Returns the honest ``N origins · M reachable``
    counts, a per-origin ``dot`` for the popover, and the **drop signal**: an
    origin that is *running a loop* but is no longer reachable — the amber-dot +
    quiet-toast case the top bar surfaces. Pure; never raises.

    ``dot`` per origin: ``on`` (reachable + healthy), ``amber`` (reachable but
    stale/pending, OR a dropped origin that still owns loops), ``off`` (an idle
    unreachable origin — nothing running, nothing to alarm about)."""
    real = [o for o in (origins_list or [])
            if isinstance(o, dict) and o.get("kind") != "connected-session"]
    total = len(real)
    reachable = 0
    items: list = []
    dropped: list = []
    for o in real:
        loops = _loop_count(o)
        reach = bool(o.get("reachable"))
        health = o.get("health")
        if reach:
            reachable += 1
            dot = "amber" if health in ("stale", "pending") else "on"
        else:
            dot = "amber" if loops > 0 else "off"
        name = o.get("name") or o.get("id")
        items.append({"id": o.get("id"), "name": name, "kind": o.get("kind"),
                      "health": health, "reachable": reach, "loops": loops,
                      "dot": dot})
        if not reach and loops > 0:
            dropped.append({"id": o.get("id"), "name": name,
                            "health": health, "loops": loops})
    noun = "origin" if total == 1 else "origins"
    return {
        "origins": total,
        "reachable": reachable,
        "label": f"{total} {noun} · {reachable} reachable",
        "dots": "".join("●" if i["reachable"] else "○" for i in items),
        "dropped": dropped,
        "dropCount": len(dropped),
        "items": items,
    }


def picker_options(origins_list: list[dict],
                   sessions: Optional[list[dict]] = None) -> list[dict]:
    """The **one shared health-aware Origin picker** (rd-origins mandate: the
    composer must offer the *full reachable fleet*, not only ``local``). Orders
    the local origin first, then reachable mirrors/live origins, then unreachable
    ones shown but ``disabled`` with an honest reason; finally targetable
    connected **sessions** as ``(session)`` compute (rd-origins: sessions appear
    in pickers, their home is the Sessions page). Pure; never raises."""
    opts: list = []
    real = [o for o in (origins_list or [])
            if isinstance(o, dict) and o.get("kind") != "connected-session"]

    def _key(o: dict):
        return (o.get("kind") != "local", not bool(o.get("reachable")),
                str(o.get("id") or ""))

    for o in sorted(real, key=_key):
        reach = bool(o.get("reachable"))
        health = o.get("health")
        name = o.get("name") or o.get("id")
        opts.append({
            "id": o.get("id"), "name": name, "kind": o.get("kind"),
            "reachable": reach, "health": health, "disabled": not reach,
            "label": name, "note": None if reach else _unreachable_note(health),
        })
    for s in (sessions or []):
        if not isinstance(s, dict):
            continue
        live = bool(s.get("reachable")) if "reachable" in s else bool(s.get("live"))
        raw = str(s.get("id") or "")
        sid = raw if raw.startswith("session:") else f"session:{raw}"
        base = s.get("name") or raw.replace("session:", "")
        opts.append({
            "id": sid, "name": base, "kind": "session", "reachable": live,
            "health": s.get("health"), "disabled": not live,
            "label": f"{base} (session)",
            "note": None if live else "session offline",
            "runtime": s.get("runtime"),
        })
    return opts


def tag_loops_with_origins(local_path: str, loop_entries: list[dict]) -> list[dict]:
    """Enrich a list of loop dicts (each with a ``name`` field) with an
    ``origin`` field naming the mirror they live under. Non-destructive — the
    input list is not mutated. Loops we can't locate get ``origin: None`` (be
    honest — never make one up)."""
    tagged: list[dict] = []
    for entry in loop_entries:
        if not isinstance(entry, dict):
            tagged.append(entry)
            continue
        out = dict(entry)
        out.setdefault("origin", origin_for_loop(local_path, entry.get("name") or ""))
        tagged.append(out)
    return tagged
