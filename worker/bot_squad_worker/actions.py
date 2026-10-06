"""Worker action allowlist.

Every action is a typed function registered here. The dispatcher refuses
unknown action names and unexpected parameters. This is the security
boundary: even if the API is compromised, the attacker can only invoke
actions on this allowlist with their declared parameter shapes.

v1 ships `noop` (proof-of-life) and `tg_verify_login` (HMAC verification
proxied from the API — the bot token lives only in the worker post spec #3).
Spec #3 adds `tg_notify`.

Phase 2 multi-user: each action carries a mode tag — coordinator_only,
tmux_only, or both. The dispatcher checks the running worker's mode
(set by __main__ from BOT_SQUAD_MODE env) before invoking.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable


class ActionError(Exception):
    """Raised when an action call is invalid (unknown name, bad params)."""


class DegradedError(Exception):
    """Raised by dispatch when the worker started with a broken/incomplete
    config. The socket is bound, but normal actions cannot run because there
    is no usable Config. Carries the human-readable config-load detail so the
    caller sees *why* (which file, which missing keys). The server maps this
    to HTTP 503."""


# ---------------------------------------------------------------------------
# Worker mode — set by __main__.py at startup from BOT_SQUAD_MODE env.
# "coordinator": runs scheduler + all actions (single-host).
# "user-worker": tmux ops only; coordinator actions return ActionError.
# None (unset): legacy single-process — behaves like coordinator.
# ---------------------------------------------------------------------------

_MODE: str | None = None  # one of {"coordinator", "user-worker", None}


def set_mode(mode: str | None) -> None:
    """Called once at startup; tests can call directly to flip modes."""
    global _MODE
    _MODE = mode


def get_mode() -> str:
    """Return effective mode — unset is treated as coordinator (back-compat)."""
    return _MODE or "coordinator"


# ---------------------------------------------------------------------------
# Config accessor — injected at startup by __main__.py via set_config().
# Tests override it via monkeypatch.setattr(A, "_get_config", lambda: cfg).
# ---------------------------------------------------------------------------

_CONFIG: Any = None  # will be set to a Config instance
_CONFIG_LAST_RELOAD: float = 0.0
_CONFIG_RELOAD_TTL_SEC: float = 5.0

# Set by __main__ when Config.load() fails at startup. When non-empty, the
# worker is "degraded": the socket is bound and /health answers, but normal
# actions return a clear error instead of the daemon crash-looping. The string
# is the human-readable config-load detail (which file, which missing keys).
_DEGRADED_REASON: str = ""


def set_config(cfg: Any) -> None:
    """Called once at startup (and in integration tests) to inject the live config."""
    global _CONFIG, _CONFIG_LAST_RELOAD, _DEGRADED_REASON
    _CONFIG = cfg
    _DEGRADED_REASON = ""  # a successful config clears any degraded flag
    import time
    _CONFIG_LAST_RELOAD = time.monotonic()


def set_degraded(reason: str) -> None:
    """Mark (or, with an empty string, clear) degraded mode. Called by
    __main__ with the config-load detail when Config.load() fails at startup,
    so dispatch() refuses actions with a clear DegradedError and /health
    reports ok:false with this reason."""
    global _DEGRADED_REASON
    _DEGRADED_REASON = reason


def get_degraded() -> str:
    """Return the degraded reason, or "" if the worker is healthy."""
    return _DEGRADED_REASON


def _get_config() -> Any:
    """Return the current config, transparently re-loading from disk
    when stale.

    Bot-swarm: the dashboard's create-project flow writes a new entry
    into projects.toml at runtime, and the worker needs to see it
    without an operator restart. We Config.load() on a short TTL
    (default 5s) and fall back to the cached object if the file is
    transiently unparseable (mid-write). Tests monkeypatch this whole
    function so the reload never runs there.
    """
    global _CONFIG, _CONFIG_LAST_RELOAD
    if _CONFIG is None:
        raise ActionError("worker config not initialised")
    import time
    now = time.monotonic()
    if (now - _CONFIG_LAST_RELOAD) >= _CONFIG_RELOAD_TTL_SEC:
        try:
            from bot_squad_worker.config import Config
            cfg_dir = getattr(_CONFIG, "config_dir", None)
            if cfg_dir is not None:
                _CONFIG = Config.load(cfg_dir)
        except Exception:
            # Fall back to cached. Don't crash the worker because
            # someone is mid-edit on projects.toml.
            pass
        _CONFIG_LAST_RELOAD = now
    return _CONFIG


# ---------------------------------------------------------------------------
# Scheduler singleton — injected at startup by __main__.py via set_scheduler().
# Tests can monkeypatch _SCHED with a stub BackgroundScheduler.
# ---------------------------------------------------------------------------

_SCHED: Any = None  # BackgroundScheduler | None


def set_scheduler(sched: Any) -> None:
    """Called once at startup after the scheduler is created."""
    global _SCHED
    _SCHED = sched


# ---------------------------------------------------------------------------
# TgClient singleton — created lazily on first use.
# Tests replace _TG or monkeypatch _get_tg_client directly.
# ---------------------------------------------------------------------------

_TG: Any = None  # TgClient | None


def _get_tg_client(cfg: Any) -> Any:
    """Return the module-level TgClient, creating it on first call."""
    global _TG
    if _TG is None:
        from bot_squad_worker.tg import TgClient
        _TG = TgClient(cfg)
    return _TG


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_noop(params: dict[str, Any]) -> dict[str, Any]:
    """Proof-of-life: takes no params, returns {ok, ts}."""
    if params:
        raise ActionError(f"noop takes no params, got: {sorted(params)}")
    return {"ok": True, "ts": time.time()}


def _action_tg_verify_login(params: dict[str, Any]) -> dict[str, Any]:
    """Verify a Telegram Login Widget payload using the bot token from secrets.toml.

    Expected params: {"payload": <tg login dict>}
    Returns: {"ok": true, "user": {...}} or {"ok": false, "error": "..."}
    """
    extra = set(params) - {"payload"}
    if extra:
        raise ActionError(f"tg_verify_login got unexpected params: {sorted(extra)}")
    if "payload" not in params:
        raise ActionError("tg_verify_login missing required param: payload")

    cfg = _get_config()
    if not cfg.tg_bot_token:
        raise ActionError("tg_verify_login: bot token not configured")

    try:
        from bot_squad_worker.auth import verify_tg_login
        user = verify_tg_login(params["payload"], cfg.tg_bot_token, cfg.tg_auth_age_max)
        return {"ok": True, "user": user}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_TG_NOTIFY_ALLOWED = {"slug", "chat_id", "message", "sid", "user", "urgent"}


def _action_tg_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram message, with optional SID prefix and debounce.

    Params (all optional except ``message``):
        message  : str  — required; the text to send
        chat_id  : str  — explicit chat; takes precedence over slug
        slug     : str  — project slug; resolved to tg_chat in projects.toml
        sid      : str  — SID prefix component  (e.g. "S-almdudleer-claude-p5")
        user     : str  — user prefix component

    If neither ``chat_id`` nor ``slug`` is given, falls back to the first
    project's tg_chat (there is usually only one project).  Unknown slug
    raises ActionError.

    Returns {ok: true, sent: <bool>}.
    """
    extra = set(params) - _TG_NOTIFY_ALLOWED
    if extra:
        raise ActionError(f"tg_notify got unexpected params: {sorted(extra)}")
    if "message" not in params:
        raise ActionError("tg_notify missing required param: message")

    cfg = _get_config()

    # --- resolve chat_id ---
    chat_id: str | None = params.get("chat_id") or None
    if not chat_id:
        slug: str = params.get("slug") or ""
        if slug:
            project = cfg.projects.get(slug)
            if project is None:
                raise ActionError(f"tg_notify: unknown project slug {slug!r}")
            chat_id = project.tg_chat
        else:
            # Fallback: first registered project's chat (single-project setups)
            if cfg.projects:
                chat_id = next(iter(cfg.projects.values())).tg_chat
            else:
                raise ActionError("tg_notify: no chat_id, no slug, and no projects configured")

    tg = _get_tg_client(cfg)
    sent = tg.send(
        chat_id=chat_id,
        text=params["message"],
        sid=params.get("sid", ""),
        user=params.get("user", ""),
        urgent=bool(params.get("urgent", False)),
    )
    return {"ok": True, "sent": sent}


