"""The hub's own durable store — objectives queue + issue log, zero setup.

CORE-MODEL §3 wants the hub to be the product's control surface: an objectives
queue and an issue log that are *durable state*. SLICE-1-SPEC §4.5 originally made
that render-only on the floor because the OLD hub (``hub_capabilities.py`` /
``capabilities.py``) writes into a Project checkout (``_resolve_checkout_for``),
and a first-time user has no Project. The BRIEF-ADDENDUM overrides that: the hub
must **write from the first click, zero setup**.

So this module is a small, clean, self-contained store that owes NOTHING to the
capabilities mechanism and never resolves a checkout. It uses the exact trick the
disposition log uses: an append-only JSONL under the resolved *local* data dir
(``paths.resolve_data_dir()``), a sibling of ``_output`` / ``_issues``. A
first-run user's objectives and issues persist immediately and survive reload /
restart. Project-scoping is optional depth *later*; it is never a floor gate here.

The log is append-only *events* (``add`` / ``done`` / ``reopen`` / ``resolve`` /
``remove``) folded on read into current state, so a write is one cheap append and
history is never rewritten — the same durability property ``status.jsonl`` and the
disposition log rely on.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from . import paths

# ── where the store lives ──────────────────────────────────────────────────────
# A per-install LOCAL dir, resolved through the one canonical precedence
# (explicit arg > $LOOPS_DATA_DIR > <install>/data/_loops). No Project, no
# checkout, no capabilities dir. Sibling of _output / _issues.


def hub_dir() -> str:
    return os.path.join(paths.resolve_data_dir(), "_hub")


def objectives_log() -> str:
    return os.path.join(hub_dir(), "objectives.jsonl")


def issues_log() -> str:
    return os.path.join(hub_dir(), "issues.jsonl")


# ── append-only event write (mirrors report.record) ────────────────────────────

def _append(path: str, event: Dict[str, Any]) -> Dict[str, Any]:
    os.makedirs(hub_dir(), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def _read_events(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001 — one bad line never poisons the store
                continue
    return out


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── fold events → current state ────────────────────────────────────────────────
# Insertion order is preserved (objectives ARE a queue). Each item folds its
# events: text from the latest text-bearing event, status from the latest
# lifecycle event; a ``remove`` drops it entirely.

def _fold(events: List[Dict[str, Any]], open_status: str, closed_status: str,
          close_op: str, reopen_op: str = "reopen") -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for ev in events:
        op = ev.get("op")
        iid = ev.get("id")
        if not iid:
            continue
        if op == "add":
            if iid not in items:
                order.append(iid)
            items[iid] = {
                "id": iid,
                "text": ev.get("text", ""),
                "status": open_status,
                "createdAt": ev.get("ts"),
                "updatedAt": ev.get("ts"),
            }
        elif op == "remove":
            items.pop(iid, None)
            if iid in order:
                order.remove(iid)
        elif iid in items:
            it = items[iid]
            if op == close_op:
                it["status"] = closed_status
            elif op == reopen_op:
                it["status"] = open_status
            elif op == "edit":
                it["text"] = ev.get("text", it["text"])
            it["updatedAt"] = ev.get("ts", it.get("updatedAt"))
    return [items[i] for i in order if i in items]


# ── objectives ─────────────────────────────────────────────────────────────────

def add_objective(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("objective text is empty")
    ev = {"op": "add", "id": _new_id("obj"), "text": text, "ts": time.time()}
    _append(objectives_log(), ev)
    return {"id": ev["id"], "text": text, "status": "open",
            "createdAt": ev["ts"], "updatedAt": ev["ts"]}


def set_objective_status(oid: str, status: str) -> Dict[str, Any]:
    op = {"open": "reopen", "done": "done"}.get(status)
    if not op:
        raise ValueError(f"unknown objective status: {status!r} (open|done)")
    ev = {"op": op, "id": oid, "ts": time.time()}
    _append(objectives_log(), ev)
    return {"id": oid, "status": status, "updatedAt": ev["ts"]}


def remove_objective(oid: str) -> Dict[str, Any]:
    ev = {"op": "remove", "id": oid, "ts": time.time()}
    _append(objectives_log(), ev)
    return {"id": oid, "removed": True}


def list_objectives() -> List[Dict[str, Any]]:
    return _fold(_read_events(objectives_log()), "open", "done", "done")


# ── issues ─────────────────────────────────────────────────────────────────────

def add_issue(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("issue text is empty")
    ev = {"op": "add", "id": _new_id("iss"), "text": text, "ts": time.time()}
    _append(issues_log(), ev)
    return {"id": ev["id"], "text": text, "status": "open",
            "createdAt": ev["ts"], "updatedAt": ev["ts"]}


def set_issue_status(iid: str, status: str) -> Dict[str, Any]:
    op = {"open": "reopen", "resolved": "resolve"}.get(status)
    if not op:
        raise ValueError(f"unknown issue status: {status!r} (open|resolved)")
    ev = {"op": op, "id": iid, "ts": time.time()}
    _append(issues_log(), ev)
    return {"id": iid, "status": status, "updatedAt": ev["ts"]}


def remove_issue(iid: str) -> Dict[str, Any]:
    ev = {"op": "remove", "id": iid, "ts": time.time()}
    _append(issues_log(), ev)
    return {"id": iid, "removed": True}


def list_issues() -> List[Dict[str, Any]]:
    return _fold(_read_events(issues_log()), "open", "resolved", "resolve")


# ── the dashboard's one read ───────────────────────────────────────────────────

def view() -> Dict[str, Any]:
    """Everything the hub surface renders, in one read. Local store only —
    no Project, no checkout, no capabilities mechanism on this path."""
    objs = list_objectives()
    isss = list_issues()
    return {
        "objectives": objs,
        "issues": isss,
        "counts": {
            "objectives_open": sum(1 for o in objs if o["status"] == "open"),
            "objectives_done": sum(1 for o in objs if o["status"] == "done"),
            "issues_open": sum(1 for i in isss if i["status"] == "open"),
            "issues_resolved": sum(1 for i in isss if i["status"] == "resolved"),
        },
        "store": hub_dir(),
    }
