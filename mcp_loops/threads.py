"""§8 capstone — the OBJECTIVE-MANAGER THREAD: a durable, doc-anchored
conversation that reads and rewrites a Hub objective, files issues, and points
loops — all from one place, and all backed by an *attached session* (REDESIGN-SPEC
§4 Q2, §8 build order).

Q2 resolved the objective-manager as **an in-UI conversational thread docked to the
objective document, BACKED BY an attached session — not tmux**. So this module owns
the two halves of that resolution:

* the **interface** is a doc-anchored thread — durable + addressable: one thread per
  Hub doc (keyed by doc id), stored under ``<data>/_threads/<doc_id>.json`` so you
  can close the tab and return tomorrow to the *same* conversation; and
* the **runtime is a session** — every agent turn is really executed by a chosen
  attached connector (:mod:`mcp_loops.connect`). The thread never generates a reply
  itself; an agent turn is a *pending placeholder* until the backing session claims
  the dispatched task, does the work (rewrites the doc / files an issue / points a
  loop, via the very MCP tools it already holds), and **returns a real envelope**
  that folds back into the turn. No live session ⇒ an honest **"attach a session"**
  state and NO fabricated reply — never a faked chat.

Storage + fail-soft discipline mirror :mod:`mcp_loops.ideahub` / :mod:`mcp_loops.issues`:
one file per thread + serialized writes; a write never raises into a request; a
missing store reads as an empty, unattached thread. This module is DATA MODEL +
storage + the pure state/fold logic; :mod:`mcp_loops.server` wires the dispatch
(enqueue a connect task) and the read-time fold, and the §8 right-pane UI paints it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

from mcp_loops import report

# The honest thread states the right pane renders. NONE of these ever fabricates an
# assistant reply — they gate whether you *can* converse and say why when you can't.
#   unattached — no backing session chosen yet → the "attach a session" empty state
#   offline    — a session is attached but its heartbeat is stale/gone → can't run
#   awaiting   — a message was dispatched; the session is working, reply pending
#   ready      — a live session is attached and idle → send away
STATES = ("unattached", "offline", "awaiting", "ready")

# capabilities the objective-manager needs of its backing session: it edits the doc,
# files issues and points loops through the MCP tools, so a plain claude runtime with
# tool access suffices — declared so a picker can gate honestly if we tighten later.
THREAD_CAPABILITY = "objective-manager"


def _sanitize(text: str) -> str:
    """Filename-safe segment (``[A-Za-z0-9._-]``); can never traverse a path."""
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(text))
    return out.strip("._") or "thread"


def build_thread(doc_id: str, *, session: Optional[str] = None,
                 now: Optional[float] = None) -> dict:
    """The pure payload for a fresh (empty, unattached) thread docked to ``doc_id``.
    A thread with no ``session`` derives as **unattached** — the honest state that
    tells the UI to show "attach a session," not a chat box pretending to answer."""
    now = time.time() if now is None else now
    return {
        "doc_id": str(doc_id),
        "session": (session.strip() or None) if isinstance(session, str) else None,
        "messages": [],
        "created": now,
        "updated": now,
    }


def _mid(thread: dict) -> str:
    """Stable per-thread message id: an ordinal, human-scannable in a timeline."""
    return f"m{len(thread.get('messages') or []) + 1}"


def build_user_message(thread: dict, text: str, *,
                       dispatched: bool = False, now: Optional[float] = None) -> dict:
    """A YOU turn — the human's real words. ``dispatched`` records whether it was
    handed to a session (False when none was attached: the words are kept, but no
    reply is faked — the UI shows the honest state and re-dispatches on attach)."""
    now = time.time() if now is None else now
    return {"id": _mid(thread), "role": "you", "text": (text or "").strip(),
            "ts": now, "dispatched": bool(dispatched)}


def build_agent_turn(thread: dict, task_id: str, *,
                     now: Optional[float] = None) -> dict:
    """An AGENT turn placeholder — created ``pending`` the instant a task is
    dispatched to the backing session, carrying ``task_id`` so the real result can
    fold back in. It holds NO text until the session returns: the thread never
    invents the objective-manager's reply (§8 "never a faked chat")."""
    now = time.time() if now is None else now
    return {"id": _mid(thread), "role": "agent", "task_id": task_id,
            "status": "pending", "text": "", "actions": [],
            "ts": now, "done_ts": None}


