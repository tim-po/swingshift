"""LLM extraction pipelines (chat-tracking system, increment 2).

Scan NEW messages in TRACKED chats and extract, via headless ``claude -p``
turns, three products into ``tracking.db`` (``tracking_store``):

  (a) action items / todos      \\  ONE extraction call (``extract_from_chat``),
  (b) meetings / calls          /   incremental on the new-message batch.
  (c) a suggested reply         —   SEPARATE call (``generate_reply``) with rich,
                                    layered context + a real-question gate;
                                    upserts at most one PENDING draft per chat.

This module is the PERIODIC extraction job (``tg_tracking_tick``, wired into the
scheduler at ~10-min cadence) plus the shared, reusable per-chat path
(``_process_chat`` → ``extract_from_chat`` + ``generate_reply``). The real-time
bridge hook reuses the very same path for hot-list chats on message arrival.

COST GUARDS (the system must NOT trigger a large LLM bill):
  - ``proj.tg_track_enabled`` (default False) gates EVERYTHING. The tick is a
    pure no-op for projects that have not opted in — no DB opens, no LLM calls.
  - NO history backfill: the first time a (slug, chat_id) is seen with no
    ``extract`` watermark, the watermark is initialised to the chat's CURRENT
    max ``msg_id`` and NOTHING is processed that pass. Only messages arriving
    AFTER enablement are ever extracted (the 45k existing messages are skipped).
  - ``MAX_BATCH`` caps how many newest-unprocessed messages a single chat pass
    feeds to the LLM.
  - Only INCOMING messages (``out`` is false in ``raw_json``) are processed; a
    chat with zero new incoming messages is skipped (no LLM call).
  - Only chats where ``tg_tracking.is_tracked`` is True are considered.

The LLM is invoked exactly like the TG brain (``tg_listener._run_coordinator_turn``):
the ``claude`` binary, ``--output-format text``,
``--dangerously-skip-permissions``, ALL tools disallowed, a hard timeout, and an
environment with the proxy stripped (Anthropic is on the direct route).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from bot_squad_worker import icloud_calendar, tg_tracking, tracking_store
from bot_squad_worker.tg_tracking import is_realtime, is_tracked

log = logging.getLogger(__name__)

# --- LLM invocation knobs (mirror tg_listener) -------------------------------
_CLAUDE_BIN = "claude"
# Extraction is a pure text->JSON transform: the model must NEVER touch the host.
_DISALLOWED_TOOLS = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
]
_EXTRACT_TIMEOUT = 120  # seconds

# --- Pipeline knobs ----------------------------------------------------------
# Single combined watermark for the extraction pass (todos+meetings+reply share
# one LLM call, so one cursor). Named distinctly from the design's per-pipeline
# names so the combined pass has its own cursor.
PIPELINE = "extract"
# COST GUARD: never feed more than this many newest-unprocessed messages to the
# LLM in one chat pass. Bursts beyond this are intentionally truncated to the
# newest window (watermark still advances past them) so a flood can't fan out.
MAX_BATCH = 40


# ---------------------------------------------------------------------------
# Headless LLM helper.
# ---------------------------------------------------------------------------


def _claude_env() -> dict:
    """Env for the claude subprocess WITHOUT proxy vars.

    Mirrors ``tg_listener._claude_env``: the worker routes its own egress through
    the Xray/WARP proxy to reach the DPI-blocked Telegram API, but Anthropic is
    reachable directly and the proxy is a slow bottleneck. Strip it for claude.
    """
    env = dict(os.environ)
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        env.pop(k, None)
    return env


def _first_json_obj(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` block in ``text``, or None.

    Brace-counts while respecting string literals and escapes so braces inside
    quoted strings don't unbalance the scan. Tolerates prose/markdown fences
    around the JSON (the model is told to return only JSON, but be robust).
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _extract_llm(system_prompt: str, user_payload: str) -> dict:
    """Run one headless ``claude -p`` turn; return the parsed JSON object.

    Returns ``{}`` on timeout, non-zero exit, missing/malformed JSON, or any
    other failure — callers treat ``{}`` as "extracted nothing", so a flaky LLM
    pass simply yields no rows rather than crashing the tick.
    """
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", system_prompt,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_EXTRACT_TIMEOUT, input=user_payload,
        )
    except subprocess.TimeoutExpired:
        log.warning("tg_pipelines: extraction LLM timed out")
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("tg_pipelines: extraction LLM error: %s", e)
        return {}
    if r.returncode != 0:
        log.warning("tg_pipelines: extraction LLM rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return {}
    out = (r.stdout or "").strip()
    raw = _first_json_obj(out)
    if raw is None:
        log.warning("tg_pipelines: no JSON object in LLM output (len=%d)", len(out))
        return {}
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("tg_pipelines: JSON parse failed: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _reply_llm(system_prompt: str, user_payload: str) -> dict:
    """Run the suggested-reply LLM turn; return the parsed JSON object or ``{}``.

    A DISTINCT symbol from ``_extract_llm`` (though it shares the same headless
    runner) so the reply call is independently monkeypatchable in tests and so a
    reply pass can be counted/skipped separately from the todos/meetings pass.
    """
    return _extract_llm(system_prompt, user_payload)


def _summary_llm(system_prompt: str, user_payload: str) -> str:
    """Run one headless ``claude -p`` turn for a day summary; return PLAIN TEXT.

    Mirrors ``_extract_llm`` (same binary, tools disallowed, proxy stripped, hard
    timeout) but the summary is a short free-text blurb, so this returns the
    trimmed stdout string. Tolerates a model that wraps its answer in a
    ``{"summary": "..."}`` JSON object. Returns ``""`` on timeout / non-zero exit
    / any failure, and callers treat ``""`` as "no summary" — a flaky pass simply
    records nothing (and is retried on the next run) rather than crashing.

    A DISTINCT symbol from ``_extract_llm`` so the summary call is independently
    monkeypatchable in tests (tests NEVER spawn a real ``claude``).
    """
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", system_prompt,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_EXTRACT_TIMEOUT, input=user_payload,
        )
    except subprocess.TimeoutExpired:
        log.warning("tg_pipelines: summary LLM timed out")
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("tg_pipelines: summary LLM error: %s", e)
        return ""
    if r.returncode != 0:
        log.warning("tg_pipelines: summary LLM rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return ""
    out = (r.stdout or "").strip()
    # Be tolerant if the model wrapped the blurb in a JSON object.
    raw = _first_json_obj(out)
    if raw is not None:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("summary"):
                return str(data["summary"]).strip()
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# Semantic meeting-dedup judge — decides whether two meeting descriptions name
# the SAME real event, even when phrased differently (Tim's recurring "duplicate
# meetings" bug: e.g. two "coffee with my father" rows worded differently). ONE
# cheap headless ``claude`` call per pair, yes/no. Fully PLUGGABLE: every caller
# takes a ``judge`` param, so tests inject a FAKE judge and NEVER spawn claude.
# ---------------------------------------------------------------------------

_SAME_MEETING_SYSTEM_PROMPT = (
    "You are a deduplication judge for a personal calendar. You are given two "
    "meeting descriptions (title, date, people, location), OFTEN phrased "
    "differently or in different languages. Decide if they are the SAME "
    "real-world event.\n"
    "STRONG RULE: if the two share the same DATE and TIME (or nearly) AND involve "
    "the same person(s), they are almost certainly the SAME event — EVEN IF the "
    "titles are worded completely differently or in different languages. Example: "
    "'Meet Pops at Fancy' and 'Обед или кофе с Pops' at the same date/time are the "
    "SAME event. Title wording, language, phrasing, and location detail do NOT "
    "make them different when the person and date/time match.\n"
    "Answer 'no' ONLY if there is a REAL conflict: clearly different people, or "
    "clearly different dates/times, or activities that genuinely cannot be the "
    "same event.\n"
    "Answer with ONE word only: 'yes' or 'no'."
)


def _mget(m: Any, key: str) -> Any:
    """Read ``key`` from a meeting that may be a dict or a sqlite3.Row."""
    if isinstance(m, dict):
        return m.get(key)
    try:
        return m[key]
    except (KeyError, IndexError, TypeError):
        return None


def _meeting_desc(m: Any) -> str:
    """Compact one-line description of a meeting for the judge prompt."""
    return "; ".join((
        f"title: {_mget(m, 'title') or '(none)'}",
        f"date: {_mget(m, 'start_dt') or '(unknown)'}",
        f"people: {_mget(m, 'participants') or '(unknown)'}",
        f"location: {_mget(m, 'location') or '(unknown)'}",
    ))


def _same_meeting_llm(a: Any, b: Any) -> bool:
    """Headless ``claude`` yes/no: are ``a`` and ``b`` the SAME real event?

    Mirrors ``_extract_llm`` (same binary, tools disallowed, proxy stripped, hard
    timeout). CONSERVATIVE fail-soft: any timeout / non-zero exit / unparseable
    answer / error returns ``False`` (treat as DIFFERENT), so a flaky judge never
    causes a wrong merge or a dropped meeting. Distinct symbol so tests inject a
    fake judge and never spawn a real ``claude``.
    """
    payload = (
        "Meeting A: " + _meeting_desc(a) + "\n"
        "Meeting B: " + _meeting_desc(b) + "\n"
        "Are these the SAME real event? Answer yes or no."
    )
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", _SAME_MEETING_SYSTEM_PROMPT,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_EXTRACT_TIMEOUT, input=payload,
        )
    except Exception as e:  # noqa: BLE001 - includes TimeoutExpired
        log.warning("tg_pipelines: same-meeting judge error: %s", e)
        return False
    if r.returncode != 0:
        log.warning("tg_pipelines: same-meeting judge rc=%s", r.returncode)
        return False
    ans = (r.stdout or "").strip().lower().lstrip("\"'` \t")
    return ans.startswith("y")


def _meeting_date(start_dt: Optional[str]):
    """Parse a meeting ``start_dt`` to a ``date`` (day only), or None.

    Tolerant of the same ISO/space formats ``_meeting_is_past`` accepts. Used to
    bucket candidates by calendar day for the dedup grouping.
    """
    import datetime as _dt
    s = (start_dt or "").strip()
    if not s:
        return None
    dt = None
    try:
        dt = _dt.datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    return dt.date() if dt is not None else None


def _near_day(a_start: Optional[str], b_start: Optional[str]) -> bool:
    """True if two same-chat meetings are worth COMPARING as dedup candidates.

    At least one has NO date ⇒ True — let the judge decide (a no-date copy can be
    the same event as a dated one, e.g. "call mom" vs "Звонок с мамой 11:00").
    Both dated ⇒ within ~1 calendar day.
    """
    da, db = _meeting_date(a_start), _meeting_date(b_start)
    if da is None or db is None:
        return True
    return abs((da - db).days) <= 1


# Bound on how many existing candidates the at-insert dedup guard will judge, and
# the max size of a day-group dedupe_meetings will pairwise-compare — a hard cap
# on LLM fan-out so a busy day / chat can never explode the judge calls.
_DEDUP_CANDIDATE_CAP = 6
_DEDUP_GROUP_CAP = 8


def _meeting_quality(m: Any) -> tuple:
    """Keep-best ranking key (higher = better; ties → earliest created/id).

    Prefers, in order: has an ``ics_uid`` (already on the calendar) → a decided/
    live ``status`` (invited/accepted/approved) → more complete fields → the
    EARLIEST created row (stable, oldest wins) → lowest id.
    """
    status = (_mget(m, "status") or "")
    completeness = sum(
        1 for k in ("start_dt", "end_dt", "location", "participants", "link")
        if _mget(m, k)
    )
    created = _mget(m, "created_at") or 0
    mid = _mget(m, "id") or 0
    return (
        1 if _mget(m, "ics_uid") else 0,
        1 if status in ("invited", "accepted", "approved") else 0,
        completeness,
        -int(created),
        -int(mid),
    )


def _merge_meeting_detail(
    conn: sqlite3.Connection, keep: Any, incoming: dict
) -> None:
    """Backfill fields the kept row is MISSING from a fresh duplicate detection.

    Non-destructive: only fills a NULL/empty column on ``keep`` (never overwrites
    an existing value), so a re-worded duplicate can still contribute a detail the
    first row lacked (e.g. a location).
    """
    updates: dict = {}
    for k in ("start_dt", "end_dt", "location", "participants", "link"):
        if not _mget(keep, k) and incoming.get(k):
            updates[k] = incoming[k]
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE meetings SET {sets} WHERE id = ?",  # noqa: S608 - keys are literals
        (*updates.values(), int(_mget(keep, "id"))),
    )


def _find_semantic_dup(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    incoming: dict,
    judge: Callable[[Any, Any], bool],
) -> Optional[sqlite3.Row]:
    """Return an existing meeting the ``incoming`` one duplicates, or None.

    Scans recent NON-duplicate meetings for (slug, chat_id) whose date is NEAR
    the incoming one, and asks ``judge`` (capped at ``_DEDUP_CANDIDATE_CAP``
    calls). FAIL-SOFT: if the judge raises, we abort the scan and return None so
    the caller inserts the meeting rather than risk dropping a real one.
    """
    candidates = tracking_store.recent_meetings_for_chat(conn, slug, chat_id)
    checked = 0
    for ex in candidates:
        if not _near_day(incoming.get("start_dt"), _mget(ex, "start_dt")):
            continue
        if checked >= _DEDUP_CANDIDATE_CAP:
            break
        checked += 1
        try:
            if judge(incoming, ex):
                return ex
        except Exception as e:  # noqa: BLE001 - judge is fail-soft
            log.warning("tg_pipelines: dedup judge raised, inserting: %s", e)
            return None
    return None


def dedupe_meetings(
    conn: sqlite3.Connection,
    slug: str,
    judge: Optional[Callable[[Any, Any], bool]] = None,
) -> dict:
    """Semantic-dedupe a slug's meetings; mark redundant rows ``status='duplicate'``.

    Groups candidates by calendar day (dated meetings) or by chat (null-start
    meetings), then within each group uses ``judge`` (default: the headless
    ``_same_meeting_llm``) to cluster SAME-event rows. For each cluster of >1 the
    BEST row survives (see ``_meeting_quality``) and the losers are marked
    ``status='duplicate'`` with ``dup_of`` pointing at the survivor
    (non-destructive / recoverable). LLM fan-out is bounded: only rows in the
    same day/chat group are compared, and each group is capped at
    ``_DEDUP_GROUP_CAP`` rows.

    Returns ``{"groups_examined": n, "duplicates_marked": n}``.
    """
    if judge is None:
        judge = _same_meeting_llm
    rows = list(conn.execute(
        "SELECT * FROM meetings WHERE slug = ? AND status IS NOT 'duplicate' "
        "ORDER BY id ASC",
        (slug,),
    ).fetchall())

    # Bucket by CHAT — duplicates almost always come from the SAME conversation.
    # Within a chat, only *comparable* pairs are judged (see the _near_day filter
    # in the pairwise loop): both dated on the same/near day, OR at least one has
    # no date (so a no-date copy dedupes against a dated copy of the same event).
    groups: dict[Any, list] = {}
    for r in rows:
        groups.setdefault(_mget(r, "chat_id"), []).append(r)

    groups_examined = 0
    duplicates_marked = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        groups_examined += 1
        members = members[:_DEDUP_GROUP_CAP]  # cap LLM fan-out per group

        # Union-find over pairwise SAME-event judgements.
        parent = {int(_mget(m, "id")): int(_mget(m, "id")) for m in members}

        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if not _near_day(_mget(members[i], "start_dt"),
                                 _mget(members[j], "start_dt")):
                    continue   # same chat but far-apart dated → not the same event
                try:
                    same = bool(judge(members[i], members[j]))
                except Exception as e:  # noqa: BLE001 - judge is fail-soft
                    log.warning("tg_pipelines: dedupe judge raised, skipping pair: %s", e)
                    continue
                if same:
                    ri, rj = _find(int(_mget(members[i], "id"))), _find(int(_mget(members[j], "id")))
                    if ri != rj:
                        parent[ri] = rj

        clusters: dict[int, list] = {}
        for m in members:
            clusters.setdefault(_find(int(_mget(m, "id"))), []).append(m)

        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            best = max(cluster, key=_meeting_quality)
            best_id = int(_mget(best, "id"))
            for m in cluster:
                if int(_mget(m, "id")) == best_id:
                    continue
                tracking_store.mark_meeting_duplicate(conn, int(_mget(m, "id")), best_id)
                duplicates_marked += 1

    return {"groups_examined": groups_examined, "duplicates_marked": duplicates_marked}


# ---------------------------------------------------------------------------
# Prompt construction + extraction.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an extraction engine for a personal Telegram assistant. You are "
    "given a short transcript of recent INCOMING messages from one chat. "
    "Extract, conservatively, two things and return them as a SINGLE JSON "
    "object and NOTHING else (no prose, no markdown fences):\n"
    '{"todos": [{"text": str, "suggested_action": str|null, "actionable": '
    'bool}], "meetings": [{"title": str, "start_dt": ISO8601-or-null, '
    '"end_dt": ISO8601-or-null, "location": str|null, "participants": str|null,'
    ' "link": str|null}]}\n'
    "Rules: include a todo only for a concrete action item the user must do; "
    "set actionable=true only if a software swarm could plausibly do it. "
    "Include a meeting only if a real call/meeting with a time or clear intent "
    "is discussed. If nothing is found, return empty lists. Return ONLY the "
    "JSON object."
)

# Tim's voice, baked into every AS-TIM generation surface (suggested replies +
# /swarm-answer drafts — NOT the disclosed "🤖 Claude" auto-reply). Tim rejected
# assistant-voice drafts outright (board 7843f515): pleasantries, filler and
# helper phrasing read as fake coming from him. Both payload builders label his
# own outgoing lines "Tim (you)", so "mirror his earlier messages" is grounded.
_TIM_VOICE = (
    "VOICE — the text must read as a message Tim typed himself, never as an "
    "assistant:\n"
    "- Terse. Say only what is needed; a short sentence or fragment is usually "
    "enough.\n"
    "- NO pleasantries or filler: no greetings, no 'hope you're well', no "
    "'thanks for reaching out', no sign-offs, no 'let me know if anything'.\n"
    "- No assistant phrasing ('I'd be happy to', 'sure thing!', 'great "
    "question') and no bureaucratic hedging.\n"
    "- Mirror Tim's OWN earlier messages in the conversation (the lines marked "
    "\"Tim (you)\"): match their length, casing, punctuation and emoji habits. "
    "When in doubt, shorter and plainer.\n"
    "- Same LANGUAGE as the conversation.\n"
    "- Lead with the answer or decision; add a reason only when it matters."
)

# Suggested-reply generation is a SEPARATE LLM call (reply-pipeline v2) with its
# own RICH, LAYERED context and a strict real-question gate. Kept apart from the
# todos/meetings extraction so each prompt stays focused and the reply call can
# be skipped entirely when there is nothing awaiting the user (cost guard).
_REPLY_SYSTEM_PROMPT = (
    "You are drafting a reply AS the user (Tim) for one of his personal "
    "Telegram chats. You are given three layers of context: the UNANSWERED "
    "messages he has not yet responded to, the RECENT CONVERSATION for tone and "
    "immediate context, and a BROADER history of daily summaries for background. "
    "Decide whether the unanswered messages genuinely await a reply from Tim.\n"
    "REAL-QUESTION GATE — set needed=true ONLY if the unanswered messages "
    "contain a genuine question directed at Tim, or a conversational turn that "
    "is actually awaiting his response (someone asked him something, is waiting "
    "on his decision or answer, or the thread clearly expects his reply). Set "
    "needed=false for mere statements, acknowledgements, broadcasts or "
    "forwards, bare links, reactions, or group chatter not directed at him. Be "
    "conservative: when unsure, needed=false.\n"
    "When needed=true, write the reply that answers the unanswered messages. "
    "When needed=false, text must be empty.\n"
    + _TIM_VOICE + "\n"
    'Return ONLY this JSON object and nothing else: {"needed": bool, '
    '"text": str}'
)


def _fmt_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
    except Exception:  # noqa: BLE001
        return "?"


def _build_transcript(chat_title: Optional[str], messages: list[dict]) -> str:
    """Compact human-readable transcript from message dicts (ts/sender/text)."""
    lines = [f"Chat: {chat_title or '(unknown)'}", "Recent messages:"]
    for m in messages:
        sender = (m.get("sender_name") or "Unknown").strip()
        text = (m.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{_fmt_ts(m.get('ts'))}] {sender}: {text}")
    return "\n".join(lines)


def _as_text(v: Any) -> Optional[str]:
    """Coerce an LLM field to a string (joining lists), or None if empty."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v if str(x).strip())
    s = str(v).strip()
    return s or None


