"""RELABEL — human-friendly naming for the knowledge substrate.

Tim's complaint (2026-07-03): artifact labels like ``"2026-06-30 · Mother"`` and
tags like ``"chat:190846964"`` are useless. Two fixes live here, both operating
non-destructively on an existing core db (``bot_squad_worker.core.store``):

1. **Retag chats by name** (``retag_chats_by_name``) — rewrite every
   ``chat:<numericid>`` tag to ``chat:<contact name>`` (contact names are unique
   in the owner's Telegram), moving all tag memberships onto the renamed tag and
   dropping the old one. Idempotent; an unknown id keeps its numeric tag.

2. **Subject re-titling** (``retitle_summaries``) — replace date/chat STUB labels
   on distilled summary artifacts with a real SUBJECT (what the conversation was
   ABOUT), via a pluggable ``titler`` (default: a cheap headless-claude call,
   mirroring ``librarian.default_summarizer``). Fail-soft: any error keeps the
   existing label.

House style mirrors ``store``: every function takes an explicit
``sqlite3.Connection`` as its first arg. The LLM titler is pluggable so tests
inject a FAKE and NEVER spawn a real claude / hit the network.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from typing import Callable, Optional

from . import store
# Reuse the headless-claude plumbing (binary, proxy-stripped env, disallowed
# tools) so the titler behaves exactly like the librarian's summarizer/judges.
# CTX_TAG_PREFIX is imported (not re-declared) so the context-cloud boundary is
# ONE prefix across every curation door — librarian, inspect, and here.
from .librarian import _CLAUDE_BIN, _DISALLOWED_TOOLS, _claude_env, CTX_TAG_PREFIX

log = logging.getLogger(__name__)


# ---- CONTEXT-CLOUD BOUNDARY (coord_core/CONTEXT_CLOUD.md §4, board 9332e585) --
# ctx:*-tagged entities are the coordinators' CONTEXT CLOUD — append-only +
# supersede, NEVER mutated by an engine tuned for the life-assistant chat
# substrate. retitle_summaries LLM-REWRITES an artifact's label; without this
# filter a distilled cloud entry with a date-ish label would get silently
# retitled (the 64f22c0 scar class). Same predicate as librarian._curatable,
# one shared prefix. (retag_chats_by_name needs no filter: its ^chat:(-?\d+)$
# regex structurally cannot match a ctx:* tag, so it never renames one.)
def _is_ctx_cloud(conn: sqlite3.Connection, entity_id: str) -> bool:
    """True if the entity carries any ctx:* tag — cloud knowledge to leave alone."""
    return any(t.startswith(CTX_TAG_PREFIX) for t in store.tags_of(conn, entity_id))

# A Titler turns a summary's CONTENT into a short SUBJECT title (what it is
# ABOUT). Pluggable so tests inject a fake and NEVER spawn a real claude.
Titler = Callable[[str], str]


def sanitize_title(title: Optional[str]) -> str:
    """Clean a raw chat/contact title into a human tag suffix.

    Strips surrounding whitespace and collapses internal runs of whitespace to a
    single space. Keeps the text human (e.g. ``"Макс Белов"``). Returns ``""`` for
    a falsy / all-whitespace title.
    """
    if not title:
        return ""
    return re.sub(r"\s+", " ", str(title)).strip()


# ===========================================================================
# 1. retag_chats_by_name — chat:<id>  ->  chat:<contact name>
# ===========================================================================
_CHAT_ID_RE = re.compile(r"^chat:(-?\d+)$")   # -? so GROUP chats (negative ids) match too


def _name_for(id_to_name: dict, chat_id: int) -> Optional[str]:
    """Look up a chat id in the map, tolerating int/str keys; sanitize the hit."""
    raw = id_to_name.get(chat_id)
    if raw is None:
        raw = id_to_name.get(str(chat_id))
    return sanitize_title(raw) or None


def retag_chats_by_name(core_conn: sqlite3.Connection,
                        id_to_name: dict) -> dict:
    """Rewrite ``chat:<numericid>`` tags to ``chat:<contact name>`` in place.

    For every tag whose name matches ``chat:<digits>``: resolve the title from
    ``id_to_name`` (int OR str keys accepted; values sanitized). If unknown, the
    tag is LEFT as the numeric id. Otherwise a ``chat:<title>`` tag is
    get-or-created and ALL ``metadata_tag`` memberships of the old tag are moved
    onto it (``INSERT OR IGNORE`` so a pre-existing name-tag merges cleanly), the
    old tag's ``meta_artifact_id`` is carried over if the new tag lacks one, and
    the old numeric tag is dropped.

    Idempotent: after a run there are no ``chat:<digits>`` tags left to match
    (renamed ones contain non-digits; unknown ids stay numeric but map to nothing
    new), so a second run is a no-op. Returns ``{"renamed": n, "moved": m,
    "unknown": u}`` where ``moved`` counts moved membership rows.
    """
    result = {"renamed": 0, "moved": 0, "unknown": 0}
    rows = core_conn.execute("SELECT id, name, meta_artifact_id FROM tag").fetchall()
    for row in rows:
        m = _CHAT_ID_RE.match(row["name"])
        if not m:
            continue
        chat_id = int(m.group(1))
        title = _name_for(id_to_name, chat_id)
        if not title:
            result["unknown"] += 1
            continue  # unknown id -> keep the numeric tag
        new_name = f"chat:{title}"
        if new_name == row["name"]:
            continue  # already named (defensive; regex wouldn't match anyway)
        old_tid = row["id"]
        new_tid = store.get_or_create_tag(core_conn, new_name)

        # Move memberships onto the renamed tag (dedupe against any that already
        # sit on a pre-existing name-tag).
        moved = core_conn.execute(
            "INSERT OR IGNORE INTO metadata_tag(metadata_id, tag_id) "
            "SELECT metadata_id, ? FROM metadata_tag WHERE tag_id=?",
            (new_tid, old_tid),
        ).rowcount
        result["moved"] += moved if moved and moved > 0 else 0
        core_conn.execute("DELETE FROM metadata_tag WHERE tag_id=?", (old_tid,))

        # Carry over the librarian rollup pointer if the new tag lacks one.
        if row["meta_artifact_id"]:
            new_row = core_conn.execute(
                "SELECT meta_artifact_id FROM tag WHERE id=?", (new_tid,)
            ).fetchone()
            if new_row and not new_row["meta_artifact_id"]:
                core_conn.execute(
                    "UPDATE tag SET meta_artifact_id=? WHERE id=?",
                    (row["meta_artifact_id"], new_tid))

        core_conn.execute("DELETE FROM tag WHERE id=?", (old_tid,))
        result["renamed"] += 1
    return result


# ===========================================================================
# 2. retitle_summaries — date/chat STUB label  ->  real SUBJECT title
# ===========================================================================
_TITLE_TIMEOUT = 45  # seconds — ONE cheap title per summary
_TITLE_SYSTEM = (
    "You title stored conversation summaries for a goal-driven personal "
    "assistant. Given ONE summary, reply with a short 3-8 word SUBJECT title for "
    "what the conversation is ABOUT — the topic, decision or event (e.g. "
    "'Apartment lease renewal terms', 'Planning mother's birthday dinner'). Do "
    "NOT answer 'chat with X' or a date. Reply with the title ONLY — no quotes, "
    "no preamble, no punctuation at the end."
)

# A label is a re-titleable STUB when it starts with an ISO date (the migrate /
# daily-summary format ``"2026-06-30 chat:<id>"``) or carries a "· <name>"
# separator (the old "date · person" style Tim flagged as useless).
_STUB_LABEL_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}|·")


def _is_stub_label(label: Optional[str]) -> bool:
    return bool(_STUB_LABEL_RE.search(label or ""))


def default_titler(content: str) -> str:
    """Production titler: ONE cheap headless ``claude -p`` SUBJECT title.

    Mirrors ``librarian.default_summarizer`` (same binary, tools disallowed,
    proxy stripped, hard timeout; Anthropic on the direct route). **Fail-soft:**
    timeout / non-zero exit / any error → ``""`` so the caller keeps the existing
    label. **Never invoked by the tests** — they pass a fake titler.
    """
    content = (content or "").strip()
    if not content:
        return ""
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _TITLE_SYSTEM,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_TITLE_TIMEOUT, input=content,
        )
    except subprocess.TimeoutExpired:
        log.warning("relabel: titler timed out")
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("relabel: titler error: %s", e)
        return ""
    if r.returncode != 0:
        log.warning("relabel: titler rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return ""
    return (r.stdout or "").strip().strip('"').strip()


def retitle_summaries(core_conn: sqlite3.Connection,
                      titler: Optional[Titler] = None, cap: int = 80) -> dict:
    """Replace date/chat STUB labels on distilled summaries with a SUBJECT title.

    Scans ``distilled`` artifacts whose label is a re-titleable STUB (starts with
    an ISO date, or contains a "·" name separator). For each, calls the pluggable
    ``titler`` on the artifact CONTENT to get a short subject and ``set_label`` to
    it. Bounded to ``cap`` summaries per pass (bounds the LLM fan-out). Fail-soft:
    if the titler raises, returns empty, or yields only whitespace, the existing
    label is KEPT. Already-good labels (not stubs) are skipped untouched.

    Returns ``{"examined": n, "retitled": m, "kept": k}``.
    """
    titler = titler or default_titler
    result = {"examined": 0, "retitled": 0, "kept": 0}
    for a in store.by_type(core_conn, "artifact"):
        if a.get("state") != "distilled":
            continue
        if _is_ctx_cloud(core_conn, a.id):
            continue  # CONTEXT-CLOUD BOUNDARY: never LLM-retitle cloud knowledge
        if not _is_stub_label(a.label):
            continue  # already a real subject — leave it
        if result["examined"] >= cap:
            break
        result["examined"] += 1
        try:
            subject = titler(a.get("content") or "")
        except Exception as e:  # noqa: BLE001
            log.warning("relabel: titler raised for %s: %s — keep label", a.id, e)
            subject = ""
        subject = (subject or "").strip()
        if subject:
            store.set_label(core_conn, a.id, subject)
            result["retitled"] += 1
        else:
            result["kept"] += 1  # fail-soft: keep the existing stub label
    return result