def fold_envelope(turn: dict, envelope: dict, *, now: Optional[float] = None) -> dict:
    """Fold a returned connect ENVELOPE into a pending agent turn — the ONLY way an
    agent turn gets real content. The turn's text becomes the session's ``summary``;
    ``actions`` become the artifacts it reported it did (rewrote the doc / filed an
    issue / pointed a loop); ``status`` mirrors the envelope's terminal status; a
    failed turn also carries ``error`` (the session's own reason, else ``None``). Pure:
    returns the completed turn, mutates nothing; a non-terminal/absent envelope
    leaves the turn pending (still honest — no fabricated reply)."""
    now = time.time() if now is None else now
    if not isinstance(envelope, dict):
        return turn
    status = envelope.get("status")
    if status == "canceled":
        # The task was withdrawn before the session returned: close the turn with
        # NO text — there is no reply, and we will not pretend there was one.
        return {**turn, "status": "canceled", "text": "", "actions": [],
                "done_ts": now}
    if status not in ("returned", "failed"):
        return turn
    out = {**turn}
    out["status"] = "done" if status == "returned" else "failed"
    out["text"] = (envelope.get("summary") or "").strip()
    # A failed turn shows the session's OWN reason (verbatim) — appended when a
    # summary exists, else it is the text. No reason given → stays blank: the
    # view says "failed" honestly, never an invented cause.
    reason = envelope.get("error") if status == "failed" else None
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    if reason and reason not in out["text"]:
        out["text"] = f"{out['text']}\n{reason}" if out["text"] else reason
    out["error"] = reason
    arts = envelope.get("artifacts")
    out["actions"] = list(arts) if isinstance(arts, list) else []
    out["git_commit"] = envelope.get("git_commit")
    out["done_ts"] = now
    return out


def describe_state(thread: dict, connector: Optional[dict]) -> dict:
    """The HONEST thread state (see :data:`STATES`) — what the right pane renders and
    whether you can send. ``connector`` is the backing session's live connect record
    (or ``None`` when it is unknown/never registered). Rules, in order:

    * no ``session`` set → **unattached** (show "attach a session," ``can_send`` off);
    * session set but ``connector`` missing or not ``live`` → **offline** (say which
      session and that it's offline; ``can_send`` off — we will not fake a reply);
    * a pending agent turn exists → **awaiting** (a real task is in flight);
    * otherwise → **ready**.

    Pure and total: reads, never raises, never invents a fact about the session."""
    session = thread.get("session")
    if not session:
        return {"state": "unattached", "session": None, "can_send": False,
                "reason": "No session attached — attach one to steer this objective."}
    live = bool(connector and connector.get("live"))
    if not live:
        seen = connector.get("lastSeen") if isinstance(connector, dict) else None
        why = (f"Session {session!r} is offline"
               if connector else f"Session {session!r} is not connected")
        return {"state": "offline", "session": session, "can_send": False,
                "reason": why + " — its agent turns can't run until it reconnects.",
                "lastSeen": seen}
    pending = any(m.get("role") == "agent" and m.get("status") == "pending"
                  for m in (thread.get("messages") or []))
    if pending:
        return {"state": "awaiting", "session": session, "can_send": True,
                "reason": f"{session} is working — a reply is on the way."}
    return {"state": "ready", "session": session, "can_send": True,
            "reason": f"Backed by {session} — send a message to steer the objective."}


def pending_task_ids(thread: dict) -> list:
    """Task ids of the thread's still-pending agent turns, in order."""
    return [m["task_id"] for m in (thread or {}).get("messages") or []
            if m.get("role") == "agent" and m.get("status") == "pending"
            and m.get("task_id")]


def annotate_pending(thread: dict, task_for: Callable[[str], Optional[dict]]) -> dict:
    """Read-time, NON-persisted progress for each pending agent turn, taken from the
    REAL connect task: ``task_status`` is ``queued`` (dispatched, not yet claimed by
    the session) or ``working`` (the session claimed it), plus ``claimed_by`` /
    ``claimed_at``. An unknown task reads ``unknown`` — honest, never guessed. Pure:
    returns a new thread dict; the stored file is untouched."""
    msgs = []
    for m in (thread.get("messages") or []):
        if m.get("role") == "agent" and m.get("status") == "pending":
            task = task_for(m.get("task_id")) if m.get("task_id") else None
            st = (task or {}).get("status")
            m = {**m,
                 "task_status": ("working" if st == "claimed" else
                                 "queued" if st == "pending" else "unknown"),
                 "claimed_by": (task or {}).get("claimedBy"),
                 "claimed_at": (task or {}).get("claimedAt")}
        msgs.append(m)
    return {**thread, "messages": msgs}