def extract_from_chat(
    cfg: Any,
    proj: Any,
    chat_id: int,
    chat_title: Optional[str],
    messages: list[dict],
    judge: Optional[Callable[[Any, Any], bool]] = None,
) -> dict:
    """Extract todos/meetings/reply from ``messages`` and write to tracking.db.

    ``messages`` is a list of dicts with at least ``msg_id``, ``ts``,
    ``sender_name``, ``text`` (chronological order preferred). Rows are stamped
    with ``slug``/``chat_id``/``chat_title``, ``msg_id`` = the latest in the
    batch, that message's ``ts``, and ``created_at`` = now.

    Handles ONLY todos + meetings (incremental on the new-message batch).
    Suggested replies are generated separately by ``generate_reply`` (its own
    LLM call with richer context), wired alongside this in ``_process_chat``.

    Returns a small summary ``{"todos": n, "meetings": n}``. Reusable by the
    periodic tick AND the real-time bridge hook.
    """
    slug = getattr(proj, "slug", "") or ""
    if judge is None:
        judge = _same_meeting_llm
    summary = {"todos": 0, "meetings": 0}
    if not messages:
        return summary

    latest = max(messages, key=lambda m: int(m.get("msg_id") or 0))
    latest_msg_id = int(latest.get("msg_id") or 0)
    latest_ts = int(latest.get("ts") or 0)

    transcript = _build_transcript(chat_title, messages)
    data = _extract_llm(_SYSTEM_PROMPT, transcript)
    if not data:
        return summary

    now = int(time.time())
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        for t in data.get("todos") or []:
            if not isinstance(t, dict):
                continue
            text = _as_text(t.get("text"))
            if not text:
                continue
            tracking_store.insert_todo(
                conn, slug, chat_id, chat_title, latest_msg_id, latest_ts,
                text, suggested_action=_as_text(t.get("suggested_action")),
                actionable=bool(t.get("actionable")), created_at=now,
            )
            summary["todos"] += 1

        for m in data.get("meetings") or []:
            if not isinstance(m, dict):
                continue
            title = _as_text(m.get("title"))
            if not title:
                continue
            start_dt = _as_text(m.get("start_dt"))
            end_dt = _as_text(m.get("end_dt"))
            location = _as_text(m.get("location"))
            # DEDUP (duplicate-invite fix): if this exact meeting (slug, chat,
            # title, start_dt) already reached the calendar — has an ics_uid or
            # is invited/accepted — don't insert it again or write a 2nd invite.
            existing = tracking_store.find_meeting(
                conn, slug, chat_id, title, start_dt)
            if existing is not None and (
                existing["ics_uid"]
                or existing["status"] in ("invited", "accepted")
            ):
                continue
            # SEMANTIC dedup (Tim's re-worded-duplicate bug): before inserting a
            # NEW row, ask the pluggable judge whether this is the SAME event as a
            # recent meeting in this chat/day. If so, backfill any missing detail
            # onto the survivor and SKIP the insert (no second row). Fail-soft:
            # ``_find_semantic_dup`` returns None on any judge error, so a flaky
            # judge falls back to the exact current behaviour and never drops a
            # real meeting.
            incoming = {
                "title": title, "start_dt": start_dt, "end_dt": end_dt,
                "location": location,
                "participants": _as_text(m.get("participants")),
                "link": _as_text(m.get("link")),
            }
            sem_dup = _find_semantic_dup(conn, slug, chat_id, incoming, judge)
            if sem_dup is not None:
                _merge_meeting_detail(conn, sem_dup, incoming)
                continue
            meeting_id = tracking_store.insert_meeting(
                conn, slug, chat_id, chat_title, latest_msg_id, latest_ts,
                title, start_dt=start_dt, end_dt=end_dt, location=location,
                participants=_as_text(m.get("participants")),
                link=_as_text(m.get("link")), created_at=now,
            )
            summary["meetings"] += 1
            # AUTO-SCHEDULE: a detected meeting with a real start time goes
            # straight into the iCloud Swarm calendar as a PLAIN, editable event
            # (no invitation) — Tim edits the time/title or deletes it in Apple
            # Calendar and the two-way-sync reconcile reads that back. A meeting
            # with NULL start_dt stays status='new' (nothing to schedule).
            _maybe_schedule_meeting(
                cfg, conn, meeting_id, title, start_dt, end_dt, location)
    finally:
        conn.close()
    return summary


def _meeting_is_past(start_dt: str) -> bool:
    """True if the meeting start is before now — used to skip auto-inviting
    past meetings (e.g. ones a historic sweep surfaces). Unparseable → False
    (don't block). Compares naive-to-naive so a tz offset doesn't raise."""
    import datetime as _dt
    s = (start_dt or "").strip()
    dt = None
    try:
        dt = _dt.datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return False
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt < _dt.datetime.now()


def _maybe_schedule_meeting(
    cfg: Any, conn: sqlite3.Connection, meeting_id: int, title: str,
    start_dt: Optional[str], end_dt: Optional[str], location: Optional[str],
) -> None:
    """Write a detected meeting into the Swarm calendar as a PLAIN, editable
    event (NOT an invitation).

    Plain events carry no ORGANIZER/ATTENDEE, so Tim can EDIT the time/title or
    DELETE the event directly in Apple Calendar — the two-way-sync reconcile
    (calendar_sync_tick) reads those changes back (edit => update start_dt/title,
    delete => decline). Invitations couldn't do this: Tim was only an attendee.

    No-op (leaves status='new') when the meeting has no ``start_dt`` (nothing to
    schedule) or iCloud isn't configured; already-past meetings are skipped. On
    success stamps the returned UID and flips status to 'scheduled'. Any CalDAV
    hiccup is swallowed + logged so a bad calendar call never breaks extraction.
    """
    if not start_dt:
        return
    if _meeting_is_past(start_dt):
        # Don't put meetings that already happened on the calendar (e.g.
        # surfaced by a historic sweep). Leave status='new'.
        return
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None:
        return
    try:
        if not icloud_calendar.is_configured(config_dir):
            return
        uid = icloud_calendar.add_meeting(
            config_dir, title or "(meeting)", start_dt, end_dt, location=location)
        tracking_store.set_meeting_ics_uid(conn, meeting_id, uid)
        tracking_store.set_meeting_status(conn, meeting_id, "scheduled")
    except Exception:  # noqa: BLE001
        log.exception("auto-schedule failed for meeting %s", meeting_id)


