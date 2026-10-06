"""Hub-as-Workspace — objective = a FOLDER of *statused* documents.

HUB-WORKSPACE-SPEC (owner, 2026-09-24, LOCKED) evolves the Idea Hub from a
single-markdown editor (:mod:`mcp_loops.ideahub`) into a real workspace:

* **Objective = free-form folder of documents.** Any number of files, no enforced
  naming. Each doc carries a status: ``draft`` · ``todo`` · ``in progress`` ·
  ``done``.
* **Status is evidence-derived, human-overridable.** The objective's headline
  status **rolls up** from its docs (:func:`rollup_status`: any *in progress* ⇒
  in progress; any *todo* and no in-progress ⇒ todo; all *done* ⇒ done; else
  draft) and the human can **override** (pin) it.
* **ADDITIVE-ONLY invariant (safety spine).** No agent EVER edits or overwrites a
  human-authored doc. Agents ONLY emit **new files** as suggestions; the human's
  **Accept** is the sole writer of accepted content. Enforced here in code
  (:class:`AdditiveOnlyViolation` — the doc-write path is gated on the human
  actor) and proven by ``test_workspace.py``.
* **Suggestions are actionable — Accept / Decline / Discuss.** Accept → writes the
  new file / applies the status (the human click writes). Decline → discard.
  Discuss → move the suggested edits into the **to-discuss folder** a human can
  later point a loop/session at.

Storage mirrors :mod:`mcp_loops.hub_store` / :mod:`mcp_loops.ideahub`: fail-soft,
atomic doc writes, an append-only suggestions log folded on read. An objective is
a real directory so its docs are real markdown files (the UI renders + edits
them directly)::

    <data>/_workspace/<objective_id>/
        objective.json        # {id, title, created, updated, status_override}
        docs/<slug>.md        # human-authored markdown — SOLE writer is the human
        docmeta.json          # {slug: {status, title, author, created, updated}}
        suggestions.jsonl     # append-only add/accept/decline/discuss events
        to-discuss/<id>.json  # discuss items (to-discuss is a FOLDER, not a status)

Open questions resolved (assumptions noted):

* **To-discuss = a folder, not a status.** A discuss item is a *bundle of
  suggested edits a human later points a loop at* — it is content awaiting a
  human, not a lifecycle state of a doc, so it lives in its own area and never
  pollutes the doc-status rollup.
* **Dedupe (Sweep) = normalized-title match** against existing objective titles +
  doc slugs (see :func:`normalize_title` / :meth:`find_duplicate`); embedding is a
  later depth, title-normalization is the deterministic floor a test can prove.

HUB-UNIFY (Workspace IS the Hub): every living doc in the old Idea Hub store
(``<data>/_ideahub``, :mod:`mcp_loops.ideahub`) is surfaced as a workspace
objective through a **read-adapter** — never a move, never a copy that can drift:

* objective id = ``ih.<ideahub id>`` (native ids are slugs and never contain a
  ``.``, so the namespaces cannot collide); title / created / updated come from
  the ideahub doc; its markdown ``body`` is the objective's ``index`` doc.
* the ``index`` doc's status defaults from the doc's derived Hub state (note →
  draft, proposal → todo, objective → in progress) and is human-overridable.
* everything *new* on an adapted objective (extra docs, status pins, doc
  statuses, suggestions, to-discuss) lives in an **overlay** folder
  ``<workspace>/_ideahub/<ideahub id>/`` — the ideahub file is never touched by it.
* editing the ``index`` doc writes THROUGH :meth:`LocalDocStore.update` (the
  ideahub's own human writer, which appends a timeline entry), and ONLY after the
  pre-edit file is copied to ``<workspace>/_ideahub-backups/<id>/<ms>.json`` —
  every prior version survives. Nothing here ever deletes or renames an ideahub
  file.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional

from . import paths

# ── the doc status ladder ──────────────────────────────────────────────────────
DOC_STATUSES = ("draft", "todo", "in progress", "done")
_STATUS_ALIASES = {
    "in-progress": "in progress",
    "inprogress": "in progress",
    "wip": "in progress",
    "todo": "todo",
    "to do": "todo",
    "done": "done",
    "draft": "draft",
}
# the human actor — the ONLY writer permitted to touch a doc under docs/
HUMAN = "you"

# the reserved staging objective that holds Sweep's pending *new-objective*
# candidates before a human accepts one into a real objective of its own. A
# fixed, slug-clean id so it is stable across runs (create_objective's ids carry
# a random suffix, so this never collides with a real objective).
INBOX_ID = "sweep-inbox"
INBOX_TITLE = "Sweep candidates (inbox)"

# HUB-UNIFY: ideahub docs surface as objectives under this id prefix. Native ids
# are ``_slugify`` output (``[a-z0-9-]``) so a ``.`` can never appear in one.
IH_PREFIX = "ih."
# the ideahub doc body is exposed as this doc slug on its adapted objective
IH_INDEX_SLUG = "index"
# the derived Hub state → the index doc's default workspace status
IH_STATE_STATUS = {"note": "draft", "proposal": "todo", "objective": "in progress",
                   "done": "done"}


def _hub_result(doc: dict) -> Optional[dict]:
    """The compact result ribbon an adapted objective carries (Mark done/Reopen)."""
    r = doc.get("result")
    if not isinstance(r, dict):
        return None
    return {k: r.get(k) for k in ("loop", "ended", "green", "verdict", "summary", "by", "ts")
            if r.get(k) is not None}


def is_ideahub_oid(oid: str) -> bool:
    return isinstance(oid, str) and oid.startswith(IH_PREFIX) and len(oid) > len(IH_PREFIX)


class AdditiveOnlyViolation(RuntimeError):
    """Raised when a non-human actor tries to write/overwrite a human doc, or an
    accept would overwrite an existing doc. The additive-only safety spine: agents
    emit new suggestion files only; the human's Accept is the sole doc writer."""


