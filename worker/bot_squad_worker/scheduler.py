"""APScheduler wiring."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from bot_squad_worker.config import Config

if TYPE_CHECKING:
    pass

# Module-level started_at timestamp, set once at import time.
_STARTED_AT: datetime = datetime.now(timezone.utc)


def state_for_api(sched: BackgroundScheduler, cfg: Config) -> dict:
    """Return a serialisable dict describing the current scheduler state."""
    jobs = []
    for j in sched.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })

    hb_age = None
    hb = cfg.heartbeat_path
    if hb.exists():
        try:
            hb_age = time.time() - hb.stat().st_mtime
        except OSError:
            pass

    return {
        "jobs": jobs,
        "worker_started_at": _STARTED_AT.isoformat(),
        "last_heartbeat_age_seconds": hb_age,
    }


def build_scheduler(cfg: Config) -> BackgroundScheduler:
    # R23: imported here, not at module top — ``__main__`` imports this module
    # in every mode, and a user-worker must never load the coordinator-only
    # autoupdate / sleep / swarm_* modules (they are not shipped in a bundle).
    from bot_squad_worker import autoupdate as _autoupdate
    from bot_squad_worker.jobs import (
        autoupdate_apply_tick,
        autoupdate_tick,
        calendar_sync_tick,
        daily_digest_tick,
        deploy_monitor,
        heartbeat,
        librarian_tick,
        morning_events_tick,
        morning_reminder_tick,
        nudge_reconcile_tick,
        oauth_refresh,
        reply_watch_tick,
        summaries_tick,
        tg_listener_tick,
        tg_tracking_tick,
    )
    from bot_squad_worker.swarm_autosleep import autosleep_tick

    sched = BackgroundScheduler(timezone="UTC")

    sched.add_job(
        heartbeat,
        "interval",
        seconds=60,
        args=[cfg],
        id="heartbeat",
        replace_existing=True,
    )
    sched.add_job(
        deploy_monitor,
        "interval",
        seconds=60,
        args=[cfg],
        id="deploy_monitor",
        replace_existing=True,
    )
    # oauth_refresh: v1 placeholder — checks claude binary reachable.
    # Full token-rotation port from cctv-backend deferred to a later spec.
    sched.add_job(
        oauth_refresh,
        CronTrigger(hour="2,8,14,20", minute=0, timezone="UTC"),
        args=[cfg],
        id="oauth_refresh",
        replace_existing=True,
    )
    # tg_listener: poll Telegram for replies and route them into sessions (spec #7).
    sched.add_job(
        tg_listener_tick,
        "interval",
        seconds=30,
        args=[cfg],
        id="tg_listener",
        replace_existing=True,
    )
    # tg_tracking: periodic LLM extraction over tracked chats (chat-tracking
    # inc. 2). ~10-min cadence. Gated on per-project tg_track_enabled (default
    # False) so it is a cheap no-op until a project opts in — no LLM cost.
    # max_instances=1 + coalesce so a slow LLM pass can't pile up overlapping
    # sweeps.
    sched.add_job(
        tg_tracking_tick,
        "interval",
        seconds=600,
        args=[cfg],
        id="tg_tracking",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # summaries_tick: daily per-chat summary catch-up (chat-tracking 30-day
    # memory). Every 6h — summarizes recent COMPLETE UTC days for tracked chats.
    # Gated on the same EFFECTIVE tg_track_enabled cost guard as tg_tracking, so
    # it is a cheap no-op until a project opts in, and near-free thereafter
    # (already-summarized days are skipped). max_instances=1 + coalesce so a slow
    # LLM pass can't pile up overlapping sweeps.
    sched.add_job(
        summaries_tick,
        CronTrigger(hour="0,6,12,18", minute=0, timezone="UTC"),
        args=[cfg],
        id="tg_summaries",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # calendar_sync: poll the iCloud Swarm calendar for accept/decline on
    # invited meetings (CalDAV invitation flow) and sync status back into
    # tracking.db. ~5-min cadence. Pure no-op when iCloud isn't configured.
    # max_instances=1 + coalesce so a slow CalDAV poll can't pile up overlapping
    # sweeps.
    sched.add_job(
        calendar_sync_tick,
        "interval",
        seconds=300,
        args=[cfg],
        id="calendar_sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # morning_reminder: once-a-day proactive digest of pending todos to Tim on
    # the life bot at a set local AM time. The tick itself gates on time window
    # + sent-today stamp (app_settings), so the 5-min cadence is a cheap no-op
    # outside the window. max_instances=1 + coalesce as usual.
    sched.add_job(
        morning_reminder_tick,
        "interval",
        seconds=300,
        args=[cfg],
        id="morning_reminder",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # daily_digest: once-a-day proactive brief of yesterday's chat summaries to
    # the owner on the configured bot. Same self-gating pattern as morning_reminder — the
    # 5-min cadence is a cheap no-op outside the window.
    sched.add_job(
        daily_digest_tick,
        "interval",
        seconds=300,
        args=[cfg],
        id="daily_digest",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # morning_events: once-a-day standalone message with TODAY's calendar
    # events (split from the daily brief per Tim, board aad153ce). Same
    # self-gating pattern — 5-min cadence, cheap no-op outside the window.
    sched.add_job(
        morning_events_tick,
        "interval",
        seconds=300,
        args=[cfg],
        id="morning_events",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # reply_watch: fast sweep for landed third-party replies (reply_watches
    # table) — fired watches are DM'd to the owner on the configured bot within ~45s
    # instead of waiting for coord-life's next turn. Pure no-op when no watch
    # is open or the [lifetrack] bot isn't configured. max_instances=1 +
    # coalesce so a slow Telegram send can't pile up overlapping sweeps.
    sched.add_job(
        reply_watch_tick,
        "interval",
        seconds=45,
        args=[cfg],
        id="reply_watch",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # autonomous_tick: DISABLED 2026-05-12 — autonomous work is frozen pending
    # the new operating model. The autonomous module + actions remain on disk
    # but no background tick fires. Re-enable here when the model is ready.

    # autoupdate_tick: poll mothership release feed (T-0083). No-op on the
    # mothership itself (self-exclusion via T-0086). Cadence is configurable
    # via BOT_SQUAD_AUTOUPDATE_INTERVAL_SECONDS (default 900s = 15min).
    sched.add_job(
        autoupdate_tick,
        "interval",
        seconds=_autoupdate.interval_seconds(),
        args=[cfg],
        id="autoupdate",
        replace_existing=True,
    )
    # autoupdate_apply_tick: drain the apply queue (T-0084). Runs at a
    # shorter cadence than the poll so a freshly-enqueued release starts
    # applying promptly. max_instances=1 prevents overlapping applies (one
    # apply can take many minutes; the next tick must not pile on).
    sched.add_job(
        autoupdate_apply_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="autoupdate_apply",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # Bot-swarm autosleep: every minute, per-project, fire sleep on
    # any live expert past its transcript threshold. No-op for projects
    # where strategy.auto_sleep_enabled is False (default). One sleep
    # cycle takes 30–60s, so max_instances=1 + coalesce=True prevents
    # piled-up ticks while a slow scribe finishes.
    sched.add_job(
        autosleep_tick,
        "interval",
        seconds=60,
        args=[cfg],
        id="swarm_autosleep",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # librarian: continuous artifact-substrate keeper. Every ~6h it assesses
    # ACTIVE (distilled/canonical) artifacts for RELEVANCE via an LLM judge and
    # archives the stale/trivial/superseded ones (non-destructively — state
    # flips to 'archived', content preserved), then runs a dedup sweep. No-op
    # when core.db doesn't exist. max_instances=1 + coalesce so a slow LLM pass
    # can't pile up overlapping sweeps.
    sched.add_job(
        librarian_tick,
        CronTrigger(hour="4,10,16,22", minute=0, timezone="UTC"),
        args=[cfg],
        id="librarian",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # S1 nudge reconcile: every ~30s, re-deliver wakes that inject_input
    # silently dropped (suspended/respawned/zombie pane, or busy pane). For
    # each active session with unread inbox + idle pane whose last nudge is
    # older than the backoff, re-fire the nudge. max_instances=1 + coalesce
    # keeps overlapping ticks (idle-probe sleeps make a sweep take a few
    # seconds) from piling up.
    sched.add_job(
        nudge_reconcile_tick,
        "interval",
        seconds=30,
        args=[cfg],
        id="nudge_reconcile",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # Token-saving pause (Tim 2026-07-08): PAUSE_LIFE_JOBS=1 drops the proactive
    # life-bot jobs — the LLM chat-summary sweep and the daily AM sends — while
    # the life coordinator is stopped. Cheap infra ticks (calendar_sync,
    # reply_watch, nudges) stay. Unset the env + restart the worker to restore.
    if os.environ.get("PAUSE_LIFE_JOBS", "").lower() in ("1", "true", "yes"):
        for _jid in ("tg_summaries", "morning_reminder", "daily_digest",
                     "morning_events"):
            try:
                sched.remove_job(_jid)
            except Exception:  # noqa: BLE001 — job absent = already gone
                pass

    return sched