# ---------------------------------------------------------------------------
# Suggested-reply generation (reply-pipeline v2) — its own LLM call.
# ---------------------------------------------------------------------------


# RECENT CONTEXT window: messages (both directions) from the last 24h, capped.
_RECENT_WINDOW_SECS = 24 * 3600
_RECENT_CAP = 60
# BROADER CONTEXT: how many of the most recent daily summaries to pull.
_SUMMARY_CAP = 30


def _last_outgoing_msg_id(messages_conn: sqlite3.Connection, chat_id: int) -> int:
    """Return Tim's LAST OUTGOING ``msg_id`` in this chat, or 0 if none.

    ``out`` lives inside ``raw_json`` (not a column), so scan newest-first and
    return the first message whose ``out`` flag is true. Stops at the first hit,
    so it's cheap whenever Tim has replied recently.
    """
    cur = messages_conn.execute(
        "SELECT msg_id, raw_json FROM messages WHERE chat_id = ? "
        "ORDER BY msg_id DESC",
        (int(chat_id),),
    )
    for row in cur:
        raw = row["raw_json"] if isinstance(row, sqlite3.Row) else row[1]
        try:
            if bool(json.loads(raw or "{}").get("out", False)):
                return int(row[0])
        except Exception:  # noqa: BLE001
            continue
    return 0


def _recent_context(
    messages_conn: sqlite3.Connection, chat_id: int, now: int
) -> list[dict]:
    """Last-24h messages (both directions), chronological, capped at _RECENT_CAP.

    Each entry is ``{sender_name, text, ts, out}`` — the exact background the
    draft saw; persisted as ``context_json`` so the frontend shows it verbatim.
    """
    cutoff = int(now) - _RECENT_WINDOW_SECS
    rows = messages_conn.execute(
        "SELECT msg_id, ts, sender_name, text, raw_json FROM messages "
        "WHERE chat_id = ? AND ts >= ? AND text IS NOT NULL AND text != '' "
        "ORDER BY msg_id DESC LIMIT ?",
        (int(chat_id), cutoff, _RECENT_CAP),
    ).fetchall()
    ctx = []
    for r in reversed(rows):  # chronological
        ctx.append({
            "sender_name": r["sender_name"],
            "text": r["text"],
            "ts": int(r["ts"] or 0),
            "out": not _is_incoming(r["raw_json"]),
        })
    return ctx


