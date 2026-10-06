"""Time-driven jobs the worker runs via APScheduler.

v1 ships only the heartbeat. Spec #3 adds deploy_monitor, oauth_refresh.
"""
from __future__ import annotations

import logging

from bot_squad_worker.config import Config

log = logging.getLogger(__name__)


def heartbeat(cfg: Config) -> None:
    """Touch heartbeat file so the API can show worker liveness."""
    cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_path.touch()


def deploy_monitor(cfg: Config) -> None:
    """Iterate registered projects; run the next queued deploy for each.

    Per-project exceptions are caught and logged; one bad project doesn't
    kill the whole sweep.
    """
    from bot_squad_worker import deploy as _deploy

    for slug, project in cfg.projects.items():
        try:
            _run_project_deploy(cfg, slug, project)
        except Exception:
            log.exception("deploy_monitor: unhandled error for project %s", slug)


def _run_project_deploy(cfg: Config, slug: str, project: object) -> None:
    """Pop and run one deploy for ``slug``, sending TG pings."""
    from bot_squad_worker import deploy as _deploy
    from bot_squad_worker.actions import _get_tg_client

    queued = _deploy.list_queued(cfg, slug)
    if not queued:
        return

    tg = _get_tg_client(cfg)
    chat_id = project.tg_chat  # type: ignore[attr-defined]
    sid = "deploy_monitor"

    # Ping at queue time
    tg.send(chat_id=chat_id, text=f"🚚 starting deploy for {slug}", sid=sid)

    result = _deploy.run_next(cfg, slug)
    if result is None:
        # Tree was dirty — don't ping (not an error, just deferred)
        log.info("deploy_monitor: %s deploy deferred (dirty tree)", slug)
        return

    if result.ok:
        tg.send(
            chat_id=chat_id,
            text=f"✅ deploy {slug} SUCCESS (rc={result.returncode})",
            sid=sid,
        )
    else:
        tg.send(
            chat_id=chat_id,
            text=f"❌ deploy {slug} FAILED rc={result.returncode}",
            sid=sid,
        )


def tg_listener_tick(cfg: Config) -> None:
    """Poll Telegram for incoming replies and route them into sessions.

    Scheduled every 30s by APScheduler. Errors are caught and logged so
    one bad update cycle never kills the scheduler.
    """
    from bot_squad_worker import tg_listener
    try:
        tg_listener.tick(cfg)
    except Exception:
        log.exception("tg_listener_tick error")


def tg_tracking_tick(cfg: Config) -> None:
    """Periodic LLM extraction sweep over tracked chats (chat-tracking inc. 2).

    Scheduled every ~10 min by APScheduler. Gated on per-project
    ``tg_track_enabled`` (default False), so it is a pure no-op — and incurs
    ZERO LLM cost — until an operator opts a project in. Errors are caught and
    logged so one bad sweep never kills the scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        tg_pipelines.tg_tracking_tick(cfg)
    except Exception:
        log.exception("tg_tracking_tick error")


def calendar_sync_tick(cfg: Config) -> None:
    """Poll the iCloud Swarm calendar for accept/decline on invited meetings.

    Scheduled every ~5 min by APScheduler. Detected meetings are auto-written to
    the Swarm calendar as CalDAV INVITATIONS (Tim accepts/declines in Apple
    Calendar); this tick reads each invited meeting's ATTENDEE PARTSTAT back and
    updates the meeting row (accepted / declined+delete). Pure no-op when iCloud
    isn't configured. Errors are caught and logged so one bad poll never kills
    the scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        # First reconcile any confirmed meeting that never reached the calendar
        # (e.g. assistant-scheduled, inserted outside the extraction path), THEN
        # poll acceptances — so a freshly-backfilled invite is visible next tick.
        tg_pipelines.calendar_backfill_tick(cfg)
    except Exception:
        log.exception("calendar_backfill_tick error")
    try:
        tg_pipelines.calendar_sync_tick(cfg)
    except Exception:
        log.exception("calendar_sync_tick error")


