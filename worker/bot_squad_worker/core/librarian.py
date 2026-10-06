"""LIBRARIAN — keeps the Information-Artifact substrate clean + rolls up per-tag
meta-artifacts (product-core System #4).

The Librarian is the Artifact manager described in
``claude-memory/product-core/SYSTEMS.md``. It operates *non-destructively* on the
core store (``bot_squad_worker.core.store``): it marks duplicates with edges,
promotes artifacts along the ``raw → distilled → canonical`` lifecycle, compresses
episodic ``raw`` noise into ``distilled`` semantic artifacts, and — the key feature
— rolls a *whole tag's context* (every Task/Event/Artifact/Worker sharing the tag)
into a single ``canonical`` **meta-artifact** that serves as the project/goal-state
surface (MODEL.md: "the meta-artifact IS the project's/goal's summary").

House style mirrors ``store``/``tracking_store``: every function takes an explicit
``sqlite3.Connection`` as its first arg.

LLM boundary — the summarizer is *pluggable*. Anything that needs natural-language
compression (``consolidate``, ``meta_artifact``) takes a ``summarizer`` callable of
shape ``Callable[[list[str]], str]``. In production this defaults to
``default_summarizer``, a headless ``claude -p`` turn with the egress proxy stripped
(the same pattern as ``tg_pipelines._summary_llm`` — Anthropic is on the direct
route). Tests inject a FAKE summarizer and therefore NEVER spawn a real claude / hit
the network.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from typing import Callable, Optional

from bot_squad_worker.core import store
from bot_squad_worker.core.store import Entity

log = logging.getLogger(__name__)


# ---- CONTEXT-CLOUD BOUNDARY (coord_core/CONTEXT_CLOUD.md §4, board 9332e585) --
# ctx:*-tagged artifacts belong to the coordinators' CONTEXT CLOUD, whose merge
# discipline is append-only + supersede tombstones — NEVER this librarian's
# archive/dedup/delete, which is tuned for the life-assistant chat substrate.
# Without this filter, relevance_pass would LLM-archive cloud knowledge at 14
# days, dedup would collapse it cross-tag, and decay would DELETE raw entries —
# the 64f22c0 scar class (a destructive default inside the mechanism built to
# survive context loss). Every curation pass filters through here.
CTX_TAG_PREFIX = "ctx:"


def _curatable(conn, arts):
    """Drop context-cloud (ctx:*-tagged) artifacts from a curation candidate list."""
    return [a for a in arts
            if not any(t.startswith(CTX_TAG_PREFIX)
                       for t in store.tags_of(conn, a.id))]

Summarizer = Callable[[list[str]], str]
# A RelevanceJudge decides, from a small feature dict, whether ONE artifact is
# still worth keeping in long-term memory (True = KEEP) or is stale/trivial/
# superseded/one-off and safe to archive (False). Pluggable so tests inject a
# fake and NEVER spawn a real claude / touch the network.
RelevanceJudge = Callable[[dict], bool]
# A SemanticJudge decides whether TWO artifacts (each a small feature dict:
# title/excerpt/tags) state the SAME underlying knowledge, even if worded
# differently (True = SAME/duplicate). Pluggable so tests inject a fake and
# NEVER spawn a real claude / touch the network. CONSERVATIVE contract: any
# error / ambiguity → treat as NOT a duplicate (return False).
SemanticJudge = Callable[[dict, dict], bool]
# A Researcher takes a topic string (or artifact) and returns a concise
# external-context note (``""`` when it finds nothing / on any failure).
# Pluggable so tests inject a fake and NEVER hit the web / a real claude.
Researcher = Callable[[str], str]

# --- artifact lifecycle ordering -------------------------------------------
_STATES = ("raw", "distilled", "canonical")
_STATE_RANK = {s: i for i, s in enumerate(_STATES)}

# Near-duplicate token-overlap (Jaccard) threshold for the fuzzy dedup pass.
# Deliberately high: only collapse artifacts that are essentially the same text,
# never merely topically related ones.
_OVERLAP_THRESHOLD = 0.85


# ===========================================================================
# Default (production) summarizer — headless claude, proxy stripped.
# ===========================================================================
_CLAUDE_BIN = "claude"
_DISALLOWED_TOOLS = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
]
_SUMMARY_TIMEOUT = 120  # seconds
_SUMMARY_SYSTEM = (
    "You are the Librarian for a goal-driven assistant. You compress a set of "
    "information artifacts into ONE faithful, self-contained summary. Preserve "
    "concrete facts, decisions, statuses and dates. Drop redundancy and chatter. "
    "Return PLAIN TEXT only — no preamble, no markdown fences."
)


def _claude_env() -> dict:
    """Env for the claude subprocess WITHOUT proxy vars (Anthropic is direct)."""
    env = dict(os.environ)
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        env.pop(k, None)
    return env


def default_summarizer(contents: list[str]) -> str:
    """Production summarizer: one headless ``claude -p`` turn, proxy stripped.

    Mirrors ``tg_pipelines._summary_llm`` (same binary, tools disallowed, hard
    timeout). Returns ``""`` on timeout / non-zero exit / any failure so a flaky
    pass records nothing rather than crashing. **Never invoked by the tests** —
    they pass a fake callable instead.
    """
    payload = "\n\n---\n\n".join(c for c in contents if c)
    if not payload:
        return ""
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _SUMMARY_SYSTEM,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_SUMMARY_TIMEOUT, input=payload,
        )
    except subprocess.TimeoutExpired:
        log.warning("librarian: summarizer timed out")
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("librarian: summarizer error: %s", e)
        return ""
    if r.returncode != 0:
        log.warning("librarian: summarizer rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return ""
    return (r.stdout or "").strip()


# ===========================================================================
# 1. dedup — mark near-duplicate artifacts (non-destructive).
# ===========================================================================
def _norm(content: Optional[str]) -> str:
    """Normalize artifact content for comparison: trim, lowercase, collapse ws."""
    return re.sub(r"\s+", " ", (content or "").strip().lower())


def _content_hash(content: Optional[str]) -> str:
    return hashlib.sha256(_norm(content).encode("utf-8")).hexdigest()


def _tokens(content: Optional[str]) -> set[str]:
    return set(_norm(content).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _keeper_of(group: list[Entity]) -> Entity:
    """Pick the survivor of a duplicate group: most-canonical, then oldest."""
    return sorted(
        group,
        key=lambda e: (-_STATE_RANK.get(e.get("state", "raw"), 0), e.created_at),
    )[0]


def dedup(conn) -> list[tuple[str, str]]:
    """Mark near-duplicate artifacts; return ``[(dup_id, keeper_id), ...]``.

    Two-stage, non-destructive (nothing is deleted):

    1. **Exact:** group by normalized-content hash; within each group keep the
       most-canonical / oldest artifact and mark the rest.
    2. **Fuzzy:** over the surviving representatives, collapse pairs whose token
       Jaccard overlap ``>= _OVERLAP_THRESHOLD`` (catches reworded near-dups the
       hash misses), keeper = the older/representative one.

    Each ``dup`` gets a ``dup -duplicate_of-> keeper`` edge. The original artifact
    is preserved (provenance survives); reconciliation can later read the edges.
    """
    arts = _curatable(conn, store.by_type(conn, "artifact"))  # ordered by created_at
    pairs: list[tuple[str, str]] = []
    dup_ids: set[str] = set()

    # Stage 1 — exact normalized-content groups.
    groups: dict[str, list[Entity]] = {}
    for a in arts:
        groups.setdefault(_content_hash(a.get("content")), []).append(a)

    representatives: list[Entity] = []
    for group in groups.values():
        keeper = _keeper_of(group)
        representatives.append(keeper)
        for a in group:
            if a.id == keeper.id:
                continue
            store.add_edge(conn, a.id, keeper.id, "duplicate_of")
            pairs.append((a.id, keeper.id))
            dup_ids.add(a.id)

    # Stage 2 — fuzzy overlap among the survivors (oldest first = keeper).
    representatives.sort(key=lambda e: e.created_at)
    tok = {e.id: _tokens(e.get("content")) for e in representatives}
    kept: list[Entity] = []
    for e in representatives:
        match = None
        for k in kept:
            if _jaccard(tok[e.id], tok[k.id]) >= _OVERLAP_THRESHOLD:
                match = k
                break
        if match is None:
            kept.append(e)
        else:
            store.add_edge(conn, e.id, match.id, "duplicate_of")
            pairs.append((e.id, match.id))
            dup_ids.add(e.id)

    return pairs


# ===========================================================================
# 2. promote — advance one artifact along raw -> distilled -> canonical.
# ===========================================================================
def promote(conn, artifact_id: str, to_state: str):
    """Promote an artifact's lifecycle state. Returns the updated Entity, or
    ``False`` if the move is invalid (unknown entity/state or a backward skip).

    Guards the ordering: ``raw -> distilled -> canonical`` may only move forward
    (or stay). A backward request (e.g. ``canonical -> raw``) is rejected loudly
    rather than silently demoting memory.
    """
    if to_state not in _STATE_RANK:
        log.warning("librarian.promote: unknown state %r", to_state)
        return False
    e = store.get(conn, artifact_id)
    if e is None or e.type != "artifact":
        log.warning("librarian.promote: %s is not an artifact", artifact_id)
        return False
    cur = e.get("state", "raw")
    if _STATE_RANK.get(to_state, -1) < _STATE_RANK.get(cur, 0):
        log.warning("librarian.promote: refusing backward %s -> %s (%s)",
                    cur, to_state, artifact_id)
        return False
    return store.update_payload(conn, artifact_id, state=to_state)


# ===========================================================================
# 3. consolidate — compress a tag's raw artifacts into one distilled artifact.
# ===========================================================================
def consolidate(conn, tag: str,
                summarizer: Optional[Summarizer] = None) -> Optional[Entity]:
    """Distill all ``raw`` artifacts under ``tag`` into ONE ``distilled`` artifact.

    Gathers the raw artifacts sharing the tag, hands their contents to the
    pluggable ``summarizer`` (default: headless claude; tests inject a fake), and
    creates a single ``distilled`` artifact carrying the summary, tagged the same.
    Each raw source gets a ``distilled -supersedes-> raw`` edge so the lineage is
    preserved (the raws are kept, not deleted). Returns the new distilled
    artifact, or ``None`` if there are no raw artifacts to consolidate.
    """
    summarizer = summarizer or default_summarizer
    raws = [a for a in store.entities_by_tag(conn, tag, type_="artifact")
            if a.get("state") == "raw"]
    if not raws:
        return None
    summary = summarizer([a.get("content") or "" for a in raws])
    distilled = store.create_artifact(
        conn, content=summary, state="distilled",
        label=f"distilled:{tag}", trust="proposed", tags=[tag])
    for raw in raws:
        store.add_edge(conn, distilled.id, raw.id, "supersedes")
    return distilled


# ===========================================================================
# 4. meta_artifact — roll a tag's WHOLE context into one canonical artifact.
# ===========================================================================
def _build_digest(conn, tag: str, ents: list[Entity]) -> str:
    """Render a tag's entities into a structured text digest for summarization.

    Shape (stable, machine-skimmable headers so the summarizer has scaffolding):

        TAG: <tag>
        ENTITY COUNTS: task=.. event=.. artifact=.. worker=..
        TASKS (status / priority): ...
        TASK STATUS: backlog=.. todo=.. done=..
        UPCOMING EVENTS (by start): ...
        KEY ARTIFACTS (state): ...
        WORKERS (capabilities): ...
    """
    by_type: dict[str, list[Entity]] = {t: [] for t in store.TYPES}
    for e in ents:
        by_type.setdefault(e.type, []).append(e)

    lines: list[str] = [f"TAG: {tag}"]
    counts = " ".join(f"{t}={len(by_type.get(t, []))}" for t in store.TYPES)
    lines.append(f"ENTITY COUNTS: {counts}")

    tasks = by_type.get("task", [])
    if tasks:
        lines.append("TASKS (status / priority):")
        for t in tasks:
            lines.append(
                f"  - [{t.get('status', '?')}/{t.get('priority', '?')}] "
                f"{t.label or t.id}"
                + (f" (due {t.get('due_date')})" if t.get("due_date") else ""))
        breakdown: dict[str, int] = {}
        for t in tasks:
            breakdown[t.get("status", "?")] = breakdown.get(t.get("status", "?"), 0) + 1
        lines.append("TASK STATUS: "
                     + " ".join(f"{k}={v}" for k, v in sorted(breakdown.items())))

    events = [e for e in by_type.get("event", [])]
    if events:
        events.sort(key=lambda e: (e.get("start_dt") or ""))
        lines.append("UPCOMING EVENTS (by start):")
        for e in events:
            lines.append(
                f"  - {e.get('start_dt') or '?'} | {e.label or e.id}"
                + (f" @ {e.get('location')}" if e.get("location") else "")
                + f" ({e.get('status', '?')})")

    arts = by_type.get("artifact", [])
    if arts:
        lines.append("KEY ARTIFACTS (state):")
        for a in arts:
            snippet = re.sub(r"\s+", " ", (a.get("content") or "").strip())[:160]
            lines.append(f"  - [{a.get('state', '?')}] {a.label or a.id}: {snippet}")

    workers = by_type.get("worker", [])
    if workers:
        lines.append("WORKERS (capabilities):")
        for w in workers:
            lines.append(
                f"  - {w.label or w.id}"
                + (f": {w.get('capabilities')}" if w.get("capabilities") else ""))

    return "\n".join(lines)


def meta_artifact(conn, tag: str,
                  summarizer: Optional[Summarizer] = None) -> Entity:
    """Roll a tag's whole context into a single ``canonical`` meta-artifact.

    This is the project/goal-state surface (MODEL.md): there is no Project entity,
    so a tag's meta-artifact *is* its overview. Gathers every entity sharing the
    tag (Tasks/Events/Artifacts/Workers via ``entities_by_tag``), builds a
    structured digest, hands it to the pluggable ``summarizer``, and writes the
    result into a canonical artifact linked to the tag via
    ``set_tag_meta_artifact``.

    **Idempotent.** If the tag already points at a meta-artifact, its content is
    UPDATED in place (same id) rather than spawning a new one each pass — so the
    tag has exactly one meta-artifact that is regenerated as its entities change.
    """
    summarizer = summarizer or default_summarizer
    existing = store.tag_meta_artifact(conn, tag)
    existing_id = existing.id if existing else None

    # Exclude the meta-artifact itself so it doesn't summarize its own prior text.
    ents = [e for e in store.entities_by_tag(conn, tag) if e.id != existing_id]
    digest = _build_digest(conn, tag, ents)
    summary = summarizer([digest])

    if existing is not None:
        store.update_payload(conn, existing.id, content=summary,
                             state="canonical")
        store.add_tag(conn, existing.id, tag)  # ensure linkage survives
        store.set_tag_meta_artifact(conn, tag, existing.id)
        return store.get(conn, existing.id)

    art = store.create_artifact(
        conn, content=summary, state="canonical",
        label=f"meta:{tag}", trust="proposed", tags=[tag])
    store.set_tag_meta_artifact(conn, tag, art.id)
    return art


# ===========================================================================
# 5. decay — flag stale raw artifacts for archival (non-destructive default).
# ===========================================================================
def decay(conn, older_than_days: float, dry_run: bool = True) -> list[str]:
    """Identify stale ``raw`` artifacts (older than ``older_than_days``).

    Returns the candidate artifact ids. With ``dry_run=True`` (the default) it is
    purely advisory — nothing is touched. Only when ``dry_run=False`` are the
    candidates deleted (provenance-preserving callers should consolidate first).
    """
    cutoff = int(time.time()) - int(older_than_days * 86400)
    stale = [a.id for a in _curatable(conn, store.by_type(conn, "artifact"))
             if a.get("state") == "raw" and a.created_at < cutoff]
    if not dry_run:
        for aid in stale:
            store.delete(conn, aid)
    return stale


# ===========================================================================
# 6. relevance — a continuous worker that assesses whether an ACTIVE artifact
#    is still worth keeping, and ARCHIVES (non-destructively) the stale ones.
# ===========================================================================
#
# Tim's framing: "a 2-month-old summary of a chat with an acquaintance is
# irrelevant — the librarian should watch and remove those." Unlike ``decay``
# (which only looks at raw AGE), this judges RELEVANCE with an LLM: age +
# importance + triviality together. It is deliberately CONSERVATIVE — any
# doubt, error, timeout or unparseable answer means KEEP, never archive.
#
# ``'archived'`` is a new TERMINAL artifact ``state`` value (state is free text —
# no schema change). Archiving is NON-DESTRUCTIVE: only the state flips, the
# content and every edge are preserved, so an archived artifact stays fully
# recoverable (``?state=archived`` in the knowledge UI, or promote it back).

_ARCHIVED_STATE = "archived"
# Only ACTIVE, curated artifacts are assessed: distilled + canonical. Raw
# per-message provenance stubs are the ``decay`` pass's business, and already
# ``archived`` artifacts are terminal.
_ACTIVE_STATES = ("distilled", "canonical")
_EXCERPT_CHARS = 600
_RELEVANCE_TIMEOUT = 45  # seconds — ONE cheap yes/no per artifact

_RELEVANCE_SYSTEM = (
    "You are the Librarian for a goal-driven personal assistant, curating its "
    "long-term memory. You are shown ONE stored information artifact (its title, "
    "a content excerpt, its age in days, its lifecycle state and its tags). "
    "Decide whether it is still worth KEEPING in long-term memory, or is stale / "
    "trivial / superseded / a one-off and safe to ARCHIVE.\n"
    "KEEP if it carries durable value: a decision, a commitment, a fact about a "
    "person/project the assistant should remember, an ongoing goal, reference "
    "knowledge, anything the assistant would want to recall months from now.\n"
    "ARCHIVE only when it is clearly low-value NOW: e.g. a months-old summary of "
    "a passing chat with an acquaintance, ephemeral chit-chat, an expired/handled "
    "one-off, or content plainly superseded. Age alone is NOT enough — an old but "
    "important fact stays. Recent artifacts almost always KEEP.\n"
    "Be CONSERVATIVE: when unsure, KEEP. Reply with EXACTLY one word — KEEP or "
    "ARCHIVE — and nothing else."
)


def _render_relevance_prompt(features: dict) -> str:
    """Render the small feature dict into the judge's user prompt."""
    tags = ", ".join(features.get("tags") or []) or "(none)"
    return (
        f"TITLE: {features.get('title') or '(untitled)'}\n"
        f"AGE (days): {features.get('age_days')}\n"
        f"STATE: {features.get('state')}\n"
        f"TAGS: {tags}\n"
        f"CONTENT EXCERPT:\n{features.get('excerpt') or '(empty)'}\n\n"
        "Should this be kept in long-term memory? Answer KEEP or ARCHIVE."
    )