def normalize_status(status: str) -> str:
    """Canonicalise a status string to one of :data:`DOC_STATUSES`; unknown → draft."""
    key = str(status or "").strip().lower()
    if key in DOC_STATUSES:
        return key
    return _STATUS_ALIASES.get(key, "draft")


def rollup_status(statuses: List[str]) -> str:
    """The objective's headline status DERIVED from its docs — the LOCKED rule:

    * any ``in progress`` ⇒ **in progress**
    * any ``todo`` and no in-progress ⇒ **todo**
    * all ``done`` (and at least one doc) ⇒ **done**
    * otherwise ⇒ **draft**

    Pure and total; an empty objective (no docs) derives as ``draft``."""
    s = [normalize_status(x) for x in (statuses or [])]
    if not s:
        return "draft"
    if any(x == "in progress" for x in s):
        return "in progress"
    if any(x == "todo" for x in s):
        return "todo"
    if all(x == "done" for x in s):
        return "done"
    return "draft"


def normalize_title(title: str) -> str:
    """Dedupe key for Sweep: lowercased, alnum-collapsed. ``"Check-out Rewrite!"``
    and ``"check out rewrite"`` collide → the same candidate objective."""
    return " ".join("".join(c if c.isalnum() else " " for c in str(title).lower()).split())


def _slugify(text: str) -> str:
    """Filename-safe doc slug (``[a-z0-9-]``); can never traverse a path."""
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(text).lower())
    out = "-".join(p for p in out.replace("_", "-").split("-") if p)
    return out[:48].strip("-") or "doc"


# ── suggestion kinds + states ───────────────────────────────────────────────────
# A suggestion is an agent-emitted proposal that a human resolves. It NEVER writes
# a doc itself — only the human's accept does.
SUG_KINDS = ("new_objective", "new_doc", "enrichment", "status")
SUG_STATES = ("pending", "accepted", "declined", "discussing")


# One RLock per workspace base dir, PROCESS-wide: every Workspace instance over the
# same dir shares it, so writes serialize across HTTP requests / threads even when
# callers construct their own instance (loopyard-follow-up-1790338898).
_BASE_LOCKS: dict = {}
_BASE_LOCKS_GUARD = threading.Lock()


def _lock_for(base: str) -> "threading.RLock":
    key = os.path.realpath(base)
    with _BASE_LOCKS_GUARD:
        lock = _BASE_LOCKS.get(key)
        if lock is None:
            lock = _BASE_LOCKS[key] = threading.RLock()
        return lock