def reply_watch_tick(cfg: Config) -> None:
    """Fast reply-watch sweep: detect fired watches and DM the owner on the configured bot.

    Scheduled every ~45s by APScheduler — the low-latency delivery path for
    reply watches (a landed third-party reply reaches Tim within a minute
    instead of waiting for coord-life's next turn). Pure no-op when no watch
    fires or the [lifetrack] bot isn't configured. Errors are caught and
    logged so one bad sweep never kills the scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        tg_pipelines.reply_watch_notify_tick(cfg)
    except Exception:
        log.exception("reply_watch_notify_tick error")


def morning_reminder_tick(cfg: Config) -> None:
    """Proactive morning digest: DM Tim his pending todos at a set local AM time.

    Scheduled every ~5 min by APScheduler; tg_pipelines.morning_reminder_tick
    does the time-window/once-per-day gating, so almost every run is a pure
    no-op. Errors are caught and logged so one bad run never kills the
    scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        tg_pipelines.morning_reminder_tick(cfg)
    except Exception:
        log.exception("morning_reminder_tick error")


def daily_digest_tick(cfg: Config) -> None:
    """Proactive morning brief: DM Tim yesterday's chat summaries once a day.

    Scheduled every ~5 min by APScheduler; tg_pipelines.daily_digest_tick does
    the time-window/once-per-day gating (shared with the morning reminder), so
    almost every run is a pure no-op. Errors are caught and logged so one bad
    run never kills the scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        tg_pipelines.daily_digest_tick(cfg)
    except Exception:
        log.exception("daily_digest_tick error")


def morning_events_tick(cfg: Config) -> None:
    """Standalone morning events message: DM Tim today's calendar once a day.

    Split from the daily brief per Tim (board aad153ce). Scheduled every ~5 min
    by APScheduler; tg_pipelines.morning_events_tick does the time-window/
    once-per-day gating, so almost every run is a pure no-op. Errors are caught
    and logged so one bad run never kills the scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        tg_pipelines.morning_events_tick(cfg)
    except Exception:
        log.exception("morning_events_tick error")


def summaries_tick(cfg: Config) -> None:
    """Daily per-chat summary catch-up over tracked chats (chat-tracking memory).

    Scheduled every ~6h by APScheduler. Summarizes recent COMPLETE UTC days
    (yesterday back) for each enabled project's tracked chats. Gated on the same
    EFFECTIVE ``tg_track_enabled`` cost guard as ``tg_tracking_tick`` — a pure
    no-op (ZERO LLM cost) until a project opts in — and near-free thereafter
    because already-summarized days are skipped. Errors are caught and logged so
    one bad sweep never kills the scheduler.
    """
    from bot_squad_worker import tg_pipelines
    try:
        tg_pipelines.summaries_tick(cfg)
    except Exception:
        log.exception("summaries_tick error")


def autoupdate_tick(cfg: Config) -> None:
    """Poll the mothership release feed and queue apply jobs when newer.

    Scheduled at ``BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS`` (default 900s)
    by APScheduler. Errors are caught and logged so one bad poll cycle
    never kills the scheduler. See ``bot_squad_worker.autoupdate`` for
    the full contract (T-0083). No-op on the mothership itself (T-0086).
    """
    from bot_squad_worker import autoupdate as _autoupdate
    try:
        _autoupdate.tick(cfg)
    except Exception:
        log.exception("autoupdate_tick error")


def autoupdate_apply_tick(cfg: Config) -> None:
    """Drain the apply queue produced by the poller (T-0084).

    Scheduled at a shorter cadence than the poll tick so a freshly-enqueued
    release starts applying within ~1 minute. The apply itself can take
    many minutes (download + build + smoke); APScheduler ``max_instances=1``
    on the registered job keeps overlapping ticks from starting a second
    apply mid-flight. No-op on the mothership itself (T-0086).
    """
    from bot_squad_worker import autoupdate_apply as _apply
    try:
        _apply.tick(cfg)
    except Exception:
        log.exception("autoupdate_apply_tick error")


def autonomous_tick(cfg: Config) -> None:
    """Run one orchestrator tick for every project that has autonomous mode enabled.

    Scheduled every 60 seconds by APScheduler. Per-project exceptions are
    caught and logged so one bad project doesn't kill the whole sweep.
    """
    from bot_squad_worker import autonomous as _auto

    for slug in cfg.projects:
        try:
            _auto.tick(cfg, slug)
        except Exception:
            log.exception("autonomous_tick: unhandled error for project %s", slug)


# ---------------------------------------------------------------------------
# Nudge reconcile (S1): re-deliver wakes that inject_input silently dropped
# ---------------------------------------------------------------------------