def _parse_relevance(text: Optional[str]) -> bool:
    """Parse the judge's reply into KEEP(True)/ARCHIVE(False).

    CONSERVATIVE: anything ambiguous or unparseable → KEEP. Only an unambiguous
    ARCHIVE (says ARCHIVE and not KEEP) archives.
    """
    t = (text or "").strip().upper()
    if not t:
        return True
    has_archive = "ARCHIVE" in t
    has_keep = "KEEP" in t
    if has_archive and not has_keep:
        return False
    return True  # KEEP, both, or neither → keep


def default_relevance_judge(features: dict) -> bool:
    """Production judge: ONE cheap headless ``claude -p`` yes/no, proxy stripped.

    Mirrors ``default_summarizer`` (same binary, tools disallowed, hard timeout,
    Anthropic on the direct route). **Fail-soft:** timeout / non-zero exit / any
    error / unparseable answer all return ``True`` (KEEP) — the pass never
    archives on doubt. **Never invoked by the tests** — they pass a fake judge.
    """
    prompt = _render_relevance_prompt(features)
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _RELEVANCE_SYSTEM,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_RELEVANCE_TIMEOUT, input=prompt,
        )
    except subprocess.TimeoutExpired:
        log.warning("librarian: relevance judge timed out — KEEP")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("librarian: relevance judge error: %s — KEEP", e)
        return True
    if r.returncode != 0:
        log.warning("librarian: relevance judge rc=%s stderr=%s — KEEP",
                    r.returncode, (r.stderr or "")[:200])
        return True
    return _parse_relevance(r.stdout)