def session_options(connectors: list, attached: Optional[str]) -> list:
    """The runtime/session SWITCHER's choices for a thread: every registered
    connector with its REAL heartbeat liveness, with the attached one flagged.
    Live sessions come first, then the rest most-recently-seen first (the order
    :func:`connect.list_connectors` already gives). An attached id that is no
    longer registered is still listed with ``registered: False`` so the UI can
    show honestly what the thread points at instead of hiding it. Pure."""
    out = []
    seen = set()
    for c in connectors or []:
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        meta = c.get("meta") if isinstance(c.get("meta"), dict) else {}
        out.append({"id": cid, "runtime": c.get("runtime") or "claude",
                    "label": meta.get("name") or meta.get("label") or cid,
                    "live": bool(c.get("live")), "idleSeconds": c.get("idleSeconds"),
                    "capabilities": list(c.get("capabilities") or []),
                    "attached": cid == attached, "registered": True})
    if attached and attached not in seen:
        out.append({"id": attached, "runtime": None, "label": attached,
                    "live": False, "idleSeconds": None, "capabilities": [],
                    "attached": True, "registered": False})
    out.sort(key=lambda o: 0 if o["live"] else 1)   # stable: keeps recency within groups
    return out


def summarize_thread(doc_row: dict, thread: Optional[dict],
                     connector: Optional[dict]) -> dict:
    """One row of the Chat index for a Hub doc: the doc's identity plus what its
    thread holds. ``thread`` is ``None`` when nobody has opened a thread yet, and
    the row says so (``has_thread: False``, zero messages). The ``last`` preview is
    the newest message with real text; a pending agent turn has no text, so it is
    never shown as a preview. Pure; never raises."""
    doc_row = doc_row or {}
    th = thread or {}
    msgs = th.get("messages") or []
    pending = sum(1 for m in msgs
                  if m.get("role") == "agent" and m.get("status") == "pending")
    last = None
    for m in reversed(msgs):
        if (m.get("text") or "").strip():
            text = m["text"].strip()
            last = {"id": m.get("id"), "role": m.get("role"), "status": m.get("status"),
                    "ts": m.get("done_ts") or m.get("ts"),
                    "text": text if len(text) <= 160 else text[:157].rstrip() + "…"}
            break
    state = describe_state(th, connector) if thread is not None else \
        {"state": "unattached", "session": None, "can_send": False}
    return {"doc_id": doc_row.get("id"), "title": doc_row.get("title") or "(untitled)",
            "project": doc_row.get("project") or None,
            "doc_state": doc_row.get("state"), "glyph": doc_row.get("glyph"),
            "has_thread": thread is not None, "session": th.get("session"),
            "state": state.get("state"), "can_send": bool(state.get("can_send")),
            "message_count": len(msgs), "pending": pending, "last": last,
            "updated": _num(th.get("updated") or doc_row.get("updated") or doc_row.get("ts"))}


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def thread_revision(view: dict) -> str:
    """A short, stable fingerprint of everything a live client renders: the message
    timeline (ids, statuses, pending sub-state, text), the attached session and the
    honest session state. Equal revs ⇒ nothing visible changed, so a long-poll can
    keep waiting and an SSE stream can stay quiet. Pure; never raises."""
    import hashlib
    th = (view or {}).get("thread") or {}
    st = (view or {}).get("session_state") or {}
    parts = [str(th.get("session")), str(st.get("state")), str(st.get("can_send"))]
    # the switcher's choices: a session coming online / going stale is visible
    parts.append(",".join(f"{o.get('id')}:{int(bool(o.get('live')))}"
                          for o in ((view or {}).get("sessions") or [])))
    for m in (th.get("messages") or []):
        parts.append("|".join(str(m.get(k)) for k in
                              ("id", "role", "status", "task_status", "dispatched", "text")))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def dispatch_prompt(doc: dict, thread: dict, text: str) -> str:
    """The task prompt handed to the backing session for one agent turn — it frames
    the objective-manager's job around the LIVE doc (title + body), the conversation
    so far, and the human's new message, and names the exact gestures it may make so
    the session acts on the real doc, not a summary of it. Pure string builder."""
    doc = doc or {}
    title = (doc.get("title") or "(untitled)").strip()
    project = doc.get("project") or "Unattributed"
    body = doc.get("body") or ""
    convo = "\n".join(
        f"  {m.get('role')}: {m.get('text')}"
        for m in (thread.get("messages") or [])
        if m.get("role") == "you" or (m.get("status") == "done" and m.get("text")))
    doc_id = doc.get("id") or thread.get("doc_id")
    return (
        "You are the OBJECTIVE-MANAGER for a Loopyard Hub objective. You steer this "
        "living document from one conversation: read it, rewrite it in place, file "
        "issues, and point loops at it.\n\n"
        f"PROJECT: {project}\nDOC id: {doc_id}\nTITLE: {title}\n"
        f"CURRENT BODY:\n{body}\n\n"
        f"CONVERSATION SO FAR:\n{convo or '  (none yet)'}\n\n"
        f"THE HUMAN JUST SAID:\n  {text.strip()}\n\n"
        "Do what they asked using your Loopyard tools — rewrite the doc with "
        f"loop_doc_update(id={doc_id!r}, body=..., actor={thread.get('session')!r}); "
        f"file a fault with loop_issue_file(project={project!r}, ...); point a loop "
        f"with loop_doc_build_loop(id={doc_id!r}) or loop_doc_point. Then RETURN a "
        "one-paragraph summary of what changed and list each concrete action you "
        "took as an artifact. If nothing is needed, say so — never invent an edit.")


