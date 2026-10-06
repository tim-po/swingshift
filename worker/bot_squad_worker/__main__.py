"""Worker entrypoint.

Run with:
    python -m bot_squad_worker --config /home/www/bot-squad/config

In production, started by systemd via bot-squad-worker.service.

Phase 2 multi-user: BOT_SQUAD_MODE selects role.
  - "coordinator" (or unset): scheduler + all actions, socket worker.sock.
  - "user-worker": tmux ops only, socket user-<USER>.sock; no scheduler.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from bot_squad_worker.actions import set_config, set_degraded, set_mode, set_scheduler
from bot_squad_worker.config import Config, ConfigError, default_config_dir
from bot_squad_worker.install_role import is_mothership, warn_if_misconfigured
from bot_squad_worker.scheduler import build_scheduler
from bot_squad_worker.server import build_app


def _user_sock_path(cfg: Config, linux_user: str) -> Path:
    return cfg.data_dir / "_sock" / f"user-{linux_user}.sock"


def _start_sock_chgrp(sock_path: Path) -> "threading.Thread | None":
    """Best-effort: set group ownership on the per-user socket so the API
    container (which runs as group www) can connect. R22: when the group does
    not exist (every fresh box / bundle) start nothing and log at DEBUG only."""
    import grp, threading, time as _time
    log = logging.getLogger("bot-squad-worker")
    group = os.environ.get("BOT_SQUAD_SOCK_GROUP", "www")
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        log.debug("no %r group: user socket chgrp skipped", group)
        return None
    # B-0: macOS ships a `www` group (alias of _www) the user is not in, so the
    # chown is EPERM and every start logged a WARNING. A non-root process can
    # only chgrp to a group it belongs to — skip quietly otherwise.
    if (os.geteuid() != 0 and gid != os.getegid()
            and gid not in os.getgroups()):
        log.debug("not a member of %r: user socket chgrp skipped", group)
        return None

    def _delayed_chgrp() -> None:
        _time.sleep(1.0)
        try:
            if sock_path.exists():
                os.chown(sock_path, -1, gid)
                sock_path.chmod(0o660)
                log.info("user socket perms tightened to 0660 (group %s)", group)
        except (PermissionError, OSError) as e:
            log.warning("could not chgrp user socket: %s", e)
    t = threading.Thread(target=_delayed_chgrp, daemon=True)
    t.start()
    return t


def main() -> int:
    parser = argparse.ArgumentParser(prog="bot-squad-worker")
    parser.add_argument(
        "--config",
        default=str(default_config_dir()),
        help="path to bot-squad config dir (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "info"),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    log = logging.getLogger("bot-squad-worker")

    mode = os.environ.get("BOT_SQUAD_MODE", "").strip() or "coordinator"
    if mode not in ("coordinator", "user-worker"):
        log.error("invalid BOT_SQUAD_MODE: %r (want coordinator|user-worker)", mode)
        return 2
    set_mode(mode)
    log.info("mode=%s", mode)

    # F1 readiness gating: a malformed/incomplete config must NOT crash-loop
    # the daemon. If Config.load() fails we still BIND the socket (so the
    # coordinator/API get a clear error instead of "connection refused") and
    # run in a degraded mode: /health → ok:false, actions → 503 with detail.
    config_dir = Path(args.config)
    degraded = ""
    try:
        cfg = Config.load(config_dir)
        set_config(cfg)
        log.info("loaded config: %d project(s)", len(cfg.projects))
    except (ConfigError, FileNotFoundError, OSError) as e:
        degraded = str(e) or f"{type(e).__name__}: config could not be loaded"
        # Bare Config so the socket path (config_dir-derived) still resolves;
        # projects is empty and set_degraded() makes dispatch refuse actions.
        cfg = Config(config_dir=config_dir)
        set_config(cfg)
        set_degraded(degraded)
        log.error("config load failed; entering DEGRADED mode: %s", degraded)

    if not degraded:
        # T-0086 integration check: warn early if mothership flag and prod
        # deploy_targets disagree for bot-squad itself.
        log.info(
            "install_role: is_mothership(bot-squad)=%s",
            is_mothership(slug="bot-squad", config_dir=cfg.config_dir),
        )
        warn_if_misconfigured(cfg.config_dir, slug="bot-squad", log=log)

    if mode == "coordinator":
        sock_path = cfg.sock_path
    else:
        linux_user = getpass.getuser()
        sock_path = _user_sock_path(cfg, linux_user)
        log.info("user-worker for linux_user=%s", linux_user)

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    sched = None
    if mode == "coordinator" and not degraded:
        sched = build_scheduler(cfg)
        sched.start()
        set_scheduler(sched)
        log.info("scheduler started")
    elif mode == "coordinator" and degraded:
        # Don't run scheduled jobs against a broken config; they'd just throw
        # on every tick. Socket still binds so the error is observable.
        log.warning("scheduler NOT started (degraded config)")

    app = build_app()

    def _shutdown(*_: object) -> None:
        log.info("shutdown signal received")
        if sched is not None:
            sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if mode == "coordinator" and sched is not None:
        def _fix_sock_perms() -> None:
            """Run shortly after uvicorn binds; tighten socket perms to 0660 + group www."""
            import grp, os
            if not sock_path.exists():
                return
            try:
                gid = grp.getgrnam("www").gr_gid
                os.chown(sock_path, -1, gid)
            except (KeyError, PermissionError, OSError):
                log.warning("could not chgrp socket to www; falling back to current group")
            sock_path.chmod(0o660)
            log.info("socket perms tightened to 0660 (group www)")

        from datetime import datetime, timedelta, timezone
        sched.add_job(
            _fix_sock_perms,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
            id="fix_sock_perms",
        )
    elif mode == "user-worker":
        _start_sock_chgrp(sock_path)

    log.info("uvicorn binding to %s", sock_path)
    uvicorn.run(app, uds=str(sock_path), log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