def _relevance_features(artifact: Entity, now_ts: Optional[int]) -> dict:
    """Build the small feature dict handed to a RelevanceJudge for one artifact.

    Tags are read from ``artifact.payload['tags']`` if a caller (``relevance_pass``)
    enriched the entity in-memory; otherwise empty. AGE is in days from
    ``created_at``, clamped at >= 0.
    """
    now = now_ts if now_ts is not None else int(time.time())
    age_days = max(0.0, (now - artifact.created_at) / 86400.0)
    excerpt = re.sub(r"\s+", " ", (artifact.get("content") or "").strip())[:_EXCERPT_CHARS]
    return {
        "title": artifact.label or artifact.id,
        "excerpt": excerpt,
        "age_days": round(age_days, 1),
        "state": artifact.get("state", "raw"),
        "tags": artifact.get("tags") or [],
    }


def assess_relevance(artifact: Entity, judge: Optional[RelevanceJudge] = None,
                     now_ts: Optional[int] = None) -> bool:
    """Judge whether ONE artifact is still worth keeping. ``True`` = KEEP.

    Builds the feature dict (title, content excerpt, age in days, state, tags)
    and delegates the verdict to the pluggable ``judge`` (default: a headless
    claude yes/no; tests inject a fake). **CONSERVATIVE fail-soft:** if the judge
    raises, returns ``True`` (KEEP) — we never archive on doubt.
    """
    judge = judge or default_relevance_judge
    features = _relevance_features(artifact, now_ts)
    try:
        return bool(judge(features))
    except Exception as e:  # noqa: BLE001
        log.warning("librarian: assess_relevance judge raised: %s — KEEP", e)
        return True