def _daily_summaries(
    tracking_conn: sqlite3.Connection, slug: str, chat_id: int
) -> str:
    """Last ~30 daily summaries for this chat as BROADER CONTEXT; '' if none.

    Reads defensively: the ``chat_daily_summaries`` table (slug, chat_id, day,
    summary) is filled by a LATER increment, so a missing table/rows yields ''
    rather than an error.
    """
    try:
        rows = tracking_conn.execute(
            "SELECT day, summary FROM chat_daily_summaries "
            "WHERE slug = ? AND chat_id = ? ORDER BY day DESC LIMIT ?",
            (slug, int(chat_id), _SUMMARY_CAP),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""  # table doesn't exist yet
    lines = []
    for r in reversed(rows):  # oldest→newest
        summ = (r["summary"] or "").strip()
        if summ:
            lines.append(f"[{r['day']}] {summ}")
    return "\n".join(lines)


def _build_reply_payload(
    chat_title: Optional[str],
    unanswered: list[dict],
    recent: list[dict],
    summaries: str,
) -> str:
    """Assemble the three clearly-labeled context layers for the reply prompt."""
    def _fmt(m: dict) -> str:
        who = "Tim (you)" if m.get("out") else (m.get("sender_name") or "Unknown")
        return f"[{_fmt_ts(m.get('ts'))}] {who}: {(m.get('text') or '').strip()}"

    parts = [f"Chat: {chat_title or '(unknown)'}", ""]
    parts.append("=== UNANSWERED MESSAGES (these await Tim's reply) ===")
    parts.extend(_fmt(m) for m in unanswered)
    parts.append("")
    parts.append("=== RECENT CONVERSATION (last 24h, for context) ===")
    if recent:
        parts.extend(_fmt(m) for m in recent)
    else:
        parts.append("(none)")
    parts.append("")
    parts.append("=== BROADER CONTEXT (recent daily summaries) ===")
    parts.append(summaries if summaries else "(none)")
    return "\n".join(parts)


def generate_reply(
    cfg: Any,
    proj: Any,
    chat_id: int,
    chat_title: Optional[str],
    messages_conn: sqlite3.Connection,
    tracking_conn: sqlite3.Connection,
) -> dict:
    """Generate (or decline) a suggested reply for one chat — its own LLM call.

    Returns ``{"needed": bool, "text": str, "reply_to_msg_id": int, "ts": int,
    "context": list}`` where ``context`` is the recent-conversation layer
    (``{sender_name, text, ts, out}`` dicts) the draft was generated against.

    COST GUARD: the "TO REPLY TO" set is the INCOMING messages with
    ``msg_id`` > Tim's last OUTGOING message. If Tim already replied (none
    unanswered), this returns needed=false WITHOUT calling the LLM at all.
    """
    slug = getattr(proj, "slug", "") or ""
    none = {"needed": False, "text": "", "reply_to_msg_id": 0, "ts": 0,
            "context": []}

    last_out = _last_outgoing_msg_id(messages_conn, chat_id)
    rows = messages_conn.execute(
        "SELECT msg_id, ts, sender_name, text, raw_json FROM messages "
        "WHERE chat_id = ? AND msg_id > ? ORDER BY msg_id ASC",
        (int(chat_id), int(last_out)),
    ).fetchall()
    unanswered = [
        {
            "msg_id": int(r["msg_id"]),
            "ts": int(r["ts"] or 0),
            "sender_name": r["sender_name"],
            "text": r["text"],
            "out": False,
        }
        for r in rows
        if _is_incoming(r["raw_json"]) and (r["text"] or "").strip()
    ]
    if not unanswered:
        return none  # Tim's last msg is the latest → skip the reply LLM call.

    reply_to = unanswered[-1]
    now = int(time.time())
    recent = _recent_context(messages_conn, chat_id, now)
    summaries = _daily_summaries(tracking_conn, slug, chat_id)
    payload = _build_reply_payload(chat_title, unanswered, recent, summaries)

    data = _reply_llm(_REPLY_SYSTEM_PROMPT, payload)
    needed = bool(data.get("needed"))
    text = _as_text(data.get("text")) if needed else None
    return {
        "needed": needed and bool(text),
        "text": text or "",
        "reply_to_msg_id": int(reply_to["msg_id"]),
        "ts": int(reply_to["ts"]),
        "context": recent,
    }


# ---------------------------------------------------------------------------
# @claude auto-reply — generate an AI reply (sent AS Tim) for a chat.
#
# A SEPARATE, plain-text LLM call from the extraction/reply pipelines. Used by
# the bridge's @claude trigger (one-shot) and auto-help window (follow-ups).
# INDEPENDENT of tracking scope. The reply ALWAYS carries a clear AI disclaimer
# (the model is told to write one in the conversation's language) plus a
# guaranteed leading "🤖 " marker so recipients know it's an AI on Tim's behalf.
# ---------------------------------------------------------------------------


# Cap on how many recent messages (both directions) of the chat we feed as
# context to the auto-reply. ~24h window, newest-capped.
_AUTOREPLY_CONTEXT_CAP = 40

# Guaranteed leading marker on every auto-reply (robot emoji + space).
AI_MARKER = "\U0001F916 "  # "🤖 "

_AUTOREPLY_SYSTEM_PROMPT = (
    "You are Claude, an AI assistant participating in a Telegram chat on Tim's "
    "behalf. Someone invoked you with @claude. Answer helpfully and concisely in "
    "the SAME LANGUAGE as the conversation. BEGIN your reply with a one-line "
    "disclaimer (in that language) that this is an automated AI (Claude) reply on "
    "Tim's behalf. If no useful reply is warranted (e.g. nothing was actually "
    "asked), output exactly NABO."
)


def _autoreply_llm(system_prompt: str, user_payload: str) -> str:
    """Run one headless ``claude -p`` turn for an @claude auto-reply; PLAIN TEXT.

    Mirrors ``_summary_llm`` (same binary, tools disallowed, proxy stripped, hard
    timeout) and returns the trimmed stdout string, or ``""`` on timeout /
    non-zero exit / any failure. A DISTINCT symbol so it is independently
    monkeypatchable in tests (tests NEVER spawn a real ``claude``).
    """
    cmd = [
        _CLAUDE_BIN, "-p", "--output-format", "text",
        "--system-prompt", system_prompt,
        "--disallowed-tools", *_DISALLOWED_TOOLS,
        "--dangerously-skip-permissions",
    ]
    try:
        r = subprocess.run(
            cmd, env=_claude_env(), capture_output=True, text=True,
            timeout=_EXTRACT_TIMEOUT, input=user_payload,
        )
    except subprocess.TimeoutExpired:
        log.warning("tg_pipelines: auto-reply LLM timed out")
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("tg_pipelines: auto-reply LLM error: %s", e)
        return ""
    if r.returncode != 0:
        log.warning("tg_pipelines: auto-reply LLM rc=%s stderr=%s",
                    r.returncode, (r.stderr or "")[:200])
        return ""
    return (r.stdout or "").strip()


def _ensure_ai_marker(text: str) -> str:
    """Guarantee ``text`` starts with the "🤖 " marker (idempotent).

    If it already opens with the robot emoji, normalize the spacing; otherwise
    prepend the marker. This is the LAST-LINE guarantee that every auto-reply is
    visibly machine-authored, regardless of what the model emitted.
    """
    robot = "\U0001F916"
    t = (text or "").strip()
    if t.startswith(robot):
        return f"{robot} {t[len(robot):].lstrip()}"
    return f"{robot} {t}"


def _build_autoreply_payload(
    chat_title: Optional[str], recent: list[dict], trigger_text: str
) -> str:
    """Assemble the recent-conversation context + the invoking message."""
    def _fmt(m: dict) -> str:
        who = "Tim (you)" if m.get("out") else (m.get("sender_name") or "Unknown")
        return f"[{_fmt_ts(m.get('ts'))}] {who}: {(m.get('text') or '').strip()}"

    parts = [f"Chat: {chat_title or '(unknown)'}", ""]
    parts.append("=== RECENT CONVERSATION (last 24h, for context) ===")
    if recent:
        parts.extend(_fmt(m) for m in recent)
    else:
        parts.append("(none)")
    parts.append("")
    parts.append("=== MESSAGE THAT INVOKED YOU (reply to this) ===")
    parts.append((trigger_text or "").strip())
    return "\n".join(parts)


def auto_reply(
    cfg: Any,
    chat_id: int,
    chat_title: Optional[str],
    trigger_text: str,
    messages_conn: Optional[sqlite3.Connection],
) -> Optional[str]:
    """Generate an AI auto-reply (as Tim) for ``chat_id``, or None to stay silent.

    Builds recent-conversation context (last ~24h, both directions, capped) from
    ``messages_conn`` plus the invoking ``trigger_text``, runs ``_autoreply_llm``
    and returns the reply text with a GUARANTEED leading "🤖 " marker. Returns
    None when the model declines (outputs ``NABO``) or yields nothing — the
    caller then enqueues nothing.

    INDEPENDENT of tracking scope: no project/slug, no watermark — works for any
    chat the bridge sees.
    """
    now = int(time.time())
    recent: list[dict] = []
    if messages_conn is not None:
        try:
            recent = _recent_context(messages_conn, int(chat_id), now)
        except Exception:  # noqa: BLE001 — context is best-effort
            log.exception("auto_reply: recent-context query failed for chat %s", chat_id)
            recent = []
    if len(recent) > _AUTOREPLY_CONTEXT_CAP:
        recent = recent[-_AUTOREPLY_CONTEXT_CAP:]

    payload = _build_autoreply_payload(chat_title, recent, trigger_text)
    out = _autoreply_llm(_AUTOREPLY_SYSTEM_PROMPT, payload).strip()
    if not out or out.upper() == "NABO":
        return None
    return _ensure_ai_marker(out)


# ---------------------------------------------------------------------------
# /swarm-answer draft — a reply Tim sends himself, in his own voice.
#
# Like ``auto_reply`` BUT: NO "🤖"/AI disclaimer and NO marker (Tim reviews +
# sends it manually, so it must read as his own message), and a different system
# prompt. The bridge's /swarm-answer command runs this OFF the loop and drops the
# result into Tim's input box (Telethon SaveDraft) for review/edit.
# ---------------------------------------------------------------------------

_DRAFT_REPLY_SYSTEM_PROMPT = (
    "Draft a reply Tim could send in this chat. Output ONLY the message text, "
    "or NABO if nothing to draft.\n"
    + _TIM_VOICE
)


def _build_draft_payload(chat_title: Optional[str], recent: list[dict]) -> str:
    """Assemble the recent-conversation context for the draft prompt."""
    def _fmt(m: dict) -> str:
        who = "Tim (you)" if m.get("out") else (m.get("sender_name") or "Unknown")
        return f"[{_fmt_ts(m.get('ts'))}] {who}: {(m.get('text') or '').strip()}"

    parts = [f"Chat: {chat_title or '(unknown)'}", ""]
    parts.append("=== RECENT CONVERSATION (last 24h, for context) ===")
    if recent:
        parts.extend(_fmt(m) for m in recent)
    else:
        parts.append("(none)")
    parts.append("")
    parts.append("=== TASK ===")
    parts.append("Draft Tim's next reply in this conversation.")
    return "\n".join(parts)


def draft_reply(
    cfg: Any,
    chat_id: int,
    chat_title: Optional[str],
    messages_conn: Optional[sqlite3.Connection],
) -> Optional[str]:
    """Draft a context-aware reply (in Tim's voice) for ``chat_id``, or None.

    Builds the same recent-conversation context as ``auto_reply`` (last ~24h,
    both directions, capped) from ``messages_conn`` and runs the plain-text LLM,
    BUT returns the text WITHOUT any AI marker/disclaimer — Tim sends it himself.
    Returns None when the model declines (``NABO``) or yields nothing. INDEPENDENT
    of tracking scope; safe with a missing/None messages.db (context best-effort).
    """
    now = int(time.time())
    recent: list[dict] = []
    if messages_conn is not None:
        try:
            recent = _recent_context(messages_conn, int(chat_id), now)
        except Exception:  # noqa: BLE001 — context is best-effort
            log.exception("draft_reply: recent-context query failed for chat %s", chat_id)
            recent = []
    if len(recent) > _AUTOREPLY_CONTEXT_CAP:
        recent = recent[-_AUTOREPLY_CONTEXT_CAP:]

    payload = _build_draft_payload(chat_title, recent)
    out = _autoreply_llm(_DRAFT_REPLY_SYSTEM_PROMPT, payload).strip()
    if not out or out.upper() == "NABO":
        return None
    return out


# ---------------------------------------------------------------------------
# Periodic tick.
# ---------------------------------------------------------------------------


def _is_incoming(raw_json: Optional[str]) -> bool:
    """True if a message is INCOMING (not sent by self).

    Telethon's ``out`` flag is snapshotted into ``raw_json``; ``out=True`` means
    the user sent it. Anything unparseable defaults to incoming (fail-open: a
    chat someone wrote to should still get a reply suggestion).
    """
    if raw_json:
        try:
            return not bool(json.loads(raw_json).get("out", False))
        except Exception:  # noqa: BLE001
            pass
    return True


def _process_chat(
    cfg: Any,
    proj: Any,
    mconn: sqlite3.Connection,
    tconn: sqlite3.Connection,
    chat_id: int,
    chat_title: Optional[str],
    summary: dict,
    username: Optional[str] = None,
) -> None:
    """Extract one tracked chat's new incoming messages. Advances watermark."""
    slug = getattr(proj, "slug", "") or ""
    if not is_tracked(proj, chat_id, chat_title, username):
        summary["chats_skipped_untracked"] += 1
        return
    summary["chats_scanned"] += 1

    cur_max_row = mconn.execute(
        "SELECT MAX(msg_id) FROM messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    cur_max = cur_max_row[0] if cur_max_row else None
    if cur_max is None:
        return  # empty chat

    wm_row = tconn.execute(
        "SELECT last_msg_id FROM pipeline_watermark "
        "WHERE slug = ? AND chat_id = ? AND pipeline = ?",
        (slug, int(chat_id), PIPELINE),
    ).fetchone()

    if wm_row is None:
        # COST GUARD — NO backfill: first sight of this chat just plants the
        # watermark at the current max and processes nothing. Only messages
        # arriving AFTER this pass are ever extracted.
        tracking_store.set_watermark(
            tconn, slug, chat_id, PIPELINE, int(cur_max), 0
        )
        summary["watermarks_initialized"] += 1
        return

    wm = int(wm_row[0])
    # Newest-unprocessed window, capped (COST GUARD). DESC + LIMIT keeps the
    # newest MAX_BATCH; we feed only the INCOMING ones to the LLM.
    rows = mconn.execute(
        "SELECT msg_id, ts, sender_name, text, raw_json FROM messages "
        "WHERE chat_id = ? AND msg_id > ? ORDER BY msg_id DESC LIMIT ?",
        (int(chat_id), wm, MAX_BATCH),
    ).fetchall()
    if not rows:
        return  # no new messages at all

    # Advance the watermark past EVERYTHING we looked at (incoming or not) so a
    # chat with only outgoing messages doesn't get rescanned forever.
    max_seen = max(int(r["msg_id"]) for r in rows)
    last_ts = max(int(r["ts"] or 0) for r in rows)

    # TODOS/MEETINGS consider BOTH directions: Tim's OWN messages can carry action
    # items too ("напомни мне позвонить в банк" → a todo). REPLIES stay
    # incoming-only — that logic lives in generate_reply (it targets unanswered
    # INCOMING messages and never replies to Tim himself).
    extract_rows = [r for r in rows if (r["text"] or "").strip()]
    if not extract_rows:
        # Nothing with text to extract (e.g. media-only) → advance + done. (Reuse
        # the existing counter name; it now means "no extractable text".)
        summary["chats_skipped_no_incoming"] += 1
        tracking_store.set_watermark(
            tconn, slug, chat_id, PIPELINE, max_seen, last_ts
        )
        return

    extract_rows.sort(key=lambda r: int(r["msg_id"]))  # chronological for the LLM
    msgs = [
        {
            "msg_id": int(r["msg_id"]),
            "ts": int(r["ts"] or 0),
            "sender_name": r["sender_name"],
            "text": r["text"],
        }
        for r in extract_rows
    ]
    res = extract_from_chat(cfg, proj, chat_id, chat_title, msgs)
    summary["chats_processed"] += 1
    summary["todos"] += res.get("todos", 0)
    summary["meetings"] += res.get("meetings", 0)

    # Suggested reply — SEPARATE LLM call with layered context (reply v2). Runs
    # only because we already know there are new incoming messages; the reply
    # call itself is further skipped inside generate_reply when Tim has already
    # answered (no unanswered messages → no LLM call). Regenerate-upsert keeps
    # at most ONE pending draft per chat; needed=false clears a stale one.
    rep = generate_reply(cfg, proj, chat_id, chat_title, mconn, tconn)
    if rep.get("needed"):
        tracking_store.upsert_pending_reply(
            tconn, slug, chat_id, chat_title,
            rep["reply_to_msg_id"], rep["ts"], rep["text"],
            context_json=json.dumps(rep.get("context") or []),
        )
        summary["replies"] += 1
        # AUTO-DRAFT: for chats Tim toggled on, queue the suggested reply to be
        # set as the chat's DRAFT by the bridge (the only process with the live
        # Telethon session). Mirrors the outgoing queue — nothing is sent.
        if tg_tracking.is_auto_draft_enabled(
            cfg, chat_id, chat_title, username, conn=tconn
        ):
            tracking_store.upsert_draft(tconn, chat_id, rep["text"])
    else:
        pending = tracking_store.get_pending_reply(tconn, slug, chat_id)
        if pending is not None:
            tracking_store.set_reply_status(tconn, int(pending["id"]), "skipped")
            # The suggestion is stale (Tim already replied) — CLEAR any auto-draft
            # so a no-longer-relevant reply doesn't linger in his input box. Empty
            # text ⇒ the bridge setter blanks the draft (clobber-guarded).
            if tg_tracking.is_auto_draft_enabled(
                cfg, chat_id, chat_title, username, conn=tconn
            ):
                tracking_store.upsert_draft(tconn, chat_id, "")

    tracking_store.set_watermark(
        tconn, slug, chat_id, PIPELINE, max_seen, last_ts
    )


def calendar_sync_tick(cfg: Any) -> dict:
    """Poll the Swarm calendar and sync accept/decline back to meeting rows.

    For every meeting with status='invited' and a non-empty ``ics_uid``, read
    Tim's ATTENDEE ``PARTSTAT`` from the iCloud Swarm calendar:
      - ACCEPTED  → status 'accepted'
      - DECLINED  → status 'declined' AND delete the event from the calendar
      - NEEDS-ACTION / None (still pending or not found) → unchanged

    Then FULL-reconcile plain events from ONE shared calendar listing
    (``read_events_map``, O(events) per tick): for every meeting with
    status='scheduled' and an ``ics_uid``,
      - event GONE from the calendar → Tim deleted it = decline → status 'declined'
      - DTSTART or SUMMARY differ from tracking.db → Tim edited it = correction
        → update the row's start_dt/title (calendar is the source of truth)

    Pure no-op when iCloud isn't configured (or ``cfg`` has no ``config_dir``).
    Every CalDAV call is guarded so one hiccup never crashes the tick. Only a
    CLEAN 'not found' is treated as a deletion: a failed listing skips the
    whole reconcile, and a uid absent from a listing that had per-event data
    errors is inconclusive and skipped (never auto-declined). Returns an
    aggregate summary for the scheduler/tests.
    """
    summary = {"checked": 0, "accepted": 0, "declined": 0,
               "reconciled": 0, "gone": 0, "updated": 0}
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None or not icloud_calendar.is_configured(config_dir):
        return summary
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        rows = conn.execute(
            "SELECT id, ics_uid FROM meetings "
            "WHERE status = 'invited' AND ics_uid IS NOT NULL AND ics_uid != ''"
        ).fetchall()
        for row in rows:
            mid, uid = int(row["id"]), row["ics_uid"]
            summary["checked"] += 1
            try:
                partstat = icloud_calendar.read_partstat(config_dir, uid)
            except Exception:  # noqa: BLE001
                log.exception("calendar_sync: read_partstat failed for %s", uid)
                continue
            if partstat == "ACCEPTED":
                tracking_store.set_meeting_status(conn, mid, "accepted")
                summary["accepted"] += 1
            elif partstat == "DECLINED":
                tracking_store.set_meeting_status(conn, mid, "declined")
                summary["declined"] += 1
                try:
                    icloud_calendar.delete_meeting(config_dir, uid)
                except Exception:  # noqa: BLE001
                    log.exception("calendar_sync: delete failed for %s", uid)
            # NEEDS-ACTION / None → leave as 'invited' (still pending in Apple Cal)

        # --- plain-event reconcile: Tim's edits/deletes on 'scheduled' rows ---
        rows = conn.execute(
            "SELECT id, title, start_dt, ics_uid FROM meetings "
            "WHERE status = 'scheduled' AND ics_uid IS NOT NULL AND ics_uid != ''"
        ).fetchall()
        listing = None
        if rows:
            # ONE calendar listing shared by every row (O(events), not
            # O(rows x events)); if it fails the whole reconcile is skipped
            # this tick — no per-row guesswork on a dead connection.
            try:
                listing = icloud_calendar.read_events_map(config_dir)
            except Exception:  # noqa: BLE001
                log.exception("calendar_sync: read_events_map failed")
        for row in (rows if listing is not None else []):
            mid, uid = int(row["id"]), row["ics_uid"]
            summary["reconciled"] += 1
            ev = listing["events"].get(uid)
            if ev is None:
                if listing["data_errors"]:
                    # Some events' data never came back — the missing uid may be
                    # one of them. Inconclusive: never auto-decline on a dirty
                    # listing; the next tick retries.
                    log.warning(
                        "calendar_sync: %s absent from listing with %d data "
                        "error(s) — skipped", uid, listing["data_errors"])
                    continue
                # Clean 'not found' (listing succeeded): Tim deleted = decline.
                tracking_store.set_meeting_status(conn, mid, "declined")
                summary["gone"] += 1
                log.info("calendar_sync: meeting %s gone from calendar -> declined",
                         mid)
                continue
            new_title, new_start = ev.get("summary"), ev.get("start")
            fields: dict = {}
            if new_title and new_title != (row["title"] or ""):
                fields["title"] = new_title
            if new_start and not _same_start(row["start_dt"], new_start):
                fields["start_dt"] = new_start
            if fields:
                tracking_store.update_meeting_fields(conn, mid, **fields)
                summary["updated"] += 1
                log.info("calendar_sync: meeting %s edited in calendar -> %s",
                         mid, sorted(fields))
    finally:
        conn.close()
    return summary


def _same_start(a: Optional[str], b: Optional[str]) -> bool:
    """True when two start timestamps mean the same wall time (format-tolerant:
    '2026-06-29T09:00' == '2026-06-29T09:00:00'). Falls back to string equality
    when either side doesn't parse."""
    if not a or not b:
        return (a or "") == (b or "")
    try:
        return icloud_calendar._parse(a) == icloud_calendar._parse(b)
    except Exception:  # noqa: BLE001
        return a == b


def migrate_invited_to_plain(cfg: Any) -> dict:
    """ONE-SHOT migration (calendar sync part 3/3): convert legacy INVITATION
    events into plain editable ones.

    Invites made Tim an attendee of a swarm-organized event, so he couldn't
    edit them; parts 1/2 switched creation to plain events plus a full
    reconcile of 'scheduled' rows. This migrates the pre-switch stock: for
    every meeting with status='invited' and an ``ics_uid``,
      - a decision already sitting on the old invite WINS: PARTSTAT ACCEPTED /
        DECLINED is applied exactly like ``calendar_sync_tick`` (declined also
        deletes the event) and the row is not migrated;
      - otherwise the invitation VEVENT is deleted and re-created as a plain
        ``add_meeting`` event, the new uid is restamped and status flips to
        'scheduled' — from then on the part-2 reconcile watches it. An invite
        already gone from the calendar is recreated too (deletion was never a
        decline signal under invite semantics; Tim can delete the plain event
        to decline).

    Idempotent / retry-safe: migrated rows leave the 'invited' set so a re-run
    is a no-op, and on a mid-flight failure (delete ok, add failed) the row
    stays 'invited' with the old uid — the next run retries the add while the
    delete degrades to a no-op False. Any CalDAV error skips the row (carried
    to the next run, never dropped). No-op when iCloud isn't configured.
    Returns a summary: candidates / migrated / resolved (partstat applied) /
    skipped (error or no start_dt).
    """
    summary = {"candidates": 0, "migrated": 0, "resolved": 0, "skipped": 0}
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None or not icloud_calendar.is_configured(config_dir):
        return summary
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, start_dt, end_dt, location, ics_uid FROM meetings "
            "WHERE status = 'invited' AND ics_uid IS NOT NULL AND ics_uid != ''"
        ).fetchall()
        for row in rows:
            mid, uid = int(row["id"]), row["ics_uid"]
            summary["candidates"] += 1
            try:
                partstat = icloud_calendar.read_partstat(config_dir, uid)
            except Exception:  # noqa: BLE001
                log.exception("migrate_invited: read_partstat failed for %s", uid)
                summary["skipped"] += 1
                continue
            if partstat == "ACCEPTED":
                tracking_store.set_meeting_status(conn, mid, "accepted")
                summary["resolved"] += 1
                continue
            if partstat == "DECLINED":
                tracking_store.set_meeting_status(conn, mid, "declined")
                try:
                    icloud_calendar.delete_meeting(config_dir, uid)
                except Exception:  # noqa: BLE001
                    log.exception("migrate_invited: delete failed for %s", uid)
                summary["resolved"] += 1
                continue
            if not row["start_dt"]:
                # Nothing to recreate the plain event from — leave the row for
                # a human look rather than guessing a time.
                log.warning("migrate_invited: meeting %s has no start_dt — skipped",
                            mid)
                summary["skipped"] += 1
                continue
            # Delete FIRST so a failure can never leave two calendar events; a
            # False return (already gone, e.g. a retry after a failed add)
            # still proceeds to the add.
            try:
                icloud_calendar.delete_meeting(config_dir, uid)
            except Exception:  # noqa: BLE001
                log.exception("migrate_invited: delete failed for %s — row kept",
                              uid)
                summary["skipped"] += 1
                continue
            try:
                new_uid = icloud_calendar.add_meeting(
                    config_dir, row["title"] or "(meeting)", row["start_dt"],
                    row["end_dt"], location=row["location"])
            except Exception:  # noqa: BLE001
                # Invite already deleted; the row keeps 'invited' + the old uid
                # so the NEXT run retries the add (its delete no-ops).
                log.exception("migrate_invited: add failed for meeting %s — "
                              "will retry on next run", mid)
                summary["skipped"] += 1
                continue
            tracking_store.set_meeting_ics_uid(conn, mid, new_uid)
            tracking_store.set_meeting_status(conn, mid, "scheduled")
            summary["migrated"] += 1
            log.info("migrate_invited: meeting %s invite %s -> plain %s",
                     mid, uid, new_uid)
    finally:
        conn.close()
    return summary


def calendar_backfill_tick(cfg: Any) -> dict:
    """Push any confirmed meeting that never reached the iCloud calendar.

    Meetings can land in ``tracking.db`` without a CalDAV push: ``_maybe_schedule_
    meeting`` only runs inside the extraction path, so a meeting scheduled by the
    assistant (or inserted any other way) stays off the calendar even though it's
    real. This tick reconciles that gap — for every meeting with a ``start_dt``
    but an empty ``ics_uid`` that isn't a duplicate/declined/cancelled row, it
    writes the Swarm-calendar PLAIN event and stamps ``ics_uid`` (flipping status
    to 'scheduled'). Already-past meetings are skipped by ``_maybe_schedule_meeting``.

    No-op when iCloud isn't configured (or ``cfg`` has no ``config_dir``). Every
    CalDAV call is guarded, so one hiccup never crashes the tick. Returns a
    summary for the scheduler/tests.
    """
    summary = {"candidates": 0, "pushed": 0}
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None or not icloud_calendar.is_configured(config_dir):
        return summary
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, start_dt, end_dt, location FROM meetings "
            "WHERE (ics_uid IS NULL OR ics_uid = '') "
            "AND start_dt IS NOT NULL AND start_dt != '' "
            "AND status NOT IN ('duplicate', 'declined', 'cancelled')"
        ).fetchall()
        for row in rows:
            summary["candidates"] += 1
            mid = int(row["id"])
            _maybe_schedule_meeting(
                cfg, conn, mid, row["title"], row["start_dt"],
                row["end_dt"], row["location"])
            # Confirm it actually landed — past meetings are silently skipped by
            # the guard, so only count rows that now carry an ics_uid.
            got = conn.execute(
                "SELECT ics_uid FROM meetings WHERE id = ?", (mid,)
            ).fetchone()
            if got and got["ics_uid"]:
                summary["pushed"] += 1
    finally:
        conn.close()
    return summary


