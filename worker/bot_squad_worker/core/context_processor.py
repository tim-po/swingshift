"""Context Processor — raw context → reconciled, *proposed* entities.

This is the left half of the heartbeat loop (design:
``claude-memory/product-core/SYSTEMS.md`` §1 "Context Processor"): take raw
context, **extract** candidate entities, **reconcile** them against the real
state (existing entities) — deciding *new / update / garbage / duplicate* — and
emit only **proposed** entities (the confirm gate lives downstream, see
AUTONOMY.md / ``store.confirm``).

Two design rules this module obeys:

* **The core logic never touches an LLM.** The only LLM contact is the
  *extractor*, which is a pluggable ``Callable[[str], list[dict]]``. Core
  ``reconcile`` / ``ingest`` are pure, deterministic SQLite work. ``default_
  extractor`` shells the headless ``claude -p`` pattern (mirrors
  ``tg_pipelines._extract_llm``), but tests inject a FAKE extractor — no network.
* **Reconciliation is a deterministic FIRST CUT** (see ``# Q2`` block below).
  ``OPEN-QUESTIONS.md`` Q2 keeps the LLM-semantic matching + merge open; this
  module is the heuristic stand-in and marks exactly where that upgrade slots.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from typing import Any, Callable

from bot_squad_worker.core import lifecycle, store

log = logging.getLogger(__name__)

# An extractor maps raw text -> a list of proposed-entity dicts. Shapes:
#   {"type": "task",     "label": ..., "tags": [...], "due_date": ..., ...}
#   {"type": "event",    "label": ..., "start_dt": ..., "end_dt": ..., ...}
#   {"type": "artifact", "content": ..., "state": ..., "tags": [...]}
Extractor = Callable[[str], list[dict]]


# ---------------------------------------------------------------------------
# 1. Extractor — the ONLY LLM seam. Pluggable; faked in tests.
# ---------------------------------------------------------------------------
_CLAUDE_BIN = "claude"
_DISALLOWED_TOOLS = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
]
_EXTRACT_TIMEOUT = 120  # seconds

_EXTRACT_SYSTEM_PROMPT = (
    "You convert raw context into PROPOSED structured entities for a personal "
    "task/event manager. Return ONLY a JSON object of the form "
    '{"entities": [...]}. Each entity is one of:\n'
    '  {"type":"task","label":"<short>","due_date":"<ISO date|null>",'
    '"priority":"P0|P1|P2|P3"}\n'
    '  {"type":"event","label":"<short>","start_dt":"<ISO datetime>",'
    '"end_dt":"<ISO datetime|null>","location":"<text|null>"}\n'
    '  {"type":"artifact","content":"<fact/note>"}\n'
    "Only emit entities clearly supported by the text. No prose, JSON only."
)


def _claude_env() -> dict:
    """Env for the claude subprocess WITHOUT proxy vars (Anthropic is direct).

    Mirrors ``tg_pipelines._claude_env``: the worker proxies its egress to reach
    DPI-blocked APIs, but Anthropic is reachable directly and the proxy is a slow
    bottleneck, so strip it for the extraction call.
    """
    env = dict(os.environ)
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        env.pop(k, None)
    return env


def _first_json_value(text: str) -> Any:
    """Return the first balanced ``{...}`` or ``[...]`` JSON value, or None.

    Brace/bracket-counts while respecting string literals so punctuation inside
    quoted text can't unbalance the scan. Tolerates prose/markdown fences.
    """
    opens = {"{": "}", "[": "]"}
    start = -1
    for i, ch in enumerate(text):
        if ch in opens:
            start = i
            open_ch, close_ch = ch, opens[ch]
            break
    if start < 0:
        return None
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:  # noqa: BLE001
                        return None
    return None


def default_extractor(raw_text: str) -> list[dict]:
    """Headless ``claude -p`` extraction → list of proposed-entity dicts.

    Returns ``[]`` on timeout / non-zero exit / malformed output, so a flaky LLM
    pass simply yields nothing rather than crashing the tick (same failure
    posture as ``tg_pipelines._extract_llm``). NOT exercised by tests — tests
    pass a fake extractor.
    """
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _EXTRACT_SYSTEM_PROMPT,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_EXTRACT_TIMEOUT, input=raw_text,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("context_processor: extractor failed: %s", e)
        return []
    if r.returncode != 0:
        log.warning("context_processor: extractor rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return []
    val = _first_json_value((r.stdout or "").strip())
    if isinstance(val, dict):
        val = val.get("entities", [])
    if not isinstance(val, list):
        return []
    return [e for e in val if isinstance(e, dict) and e.get("type") in store.TYPES]


# ---------------------------------------------------------------------------
# 2. Reconciliation — the deterministic first cut.
# ---------------------------------------------------------------------------
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

# fields compared (and updatable) per type when a live match is found.
_COMPARE_FIELDS = {
    "task": ("status", "due_date", "priority"),
    "event": ("status", "start_dt", "end_dt", "location"),
    "artifact": ("content", "state"),
}


def normalize(text: str | None) -> str:
    """lowercase → strip punctuation → collapse whitespace. The label normaliser
    behind task/event match keys."""
    if not text:
        return ""
    t = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", t).strip()


def _content_hash(content: str | None) -> str:
    return hashlib.sha256(normalize(content).encode("utf-8")).hexdigest()


def _date_part(dt: str | None) -> str:
    """YYYY-MM-DD from an ISO date/datetime string (best-effort)."""
    if not dt:
        return ""
    try:
        return datetime.fromisoformat(dt).date().isoformat()
    except ValueError:
        return dt[:10]


def _is_past(start_dt: str | None) -> bool:
    """True if ``start_dt`` parses and is strictly before now."""
    if not start_dt:
        return False
    try:
        dt = datetime.fromisoformat(start_dt)
    except ValueError:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return dt < now


def _match_key(type_: str, label: str | None, payload: dict,
               scope_tags: list[str]) -> tuple:
    """The reconciliation key for an entity (proposed OR existing), within a scope.

    * task     → (normalize(label), sorted(scope_tags))
    * event    → (normalize(label), date(start_dt))
    * artifact → (content_hash,)

    Because the scope (``scope_tags``) is fixed for a given reconcile call, the
    task key reduces to the normalised label *within that scope* — exactly the
    intent: "the same-named task in the same project".
    """
    tags_key = tuple(sorted(scope_tags))
    if type_ == "task":
        return (type_, normalize(label), tags_key)
    if type_ == "event":
        return (type_, normalize(label), _date_part(payload.get("start_dt")))
    if type_ == "artifact":
        return (type_, _content_hash(payload.get("content")))
    # worker (not normally extracted) — fall back to label.
    return (type_, normalize(label), tags_key)


def _is_dead(e: store.Entity) -> bool:
    """An existing entity a duplicate of which is GARBAGE: a done task or a
    rejected entity."""
    if e.trust == "rejected":
        return True
    return e.type == "task" and e.get("status") == "done"


def _diff_fields(type_: str, item: dict, existing: store.Entity) -> dict:
    """Fields PRESENT in the proposed item whose value differs from the existing
    entity. Absent fields are never overwritten (the extractor not mentioning a
    field is not a signal to clear it)."""
    changes = {}
    for f in _COMPARE_FIELDS.get(type_, ()):
        if f in item and item[f] != existing.get(f):
            changes[f] = item[f]
    return changes


def _create(conn, item: dict, scope_tags: list[str]) -> store.Entity:
    """Create a proposed entity from a proposed-item dict + apply scope tags."""
    type_ = item["type"]
    label = item.get("label")
    tags = list(dict.fromkeys([*scope_tags, *item.get("tags", [])]))
    if type_ == "task":
        return store.create_task(
            conn, label=label, status=item.get("status", "backlog"),
            due_date=item.get("due_date"), priority=item.get("priority", "P2"),
            trust="proposed", tags=tags)
    if type_ == "event":
        return store.create_event(
            conn, label=label, status=item.get("status", "scheduled"),
            start_dt=item.get("start_dt"), end_dt=item.get("end_dt"),
            location=item.get("location"), trust="proposed", tags=tags)
    if type_ == "artifact":
        return store.create_artifact(
            conn, label=label, content=item.get("content"),
            state=item.get("state", "raw"), trust="proposed", tags=tags)
    raise ValueError(f"cannot create unsupported proposed type: {type_!r}")


def reconcile(conn, proposed: list[dict], scope_tags: list[str]) -> dict:
    """Compare proposed entities against existing scope state → new/update/
    garbage/duplicate. Everything created is ``proposed``.

    Returns ``{"new": [ids], "updated": [ids], "garbage": [reasons],
    "duplicate": [ids]}``.
    """
    # ----- gather existing entities in scope, index by match key -------------
    index: dict[tuple, store.Entity] = {}
    seen: set[str] = set()
    for tag in scope_tags:
        for e in store.entities_by_tag(conn, tag):
            if e.id in seen:
                continue
            seen.add(e.id)
            index[_match_key(e.type, e.label, e.payload, scope_tags)] = e

    result = {"new": [], "updated": [], "garbage": [], "duplicate": []}

    for item in proposed:
        type_ = item.get("type")
        if type_ not in store.TYPES:
            result["garbage"].append(f"unknown_type:{type_!r}")
            continue
        label = item.get("label")
        key = _match_key(type_, label, item, scope_tags)
        match = index.get(key)

        # --- GARBAGE: an event whose start is strictly in the past -----------
        if type_ == "event" and _is_past(item.get("start_dt")):
            result["garbage"].append(f"past_event:{normalize(label) or key}")
            continue
        # --- GARBAGE: exact duplicate of a done/rejected entity --------------
        if match is not None and _is_dead(match):
            result["garbage"].append(f"duplicate_of_dead:{match.id}")
            continue

        if match is not None:
            # --- UPDATE: live match, some field differs → patch in place -----
            changes = _diff_fields(type_, item, match)
            if changes:
                store.update_payload(conn, match.id, **changes)
                result["updated"].append(match.id)
            else:
                # --- DUPLICATE: live match, nothing differs -----------------
                # Q2 NOTE / store-helper gap: the model names a
                # ``candidate -duplicate_of-> existing`` edge here, but honoring
                # "skip creation; not a 2nd entity" leaves NO candidate node to
                # anchor that edge (FK requires both endpoints to exist). So the
                # duplicate is recorded in the summary only. The natural upgrade
                # is a lightweight duplicate/provenance "tombstone" row that CAN
                # anchor the edge — see the Q2 block below.
                result["duplicate"].append(match.id)
            continue

        # --- NEW: no match → create a proposed entity + scope tags -----------
        created = _create(conn, item, scope_tags)
        result["new"].append(created.id)
        # re-index so a later item in THIS batch dedups against it.
        index[key] = created

    return result


# ===========================================================================
# Q2 — RECONCILIATION ALGORITHM (OPEN-QUESTIONS.md Q2). FIRST CUT.
# ---------------------------------------------------------------------------
# Everything in `reconcile` above is a DETERMINISTIC, syntactic heuristic:
#   * match keys are normalised-string / date / content-hash equality;
#   * "a field differs" is exact-value comparison;
#   * garbage = (past event) or (duplicate of a done/rejected entity);
#   * a duplicate is *dropped* (recorded in the summary), not merged.
#
# This is intentionally NOT the real version. The real Q2 reconciler is
# LLM-SEMANTIC and slots in at exactly two seams, with the rest unchanged:
#   1. MATCHING  — replace `_match_key` exact-equality lookup with a semantic
#      candidate search ("buy milk" ≈ "get milk from the shop", "standup" ≈
#      "daily sync"): embed/ask the LLM to pick the best existing match (or none)
#      from the in-scope set, instead of dict-keying on normalised strings.
#   2. MERGE     — replace `_diff_fields` exact-diff with an LLM MERGE that
#      reconciles contradictory fields (newer/better-sourced wins; unresolved
#      conflicts logged, not silently picked — see Librarian §4) and PERSISTS the
#      duplicate candidate linked by a real `duplicate_of` / `supersedes` edge
#      (the tombstone the store-helper gap above wants).
# Keep the deterministic path as the cheap pre-filter / fallback. Don't
# over-engineer the first cut.
# ===========================================================================


# ---------------------------------------------------------------------------
# 3. The loop entry — extract → reconcile.
# ---------------------------------------------------------------------------
def ingest(conn, raw_text: str, scope_tags: list[str],
           extractor: Extractor = default_extractor) -> dict:
    """Run one Context-Processor pass: extract proposed entities from raw text,
    then reconcile them into the scope. Returns the reconcile summary.

    Entity EXISTENCE is never human-gated (SHARED CONTRACT / AUTONOMY.md), so
    everything new/updated on this ingest path is AUTO-CONFIRMED (and tasks land
    on the board: ``status='backlog'``, ``assignee='unassigned'``). The only
    human gate left is approving HIGH-risk AI-task execution downstream
    (``core.lifecycle``).
    """
    proposed = extractor(raw_text) or []
    result = reconcile(conn, proposed, scope_tags)
    lifecycle.auto_confirm_all(conn, [*result["new"], *result["updated"]])
    return result


# ---------------------------------------------------------------------------
# 4. Change-watch helper.
# ---------------------------------------------------------------------------
def affected_by(conn, entity_id: str) -> list[str]:
    """Edge-neighbours of ``entity_id`` (outgoing + incoming) — the entities a
    change here should make you revisit (SYSTEMS.md §1 "Change-watch"). Pure
    traversal; takes no action and does not follow transitively."""
    out = store.edges_out(conn, entity_id)
    inn = store.edges_in(conn, entity_id)
    return list(dict.fromkeys([*out, *inn]))