def relevance_pass(conn, judge: Optional[RelevanceJudge] = None,
                   min_age_days: float = 14, cap: int = 40,
                   now_ts: Optional[int] = None) -> dict:
    """Assess ACTIVE artifacts for relevance; ARCHIVE the stale/irrelevant ones.

    Scans artifacts whose ``state`` is ``distilled`` or ``canonical`` (not raw,
    not already archived) and whose AGE is at least ``min_age_days`` — recent
    artifacts are given a grace period and are NOT assessed. Processes OLDEST
    FIRST, at most ``cap`` per pass (bounds the LLM fan-out). For each, if
    ``assess_relevance`` says archive, the artifact's ``state`` is flipped to
    ``'archived'`` (NON-DESTRUCTIVE: content + edges preserved, recoverable).

    Returns ``{"assessed": n, "archived": m}``.
    """
    now = now_ts if now_ts is not None else int(time.time())
    cutoff = now - int(min_age_days * 86400)
    # by_type is ordered by created_at ascending == oldest first.
    candidates = [a for a in _curatable(conn, store.by_type(conn, "artifact"))
                  if a.get("state") in _ACTIVE_STATES and a.created_at <= cutoff]
    candidates = candidates[:cap]

    assessed = 0
    archived = 0
    for art in candidates:
        # Enrich in-memory (not persisted) so the judge sees the tags.
        art.payload["tags"] = store.tags_of(conn, art.id)
        assessed += 1
        if not assess_relevance(art, judge=judge, now_ts=now):
            store.update_payload(conn, art.id, state=_ARCHIVED_STATE)
            archived += 1
    return {"assessed": assessed, "archived": archived}