def detect_reply_watches(cfg: Any) -> list[dict]:
    """Fire any reply watch whose awaited incoming reply has now landed.

    For each open ``reply_watches`` row, look in messages.db for the FIRST
    incoming message (``raw_json.out`` false/absent) in that chat with
    ``msg_id > after_msg_id``. When found, flip the watch to 'fired', stamp the
    reply, and include it in the returned list so the caller (coord-life on its
    heartbeat, or a notify tick) can surface it to Tim — closing the gap where a
    third-party reply was missed until Tim happened to ping.

    Read-only against messages.db; guarded so a missing/locked messages db (or a
    single bad row) never crashes the sweep. Returns a list of fired-watch dicts
    ``{watch_id, slug, chat_id, chat_title, context, reply_msg_id, reply_text}``.
    """
    fired: list[dict] = []
    tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        watches = tracking_store.list_open_reply_watches(tconn)
        if not watches:
            return fired
        mpath = getattr(cfg, "tg_user_db_path", None)
        if not mpath or not Path(mpath).exists():
            return fired
        mconn = sqlite3.connect(f"file:{mpath}?mode=ro", uri=True)
        mconn.row_factory = sqlite3.Row
        try:
            for w in watches:
                try:
                    hit = mconn.execute(
                        "SELECT msg_id, ts, sender_name, text FROM messages "
                        "WHERE chat_id = ? AND msg_id > ? "
                        "AND COALESCE(json_extract(raw_json, '$.out'), 0) = 0 "
                        "ORDER BY msg_id ASC LIMIT 1",
                        (int(w["chat_id"]), int(w["after_msg_id"])),
                    ).fetchone()
                except Exception:  # noqa: BLE001 — one bad row never stalls the sweep
                    log.exception("detect_reply_watches: query failed for watch %s",
                                  w["id"])
                    continue
                if hit is None:
                    continue
                tracking_store.mark_reply_watch_fired(
                    tconn, int(w["id"]), int(hit["msg_id"]), hit["text"])
                fired.append({
                    "watch_id": int(w["id"]),
                    "slug": w["slug"],
                    "chat_id": int(w["chat_id"]),
                    "chat_title": w["chat_title"],
                    "context": w["context"],
                    "reply_msg_id": int(hit["msg_id"]),
                    "reply_text": hit["text"],
                })
        finally:
            mconn.close()
    finally:
        tconn.close()
    return fired


def _lifetrack_bot(cfg: Any) -> tuple[str, str]:
    """([lifetrack] bot token, Tim's DM chat id) for worker-side life-bot sends.

    Token comes from ``secrets.toml [lifetrack].bot_token``; the chat prefers
    ``[lifetrack].dm_chat`` and falls back to the org owner channel (Tim).
    Returns ``("", chat)`` when the life bot isn't configured.
    """
    import tomllib
    token, chat = "", str(getattr(cfg, "org_owner_channel", "") or "")
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None:
        return token, chat
    try:
        sec = tomllib.loads((Path(config_dir) / "secrets.toml").read_text())
        lt = sec.get("lifetrack", {}) or {}
        token = lt.get("bot_token", "") or ""
        chat = str(lt.get("dm_chat", "") or "") or chat
    except Exception:  # noqa: BLE001
        log.exception("_lifetrack_bot: secrets read failed")
    return token, chat


def _tg_bot_send(cfg: Any, token: str, chat_id: str, text: str) -> None:
    """Minimal Bot-API sendMessage for an arbitrary bot token (plain text).

    Uses the xray proxy (tg_egress_proxy from config) with 3 retry attempts on
    transport errors, since the proxy is flaky. An HTTP/API error from Telegram
    itself is a real answer and is not retried.
    """
    import httpx  # lazy import — matches tg.TgClient._post

    proxy = getattr(cfg, "tg_egress_proxy", "") or ""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    last: Exception | None = None
    attempts = 3
    backoff_sec = 2.0

    for attempt in range(attempts):
        if attempt:
            time.sleep(backoff_sec * attempt)
        try:
            resp = httpx.post(
                url,
                json={"chat_id": chat_id, "text": text[:4000]},
                timeout=10,
                proxy=proxy or None,
            )
        except httpx.TransportError as e:
            last = e
            log.warning("_tg_bot_send: transport error on attempt %d/%d via proxy %r: %s",
                       attempt + 1, attempts, proxy or "(none)", e)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        log.debug("_tg_bot_send: sent to chat %s (text len=%d)", chat_id, len(text))
        return
    raise RuntimeError(
        f"_tg_bot_send to chat {chat_id} was NOT delivered after {attempts} attempts "
        f"(last error: {last})")


def reply_watch_notify_tick(cfg: Any) -> dict:
    """Detect fired reply watches and DM Tim on the LIFE bot immediately.

    The low-latency half of reply watching: a ~45s scheduler tick runs
    ``detect_reply_watches`` and pushes each fired watch straight to Tim as a
    life-bot message (watch context + reply text) instead of waiting for
    coord-life's next turn. ``detect`` flips a watch to 'fired' atomically, so
    this tick and coord-life's turn-start sweep never double-notify — whoever
    detects first owns the delivery. A SUCCESSFUL send stamps ``notified_at``
    (delivered); a failed send is logged, NOT retried here, and leaves
    ``notified_at`` NULL — coord-life's safety net re-surfaces exactly the
    fired-and-unstamped set via ``list_fired_reply_watches(undelivered_only)``.

    No-op when nothing fired or the [lifetrack] bot isn't configured. Returns
    ``{"fired": n, "sent": m}``.
    """
    summary = {"fired": 0, "sent": 0}
    fired = detect_reply_watches(cfg)
    if not fired:
        return summary
    summary["fired"] = len(fired)
    token, chat_id = _lifetrack_bot(cfg)
    if not token or not chat_id:
        log.warning(
            "reply_watch_notify_tick: [lifetrack] bot/chat not configured — "
            "%d fired watch(es) left for coord-life to surface", len(fired))
        return summary
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        for w in fired:
            title = w.get("chat_title") or w.get("slug") or str(w.get("chat_id"))
            lines = [f"Reply landed in {title}:"]
            if w.get("context"):
                lines.append(f"(waiting on: {w['context']})")
            lines.append(w.get("reply_text") or "(no text)")
            try:
                _tg_bot_send(cfg, token, chat_id, "\n".join(lines))
                tracking_store.mark_reply_watch_notified(conn, w["watch_id"])
                summary["sent"] += 1
            except Exception:  # noqa: BLE001
                log.exception(
                    "reply_watch_notify_tick: send failed for watch %s "
                    "(stays 'fired', notified_at NULL — safety-net set)",
                    w["watch_id"])
    finally:
        conn.close()
    return summary


# Morning reminder — proactive daily digest of PENDING todos to Tim on the life
# bot at a set AM time. State/config in tracking.db app_settings:
#   morning_reminder_time       'HH:MM' (default 08:30) or 'off' — read in
#                               assistant_tz when set, host clock otherwise
#   morning_reminder_last_sent  'YYYY-MM-DD' once-per-day stamp
MORNING_REMINDER_KEY_TIME = "morning_reminder_time"
MORNING_REMINDER_KEY_SENT = "morning_reminder_last_sent"
MORNING_REMINDER_DEFAULT_TIME = "08:30"
MORNING_REMINDER_WINDOW_H = 4     # deliver late after downtime, but never at night
MORNING_REMINDER_CAP = 15
MORNING_REMINDER_MAX_AGE_DAYS = 7  # pendings older than this are stale, not agenda


# IANA tz name (e.g. 'Europe/Moscow') that all daily HH:MM settings are read
# in. Unset -> host clock (backward compatible: times were UTC-expressed while
# the host runs Etc/UTC). Set it together with re-expressing the HH:MM
# settings — the two must flip as one or reminders shift by the offset.
ASSISTANT_TZ_KEY = "assistant_tz"