_DEPLOY_REQUIRED = {"slug", "target", "reason", "requested_by"}
_DEPLOY_ALLOWED = _DEPLOY_REQUIRED


def _action_deploy(params: dict[str, Any]) -> dict[str, Any]:
    """Queue a deploy request for a registered project.

    Required params: slug, target, reason, requested_by
    Returns: {ok: true, queue_id: str, queued_at: float}

    Raises ActionError on unknown slug, unknown target, extra/missing params.
    """
    extra = set(params) - _DEPLOY_ALLOWED
    if extra:
        raise ActionError(f"deploy got unexpected params: {sorted(extra)}")
    missing = _DEPLOY_REQUIRED - set(params)
    if missing:
        raise ActionError(f"deploy missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    target = params["target"]

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"deploy: unknown project slug {slug!r}")

    from bot_squad_worker import deploy as _deploy
    try:
        queue_id = _deploy.enqueue(
            cfg,
            slug=slug,
            target=target,
            reason=params["reason"],
            requested_by=params["requested_by"],
        )
    except ValueError as e:
        raise ActionError(f"deploy: {e}") from e

    import time as _time
    return {"ok": True, "queue_id": queue_id, "queued_at": _time.time()}


# ---------------------------------------------------------------------------
# Session management actions (spec #5)
# ---------------------------------------------------------------------------

_LIST_SESSIONS_REQUIRED = {"slug"}
_LIST_SESSIONS_ALLOWED = _LIST_SESSIONS_REQUIRED


def _action_list_sessions(params: dict[str, Any]) -> dict[str, Any]:
    """List all Claude sessions for a project (active + paused).

    Required params: slug
    Returns: [{sid, status, window, cwd, started_at, last_prompt_at, claude_uuid, linked_tasks}]
    """
    extra = set(params) - _LIST_SESSIONS_ALLOWED
    if extra:
        raise ActionError(f"list_sessions got unexpected params: {sorted(extra)}")
    missing = _LIST_SESSIONS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"list_sessions missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    # Wrap in a dict so the FastAPI response (typed `-> dict`) validates.
    return {"sessions": _sessions.list_sessions(cfg, params["slug"])}


_PAUSE_SESSION_REQUIRED = {"slug", "sid"}
_PAUSE_SESSION_ALLOWED = _PAUSE_SESSION_REQUIRED


def _action_pause_session(params: dict[str, Any]) -> dict[str, Any]:
    """Pause a running Claude session.

    Required params: slug, sid
    Returns: {ok: true, paused: true}
    """
    extra = set(params) - _PAUSE_SESSION_ALLOWED
    if extra:
        raise ActionError(f"pause_session got unexpected params: {sorted(extra)}")
    missing = _PAUSE_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"pause_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.pause(cfg, params["slug"], params["sid"])


_SUSPEND_SESSION_REQUIRED = {"slug", "sid"}
_SUSPEND_SESSION_ALLOWED = _SUSPEND_SESSION_REQUIRED


def _action_suspend_session(params: dict[str, Any]) -> dict[str, Any]:
    """Suspend a Claude session — close the pane to free resources.

    Required params: slug, sid
    Returns: {ok: true, suspended: true}
    The registry md is preserved so resume() can resurrect via --resume.
    """
    extra = set(params) - _SUSPEND_SESSION_ALLOWED
    if extra:
        raise ActionError(f"suspend_session got unexpected params: {sorted(extra)}")
    missing = _SUSPEND_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"suspend_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.suspend(cfg, params["slug"], params["sid"])


_RESUME_SESSION_REQUIRED = {"slug", "sid"}
_RESUME_SESSION_ALLOWED = _RESUME_SESSION_REQUIRED