# ===========================================================================
# 7. semantic_dedup — LLM-judged near-duplicate collapse (non-destructive).
# ===========================================================================
#
# The exact/fuzzy ``dedup`` above only catches artifacts with near-identical
# TEXT. This pass catches artifacts that state the SAME knowledge phrased
# differently — e.g. two summaries of the same chat written on the same day, or
# two notes about the same decision. It groups CANDIDATES (artifacts sharing a
# tag), judges pairs with a pluggable ``judge`` (default: a cheap headless
# claude SAME/DIFFERENT), unions duplicates into clusters, then for each cluster
# KEEPS the best (most canonical / most complete / oldest) and ARCHIVES the
# losers with a ``loser -duplicate_of-> keeper`` edge (NON-DESTRUCTIVE: content +
# edges preserved, recoverable). BOUNDED by ``cap`` judged pairs. CONSERVATIVE
# fail-soft: any judge error / ambiguity → NOT a duplicate.

_SEMDEDUP_TIMEOUT = 45  # seconds — ONE cheap SAME/DIFFERENT per pair
_SEMDEDUP_SYSTEM = (
    "You are the Librarian for a goal-driven assistant, de-duplicating its "
    "long-term memory. You are shown TWO stored information artifacts (each: a "
    "title, a content excerpt and its tags). Decide whether they state the SAME "
    "underlying knowledge — the same facts / decision / information — even if "
    "worded differently or at different length. They are the SAME if one is "
    "essentially a rephrasing, subset or superset of the other's substance; they "
    "are DIFFERENT if either carries a distinct fact the other lacks.\n"
    "Be CONSERVATIVE: when unsure, answer DIFFERENT (better to keep both than to "
    "wrongly archive a unique fact). Reply with EXACTLY one word — SAME or "
    "DIFFERENT — and nothing else."
)