class LocalThreadStore:
    """Local-first thread store: ``<base>/<doc_id>.json`` — one durable file per
    doc-thread (its backing session + the full message timeline). Serialized so
    concurrent posts never interleave; fail-soft so a write never raises into a
    request and a missing store reads as an empty, unattached thread."""

    def __init__(self, base_dir: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.base = base_dir or report.threads_dir()
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()

    def _path(self, doc_id: str) -> str:
        return os.path.join(self.base, _sanitize(doc_id) + ".json")

    def _persist(self, rec: dict) -> dict:
        """Atomically write the thread file (caller holds the lock)."""
        os.makedirs(self.base, exist_ok=True)
        path = self._path(rec["doc_id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)                # atomic: a reader never sees a partial file
        return rec

    def get(self, doc_id: str) -> Optional[dict]:
        """The stored thread for a doc, or ``None`` if none exists yet."""
        try:
            with open(self._path(doc_id), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def list_ids(self) -> list:
        """Doc ids that have a stored thread (read from the files themselves, so a
        sanitized filename never loses the real id). A missing store → ``[]``."""
        out = []
        try:
            names = sorted(os.listdir(self.base))
        except OSError:
            return []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.base, fn), encoding="utf-8") as fh:
                    did = (json.load(fh) or {}).get("doc_id")
            except (OSError, ValueError, AttributeError):
                continue
            if did:
                out.append(did)
        return out

    def get_or_create(self, doc_id: str) -> Optional[dict]:
        """The thread for ``doc_id``, creating an empty unattached one on first
        open — a Hub objective always *has* a thread the moment you look at it."""
        try:
            with self._lock:
                rec = self.get(doc_id)
                if rec is None:
                    rec = self._persist(build_thread(doc_id, now=self.clock()))
                return rec
        except Exception as e:  # noqa: BLE001 — never crash a request
            self.log(f"[threads] get_or_create failed: {type(e).__name__}: {e}")
            return None

    def attach(self, doc_id: str, session: Optional[str]) -> Optional[dict]:
        """Attach (or detach, with ``session`` falsy) the backing session. Attaching
        does not fake anything: it just names the compute the next turn will run on;
        an empty string clears it back to the honest unattached state."""
        try:
            with self._lock:
                rec = self.get(doc_id) or build_thread(doc_id, now=self.clock())
                rec["session"] = (session.strip() or None) if isinstance(session, str) else None
                rec["updated"] = self.clock()
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[threads] attach failed: {type(e).__name__}: {e}")
            return None

    def append(self, doc_id: str, message: dict) -> Optional[dict]:
        """Append one message (a YOU turn or a pending AGENT turn) and persist."""
        try:
            with self._lock:
                rec = self.get(doc_id) or build_thread(doc_id, now=self.clock())
                rec.setdefault("messages", []).append(message)
                rec["updated"] = self.clock()
                return self._persist(rec)
        except Exception as e:  # noqa: BLE001
            self.log(f"[threads] append failed: {type(e).__name__}: {e}")
            return None

    def fold(self, doc_id: str, envelope_for) -> Optional[dict]:
        """Fold every returnable pending agent turn using ``envelope_for(task_id)``
        (the server passes a lookup into the connect store). Persists only if a turn
        actually completed, so a read that changes nothing does no write. Returns the
        current thread (possibly updated). This is where a real session result — and
        ONLY a real result — turns a pending turn into an answered one."""
        try:
            with self._lock:
                rec = self.get(doc_id)
                if rec is None:
                    return None
                changed = False
                for i, m in enumerate(rec.get("messages") or []):
                    if m.get("role") == "agent" and m.get("status") == "pending":
                        env = envelope_for(m.get("task_id"))
                        folded = fold_envelope(m, env, now=self.clock())
                        if folded is not m and folded.get("status") != "pending":
                            rec["messages"][i] = folded
                            changed = True
                if changed:
                    rec["updated"] = self.clock()
                    return self._persist(rec)
                return rec
        except Exception as e:  # noqa: BLE001
            self.log(f"[threads] fold failed: {type(e).__name__}: {e}")
            return self.get(doc_id)