def _action_resume_session(params: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused Claude session.

    Required params: slug, sid
    Returns: {ok: true, sid: <new_sid>}
    """
    extra = set(params) - _RESUME_SESSION_ALLOWED
    if extra:
        raise ActionError(f"resume_session got unexpected params: {sorted(extra)}")
    missing = _RESUME_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"resume_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.resume(cfg, params["slug"], params["sid"])


_SPAWN_SESSION_REQUIRED = {"slug", "window"}
# CAP-1 (multi-CLI runtime): `model` and `runtime` are additive optional params.
# `runtime` ∈ {claude, codex} selects which CLI the pane launches; `model` is the
# per-session model id applied to that CLI. Absent → today's behaviour (claude,
# host-default model). mcp_loops only sends them under its passthrough gate, so a
# daemon carrying this change stays byte-for-byte compatible with an un-gated caller.
_SPAWN_SESSION_ALLOWED = _SPAWN_SESSION_REQUIRED | {
    "initial_prompt", "task_id", "initiative", "owner", "role", "model", "runtime",
    # loops_data_dir (mcp_loops P1c): the LOOPS_DATA_DIR the loops server resolved,
    # exported into the spawned pane so the agent's end-of-turn report lands in the
    # SAME data dir the server tails — closes the fresh-install split-brain.
    "loops_data_dir",
    # workspace (mcp_loops loop-workspaces P1): an explicit, engine-provisioned
    # ephemeral worktree used as the session cwd, DETACHED from the slug's
    # projects.toml repo_path. Absent ⇒ legacy slug repo_path (byte-for-byte).
    "workspace",
    # role_caps (role-caps): the engine's per-role --settings profile (a dict)
    # for a manager / input_provider step ⇒ dontAsk + settings instead of
    # bypass. Absent ⇒ today's command byte-for-byte.
    "role_caps",
    # sandbox (loop cage): the engine's mcp_loops.sandbox Policy spec ⇒ the pane
    # runs inside the loop slice + exec stage. Absent ⇒ today's command.
    "sandbox"}


def _action_spawn_session(params: dict[str, Any]) -> dict[str, Any]:
    """Spawn a new coding-CLI session in the project's repo.

    Required params: slug, window
    Optional params: initial_prompt, task_id, initiative, owner, role, model,
        runtime, loops_data_dir, workspace, role_caps, sandbox
    Returns: {ok: true, sid: <new_sid>}
    """
    extra = set(params) - _SPAWN_SESSION_ALLOWED
    if extra:
        raise ActionError(f"spawn_session got unexpected params: {sorted(extra)}")
    missing = _SPAWN_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"spawn_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.spawn(
        cfg,
        params["slug"],
        params["window"],
        params.get("initial_prompt"),
        task_id=params.get("task_id"),
        initiative=params.get("initiative"),
        owner=params.get("owner"),
        role=params.get("role"),
        model=params.get("model"),
        runtime=params.get("runtime"),
        loops_data_dir=params.get("loops_data_dir"),
        workspace=params.get("workspace"),
        role_caps=params.get("role_caps"),
        sandbox=params.get("sandbox"),
    )


# ---------------------------------------------------------------------------
# Bot-swarm: sleep cycle
# ---------------------------------------------------------------------------

_SLEEP_EXPERT_REQUIRED = {"slug", "sid"}
_SLEEP_EXPERT_ALLOWED = _SLEEP_EXPERT_REQUIRED


def _action_sleep_expert(params: dict[str, Any]) -> dict[str, Any]:
    """Compact a swarm expert's recent transcript into its memory files.

    Required params: slug, sid.
    Returns: {ok, role, compacted_bytes, mark_before, mark_after,
              scribe_stdout_tail}.
    """
    extra = set(params) - _SLEEP_EXPERT_ALLOWED
    if extra:
        raise ActionError(f"sleep_expert got unexpected params: {sorted(extra)}")
    missing = _SLEEP_EXPERT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sleep_expert missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import sleep as _sleep
    return _sleep.sleep_expert(cfg, params["slug"], params["sid"])


# Read-only swarm state surface used by the API/UI.

def _action_list_initiatives(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug"}
    if extra:
        raise ActionError(f"list_initiatives got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("list_initiatives requires slug")
    cfg = _get_config()
    from bot_squad_worker import swarm_state as _swarm
    return {"ok": True, "initiatives": _swarm.list_initiatives(cfg, params["slug"])}


def _action_read_initiative(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug", "id"}
    if extra:
        raise ActionError(f"read_initiative got unexpected params: {sorted(extra)}")
    missing = {"slug", "id"} - set(params)
    if missing:
        raise ActionError(f"read_initiative missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import swarm_state as _swarm
    return {"ok": True, **_swarm.read_initiative(cfg, params["slug"], params["id"])}


def _action_list_tasks(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug"}
    if extra:
        raise ActionError(f"list_tasks got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("list_tasks requires slug")
    cfg = _get_config()
    from bot_squad_worker import swarm_state as _swarm
    return {"ok": True, "tasks": _swarm.list_tasks(cfg, params["slug"])}


def _action_read_task(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug", "id"}
    if extra:
        raise ActionError(f"read_task got unexpected params: {sorted(extra)}")
    missing = {"slug", "id"} - set(params)
    if missing:
        raise ActionError(f"read_task missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import swarm_state as _swarm
    return {"ok": True, **_swarm.read_task(cfg, params["slug"], params["id"])}


def _action_list_experts(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug"}
    if extra:
        raise ActionError(f"list_experts got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("list_experts requires slug")
    cfg = _get_config()
    from bot_squad_worker import swarm_state as _swarm
    return {"ok": True, "experts": _swarm.list_experts(cfg, params["slug"])}


def _action_bootstrap_project_repo(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug"}
    if extra:
        raise ActionError(f"bootstrap_project_repo got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("bootstrap_project_repo requires slug")
    cfg = _get_config()
    from bot_squad_worker import bootstrap as _bootstrap
    return _bootstrap.bootstrap_project_repo(cfg, params["slug"])


def _action_read_expert_memory(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug", "role", "filename"}
    if extra:
        raise ActionError(f"read_expert_memory got unexpected params: {sorted(extra)}")
    missing = {"slug", "role", "filename"} - set(params)
    if missing:
        raise ActionError(f"read_expert_memory missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import swarm_state as _swarm
    return {"ok": True, **_swarm.read_expert_memory(
        cfg, params["slug"], params["role"], params["filename"],
    )}


def _action_get_strategy(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug"}
    if extra:
        raise ActionError(f"get_strategy got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("get_strategy requires slug")
    cfg = _get_config()
    from bot_squad_worker import swarm_strategy as _strategy
    return {"ok": True, "strategy": _strategy.read_strategy(cfg, params["slug"])}


def _action_set_strategy(params: dict[str, Any]) -> dict[str, Any]:
    extra = set(params) - {"slug", "patch"}
    if extra:
        raise ActionError(f"set_strategy got unexpected params: {sorted(extra)}")
    missing = {"slug", "patch"} - set(params)
    if missing:
        raise ActionError(f"set_strategy missing required params: {sorted(missing)}")
    if not isinstance(params["patch"], dict):
        raise ActionError("set_strategy: patch must be an object")
    cfg = _get_config()
    from bot_squad_worker import swarm_strategy as _strategy
    return {"ok": True, "strategy": _strategy.write_strategy(cfg, params["slug"], params["patch"])}


_SLEEP_AND_RESPAWN_REQUIRED = {"slug", "sid"}
_SLEEP_AND_RESPAWN_ALLOWED = _SLEEP_AND_RESPAWN_REQUIRED


def _action_sleep_and_respawn(params: dict[str, Any]) -> dict[str, Any]:
    """Sleep, then immediately respawn — the canonical "compact and
    reset" cycle. If sleep returns ``compacted_bytes == 0`` (nothing
    new), still respawns; if you only want the respawn, call
    respawn_expert directly.

    Required params: slug, sid.
    Returns: {ok, role, compacted_bytes, old_sid, new_sid, drained_lines}.
    """
    extra = set(params) - _SLEEP_AND_RESPAWN_ALLOWED
    if extra:
        raise ActionError(f"sleep_and_respawn got unexpected params: {sorted(extra)}")
    missing = _SLEEP_AND_RESPAWN_REQUIRED - set(params)
    if missing:
        raise ActionError(f"sleep_and_respawn missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import sleep as _sleep
    from bot_squad_worker import sessions as _sessions
    sleep_result = _sleep.sleep_expert(cfg, params["slug"], params["sid"])
    respawn_result = _sessions.respawn_expert(cfg, params["slug"], params["sid"])
    return {
        "ok": True,
        "role": sleep_result["role"],
        "compacted_bytes": sleep_result["compacted_bytes"],
        "old_sid": respawn_result["old_sid"],
        "new_sid": respawn_result["new_sid"],
        "drained_lines": respawn_result["drained_lines"],
        "window": respawn_result["window"],
    }


_RESPAWN_EXPERT_REQUIRED = {"slug", "sid"}
_RESPAWN_EXPERT_ALLOWED = _RESPAWN_EXPERT_REQUIRED


def _action_respawn_expert(params: dict[str, Any]) -> dict[str, Any]:
    """Kill a swarm expert pane and spawn a fresh one with the same role,
    preserving any unread mail via the holding inbox. Closes the sleep
    loop: after ``sleep_expert`` writes memory, ``respawn_expert``
    restarts the pane so the fresh session loads the updated memory.

    Required params: slug, sid.
    Returns: {ok, old_sid, new_sid, role, window, drained_lines}.
    """
    extra = set(params) - _RESPAWN_EXPERT_ALLOWED
    if extra:
        raise ActionError(f"respawn_expert got unexpected params: {sorted(extra)}")
    missing = _RESPAWN_EXPERT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"respawn_expert missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.respawn_expert(cfg, params["slug"], params["sid"])


# ---------------------------------------------------------------------------
# Scheduler state action (spec #6)
# ---------------------------------------------------------------------------

def _action_scheduler_state(params: dict[str, Any]) -> dict[str, Any]:
    """Return current APScheduler state (jobs, uptime, heartbeat age).

    Takes no params. Returns {jobs, worker_started_at, last_heartbeat_age_seconds}.
    """
    if params:
        raise ActionError(f"scheduler_state takes no params, got: {sorted(params)}")

    cfg = _get_config()
    if _SCHED is None:
        raise ActionError("scheduler not initialised")

    from bot_squad_worker.scheduler import state_for_api
    return state_for_api(_SCHED, cfg)


# ---------------------------------------------------------------------------
# inject_input action (spec #7)
# ---------------------------------------------------------------------------

_INJECT_INPUT_REQUIRED = {"sid", "text"}
_INJECT_INPUT_ALLOWED = _INJECT_INPUT_REQUIRED | {"wait_for_idle", "idle_timeout_sec"}


def _pane_appears_idle(pane_id: str) -> bool:
    """T-0005: heuristic idleness check. Capture the last 5 lines of the pane;
    sleep a moment; capture again. If identical, the pane isn't actively
    rendering output. Not perfect (cursor blink can shift bytes; some Claude
    spinners pulse), but good enough to avoid clobbering an in-flight tool
    call most of the time.
    """
    import subprocess
    import time as _time

    def _cap():
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-5"],
                check=False, capture_output=True, timeout=2,
            )
            return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    first = _cap()
    if first is None:
        return False
    _time.sleep(0.5)
    second = _cap()
    return first == second and first is not None


def _resolve_pane_for_sid(sid: str):
    """Return the live tmux PaneInfo whose computed SID matches ``sid``, or
    None if no pane matches (expert suspended/respawned/zombie). Shared by
    inject_input and the S1 nudge-reconcile job.
    """
    from bot_squad_worker import sessions as S
    panes = S.list_panes()
    user = S._get_current_user()
    pane = next(
        (p for p in panes if S.compute_sid(user, p.window, p.pane_id) == sid),
        None,
    )
    # A pane that merely REUSES the sid string (tmux restart reset the pane ids)
    # is not this session: answer "no live pane" so the engine respawns, rather
    # than typing into another loop's agent.
    if pane is not None:
        try:
            cfg = _get_config()
        except ActionError:
            return pane          # no config (bare unit use): legacy match
        if not S.sid_binding_matches(cfg, sid, pane):
            return None
    return pane


def _action_inject_input(params: dict[str, Any]) -> dict[str, Any]:
    """Send text to the tmux pane for a SID (one Enter per line).

    Required params: sid, text
    Optional params:
        wait_for_idle (bool, default False): if True, poll the pane's tail
            for stable output before injecting. Avoids interrupting an
            in-flight Claude tool call. Falls back to immediate inject if
            the pane stays busy past `idle_timeout_sec`.
        idle_timeout_sec (number, default 30): how long to wait for idle.
            Effective only when wait_for_idle is True.
    Returns: {ok: true, pane_id, lines_sent: int, waited_for_idle_sec: float}
    """
    extra = set(params) - _INJECT_INPUT_ALLOWED
    if extra:
        raise ActionError(f"inject_input got unexpected params: {sorted(extra)}")
    missing = _INJECT_INPUT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"inject_input missing required params: {sorted(missing)}")

    sid = params["sid"]
    text = params["text"]
    if not text.strip():
        raise ActionError("inject_input: empty text")

    pane = _resolve_pane_for_sid(sid)
    if pane is None:
        raise ActionError(f"inject_input: no live pane for sid {sid!r}")

    # T-0005: optional idle wait
    waited = 0.0
    if params.get("wait_for_idle"):
        import time as _time
        timeout = float(params.get("idle_timeout_sec", 30))
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if _pane_appears_idle(pane.pane_id):
                break
            _time.sleep(1.0)
        waited = round(timeout - max(0.0, deadline - _time.monotonic()), 2)

    # Submit via the SAME settle-and-verify path as spawn (sessions._deliver_prompt):
    # send the whole text as one bracketed paste, wait for it to SETTLE, then Enter,
    # verifying a turn actually started (retry Enter if swallowed). A fixed
    # sleep-then-Enter races the paste under load and silently drops the turn — the
    # bug that wedged parallel loops. wait_ready=False: the pane is already a live REPL.
    from bot_squad_worker import sessions as _sessions
    # CAP-1: use the session's recorded runtime so turn-2+ delivery verifies the
    # right per-CLI "running" markers (codex vs claude). Defaults to claude.
    runtime = _sessions.runtime_for_sid(_get_config(), sid)
    submitted = _sessions._deliver_prompt(pane.pane_id, text, wait_ready=False,
                                          runtime=runtime)
    return {
        "ok": True,
        "pane_id": pane.pane_id,
        "lines_sent": len(text.split("\n")),
        "submitted": submitted,
        "waited_for_idle_sec": waited,
    }


_CAPTURE_PANE_REQUIRED = {"sid"}
_CAPTURE_PANE_ALLOWED = _CAPTURE_PANE_REQUIRED | {"max_lines"}


def _action_capture_pane(params: dict[str, Any]) -> dict[str, Any]:
    """Snapshot a session's pane scrollback (Q2 observability: persist agent
    transcripts). The loop substrate calls this at the end of every turn — INCLUDING
    a wedged/timed-out turn, whose pane is still alive — so the raw agent output is
    inspectable on disk without spelunking the 10MB engine log.

    Required params: sid
    Optional params:
        max_lines (int, default 5000): how many lines of scrollback history to
            pull (``tmux capture-pane -S -<max_lines>``). Capped to keep the
            capture bounded regardless of caller input.
    Returns: {ok, sid, pane_id, text, lines, missing?}
        A vanished pane (session suspended/zombie) is NOT an error — it returns
        ok:true, missing:true, text:"" so the caller can still persist a stub
        rather than fail the turn on a best-effort capture.
    """
    extra = set(params) - _CAPTURE_PANE_ALLOWED
    if extra:
        raise ActionError(f"capture_pane got unexpected params: {sorted(extra)}")
    missing = _CAPTURE_PANE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"capture_pane missing required params: {sorted(missing)}")

    sid = params["sid"]
    try:
        max_lines = int(params.get("max_lines", 5000))
    except (TypeError, ValueError):
        raise ActionError("capture_pane: max_lines must be an integer")
    max_lines = max(1, min(max_lines, 50000))

    pane = _resolve_pane_for_sid(sid)
    if pane is None:
        return {"ok": True, "sid": sid, "pane_id": None,
                "text": "", "lines": 0, "missing": True}

    from bot_squad_worker import sessions as _sessions
    # Full-history scrollback (-S -N), not just the visible screen, so a long
    # transcript survives; joined with a trailing newline like the terminal shows.
    res = _sessions._run(
        ["tmux", "capture-pane", "-p", "-t", pane.pane_id, "-S", f"-{max_lines}"])
    text = res.stdout if res.returncode == 0 else ""
    return {
        "ok": True, "sid": sid, "pane_id": pane.pane_id,
        "text": text, "lines": text.count("\n"),
    }


# ---------------------------------------------------------------------------
# Autonomous orchestrator actions (spec #8)
# ---------------------------------------------------------------------------

_AUTO_STATUS_ALLOWED = {"slug"}


def _action_autonomous_status(params: dict[str, Any]) -> dict[str, Any]:
    """Return the current autonomous orchestrator state for a project.

    Required params: slug
    Returns: {enabled, status, current_task_id, current_pane_id, last_tick_at,
              sleep_start_hour, sleep_end_hour, tick_log (last 5)}
    """
    extra = set(params) - _AUTO_STATUS_ALLOWED
    if extra:
        raise ActionError(f"autonomous_status got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autonomous_status missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autonomous_status: unknown project slug {slug!r}")

    from bot_squad_worker import autonomous as _auto
    state = _auto.load_state(cfg, slug)
    from dataclasses import asdict
    d = asdict(state)
    # Return last 5 tick log entries in status (full log via log endpoint)
    d["tick_log"] = state.tick_log[-5:]
    return {"ok": True, **d}


_AUTO_ENABLE_ALLOWED = {"slug", "sleep_start_hour", "sleep_end_hour"}


def _action_autonomous_enable(params: dict[str, Any]) -> dict[str, Any]:
    """Enable the autonomous orchestrator for a project.

    Required params: slug
    Optional params: sleep_start_hour (int, default 22), sleep_end_hour (int, default 8)
    Returns: {ok: true, slug, enabled: true}
    """
    extra = set(params) - _AUTO_ENABLE_ALLOWED
    if extra:
        raise ActionError(f"autonomous_enable got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autonomous_enable missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autonomous_enable: unknown project slug {slug!r}")

    from bot_squad_worker import autonomous as _auto
    state = _auto.load_state(cfg, slug)
    state.enabled = True
    if "sleep_start_hour" in params:
        state.sleep_start_hour = int(params["sleep_start_hour"])
    if "sleep_end_hour" in params:
        state.sleep_end_hour = int(params["sleep_end_hour"])
    _auto.save_state(cfg, state)
    return {"ok": True, "slug": slug, "enabled": True}


_AUTO_DISABLE_ALLOWED = {"slug"}


def _action_autonomous_disable(params: dict[str, Any]) -> dict[str, Any]:
    """Disable the autonomous orchestrator for a project.

    Required params: slug
    Returns: {ok: true, slug, enabled: false}

    In-flight tasks complete normally; the orchestrator won't start new ones.
    """
    extra = set(params) - _AUTO_DISABLE_ALLOWED
    if extra:
        raise ActionError(f"autonomous_disable got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("autonomous_disable missing required param: slug")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autonomous_disable: unknown project slug {slug!r}")

    from bot_squad_worker import autonomous as _auto
    state = _auto.load_state(cfg, slug)
    state.enabled = False
    _auto.save_state(cfg, state)
    return {"ok": True, "slug": slug, "enabled": False}


# ---------------------------------------------------------------------------
# Cross-session message bus actions (Phase 1)
# ---------------------------------------------------------------------------

_STATUS_SUMMARY_REQUIRED = {"slug"}
_STATUS_SUMMARY_ALLOWED = _STATUS_SUMMARY_REQUIRED


def _action_status_summary(params: dict[str, Any]) -> dict[str, Any]:
    """Compact swarm-state summary for a coord dashboard (T-0009).

    Required params: slug
    Returns: {
        ok, slug,
        active_sessions: [{sid, role, last_prompt_at, unread_count, halted}],
        suspended_sessions_count,
        archived_sessions_count,
        inbox_total_unread,
        services: {worker_socket, tg_poller, tg_bridge},
    }

    Service detection uses pgrep on common process names (portable across
    Mac + Linux). The worker_socket is always True if this action is
    answering — by definition.
    """
    extra = set(params) - _STATUS_SUMMARY_ALLOWED
    if extra:
        raise ActionError(f"status_summary got unexpected params: {sorted(extra)}")
    missing = _STATUS_SUMMARY_REQUIRED - set(params)
    if missing:
        raise ActionError(f"status_summary missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    from bot_squad_worker import sessions as _sessions
    from pathlib import Path
    import subprocess

    all_rows = _sessions.list_sessions(cfg, slug)
    active = [r for r in all_rows if r["status"] == "active"]
    suspended = [r for r in all_rows if r["status"] == "suspended"]
    archived = [r for r in all_rows if r.get("archived")]

    # Per-active-session unread count (cheap: read offset → end-of-file, count \n).
    chat_dir = Path(cfg.data_dir) / slug / "_chat"
    total_unread = 0
    active_summary = []
    for r in active:
        sid = r["sid"]
        inbox = chat_dir / f"inbox-{sid}.log"
        seen = chat_dir / f"seen-{sid}"
        unread_count = 0
        try:
            if inbox.exists():
                size = inbox.stat().st_size
                try:
                    offset = int(seen.read_text().strip()) if seen.exists() else 0
                except (FileNotFoundError, ValueError):
                    offset = 0
                if size > offset:
                    with inbox.open("rb") as f:
                        f.seek(offset)
                        unread_count = f.read(size - offset).count(b"\n")
        except OSError:
            unread_count = 0
        total_unread += unread_count
        active_summary.append({
            "sid": sid,
            "role": r.get("role"),
            "last_prompt_at": r.get("last_prompt_at"),
            "unread_count": unread_count,
            "halted": r.get("halted", False),
        })

    def _proc_running(pattern: str) -> bool:
        try:
            subprocess.run(
                ["pgrep", "-f", pattern],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False

    services = {
        "worker_socket": True,  # we ARE answering
        "tg_poller": _proc_running("tg_inbox.mjs"),
        "tg_bridge": _proc_running("bridge.py"),
    }

    return {
        "ok": True,
        "slug": slug,
        "active_sessions": active_summary,
        "suspended_sessions_count": len(suspended),
        "archived_sessions_count": len(archived),
        "inbox_total_unread": total_unread,
        "services": services,
    }


_SET_HALT_REQUIRED = {"slug", "sid"}
_SET_HALT_ALLOWED = _SET_HALT_REQUIRED | {"halted"}


def _action_set_halt(params: dict[str, Any]) -> dict[str, Any]:
    """Set/clear the halt flag on a session (T-0006).

    Required params: slug, sid
    Optional params: halted (bool, default True). Pass `halted: false` to clear.

    Halt is advisory: peer_send still delivers to halted sessions, but the
    response gains `halted: {sid: true}` so coord knows the recipient isn't
    expected to act until the halt clears. Use after a phase-gated task
    completion to enforce a review pause.

    Returns: {ok, sid, halted, changed}
    """
    extra = set(params) - _SET_HALT_ALLOWED
    if extra:
        raise ActionError(f"set_halt got unexpected params: {sorted(extra)}")
    missing = _SET_HALT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"set_halt missing required params: {sorted(missing)}")
    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.set_halt(
        cfg, params["slug"], params["sid"], bool(params.get("halted", True)),
    )


_PEER_SEND_REQUIRED = {"slug", "from_sid", "to", "text"}
_PEER_SEND_ALLOWED = _PEER_SEND_REQUIRED | {"nudge"}


def _action_peer_send(params: dict[str, Any]) -> dict[str, Any]:
    """Append a message to recipient inbox(es) and (best-effort) wake each
    recipient's tmux pane so they actually notice.

    Required params: slug, from_sid, to, text
        `to` accepts either:
          - a single SID or role keyword string ("S-...-coord-p17" or "planner")
          - a list of mixed SIDs and/or role keywords (T-0004: batch mode)
    Optional params: nudge (bool, default True) — when False, skip the pane
        wake. The recipient still gets the inbox line; they just have to
        peer_inbox_read on their own cadence.

    Returns: {
        ok: true,
        delivered_to: [sid, ...],          # who got the inbox line (deduped)
        nudged: {sid: bool, ...},           # which panes were successfully woken
    }
    """
    extra = set(params) - _PEER_SEND_ALLOWED
    if extra:
        raise ActionError(f"peer_send got unexpected params: {sorted(extra)}")
    missing = _PEER_SEND_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_send missing required params: {sorted(missing)}")

    cfg = _get_config()
    from_sid = params["from_sid"]
    raw_to = params["to"]
    # T-0004: normalize to a list. Singletons keep current behavior.
    to_list: list[str]
    if isinstance(raw_to, list):
        to_list = [str(t) for t in raw_to]
    elif isinstance(raw_to, str):
        to_list = [raw_to]
    else:
        raise ActionError(f"peer_send: 'to' must be string or list, got {type(raw_to).__name__}")

    from bot_squad_worker import intersession as _is
    all_delivered: list[str] = []
    seen: set[str] = set()
    for one_to in to_list:
        try:
            r = _is.send(cfg, params["slug"], from_sid, one_to, params["text"])
        except _is.PeerSendTooLarge as e:
            raise ActionError(str(e)) from None
        for sid in r.get("delivered_to", []):
            if sid not in seen:
                seen.add(sid)
                all_delivered.append(sid)

    # T-0006: per-recipient halt flag so coord knows which targets are paused.
    from bot_squad_worker import sessions as _sessions
    halted_per: dict[str, bool] = {}
    for sid in all_delivered:
        try:
            halted_per[sid] = _sessions.session_is_halted(cfg, params["slug"], sid)
        except Exception:  # noqa: BLE001
            halted_per[sid] = False

    result: dict[str, Any] = {
        "ok": True,
        "delivered_to": all_delivered,
        "halted": halted_per,
    }

    nudged: dict[str, bool] = {}
    if params.get("nudge", True) and all_delivered:
        nudge_text = f"[swarm] new peer_send from {from_sid} — call peer_inbox_read to view"
        for sid in all_delivered:
            try:
                _action_inject_input({"sid": sid, "text": nudge_text})
                nudged[sid] = True
            except ActionError:
                nudged[sid] = False
            except Exception:  # noqa: BLE001
                nudged[sid] = False
    result["nudged"] = nudged
    return result


_PEER_INBOX_READ_REQUIRED = {"slug", "sid"}
_PEER_INBOX_READ_ALLOWED = _PEER_INBOX_READ_REQUIRED


def _action_peer_inbox_read(params: dict[str, Any]) -> dict[str, Any]:
    """Drain new inbox lines since last read.

    Required params: slug, sid
    Returns: {ok: true, messages: [line, ...], count: N}
    """
    extra = set(params) - _PEER_INBOX_READ_ALLOWED
    if extra:
        raise ActionError(f"peer_inbox_read got unexpected params: {sorted(extra)}")
    missing = _PEER_INBOX_READ_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_inbox_read missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import intersession as _is
    return _is.inbox_read(cfg, params["slug"], params["sid"])


_TASK_PROGRESS_REQUIRED = {"slug", "task_id", "sid", "text"}
_TASK_PROGRESS_ALLOWED = _TASK_PROGRESS_REQUIRED


def _action_task_progress_add(params: dict[str, Any]) -> dict[str, Any]:
    """Append a short progress note to a backlog task's `## Progress` section.

    Required params: slug, task_id, sid, text
    Returns: {ok: true, task_id, line_appended}

    The task md is updated atomically (tmp + rename). The verbatim and
    context sections are preserved exactly. Text is sanitised: newlines
    collapsed to spaces, capped at 240 chars.
    """
    extra = set(params) - _TASK_PROGRESS_ALLOWED
    if extra:
        raise ActionError(f"task_progress_add got unexpected params: {sorted(extra)}")
    missing = _TASK_PROGRESS_REQUIRED - set(params)
    if missing:
        raise ActionError(f"task_progress_add missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    task_id = params["task_id"]
    sid = params["sid"]
    text = params["text"]

    if not isinstance(text, str) or not text.strip():
        raise ActionError("task_progress_add: empty text")
    if cfg.projects.get(slug) is None:
        raise ActionError(f"task_progress_add: unknown project slug {slug!r}")

    backlog_dir: Path = cfg.data_dir / slug / "backlog"
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"task_progress_add: task not found: {task_id}")
    path = matches[0]

    from datetime import datetime, timezone
    import os as _os
    import re as _re
    from bot_squad_worker.task_body import append_progress, _sanitize_progress_text

    text_raw = path.read_text()
    fm_match = _re.match(r"\A---\n(.*?)\n---\n(.*)", text_raw, _re.DOTALL)
    if not fm_match:
        raise ActionError(f"task_progress_add: no frontmatter in {path}")
    fm_block = fm_match.group(1)
    body = fm_match.group(2).lstrip("\n")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        new_body = append_progress(body, ts, sid, text)
    except ValueError as e:
        raise ActionError(f"task_progress_add: {e}") from e

    # Update `updated:` in place (or append) without parsing YAML — the same
    # line-based pattern autonomous._patch_task_file uses.
    fm_lines = fm_block.splitlines()
    has_updated = False
    for i, ln in enumerate(fm_lines):
        if ln.lstrip().startswith("updated:"):
            fm_lines[i] = f"updated: {ts}"
            has_updated = True
            break
    if not has_updated:
        fm_lines.append(f"updated: {ts}")
    new_fm = "\n".join(fm_lines)

    content = f"---\n{new_fm}\n---\n\n{new_body}"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    _os.rename(tmp, path)

    line = f"- {ts} · {sid} · {_sanitize_progress_text(text)}"
    return {"ok": True, "task_id": task_id, "line_appended": line}


_PEER_INBOX_WAIT_REQUIRED = {"slug", "sid", "timeout"}
_PEER_INBOX_WAIT_ALLOWED = _PEER_INBOX_WAIT_REQUIRED


def _action_peer_inbox_wait(params: dict[str, Any]) -> dict[str, Any]:
    """Long-poll until inbox grows past the seen offset, or timeout.

    Required params: slug, sid, timeout (seconds, capped at 1800)
    Returns: {ok: true, ready: bool, elapsed_sec: float}
    """
    extra = set(params) - _PEER_INBOX_WAIT_ALLOWED
    if extra:
        raise ActionError(f"peer_inbox_wait got unexpected params: {sorted(extra)}")
    missing = _PEER_INBOX_WAIT_REQUIRED - set(params)
    if missing:
        raise ActionError(f"peer_inbox_wait missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import intersession as _is
    return _is.inbox_wait(cfg, params["slug"], params["sid"], params["timeout"])


# ---------------------------------------------------------------------------
# Phase 9: bind_task / bind_initiative — append to a session's extras
# ---------------------------------------------------------------------------

_BIND_TASK_REQUIRED = {"slug", "sid", "task_id"}
_BIND_TASK_ALLOWED = _BIND_TASK_REQUIRED


def _action_bind_task(params: dict[str, Any]) -> dict[str, Any]:
    """Bind another task to an already-running dev session.

    Required params: slug, sid, task_id
    Returns: {ok, sid, task_id, extras: [...full extra list...]}
    """
    extra = set(params) - _BIND_TASK_ALLOWED
    if extra:
        raise ActionError(f"bind_task got unexpected params: {sorted(extra)}")
    missing = _BIND_TASK_REQUIRED - set(params)
    if missing:
        raise ActionError(f"bind_task missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.bind_task(cfg, params["slug"], params["sid"], params["task_id"])


_BIND_INITIATIVE_REQUIRED = {"slug", "sid", "initiative"}
_BIND_INITIATIVE_ALLOWED = _BIND_INITIATIVE_REQUIRED


def _action_bind_initiative(params: dict[str, Any]) -> dict[str, Any]:
    """Bind another initiative to an already-running teamlead session.

    Required params: slug, sid, initiative
    Returns: {ok, sid, initiative, extras: [...full extra list...]}
    """
    extra = set(params) - _BIND_INITIATIVE_ALLOWED
    if extra:
        raise ActionError(f"bind_initiative got unexpected params: {sorted(extra)}")
    missing = _BIND_INITIATIVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"bind_initiative missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.bind_initiative(cfg, params["slug"], params["sid"], params["initiative"])


_UNBIND_TASK_REQUIRED = {"slug", "sid", "task_id"}
_UNBIND_TASK_ALLOWED = _UNBIND_TASK_REQUIRED


def _action_unbind_task(params: dict[str, Any]) -> dict[str, Any]:
    """Remove a task binding from a dev session's extras.

    Required params: slug, sid, task_id
    Returns: {ok, sid, task_id, extras, changed}
    """
    extra = set(params) - _UNBIND_TASK_ALLOWED
    if extra:
        raise ActionError(f"unbind_task got unexpected params: {sorted(extra)}")
    missing = _UNBIND_TASK_REQUIRED - set(params)
    if missing:
        raise ActionError(f"unbind_task missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.unbind_task(cfg, params["slug"], params["sid"], params["task_id"])


_UNBIND_INITIATIVE_REQUIRED = {"slug", "sid", "initiative"}
_UNBIND_INITIATIVE_ALLOWED = _UNBIND_INITIATIVE_REQUIRED


def _action_unbind_initiative(params: dict[str, Any]) -> dict[str, Any]:
    """Remove an initiative binding from a teamlead session.

    Required params: slug, sid, initiative
    Returns: {ok, sid, initiative, extras: [...], changed: bool}
    """
    extra = set(params) - _UNBIND_INITIATIVE_ALLOWED
    if extra:
        raise ActionError(f"unbind_initiative got unexpected params: {sorted(extra)}")
    missing = _UNBIND_INITIATIVE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"unbind_initiative missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.unbind_initiative(cfg, params["slug"], params["sid"], params["initiative"])


_ARCHIVE_SESSION_REQUIRED = {"slug", "sid"}
_ARCHIVE_SESSION_ALLOWED = _ARCHIVE_SESSION_REQUIRED


def _action_archive_session(params: dict[str, Any]) -> dict[str, Any]:
    """Archive a suspended session (set archived: true in its md).

    Required params: slug, sid
    Returns: {ok: true, sid, archived: true}
    """
    extra = set(params) - _ARCHIVE_SESSION_ALLOWED
    if extra:
        raise ActionError(f"archive_session got unexpected params: {sorted(extra)}")
    missing = _ARCHIVE_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"archive_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.archive_session(cfg, params["slug"], params["sid"])


_REGISTER_SESSION_ROLE_REQUIRED = {"slug", "sid", "role"}
_REGISTER_SESSION_ROLE_ALLOWED = _REGISTER_SESSION_ROLE_REQUIRED | {"loop"}


def _action_register_session_role(params: dict[str, Any]) -> dict[str, Any]:
    """Bind a role (+ loop) to an existing session so role-gated ops accept it —
    the engine's hook for an ADOPTED briefed manager (loopyard-bug-1790190791).

    Required params: slug, sid, role
    Optional params: loop
    Returns: {ok: true, sid, role, loop, changed}
    """
    extra = set(params) - _REGISTER_SESSION_ROLE_ALLOWED
    if extra:
        raise ActionError(f"register_session_role got unexpected params: {sorted(extra)}")
    missing = _REGISTER_SESSION_ROLE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"register_session_role missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.register_session_role(cfg, params["slug"], params["sid"],
                                           params["role"], params.get("loop") or None)


def _action_unarchive_session(params: dict[str, Any]) -> dict[str, Any]:
    """Remove the archived flag from a session.

    Required params: slug, sid
    Returns: {ok: true, sid, archived: false}
    """
    extra = set(params) - _ARCHIVE_SESSION_ALLOWED
    if extra:
        raise ActionError(f"unarchive_session got unexpected params: {sorted(extra)}")
    missing = _ARCHIVE_SESSION_REQUIRED - set(params)
    if missing:
        raise ActionError(f"unarchive_session missing required params: {sorted(missing)}")

    cfg = _get_config()
    from bot_squad_worker import sessions as _sessions
    return _sessions.unarchive_session(cfg, params["slug"], params["sid"])


# ---------------------------------------------------------------------------
# Autoupdate operator handoff actions (T-0085)
# ---------------------------------------------------------------------------

_AUTOUPDATE_RETRY_REQUIRED = {"slug"}
_AUTOUPDATE_RETRY_ALLOWED = _AUTOUPDATE_RETRY_REQUIRED


def _action_autoupdate_retry(params: dict[str, Any]) -> dict[str, Any]:
    """Re-enqueue the most-recent failed apply job.

    Required params: slug
    Returns: {ok, requeued: bool, version?: str, queue_file?: str, reason?: str}

    Idempotent — when the failed-queue is empty, returns
    ``{ok: true, requeued: false}`` without raising. The ``slug`` param is
    accepted (and validated) for consistency with peer actions even though
    the failed-queue is install-scoped, not project-scoped.
    """
    extra = set(params) - _AUTOUPDATE_RETRY_ALLOWED
    if extra:
        raise ActionError(f"autoupdate_retry got unexpected params: {sorted(extra)}")
    missing = _AUTOUPDATE_RETRY_REQUIRED - set(params)
    if missing:
        raise ActionError(f"autoupdate_retry missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autoupdate_retry: unknown project slug {slug!r}")

    from bot_squad_worker import autoupdate_apply as _apply
    try:
        return _apply.retry_last_failed(cfg)
    except Exception as e:
        raise ActionError(f"autoupdate_retry: {e}") from e


_AUTOUPDATE_FORCE_REQUIRED = {"slug", "version"}
_AUTOUPDATE_FORCE_ALLOWED = _AUTOUPDATE_FORCE_REQUIRED


def _action_autoupdate_force(params: dict[str, Any]) -> dict[str, Any]:
    """Force-apply a specific release version (skips poller's newer-than gate).

    Required params: slug, version
    Returns: {ok: true, version, queue_file}

    The manifest entry is fetched from the mothership's
    ``/api/releases/<version>`` endpoint and enqueued for the drain loop.
    Apply itself runs out-of-band on the next tick.
    """
    extra = set(params) - _AUTOUPDATE_FORCE_ALLOWED
    if extra:
        raise ActionError(f"autoupdate_force got unexpected params: {sorted(extra)}")
    missing = _AUTOUPDATE_FORCE_REQUIRED - set(params)
    if missing:
        raise ActionError(f"autoupdate_force missing required params: {sorted(missing)}")

    cfg = _get_config()
    slug = params["slug"]
    version = params["version"]
    if cfg.projects.get(slug) is None:
        raise ActionError(f"autoupdate_force: unknown project slug {slug!r}")
    if not isinstance(version, str) or not version.strip():
        raise ActionError("autoupdate_force: empty version")

    from bot_squad_worker import autoupdate_apply as _apply
    try:
        return _apply.force_apply(cfg, version)
    except Exception as e:
        raise ActionError(f"autoupdate_force: {e}") from e


_TG_READ_ALLOWED = {"mode", "limit", "chat_id", "query", "since_ts"}
_TG_READ_DEFAULT_LIMIT = 30
_TG_READ_MAX_LIMIT = 200


def _action_tg_read(params: dict[str, Any]) -> dict[str, Any]:
    """Read ingested personal-account Telegram messages (read-only, SELECT-only).

    Queries the SQLite db written by the tg_user (Telethon) ingest worker at
    ``data/_tg/messages.db``. Coordinator-invoked only; this is NOT wired into
    the conversational TG brain (personal-message reading stays a deliberate
    socket action for privacy).

    Params:
        mode     : "recent" (default) | "chat" | "search"
        limit    : int, default 30, capped at 200
        chat_id  : int — required for mode="chat", optional filter otherwise
        query    : str — required for mode="search"; LIKE-matched over text
        since_ts : int — optional unix-seconds lower bound (ts >= since_ts)

    Returns {"messages": [{ts, chat_id, chat_title, sender_name, text, msg_id}, ...]}
    newest-first. If the db file does not exist yet, returns an empty list with
    a note (ingest not running / not logged in).
    """
    extra = set(params) - _TG_READ_ALLOWED
    if extra:
        raise ActionError(f"tg_read got unexpected params: {sorted(extra)}")

    mode = params.get("mode", "recent")
    if mode not in ("recent", "chat", "search"):
        raise ActionError(f"tg_read: invalid mode {mode!r} (recent|chat|search)")

    # Clamp limit into [1, MAX]; tolerate non-int by falling back to default.
    try:
        limit = int(params.get("limit", _TG_READ_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ActionError("tg_read: limit must be an integer")
    if limit < 1:
        limit = 1
    if limit > _TG_READ_MAX_LIMIT:
        limit = _TG_READ_MAX_LIMIT

    chat_id = params.get("chat_id")
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            raise ActionError("tg_read: chat_id must be an integer")

    since_ts = params.get("since_ts")
    if since_ts is not None:
        try:
            since_ts = int(since_ts)
        except (TypeError, ValueError):
            raise ActionError("tg_read: since_ts must be an integer")

    query = params.get("query")
    if mode == "chat" and chat_id is None:
        raise ActionError("tg_read: mode='chat' requires chat_id")
    if mode == "search" and not (isinstance(query, str) and query.strip()):
        raise ActionError("tg_read: mode='search' requires a non-empty query")

    cfg = _get_config()
    db_path = cfg.tg_user_db_path
    if not Path(db_path).exists():
        return {
            "messages": [],
            "note": "no messages.db yet — ingest not running/logged in",
        }

    import sqlite3

    # Open strictly read-only via the URI immutable/ro mode so this action can
    # never mutate the ingest db even if a bug slips a write through.
    uri = f"file:{db_path}?mode=ro"
    where: list[str] = []
    args: list[Any] = []
    if mode == "chat":
        where.append("chat_id = ?")
        args.append(chat_id)
    elif mode == "search":
        where.append("text LIKE ?")
        args.append(f"%{query}%")
        if chat_id is not None:
            where.append("chat_id = ?")
            args.append(chat_id)
    else:  # recent
        if chat_id is not None:
            where.append("chat_id = ?")
            args.append(chat_id)
    if since_ts is not None:
        where.append("ts >= ?")
        args.append(since_ts)

    sql = (
        "SELECT ts, chat_id, chat_title, sender_name, text, msg_id "
        "FROM messages"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, msg_id DESC LIMIT ?"
    args.append(limit)

    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    messages = [
        {
            "ts": r["ts"],
            "chat_id": r["chat_id"],
            "chat_title": r["chat_title"],
            "sender_name": r["sender_name"],
            "text": r["text"],
            "msg_id": r["msg_id"],
        }
        for r in rows
    ]
    return {"messages": messages}


_LIST_ACTIONS_ALLOWED: set[str] = set()


def _action_list_actions(params: dict[str, Any]) -> dict[str, Any]:
    """Enumerate the action registry — names, modes, docstring summaries.

    Required params: (none)
    Returns: {
        ok: true,
        actions: [{name, mode, summary, available_in_current_mode}, ...],
        current_mode: "coordinator" | "user-worker",
    }

    The summary is the first non-empty line of each action's docstring. Lets
    a coord (or any HTTP client) discover what's callable without grepping
    source. T-0003 in the bot-swarm punch list.
    """
    extra = set(params) - _LIST_ACTIONS_ALLOWED
    if extra:
        raise ActionError(f"list_actions got unexpected params: {sorted(extra)}")

    current = get_mode()
    out = []
    for name in sorted(ACTION_REGISTRY):
        handler = ACTION_REGISTRY[name]
        tag = ACTION_MODES.get(name, "coordinator_only")
        doc = (handler.__doc__ or "").strip()
        summary = doc.split("\n\n", 1)[0].split("\n", 1)[0].strip() if doc else ""
        out.append({
            "name": name,
            "mode": tag,
            "summary": summary,
            "available_in_current_mode": _mode_allows(current, tag),
        })
    return {"ok": True, "current_mode": current, "actions": out}


ACTION_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "noop": _action_noop,
    "list_actions": _action_list_actions,
    "set_halt": _action_set_halt,
    "status_summary": _action_status_summary,
    "tg_verify_login": _action_tg_verify_login,
    "tg_notify": _action_tg_notify,
    "deploy": _action_deploy,
    "list_sessions": _action_list_sessions,
    "pause_session": _action_pause_session,
    "suspend_session": _action_suspend_session,
    "resume_session": _action_resume_session,
    "spawn_session": _action_spawn_session,
    "sleep_expert": _action_sleep_expert,
    "respawn_expert": _action_respawn_expert,
    "sleep_and_respawn": _action_sleep_and_respawn,
    "list_initiatives": _action_list_initiatives,
    "read_initiative": _action_read_initiative,
    "list_tasks": _action_list_tasks,
    "read_task": _action_read_task,
    "list_experts": _action_list_experts,
    "read_expert_memory": _action_read_expert_memory,
    "bootstrap_project_repo": _action_bootstrap_project_repo,
    "get_strategy": _action_get_strategy,
    "set_strategy": _action_set_strategy,
    "scheduler_state": _action_scheduler_state,
    "inject_input": _action_inject_input,
    "capture_pane": _action_capture_pane,
    "autonomous_status": _action_autonomous_status,
    "autonomous_enable": _action_autonomous_enable,
    "autonomous_disable": _action_autonomous_disable,
    "peer_send": _action_peer_send,
    "peer_inbox_read": _action_peer_inbox_read,
    "peer_inbox_wait": _action_peer_inbox_wait,
    "task_progress_add": _action_task_progress_add,
    "bind_task": _action_bind_task,
    "bind_initiative": _action_bind_initiative,
    "unbind_task": _action_unbind_task,
    "unbind_initiative": _action_unbind_initiative,
    "archive_session": _action_archive_session,
    "unarchive_session": _action_unarchive_session,
    "register_session_role": _action_register_session_role,
    # T-0085: autoupdate operator handoff levers.
    "autoupdate_retry": _action_autoupdate_retry,
    "autoupdate_force": _action_autoupdate_force,
    # Personal-account Telegram read path (coordinator-invoked, read-only).
    "tg_read": _action_tg_read,
}


# Mode tags: which worker role is allowed to invoke each action.
# coordinator_only — needs scheduler / coordinator-only state (TG client,
#   deploy queue, peer inbox, autonomous orchestrator).
# tmux_only — purely tmux/filesystem ops local to a Linux user.
# both — universally safe (proof-of-life).
ACTION_MODES: dict[str, str] = {
    "noop": "both",
    "list_actions": "both",
    "set_halt": "coordinator_only",
    "status_summary": "both",
    "tg_verify_login": "coordinator_only",
    "tg_notify": "coordinator_only",
    "deploy": "coordinator_only",
    "list_sessions": "tmux_only",
    "pause_session": "tmux_only",
    "suspend_session": "tmux_only",
    "resume_session": "tmux_only",
    "spawn_session": "tmux_only",
    # R30: these lazily import coordinator-only modules (sleep / swarm_state /
    # swarm_strategy) that a user-worker bundle does not ship.
    "sleep_expert": "coordinator_only",
    "respawn_expert": "tmux_only",
    "sleep_and_respawn": "coordinator_only",
    "list_initiatives": "coordinator_only",
    "read_initiative": "coordinator_only",
    "list_tasks": "coordinator_only",
    "read_task": "coordinator_only",
    "list_experts": "coordinator_only",
    "read_expert_memory": "coordinator_only",
    "bootstrap_project_repo": "tmux_only",
    "get_strategy": "coordinator_only",
    "set_strategy": "coordinator_only",
    "scheduler_state": "coordinator_only",
    "inject_input": "tmux_only",
    "capture_pane": "tmux_only",
    "autonomous_status": "coordinator_only",
    "autonomous_enable": "coordinator_only",
    "autonomous_disable": "coordinator_only",
    "peer_send": "coordinator_only",
    "peer_inbox_read": "coordinator_only",
    "peer_inbox_wait": "coordinator_only",
    "task_progress_add": "coordinator_only",
    "bind_task": "coordinator_only",
    "bind_initiative": "coordinator_only",
    "unbind_task": "coordinator_only",
    "unbind_initiative": "coordinator_only",
    # archive/unarchive are pure tmux/filesystem ops on a session's own md
    # frontmatter (no scheduler / TG / coordinator state) — so a user-worker
    # daemon MUST be able to run them. Loopyard's headless shutdown() archives
    # each agent session at wind-down; under a user-worker loops daemon that call
    # would otherwise be refused and leave dangling tmux sessions (round-4 B1).
    "archive_session": "tmux_only",
    "unarchive_session": "tmux_only",
    "register_session_role": "tmux_only",
    # T-0085: autoupdate handoff is install-scoped (coordinator).
    "autoupdate_retry": "coordinator_only",
    "autoupdate_force": "coordinator_only",
    # Personal-message reading is coordinator-only (privacy).
    "tg_read": "coordinator_only",
}


def _mode_allows(mode: str, tag: str) -> bool:
    if tag == "both":
        return True
    if mode == "coordinator":
        return True  # coordinator also serves its own user — all tags allowed
    # user-worker: only tmux_only + both
    return tag == "tmux_only"


def dispatch(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Look up `name` in the allowlist; reject if missing or mode-disallowed; invoke."""
    if _DEGRADED_REASON:
        # Worker came up with a broken config. Don't run actions against a
        # missing/garbage Config — surface the load error instead.
        raise DegradedError(_DEGRADED_REASON)
    handler = ACTION_REGISTRY.get(name)
    if handler is None:
        raise ActionError(f"unknown action: {name!r}")
    tag = ACTION_MODES.get(name, "coordinator_only")
    mode = get_mode()
    if not _mode_allows(mode, tag):
        raise ActionError(f"action {name!r} not available in {mode} mode")
    return handler(params or {})