# Backoff between re-nudges for the same sid. A wake that was lost (no live
# pane at peer_send time, or the pane was busy) should be retried, but not
# every tick — that would spam a pane whose owner is simply slow to read.
_NUDGE_BACKOFF_SEC = 60.0


def _last_nudge_path(cfg: Config, sid: str):
    """Marker file recording the monotonic-wall time of the last nudge for a
    sid. Lives under data/_worker so it's outside any project slug namespace.
    """
    return cfg.data_dir / "_worker" / f"last_nudge-{sid}"


def _last_nudge_age_sec(cfg: Config, sid: str, now: float) -> float:
    """Seconds since the last recorded nudge for ``sid``; +inf if never."""
    import math

    p = _last_nudge_path(cfg, sid)
    if not p.is_file():
        return math.inf
    try:
        return now - p.stat().st_mtime
    except OSError:
        return math.inf


def _record_nudge(cfg: Config, sid: str) -> None:
    p = _last_nudge_path(cfg, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def _unread_count(cfg: Config, slug: str, sid: str) -> int:
    """Number of inbox lines past the seen high-water mark for ``sid``.

    Derived the same way intersession.inbox_read does (file size vs seen
    offset) but WITHOUT mutating seen — this is a read-only probe.
    """
    from bot_squad_worker import intersession as _is

    inbox = _is._inbox_path(cfg, slug, sid)
    seen = _is._seen_path(cfg, slug, sid)
    if not inbox.exists():
        return 0
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    try:
        size = inbox.stat().st_size
    except FileNotFoundError:
        return 0
    if size <= offset:
        return 0
    with inbox.open("rb") as f:
        f.seek(offset)
        unread = f.read(size - offset)
    return unread.count(b"\n")


def nudge_reconcile_tick(cfg: Config) -> dict:
    """S1: re-deliver wakes that were silently lost.

    A peer_send appends a durable inbox line AND fires a tmux nudge via
    inject_input. The nudge can vanish (expert suspended/respawned/zombie so
    no pane matched the sid, or the pane was busy) while the inbox line
    persists — and nothing re-nudges. This tick reconciles: for each ACTIVE
    session with unread inbox and an idle pane, if the last nudge was longer
    ago than the backoff, re-fire the SAME nudge inject_input uses.

    Sleeping/halted and suspended/paused sessions are skipped (no live pane
    to wake, or the operator deliberately quieted them).

    Returns a summary dict for scheduler_state / tests.
    """
    import time

    from bot_squad_worker import actions as _actions
    from bot_squad_worker import sessions as _sessions

    nudged: list[dict] = []
    skipped: list[dict] = []
    scanned = 0
    now = time.time()

    for slug in cfg.projects:
        scanned += 1
        try:
            rows = _sessions.list_sessions(cfg, slug)
        except Exception as e:  # noqa: BLE001
            log.warning("nudge_reconcile: list_sessions(%s) failed: %s", slug, e)
            skipped.append({"slug": slug, "reason": f"list_sessions: {e}"})
            continue

        for row in rows:
            sid = row.get("sid")
            if not sid:
                continue
            # Only sessions with a live pane (active) are nudgeable.
            if row.get("status") != "active":
                skipped.append({"slug": slug, "sid": sid, "reason": "not_active"})
                continue
            # Halted/sleeping sessions were deliberately quieted — don't wake.
            if row.get("halted"):
                skipped.append({"slug": slug, "sid": sid, "reason": "halted"})
                continue
            if _unread_count(cfg, slug, sid) <= 0:
                continue  # nothing pending, no need to nudge
            age = _last_nudge_age_sec(cfg, sid, now)
            if age < _NUDGE_BACKOFF_SEC:
                skipped.append({"slug": slug, "sid": sid, "reason": "backoff",
                                "age_sec": age})
                continue
            # Resolve the live pane; only nudge if it's currently idle so we
            # don't clobber an in-flight tool call.
            try:
                pane = _actions._resolve_pane_for_sid(sid)
            except Exception as e:  # noqa: BLE001
                log.warning("nudge_reconcile: resolve pane(%s) failed: %s", sid, e)
                pane = None
            if pane is None:
                skipped.append({"slug": slug, "sid": sid, "reason": "no_pane"})
                continue
            try:
                idle = _actions._pane_appears_idle(pane.pane_id)
            except Exception:  # noqa: BLE001
                idle = False
            if not idle:
                skipped.append({"slug": slug, "sid": sid, "reason": "pane_busy"})
                continue
            # Re-fire the SAME mechanism peer_send uses. The nudge string is
            # generic (no from_sid) — the recipient just needs to be told to
            # call peer_inbox_read.
            nudge_text = "[swarm] unread peer mail — call peer_inbox_read to view"
            try:
                _actions._action_inject_input({"sid": sid, "text": nudge_text})
            except Exception as e:  # noqa: BLE001
                log.warning("nudge_reconcile: inject_input(%s) failed: %s", sid, e)
                skipped.append({"slug": slug, "sid": sid, "reason": f"inject: {e}"})
                continue
            _record_nudge(cfg, sid)
            nudged.append({"slug": slug, "sid": sid})

    return {"projects_scanned": scanned, "nudged": nudged, "skipped": skipped}


def librarian_tick(cfg: Config) -> None:
    """Periodic LIBRARIAN pass over the core.db artifact substrate.

    Scheduled every ~6h by APScheduler. Opens the sibling ``core.db`` (same path
    the UI + other core-touching code use: ``<tracking.db dir>/core.db``) and runs
    the full continuous-tending toolkit over the artifact substrate:

    1. RELEVANCE pass — an LLM judge that archives stale/trivial/superseded/one-off
       artifacts (CONSERVATIVE: KEEP on any doubt).
    2. exact/fuzzy ``dedup`` — mark near-identical-text duplicates.
    3. ``semantic_dedup`` — LLM-judged collapse of artifacts stating the SAME
       knowledge phrased differently (archive losers, keep the best).
    4. ``consolidate_old`` — roll OLD per-chat daily summaries into a canonical
       rollup + archive the granular dailies.
    (enrich_pass — web-context enrichment — is intentionally NOT run here;
       artifacts as SEPARATE linked notes.

    Everything is non-destructive (state flips to 'archived', content + edges
    preserved). Each pass is BOUNDED (per-pass caps) and guarded in its OWN
    try/except so one failing pass never stops the others. Pure no-op when core.db
    doesn't exist yet.
    """
    from pathlib import Path
    try:
        from bot_squad_worker.core import librarian as _librarian
        from bot_squad_worker.core import store as _core_store

        core_path = Path(cfg.tg_tracking_db_path).parent / "core.db"
        if not core_path.exists():
            return
        conn = _core_store.connect(core_path)
        try:
            try:
                res = _librarian.relevance_pass(conn)
                _librarian.dedup(conn)
                log.info("librarian_tick: relevance %s", res)
            except Exception:
                log.exception("librarian_tick: relevance/dedup pass failed")
            try:
                sem = _librarian.semantic_dedup(conn)
                log.info("librarian_tick: semantic_dedup %s", sem)
            except Exception:
                log.exception("librarian_tick: semantic_dedup pass failed")
            try:
                roll = _librarian.consolidate_old(conn)
                log.info("librarian_tick: consolidate_old %s", roll)
            except Exception:
                log.exception("librarian_tick: consolidate_old pass failed")
            # NOTE: enrich_pass (web-context enrichment) is DELIBERATELY NOT run
            # automatically — it over-reached, blanket-researching contacts/people
            # and creating junk "context:" artifacts (Tim, 2026-07-03). Enrichment
            # must be RARE + targeted (a specific, non-personal knowledge gap the
            # Librarian explicitly flags), not per-artifact. The capability stays
            # in core.librarian for deliberate/gated future use, but it is OFF here.
        finally:
            conn.close()
    except Exception:
        log.exception("librarian_tick error")


def oauth_refresh(cfg: Config) -> None:
    """Refresh Claude OAuth credentials. TG-ping on failure only.

    Calls refresh_oauth(cfg) from refresh_oauth.py and pings TG if it
    returns ok=False.
    """
    from bot_squad_worker.refresh_oauth import refresh_oauth as _refresh
    from bot_squad_worker.actions import _get_tg_client

    try:
        result = _refresh(cfg)
    except Exception as e:
        log.exception("oauth_refresh: unexpected error")
        result = {"ok": False, "action": "failed", "detail": str(e)}

    if not result.get("ok"):
        tg = _get_tg_client(cfg)
        detail = result.get("detail", "unknown error")
        for slug, project in cfg.projects.items():
            tg.send(
                chat_id=project.tg_chat,
                text=f"❌ oauth_refresh FAILED: {detail}",
                sid="oauth_refresh",
            )
            break  # ping only the first project (single TG chat for now)
