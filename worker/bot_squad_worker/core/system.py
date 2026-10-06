"""CoreSystem — the heartbeat loop, wired.

One object that ties the data layer + the four systems + the review surface into
the loop from the design (README.md):

    intake -> CONTEXT PROCESSOR -> (proposed entities) -> CONFIRM gate
           -> MANAGERS (drive Tasks/Events) -> worker actions -> new intake
    (LIBRARIAN keeps the artifact substrate clean on a periodic tick)

This is the facade a human (or the coordinator) reviews and drives. It owns one
``core.store`` connection; everything below delegates to the per-system modules.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

from . import context_processor as cp
from . import event_manager as em
from . import inspect as insp
from . import librarian as lib
from . import migrate
from . import store
from . import task_manager as tm

Summarizer = Callable[[list[str]], str]


class CoreSystem:
    def __init__(self, db_path: str = ":memory:"):
        self.conn: sqlite3.Connection = store.connect(db_path)

    # --- INTAKE — Context Processor (left half of the loop) ----------------
    def ingest(self, raw_text: str, scope_tags: list[str],
               extractor: Optional[cp.Extractor] = None) -> dict:
        """Raw context -> extract -> reconcile -> proposed entities. Returns the
        reconcile summary {new, updated, garbage, duplicate}."""
        if extractor is None:
            return cp.ingest(self.conn, raw_text, scope_tags)
        return cp.ingest(self.conn, raw_text, scope_tags, extractor=extractor)

    def migrate_tracking(self, tracking_conn: sqlite3.Connection,
                         slug: str) -> dict:
        """One-time/idempotent import of the live tracking db into the core."""
        return migrate.migrate_tracking(tracking_conn, self.conn, slug)

    def affected_by(self, entity_id: str) -> list[str]:
        return cp.affected_by(self.conn, entity_id)

    # --- CONFIRM gate (proposed -> confirmed) ------------------------------
    def pending(self) -> list[store.Entity]:
        return store.by_trust(self.conn, "proposed")

    def confirm(self, eid: str) -> None:
        store.confirm(self.conn, eid)

    def reject(self, eid: str) -> None:
        store.reject(self.conn, eid)

    # --- DRIVE — Task / Event Managers (right half) ------------------------
    def next_actions(self, scope_tag: Optional[str] = None) -> list[dict]:
        return tm.next_actions(self.conn, scope_tag)

    def assign(self, task_id: str, worker_id: str) -> dict:
        return tm.assign(self.conn, task_id, worker_id)

    def upcoming(self, within_hours: int = 48) -> list[store.Entity]:
        return em.upcoming(self.conn, within_hours=within_hours)

    # --- LIBRARIAN — substrate upkeep --------------------------------------
    def dedup(self) -> list[tuple[str, str]]:
        return lib.dedup(self.conn)

    def refresh_meta(self, tag: str,
                     summarizer: Optional[Summarizer] = None) -> store.Entity:
        return lib.meta_artifact(self.conn, tag, summarizer)

    def tick(self, summarizer: Optional[Summarizer] = None) -> dict:
        """One periodic upkeep beat: dedup the artifact substrate, then refresh
        each populated tag's meta-artifact (the rolled-up project/goal state)."""
        deduped = self.dedup()
        # ctx:* excluded (CONTEXT_CLOUD.md §4): refresh_meta spawns an LLM
        # summarizer per populated tag — cloud tags must never trigger that.
        tags = [r["name"] for r in
                self.conn.execute("SELECT name FROM tag "
                                  "WHERE name NOT LIKE 'ctx:%' ORDER BY name")]
        refreshed = [t for t in tags
                     if store.entities_by_tag(self.conn, t)
                     and (self.refresh_meta(t, summarizer) or True)]
        return {"deduped": len(deduped), "meta_refreshed": refreshed}

    # --- REVIEW surface ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return insp.snapshot(self.conn)

    def review(self) -> str:
        """Full human-readable review: the store snapshot + the managers' live
        proposals (what the system would DO next)."""
        out = [insp.render(self.conn)]
        acts = self.next_actions()
        out.append(f"\n-- task-manager: next actions ({len(acts)}) --")
        for a in acts[:15]:
            label = a.get("task_label") or a.get("task") or a.get("task_id", "")
            out.append(f"  {a.get('action', '?')}: {str(label)[:50]} "
                       f"{'(needs confirm)' if a.get('requires_confirmation') else ''}")
        up = self.upcoming()
        out.append(f"\n-- event-manager: upcoming ({len(up)}) --")
        for e in up[:15]:
            out.append(f"  {e.get('start_dt')} | {(e.label or '')[:50]}")
        return "\n".join(out)

    def close(self) -> None:
        self.conn.close()
