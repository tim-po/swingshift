"""The Idea Hub — targets (a): a per-project workspace of LIVING DOCUMENTS.

REDESIGN-SPEC §3 (rd-ideahub) + §6: the Idea Hub is *not* a queue of strings, it
is a workspace where a rough **note** thickens into a **proposal** and — the moment
a loop is pointed at it — that same document *becomes* an **objective**. The three
are points on ONE continuous substrate, never three tables and never a type you
pick from a dropdown:

    ·  note       just created, only you have touched it
    ○  proposal   has been marked ready / shared — a design a loop *could* take
    ◔  objective  a loop is (or has been) pointed at it — a live target
    ●  done       its RESULT ribbon is green (a pointed loop finished clean, or
                  the owner marked it done) — never on an error/stopped end

So the "type" is **derived, never chosen** (:func:`derive_state`): pointing a loop
is what promotes a doc to an objective, exactly as the vision states. A doc also
carries a **loop-rewrite** flag — the read-only-canvas vs loop-may-rewrite toggle
that keeps the human in control of which docs a loop may edit in place.

Storage mirrors :mod:`mcp_loops.issues`: one file per doc under
``<data>/_ideahub/<id>.json`` plus an append-only ``index.jsonl`` the Hub lists
from. Same fail-soft discipline — a write never raises into a request thread, a
missing store reads as empty. This is DATA MODEL + storage + CRUD only; the §6
two-pane UI (next round) paints these records.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

from mcp_loops import report

# the derived spectrum (§2.3) — state name → the rail glyph the UI renders
STATES = ("note", "proposal", "objective", "done")
GLYPH = {"note": "·", "proposal": "○", "objective": "◔", "done": "●"}

# the only actor that may set/clear a doc's result by hand (loop_doc_update result=)
OWNER_ACTOR = "you"


def result_green(result) -> bool:
    """True only for a result ribbon that is explicitly green (``green is True``).
    A missing/``None`` result, ``green: false`` (error/stopped/guardian_stopped
    ends) or a malformed value never counts as done."""
    return isinstance(result, dict) and result.get("green") is True


def _sanitize(text: str) -> str:
    """Filename-safe segment (``[A-Za-z0-9._-]``); can never traverse a path."""
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(text))
    return out.strip("._") or "doc"


def derive_state(doc: dict) -> str:
    """The doc's position on the note→proposal→objective spectrum — DERIVED from
    what has happened to it, never a stored/chosen type (§2.3):

    * **done**      — its ``result`` ribbon is green (:func:`result_green`); a
      ``green: false`` ribbon (error/stopped end) stays an objective;
    * **objective** — a loop is (or has been) pointed at it (``loops`` non-empty);
    * **proposal**  — it has been marked ``ready`` (a considered design);
    * **note**      — otherwise (raw thought, only you have touched it).

    Pure and total: reads the doc, mutates nothing, never raises."""
    if not isinstance(doc, dict):
        return "note"
    if result_green(doc.get("result")) or doc.get("result_green") is True:
        return "done"                   # ``result_green`` = the index-row projection
    if doc.get("loops"):
        return "objective"
    if doc.get("ready"):
        return "proposal"
    return "note"


def glyph_for(doc: dict) -> str:
    return GLYPH.get(derive_state(doc), "·")


def build_doc(project: str, title: str, *, body: str = "",
              loop_rewrite: bool = False, now: Optional[float] = None) -> dict:
    """The pure payload for a freshly-created doc. ``project`` is the frame but
    NOT required (§2.2 keeps zero-friction capture — ``＋ New doc`` types in <1s
    with no required project field); an empty one is stored as ``None`` so the doc
    shows in the cross-project "everything" view rather than a fake bucket. A new
    doc has no loops and is not ready → it derives as a **note**."""
    now = time.time() if now is None else now
    proj = project.strip() if isinstance(project, str) and project.strip() else None
    return {
        "project": proj,
        "title": (title or "").strip() or "(untitled)",
        "body": body or "",
        "ready": False,
        "loop_rewrite": bool(loop_rewrite),
        "loops": [],                 # loop names pointed at this doc (drives ●LOOP + objective)
        "result": None,              # the honest RESULT ribbon folded back on finish (§2.5)
        "history": [],               # interleaved you/loop edits (the per-doc timeline §2.4)
        "created": now,
        "updated": now,
    }


class LocalDocStore:
    """Local-first doc store: ``<base>/<id>.json`` (one file per doc, the living
    body + its loop-relationship) plus an append-only ``<base>/index.jsonl`` the
    Hub rail lists from. Serialized so concurrent writes never interleave an index
    line or collide on an id; fail-soft so a write never raises into a request."""

    def __init__(self, base_dir: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.base = base_dir or report.ideahub_dir()
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()

    def _new_id(self, doc: dict) -> str:
        stamp = int(doc.get("created") or self.clock())
        lead = doc.get("project") or "doc"
        base = _sanitize(f"{lead}-{(doc.get('title') or 'doc')[:24]}-{stamp}")
        cand, n = base, 1
        while os.path.exists(os.path.join(self.base, cand + ".json")):
            n += 1
            cand = f"{base}-{n}"
        return cand

    def _index_row(self, rec: dict) -> dict:
        """The rail-card projection of a doc: enough to render the index without
        opening every file (id, title, project, derived state+glyph, loop badge)."""
        state = derive_state(rec)
        loops = rec.get("loops") or []
        return {"id": rec["id"], "ts": rec.get("updated") or rec.get("created"),
                "project": rec.get("project"), "title": rec.get("title"),
                "state": state, "glyph": GLYPH.get(state, "·"),
                "loops": loops, "loop_count": len(loops),
                "loop_rewrite": bool(rec.get("loop_rewrite")),
                "has_result": rec.get("result") is not None,
                "result_green": result_green(rec.get("result")),
                "ready": bool(rec.get("ready")),
                "updated": rec.get("updated")}

    def _persist(self, rec: dict) -> dict:
        """Atomically write the doc file + append its current index row. Caller
        holds the lock. The index is append-only; :meth:`list_docs` folds by id."""
        os.makedirs(self.base, exist_ok=True)
        path = os.path.join(self.base, _sanitize(rec["id"]) + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)                        # atomic: a reader never sees a partial file
        row = self._index_row(rec)
        row["path"] = path
        with open(os.path.join(self.base, "index.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return rec

    def create(self, doc: dict) -> Optional[dict]:
        try:
            with self._lock:
                os.makedirs(self.base, exist_ok=True)
                rec = {**doc}
                rec["id"] = self._new_id(rec)
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001 — capture must never crash a request
            self.log(f"[ideahub] create failed: {type(e).__name__}: {e}")
            return None

    def get(self, doc_id: str) -> Optional[dict]:
        try:
            with open(os.path.join(self.base, _sanitize(doc_id) + ".json"),
                      encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def update(self, doc_id: str, *, title: Optional[str] = None,
               body: Optional[str] = None, ready: Optional[bool] = None,
               loop_rewrite: Optional[bool] = None, result: Optional[dict] = None,
               actor: str = "you") -> Optional[dict]:
        """Edit a doc in place. A loop (``actor`` prefixed ``loop:`` or an agent
        name passed by a loop) may only rewrite the BODY when ``loop_rewrite`` is
        set — the read-only-vs-loop-may-rewrite toggle (§2.4) is enforced here, so
        a canvas the human flagged read-only can never be silently overwritten by a
        loop. Every edit appends a timeline entry (who/when/what) for the per-doc
        history strip. Returns the updated record, or ``None`` on unknown doc /
        rejected loop write."""
        try:
            with self._lock:
                rec = self.get(doc_id)
                if rec is None:
                    return None
                is_loop = isinstance(actor, str) and actor != "you"
                if (body is not None and is_loop and not rec.get("loop_rewrite")):
                    self.log(f"[ideahub] loop {actor!r} blocked from read-only doc {doc_id!r}")
                    return None
                changed = []
                if title is not None:
                    rec["title"] = title.strip() or rec.get("title") or "(untitled)"
                    changed.append("title")
                if body is not None:
                    rec["body"] = body
                    changed.append("body")
                if ready is not None:
                    rec["ready"] = bool(ready)
                    changed.append("ready")
                if loop_rewrite is not None:
                    rec["loop_rewrite"] = bool(loop_rewrite)
                    changed.append("loop_rewrite")
                if result is not None:
                    rec["result"] = result
                    changed.append("result")
                now = self.clock()
                rec["updated"] = now
                rec.setdefault("history", []).append(
                    {"ts": now, "actor": actor, "changed": changed})
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[ideahub] update failed: {type(e).__name__}: {e}")
            return None

    def owner_result(self, doc_id: str, action: str, *,
                     actor: str = OWNER_ACTOR) -> Optional[dict]:
        """The OWNER's explicit result action (``loop_doc_update result=``):
        ``"set"`` marks the doc done (a green ribbon stamped ``by: you``),
        ``"clear"`` reopens it (``result`` → ``None``). Owner-only — any other
        actor (a loop, an agent) is refused so a loop can never mark its own
        objective done; the engine's computed fold is the only loop-side writer.
        Returns the updated record, or ``None`` on unknown doc / refused actor /
        bad action."""
        if actor != OWNER_ACTOR or action not in ("set", "clear"):
            self.log(f"[ideahub] result {action!r} by {actor!r} refused on {doc_id!r}")
            return None
        try:
            with self._lock:
                rec = self.get(doc_id)
                if rec is None:
                    return None
                now = self.clock()
                if action == "set":
                    loops = rec.get("loops") or []
                    rec["result"] = {"loop": loops[-1] if loops else None,
                                     "green": True, "verdict": "marked done",
                                     "by": OWNER_ACTOR, "ts": now}
                else:
                    rec["result"] = None
                rec["updated"] = now
                rec.setdefault("history", []).append(
                    {"ts": now, "actor": actor,
                     "changed": ["result"], "result": action})
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[ideahub] owner_result failed: {type(e).__name__}: {e}")
            return None

    def reindex(self, doc_id: str) -> Optional[dict]:
        """Re-append a doc's CURRENT index row without touching the record — heals
        a stale rail row (e.g. one written before ``done`` existed). Idempotent."""
        try:
            with self._lock:
                rec = self.get(doc_id)
                return None if rec is None else self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[ideahub] reindex failed: {type(e).__name__}: {e}")
            return None

    def point_loop(self, doc_id: str, loop: str, *, actor: str = "you") -> Optional[dict]:
        """Point a loop at this doc — the gesture that PROMOTES it to an objective
        (§2.2: "the act of pointing a loop is what promotes the doc to an
        objective"). Idempotent: pointing the same loop twice adds it once. After
        this the doc derives as ``objective`` and carries a ``●LOOP`` badge. Returns
        the updated record, or ``None`` on unknown doc."""
        try:
            with self._lock:
                rec = self.get(doc_id)
                if rec is None:
                    return None
                loops = rec.setdefault("loops", [])
                if loop and loop not in loops:
                    loops.append(loop)
                now = self.clock()
                rec["updated"] = now
                rec.setdefault("history", []).append(
                    {"ts": now, "actor": actor, "changed": ["pointed"], "loop": loop})
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[ideahub] point_loop failed: {type(e).__name__}: {e}")
            return None

    def unpoint_loop(self, doc_id: str, loop: str, *, actor: str = "you") -> Optional[dict]:
        """Take a loop off this doc (moving it to another plan). The opposite of
        :meth:`point_loop`; a doc left with no loops derives back from its body
        (note / proposal). No-op when the loop isn't on it. Returns the updated
        record, or ``None`` on unknown doc."""
        try:
            with self._lock:
                rec = self.get(doc_id)
                if rec is None:
                    return None
                loops = rec.get("loops") or []
                if loop not in loops:
                    return rec
                rec["loops"] = [l for l in loops if l != loop]
                now = self.clock()
                rec["updated"] = now
                rec.setdefault("history", []).append(
                    {"ts": now, "actor": actor, "changed": ["unpointed"], "loop": loop})
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[ideahub] unpoint_loop failed: {type(e).__name__}: {e}")
            return None

    def list_docs(self) -> list:
        """The CURRENT rail view of every doc: index rows folded by id so the
        latest row (from an update/point) supersedes the create row, first-seen
        order preserved (newest last, append order). Malformed lines skipped; a
        missing store → ``[]`` (never a crash)."""
        rows: dict = {}
        order: list = []
        try:
            with open(os.path.join(self.base, "index.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    iid = row.get("id")
                    if iid is None:
                        continue
                    if iid not in rows:
                        order.append(iid)
                    rows[iid] = row          # last row for an id wins (current state)
        except OSError:
            return []
        return [rows[i] for i in order]