def _assistant_now(conn: sqlite3.Connection,
                   now: Optional[datetime]) -> datetime:
    """Tim-local NAIVE wall time for the daily gates.

    ``now`` (tests) is returned as-is — injected values are already 'local'.
    Otherwise: app_settings ``assistant_tz`` (IANA name) when set and valid,
    host clock when unset/bad (with a warning — never a crash at tick time).
    """
    if now is not None:
        return now
    tz_name = (tracking_store.get_setting(conn, ASSISTANT_TZ_KEY) or "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            log.warning("%s: bad tz %r — using host clock",
                        ASSISTANT_TZ_KEY, tz_name)
    return datetime.now()


def _once_daily_gate(conn: sqlite3.Connection, key_time: str, key_sent: str,
                     default_time: str, now: Optional[datetime],
                     window_h: int) -> tuple[str, str, datetime]:
    """Shared once-per-day time gate for proactive daily life-bot sends.

    Returns ``(verdict, today, local)``, verdict one of 'off' | 'early' |
    'done' | 'late' | 'due'; ``local`` is the Tim-local wall time the verdict
    was computed against (``_assistant_now`` — assistant_tz setting or host
    clock) so callers date their content off the SAME clock. Semantics (shared
    by morning reminder + daily digest): fire inside [configured HH:MM,
    +window_h) once per local day; 'late' (worker down past the window) STAMPS
    today here so nothing pings at night; the CALLER stamps on 'due' after its
    send succeeds (or when it has nothing to send), so a failed send retries
    next tick in-window. Bad HH:MM falls back to ``default_time``; 'off'
    disables.
    """
    local = _assistant_now(conn, now)
    hhmm = (tracking_store.get_setting(conn, key_time, default_time)
            or "").strip()
    if hhmm.lower() == "off":
        return "off", "", local
    try:
        target = datetime.strptime(hhmm, "%H:%M").time()
    except ValueError:
        log.warning("%s: bad time %r — using default", key_time, hhmm)
        target = datetime.strptime(default_time, "%H:%M").time()
    today = local.strftime("%Y-%m-%d")
    if tracking_store.get_setting(conn, key_sent) == today:
        return "done", today, local
    start = local.replace(hour=target.hour, minute=target.minute,
                          second=0, microsecond=0)
    if local < start:
        return "early", today, local
    if local >= start + timedelta(hours=window_h):
        tracking_store.set_setting(conn, key_sent, today)
        log.info("%s: past the %sh window — skipping %s",
                 key_time, window_h, today)
        return "late", today, local
    return "due", today, local


def morning_reminder_tick(cfg: Any, now: Optional[datetime] = None) -> dict:
    """Send Tim his pending todos ONCE per day at the configured local AM time.

    Runs on a ~5-min scheduler cadence; fires when local time is inside
    [configured time, +4h) and today isn't stamped yet. Surfaces only CURRENT
    work: pending todos created in the last 7 days, exact-duplicate texts
    folded; older pendings are counted in a tail line, never listed (they are
    usually already handled in real life — Tim, board 5ccf1b20). A SUCCESSFUL
    send (or a day with zero current todos, which sends nothing) stamps today;
    a FAILED send leaves the stamp unset so the next tick retries while still
    inside the window. If the worker was down past the whole window, the day
    is stamped as skipped — reminders never arrive at night. 'off' disables.
    ``now`` is injectable for tests (defaults to local wall time).
    """
    summary = {"due": False, "sent": 0, "todos": 0, "skipped_late": False}
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        verdict, today, _local = _once_daily_gate(
            conn, MORNING_REMINDER_KEY_TIME, MORNING_REMINDER_KEY_SENT,
            MORNING_REMINDER_DEFAULT_TIME, now, MORNING_REMINDER_WINDOW_H)
        summary["skipped_late"] = verdict == "late"
        if verdict != "due":
            return summary
        summary["due"] = True
        # Over-fetch past the display cap so the tail line can say how many
        # pending todos were NOT shown (no silent cap).
        rows = tracking_store.list_todos(conn, status="pending", limit=500)
        # RELEVANCE FILTER (Tim, board 5ccf1b20): chat-extracted todos never
        # get flipped when he handles them in real life, so old 'pending' rows
        # are mostly already done — the agenda must not nag about them. Show
        # only RECENT pendings (created within the last 7 days) and count the
        # stale rest in one tail line. Also fold exact-duplicate texts (the
        # extractor re-detects the same task across days).
        cutoff = time.time() - MORNING_REMINDER_MAX_AGE_DAYS * 86400
        fresh, seen_texts = [], set()
        stale = 0
        for r in rows:
            if int(r["created_at"] or 0) < cutoff:
                stale += 1
                continue
            key = " ".join((r["text"] or "").lower().split())
            if key in seen_texts:
                continue
            seen_texts.add(key)
            fresh.append(r)
        summary["todos"] = len(fresh)
        summary["stale"] = stale
        if not fresh:
            tracking_store.set_setting(conn, MORNING_REMINDER_KEY_SENT, today)
            return summary  # nothing current — quiet day, no message
        token, chat_id = _lifetrack_bot(cfg)
        if not token or not chat_id:
            log.warning("morning_reminder: [lifetrack] bot/chat not configured")
            return summary
        # list_todos returns newest-first; the agenda reads oldest-first
        # (within the fresh window, oldest = closest to falling off).
        shown = list(reversed(fresh))[:MORNING_REMINDER_CAP]
        lines = [f"Morning reminders ({len(fresh)}):"]
        for i, r in enumerate(shown, 1):
            lines.append(f"{i}) {r['text']}")
        if len(fresh) > len(shown):
            lines.append(f"(+{len(fresh) - len(shown)} more recent pending)")
        if stale:
            lines.append(f"({stale} older pending >"
                         f"{MORNING_REMINDER_MAX_AGE_DAYS}d not shown — "
                         "say 'old todos' to review)")
        try:
            _tg_bot_send(cfg, token, chat_id, "\n".join(lines))
            tracking_store.set_setting(conn, MORNING_REMINDER_KEY_SENT, today)
            summary["sent"] = 1
        except Exception:  # noqa: BLE001 — no stamp: next tick retries in-window
            log.exception("morning_reminder: send failed (will retry)")
    finally:
        conn.close()
    return summary


# Daily digest — proactive morning brief of YESTERDAY's chat_daily_summaries
# to the owner on the configured bot. Same app_settings/gate pattern as morning reminder;
# daily_digest_time is read in assistant_tz when set (host clock otherwise),
# default 06:00 — keep it after the morning reminders in the same tz.
DAILY_DIGEST_KEY_TIME = "daily_digest_time"
DAILY_DIGEST_KEY_SENT = "daily_digest_last_sent"
DAILY_DIGEST_DEFAULT_TIME = "06:00"
DAILY_DIGEST_WINDOW_H = 4
# Tim: the brief was TOO MUCH (board 6418ec47) — cut hard: only the busiest
# few chats, shorter blurbs, and drop single-exchange chats entirely (a 1-2
# message day in a chat is noise, not news). Tail line stays honest about
# everything not shown.
DAILY_DIGEST_CHAT_CAP = 5           # hard display cap, busiest first
DAILY_DIGEST_SUMMARY_CHARS = 200
DAILY_DIGEST_MIN_MSGS = 3           # below this a chat isn't 'important'


def _chat_titles(msgs_db_path: Any, chat_ids: list[int]) -> dict[int, str]:
    """Best-effort chat_id -> title map from messages.db (read-only)."""
    titles: dict[int, str] = {}
    if not chat_ids:
        return titles
    try:
        mconn = sqlite3.connect(f"file:{msgs_db_path}?mode=ro", uri=True)
        try:
            mconn.row_factory = sqlite3.Row
            ph = ",".join("?" * len(chat_ids))
            for r in mconn.execute(
                f"SELECT chat_id, title FROM chats WHERE chat_id IN ({ph})",  # noqa: S608
                [int(c) for c in chat_ids],
            ):
                titles[int(r["chat_id"])] = (r["title"] or "").strip()
        finally:
            mconn.close()
    except Exception:  # noqa: BLE001 — digest degrades to bare chat ids
        log.exception("daily_digest: chat-title lookup failed")
    return titles


def daily_digest_tick(cfg: Any, now: Optional[datetime] = None) -> dict:
    """Send Tim ONE morning brief of YESTERDAY's per-chat daily summaries.

    Same delivery contract as ``morning_reminder_tick`` (shared
    ``_once_daily_gate``): [configured time, +4h) window, once per day, failed
    send retries next tick in-window, past-window downtime never pings at
    night, 'off' disables. UNLIKE the reminder, a day with no summary rows yet
    does NOT stamp: yesterday's summaries come from the 6h-interval
    ``summaries_tick`` whose phase resets on every worker restart, so they can
    land after the window opens — the digest keeps retrying each tick and a
    genuinely quiet day is stamped by the gate's 'late' branch at window end.
    Only IMPORTANT chats (Tim, board 6418ec47): busiest first, chats under
    ``DAILY_DIGEST_MIN_MSGS`` messages dropped, hard cap of
    ``DAILY_DIGEST_CHAT_CAP`` with an honest tail line for everything not
    shown; a day where no chat crosses the threshold sends nothing. Titles
    from messages.db (read-only), falling back to bare chat ids.
    """
    summary = {"due": False, "sent": 0, "chats": 0, "skipped_late": False}
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        verdict, today, local = _once_daily_gate(
            conn, DAILY_DIGEST_KEY_TIME, DAILY_DIGEST_KEY_SENT,
            DAILY_DIGEST_DEFAULT_TIME, now, DAILY_DIGEST_WINDOW_H)
        summary["skipped_late"] = verdict == "late"
        if verdict != "due":
            return summary
        summary["due"] = True
        # 'yesterday' off the gate's clock (assistant_tz-aware), not the host's.
        # CAVEAT: chat_daily_summaries rows are keyed by UTC day; the two dates
        # coincide for any digest time from 03:00 MSK on (UTC+3). Setting
        # daily_digest_time inside 00:00-02:59 MSK would date 'yesterday' one
        # day AHEAD of the newest complete UTC day — rows land only after
        # 03:00 MSK, so the digest would retry into that band or late-stamp.
        # Keep the digest time at 03:00 MSK or later (default 09:00).
        day = (local - timedelta(days=1)).strftime("%Y-%m-%d")
        rows = tracking_store.get_summaries_for_day(conn, day)
        summary["chats"] = len(rows)
        if not rows:
            # NO stamp here: summaries_tick is a 6h interval whose phase
            # resets on restart, so yesterday's rows can arrive after the
            # window opens — stamping now would eat that day's brief. Retry
            # next tick; a genuinely quiet day is stamped by the gate's
            # 'late' branch when the window closes (still nothing at night).
            return summary
        # IMPORTANCE FILTER (Tim, board 6418ec47): only chats with a real
        # conversation yesterday. Rows come busiest-first; a day where nothing
        # crossed the threshold is a quiet day — no brief at all.
        important = [r for r in rows
                     if int(r["msg_count"] or 0) >= DAILY_DIGEST_MIN_MSGS]
        if not important:
            tracking_store.set_setting(conn, DAILY_DIGEST_KEY_SENT, today)
            log.info("daily_digest: %d chat(s) all below %d msgs — quiet day",
                     len(rows), DAILY_DIGEST_MIN_MSGS)
            return summary
        token, chat_id = _lifetrack_bot(cfg)
        if not token or not chat_id:
            log.warning("daily_digest: [lifetrack] bot/chat not configured")
            return summary
        shown = important[:DAILY_DIGEST_CHAT_CAP]
        titles = _chat_titles(cfg.tg_user_db_path,
                              [int(r["chat_id"]) for r in shown])
        lines = [f"Daily brief — {day}:"]
        for r in shown:
            title = titles.get(int(r["chat_id"])) or str(r["chat_id"])
            text = (r["summary"] or "").strip()
            if len(text) > DAILY_DIGEST_SUMMARY_CHARS:
                text = text[:DAILY_DIGEST_SUMMARY_CHARS - 1] + "…"
            lines.append(f"• {title}: {text}")
        if len(rows) > len(shown):
            lines.append(f"(+{len(rows) - len(shown)} quieter chats — "
                         "say 'full brief' for everything)")
        try:
            _tg_bot_send(cfg, token, chat_id, "\n".join(lines))
            tracking_store.set_setting(conn, DAILY_DIGEST_KEY_SENT, today)
            summary["sent"] = 1
        except Exception:  # noqa: BLE001 — no stamp: next tick retries in-window
            log.exception("daily_digest: send failed (will retry)")
    finally:
        conn.close()
    return summary


# Morning events — TODAY's calendar as its OWN standalone message (Tim, board
# aad153ce: events must not be bundled into the brief). Same gate pattern;
# default 08:45 assistant_tz — after the 08:30 reminders, before the 09:00
# brief, so the morning reads: todos, events, chat brief.
MORNING_EVENTS_KEY_TIME = "morning_events_time"
MORNING_EVENTS_KEY_SENT = "morning_events_last_sent"
MORNING_EVENTS_DEFAULT_TIME = "08:45"
MORNING_EVENTS_WINDOW_H = 4
MORNING_EVENTS_CAP = 15