class Workspace:
    """The workspace store. One :class:`Workspace` over a base dir; every objective
    is a subdirectory. Serialized writes (no interleaved index/collided ids) via a
    per-base-dir process-wide lock — NOT across processes (a multi-worker server
    relies on write_doc's O_EXCL create for no-clobber). Fail-soft reads (a missing
    store reads empty, a bad line never poisons)."""

    def __init__(self, base_dir: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None,
                 ideahub_dir: Optional[str] = None):
        # The ideahub adapter reads the real store only for the default (install)
        # workspace; an explicit base_dir (tests, sandboxes) gets NO adapter unless
        # an ideahub_dir is passed too — a test can never read the user's Hub.
        if ideahub_dir is None and base_dir is None:
            from . import report
            ideahub_dir = report.ideahub_dir()
        self.base = base_dir or os.path.join(paths.resolve_data_dir(), "_workspace")
        self.ideahub_dir = ideahub_dir
        self.clock = clock
        self.log = log
        self._lock = _lock_for(self.base)

    # ── the ideahub read-adapter ─────────────────────────────────────────────────
    def _hub(self):
        if not self.ideahub_dir:
            return None
        from .ideahub import LocalDocStore
        return LocalDocStore(base_dir=self.ideahub_dir, clock=self.clock, log=self.log)

    def _hub_doc(self, oid: str) -> Optional[dict]:
        """The ideahub doc behind an adapted objective id, or ``None``."""
        hub = self._hub() if is_ideahub_oid(oid) else None
        if hub is None:
            return None
        doc = hub.get(oid[len(IH_PREFIX):])
        return doc if isinstance(doc, dict) and doc.get("id") else None

    def _hub_rec(self, doc: dict) -> dict:
        """Project an ideahub doc onto an objective record (+ overlay pin)."""
        from .ideahub import derive_state
        oid = IH_PREFIX + doc["id"]
        ov = self._read_json(os.path.join(self._odir(oid), "objective.json"), {})
        return {"id": oid, "title": doc.get("title") or "(untitled)",
                "created": doc.get("created"),
                "updated": max(doc.get("updated") or 0, ov.get("updated") or 0) or None,
                "status_override": ov.get("status_override"),
                "source": "ideahub", "ideahub_id": doc["id"],
                "project": doc.get("project"), "hub_state": derive_state(doc),
                "hub_result": _hub_result(doc),
                "loops": list(doc.get("loops") or [])}

    def _load_rec(self, oid: str) -> dict:
        if is_ideahub_oid(oid):
            doc = self._hub_doc(oid)
            return self._hub_rec(doc) if doc else {}
        return self._read_json(self._obj_path(oid), {})

    def _save_rec(self, oid: str, rec: dict) -> None:
        if is_ideahub_oid(oid):
            # only the overlay fields — the ideahub doc itself is never written here
            self._write_json(self._obj_path(oid), {
                "id": oid, "status_override": rec.get("status_override"),
                "updated": rec.get("updated")})
            return
        self._write_json(self._obj_path(oid), rec)

    def _load_meta(self, oid: str) -> dict:
        meta = self._read_json(self._docmeta_path(oid), {})
        if is_ideahub_oid(oid):
            doc = self._hub_doc(oid)
            if doc is None:
                return {}
            from .ideahub import derive_state
            ov = meta.get(IH_INDEX_SLUG) or {}
            meta[IH_INDEX_SLUG] = {
                "title": doc.get("title") or "(untitled)",
                "status": ov.get("status") or IH_STATE_STATUS.get(derive_state(doc), "draft"),
                "author": HUMAN, "created": doc.get("created"),
                "updated": max(doc.get("updated") or 0, ov.get("updated") or 0) or None,
                "source": "ideahub"}
        return meta

    def _save_meta(self, oid: str, meta: dict) -> None:
        if is_ideahub_oid(oid) and IH_INDEX_SLUG in meta:
            meta = dict(meta)
            ix = meta[IH_INDEX_SLUG]
            # the index doc's title/body live in the ideahub; overlay keeps status only
            meta[IH_INDEX_SLUG] = {"status": ix.get("status"), "updated": ix.get("updated")}
        self._write_json(self._docmeta_path(oid), meta)

    def _backup_hub_doc(self, iid: str) -> Optional[str]:
        """Copy the ideahub doc file aside BEFORE any write-through. Never moves."""
        from .ideahub import _sanitize
        src = os.path.join(self.ideahub_dir, _sanitize(iid) + ".json")
        if not os.path.exists(src):
            return None
        dst_dir = os.path.join(self.base, "_ideahub-backups", _sanitize(iid))
        os.makedirs(dst_dir, exist_ok=True)
        stamp = int(self.clock() * 1000)
        dst = os.path.join(dst_dir, f"{stamp}.json")
        n = 1
        while os.path.exists(dst):
            n += 1
            dst = os.path.join(dst_dir, f"{stamp}-{n}.json")
        shutil.copy2(src, dst)
        return dst

    def _write_hub_index(self, oid: str, title: str, body: str) -> None:
        """Write-through of an adapted objective's index doc into the ideahub via
        its own human writer, after a per-edit backup copy."""
        iid = oid[len(IH_PREFIX):]
        self._backup_hub_doc(iid)
        hub = self._hub()
        if hub is None or hub.update(iid, title=title, body=body or "", actor=HUMAN) is None:
            raise ValueError(f"ideahub write-through failed for {iid!r}")

    def list_hub_objectives(self) -> List[dict]:
        """Every ideahub doc as an (undecorated) objective record — one per unique
        id in the ideahub's own :meth:`LocalDocStore.list_docs` fold."""
        hub = self._hub()
        if hub is None:
            return []
        out: List[dict] = []
        for row in hub.list_docs():
            iid = row.get("id")
            if not iid:
                continue
            doc = hub.get(iid)
            if not isinstance(doc, dict) or not doc.get("id"):
                # the file is unreadable — still surface the index row, never hide it
                doc = {"id": iid, "title": row.get("title"), "project": row.get("project"),
                       "created": row.get("ts"), "updated": row.get("updated"),
                       "loops": row.get("loops") or []}
            out.append(self._hub_rec(doc))
        return out

    # ── paths ───────────────────────────────────────────────────────────────────
    def _odir(self, oid: str) -> str:
        if is_ideahub_oid(oid):
            from .ideahub import _sanitize
            return os.path.join(self.base, "_ideahub", _sanitize(oid[len(IH_PREFIX):]))
        return os.path.join(self.base, _slugify(oid))

    def _docs_dir(self, oid: str) -> str:
        return os.path.join(self._odir(oid), "docs")

    def _discuss_dir(self, oid: str) -> str:
        return os.path.join(self._odir(oid), "to-discuss")

    def _obj_path(self, oid: str) -> str:
        return os.path.join(self._odir(oid), "objective.json")

    def _docmeta_path(self, oid: str) -> str:
        return os.path.join(self._odir(oid), "docmeta.json")

    def _sug_log(self, oid: str) -> str:
        return os.path.join(self._odir(oid), "suggestions.jsonl")

    # ── low-level json helpers (atomic write, fail-soft read) ────────────────────
    def _write_json(self, path: str, obj: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _read_json(self, path: str, default: dict) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return dict(default)

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{int(self.clock() * 1000):x}_{os.urandom(3).hex()}"

    # ── objectives ───────────────────────────────────────────────────────────────
    def create_objective(self, title: str, project: Optional[str] = None) -> dict:
        """Create an empty objective folder. Title is required (an objective is a
        named target); docs are added later via the human doc writer or Accept.
        ``project`` (optional) files the plan under a project up front."""
        title = (title or "").strip()
        project = (project or "").strip() if isinstance(project, str) else ""
        if not title:
            raise ValueError("objective title is empty")
        with self._lock:
            # Reserve room for the "-<hex>" uniqueness suffix INSIDE the 48-char cap
            # that _slugify (and _odir's re-slugify) enforce — otherwise the suffix is
            # truncated away and two long titles collide into one folder, so the second
            # create would silently wipe the first objective. Retry on the (tiny) chance
            # the id already exists.
            base_slug = _slugify(title)[:40].strip("-") or "doc"
            oid = base_slug + "-" + os.urandom(3).hex()
            while os.path.exists(self._obj_path(oid)):
                oid = base_slug + "-" + os.urandom(3).hex()
            now = self.clock()
            rec = {"id": oid, "title": title, "created": now, "updated": now,
                   "status_override": None}
            if project:
                rec["project"] = project
            os.makedirs(self._docs_dir(oid), exist_ok=True)
            self._write_json(self._obj_path(oid), rec)
            self._write_json(self._docmeta_path(oid), {})
            return rec

    def ensure_inbox(self) -> str:
        """The reserved staging objective for Sweep's new-objective candidates.
        Idempotent: creates it once with the fixed :data:`INBOX_ID`, returns it.
        A pending ``new_objective`` suggestion lives here until a human Accept
        promotes it to a real objective (see :meth:`accept_suggestion`)."""
        with self._lock:
            rec = self._read_json(self._obj_path(INBOX_ID), {})
            if not rec.get("id"):
                now = self.clock()
                rec = {"id": INBOX_ID, "title": INBOX_TITLE, "created": now,
                       "updated": now, "status_override": None, "inbox": True}
                os.makedirs(self._docs_dir(INBOX_ID), exist_ok=True)
                self._write_json(self._obj_path(INBOX_ID), rec)
                self._write_json(self._docmeta_path(INBOX_ID), {})
            return INBOX_ID

    def list_objectives(self) -> List[dict]:
        """Every objective with its rolled-up + effective status, newest-updated
        last is NOT enforced — insertion (dir) order via mtime is unstable, so we
        sort by ``created`` for a stable queue view."""
        out: List[dict] = []
        try:
            names = os.listdir(self.base)
        except OSError:
            names = []
        for name in names:
            rec = self._read_json(os.path.join(self.base, name, "objective.json"), {})
            if rec.get("id"):
                out.append(self._decorate(rec))
        for rec in self.list_hub_objectives():
            out.append(self._decorate(rec))
        out.sort(key=lambda r: r.get("created") or 0)
        return out

    def _decorate(self, rec: dict) -> dict:
        """Attach the derived rollup + effective (override-aware) status + doc count."""
        oid = rec["id"]
        meta = self._load_meta(oid)
        statuses = [m.get("status", "draft") for m in meta.values()]
        rolled = rollup_status(statuses)
        override = rec.get("status_override")
        out = dict(rec)
        out["rollup"] = rolled
        out["status"] = normalize_status(override) if override else rolled
        out["overridden"] = bool(override)
        out["doc_count"] = len(meta)
        # the loops pointed at this objective (plan↔loop link; see point_loop)
        out["loops"] = [x for x in (rec.get("loops") or []) if isinstance(x, str) and x]
        return out

    def get_objective(self, oid: str) -> Optional[dict]:
        """The full objective: its record (with rollup/effective status) + its docs
        (each with status + provenance, body NOT inlined — read via :meth:`get_doc`)."""
        rec = self._load_rec(oid)
        if not rec.get("id"):
            return None
        out = self._decorate(rec)
        meta = self._load_meta(oid)
        docs = []
        for slug, m in meta.items():
            docs.append({"slug": slug, "title": m.get("title", slug),
                         "status": normalize_status(m.get("status", "draft")),
                         "author": m.get("author", HUMAN),
                         "created": m.get("created"), "updated": m.get("updated")})
        docs.sort(key=lambda d: d.get("created") or 0)
        out["docs"] = docs
        return out

    def override_objective_status(self, oid: str, status: Optional[str]) -> Optional[dict]:
        """Pin (or clear, with ``None``) the objective's headline status. Human-only
        gesture; the override wins over the rollup until cleared."""
        with self._lock:
            rec = self._load_rec(oid)
            if not rec.get("id"):
                return None
            rec["status_override"] = normalize_status(status) if status else None
            rec["updated"] = self.clock()
            self._save_rec(oid, rec)
            return self._decorate(self._load_rec(oid))

    # ── the plan↔loop link ──────────────────────────────────────────────────────
    def point_loop(self, oid: str, loop: str, *, project: Optional[str] = None,
                   actor: str = HUMAN) -> Optional[dict]:
        """Record that ``loop`` was pointed at this objective (a *plan* in the UI) —
        the durable link the Plans view reads to list "loops on this plan" and to
        show a plan as running while its loop runs. Idempotent (a loop is recorded
        once). An adapted ideahub objective records it on the ideahub doc itself
        (:meth:`LocalDocStore.point_loop`, the same field ``loop_doc_build_loop``
        writes) so both surfaces agree; a native one keeps ``loops`` in its
        ``objective.json`` (plus the ``project`` the loop was bound to, if given).
        Returns the refreshed objective, or ``None`` for an unknown one."""
        loop = (loop or "").strip()
        if is_ideahub_oid(oid):
            hub = self._hub()
            if hub is None or hub.point_loop(oid[len(IH_PREFIX):], loop, actor=actor) is None:
                return None
            return self.get_objective(oid)
        with self._lock:
            rec = self._read_json(self._obj_path(oid), {})
            if not rec.get("id") or rec.get("inbox"):
                return None
            loops = [x for x in (rec.get("loops") or []) if isinstance(x, str) and x]
            if loop and loop not in loops:
                loops.append(loop)
            rec["loops"] = loops
            if project:
                rec["project"] = project
            rec["updated"] = self.clock()
            self._write_json(self._obj_path(oid), rec)
        return self.get_objective(oid)

    def loop_seed(self, oid: str) -> Optional[dict]:
        """What a loop built from this objective is seeded with: its title, the
        markdown of its docs (a lone doc verbatim; several under ``##`` headings, in
        doc order) and its project. ``None`` for an unknown objective."""
        obj = self.get_objective(oid)
        if obj is None:
            return None
        docs = obj.get("docs") or []
        parts = []
        for d in docs:
            body = ((self.get_doc(oid, d["slug"]) or {}).get("body") or "").strip()
            if not body:
                continue
            parts.append(body if len(docs) == 1 else f"## {d.get('title') or d['slug']}\n\n{body}")
        return {"title": obj.get("title") or "", "detail": "\n\n".join(parts),
                "project": obj.get("project") or ""}

    # ── docs (the human-only writer — the additive-only choke point) ─────────────
    def write_doc(self, oid: str, title: str, body: str, *, actor: str,
                  slug: Optional[str] = None, status: Optional[str] = None,
                  overwrite: bool = False) -> dict:
        """Write a doc into the objective. THE additive-only choke point:

        * a **non-human ``actor`` is rejected** (:class:`AdditiveOnlyViolation`) —
          no agent code path can ever reach ``docs/``;
        * writing a slug that already exists is rejected unless ``overwrite=True``,
          so even the human's Accept of a *new-file* suggestion creates a NEW file
          and never clobbers an existing human doc.

        Returns the doc's meta record. This is the ONLY method that mutates
        ``docs/``; :meth:`add_suggestion` (the agent path) never touches it."""
        if actor != HUMAN:
            raise AdditiveOnlyViolation(
                f"actor {actor!r} may not write docs — agents emit suggestions only")
        title = (title or "").strip() or "(untitled)"
        with self._lock:
            rec = self._load_rec(oid)
            if not rec.get("id"):
                raise ValueError(f"unknown objective {oid!r}")
            meta = self._load_meta(oid)
            slug = _slugify(slug or title)
            if slug in meta and not overwrite:
                # additive: never silently clobber. Disambiguate to a fresh slug.
                base, n = slug, 2
                while f"{base}-{n}" in meta:
                    n += 1
                slug = f"{base}-{n}"
            if is_ideahub_oid(oid) and slug == IH_INDEX_SLUG:
                # the adapted index doc IS the ideahub doc: back up, write through
                self._write_hub_index(oid, title, body)
            elif overwrite:
                path = os.path.join(self._docs_dir(oid), slug + ".md")
                os.makedirs(self._docs_dir(oid), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(body or "")
                os.replace(tmp, path)
            else:
                # create-new: O_CREAT|O_EXCL, so a file already on disk (another
                # process, or a doc whose meta entry was lost) is never clobbered
                # — the colliding create moves on to the next free slug instead.
                os.makedirs(self._docs_dir(oid), exist_ok=True)
                base, n = slug, 2
                while True:
                    path = os.path.join(self._docs_dir(oid), slug + ".md")
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                    except FileExistsError:
                        while f"{base}-{n}" in meta:
                            n += 1
                        slug, n = f"{base}-{n}", n + 1
                        continue
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(body or "")
                    break
            now = self.clock()
            m = meta.get(slug, {})
            # Preserve an existing doc's status when the caller omits one — a plain
            # in-place edit must not silently reset a done/in-progress doc to draft.
            eff_status = status if status else (m.get("status") or "draft")
            entry = {"title": title, "status": normalize_status(eff_status),
                     "author": m.get("author", actor),
                     "created": m.get("created", now), "updated": now}
            meta[slug] = entry
            self._save_meta(oid, meta)
            rec["updated"] = now
            self._save_rec(oid, rec)
            return {"slug": slug, **entry}

    def get_doc(self, oid: str, slug: str) -> Optional[dict]:
        """A doc's meta + markdown body, for the pretty-render/Edit view."""
        meta = self._load_meta(oid)
        m = meta.get(slug)
        if m is None:
            return None
        if is_ideahub_oid(oid) and slug == IH_INDEX_SLUG:
            doc = self._hub_doc(oid) or {}
            return {"slug": slug, "body": doc.get("body") or "",
                    "status": normalize_status(m.get("status", "draft")),
                    "title": m.get("title", slug), "author": HUMAN,
                    "created": m.get("created"), "updated": m.get("updated"),
                    "source": "ideahub"}
        try:
            with open(os.path.join(self._docs_dir(oid), _slugify(slug) + ".md"),
                      encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = ""
        return {"slug": slug, "body": body,
                "status": normalize_status(m.get("status", "draft")),
                "title": m.get("title", slug), "author": m.get("author", HUMAN),
                "created": m.get("created"), "updated": m.get("updated")}

    def set_doc_status(self, oid: str, slug: str, status: str) -> Optional[dict]:
        """Set a doc's status (human gesture, or the applied side of an accepted
        status suggestion). Touches meta only — never the body."""
        with self._lock:
            meta = self._load_meta(oid)
            if slug not in meta:
                return None
            meta[slug]["status"] = normalize_status(status)
            meta[slug]["updated"] = self.clock()
            self._save_meta(oid, meta)
            return {"slug": slug, **meta[slug]}

    # ── suggestion store (the agent path — NEVER writes docs/) ───────────────────
    def add_suggestion(self, oid: str, *, kind: str, title: str = "",
                       body: str = "", target_slug: Optional[str] = None,
                       status: Optional[str] = None, origin: str = "",
                       agent: str = "", evidence: Optional[list] = None) -> dict:
        """Record an agent-emitted suggestion (Sweep/Reconcile). Appends ONE event
        to the suggestions log; it NEVER writes into ``docs/``. ``kind`` is one of
        :data:`SUG_KINDS`. ``evidence`` carries Reconcile's commit links."""
        if kind not in SUG_KINDS:
            raise ValueError(f"unknown suggestion kind {kind!r}")
        with self._lock:
            rec = self._load_rec(oid)
            if not rec.get("id"):
                raise ValueError(f"unknown objective {oid!r}")
            sid = self._new_id("sug")
            ev = {"op": "add", "id": sid, "kind": kind, "title": title,
                  "body": body, "target_slug": target_slug,
                  "status": normalize_status(status) if status else None,
                  "origin": origin, "agent": agent, "evidence": evidence or [],
                  "ts": self.clock()}
            os.makedirs(self._odir(oid), exist_ok=True)
            with open(self._sug_log(oid), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            return {"id": sid, "state": "pending", **{k: ev[k] for k in
                    ("kind", "title", "body", "target_slug", "status", "origin",
                     "agent", "evidence", "ts")}}

    def _fold_suggestions(self, oid: str) -> Dict[str, dict]:
        items: Dict[str, dict] = {}
        try:
            with open(self._sug_log(oid), encoding="utf-8") as fh:
                lines = list(fh)
        except OSError:
            return {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            sid, op = ev.get("id"), ev.get("op")
            if not sid:
                continue
            if op == "add":
                items[sid] = {"id": sid, "state": "pending",
                              **{k: ev.get(k) for k in ("kind", "title", "body",
                                 "target_slug", "status", "origin", "agent",
                                 "evidence", "ts")}}
            elif sid in items:
                if op in ("accept", "decline", "discuss"):
                    items[sid]["state"] = {"accept": "accepted", "decline": "declined",
                                           "discuss": "discussing"}[op]
                    items[sid]["resolved_ts"] = ev.get("ts")
                    if ev.get("result"):
                        items[sid]["result"] = ev["result"]
        return items

    def list_suggestions(self, oid: str, *, state: Optional[str] = None) -> List[dict]:
        items = list(self._fold_suggestions(oid).values())
        if state:
            items = [s for s in items if s.get("state") == state]
        items.sort(key=lambda s: s.get("ts") or 0)
        return items

    def _log_sug_event(self, oid: str, sid: str, op: str, result: Optional[dict] = None) -> None:
        ev = {"op": op, "id": sid, "ts": self.clock()}
        if result:
            ev["result"] = result
        with open(self._sug_log(oid), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def accept_suggestion(self, oid: str, sid: str, *, actor: str) -> Optional[dict]:
        """Accept — the human's click is the SOLE writer of accepted content. Only a
        human actor may accept; the write goes through :meth:`write_doc` (which
        re-asserts the additive-only guard) or applies a status:

        * ``new_objective`` → creates a **real objective** (a fresh folder) and
          writes the drafted body as its first doc — a Sweep candidate becomes a
          first-class objective, and a brand-new folder cannot overwrite anything;
        * ``new_doc`` / ``enrichment`` → a NEW file in ``oid`` (never overwrites);
        * ``status`` → applies the proposed doc status.

        Every branch has the human as the writer; no agent path reaches ``docs/``."""
        if actor != HUMAN:
            raise AdditiveOnlyViolation(f"only the human may accept — got {actor!r}")
        with self._lock:
            sug = self._fold_suggestions(oid).get(sid)
            if sug is None or sug.get("state") != "pending":
                return None
            result: dict
            kind = sug.get("kind")
            if kind == "status":
                slug = sug.get("target_slug")
                applied = self.set_doc_status(oid, slug, sug.get("status") or "draft")
                if applied is None:
                    return None
                result = {"applied_status": applied["status"], "slug": slug}
            elif kind == "new_objective":
                # promote the Sweep candidate into a real objective of its own
                new_obj = self.create_objective(sug.get("title") or "(untitled objective)")
                doc = self.write_doc(new_obj["id"], sug.get("title") or "(untitled)",
                                     sug.get("body") or "", actor=actor,
                                     status=sug.get("status") or "draft")
                result = {"created_objective": new_obj["id"],
                          "objective_title": new_obj["title"],
                          "created_slug": doc["slug"], "title": doc["title"]}
            else:
                # new_doc / enrichment → a NEW human doc in this objective (actor=HUMAN)
                doc = self.write_doc(oid, sug.get("title") or "(untitled)",
                                     sug.get("body") or "", actor=actor,
                                     slug=sug.get("target_slug"),
                                     status=sug.get("status") or "draft")
                result = {"created_slug": doc["slug"], "title": doc["title"]}
            self._log_sug_event(oid, sid, "accept", result)
            return {"id": sid, "state": "accepted", "result": result}

    def decline_suggestion(self, oid: str, sid: str) -> Optional[dict]:
        """Decline — discard the suggestion. No file is written or removed; the
        agent's emitted file was only ever a pending record."""
        with self._lock:
            sug = self._fold_suggestions(oid).get(sid)
            if sug is None or sug.get("state") != "pending":
                return None
            self._log_sug_event(oid, sid, "decline")
            return {"id": sid, "state": "declined"}

    def discuss_suggestion(self, oid: str, sid: str) -> Optional[dict]:
        """Discuss — move the suggested edits into the to-discuss FOLDER, a human
        can later point a loop/session at it. Still never writes a doc."""
        with self._lock:
            sug = self._fold_suggestions(oid).get(sid)
            if sug is None or sug.get("state") != "pending":
                return None
            did = self._new_id("disc")
            item = {"id": did, "from_suggestion": sid, "kind": sug.get("kind"),
                    "title": sug.get("title"), "body": sug.get("body"),
                    "target_slug": sug.get("target_slug"),
                    "status": sug.get("status"), "evidence": sug.get("evidence"),
                    "ts": self.clock()}
            self._write_json(os.path.join(self._discuss_dir(oid), did + ".json"), item)
            self._log_sug_event(oid, sid, "discuss", {"discuss_id": did})
            return {"id": sid, "state": "discussing", "discuss_id": did}

    def list_to_discuss(self, oid: str) -> List[dict]:
        d = self._discuss_dir(oid)
        out: List[dict] = []
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return []
        for name in names:
            if name.endswith(".json"):
                out.append(self._read_json(os.path.join(d, name), {}))
        out.sort(key=lambda i: i.get("ts") or 0)
        return out

    # ── Sweep dedupe support ─────────────────────────────────────────────────────
    def find_duplicate(self, title: str) -> Optional[str]:
        """Return the id of an existing objective whose normalized title matches —
        the deterministic dedupe floor Sweep uses before emitting a new-objective
        suggestion. ``None`` if the candidate is novel."""
        key = normalize_title(title)
        if not key:
            return None
        for o in self.list_objectives():
            if normalize_title(o.get("title", "")) == key:
                return o["id"]
        return None


# ── process-level shared instance (the dashboard's per-request accessor) ────────
_SHARED: dict = {}
_SHARED_GUARD = threading.Lock()


def shared() -> Workspace:
    """The process-wide default :class:`Workspace` for the CURRENT data dir (keyed
    by the resolved base + ideahub dir, so a re-pointed ``$LOOPS_DATA_DIR`` gets
    its own). The dashboard reuses this instead of constructing one per request."""
    from . import report
    key = (os.path.join(paths.resolve_data_dir(), "_workspace"), report.ideahub_dir())
    with _SHARED_GUARD:
        ws = _SHARED.get(key)
        if ws is None:
            ws = _SHARED[key] = Workspace()
        return ws