def _semantic_features(artifact: Entity, tags: Optional[list[str]] = None) -> dict:
    """Small feature dict for one artifact handed to a SemanticJudge."""
    excerpt = re.sub(r"\s+", " ", (artifact.get("content") or "").strip())[:_EXCERPT_CHARS]
    return {
        "title": artifact.label or artifact.id,
        "excerpt": excerpt,
        "tags": tags if tags is not None else [],
    }


def _render_semdedup_prompt(a: dict, b: dict) -> str:
    """Render two feature dicts into the SAME/DIFFERENT judge prompt."""
    def _fmt(x: dict, n: int) -> str:
        tags = ", ".join(x.get("tags") or []) or "(none)"
        return (f"ARTIFACT {n}:\n"
                f"  TITLE: {x.get('title') or '(untitled)'}\n"
                f"  TAGS: {tags}\n"
                f"  CONTENT EXCERPT:\n{x.get('excerpt') or '(empty)'}")
    return (f"{_fmt(a, 1)}\n\n{_fmt(b, 2)}\n\n"
            "Do these two artifacts state the SAME knowledge? "
            "Answer SAME or DIFFERENT.")


def _parse_semdedup(text: Optional[str]) -> bool:
    """Parse the judge's reply into duplicate(True)/not(False).

    CONSERVATIVE: anything ambiguous or unparseable → NOT a duplicate. Only an
    unambiguous SAME (says SAME and not DIFFERENT) collapses.
    """
    t = (text or "").strip().upper()
    if not t:
        return False
    has_same = "SAME" in t
    has_diff = "DIFFER" in t  # matches DIFFERENT / DIFFER
    if has_same and not has_diff:
        return True
    return False


def default_semantic_judge(a: dict, b: dict) -> bool:
    """Production semantic judge: ONE cheap headless ``claude -p`` SAME/DIFFERENT.

    Mirrors ``default_relevance_judge`` (same binary, tools disallowed, proxy
    stripped, hard timeout). **Fail-soft + CONSERVATIVE:** timeout / non-zero
    exit / any error / unparseable answer all return ``False`` (NOT a duplicate) —
    the pass never archives on doubt. **Never invoked by the tests.**
    """
    prompt = _render_semdedup_prompt(a, b)
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _SEMDEDUP_SYSTEM,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_SEMDEDUP_TIMEOUT, input=prompt,
        )
    except subprocess.TimeoutExpired:
        log.warning("librarian: semantic judge timed out — DIFFERENT")
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("librarian: semantic judge error: %s — DIFFERENT", e)
        return False
    if r.returncode != 0:
        log.warning("librarian: semantic judge rc=%s stderr=%s — DIFFERENT",
                    r.returncode, (r.stderr or "")[:200])
        return False
    return _parse_semdedup(r.stdout)


def _semantic_keeper(group: list[Entity]) -> Entity:
    """Survivor of a semantic-duplicate cluster: most-canonical, then most
    complete (longest content), then oldest."""
    return sorted(
        group,
        key=lambda e: (-_STATE_RANK.get(e.get("state", "raw"), 0),
                       -len(e.get("content") or ""), e.created_at),
    )[0]


def semantic_dedup(conn, judge: Optional[SemanticJudge] = None,
                   cap: int = 40) -> dict:
    """Collapse artifacts that state the SAME knowledge phrased differently.

    Considers ACTIVE artifacts (``distilled`` / ``canonical``; raw provenance and
    already-archived are left alone). CANDIDATE pairs are artifacts SHARING A TAG
    (bounds the comparison space to plausibly-related artifacts). Text-identical
    pairs union for free (no LLM call). Other candidate pairs are judged by the
    pluggable ``judge`` (default: headless claude), at most ``cap`` judged pairs
    per pass. Duplicates are unioned into clusters; for each cluster the best
    artifact is KEPT and the losers are ARCHIVED (``state='archived'``) with a
    ``loser -duplicate_of-> keeper`` edge. NON-DESTRUCTIVE + fail-soft (a raising
    judge is treated as NOT-duplicate).

    Returns ``{"pairs_judged": n, "archived": m}``.
    """
    judge = judge or default_semantic_judge
    active = [a for a in _curatable(conn, store.by_type(conn, "artifact"))
              if a.get("state") in _ACTIVE_STATES]
    if len(active) < 2:
        return {"pairs_judged": 0, "archived": 0}

    byid = {a.id: a for a in active}
    tagmap = {a.id: store.tags_of(conn, a.id) for a in active}

    # Union-find over artifact ids.
    parent = {aid: aid for aid in byid}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    # Candidate pairs = artifacts sharing >= 1 tag (deduped, deterministic order).
    tag_groups: dict[str, list[Entity]] = defaultdict(list)
    for a in active:
        for t in tagmap[a.id]:
            tag_groups[t].append(a)
    pair_set: set[tuple[str, str]] = set()
    for t in sorted(tag_groups):
        m = tag_groups[t]
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                pair_set.add(tuple(sorted((m[i].id, m[j].id))))
    pairs = sorted(
        pair_set,
        key=lambda p: (byid[p[0]].created_at, byid[p[1]].created_at, p))

    pairs_judged = 0
    for x, y in pairs:
        if find(x) == find(y):
            continue  # already in the same cluster — no need to judge
        # Text-identical → duplicate for free (no LLM call, no cap consumed).
        if _content_hash(byid[x].get("content")) == _content_hash(byid[y].get("content")):
            union(x, y)
            continue
        if pairs_judged >= cap:
            break
        pairs_judged += 1
        try:
            dup = bool(judge(_semantic_features(byid[x], tagmap[x]),
                             _semantic_features(byid[y], tagmap[y])))
        except Exception as e:  # noqa: BLE001
            log.warning("librarian: semantic_dedup judge raised: %s — skip", e)
            dup = False
        if dup:
            union(x, y)

    clusters: dict[str, list[Entity]] = defaultdict(list)
    for aid in byid:
        clusters[find(aid)].append(byid[aid])

    archived = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        keeper = _semantic_keeper(members)
        for m in members:
            if m.id == keeper.id:
                continue
            store.add_edge(conn, m.id, keeper.id, "duplicate_of")
            store.update_payload(conn, m.id, state=_ARCHIVED_STATE)
            archived += 1
    return {"pairs_judged": pairs_judged, "archived": archived}