def morning_events_tick(cfg: Any, now: Optional[datetime] = None) -> dict:
    """Send Tim TODAY's calendar events ONCE per day, as a standalone message.

    Split out of the daily brief per Tim (board aad153ce): events get their
    own message and formatting, decoupled from the chat-summary digest. Same
    ``_once_daily_gate`` contract as the other daily sends. Events come from
    ``icloud_calendar.read_events`` (ALL his calendars, not just Swarm),
    filtered to the gate's local day; a day with no events sends nothing and
    stamps. A CalDAV read failure does NOT stamp — the next 5-min tick
    retries in-window (transient iCloud hiccups must not eat the day), and a
    genuinely dead window is stamped by the gate's 'late' branch. iCloud
    unconfigured = permanent quiet day (stamp, no send).
    """
    summary = {"due": False, "sent": 0, "events": 0, "skipped_late": False}
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        verdict, today, _local = _once_daily_gate(
            conn, MORNING_EVENTS_KEY_TIME, MORNING_EVENTS_KEY_SENT,
            MORNING_EVENTS_DEFAULT_TIME, now, MORNING_EVENTS_WINDOW_H)
        summary["skipped_late"] = verdict == "late"
        if verdict != "due":
            return summary
        summary["due"] = True
        config_dir = getattr(cfg, "config_dir", None)
        if config_dir is None or not icloud_calendar.is_configured(config_dir):
            tracking_store.set_setting(conn, MORNING_EVENTS_KEY_SENT, today)
            return summary
        try:
            # days_back=1: the search window starts at now-1d, so events that
            # already STARTED earlier this morning still come back — the
            # date filter below trims yesterday's.
            events = icloud_calendar.read_events(
                config_dir, days_back=1, days_forward=1)
        except Exception:  # noqa: BLE001 — no stamp: retry next tick in-window
            log.exception("morning_events: calendar read failed (will retry)")
            return summary
        # Tim-local 'today' via ISO-string prefix: Swarm events are floating
        # local wall time, Apple events carry their (Moscow) TZID — both put
        # the local date first in isoformat.
        todays = [e for e in events if (e.get("start") or "").startswith(today)]
        summary["events"] = len(todays)
        if not todays:
            tracking_store.set_setting(conn, MORNING_EVENTS_KEY_SENT, today)
            return summary  # empty calendar day — no message
        token, chat_id = _lifetrack_bot(cfg)
        if not token or not chat_id:
            log.warning("morning_events: [lifetrack] bot/chat not configured")
            return summary
        lines = [f"Today — {today}:"]
        for e in todays[:MORNING_EVENTS_CAP]:
            title = (e.get("title") or "(no title)").strip()
            if e.get("all_day"):
                when = "all day"
            else:
                when = (e.get("start") or "")[11:16] or "?"
                end_hm = (e.get("end") or "")[11:16]
                if end_hm:
                    when += f"-{end_hm}"
            loc = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"• {when} {title}{loc}")
        if len(todays) > MORNING_EVENTS_CAP:
            lines.append(f"(+{len(todays) - MORNING_EVENTS_CAP} more events)")
        try:
            _tg_bot_send(cfg, token, chat_id, "\n".join(lines))
            tracking_store.set_setting(conn, MORNING_EVENTS_KEY_SENT, today)
            summary["sent"] = 1
        except Exception:  # noqa: BLE001 — no stamp: next tick retries in-window
            log.exception("morning_events: send failed (will retry)")
    finally:
        conn.close()
    return summary


def tg_tracking_tick(cfg: Any) -> dict:
    """Periodic extraction sweep over tracked chats for opted-in projects.

    For each project whose EFFECTIVE config (DB-first; see
    ``tg_tracking.get_effective_tracking``) has tracking enabled: open
    ``tracking.db`` and ``messages.db`` (read-only); for each chat, gate on the
    effective ``is_tracked``, then either plant the watermark (first sight, no
    backfill) or extract the new incoming window and advance the watermark.
    Per-chat work is wrapped so one bad chat can't kill the sweep. Returns an
    aggregate summary.

    Resolving the EFFECTIVE config (not ``proj.tg_track_*`` directly) is what
    makes a UI change apply on the next sweep with no restart. The
    ``tg_track_enabled`` gate (a COST GUARD) is preserved — just sourced
    DB-first: no enabled project → no messages.db open, no LLM calls.
    """
    summary = {
        "projects": 0,
        "chats_scanned": 0,
        "chats_processed": 0,
        "chats_skipped_untracked": 0,
        "chats_skipped_no_incoming": 0,
        "watermarks_initialized": 0,
        "todos": 0,
        "meetings": 0,
        "replies": 0,
    }

    # tracking.db holds the live config rows; open it first so the effective
    # gate below reads DB-first (file config is the default when no row exists).
    tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    mconn = None
    try:
        enabled = [
            ep for ep in (
                tg_tracking.effective_proj(cfg, p, conn=tconn)
                for p in cfg.projects.values()
            )
            if ep.tg_track_enabled
        ]
        if not enabled:
            return summary  # COST GUARD: nothing opted in → cheap no-op.

        msgs_path = cfg.tg_user_db_path
        if not msgs_path.exists():
            log.info("tg_tracking_tick: no messages.db yet at %s", msgs_path)
            return summary

        mconn = sqlite3.connect(f"file:{msgs_path}?mode=ro", uri=True)
        mconn.row_factory = sqlite3.Row
        chats = mconn.execute(
            "SELECT chat_id, title FROM chats"
        ).fetchall()
        for proj in enabled:
            summary["projects"] += 1
            for crow in chats:
                chat_id = crow["chat_id"]
                title = crow["title"]
                try:
                    _process_chat(
                        cfg, proj, mconn, tconn, chat_id, title, summary
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "tg_tracking_tick: chat %s (%s) failed",
                        chat_id, getattr(proj, "slug", "?"),
                    )
    finally:
        if mconn is not None:
            mconn.close()
        tconn.close()
    return summary


# ---------------------------------------------------------------------------
# Real-time extraction (bridge hook, increment 2b).
# ---------------------------------------------------------------------------


def _empty_extract_summary() -> dict:
    """A zeroed summary dict with the keys ``_process_chat`` touches."""
    return {
        "chats_scanned": 0,
        "chats_processed": 0,
        "chats_skipped_untracked": 0,
        "chats_skipped_no_incoming": 0,
        "watermarks_initialized": 0,
        "todos": 0,
        "meetings": 0,
        "replies": 0,
    }


def process_realtime_chat(
    cfg: Any,
    proj: Any,
    chat_id: int,
    chat_title: Optional[str],
    username: Optional[str] = None,
) -> dict:
    """Extract ONE real-time-tracked chat's new incoming messages, right now.

    The blocking, sync core behind the Telethon bridge's debounced real-time
    hook (``tg_user``). Designed to run OFF the event loop (in a thread executor)
    because ``extract_from_chat`` may block on a ~120s ``claude`` subprocess.

    Reuses the periodic tick's per-chat machinery (``_process_chat``): the SAME
    ``extract`` watermark, the same no-backfill / batch-cap / incoming-only cost
    guards. Sharing the watermark is what dedupes real-time against the 10-min
    periodic tick — whichever runs first advances the cursor, the other sees no
    new messages.

    NO-OP (no DB work, no LLM) unless the EFFECTIVE config (DB-first; see
    ``tg_tracking.get_effective_tracking``) has tracking enabled AND the chat is
    on the effective real-time hot list. Resolving the effective config here is
    what lets a UI change take effect on the very next message — no restart.
    Opens its OWN db connections (independent of the periodic tick's). Returns a
    summary dict.
    """
    summary = _empty_extract_summary()
    # Resolve the EFFECTIVE (DB-first) config once; reuse it for both gates and
    # the per-chat work so a single view is applied consistently.
    eff = tg_tracking.effective_proj(cfg, proj)
    if not eff.tg_track_enabled:
        return summary
    # Real-time is a strict subset of tracked; this also makes the helper a
    # safe no-op for untracked / non-hot-list chats (matches the test contract).
    if not is_realtime(eff, chat_id, chat_title, username):
        return summary

    msgs_path = cfg.tg_user_db_path
    if not Path(msgs_path).exists():
        return summary

    mconn = sqlite3.connect(f"file:{msgs_path}?mode=ro", uri=True)
    mconn.row_factory = sqlite3.Row
    tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        _process_chat(
            cfg, eff, mconn, tconn, chat_id, chat_title, summary,
            username=username,
        )
    except Exception:  # noqa: BLE001 — one bad chat must not kill the bridge.
        log.exception(
            "process_realtime_chat: chat %s (%s) failed",
            chat_id, getattr(proj, "slug", "?"),
        )
    finally:
        mconn.close()
        tconn.close()
    return summary


# ---------------------------------------------------------------------------
# Per-day chat summaries (chat-tracking: 30-day memory) — feeds reply context.
#
# A separate, cheap LLM pass that condenses ONE UTC day of a chat (both
# directions) into a 1-3 sentence factual blurb stored in
# ``chat_daily_summaries`` (tracking_store). The reply pipeline already reads
# these via ``_daily_summaries`` as its BROADER CONTEXT layer.
#
# COST GUARDS mirror the extraction pipeline: tracked chats only; a day with no
# messages records NOTHING and calls NO LLM; an already-summarized day is
# skipped (``force=False``) so the daily catch-up tick is near-free.
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = (
    "You summarize ONE day of a personal Telegram chat into a SHORT, factual "
    "summary of 1-3 sentences. Capture what was actually discussed and any "
    "decisions, plans, commitments or action items. Write plainly in the third "
    "person; do not invent anything not in the transcript. Output ONLY the "
    "summary text — no preamble, no labels, no markdown, and NEVER any "
    "reasoning, self-correction or notes about the summarizing process itself."
)

# ASSISTANT-VOICE meta-commentary that must never reach a stored summary
# (observed live: a summary opened with «Wait—that's wrong. Let me re-read...»
# and was stored verbatim, board bfe04dcf). Lowercase substrings; summaries are
# third-person, so first-person assistant phrasing is a reliable tell.
_SUMMARY_META_MARKERS = (
    "let me ", "i'll ", "i will ", "i cannot", "i can't", "as an ai",
    "i apologize", "my apologies", "here is the summary", "here's the summary",
    "here is a summary", "that's wrong", "re-read", "wait—", "wait -",
)

# Balanced quoted spans ("...", «...», “...”) — single-line, bounded length.
_SUMMARY_QUOTE_RE = re.compile(r'"[^"\n]{0,300}"|«[^»\n]{0,300}»|“[^”\n]{0,300}”')


def _summary_meta_hit(s: str) -> bool:
    """Marker check BLIND to quoted speech. A legitimate third-person summary
    may quote someone (Tim said "let me check", she asked him to «re-read» it)
    — that must not trip the guard, or the day would be rejected on every 6h
    retry FOREVER (same transcript → same summary → same rejection).
    Assistant meta-babble is never inside quotes, so detection is preserved.
    """
    return any(m in _SUMMARY_QUOTE_RE.sub(" ", s).lower()
               for m in _SUMMARY_META_MARKERS)


def _sanitize_summary(text: str) -> str:
    """Return a clean summary, or '' to reject (caller records nothing).

    Light cleanup first (markdown fences, a leading 'Summary:' label). Then a
    meta-commentary check (``_summary_meta_hit`` — blind to quoted speech so a
    summary QUOTING someone can't be rejected forever); on a hit, try to
    SALVAGE the last paragraph — the observed failure shape is meta-babble
    first, real summary last — and keep it only if that paragraph alone is
    clean and substantial. Otherwise reject; a rejected day is simply retried
    on the next summaries run (same contract as an empty LLM result).
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.lower().startswith(("text\n", "markdown\n")):
            t = t.split("\n", 1)[1].strip()
    if t.lower().startswith("summary:"):
        t = t[len("summary:"):].strip()
    if not t:
        return ""
    if not _summary_meta_hit(t):
        return t
    last = t.split("\n\n")[-1].strip()
    if (len(last) > 40 and last != t
            and not _summary_meta_hit(last)):
        log.warning("summary sanitize: salvaged last paragraph "
                    "(dropped meta-commentary head)")
        return last
    log.warning("summary sanitize: REJECTED meta-commentary summary %r", t[:80])
    return ""

# Daily catch-up tick window: re-checks the last few UTC days (yesterday back)
# so a missed run is filled in. Cheap because already-summarized days are
# skipped (no LLM).
SUMMARY_TICK_DAYS = 3
# Default backfill / memory depth.
SUMMARY_BACKFILL_DAYS = 30


def _day_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _day_bounds(day: str) -> tuple[int, int]:
    """Return [start_ts, end_ts) unix-second bounds for a 'YYYY-MM-DD' UTC day."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _build_day_transcript(
    chat_title: Optional[str], day: str, messages: list[dict]
) -> str:
    """Compact transcript of one day's messages (both directions) for summarizing."""
    lines = [f"Chat: {chat_title or '(unknown)'}", f"Day (UTC): {day}", ""]
    for m in messages:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        who = "Tim (you)" if m.get("out") else (m.get("sender_name") or "Unknown")
        lines.append(f"[{_fmt_ts(m.get('ts'))}] {who}: {text}")
    return "\n".join(lines)