# ===========================================================================
# 8. consolidate_old — roll OLD per-chat daily summaries into a canonical rollup.
# ===========================================================================
#
# Chat-tracking writes one ``distilled`` artifact per chat per day (tagged
# ``chat:<id>``, label ``"<day> chat:<id>"``). Over weeks these granular dailies
# pile up. This pass rolls the OLD ones (age >= ``older_than_days``) for a chat
# into ONE higher-level ``canonical`` rollup (keeps the gist), links
# ``rollup -supersedes-> each daily`` and ARCHIVES the dailies (NON-DESTRUCTIVE).
# BOUNDED to ``cap`` chats per pass (one summarizer call per chat). Skips chats
# with < 2 old dailies. Fail-soft per chat.

_DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _chat_tag_of(tags: list[str]) -> Optional[str]:
    """The ``chat:<id>`` tag among an artifact's tags, if any."""
    for t in tags:
        if t.startswith("chat:"):
            return t
    return None


def consolidate_old(conn, older_than_days: float = 21,
                    summarizer: Optional[Summarizer] = None, cap: int = 20,
                    now_ts: Optional[int] = None) -> dict:
    """Roll OLD ``distilled`` per-chat daily summaries into a ``canonical`` rollup.

    Groups old (age >= ``older_than_days``) distilled artifacts by their
    ``chat:<id>`` tag. For each chat with >= 2 old dailies (up to ``cap`` chats),
    summarizes their contents into ONE canonical rollup tagged with the chat tag
    (label ``"<chat> — rollup thru <date>"``), adds ``rollup -supersedes-> each
    daily`` and flips each daily to ``state='archived'`` (content + edges kept).
    Recent dailies (younger than ``older_than_days``) are untouched. Fail-soft:
    a summarizer error / empty summary for one chat skips that chat only.

    Returns ``{"chats_rolled": n, "dailies_archived": m}``.
    """
    summarizer = summarizer or default_summarizer
    now = now_ts if now_ts is not None else int(time.time())
    cutoff = now - int(older_than_days * 86400)

    dailies = [a for a in _curatable(conn, store.by_type(conn, "artifact"))
               if a.get("state") == "distilled" and a.created_at <= cutoff]
    groups: dict[str, list[Entity]] = defaultdict(list)
    for a in dailies:
        ct = _chat_tag_of(store.tags_of(conn, a.id))
        if ct:
            groups[ct].append(a)

    chats_rolled = 0
    dailies_archived = 0
    for chat_tag in sorted(groups):
        if chats_rolled >= cap:
            break
        members = groups[chat_tag]  # oldest-first (by_type order)
        if len(members) < 2:
            continue  # nothing to roll up
        try:
            summary = summarizer([m.get("content") or "" for m in members])
        except Exception as e:  # noqa: BLE001
            log.warning("librarian: consolidate_old summarizer raised for %s: %s",
                        chat_tag, e)
            continue
        summary = (summary or "").strip()
        if not summary:
            continue  # fail-soft: no rollup, dailies untouched

        days = [m.group(1) for m in
                (_DAY_RE.search(e.label or "") for e in members) if m]
        thru = (max(days) if days
                else time.strftime("%Y-%m-%d",
                                   time.gmtime(max(e.created_at for e in members))))
        rollup = store.create_artifact(
            conn, content=summary, state="canonical",
            label=f"{chat_tag} — rollup thru {thru}", trust="proposed",
            tags=[chat_tag])
        for m in members:
            store.add_edge(conn, rollup.id, m.id, "supersedes")
            store.update_payload(conn, m.id, state=_ARCHIVED_STATE)
            dailies_archived += 1
        chats_rolled += 1
    return {"chats_rolled": chats_rolled, "dailies_archived": dailies_archived}


# ===========================================================================
# 9. enrich_context / enrich_pass — attach external web context to thin, but
#    important, artifacts (non-destructive; never overwrites the original).
# ===========================================================================
#
# Some artifacts mention a company / person / tool / topic that is under-
# specified. This pass RESEARCHES the key topic (the default researcher runs a
# bounded headless claude WITH WEB SEARCH ALLOWED — the web tools are deliberately
# NOT in ``--disallowed-tools``) and attaches the result as a SEPARATE linked
# ``canonical`` "context: <topic>" artifact (``original -relates_to-> note``).
# The original is NEVER overwritten; it is only marked with an ``enriched`` tag so
# it is not re-researched. BOUNDED to ``cap`` artifacts per pass. Fail-soft: if
# the researcher returns empty / raises, nothing happens.

_ENRICHED_TAG = "enriched"
_RESEARCH_TIMEOUT = 90  # seconds — one bounded web-research turn
# NOTE: WebSearch / WebFetch are intentionally OMITTED here so the researcher
# CAN reach the web. Every other tool stays disallowed.
_RESEARCH_DISALLOWED = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "Task", "NotebookEdit",
]
_RESEARCH_SYSTEM = (
    "You are the Librarian's researcher for a goal-driven assistant. Given a "
    "TOPIC drawn from a stored memory artifact, use web search to gather a few "
    "concrete, current, load-bearing facts that add useful external context "
    "(who/what it is, why it matters, key recent facts). Return a CONCISE plain-"
    "text context note of a few sentences — no preamble, no markdown fences. If "
    "you cannot find anything useful, return an empty response."
)


def _enrich_topic(artifact: Entity) -> str:
    """Derive the key research topic string from an artifact (title, else a
    short content excerpt)."""
    label = (artifact.label or "").strip()
    if label:
        return label[:200]
    return re.sub(r"\s+", " ", (artifact.get("content") or "").strip())[:200]


def default_researcher(topic: str) -> str:
    """Production researcher: ONE bounded headless ``claude -p`` WITH web search.

    Web tools are ALLOWED (not passed to ``--disallowed-tools``); all other tools
    stay disallowed. Proxy stripped, hard timeout. **Fail-soft:** timeout /
    non-zero exit / any error → ``""`` (the pass then does nothing). **Never
    invoked by the tests** — they pass a fake researcher.
    """
    topic = (topic or "").strip()
    if not topic:
        return ""
    prompt = f"Research this topic and return a concise context note:\n{topic}"
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _RESEARCH_SYSTEM,
        "--disallowed-tools", *_RESEARCH_DISALLOWED,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_RESEARCH_TIMEOUT, input=prompt,
        )
    except subprocess.TimeoutExpired:
        log.warning("librarian: researcher timed out")
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("librarian: researcher error: %s", e)
        return ""
    if r.returncode != 0:
        log.warning("librarian: researcher rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return ""
    return (r.stdout or "").strip()


def enrich_context(conn, artifact: Entity,
                   researcher: Optional[Researcher] = None) -> Optional[str]:
    """Research external context for ONE artifact; return a note or ``None``.

    Derives the key topic from the artifact and delegates to the pluggable
    ``researcher`` (default: bounded headless claude with web search). Returns the
    trimmed note, or ``None`` when the researcher yields nothing / raises
    (CONSERVATIVE fail-soft — the caller then leaves the artifact untouched).
    """
    researcher = researcher or default_researcher
    topic = _enrich_topic(artifact)
    if not topic:
        return None
    try:
        note = researcher(topic)
    except Exception as e:  # noqa: BLE001
        log.warning("librarian: enrich_context researcher raised: %s", e)
        return None
    note = (note or "").strip()
    return note or None


def enrich_pass(conn, researcher: Optional[Researcher] = None,
                cap: int = 6) -> dict:
    """Attach external web context to eligible artifacts (bounded, non-destructive).

    Selects up to ``cap`` eligible artifacts — ``distilled`` / ``canonical``, not
    already carrying the ``enriched`` tag, and not themselves a context note
    (label starting ``"context:"``) — and for each researches its key topic via
    ``enrich_context``. On a NON-EMPTY note it CREATES a separate linked
    ``canonical`` artifact (label ``"context: <topic>"``, content = the note),
    adds an ``original -relates_to-> note`` edge, and tags the original
    ``enriched`` so it is never re-researched. On an empty note / error nothing is
    created (fail-soft) and the original is left as-is (NEVER overwritten).

    Returns ``{"enriched": n, "notes_created": n}``.
    """
    researcher = researcher or default_researcher
    candidates = []
    for a in _curatable(conn, store.by_type(conn, "artifact")):
        if a.get("state") not in _ACTIVE_STATES:
            continue
        if (a.label or "").startswith("context:"):
            continue
        if _ENRICHED_TAG in store.tags_of(conn, a.id):
            continue
        candidates.append(a)
    candidates = candidates[:cap]

    enriched = 0
    for art in candidates:
        note = enrich_context(conn, art, researcher=researcher)
        if not note:
            continue  # fail-soft: nothing to attach, original untouched
        topic = _enrich_topic(art)
        ctx = store.create_artifact(
            conn, content=note, state="canonical",
            label=f"context: {topic}"[:200], trust="proposed")
        store.add_edge(conn, art.id, ctx.id, "relates_to")
        store.add_tag(conn, art.id, _ENRICHED_TAG)
        enriched += 1
    return {"enriched": enriched, "notes_created": enriched}