def summarize_chat_day(
    cfg: Any,
    proj: Any,
    chat_id: int,
    day: str,
    messages_conn: sqlite3.Connection,
    tracking_conn: sqlite3.Connection,
    force: bool = False,
) -> bool:
    """Summarize ONE UTC ``day`` of a chat into ``chat_daily_summaries``.

    Returns True iff a summary row was written. Gathers that day's messages
    (both directions) from messages.db; if there are NONE it returns False
    WITHOUT calling the LLM or recording anything. If the day is already
    summarized and ``force`` is False it is skipped (no LLM). Otherwise it builds
    a compact transcript, asks ``_summary_llm`` for a 1-3 sentence blurb, runs
    it through ``_sanitize_summary`` (meta-commentary guard), and upserts it.
    An empty/rejected LLM result records nothing (retried next run).
    """
    slug = getattr(proj, "slug", "") or ""
    if not force and tracking_store.has_daily_summary(
        tracking_conn, slug, chat_id, day
    ):
        return False

    start, end = _day_bounds(day)
    rows = messages_conn.execute(
        "SELECT msg_id, ts, sender_name, text, raw_json FROM messages "
        "WHERE chat_id = ? AND ts >= ? AND ts < ? "
        "AND text IS NOT NULL AND text != '' ORDER BY msg_id ASC",
        (int(chat_id), start, end),
    ).fetchall()
    if not rows:
        return False  # empty day → no LLM, record nothing

    crow = messages_conn.execute(
        "SELECT title FROM chats WHERE chat_id = ?", (int(chat_id),)
    ).fetchone()
    chat_title = crow["title"] if crow else None

    msgs = [
        {
            "ts": int(r["ts"] or 0),
            "sender_name": r["sender_name"],
            "text": r["text"],
            "out": not _is_incoming(r["raw_json"]),
        }
        for r in rows
    ]
    transcript = _build_day_transcript(chat_title, day, msgs)
    summary = _sanitize_summary(_summary_llm(_SUMMARY_SYSTEM_PROMPT, transcript))
    if not summary:
        return False  # LLM failed/empty/meta-babble → don't record (retry next run)

    tracking_store.upsert_daily_summary(
        tracking_conn, slug, chat_id, day, summary, len(rows)
    )
    return True


def backfill_summaries(
    cfg: Any,
    proj: Any,
    chat_id: int,
    days: int = SUMMARY_BACKFILL_DAYS,
    messages_conn: Optional[sqlite3.Connection] = None,
    tracking_conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Summarize each of the last ``days`` COMPLETE UTC days (yesterday back).

    Returns the number of days actually summarized. Empty days and
    already-summarized days are skipped (no LLM). Cost-guarded: TRACKED chats
    only — an untracked chat returns 0 without touching the LLM. Today (a partial
    day) is intentionally excluded; it is summarized once it becomes "yesterday"
    (so a stored summary always covers a whole day). Opens its own db handles
    when not supplied, and only closes the ones it opened.
    """
    slug = getattr(proj, "slug", "") or ""
    own_m = own_t = False
    count = 0
    try:
        if messages_conn is None:
            if not Path(cfg.tg_user_db_path).exists():
                return 0
            messages_conn = sqlite3.connect(
                f"file:{cfg.tg_user_db_path}?mode=ro", uri=True
            )
            messages_conn.row_factory = sqlite3.Row
            own_m = True
        if tracking_conn is None:
            tracking_conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
            own_t = True

        crow = messages_conn.execute(
            "SELECT title FROM chats WHERE chat_id = ?", (int(chat_id),)
        ).fetchone()
        chat_title = crow["title"] if crow else None
        # COST GUARD — tracked chats only.
        if not is_tracked(proj, chat_id, chat_title):
            return 0

        today = datetime.now(timezone.utc).date()
        for offset in range(1, int(days) + 1):  # yesterday .. days ago
            day = _day_str(
                datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
                - timedelta(days=offset)
            )
            try:
                if summarize_chat_day(
                    cfg, proj, chat_id, day, messages_conn, tracking_conn
                ):
                    count += 1
            except Exception:  # noqa: BLE001 — one bad day must not abort the rest
                log.exception(
                    "backfill_summaries: %s chat %s day %s failed",
                    slug, chat_id, day,
                )
    finally:
        if own_m and messages_conn is not None:
            messages_conn.close()
        if own_t and tracking_conn is not None:
            tracking_conn.close()
    return count


def summaries_tick(cfg: Any) -> dict:
    """Daily catch-up: summarize recent COMPLETE days for tracked chats.

    For every project whose EFFECTIVE (DB-first) config has tracking enabled,
    summarize the last ``SUMMARY_TICK_DAYS`` days (yesterday back) of each tracked
    chat. Near-free: already-summarized and empty days are skipped, so the only
    LLM cost is the genuinely-new day(s). Pure no-op (no messages.db open, no LLM)
    when nothing is opted in — same COST GUARD as ``tg_tracking_tick``.
    """
    summary = {"projects": 0, "chats": 0, "summaries": 0}
    tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    mconn = None
    try:
        enabled = [
            ep for ep in (
                tg_tracking.effective_proj(cfg, p, conn=tconn)
                for p in cfg.projects.values()
            )
            if ep.tg_track_enabled
        ]
        if not enabled:
            return summary  # COST GUARD: nothing opted in → cheap no-op.

        msgs_path = cfg.tg_user_db_path
        if not Path(msgs_path).exists():
            log.info("summaries_tick: no messages.db yet at %s", msgs_path)
            return summary

        mconn = sqlite3.connect(f"file:{msgs_path}?mode=ro", uri=True)
        mconn.row_factory = sqlite3.Row
        chats = mconn.execute("SELECT chat_id, title FROM chats").fetchall()
        for proj in enabled:
            summary["projects"] += 1
            for crow in chats:
                chat_id = crow["chat_id"]
                title = crow["title"]
                if not is_tracked(proj, chat_id, title):
                    continue
                summary["chats"] += 1
                try:
                    n = backfill_summaries(
                        cfg, proj, chat_id, days=SUMMARY_TICK_DAYS,
                        messages_conn=mconn, tracking_conn=tconn,
                    )
                    summary["summaries"] += n
                except Exception:  # noqa: BLE001
                    log.exception(
                        "summaries_tick: chat %s (%s) failed",
                        chat_id, getattr(proj, "slug", "?"),
                    )
    finally:
        if mconn is not None:
            mconn.close()
        tconn.close()
    return summary


# ---------------------------------------------------------------------------
# On-demand historic SWEEP (chat-tracking) — mine a chat's recent history.
#
# When Tim ADDS a chat, the forward 'extract' watermark plants at the current
# max (NO backfill), so historic todos/meetings are never mined. The sweep is an
# ON-DEMAND pass (cost) that processes the last ``days`` of a chat through the
# SAME ``extract_from_chat`` in MAX_BATCH chunks AND backfills its daily
# summaries, so a freshly-added chat gets historic data.
#
# IDEMPOTENCY: a ``pipeline_watermark`` row pipeline='sweep' stores the EARLIEST
# swept ``msg_id`` (in ``last_msg_id``). A message is SKIPPED if it was already
# swept (msg_id >= that earliest) OR is owned by the forward 'extract' cursor
# (msg_id > the 'extract' watermark — those are processed going-forward). So
# re-clicking the same window inserts nothing new; widening ``days`` mines only
# the newly-exposed older messages.
#
# COST BOUND: a single ``days``-wide window, incoming messages only, fed in
# MAX_BATCH chunks; tracked chats only.
# ---------------------------------------------------------------------------

SWEEP_PIPELINE = "sweep"


def sweep_chat(
    cfg: Any,
    proj: Any,
    chat_id: int,
    days: int = 7,
    progress: Any = None,
    messages_conn: Optional[sqlite3.Connection] = None,
    tracking_conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Mine the last ``days`` of ONE chat for todos/meetings + daily summaries.

    Returns ``{"todos": n, "meetings": n, "summaries": n, "days": days}``.
    TRACKED chats only (cost guard). Processes the historic window's INCOMING
    messages through ``extract_from_chat`` in MAX_BATCH chronological chunks,
    skipping anything already covered by a prior sweep OR owned by the forward
    'extract' cursor (see module header), then backfills the same window's daily
    summaries. ``progress(dict)``, if given, is called after each chunk.
    """
    slug = getattr(proj, "slug", "") or ""
    result = {"todos": 0, "meetings": 0, "summaries": 0, "days": int(days)}
    own_m = own_t = False
    try:
        if messages_conn is None:
            if not Path(cfg.tg_user_db_path).exists():
                return result
            messages_conn = sqlite3.connect(
                f"file:{cfg.tg_user_db_path}?mode=ro", uri=True
            )
            messages_conn.row_factory = sqlite3.Row
            own_m = True
        if tracking_conn is None:
            tracking_conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
            own_t = True

        crow = messages_conn.execute(
            "SELECT title FROM chats WHERE chat_id = ?", (int(chat_id),)
        ).fetchone()
        chat_title = crow["title"] if crow else None
        # COST GUARD — tracked chats only.
        if not is_tracked(proj, chat_id, chat_title):
            return result

        now = int(time.time())
        cutoff_ts = now - int(days) * 86400
        extract_wm = tracking_store.get_watermark(
            tracking_conn, slug, chat_id, PIPELINE
        )
        sweep_wm = tracking_store.get_watermark(
            tracking_conn, slug, chat_id, SWEEP_PIPELINE
        )

        rows = messages_conn.execute(
            "SELECT msg_id, ts, sender_name, text, raw_json FROM messages "
            "WHERE chat_id = ? AND ts >= ? ORDER BY msg_id ASC",
            (int(chat_id), cutoff_ts),
        ).fetchall()

        cand = []
        for r in rows:
            mid = int(r["msg_id"])
            # Forward 'extract' cursor owns msg_id > extract_wm (going-forward).
            if extract_wm and mid > extract_wm:
                continue
            # Already swept (earliest-swept stored in last_msg_id).
            if sweep_wm and mid >= sweep_wm:
                continue
            if not _is_incoming(r["raw_json"]):
                continue
            if not (r["text"] or "").strip():
                continue
            cand.append(r)

        for i in range(0, len(cand), MAX_BATCH):
            chunk = cand[i:i + MAX_BATCH]
            msgs = [
                {
                    "msg_id": int(r["msg_id"]),
                    "ts": int(r["ts"] or 0),
                    "sender_name": r["sender_name"],
                    "text": r["text"],
                }
                for r in chunk
            ]
            res = extract_from_chat(cfg, proj, chat_id, chat_title, msgs)
            result["todos"] += res.get("todos", 0)
            result["meetings"] += res.get("meetings", 0)
            if progress is not None:
                try:
                    progress({
                        "chat_id": chat_id, "chunk": i // MAX_BATCH + 1,
                        "todos": result["todos"], "meetings": result["meetings"],
                    })
                except Exception:  # noqa: BLE001
                    pass

        # Advance the sweep watermark DOWNWARD to the earliest swept msg_id so a
        # re-click over the same/overlapping window is a no-op.
        if cand:
            min_swept = min(int(r["msg_id"]) for r in cand)
            new_from = min_swept if not sweep_wm else min(int(sweep_wm), min_swept)
            tracking_store.set_watermark(
                tracking_conn, slug, chat_id, SWEEP_PIPELINE, new_from, cutoff_ts
            )

        # Build/refresh the same window's daily summaries (skips done/empty days).
        result["summaries"] = backfill_summaries(
            cfg, proj, chat_id, days=days,
            messages_conn=messages_conn, tracking_conn=tracking_conn,
        )
    finally:
        if own_m and messages_conn is not None:
            messages_conn.close()
        if own_t and tracking_conn is not None:
            tracking_conn.close()
    return result


def sweep_tracked(cfg: Any, slug: str, days: int = 7) -> dict:
    """Sweep EVERY effectively-tracked chat of ``slug`` (on-demand, cost).

    Resolves the EFFECTIVE (DB-first) config; a pure no-op returning zeros when
    tracking is not enabled for ``slug``. Aggregates per-chat sweep results.
    """
    agg = {"slug": slug, "days": int(days), "chats": 0,
           "todos": 0, "meetings": 0, "summaries": 0}
    if not Path(cfg.tg_user_db_path).exists():
        return agg

    tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    mconn = None
    try:
        proj = (getattr(cfg, "projects", {}) or {}).get(slug)
        if proj is None:
            proj = SimpleNamespace(
                slug=slug, tg_track_enabled=False, tg_track_mode="exclude",
                tg_track_chats=(), tg_track_realtime=(),
            )
        eff = tg_tracking.effective_proj(cfg, proj, conn=tconn)
        if not eff.tg_track_enabled:
            return agg  # COST GUARD: tracking not enabled → nothing to sweep.

        mconn = sqlite3.connect(f"file:{cfg.tg_user_db_path}?mode=ro", uri=True)
        mconn.row_factory = sqlite3.Row
        chats = mconn.execute("SELECT chat_id, title FROM chats").fetchall()
        for crow in chats:
            chat_id = crow["chat_id"]
            title = crow["title"]
            if not is_tracked(eff, chat_id, title):
                continue
            try:
                res = sweep_chat(
                    cfg, eff, chat_id, days=days,
                    messages_conn=mconn, tracking_conn=tconn,
                )
            except Exception:  # noqa: BLE001 — one bad chat must not kill the sweep
                log.exception("sweep_tracked: chat %s (%s) failed", chat_id, slug)
                continue
            agg["chats"] += 1
            agg["todos"] += res.get("todos", 0)
            agg["meetings"] += res.get("meetings", 0)
            agg["summaries"] += res.get("summaries", 0)
    finally:
        if mconn is not None:
            mconn.close()
        tconn.close()
    return agg
