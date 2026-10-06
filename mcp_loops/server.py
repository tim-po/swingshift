"""mcp-loops FastMCP server (streamable-HTTP, 127.0.0.1:8771).

Typed tools over the loop machinery (part d) — every tool is a THIN wrapper
over the existing modules: configs are validated by ``schema.validate_config``,
rendered by ``render.render_to_file``, executed by ``runner.LoopRunner`` on
``headless.HeadlessSubstrate``, and turn statuses come from the same
``report.status_log`` the agents already write. Nothing is re-implemented here.

State lives under ``data/_loops/<name>/`` (``LOOPS_DATA_DIR`` overrides, same
env report.py honours — tests point it at a tmp dir): ``config.json`` (the
normalized config), ``config.html`` (block-scheme), ``run.json`` (run state),
``status.jsonl`` (per-turn agent reports, written by mcp_loops.report).

A started loop runs in a background thread; its manager's ``ask_owner`` pauses
park the thread on an in-process queue that ``loop_reply`` feeds (run state
shows ``waiting_owner`` + the question). The registry is in-memory — after a
server restart a previously running loop can't be replied to or stopped, only
inspected; that's the same fail-soft posture as the rest of the stack.

Every tool is fail-soft: bad name / invalid config / unknown loop → a clear
``{"error": ...}`` dict, never a crash.

Run:  python -m mcp_loops.server   (from the install root, or its worker venv)
Env:  MCP_LOOPS_HOST (127.0.0.1) / MCP_LOOPS_PORT (8771) / LOOPS_DATA_DIR

Register in a Claude Code session:
  claude mcp add --transport http mcp-loops "http://127.0.0.1:8771/mcp"
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

# Prod launches ``python -m mcp_loops.server``: this module then runs as
# ``__main__``, and every lazy ``from mcp_loops import server`` (origin_service's
# hub bridge / origin-agent install, InProcessEngine) would import a SECOND copy
# and wire its globals there — invisible to the tools actually serving. Alias
# the running module first so both names are one object.
if __name__ == "__main__":
    sys.modules.setdefault("mcp_loops.server", sys.modules[__name__])
    import mcp_loops as _pkg
    _pkg.server = sys.modules["mcp_loops.server"]  # type: ignore[attr-defined]
    del _pkg

from html import escape
from types import SimpleNamespace
from typing import Any, Callable, Optional, Protocol

from mcp.server.fastmcp import FastMCP

from mcp_loops import (agents, authoring, capabilities, connect, creator,
                       creator_context, dispatch, envelope, headless, hub_capabilities,
                       hub_render, ideahub, issues, loopyard, loopyard_import,
                       onboarding, origins, origins_probe, paths, products,
                       products_gather, products_git, report, resolution,
                       runner_registry, sandbox, schema, services, sessions, standalone,
                       teamroom, threads, workspaces)
from mcp_loops.render import render_to_file
from mcp_loops.runner import FilePersistence, Guardian, LoopRunner, SubloopDispatcher
from mcp_loops.schema import validate_config
from mcp_loops import turn_identity  # H7 status-row identity (runId/turn)
from mcp_loops.sync import ids as sync_ids  # S-1 deterministic loopId/originId/runId

HOST = os.environ.get("MCP_LOOPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_LOOPS_PORT", "8771"))

# Worker-daemon slug the headless sessions are spawned under. There is NO
# hardcoded fallback (the old default was "coord", which only exists on the
# original swarm box → a fresh install died with the opaque worker-daemon trace
# "spawn_session HTTP 400: unknown project slug coord"). The slug is resolved per
# dispatch by _resolve_slug: an explicit caller slug wins, else the LOOPS_SLUG
# ops override, else the ATTACHED RUNNER's default (written by `yard up`); with
# none of those a dispatch fails CLEARLY instead of leaking the daemon trace.
def _project_slug_for_config(config: Optional[dict]) -> Optional[str]:
    """S1 slug unification — the worker slug a loop's bound PROJECT can serve as.

    A Project IS the worker spawn target, so a loop bound to a Project can derive
    its daemon slug from that Project's id when nothing more specific is set. The
    Project id must be usable as a runner/daemon slug (same charset rules as
    ``runner_registry.valid_slug``); otherwise this returns None and the caller
    falls through. Accepts the canonical ``projectId`` and the ``productId``
    alias. Pure — no I/O."""
    if not isinstance(config, dict):
        return None
    pid = config.get("projectId") or config.get("productId")
    if not isinstance(pid, str) or not pid.strip():
        return None
    pid = pid.strip()
    return pid if runner_registry.valid_slug(pid) is None else None


def _resolve_slug(explicit: str,
                  config: Optional[dict] = None) -> tuple[Optional[str], Optional[dict]]:
    """Resolve the worker-daemon slug for a dispatch → (slug, error).

    Precedence: explicit caller slug > LOOPS_SLUG env (ops escape hatch) >
    attached runner marker (`yard up`) > the loop's bound PROJECT (S1: a Project
    can serve as the slug a loop runs under). No runner + no override + no
    explicit slug + no bound project → a clear, actionable error — NEVER the
    opaque 'unknown project slug coord' daemon trace the pilot hit on a fresh box.

    The Project fallback is STRICTLY ADDITIVE: it sits BELOW the attached-runner
    default, so every existing install (which has a runner attached) resolves
    EXACTLY as before — the fallback only fills the case that used to error."""
    if explicit:
        return explicit, None
    env = os.environ.get("LOOPS_SLUG")
    if env:
        return env, None
    slug = runner_registry.default_slug()
    if slug:
        return slug, None
    proj_slug = _project_slug_for_config(config)
    if proj_slug:
        return proj_slug, None
    return None, {"error": "no runner attached — run `yard up` on this box to "
                           "attach a runner (it registers a default slug so loops "
                           "can dispatch), bind the loop to a Project, or pass an "
                           "explicit slug."}

mcp = FastMCP("mcp-loops", host=HOST, port=PORT)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run(work: Callable[[], dict]) -> dict:
    """Fail-soft shell for every tool: ANY exception becomes {"error": ...} —
    a live MCP server must never crash on a tool call."""
    try:
        return work()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ── paths + state files (all derived from report.py so env redirection holds) ──
def _check_name(name: str) -> Optional[dict]:
    if not isinstance(name, str) or not _NAME_RE.match(name or ""):
        return {"error": f"invalid loop name {name!r} (want [A-Za-z0-9._-])"}
    return None


def _config_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "config.json")


def _html_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "config.html")


def _run_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "run.json")


# ── mirror-aware path helpers ────────────────────────────────────────────────
# The tool WRITES only to the local mirror (report.status_dir → LOOPS_DATA_DIR):
# mirrors of other origins are READ-ONLY from this box (the daemon on the remote
# host owns them). These helpers let the read-side tools (loop_list / loop_get /
# runs) see loops on any origin without changing the write path.
def _data_root_for(origin_id: str) -> Optional[str]:
    """Absolute mirror path for ``origin_id``, or None if the origin isn't
    visible from this box. ``local`` → the tool's own data dir."""
    if not isinstance(origin_id, str) or not origin_id:
        return None
    for oid, _kind, path in origins._mirror_dirs(_local_mirror_path()):
        if oid == origin_id:
            return path
    return None


def _config_path_for(origin_id: str, name: str) -> Optional[str]:
    root = _data_root_for(origin_id)
    return None if root is None else os.path.join(root, name, "config.json")


def _run_path_for(origin_id: str, name: str) -> Optional[str]:
    root = _data_root_for(origin_id)
    return None if root is None else os.path.join(root, name, "run.json")


def _status_dir_for(origin_id: str, name: str) -> Optional[str]:
    """Per-loop status dir on ``origin_id``'s mirror, or None if that origin
    isn't visible from this box. For ``local`` this is exactly
    ``report.status_dir(name)`` (same LOOPS_DATA_DIR the tool writes under); a
    remote origin resolves under its READ-ONLY ``_loops_<host>`` sibling mirror.
    This is the seam that lets the DETAIL/inspect read-tools (loop_status /
    loop_live / loop_turn_detail / the result envelope) surface a loop that runs
    on ANY connected origin in the ONE hub dashboard — without a second panel and
    without changing the local write path."""
    root = _data_root_for(origin_id)
    return None if root is None else os.path.join(root, name)


def _status_log_for(origin_id: str, name: str) -> Optional[str]:
    base = _status_dir_for(origin_id, name)
    return None if base is None else os.path.join(base, "status.jsonl")


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        # OSError covers FileNotFoundError, NotADirectoryError (stray file where
        # a directory is expected — e.g. loops-origins.json inside data/_loops),
        # IsADirectoryError, and permission errors. All mean "no config here";
        # the caller treats a missing path as an absent loop.
        return None


def _write_json(path: str, data: dict) -> str:
    """Write ``data`` as JSON atomically (S-1a): a sibling temp file then
    ``os.replace``, as runner.py does for live.json — a crash mid-write leaves
    the previous file, never a truncated one. The temp name is unique per
    writer so two threads writing one path never share a temp file; it is
    created 0666 & ~umask, the same mode a plain ``open(path, "w")`` gives."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _local_origin_state_dir() -> str:
    """This box's origin state dir (``<data>/_origin``) — the same dir the origin
    agent keeps its keypair in (``origin_service.origin_state_dir``), so the
    engine and the origin client share ONE logical originId (S-1 c)."""
    return os.path.join(_local_mirror_path(), "_origin")


def _sync_loop_id(name: str) -> Optional[str]:
    """S-1 (b): this loop's deterministic ``loopId`` via ``sync.ids`` — honours
    the ``.loopid`` sidecar when it is ours (D14), else (re)derives it, rewrites
    the sidecar beside config.json and logs ``loop.split`` to
    ``<data>/_sync/split.jsonl``. Fail-soft: identity bookkeeping must never
    cost a save or a start, so any error is logged and yields None."""
    try:
        data_dir = _local_mirror_path()
        if not os.path.isdir(os.path.join(data_dir, name)):
            return None
        oid = sync_ids.local_origin_id(_local_origin_state_dir())
        return sync_ids.resolve_loop_id(data_dir, name, oid,
                                        split_log=sync_ids.split_log_path(data_dir))
    except Exception as exc:  # noqa: BLE001
        print(f"[sync-ids] {name}: loopId unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def _mint_run_id(name: str, started: float) -> str:
    """H7 ``runId`` made deterministic (S-1, D5): ``run_id(loopId, started)`` from
    the SAME ``started`` float written to run.json, so a re-ingest re-derives it.
    Same 32-hex form H7 readers expect; falls back to H7's random mint when the
    loopId is unavailable (identity must never block a start)."""
    loop_id = _sync_loop_id(name)
    return sync_ids.run_id(loop_id, started) if loop_id else turn_identity.mint_run_id()


def _update_run(name: str, **fields) -> dict:
    state = _read_json(_run_path(name)) or {}
    state.update(fields, updated=time.time())
    _write_json(_run_path(name), state)
    return state


# ── owner Telegram notify (shared seam: ask_owner ping, guardian alert, finish
# report). Factored out of TelegramRunOwner's inline try/except:pass so failures
# are LOGGED (not silently swallowed — that was the ask_owner wedge, #10) and so
# tests can monkeypatch a single function to prove the round-trip without network.
def _owner_telegram() -> tuple[Optional[str], Optional[str]]:
    return os.environ.get("LOOPS_OWNER_BOT"), os.environ.get("LOOPS_OWNER_CHAT")


def _tg_notify(text: str, *, bot: Optional[str] = None, chat: Optional[str] = None,
               doc_path: Optional[str] = None, caption: str = "") -> dict:
    """Best-effort owner ping over the configured Telegram bot (or whatever LOOPS_OWNER_BOT
    names). Returns {ok, ...} — never raises. Logs on failure. Single seam for
    all owner-facing Telegram from the loops server."""
    if bot is None or chat is None:
        eb, ec = _owner_telegram()
        bot = bot or eb
        chat = chat or ec
    if not (bot and chat):
        return {"ok": False, "skipped": "LOOPS_OWNER_BOT/LOOPS_OWNER_CHAT unset"}
    try:
        from mcp_loops import telegram_optional
        client = telegram_optional.tg_client(bot)
        if client is None:  # D8: absent module ⇒ logged-once no-op, never a crash
            return {"ok": False, "skipped": telegram_optional.SKIPPED}
        res = client.send(str(chat), text)
        out = {"ok": True, "send": res}
        if doc_path and os.path.exists(doc_path):
            try:
                out["doc"] = client.send_document(str(chat), doc_path, caption=caption)
            except Exception as e:  # noqa: BLE001 — the text got through; doc is a bonus
                out["doc_error"] = f"{type(e).__name__}: {e}"
        return out
    except Exception as e:  # noqa: BLE001 — LOG, do not silently swallow (#10 wedge)
        print(f"[loops] owner Telegram notify failed: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── background runs ───────────────────────────────────────────────────────────
_RUNS: dict[str, dict] = {}   # name -> {thread, substrate, owner, stop_requested}


class QueueOwnerChannel:
    """OwnerChannel that parks the runner thread until loop_reply delivers.

    ``ask_owner`` writes the question into run.json (state=waiting_owner) and
    blocks on an in-process queue; ``loop_reply`` feeds it. Same contract as
    the Telegram bridge — the two are interchangeable per loop_start args.
    """

    def __init__(self, name: str):
        self.name = name
        self._q: queue.Queue[str] = queue.Queue()
        self.waiting = False
        self.question = ""

    def __call__(self, question: str) -> str:
        self.question = question
        self.waiting = True
        _update_run(self.name, state="waiting_owner", question=question)
        reply = self._q.get()
        self.waiting = False
        _update_run(self.name, state="running", question=None)
        return reply

    def deliver(self, reply: str) -> None:
        self._q.put(reply)


class TelegramRunOwner(QueueOwnerChannel):
    """Owner channel that PARKS on loop_reply (exactly like QueueOwnerChannel) AND
    fires a ONE-SHOT Telegram notify so the owner sees the question on their phone.

    It deliberately does NOT poll getUpdates: on a host whose coordinator already
    consumes the bot's getUpdates (coord-core owns the configured Telegram bot), a second
    poller collides (409) and can wedge on a hung long-poll, freezing the run.
    So the owner is pinged on Telegram but ANSWERS via the coordinator/loop_reply,
    which feeds the queue this class inherits. Best-effort ping — if the send
    fails the run still parks correctly and can be answered via loop_reply."""

    def __init__(self, name: str, bot: str, chat_id: str):
        super().__init__(name)
        self._bot = bot
        self._chat = chat_id

    def __call__(self, question: str) -> str:
        res = _tg_notify(
            f"🔔 loop {self.name!r} needs your decision:\n\n{question}\n\n"
            f"(reply through the coordinator — it relays via loop_reply)",
            bot=self._bot, chat=self._chat)
        # record the notify outcome on the run so the UI/coordinator can SEE whether
        # the ping actually reached Telegram (the old silent except:pass hid this).
        _update_run(self.name, owner_notify=res)
        return super().__call__(question)             # parks until loop_reply delivers


def _make_owner(name: str):
    """Telegram owner if LOOPS_OWNER_BOT/CHAT are set (answer from your phone),
    else the in-process queue (answered via loop_reply)."""
    bot = os.environ.get("LOOPS_OWNER_BOT")
    chat = os.environ.get("LOOPS_OWNER_CHAT")
    if bot and chat:
        try:
            return TelegramRunOwner(name, bot, chat)
        except Exception:  # noqa: BLE001 — bridge unavailable: fall back to queue
            pass
    return QueueOwnerChannel(name)


# ── Loop-Workspaces (Phase 1): engine-managed ephemeral per-loop worktree ─────
# Provisioning is gated behind LOOPS_WORKSPACES=1 (default OFF), mirroring the
# LOOPS_MODEL_PASSTHROUGH convention: with the flag off, `workspace` is never set
# and a loop spawns exactly as it does today (the slug's projects.toml repo_path).
# Flip it on at DEPLOY time (an owner step) once proven. A loop with no resolvable
# projectId repo also falls through to the legacy slug path — provisioning is a
# strict requirement ONLY when both the flag is on AND a project repo resolves.
def _provision_loop_workspace(name: str, cfg: dict) -> Optional["workspaces.Workspace"]:
    """Provision (or re-attach) the ephemeral worktree for loop ``name`` off its
    PROJECT repo's base branch, returning the Workspace, or ``None`` to fall
    through to the legacy slug cwd (flag off / no project repo / provision
    failure). Fail-soft: a provisioning error is logged and downgraded to the
    legacy path so a start is never broken by workspace machinery in Phase 1."""
    if os.environ.get("LOOPS_WORKSPACES") != "1":
        return None
    # B2 (M1): a projectId-less loop on a bundle box provisions from the default
    # project `yard start` seeds. Unset (the dev box) ⇒ byte-identical to before.
    project_id = (cfg.get("projectId") or cfg.get("productId")
                  or os.environ.get("LOOPS_DEFAULT_PROJECT"))
    repo = workspaces.resolve_project_repo(str(paths.config_dir()), project_id)
    if not repo:
        return None  # legacy loop / unresolved project → slug repo_path path
    try:
        ws = workspaces.provision(_local_mirror_path(), name, repo)
        print(f"[workspaces] provisioned {name}: {ws.path} "
              f"(branch {ws.branch} off {ws.base} of {repo}"
              f"{'; re-attach sync: ' + ws.synced if ws.synced else ''})", file=sys.stderr)
        return ws
    except workspaces.WorkspaceError as exc:
        # FAIL CLOSED. The flag is on AND a project repo resolved, so this loop is
        # SUPPOSED to run in its own isolated worktree. Falling back to the slug's
        # shared repo_path here would silently run/commit the loop in the LIVE
        # canonical checkout — defeating the isolation the workspace exists to
        # provide. Refuse the start instead; the owner fixes the repo/base and
        # retries. (Legacy loops — flag off / no project — returned None earlier and
        # are unaffected.)
        raise workspaces.WorkspaceError(
            f"loop {name!r}: workspace provisioning failed and isolation is required "
            f"(refusing to fall back to the shared project checkout): {exc}") from exc


def _reap_loop_workspace(name: str, entry: dict, end_state: str) -> None:
    """Reap the loop's workspace on a terminal state, per the trigger policy:
    reap on finish/archive/stop, KEEP on error/timeout (keep-on-error) and while
    dirty (uncommitted work is never discarded). The branch always survives.
    A no-op when the loop had no workspace (legacy / flag off). Fail-soft."""
    ws = entry.get("workspace") if isinstance(entry, dict) else None
    if ws is None:
        return
    if not workspaces.should_reap(end_state):
        print(f"[workspaces] keeping {name} workspace ({end_state}): {ws.path}",
              file=sys.stderr)
        return
    try:
        res = workspaces.reap(ws)
        if res.reaped:
            print(f"[workspaces] reaped {name}: removed {res.path} "
                  f"(branch {res.branch} kept)", file=sys.stderr)
        else:
            print(f"[workspaces] kept {name} workspace ({res.kept_reason}): "
                  f"{res.path}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — reaping is best-effort, never fatal
        print(f"[workspaces] reap error for {name!r} (kept): {exc}", file=sys.stderr)


def _reap_loop_workspace_by_name(name: str, cfg: dict) -> None:
    """Reap a loop's workspace when there is no live entry to carry it — the
    archive trigger (and a wedged-stop that already disowned its run-thread). The
    Workspace is RECONSTRUCTED read-only from the loop's project repo (plan()
    touches nothing). Idempotent: an already-reaped / never-provisioned dir is a
    no-op. Gated + fail-soft exactly like the live path."""
    if os.environ.get("LOOPS_WORKSPACES") != "1":
        return
    # B2 (M1): a projectId-less loop on a bundle box provisions from the default
    # project `yard start` seeds. Unset (the dev box) ⇒ byte-identical to before.
    project_id = (cfg.get("projectId") or cfg.get("productId")
                  or os.environ.get("LOOPS_DEFAULT_PROJECT"))
    repo = workspaces.resolve_project_repo(str(paths.config_dir()), project_id)
    if not repo:
        return
    # Never reap while the run-thread is ALIVE: if a thread still owns this slot,
    # leave the workspace to that thread's own terminal-state reap. A wedged loop
    # (stop requested but the thread won't die) is the DANGEROUS case — the agent
    # is still executing in the tree — so liveness is judged purely by the thread,
    # never gated on stop_requested (a `not stop_requested` clause here would defeat
    # the guard exactly when it matters most and reap the dir under a live agent).
    live = _RUNS.get(name)
    if live and live.get("thread") and live["thread"].is_alive():
        print(f"[workspaces] archive-reap skipped for {name!r}: run-thread still alive",
              file=sys.stderr)
        return
    try:
        # reap_target, not plan(): reaping never needs the base branch, and an
        # unresolvable base must not block cleanup (leaking the dir).
        ws = workspaces.reap_target(_local_mirror_path(), name, repo)
        res = workspaces.reap(ws)
        if res.reaped:
            print(f"[workspaces] reaped {name} on archive: removed {res.path} "
                  f"(branch {res.branch} kept)", file=sys.stderr)
        elif res.kept_reason != "not-provisioned":
            print(f"[workspaces] kept {name} on archive ({res.kept_reason}): "
                  f"{res.path}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks archive
        print(f"[workspaces] archive-reap error for {name!r} (kept): {exc}",
              file=sys.stderr)


def _make_substrate(name: str, slug: str, *, adopt_sessions: Optional[dict] = None):
    """Substrate factory — a seam so tests swap in FakeSubstrate. Passes the
    per-agent turn budgets (config maxTurnMinutes → seconds) so heavy agents
    aren't falsely timed out; the guardian can still extend them mid-loop."""
    from mcp_loops.headless import HeadlessSubstrate
    cfg = _read_json(_config_path(name)) or {}
    timeouts = {sid: int(s["maxTurnMinutes"]) * 60
                for sid, s in (cfg.get("steps") or {}).items()
                if s.get("type") == "agent" and isinstance(s.get("maxTurnMinutes"), int)}
    # per-agent model ids (schema keeps norm["model"] by-value on each agent step).
    # Applied at spawn (daemon `model` param / headless `--model`) unless
    # LOOPS_MODEL_PASSTHROUGH=0 — see headless.model_passthrough_enabled.
    models = {sid: str(s["model"])
              for sid, s in (cfg.get("steps") or {}).items()
              if s.get("type") == "agent" and isinstance(s.get("model"), str) and s["model"].strip()}
    # per-agent RUNTIME ids (CAP-1 multi-CLI). Applied at spawn under the SAME
    # gate as `model`; HeadlessSubstrate rejects anything off its allowlist.
    runtimes = {sid: str(s["runtime"])
                for sid, s in (cfg.get("steps") or {}).items()
                if s.get("type") == "agent" and isinstance(s.get("runtime"), str) and s["runtime"].strip()}
    # Allowlist check BEFORE any cage/workspace is provisioned, so a bad pick is a
    # clean start refusal (loop_start maps ValueError → error) with nothing left
    # behind — never a silent fallback to a bare `claude`.
    for sid in set(runtimes) | set(models):
        try:
            headless.validate_runtime(runtimes.get(sid))
            headless.validate_model(models.get(sid))
        except ValueError as exc:
            raise ValueError(f"agent {sid!r}: {exc}") from None
    # 3.3: a claude that's installed but logged out would sit at its login
    # screen (manager pane) / fail every turn — refuse the start with the fix
    # instead. Only a confirmed logged-out CLI blocks; unknown proceeds.
    if any(s.get("type") == "agent" and headless.runtimes.selection(s.get("runtime"), s.get("model"))[0] == "claude"
           for s in (cfg.get("steps") or {}).values() if isinstance(s, dict)):
        claude_bin = headless._resolve_claude_bin()
        if (os.path.isfile(claude_bin) and os.access(claude_bin, os.X_OK)
                and origins_probe.claude_login_state(claude_bin) is False):
            raise ValueError(origins_probe.CLAUDE_NOT_LOGGED_IN)
    # Runner binding (P1): target the ATTACHED runner's daemon socket + repo +
    # python (written by `yard up`) instead of the install-derived fallback. A fresh
    # install brings its own runner up; nothing here is coupled to the original box.
    # No runner attached ⇒ the HeadlessSubstrate defaults (kept for the swarm box).
    runner = runner_registry.read() or {}
    binding: dict[str, Any] = {}
    if runner.get("sock"):
        binding["sock_path"] = runner["sock"]
    if runner.get("repo"):
        binding["repo"] = runner["repo"]
    if runner.get("python"):
        binding["python"] = runner["python"]
    if runner.get("pythonFlags"):  # R31: -I isolation, verified at attach time
        binding["python_flags"] = tuple(runner["pythonFlags"])
    # Loop-Workspaces (Phase 1): provision the ephemeral per-loop worktree (gated;
    # None ⇒ legacy slug cwd). Its path threads into EVERY spawn as `workspace`;
    # the full Workspace is stashed on the substrate so the run-thread can reap it
    # at the terminal state (see _reap_loop_workspace).
    # Loop cage: plan BEFORE provisioning so a strict refusal (IsolationRefused)
    # leaves nothing behind. None ⇒ LOOPS_ISOLATION=off ⇒ today's spawn exactly.
    policy = sandbox.plan(name, cfg.get("isolation"))
    if policy is not None:
        for line in policy.status_lines:
            print(f"[isolation] {name}: {line}", file=sys.stderr)
    ws = _provision_loop_workspace(name, cfg)
    sub = HeadlessSubstrate(
        slug, name, agent_timeouts=timeouts, agent_models=models, agent_runtimes=runtimes,
        model_passthrough=headless.model_passthrough_enabled(),
        loops_data_dir=os.path.abspath(_local_mirror_path()),
        workspace=(ws.path if ws else None),
        adopt_sessions=adopt_sessions, sandbox_policy=policy, **binding)
    sub._loop_workspace = ws  # type: ignore[attr-defined]  # for terminal-state reap
    # surfaced in run.json → loop_status.run.isolation (never silent)
    sub._isolation_status = (list(policy.status_lines) if policy is not None  # type: ignore[attr-defined]
                             else [f"isolation: off ({sandbox.host_unsupported() or sandbox.MODE_ENV + '=off'})"])
    return sub


def _guardian_alert(name: str) -> Callable[[str], str]:
    """Non-blocking owner alert for the guardian: mark the run needs_owner and
    (if LOOPS_OWNER_BOT/LOOPS_OWNER_CHAT are set) ping the owner on Telegram.
    Never blocks — unlike the ask_owner channel, a guardian give-up is a notify."""
    def notify(msg: str) -> str:
        _update_run(name, state="needs_owner", guardian_alert=msg,
                    owner_notify=_tg_notify(msg))
        return ""
    return notify


# ── A2: stop is authoritative + every run reaches a TERMINAL state ────────────
# The pilot's recurring stop-hang: loop_stop returned ok but run.json stuck at
# 'stopping', a follow-up start_loop wrongly said 'already running', and the
# promised finish-report never landed. Root cause: run.json only ever reached a
# terminal state when the run THREAD returned — a wedged agent that ignores
# should_stop + substrate.shutdown left the thread alive forever. The fixes here
# make the SERVER authoritative: bounded-join on stop, then finalize the run
# ourselves if the thread won't die; disown the orphan so it can't clobber a
# fresh re-run; and write the finish-report where the envelope advertises it.
# B4: single source of truth lives in envelope.RUN_TERMINAL so the internal
# run.json terminal vocabulary and the external status mapping can't drift.
_RUN_TERMINAL = envelope.RUN_TERMINAL
# Owner-parked run states (ask_owner / guardian alert): no process holds them
# but the engine's in-memory reply queue — so a dead engine strands them (H6a).
_RUN_PARKED = ("waiting_owner", "needs_owner")


def _stop_grace() -> float:
    """Seconds loop_stop waits for a cooperative thread to end and write its own
    terminal state before the server finalizes the run authoritatively.
    Env-overridable (LOOPS_STOP_GRACE) so tests can force the wedged path fast."""
    try:
        return max(0.0, float(os.environ.get("LOOPS_STOP_GRACE", "6.0")))
    except (TypeError, ValueError):
        return 6.0


def _finish_report_paths(name: str) -> list[str]:
    """Where finish-report.md is written / cleared: the status dir (the envelope's
    `finish_report` artifact + the analysis read-side) AND the per-loop OUTPUT
    dir (`_output/<name>/`, what the app-connect caller browses as output_dir —
    the pilot looked for it there and found nothing)."""
    return [os.path.join(report.status_dir(name), "finish-report.md"),
            os.path.join(_output_base(), name, "finish-report.md")]


def _finalize_stopped_wedged(name: str, cfg: dict) -> None:
    """Authoritative terminal write for a stopped run whose thread WON'T die.
    Promotes run.json to 'stopped' and writes a finish-report so the run never
    sticks at 'stopping' with a live orphan thread. Idempotent: if the thread
    beat us to a terminal state, leave its (richer) result untouched."""
    run = _read_json(_run_path(name)) or {}
    if run.get("state") in _RUN_TERMINAL:
        return
    prev = run.get("result") or {}
    result = SimpleNamespace(
        name=name, ended="stopped",
        turns_used=prev.get("turns_used", 0),
        winddown_turns=prev.get("winddown_turns", 0),
        retired=prev.get("retired") or [], error=None)
    _update_run(name, state="stopped", question=None,
                result={"name": name, "ended": "stopped",
                        "turns_used": result.turns_used,
                        "winddown_turns": result.winddown_turns,
                        "retired": result.retired, "events": prev.get("events") or [],
                        "error": None, "wedged": True})
    _send_finish_report(name, cfg, result, stopped=True)


# ── A2: wedge-sweep on server (re)start ───────────────────────────────────────
# The pilot-b wedge: when the MCP server + poller are re-parented to a dying
# parent shell (fixed structurally by A2 detach), an in-flight run's run.json was
# left at state=running with NO finish-report and NO live thread — silently stuck
# forever. Detach is the primary fix; this is the belt to that suspenders: on
# start, any LOCAL run still 'running'/'stopping' whose runtime plainly died with
# the prior process is finalized to a terminal 'error' + a finish-report that says
# so, so a wedged run is honestly reported instead of eternally 'running'.
def _wedge_stale_secs() -> float:
    """A local run 'running'/'stopping' but untended for this long when the server
    (re)starts is presumed WEDGED — its runtime died with the prior process.
    Env-overridable (LOOPS_WEDGE_STALE) so tests force the sweep path fast."""
    try:
        return max(0.0, float(os.environ.get("LOOPS_WEDGE_STALE", "120.0")))
    except (TypeError, ValueError):
        return 120.0


def _finalize_wedged(name: str, cfg: dict, run: dict) -> None:
    """Authoritative terminal write for a run whose RUNTIME DIED (swept on start).
    Promotes run.json 'running'/'stopping' → terminal 'error' with a ``wedged``
    marker + writes a finish-report noting the runtime died, so a run whose server
    was torn down mid-flight is never silently stuck at 'running'. Idempotent: a
    run that already reached a terminal state is left untouched. A run PARKED
    on the owner (``waiting_owner``/``needs_owner``, H6a) gets the same terminal
    write — its in-memory reply queue died with the engine, so nothing can ever
    resume it."""
    if run.get("state") in _RUN_TERMINAL:
        return
    prev = run.get("result") or {}
    if run.get("state") in _RUN_PARKED:
        msg = (f"runtime died — the run was parked ({run.get('state')}) on an owner "
               "question when its engine was torn down, and the reply queue died "
               "with it; swept to wedged on restart (start the loop again to "
               "continue)")
    else:
        msg = ("runtime died — the run was mid-flight when the server was torn "
               "down; swept to wedged on restart (no finish-report was written "
               "by the run)")
    result = SimpleNamespace(
        name=name, ended="error",
        turns_used=prev.get("turns_used", 0),
        winddown_turns=prev.get("winddown_turns", 0),
        retired=prev.get("retired") or [], error=msg)
    _update_run(name, state="error", question=None,
                result={"name": name, "ended": "error",
                        "turns_used": result.turns_used,
                        "winddown_turns": result.winddown_turns,
                        "retired": result.retired, "events": prev.get("events") or [],
                        "error": msg, "wedged": True})
    _send_finish_report(name, cfg, result, stopped=True)


# ── engine identity: which PROCESS owns a run (multi-engine split-brain guard) ──
# A run is OWNED by the engine process that set it 'running'. Normally one engine
# process serves a data dir; when the dial-out origin-agent is enabled (G2.2), a
# per-origin origin-ENGINE subprocess can serve the SAME data dir alongside the
# hub engine. Each engine's `sweep_wedged_runs` only knows its OWN live runs (via
# _RUNS), so a sibling engine used to wedge a peer's genuinely-live run — the
# split-brain that forced LOOPYARD_ORIGIN_AGENT off (2026-09-22). The fix: stamp
# every run with the owning engine's identity, and never let a sibling wedge a run
# whose owning engine is a DIFFERENT process that is STILL ALIVE. When the agent
# is OFF there is only one engine, so on restart the prior engine's pid is gone →
# the guard is inert and the single-engine sweep behaves exactly as before.
_ENGINE_BOOT = secrets.token_hex(8)   # unique per process; defeats PID reuse


def _pid_starttime(pid: int) -> Optional[int]:
    """The kernel start-time (clock ticks since boot) of ``pid`` from
    ``/proc/<pid>/stat`` field 22 — stable for the process's whole life and NOT
    inherited when the pid is later reused. Confirms a pid still names the SAME
    process. None when unavailable (pid gone, or no ``/proc`` — e.g. non-Linux),
    in which case the caller falls back to a plain liveness probe."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
    except OSError:
        return None
    rparen = data.rfind(")")          # comm (field 2) may hold spaces/parens
    if rparen < 0:
        return None
    fields = data[rparen + 2:].split()
    try:
        return int(fields[19])        # field 22 overall; index 19 after comm+state
    except (IndexError, ValueError):
        return None


def _engine_identity() -> dict:
    """This engine process's identity: ``{pid, boot, starttime?}``. Stamped on
    every run this process owns (set 'running') so a sibling engine can tell a
    live peer's run from a dead process's leftover."""
    ident = {"pid": os.getpid(), "boot": _ENGINE_BOOT}
    st = _pid_starttime(os.getpid())
    if st is not None:
        ident["starttime"] = st
    return ident


def _engine_alive(ident: dict) -> bool:
    """Is the engine named by ``ident`` still the SAME live process (pid alive and,
    where knowable, start-time matching — defeating PID reuse)? Conservative: a
    missing/malformed identity returns False so the caller falls through to the
    staleness-based sweep, preserving pre-engine-stamp behavior for old runs."""
    pid = ident.get("pid")
    if not isinstance(pid, int) or not services.is_alive(pid):
        return False
    want = ident.get("starttime")
    if want is not None:
        got = _pid_starttime(pid)
        if got is not None and got != want:
            return False              # pid reused by a different process
    return True


def _teardown_orphan_cage(name: str, run: dict) -> None:
    """Step 5 after an engine hard-kill: the dead engine's loop slice (recorded
    as run.json ``cage_slice``) may still hold agents — stop it. Fail-soft."""
    sl = run.get("cage_slice")
    if not sandbox.is_cage_slice(sl, loop=name):   # never app.slice & co.
        return
    try:
        res = sandbox.teardown(sl)
        _update_run(name, cage_teardown=res)
        print(f"[isolation] {name}: orphan cage teardown {res}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[isolation] {name}: orphan cage teardown failed: {exc}", file=sys.stderr)
        return
    _capture_loop_workspace(name, _planned_workspace(name), res)
    _teardown_descendant_cages(name)


def _teardown_descendant_cages(name: str) -> list[dict]:
    """Step 5 for sub-loops: every first-class child (``<name>.<sid>``, any
    depth) has its OWN slice, which the parent's teardown does not reach. Stop
    each one its run.json records (hash-pinned to that child). Idempotent,
    fail-soft; returns the teardown results."""
    root, out = _local_mirror_path(), []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for child in entries:
        if not child.startswith(name + "."):
            continue
        sl = (_read_json(os.path.join(root, child, "run.json")) or {}).get("cage_slice")
        if not sandbox.is_cage_slice(sl, loop=child):
            continue
        try:
            res = sandbox.teardown(sl)
        except Exception as exc:  # noqa: BLE001 — best-effort
            res = {"slice": sl, "stopped": False, "error": str(exc)[:300]}
        _update_run(child, cage_teardown=res)
        print(f"[isolation] {child}: sub-loop cage teardown {res}", file=sys.stderr)
        out.append(res)
    return out


def _planned_workspace(name: str):
    """The loop's Workspace reconstructed read-only (no live entry — hard-kill
    sweep). None when workspaces are off or the project repo is unresolvable."""
    if os.environ.get("LOOPS_WORKSPACES") != "1":
        return None
    cfg = _read_json(_config_path(name)) or {}
    project_id = (cfg.get("projectId") or cfg.get("productId")
                  or os.environ.get("LOOPS_DEFAULT_PROJECT"))
    try:
        repo = workspaces.resolve_project_repo(str(paths.config_dir()), project_id)
        return (workspaces.reap_target(_local_mirror_path(), name, repo)
                if repo else None)
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _capture_loop_workspace(name: str, ws, teardown: Optional[dict]) -> None:
    """Step 5 artifact capture after the cage slice stopped: fsync the branch and
    snapshot uncommitted work to ``refs/loop-captures/<branch>/<stamp>``; stale git
    locks are cleared only when the teardown proved the slice empty. Recorded as
    run.json ``cage_capture``. No-op without a workspace. Never raises."""
    if ws is None:
        return
    td = teardown or {}
    quiescent = bool(td.get("stopped")) and td.get("remaining") == 0
    try:
        res = workspaces.capture(ws, quiescent=quiescent).as_dict()
    except Exception as exc:  # noqa: BLE001
        res = {"captured": False, "error": str(exc)[:300]}
    _update_run(name, cage_capture=res)
    print(f"[isolation] {name}: capture {res}", file=sys.stderr)


def sweep_wedged_runs(*, now: Optional[float] = None) -> list[str]:
    """Sweep LOCAL runs stuck 'running'/'stopping' — or PARKED on the owner
    ('waiting_owner'/'needs_owner', H6a) — whose runtime died with a prior
    process: finalize each to a wedged terminal 'error' + finish-report. SKIPS a
    run younger than the staleness window (it may be a genuinely live run), a
    run THIS process still tracks live in ``_RUNS`` (never finalize an in-process
    run out from under itself), and a run OWNED BY A LIVE SIBLING ENGINE (the
    multi-engine split-brain guard — see ``_engine_alive``). Returns the names
    swept. Called once at server start (main) before serving begins; safe any
    time."""
    now = now if now is not None else time.time()
    stale = _wedge_stale_secs()
    swept: list[str] = []
    root = _local_mirror_path()
    if not os.path.isdir(root):
        return swept
    for entry in sorted(os.listdir(root)):
        if entry.startswith("_"):
            continue
        run = _read_json(os.path.join(root, entry, "run.json"))
        state = run.get("state") if run else None
        if state not in ("running", "stopping") and state not in _RUN_PARKED:
            continue
        tracked = _RUNS.get(entry)
        if tracked and tracked.get("thread") and tracked["thread"].is_alive():
            continue  # live in THIS process — not wedged
        eng = run.get("engine")
        if (isinstance(eng, dict) and eng.get("boot") != _ENGINE_BOOT
                and _engine_alive(eng)):
            continue  # owned by a LIVE sibling engine — never wedge a peer's run
        # A parked run whose stamped owner engine is provably gone can never be
        # resumed (the reply queue lived in that process), however recently it
        # parked — skip the freshness wait for it. Unstamped (legacy) parked
        # runs still wait out the window like running ones.
        dead_owner = (state in _RUN_PARKED and isinstance(eng, dict)
                      and isinstance(eng.get("pid"), int)
                      and eng.get("boot") != _ENGINE_BOOT)
        updated = run.get("updated") or run.get("started") or 0
        if not dead_owner and now - updated < stale:
            continue  # too fresh to presume the runtime is dead
        cfg = _read_json(os.path.join(root, entry, "config.json")) or {}
        _finalize_wedged(entry, cfg, run)
        _teardown_orphan_cage(entry, run)
        swept.append(entry)
    return swept


def _best_effort(name: str, what: str, fn: Callable[..., Any], *a, **kw) -> Any:
    """Run one run-finalization cleanup step; log and swallow any failure so the
    rest of the teardown (and the terminal state flip) still happens."""
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        print(f"[finalize] {name}: {what} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def _run_loop_thread(name: str, cfg: dict, entry: dict) -> None:
    persist = FilePersistence(name)
    guardian = Guardian(entry["substrate"], name, _guardian_alert(name))
    # E3: any `loop` step runs as an independent first-class loop (own run/status,
    # separately visible; parent gets its result envelope back). The dispatcher is
    # bound to THIS run's slug so child sessions spawn on the same runner.
    dispatcher = FirstClassSubloopDispatcher(entry.get("slug") or "")
    entry["dispatcher"] = dispatcher                   # loop_stop reaches live sub-loop cages
    result = LoopRunner(cfg, entry["substrate"], entry["owner"], guardian=guardian,
                        should_stop=lambda: entry.get("stop_requested", False),
                        persist=persist, subloop_dispatcher=dispatcher,
                        resume=entry.get("resume")).run()
    _best_effort(name, "substrate shutdown", entry["substrate"].shutdown)
    _best_effort(name, "tmux reap", _reap_loop_tmux, name)  # the loop's isolated tmux session/brief window
    _best_effort(name, "clear live", persist.set_live, [])  # nothing in flight once the run ends
    # A2: if a wedged stop already disowned this thread and a FRESH run replaced
    # our entry, stay silent — writing our terminal state now would clobber the
    # new run (last-writer-wins). Only the current owner of the slot finalizes.
    if _RUNS.get(name) is not entry:
        return
    if getattr(entry["substrate"], "cage_teardown", None) is not None:
        _update_run(name, cage_teardown=entry["substrate"].cage_teardown)
        # Step 5: the cage is down — collect the branch + any uncommitted work
        # BEFORE the reap decides the worktree's fate.
        _best_effort(name, "workspace capture", _capture_loop_workspace, name,
                     entry.get("workspace"), entry["substrate"].cage_teardown)
    if entry.get("stop_requested"):
        final = "stopped"
    else:
        final = "error" if result.ended == "error" else "finished"
    # Deterministic teardown: persist the RESULT, finish-report, and any issue
    # BEFORE flipping to the terminal state, then flip state LAST. Any observer
    # that sees a terminal state (a re-start guard, a finish-report/issue check) is
    # then guaranteed those artifacts are already on disk — closing the intermittent
    # finish-report + restart races (state used to flip first, leaving a window in
    # which the artifacts weren't written yet and the thread wasn't reaped).
    # Every cleanup step is best-effort: a step failing on an already-dead
    # session/tmux must never skip the terminal flip (a run left 'running' is
    # swept to 'error' on the next server start — the #2 teardown wedge).
    _update_run(name, question=None, result=result.as_dict())
    _best_effort(name, "finish-report", _send_finish_report, name, cfg, result,
                 stopped=bool(entry.get("stop_requested")))
    _best_effort(name, "issue", _maybe_file_issue, name, result)
    _update_run(name, state=final)
    # Loop-Workspaces: reap the ephemeral worktree per the trigger policy — remove
    # the dir on finish/stop, KEEP it (branch + working tree) on error for
    # debugging, and never discard uncommitted work. No-op on the legacy path.
    # A STEER keeps the workspace (unknown end-state ⇒ keep) so the restart resumes
    # in the SAME tree, then hands the owner the manager (Better-UX #6).
    _best_effort(name, "workspace reap", _reap_loop_workspace, name, entry,
                 "steered" if entry.get("steer") else final)
    if entry.get("steer"):
        _best_effort(name, "steer handoff", _steer_handoff, name, cfg, result)
    _best_effort(name, "doc fold", _maybe_fold_doc_result, name)   # RESULT ribbon back onto any doc this loop was pointed at
    _best_effort(name, "goodness", _maybe_persist_goodness, name)  # score + persist the finished run (GET stays read-only)


# ── E3: nested loops as INDEPENDENT first-class loops ─────────────────────────
def _child_loop_name(parent_loop: str, sid: str) -> str:
    """On-disk loop name for a first-class sub-loop: ``<parent>.<sid>``. Flat (one
    dir entry under the data root so loop_list / the dashboard see it) and
    self-linking (names its parent and the step). Nesting chains: ``a.b.c``."""
    return f"{parent_loop}.{sid}"


class FirstClassSubloopDispatcher(SubloopDispatcher):
    """Run a ``loop`` step as its OWN first-class loop (E3).

    The child gets its own config.json / run.json / status.jsonl under its own
    status dir, its own FilePersistence + Guardian + HeadlessSubstrate, and shows
    up SEPARATELY in loop_list / the dashboard. The parent only DISPATCHES a task
    and RECEIVES the child's result ENVELOPE + final status back — the child's
    internal turns never touch the parent's status.jsonl (the parent logs one
    dispatch→result link event by child name, in the runner).

    The child runs SYNCHRONOUSLY inside the parent's turn (a ``loop`` step has
    always counted as one parent turn) under a DISTINCT loop name, so it never
    races the parent's run.json / status writes. Recursive: the child is handed
    THIS dispatcher, so grandchildren are first-class too (bounded by
    schema.MAX_LOOP_DEPTH at config-validation time). The child inherits the
    parent's owner channel + cooperative stop, so loop_stop on the parent stops
    the child, and a child manager's ask_owner relays to the same owner.
    """

    def __init__(self, slug: str):
        self.slug = slug
        # child loop name → its substrate while the child runs (grandchildren
        # too: the dispatcher is shared down the tree). loop_stop on the parent
        # shuts these down so a child agent hung mid-turn cannot outlive the stop.
        self._live: dict[str, Any] = {}
        self._live_lock = threading.Lock()

    def shutdown_live(self) -> list[str]:
        """Shut down every running sub-loop substrate, deepest first (Step 5:
        reap its procs, stop its cage slice). Returns the child names."""
        with self._live_lock:
            live = sorted(self._live.items(), key=lambda kv: -kv[0].count("."))
        for _child, sub in live:
            try:
                sub.shutdown()
            except Exception:  # noqa: BLE001 — best-effort
                pass
        return [c for c, _ in live]

    def dispatch(self, *, parent_loop: str, sid: str, child_config: dict,
                 owner, should_stop, clock) -> dict:
        child_name = _child_loop_name(parent_loop, sid)
        # Give the child its own on-disk identity (name = child_name) so its board,
        # report routing, finish-report and envelope are all self-consistent;
        # remember the config's authored name for reference.
        cfg = json.loads(json.dumps(child_config))
        config_name = cfg.get("name")
        cfg["name"] = child_name
        # Persist the child config FIRST: loop_list needs config.json to see the
        # child, and _make_substrate reads it for per-agent timeouts/models.
        _write_json(_config_path(child_name), cfg)
        try:
            render_to_file(cfg, _html_path(child_name))     # block-scheme artifact
        except Exception:  # noqa: BLE001 — the scheme is a bonus, never block dispatch
            pass
        os.makedirs(os.path.join(_output_base(), child_name), exist_ok=True)
        # A fresh dispatch must not surface a prior dispatch's summary/turn counts.
        for stale in _finish_report_paths(child_name):
            try:
                os.remove(stale)
            except OSError:
                pass
        try:
            os.remove(os.path.join(report.status_dir(child_name), "progress.json"))
        except OSError:
            pass
        _update_run(child_name, state="running", started=clock(), slug=self.slug,
                    origin=origins.LOCAL_ORIGIN_ID, parent=parent_loop,
                    parentStep=sid, configName=config_name, kind="subloop",
                    question=None, result=None, finish_report=None,
                    guardian_alert=None, owner_notify=None)
        substrate = _make_substrate(child_name, self.slug)
        # the child's own cage, recorded like a top-level run's so the hard-kill
        # sweep (and the parent's descendant teardown) can find its slice
        _update_run(child_name, isolation=getattr(substrate, "_isolation_status", None),
                    cage_slice=(getattr(getattr(substrate, "_cage", None), "slice", "")
                                or None),
                    cage_teardown=None, cage_capture=None)
        with self._live_lock:
            self._live[child_name] = substrate
        persist = FilePersistence(child_name)
        guardian = Guardian(substrate, child_name, _guardian_alert(child_name))
        try:
            result = LoopRunner(cfg, substrate, owner, guardian=guardian,
                                should_stop=should_stop, persist=persist,
                                subloop_dispatcher=self).run()
        finally:
            with self._live_lock:
                self._live.pop(child_name, None)
            try:
                substrate.shutdown()       # Step 5 barrier even if the runner raised
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass
        if getattr(substrate, "cage_teardown", None) is not None:
            _update_run(child_name, cage_teardown=substrate.cage_teardown)
            _best_effort(child_name, "workspace capture", _capture_loop_workspace,
                         child_name, getattr(substrate, "_loop_workspace", None),
                         substrate.cage_teardown)
        _best_effort(child_name, "clear live", persist.set_live, [])
        stopped = bool(should_stop())
        final = "stopped" if stopped else (
            "error" if result.ended == "error" else "finished")
        # Same deterministic teardown order as _run_loop_thread: artifacts BEFORE
        # the terminal state flip, so any observer that sees a terminal child state
        # is guaranteed the result + finish-report are already on disk.
        _update_run(child_name, question=None, result=result.as_dict())
        _best_effort(child_name, "finish-report", _send_finish_report, child_name, cfg,
                     result, stopped=stopped)
        _best_effort(child_name, "issue", _maybe_file_issue, child_name, result)
        _update_run(child_name, state=final)
        # Loop-Workspaces: a first-class sub-loop provisions its OWN ephemeral
        # worktree (via _make_substrate above, name = <parent>.<sid>), so it must
        # reap it at ITS terminal state too — otherwise sub-loops rebuild exactly
        # the directory graveyard this redesign kills. Same trigger policy as the
        # parent: remove on finish/stop, KEEP on error (keep-on-error), branch
        # survives. The workspace rides on the child's substrate (_loop_workspace);
        # a no-op on the legacy path (flag off / no project). Reap after the state
        # flip, matching _run_loop_thread's deterministic teardown order.
        _best_effort(child_name, "workspace reap", _reap_loop_workspace, child_name,
                     {"workspace": getattr(substrate, "_loop_workspace", None)}, final)
        _best_effort(child_name, "doc fold", _maybe_fold_doc_result, child_name)
        _best_effort(child_name, "goodness", _maybe_persist_goodness, child_name)
        # The result the PARENT receives: the round-2 envelope, turned inward.
        env = _envelope_for(child_name, now=clock())
        return {"child_loop": child_name, "status": env.get("status"),
                "ended": result.ended, "turns_used": result.turns_used,
                "winddown_turns": result.winddown_turns, "envelope": env}


# ── Q3: terminal-fail hook (guardian give-up / error → durable local issue) ──
def _issue_sink() -> issues.IssueSink:
    """The active IssueSink: LocalIssueSink by default; GhIssueSink only when
    explicitly opted in (LOOPS_ISSUE_SINK=gh + LOOPS_GH_ISSUE_REPO). Indirection
    is also the test seam."""
    return issues.select_issue_sink(log=lambda m: print(m))


def _maybe_file_issue(name: str, result: Any) -> None:
    """When a run ends in a genuine failure (guardian gave up, or it errored),
    file a structured local issue with full context {loop, run, failing
    agent/phase/status, guardian attempts, transcript path, mcp_loops.log slice,
    ts}. Best-effort — an issue-filing hiccup must never break run teardown."""
    try:
        if not issues.is_terminal_failure(getattr(result, "ended", "")):
            return
        run_meta = _read_json(_run_path(name)) or {}
        run_key = run_meta.get("started") or run_meta.get("updated")
        # Resolve the crashed loop's confident project so its crash lands in the
        # SAME project-scoped stream as loop-/user-filed issues (§3.4: crash = the
        # one kind the engine files). Unconfident → "Unattributed", never a bucket.
        cfg = _read_json(_config_path(name)) or {}
        project = schema.resolve_project(cfg, loop_name=name)
        issue = issues.build_issue(name, run_key, result, project=project,
                                   log_slice=issues.read_log_slice(name))
        rec = _issue_sink().file_issue(issue)
        if rec:
            print(f"[loops] filed issue {rec.get('id')!r} for failed loop "
                  f"{name!r} (ended={getattr(result, 'ended', '?')})")
    except Exception as e:  # noqa: BLE001 — never let issue-filing crash the thread
        print(f"[loops] issue-filing failed for {name!r}: {type(e).__name__}: {e}")


# a run that ended like this is never "done", whatever the verdict card says
_DOC_NOT_DONE_ENDS = frozenset({"error", "stopped", "guardian_stopped"})


def _doc_result_ribbon(name: str) -> Optional[dict]:
    """The compact RESULT ribbon for a TERMINAL loop, or ``None`` while it is not
    terminal (still running / never ran). Keys off the SAME computed Resolution
    card the loop-detail shows (the §5.4 honesty invariant) and is additionally
    never green on an error/stopped/guardian_stopped end or a non-finished run
    state (loopyard-clear-all-bug-1790578879): only a clean finish is done."""
    run = _read_json(_run_path(name)) or {}
    state = run.get("state")
    if state not in _RUN_TERMINAL:
        return None
    res = run.get("result") if isinstance(run.get("result"), dict) else {}
    ended = res.get("ended") or run.get("ended") or state
    cfg = _read_json(_config_path(name)) or {}
    card = _resolution_card_for(name, cfg)
    verdict = card.get("verdict") or {}
    green = (bool(verdict.get("green")) and state in ("finished", "complete")
             and ended not in _DOC_NOT_DONE_ENDS)
    report_path = next((p for p in _finish_report_paths(name) if os.path.exists(p)),
                       run.get("finish_report") if isinstance(run.get("finish_report"), str)
                       else None)
    summary = (res.get("summary") or res.get("note") or card.get("resolution") or "")
    return {
        "loop": name,
        "ended": ended,
        "green": green,                            # honest: false on any non-clean end
        "verdict": verdict.get("value"),
        "resolution": card.get("resolution"),
        "summary": str(summary)[:200],
        "finishReport": report_path,
        "commit": (card.get("handles") or {}).get("commit"),
        "ts": time.time(),
    }


def _maybe_fold_doc_result(name: str, doc_ids: Optional[list] = None) -> None:
    """§rd-ideahub 2.5 — when a loop that was POINTED at a Hub doc finishes, its
    result "folds back into the doc as a RESULT ribbon" (:func:`_doc_result_ribbon`)
    on every doc whose ``loops`` names this loop (or just ``doc_ids`` — the
    retroactive fold when a loop is pointed AFTER it finished). Best-effort: called AFTER the terminal state flip so the verdict is
    final, and a hiccup here must never break run teardown."""
    try:
        store = ideahub.LocalDocStore(log=lambda m: print(m))
        pointed = [r for r in store.list_docs() if name in (r.get("loops") or [])
                   and (doc_ids is None or r.get("id") in doc_ids)]
        if not pointed:
            return                                     # not an objective's loop — nothing to fold
        ribbon = _doc_result_ribbon(name)
        if ribbon is None:
            return                                     # not terminal yet — nothing honest to fold
        for row in pointed:
            store.update(row["id"], result=ribbon, actor=f"loop:{name}")
    except Exception as e:  # noqa: BLE001 — the RESULT ribbon is a bonus, never blocks teardown
        print(f"[loops] doc result-fold failed for {name!r}: {type(e).__name__}: {e}")


def doc_results_backfill(apply: bool = False, exclude: Optional[list] = None,
                         only: Optional[list] = None) -> dict[str, Any]:
    """One-shot, idempotent backfill for docs pointed at loops that ALREADY
    finished (loopyard-clear-all-bug-1790578879): a doc with no ``result`` whose
    pointed loops are ALL terminal and whose LAST pointed loop finished green gets
    that loop's ribbon (:func:`_doc_result_ribbon`); a doc whose rail row is stale
    (derived state moved, e.g. a green result written before ``done`` existed) is
    re-indexed. Non-green ends, still-running loops and docs that already carry a
    result are left alone, as is a doc the owner Reopened (an owner result action in
    its history → ``skipped: owner_reopened``). DRY-RUN unless ``apply`` — returns the plan either way:
    ``{ok, apply, write:[{id, loop, ended}], reindex:[id], skipped:[{id, why}], unmatched:[id]}``.

    ``exclude`` / ``only`` (doc ids; loopyard-clear-all-follow-up-1790581720) hold
    back a partly-delivered objective whose loop still ended green: an excluded doc
    (or, with ``only``, any doc not listed) is never written NOR re-indexed and shows
    in the plan as ``skipped: excluded``. Ids that match no doc come back in
    ``unmatched``, and an ``apply`` with any unmatched id writes NOTHING
    (``ok: False``) — a typo must not silently green the doc it meant to protect."""
    excl = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    keep = {str(x).strip() for x in (only or []) if str(x).strip()}
    store = ideahub.LocalDocStore(log=lambda m: print(m))
    rows = store.list_docs()
    unmatched = sorted((excl | keep) - {r.get("id") for r in rows})
    if unmatched and apply:
        return {"ok": False, "apply": True, "write": [], "reindex": [], "skipped": [],
                "unmatched": unmatched,
                "error": f"no doc with id {', '.join(unmatched)} — nothing written "
                         "(fix the --exclude/--only ids, or dry-run first)"}
    write, reindex, skipped = [], [], []
    for row in rows:
        did = row.get("id")
        rec = store.get(did) if did else None
        if rec is None:
            continue
        if did in excl or (keep and did not in keep):
            skipped.append({"id": did, "why": "excluded"})
            continue
        loops = [l for l in (rec.get("loops") or []) if isinstance(l, str) and l]
        if rec.get("result") is None and loops and any(
                h.get("actor") == ideahub.OWNER_ACTOR and "result" in (h.get("changed") or [])
                for h in (rec.get("history") or []) if isinstance(h, dict)):
            # the owner already ruled on this doc's result (Reopen) — never re-green it
            skipped.append({"id": did, "why": "owner_reopened"})
        elif rec.get("result") is None and loops:
            ribbons = {l: _doc_result_ribbon(l) for l in loops}
            live = [l for l, r in ribbons.items() if r is None]
            last = ribbons[loops[-1]]
            if live:
                skipped.append({"id": did, "why": f"loop not finished: {', '.join(live)}"})
            elif not last["green"]:
                skipped.append({"id": did, "why": f"{loops[-1]} ended {last['ended']} (not green)"})
            else:
                write.append({"id": did, "loop": loops[-1], "ended": last["ended"]})
                if apply:
                    store.update(did, result=last, actor=f"loop:{loops[-1]}")
                continue
        if row.get("state") != ideahub.derive_state(rec):
            reindex.append(did)
            if apply:
                store.reindex(did)
    return {"ok": True, "apply": bool(apply), "write": write, "reindex": reindex,
            "skipped": skipped, "unmatched": unmatched}


# ── config tools ──────────────────────────────────────────────────────────────
def _unwrap_loop_config(config: Any) -> tuple[Any, str]:
    """Accept both a bare loop config and an unambiguous ``{"config": {...}}``
    double-wrap. Returns ``(config, warning)`` — warning is "" unless we
    unwrapped. Only unwraps when the OUTER dict is exactly ``{"config": <dict>}``
    and lacks a top-level ``name`` (so a legitimate field named "config" is never
    stolen). Non-dict input is returned untouched for the validator to reject."""
    if (isinstance(config, dict) and "name" not in config
            and set(config) == {"config"} and isinstance(config["config"], dict)):
        return config["config"], ("input was double-wrapped as {\"config\": {...}}; "
                                  "unwrapped it — pass the bare config directly next time")
    return config, ""


@mcp.tool()
def loop_save(config: dict) -> dict[str, Any]:
    """Validate a loop config (schema.validate_config) and persist the
    NORMALIZED form (defaults filled) to data/_loops/<name>/config.json.
    Invalid configs are not persisted. Returns {ok, name, path, warnings} or
    {"error", errors}."""
    def work():
        # CLI ergonomics: accept a BARE config OR an unambiguous {"config": {...}}
        # double-wrap (a common shim/copy-paste mistake). Unwrapping it — instead
        # of failing with a cryptic "name is required" — means the same input
        # works whether or not the caller wrapped it. Warn so it's never silent.
        cfg_in, unwrap_warn = _unwrap_loop_config(config)
        try:
            resolved = _resolve_agent_refs(json.loads(json.dumps(cfg_in)), time.time())
        except (ValueError, TypeError) as exc:
            return {"error": f"agentRef resolution failed: {exc}"}
        res = validate_config(resolved)
        if not res["ok"]:
            return {"error": "invalid config", "errors": res["errors"]}
        name = res["config"]["name"]
        bad = _check_name(name)
        if bad:
            return bad
        path = _write_json(_config_path(name), res["config"])
        # S-1 (b): the loopId sidecar lives BESIDE config.json (validate_config's
        # whitelist would drop it from the config); not surfaced in the reply.
        _sync_loop_id(name)
        # Pillar 3 output contract: every loop gets an OUTPUT FOLDER at creation
        # and the manager is told its path. Host-resolved + created here, but NOT
        # persisted into the (portable) config — resolve at run time on each box.
        out_dir = os.path.join(_output_base(), name)
        os.makedirs(out_dir, exist_ok=True)
        # north star §4: every agent seen in a loop is auto-materialized into
        # the registry so its detail view works. Runs on save so the agent nav
        # is never empty. Existing curated records are left untouched by
        # _materialize_one (create-only).
        materialized: list[str] = []
        try:
            now = time.time()
            for agent_id in _agent_ids_in_config(res["config"]):
                r = _materialize_one(agent_id, now=now)
                if r.get("created"):
                    materialized.append(agent_id)
        except Exception:  # noqa: BLE001 — never block loop_save on registry writes
            materialized = []
        warnings = list(res["warnings"])
        if unwrap_warn:
            warnings.insert(0, unwrap_warn)
        return {"ok": True, "name": name, "path": path,
                "outputDir": out_dir, "warnings": warnings,
                "materializedAgents": materialized}
    return _run(work)


@mcp.tool()
def loop_lint(config: dict) -> dict[str, Any]:
    """Check a loop config against the authoring RULES (Pillar 2) — advisory
    findings the editor / loop-creator surface: no todos in agent persona/goal,
    product/path detail belongs in the loop goal, goals stay loop-agnostic.
    Also runs schema.validate_config so structural errors come back too. Returns
    {ok, errors, warnings, lints:[{level,rule,where,message}], rules}."""
    def work():
        res = validate_config(config)
        target = res["config"] if res["ok"] else (config if isinstance(config, dict) else {})
        lints = authoring.lint_config(target)
        return {"ok": res["ok"], "errors": res["errors"],
                "warnings": res["warnings"], "lints": lints,
                "rules": authoring.RULES}
    return _run(work)


@mcp.tool()
def loop_schematic(name: str, owner: str = "") -> dict[str, Any]:
    """The SCHEMATIC view (Pillar 2) for a saved loop: every field of its JSON,
    labeled + sectioned (identity, caps, budget, human-in-loop, steps with
    role/model/version-pin, stepOrder with parallel groups, derived numbers).
    Returns the schematic or {"error": ...}.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        cfg = _read_json(_config_path(name))
        if cfg is None:
            return {"error": f"loop {name!r} not found"}
        return authoring.schematic(cfg)
    return _run(work)


@mcp.tool()
def loop_render(name: str, owner: str = "") -> dict[str, Any]:
    """Render a saved loop's HTML block-scheme (render.render_to_file) next to
    its JSON. Returns {ok, json_path, html_path} — ship both to the owner with
    telegram_bridge.send_loop_scheme / tg_send_document.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        cfg = _read_json(_config_path(name))
        if cfg is None:
            return {"error": f"loop {name!r} has no saved config (loop_save first)"}
        html = render_to_file(cfg, _html_path(name))
        return {"ok": True, "json_path": _config_path(name), "html_path": html}
    return _run(work)


@mcp.tool()
def loop_list(include_archived: bool = False,
              origin: str = origins.LOCAL_ORIGIN_ID,
              owner: str = "") -> dict[str, Any]:
    """List saved loops (dirs with a config.json) with their latest run state,
    scoped to a single ORIGIN mirror (default ``local``). Archived loops are
    hidden unless ``include_archived=True``. Returns ``{loops: [{name, state,
    updated, archived, origin}], count, archived_count, origin}``.

    To fan out across every origin visible from this box use
    :func:`loop_list_all` — this tool intentionally stays single-origin so a
    caller can page/scope predictably.

    ``owner`` (optional, Phase 5 seam) keeps only that tenant owner's loops
    (config ``owner``, else the local default owner) and echoes ``owner``. Unset →
    unchanged; rows carry ``owner`` only when a non-default owner exists."""
    owner = (owner or "").strip()

    def work():
        root = _data_root_for(origin)
        if root is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        loops = []
        archived_count = 0
        for entry in (sorted(os.listdir(root)) if os.path.isdir(root) else []):
            if entry.startswith("_"):
                continue                    # skip _registry and other private dirs
            cfg_path = os.path.join(root, entry, "config.json")
            cfg = _read_json(cfg_path)
            if cfg is None:
                continue
            row_owner = schema.resolve_owner(cfg)
            if owner and row_owner != owner:
                continue                    # another owner's loop: not counted
            run_path = os.path.join(root, entry, "run.json")
            state = _read_json(run_path) or {}
            archived = bool(state.get("archived"))
            if archived:
                archived_count += 1
                if not include_archived:
                    continue
            loops.append({"name": entry,
                          "state": state.get("state", "saved"),
                          "updated": state.get("updated"),
                          "archived": archived,
                          "origin": origin,
                          # loop-of-1 primitive + confident (never git-derived)
                          # project attribution; None → surface shows "Unattributed".
                          "single_agent": schema.is_single_agent(cfg),
                          "project": schema.resolve_project(cfg, loop_name=entry),
                          "owner": row_owner})
        loops = schema.owner_scope(loops, owner or None)
        out = {"loops": loops, "count": len(loops),
               "archived_count": archived_count, "origin": origin}
        if owner:
            out["owner"] = owner
        return out
    return _run(work)


@mcp.tool()
def loop_list_all(include_archived: bool = False,
                  owner: str = "") -> dict[str, Any]:
    """Fan-out list: every loop on every ORIGIN visible from this box (local +
    mirror siblings). Returns ``{loops:[{origin, name, state, updated,
    archived}], origins:[<ids>], count, archived_count}``. This is the tool the
    consolidated multi-host web app calls to render a single unified list.

    ``owner`` (optional, Phase 5 seam) scopes rows to that tenant owner, same
    rule and output policy as :func:`loop_list`."""
    owner = (owner or "").strip()

    def work():
        now = time.time()
        seen_origins = origins.list_origins(_local_mirror_path(), now=now)
        # keyed on (origin, name) so a CONNECTED origin's LIVE event-stream row
        # (G2.1) can supersede its rsync-mirror row below; mirror-only loops
        # (unenrolled origins, or loops an origin hasn't pushed an event for yet)
        # pass through unchanged.
        rows: dict[tuple, dict] = {}
        mirror_owner: dict[tuple, str] = {}   # owner of every mirror loop, pre-filter
        archived_count = 0
        for o in seen_origins:
            root = o["path"]
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                if entry.startswith("_"):
                    continue
                cfg_path = os.path.join(root, entry, "config.json")
                cfg = _read_json(cfg_path)
                if cfg is None:
                    continue
                row_owner = schema.resolve_owner(cfg)
                mirror_owner[(o["id"], entry)] = row_owner
                if owner and row_owner != owner:
                    continue
                state = _read_json(os.path.join(root, entry, "run.json")) or {}
                archived = bool(state.get("archived"))
                if archived:
                    archived_count += 1
                    if not include_archived:
                        continue
                # gap #3: the Hub's saved copy of a loop it dispatched to a
                # remote origin belongs to THAT origin (a live row supersedes).
                row_origin = (_dispatched_origin(os.path.join(root, entry))
                              if o["id"] == origins.LOCAL_ORIGIN_ID else None) or o["id"]
                mirror_owner[(row_origin, entry)] = row_owner
                rows[(row_origin, entry)] = {
                    "name": entry, "origin": row_origin,
                    "state": state.get("state", "saved"),
                    "updated": state.get("updated"),
                    "archived": archived, "source": "mirror",
                    "single_agent": schema.is_single_agent(cfg),
                    "project": schema.resolve_project(cfg, loop_name=entry),
                    "owner": row_owner}
        # G2.1 — overlay the LIVE event-stream loops from CONNECTED origins. A live
        # (origin, name) SUPERSEDES the mirror row (freshness comes from pushed
        # run/turn/status events, not directory mtime). An archived mirror row
        # stays hidden unless include_archived (a live event never un-archives).
        live_origins = set()
        for lo in _live_snapshot()["loops"]:
            name = lo.get("name")
            origin = lo.get("origin")
            if not name or not origin:
                continue
            live_origins.add(origin)
            prior = rows.get((origin, name))
            # S-0 (§7.3): a row from the origin's own loop.list inventory is
            # the origin's authoritative view — it carries its facets, its
            # deviceId and the ENROLLED owner, and (loop.list omits archived
            # loops) it is not archived, whatever a stale mirror copy says.
            inv = bool(lo.get("inventory"))
            if prior and prior.get("archived") and not include_archived \
                    and not inv:
                continue
            # the live stream carries no config: inherit the mirror row's owner,
            # else the record's own, else the local default (read-time rule).
            if inv and lo.get("owner"):
                live_owner = lo["owner"]
            else:
                live_owner = (mirror_owner.get((origin, name))
                              or schema.resolve_owner(lo))
            if owner and live_owner != owner:
                continue
            if inv:
                stamps = [t for t in (lo.get("lastTs"), lo.get("updated"))
                          if isinstance(t, (int, float))]
                rows[(origin, name)] = {
                    "name": name, "origin": origin,
                    "state": lo.get("state") or "saved",
                    "updated": max(stamps) if stamps else (
                        lo.get("updated") or (prior or {}).get("updated")),
                    "archived": bool(lo.get("archived")),
                    "single_agent": lo.get("single_agent",
                                           (prior or {}).get("single_agent")),
                    "project": lo.get("project", (prior or {}).get("project")),
                    "source": "live", "owner": live_owner,
                    "deviceId": lo.get("deviceId"),
                    "fetchedAt": lo.get("fetchedAt"),
                    "stale": bool(lo.get("stale"))}
                continue
            rows[(origin, name)] = {
                "name": name, "origin": origin,
                "state": lo.get("state", (prior or {}).get("state", "running")),
                "updated": lo.get("lastTs") or (prior or {}).get("updated"),
                "archived": bool((prior or {}).get("archived")),
                # carry the mirror row's facets forward (the live event stream
                # doesn't re-send config); absent mirror → unknown, honestly.
                "single_agent": (prior or {}).get("single_agent"),
                "project": (prior or {}).get("project"),
                "source": "live", "owner": live_owner}
        loops = sorted(rows.values(),
                       key=lambda l: (l.get("updated") or 0), reverse=True)
        loops = schema.owner_scope(loops, owner or None)
        origin_ids = sorted({o["id"] for o in seen_origins} | live_origins)
        out = {"loops": loops, "origins": origin_ids,
               "count": len(loops), "archived_count": archived_count}
        if owner:
            out["owner"] = owner
        return out
    return _run(work)


@mcp.tool()
def loop_get(name: str, origin: str = origins.LOCAL_ORIGIN_ID,
             owner: str = "") -> dict[str, Any]:
    """Fetch one loop: its normalized config + run state, from a specific
    ORIGIN mirror (default ``local``). Unknown name / unknown origin →
    ``{"error": ...}``. The result carries ``origin`` so callers don't have to
    remember what they asked for.

    ``owner`` (optional, Phase 5 seam): when set and the loop belongs to another
    owner, the read is refused (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        cfg_path = _config_path_for(origin, name)
        if cfg_path is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        cfg = _read_json(cfg_path)
        if cfg is None:
            return {"error": f"loop {name!r} not found on origin {origin!r}"}
        refused = schema.owner_refusal(name, cfg, owner)
        if refused:
            return refused
        run_path = _run_path_for(origin, name)
        return {"name": name, "origin": origin, "config": cfg,
                "run": _read_json(run_path) or {"state": "saved"},
                "single_agent": schema.is_single_agent(cfg),
                "project": schema.resolve_project(cfg, loop_name=name),
                # demoted inference (loopyard-bug-1790177434): a guess the
                # surface OFFERS for an unbound loop, never renders as the project
                "suggested_project": schema.suggest_project(cfg, loop_name=name)}
    return _run(work)


# ── run tools ─────────────────────────────────────────────────────────────────
# G2.2 / Phase-A M2 — the enrolled-origin dispatch seam.
#
# This is THE ONE in-process coupling the split hardens (HUB-ENGINE-SPLIT-SPEC
# §1d "the single seam that proves the tangle", §4 M2). When a local origin
# service is installed AND live, a default loop_start routes THROUGH the origin
# protocol to the box's own engine over the origin_wire channel (loopback in
# one-process mode) rather than the old hardcoded LOCAL path.
#
# M2 boundary rule: the engine depends ONLY on the wire-shaped ``OriginDispatch``
# contract below — it NEVER imports the live-fabric Hub façade (OriginHub /
# DispatchRouter / control_plane, or the LocalOriginService that embeds them).
# The concrete client is dependency-injected by the hub-side connector
# (``mcp_loops.origin_service`` → :func:`install_origin_dispatch`) and reaches
# this engine back over the loopback wire; the engine holds only the abstraction.
# ``mcp_loops.tests.test_engine_hub_boundary`` enforces this statically.
#
# Default None keeps the pre-Origins behaviour verbatim (back-compat + the §3
# standalone default: no dispatch service ⇒ loop_start stays on _loop_start_local,
# so a Hub-less deploy runs byte-for-byte as before).


class OriginDispatch(Protocol):
    """The narrow, wire-shaped contract the engine depends on to reach its own
    origin over ``origin_wire`` — the ONLY surface ``loop_start`` (and the §1d
    Hub-facing tools that still answer on :8771) know about.

    Structural-typing only (never isinstance-checked at runtime): it documents
    and pins the seam so the engine talks to an *abstraction*, not a Hub object.
    The concrete implementation today is the hub-side
    ``origin_proto.dispatch.LocalOriginService`` (which owns the OriginHub +
    ChannelClient and speaks the loopback channel), injected via
    :func:`install_origin_dispatch`; but the engine core never names it.
    """

    def is_live(self) -> bool:
        """True iff a dispatch will actually reach an engine over a live channel."""
        ...

    def dispatch_start(self, name: str, slug: str = "") -> dict:
        """Route a ``loop.start`` to the enrolled origin over the wire (fresh
        dispatch-id, idempotent on redelivery) and return the engine's result."""
        ...


    # Optional (duck-typed via getattr): ``dispatch_capabilities_probe(origin,
    # *, timeout)`` — ask a LIVE origin for its own CLI probe over the wire
    # (``capabilities.probe``). A client without it leaves remote origins on the
    # honest not-connected reason.


_ORIGIN_DISPATCH: Optional[OriginDispatch] = None


def install_origin_dispatch(service: OriginDispatch) -> None:
    """Install the local origin dispatch client so loop_start routes through the
    enrolled origin over the wire. Idempotent for the same object. Called by the
    hub-side connector (origin_service) — the sanctioned M2 wiring point; the
    engine core never constructs or imports the Hub façade itself."""
    global _ORIGIN_DISPATCH
    _ORIGIN_DISPATCH = service


def uninstall_origin_dispatch() -> None:
    global _ORIGIN_DISPATCH
    _ORIGIN_DISPATCH = None


def origin_dispatch() -> Optional[OriginDispatch]:
    return _ORIGIN_DISPATCH


# The engine ↔ standalone-Hub bridge (``origin_proto.hub_control.
# HubServeDispatch``): in prod the remote origins hold their channel to
# ``hub_serve`` (a separate process behind /hub), not to the in-process hub
# above. Installed by origin_service when LOOPYARD_HUB_CONTROL_SOCK is set; it
# carries REMOTE-origin ops only (start/status/probe/events/snapshot — no exec).
_HUB_BRIDGE: Optional[Any] = None


def install_hub_bridge(bridge: Any) -> None:
    global _HUB_BRIDGE
    _HUB_BRIDGE = bridge


def uninstall_hub_bridge() -> None:
    global _HUB_BRIDGE
    _HUB_BRIDGE = None


def _remote_dispatch(origin: str = "") -> Optional[Any]:
    """The dispatch service that reaches REMOTE ``origin``: the in-process hub
    when the origin is connected there, else the hub_serve bridge when it is
    connected THERE, else the first one installed (whose call then raises the
    honest not-connected reason). None when neither is installed."""
    cands = [c for c in (_ORIGIN_DISPATCH, _HUB_BRIDGE)
             if c is not None and hasattr(c, "dispatch_start_remote")]
    if origin and len(cands) > 1:
        for c in cands:
            try:
                c.resolve_device(origin)
                return c
            except Exception:  # noqa: BLE001 — not connected on this one
                continue
    return cands[0] if cands else None


def _live_snapshot() -> dict[str, list]:
    """G2.1 — the LIVE consolidation snapshot the dashboard read path fuses OVER
    the rsync mirror: ``{origins, loops}`` from CONNECTED origins' heartbeats +
    pushed event stream, or empty lists when no origin-agent is installed/live (so
    every read cleanly falls back to the pure-mirror view). Never raises."""
    out: dict[str, list] = {"origins": [], "loops": []}
    # the in-process hub first (it also fuses the mirror); the hub_serve bridge
    # adds only origins / loops the first did not already report.
    for svc, mirror in ((_ORIGIN_DISPATCH, True), (_HUB_BRIDGE, False)):
        if svc is None:
            continue
        try:
            snap = svc.consolidated_snapshot(
                mirror_root=_local_mirror_path() if mirror else None)
        except Exception:  # noqa: BLE001 — a live-read hiccup must never sink the list
            continue
        if not isinstance(snap, dict):
            continue
        have = {o.get("id") for o in out["origins"] if o.get("live")}
        for o in snap.get("origins") or []:
            if o.get("id") in have:
                continue
            out["origins"] = [x for x in out["origins"] if x.get("id") != o.get("id")]
            out["origins"].append(o)
        seen = {(lo.get("origin"), lo.get("name")) for lo in out["loops"]}
        out["loops"] += [lo for lo in snap.get("loops") or []
                         if (lo.get("origin"), lo.get("name")) not in seen]
    return out


def _enrolled_owner(device_id: Any) -> Optional[str]:
    """Phase 5: the P3 ENROLLED owner of a live device by ``deviceId`` through
    the one resolver (:func:`enrollment.device_owner`). H2: when a hub_serve Hub
    is bridged, ITS EnrollmentStore — where remote origins actually enroll —
    answers first (over the v2 ``device.get`` op); the in-process hub's own
    ``_origin`` store answers for this box's own device / an old Hub. None when
    no store knows the device. Never raises (a store hiccup must not sink the
    origin list)."""
    if not device_id:
        return None
    getter = getattr(_HUB_BRIDGE, "device_get", None)
    if getter is not None:
        try:
            owner = (getter(str(device_id)) or {}).get("owner")
        except Exception:  # noqa: BLE001 — Hub down / pre-v2: fall back below
            owner = None
        if owner:
            return str(owner)
    store = getattr(_ORIGIN_DISPATCH, "store", None)
    if store is None:
        return None
    try:
        rec = store.get_device(str(device_id))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rec, dict):
        return None
    from mcp_loops.origin_proto.enrollment import device_owner
    return device_owner(rec)


def _install_root() -> str:
    """The install dir a loop's setup + spawned agents resolve
    ``$LOOPYARD_INSTALL/...`` against — the blessed env if set (matches the env
    the worker daemon exports into every session), else the package's install
    root. One source of truth so a `setup` scaffold lands exactly where the
    agents will look for it."""
    return os.environ.get("LOOPYARD_INSTALL") or str(paths.install_root())


def _run_loop_setup(cfg: dict) -> Optional[dict[str, Any]]:
    """Run a loop's declared one-time SETUP (A1) on THIS origin before the first
    turn. Returns an ``{"error": ...}`` to ABORT the launch, or ``None`` when
    there's nothing to do / it succeeded.

    Safe by construction: ``setup.script``/``setup.ensure`` are schema-enforced to
    be install-relative (no absolute, no ``..``); here we re-resolve against the
    install root and refuse anything that escapes it (defence in depth). ``ensure``
    short-circuits so the (idempotent) script runs at most once — a fresh clone
    seeds the fixture, a re-run is a no-op."""
    setup = cfg.get("setup")
    if not isinstance(setup, dict):
        return None
    install = os.path.realpath(_install_root())
    # R25: with a separate state root (LOOPYARD_HOME) the scaffold — ``ensure``,
    # the script's cwd and its LOOPYARD_INSTALL — lands there, never in the
    # (read-only) code tree. Unset ⇒ the install root, exactly as before.
    state = (os.path.realpath(str(paths.state_root()))
             if os.environ.get(paths.ENV_HOME) else install)

    def _under_install(rel: str, base: str = install) -> Optional[str]:
        cand = os.path.realpath(os.path.join(base, rel))
        if cand != base and not cand.startswith(base + os.sep):
            return None
        return cand

    ensure = setup.get("ensure")
    if ensure:
        target = _under_install(ensure, state)
        if target is None:
            return {"error": f"setup.ensure {ensure!r} escapes the install root"}
        if os.path.exists(target):
            return None  # already scaffolded — idempotent no-op
    script = setup.get("script")
    if not script:
        return None
    script_path = _under_install(script)
    if script_path is None:
        return {"error": f"setup.script {script!r} escapes the install root"}
    if not os.path.isfile(script_path):
        return {"error": f"setup script {script!r} not found under install {install!r}"}
    try:
        subprocess.run(
            ["bash", script_path], cwd=state, check=True,
            env={**os.environ, "LOOPYARD_INSTALL": state},
            capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return {"error": f"setup script {script!r} failed: {detail}"}
    except Exception as exc:  # noqa: BLE001 — never leak a raw trace to the caller
        return {"error": f"setup script {script!r} error: {exc}"}
    return None


# Per-loop-name start lock (loopworkspaces-remediate-follow-up-1790341351): the
# liveness check → substrate/provision → _RUNS registration in _loop_start_local
# is one critical section, so two concurrent same-name starts can't both pass the
# 'already running' gate and double-provision / double-run one loop. Re-entrant
# so a nested same-thread start can't self-deadlock; distinct names never contend.
_START_LOCKS: dict[str, threading.RLock] = {}
_START_LOCKS_GUARD = threading.Lock()


def _start_lock(name: str) -> "threading.RLock":
    with _START_LOCKS_GUARD:
        lk = _START_LOCKS.get(name)
        if lk is None:
            lk = _START_LOCKS[name] = threading.RLock()
        return lk


def _loop_start_local(name: str, slug: str = "", *,
                      dispatch_id: Optional[str] = None,
                      adopt_manager_sid: Optional[str] = None) -> dict[str, Any]:
    """Serialised per loop name — see :data:`_START_LOCKS`."""
    bad = _check_name(name)
    if bad:
        return bad
    with _start_lock(name):
        return _loop_start_local_locked(name, slug, dispatch_id=dispatch_id,
                                        adopt_manager_sid=adopt_manager_sid)


def _loop_start_local_locked(name: str, slug: str = "", *,
                             dispatch_id: Optional[str] = None,
                             adopt_manager_sid: Optional[str] = None) -> dict[str, Any]:
    """The RAW in-process launch — execution stays on THIS origin's engine. This
    is the body loop_start used to inline; it is now a helper so BOTH the
    back-compat local path and the origin-agent's dispatched loop.start (which
    arrives carrying ``dispatch_id``) converge on the SAME launcher instead of
    re-routing. Returns {ok, name, state, dispatchId?} or {"error": ...}."""
    bad = _check_name(name)
    if bad:
        return bad
    cfg = _read_json(_config_path(name))
    if cfg is None:
        return {"error": f"loop {name!r} has no saved config (loop_save first)"}
    # A1 — run the loop's declared one-time SETUP on THIS (executing) origin
    # before the first turn, so a flagship example seeds its throwaway fixture
    # hands-free on a fresh clone. Idempotent + install-scoped; a failure aborts
    # the launch with a clear error rather than a silently no-op'd run.
    setup_err = _run_loop_setup(cfg)
    if setup_err:
        return setup_err
    # Pin the daemon slug: an explicit caller arg wins; else the loop's own config
    # `slug` (a loop can target a dedicated worktree slug); else the slug the
    # briefed session was spawned under (so Run adopts into the SAME repo the
    # manager was briefed in — manager tmux + headless workers stay co-located).
    # Any of these is treated as explicit → overrides the service-wide LOOPS_SLUG
    # default; none set ⇒ resolve exactly as before.
    _bs = _read_json(_brief_session_path(name)) or {}
    _pinned = slug or cfg.get("slug") or (_bs.get("slug") if _bs.get("sid") else "") or ""
    resolved_slug, slug_err = _resolve_slug(_pinned, cfg)
    if slug_err:
        return slug_err
    # If no explicit adoption but the owner briefed a session for this loop
    # (loop_brief_session), adopt it now so Run continues from that dialogue.
    if not adopt_manager_sid and _bs.get("sid"):
        adopt_manager_sid = _bs["sid"]
    # Session ADOPTION: bind an owner-briefed live session into the manager
    # slot so it CONTINUES as the manager (its dialogue is the context).
    _adopt = None
    _adopt_note = (_adopt_refusal(name, _bs, adopt_manager_sid, cfg)
                   if adopt_manager_sid else None)
    if _adopt_note:
        print(f"[isolation] {name}: {_adopt_note}", file=sys.stderr)
        adopt_manager_sid = None
    if adopt_manager_sid:
        _mgr = next((_aid for _aid, _st in (cfg.get("steps") or {}).items()
                     if isinstance(_st, dict) and _st.get("type") == "agent"
                     and _st.get("role") == "manager"), None)
        if not _mgr:
            return {"error": f"loop {name!r} has no manager step to adopt a session into"}
        _adopt = {_mgr: adopt_manager_sid}
    entry = _RUNS.get(name)
    if entry and entry["thread"].is_alive():
        cur = (_read_json(_run_path(name)) or {}).get("state")
        if cur in _RUN_TERMINAL:
            entry["thread"].join(timeout=_stop_grace())
        elif entry.get("stop_requested"):
            return {"error": f"loop {name!r} is still stopping, retry in a moment"}
        else:
            return {"error": f"loop {name!r} is already running"}
    # keep the non-adoption call byte-identical (tests swap a 2-arg FakeSubstrate
    # via this seam); only pass the new kwarg when a session is actually adopted.
    # A fail-closed workspace provisioning error (isolation required, provision
    # failed) surfaces as a clean start refusal rather than a crashed thread.
    try:
        _sub = (_make_substrate(name, resolved_slug, adopt_sessions=_adopt)
                if _adopt else _make_substrate(name, resolved_slug))
    except (workspaces.WorkspaceError, sandbox.IsolationRefused) as exc:
        return {"error": str(exc)}
    except ValueError as exc:       # agent runtime/model off the spawn allowlist
        return {"error": f"cannot start {name!r}: {exc}"}
    if _adopt_note and isinstance(getattr(_sub, "_isolation_status", None), list):
        _sub._isolation_status.append(_adopt_note)  # type: ignore[attr-defined]
    entry = {"substrate": _sub,
             "owner": _make_owner(name), "stop_requested": False,
             "slug": resolved_slug,
             # Loop-Workspaces: the provisioned worktree (or None on the legacy
             # path) travels on the entry so the run-thread reaps it at teardown.
             "workspace": getattr(_sub, "_loop_workspace", None)}
    # ADDITIVE (SLICE-1 §4.2, R2): capture the strongest negative signal — a human
    # RE-RUNNING a finished loop — at the choke point EVERY start origin funnels
    # through (dashboard, start_loop app-connect, the CLI/MCP loop_start, an
    # attached-loop start), BEFORE the finish-report + run.json below erase the
    # prior run's evidence. This is an INSERTION that changes no existing behavior:
    # a pure fail-soft append to a sibling log, keyed to the PRIOR run. The guard
    # fires only on a FINISHED run about to be re-run (a truthy `result`); the
    # still-alive early-return above means a mid-run start never reaches here.
    _prior = _read_json(_run_path(name))
    # Better-UX #6: a READY steer makes this start a RESUME from the same point —
    # the runner continues the turn budget and the manager gets the steer context.
    # A steer restart is not a re-run (no negative re-run disposition).
    _steer = _read_json(_steer_path(name)) or {}
    _resume = None
    if _steer.get("phase") == "ready":
        _resume = {"turnsUsed": _steer.get("turnsUsed") or 0,
                   "context": _steer.get("context") or ""}
        entry["resume"] = _resume
    if _resume is None and _prior and _prior.get("result"):
        _record_disposition(name, "re-run", source="behavior", origin="engine",
                            started=_prior.get("started"),
                            deliverable_ref=_deliverable_ref(name, _prior))
    for stale in _finish_report_paths(name):
        try:
            os.remove(stale)
        except OSError:
            pass
    try:
        os.remove(os.path.join(report.status_dir(name), "progress.json"))
    except OSError:
        pass
    try:
        os.remove(_dispatched_path(name))    # runs HERE now, not on a remote origin
    except OSError:
        pass
    _started = time.time()
    _update_run(name, state="running", started=_started, slug=resolved_slug,
                # H7: this run's identity — every v2 status row carries it;
                # S-1/D5: uuid5(NS_RUN, loopId/started) of the SAME started float
                runId=_mint_run_id(name, _started),
                origin=origins.LOCAL_ORIGIN_ID, engine=_engine_identity(),
                question=None, result=None, finish_report=None,
                git_commit=None, steer=None,
                # a fresh start clears the prior run's guardian give-up (else the
                # dashboard shows a false 'stuck' banner on the new run)
                guardian_alert=None, owner_notify=None,
                isolation=getattr(_sub, "_isolation_status", None),
                cage_slice=(getattr(getattr(_sub, "_cage", None), "slice", "") or None),
                cage_teardown=None, cage_capture=None)
    if _resume is not None:
        _steer.update(phase="restarted", restartedAt=time.time())
        _write_json(_steer_path(name), _steer)
        _update_run(name, steer=_steer_public(_steer))
    t = threading.Thread(target=_run_loop_thread, args=(name, cfg, entry),
                         name=f"loop-{name}", daemon=True)
    entry["thread"] = t
    _RUNS[name] = entry
    t.start()
    if _adopt:  # consumed the briefed session; a re-run must not re-adopt a dead sid
        # Remember WHICH manager this run adopted so Debrief can resume/resurrect it
        # after the run ends (carry-over). Written before clearing the sidecar.
        _write_json(_last_manager_path(name),
                    {"sid": adopt_manager_sid, "slug": resolved_slug,
                     "window": _bs.get("window") or f"brief-{name}"[:60], "at": time.time(),
                     "cage_slice": _bs.get("cage_slice")})
        try:
            os.remove(_brief_session_path(name))
        except OSError:
            pass
    out = {"ok": True, "name": name, "state": "running"}
    if dispatch_id:
        out["dispatchId"] = dispatch_id
    return out


def _daemon_call(action: str, params: dict, timeout: float = 30.0) -> dict:
    """Call the ATTACHED worker daemon's action over its unix socket (the same
    transport HeadlessSubstrate uses). Raises if no runner is attached."""
    import httpx
    from mcp_loops import headless as _headless
    runner = runner_registry.read() or {}
    # Same fallback the substrate uses: the attached-runner sock if present, else
    # the install's DEFAULT_SOCK (an empty registry does NOT mean no daemon — the
    # daemon still listens on the default path and every loop reaches it there).
    sock = runner.get("sock") or str(_headless.DEFAULT_SOCK)
    tr = httpx.HTTPTransport(uds=str(sock))
    with httpx.Client(transport=tr, base_url="http://w", timeout=timeout) as c:
        r = c.post(f"/actions/{action}", json=params)
        r.raise_for_status()
        return r.json() if r.content else {}


def _brief_session_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "brief_session.json")


def _last_manager_path(name: str) -> str:
    """Records the manager session a run ADOPTED (sid+slug), so loop_debrief_session
    can resume/resurrect that exact manager after the run finished + the sidecar was
    consumed — the carry-over path (Debrief continues the manager that did the work)."""
    return os.path.join(report.status_dir(name), "last_manager.json")


def _session_alive(sid: str) -> bool:
    """True iff the tmux pane behind this sid still exists (sid format
    S-<user>-<window>-p<pane_no>; the live pane id is %<pane_no>)."""
    if not sid or "-p" not in sid:
        return False
    try:
        import subprocess
        pane = "%" + sid.rsplit("-p", 1)[1]
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                             capture_output=True, text=True, timeout=5)
        return out.returncode == 0 and pane in out.stdout.split()
    except Exception:  # noqa: BLE001 — aliveness is best-effort; unknown => spawn fresh
        return False


# ── per-loop tmux isolation + reaping ────────────────────────────────────────
# Managers and brief sessions used to all spawn into the ONE shared slug tmux
# session (e.g. `swarmdev`), so attaching showed every loop's manager at once and
# stopped/re-briefed loops piled up orphan windows (the "swarmdev graveyard").
# Fix: move each loop's window into its OWN `yard-<name>` session, and reap that
# session (plus any stray `brief-<name>` window) on stop/finish/re-brief.
def _tmux(*args: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Best-effort local tmux command → (ok, combined-output)."""
    try:
        import subprocess
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
    except Exception:  # noqa: BLE001
        return (False, "")


def _yard_session(name: str) -> str:
    """The per-loop tmux session that ISOLATES this loop's manager/brief window so
    attaching shows only THIS loop — never the shared slug's pile."""
    return f"yard-{name}"[:60]


def _isolate_window(slug: str, window: str, name: str) -> bool:
    """Move a just-spawned brief/manager window OUT of the shared slug session into
    the loop's own `yard-<name>` session. move-window PRESERVES the pane id (= sid),
    so inject/adoption still target the same pane. Drops the stray default window
    the new session creates so it holds ONLY this loop. Fail-soft: any tmux error
    leaves the window where it is and the caller falls back to grouped-attach."""
    ys = _yard_session(name)
    _tmux("new-session", "-d", "-s", ys)                 # create if absent
    moved, _ = _tmux("move-window", "-s", f"{slug}:{window}", "-t", f"{ys}:")
    if not moved:
        return False
    ok, wins = _tmux("list-windows", "-t", ys, "-F", "#{window_id} #{window_name}")
    if ok:
        for line in wins.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1] != window:      # the stray default 'bash'
                _tmux("kill-window", "-t", parts[0])
    return True


def _reap_loop_tmux(name: str) -> None:
    """Kill every tmux artifact for a loop — its `yard-<name>` session AND any
    lingering `brief-<name>` windows anywhere — so a stopped/finished loop or a
    re-brief never leaves managers piling up in the shared slug. Best-effort."""
    _tmux("kill-session", "-t", _yard_session(name))
    win = f"brief-{name}"[:60]
    ok, rows = _tmux("list-panes", "-a", "-F", "#{window_id} #{window_name}")
    if ok:
        killed = set()
        for line in rows.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1] == win and parts[0] not in killed:
                killed.add(parts[0])
                _tmux("kill-window", "-t", parts[0])


@mcp.tool()
def loop_brief_session(name: str, slug: str = "", force_new: bool = False) -> dict[str, Any]:
    """Spawn an interactive MANAGER-BRIEFING session preloaded with the loop's
    FULL context (its config + the docs its goal references). The owner attaches,
    discusses and reshapes the goal/crew, then a later loop_start ADOPTS this exact
    session as the manager -- it continues with the whole dialogue as its context.

    force_new=True (the Brief button) always returns a FRESH manager, retiring any
    live prior briefing session first. Default reuses a live one (idempotent). To
    CONTINUE the manager that ran a previous round, use loop_debrief_session.
    Returns {ok, sid, attach, slug, window} or {"error": ...}."""
    bad = _check_name(name)
    if bad:
        return bad
    cfg = _read_json(_config_path(name))
    if cfg is None:
        return {"error": f"loop {name!r} has no saved config (loop_save first)"}
    # Honour the loop's pinned `slug` (see _loop_start_local) so the briefed
    # manager is spawned in the SAME repo the loop will Run under — otherwise the
    # briefing lands in the LOOPS_SLUG default and Run would adopt a mismatched dir.
    resolved_slug, slug_err = _resolve_slug(slug or cfg.get("slug") or "", cfg)
    if slug_err:
        return slug_err
    # One briefing session per loop. Default (force_new=False) is IDEMPOTENT: if a
    # live one already exists, return ITS attach command instead of spawning another.
    # force_new=True (the Brief button) always yields a FRESH manager — a live prior
    # briefing session is retired first so it isn't orphaned. To CONTINUE an existing
    # manager instead of replacing it, use loop_debrief_session (Debrief).
    _existing = _read_json(_brief_session_path(name))
    if _existing and _session_alive(_existing.get("sid", "")):
        if not force_new:
            _es = _existing.get("slug", resolved_slug)
            _ew = _existing.get("window", "")
            _bs = chr(92)
            return {"ok": True, "sid": _existing["sid"], "reused": True,
                    "slug": _es, "window": _ew,
                    "attach": (f"tmux new-session -A -d -s v-{name} -t {_es} {_bs}; "
                               f"select-window -t v-{name}:{_ew} {_bs}; switch-client -t v-{name}")}
        # force_new: retire the live briefing session so a fresh one replaces it cleanly.
        try:
            _daemon_call("suspend_session",
                         {"slug": _existing.get("slug", resolved_slug), "sid": _existing["sid"]})
        except Exception:  # noqa: BLE001 — best-effort retire; spawn fresh regardless
            pass
    prompt = (
        f"You are about to become the MANAGER of loop '{name}'. Before anything, "
        f"READ THESE IN FULL so you hold the whole picture:\n"
        f"  1. the loop config: {_config_path(name)}\n"
        f"  2. EVERY doc the goal references below -- follow the absolute paths inside it.\n\n"
        f"## THE LOOP GOAL\n{cfg.get('goal', '')}\n\n"
        f"You are shaping this loop WITH THE OWNER now. You may change the goal, the crew "
        f"roster, and how the work and context are divided per agent. When the owner is done "
        f"and starts the loop, THIS SAME SESSION becomes its manager -- nothing resets, the "
        f"whole conversation is your live context. For now: read everything above, then tell "
        f"the owner you are ready and ask what they would like to adjust.")
    return _spawn_manager_session(name, resolved_slug, prompt, "brief-session.txt")


def _brief_cage_spec(name: str) -> Optional[dict]:
    """The ``sandbox`` spawn param for a Brief/Steer manager (None ⇒ isolation
    off). Writes: only the loop's output dir + report logs (no workspace yet).
    Raises IsolationRefused under strict when the host can't cage."""
    from mcp_loops.headless import cage_write_paths
    cfg = _read_json(_config_path(name)) or {}
    policy = sandbox.plan(name, cfg.get("isolation"))
    if policy is None:
        return None
    if policy.fs_confine:
        policy = sandbox.with_write_paths(
            policy, cage_write_paths(name, None, os.path.abspath(_local_mirror_path())))
    return sandbox.to_spec(policy)


def _adopt_refusal(name: str, bs: dict, sid: str, cfg: dict) -> Optional[str]:
    """Why ``sid`` must NOT be adopted as this loop's manager, or None. With the
    cage on, only a session this server itself spawned caged into THIS loop's
    slice (recorded in the engine-owned brief sidecar) is adopted; anything
    else would run as an UNcaged manager outside the loop's teardown."""
    try:
        if sandbox.resolve_mode(cfg.get("isolation")) == "off":
            return None
    except ValueError as exc:
        return str(exc)
    sl = bs.get("cage_slice") if bs.get("sid") == sid else None
    if isinstance(sl, str) and sandbox.is_cage_slice(sl, loop=name):
        return None
    return (f"isolation: session {sid} was NOT spawned in this loop's cage — "
            f"refusing to adopt it; a caged manager is spawned instead")


def _spawn_manager_session(name: str, resolved_slug: str, prompt: str,
                           prompt_file: str) -> dict[str, Any]:
    """Spawn the interactive manager session the owner attaches to (Brief, and
    Steer's hand-off), isolate it into ``yard-<name>`` and arm the
    brief_session.json sidecar so the next loop_start ADOPTS it as the manager.
    Returns {ok, sid, attach, slug, window, session} or {"error": ...}."""
    # deliver as a one-line pointer (inject submits one Enter per line, so a
    # multi-line initial prompt fragments) -- the session reads the file.
    pdir = os.path.join(report.status_dir(name), "prompts")
    os.makedirs(pdir, exist_ok=True)
    ppath = os.path.join(pdir, prompt_file)
    with open(ppath, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    pointer = (f"Read the file {ppath} in full, then follow it. Do nothing else "
               f"until you have read it.")
    # window NAME computed from the loop so the session is identifiable in tmux
    # (the SESSION/slug must stay a configured project, so we key the window).
    window = f"brief-{name}"[:60]
    # Loop cage: the briefed manager is ADOPTED by the next Run, so it must be
    # caged exactly like a Run-spawned one (same slice → Run teardown kills it).
    try:
        cage = _brief_cage_spec(name)
    except (sandbox.IsolationRefused, ValueError) as exc:
        return {"error": str(exc)}
    try:
        # spawn boots a real interactive `claude` session (~15-20s calm, more
        # under load); the default 30s daemon timeout tips over when the box is
        # busy, and a client-side timeout ORPHANS the window the daemon already
        # made (no isolation runs) — the "swarmdev graveyard". Give it room.
        res = _daemon_call("spawn_session", {
            "slug": resolved_slug, "window": window,
            "initial_prompt": pointer, "role": "manager", "owner": "owner",
            "loops_data_dir": os.path.abspath(_local_mirror_path()),
            **({"sandbox": cage} if cage else {})},
            timeout=90.0)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not spawn briefing session: {exc}"}
    sid = res.get("sid")
    if not res.get("ok") or not sid:
        return {"error": f"spawn_session failed: {res}"}
    # ISOLATE: move the brief window into the loop's OWN `yard-<name>` session so
    # attaching shows ONLY this loop, never the shared slug's pile. Pane id (=sid)
    # is preserved, so a later Run still adopts this exact session.
    isolated = _isolate_window(resolved_slug, window, name)
    session = _yard_session(name) if isolated else resolved_slug
    _write_json(_brief_session_path(name),
                {"sid": sid, "slug": resolved_slug, "session": session,
                 "window": window, "at": time.time(),
                 "cage_slice": (cage or {}).get("slice") if cage else None})
    if isolated:
        # dedicated session holds only this manager → a plain attach lands on it.
        attach = f"tmux attach -t {session}"
    else:
        _bs = chr(92)  # backslash — escape the ; so tmux (not the shell) reads it
        # fallback: a NAMED grouped view into the slug; select-window must target
        # THIS session, not the shared slug, or the client lands on _init.
        _vs = f"v-{name}"[:60]
        attach = (f"tmux new-session -A -d -s {_vs} -t {resolved_slug} {_bs}; "
                  f"select-window -t {_vs}:{window} {_bs}; switch-client -t {_vs}")
    return {"ok": True, "sid": sid, "attach": attach,
            "slug": resolved_slug, "window": window, "session": session}


@mcp.tool()
def loop_debrief_session(name: str, slug: str = "") -> dict[str, Any]:
    """DEBRIEF — continue the SAME manager instead of spawning a fresh one (the
    carry-over path, the after to Brief's before). Resolves which manager to resume,
    in order:
      1. a still-live briefing session (before a run) -> attach to it;
      2. the manager a previous run ADOPTED (last_manager.json) -> RESURRECT it via
         the daemon (`claude --resume <uuid>` off its saved transcript, even after it
         was reaped at finish), re-arm the sidecar so a following loop_start re-adopts
         it, and return its attach command.
    The resurrected manager keeps its whole prior context, so a Debrief -> Run picks
    up the last round's conversation. Returns {ok, sid, attach, slug, window, resumed}
    or {"error": ...} when there is no manager to debrief yet."""
    bad = _check_name(name)
    if bad:
        return bad
    cfg = _read_json(_config_path(name))
    if cfg is None:
        return {"error": f"loop {name!r} has no saved config (loop_save first)"}
    resolved_slug, slug_err = _resolve_slug(slug or cfg.get("slug") or "", cfg)
    if slug_err:
        return slug_err
    _bs = chr(92)
    _vs = f"v-{name}"[:60]

    def _attach(slug_, window):
        return (f"tmux new-session -A -d -s {_vs} -t {slug_} {_bs}; "
                f"select-window -t {_vs}:{window} {_bs}; switch-client -t {_vs}")

    # 1) a live briefing session already exists -> just attach to it.
    _existing = _read_json(_brief_session_path(name))
    if _existing and _session_alive(_existing.get("sid", "")):
        _es = _existing.get("slug", resolved_slug)
        _ew = _existing.get("window", "")
        return {"ok": True, "sid": _existing["sid"], "resumed": False, "reused": True,
                "slug": _es, "window": _ew, "attach": _attach(_es, _ew)}

    # 2) resurrect the manager a previous run adopted.
    _last = _read_json(_last_manager_path(name))
    if not _last or not _last.get("sid"):
        return {"error": f"no manager to debrief for {name!r} yet — Brief one and run "
                         "the loop once; then Debrief continues that same manager."}
    _lslug = _last.get("slug", resolved_slug)
    try:
        # `claude --resume` off a saved transcript is likewise slow to boot —
        # give it the same headroom as spawn so Debrief doesn't time out.
        res = _daemon_call("resume_session", {"slug": _lslug, "sid": _last["sid"]},
                           timeout=90.0)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resume the manager session: {exc}. Use Brief for a new "
                         "conversation using saved loop context; the old chat is not restored."}
    if not res.get("ok"):
        return {"error": f"resume_session failed: {res}"}
    new_sid = res.get("sid") or _last["sid"]
    window = _last.get("window") or f"brief-{name}"[:60]
    # re-arm the sidecar so the NEXT loop_start ADOPTS the resurrected manager.
    _write_json(_brief_session_path(name),
                {"sid": new_sid, "slug": _lslug, "window": window, "at": time.time(),
                 "cage_slice": _last.get("cage_slice")})
    return {"ok": True, "sid": new_sid, "resumed": True, "slug": _lslug,
            "window": window, "attach": _attach(_lslug, window)}


# ── Better-UX #6: STEER — mirror Brief mid-run ─────────────────────────────────
# Replaces the user-written steering-notes input. One click:
#   1. GENTLE stop — the in-flight turn finishes (no substrate shutdown); the
#      runner stops cooperatively at the next slot boundary.
#   2. HAND-OFF — once the run thread ends, the manager is resumed (the one a
#      prior Brief made, via last_manager.json) or a fresh manager session is
#      spawned preloaded with where the team stopped; its attach is published.
#   3. The owner talks to the manager, then Run/loop_steer_resume RESTARTS from
#      the same point: the adopted manager keeps the dialogue, the turn budget
#      continues, the workspace is kept, and the manager's first turn carries
#      the steer context.
# steer.json phases: stopping → handoff → ready → restarted  (or error).
STEER_PHASES = ("stopping", "handoff", "ready", "restarted", "error")
STEER_ACTIVE = frozenset({"stopping", "handoff", "ready"})


def _steer_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "steer.json")


def _steer_public(st: dict) -> dict:
    """The steer fields the UI renders (run.json.steer / loop_steer_status)."""
    return {k: st.get(k) for k in ("phase", "requestedAt", "turnsUsed", "attach",
                                    "sid", "resumed", "error", "restartedAt", "conversation")
            if st.get(k) is not None}


def _steer_set(name: str, **fields) -> dict:
    st = _read_json(_steer_path(name)) or {}
    st.update(fields, updated=time.time())
    _write_json(_steer_path(name), st)
    _update_run(name, steer=_steer_public(st))
    return st


def _steer_context(name: str, turns_used: int, ended: str) -> str:
    """What the restarted manager is told: where the team stopped + to resume."""
    base = report.status_dir(name)
    # H7: only the run that was just steered — never a prior run's tail
    reps = [e for e in turn_identity.rows_for_run(
                _read_jsonl(os.path.join(base, "status.jsonl")),
                _read_json(os.path.join(base, "run.json")) or {})
            if e.get("kind") != "machinery"][-8:]
    lines = [f"- {e.get('agent', '?')} ({e.get('status', '?')}): "
             f"{str(e.get('note') or '')[:300]}" for e in reps]
    return ("The owner STEERED this loop: it was gently stopped after "
            f"{turns_used} main-phase turn(s) (ended={ended}) and the owner talked "
            "to you, the manager, about a new direction. RESUME FROM EXACTLY THAT "
            "POINT — do not restart the work or redo finished turns; apply the "
            "new direction from your conversation with the owner to the next "
            "round, then continue toward the north star.\n"
            "Where the team was when it stopped:\n" + ("\n".join(lines) or "- (no reports yet)"))


def _steer_handoff(name: str, cfg: dict, result: Any) -> dict:
    """Step 2 of a Steer (runs on the run-thread after it finalized): record the
    resume point, then hand the owner a manager to attach to. Never raises."""
    try:
        turns = int(getattr(result, "turns_used", 0) or 0)
        ended = str(getattr(result, "ended", "") or "stopped")
        ctx = _steer_context(name, turns, ended)
        _steer_set(name, phase="handoff", turnsUsed=turns, context=ctx)
        res: dict = {}
        last = _read_json(_last_manager_path(name)) or {}
        if last.get("sid"):
            # the manager a prior Brief made: resurrect it with its whole context
            res = loop_debrief_session(name)
        if not res.get("ok"):
            resolved_slug, err = _resolve_slug(cfg.get("slug") or "", cfg)
            if err:
                res = err
            else:
                prompt = (
                    f"You are the MANAGER of loop '{name}', which the owner just "
                    f"STEERED (gently stopped mid-run to change direction). READ IN "
                    f"FULL: the loop config {_config_path(name)} and every doc the "
                    f"goal references.\n\n## THE LOOP GOAL\n{cfg.get('goal', '')}\n\n"
                    f"## WHERE THE LOOP STOPPED\n{ctx}\n\n"
                    "Tell the owner you are ready and ask what they want to change. "
                    "When they restart the loop, THIS SAME SESSION continues as its "
                    "manager from that same point, with this conversation as context.")
                res = _spawn_manager_session(name, resolved_slug, prompt, "steer-session.txt")
        if res.get("ok"):
            return _steer_set(name, phase="ready", attach=res.get("attach"),
                              sid=res.get("sid"), resumed=bool(res.get("resumed")),
                              conversation=("continued" if res.get("resumed") or res.get("reused")
                                            else "new session with saved loop context; prior chat not restored"),
                              error=None)
        return _steer_set(name, phase="error",
                          error=str(res.get("error") or "could not hand off the manager"))
    except Exception as exc:  # noqa: BLE001 — a failed hand-off must not crash teardown
        try:
            return _steer_set(name, phase="error", error=f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            return {}


@mcp.tool()
def loop_steer(name: str) -> dict[str, Any]:
    """STEER a running loop — the mid-run twin of Brief (Better-UX #6). Gently
    stops the run (the current turn finishes; nothing is killed), then hands the
    owner the manager's attach (poll loop_steer_status until phase=ready). The
    owner talks to the manager; Run (or loop_steer_resume) restarts FROM THE SAME
    POINT with that new context. Returns {ok, name, state, steer} or {"error"}."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        entry = _RUNS.get(name)
        if not entry or not entry["thread"].is_alive():
            return {"error": f"loop {name!r} is not running — use Brief/Debrief to "
                             "talk to its manager"}
        if entry.get("stop_requested"):
            return {"error": f"loop {name!r} is already stopping"}
        # clear any stale steer record, THEN request the cooperative stop — no
        # substrate.shutdown(): the in-flight turn is allowed to finish (gentle).
        _write_json(_steer_path(name), {})
        entry["steer"] = True
        entry["stop_requested"] = True
        st = _steer_set(name, phase="stopping", requestedAt=time.time())
        _update_run(name, state="stopping")
        if entry["owner"].waiting:
            entry["owner"].deliver("(loop paused by the owner to steer)")
        return {"ok": True, "name": name, "state": "stopping",
                "steer": _steer_public(st)}
    return _run(work)


@mcp.tool()
def loop_steer_status(name: str, owner: str = "") -> dict[str, Any]:
    """Where a Steer is: {name, active, steer:{phase ∈ stopping|handoff|ready|
    restarted|error, attach?, sid?, turnsUsed?, error?}} — steer is {} if none.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    bad = _check_name(name)
    if bad:
        return bad
    refused = _owner_refusal_for(name, owner)
    if refused:
        return refused
    st = _read_json(_steer_path(name)) or {}
    return {"name": name, "active": st.get("phase") in STEER_ACTIVE,
            "steer": _steer_public(st)}


@mcp.tool()
def loop_steer_resume(name: str, slug: str = "") -> dict[str, Any]:
    """Step 3 of a Steer: restart the loop FROM THE SAME POINT with the steered
    manager (adopted) as its manager. Only valid once loop_steer_status reports
    phase=ready. Returns loop_start's result (+ resumedFromTurn)."""
    bad = _check_name(name)
    if bad:
        return bad
    st = _read_json(_steer_path(name)) or {}
    if st.get("phase") != "ready":
        return {"error": f"no steer ready to resume for {name!r} "
                         f"(phase={st.get('phase') or 'none'})"}
    out = _loop_start_local(name, slug)
    if out.get("ok"):
        out["resumedFromTurn"] = st.get("turnsUsed") or 0
    return out


def _classify_dispatch_error(exc: BaseException) -> str:
    """Classify a failed routed ``dispatch_start`` so loop_start only fails OPEN
    to the local launcher on a genuine transport outage:

    - ``"policy"``    — the origin's §6.4 allowlist refused the loop's Project
      (``ProjectNotAllowed``, which crosses the wire as an ``RpcRefused`` whose
      message carries the class name);
    - ``"refused"``   — any other typed refusal the origin ANSWERED with;
    - ``"transport"`` — not live / channel lost / RPC timeout / socket error;
    - ``"other"``     — anything else (a bug) — surfaced, never swallowed.
    ``DispatchUnavailable`` is matched by name: its module embeds the Hub façade
    the engine core must never import (test_engine_hub_boundary)."""
    from mcp_loops.origin_proto.agent_core import ProjectNotAllowed
    from mcp_loops.origin_proto.channel import ChannelError, RpcRefused
    if isinstance(exc, ProjectNotAllowed):
        return "policy"
    if isinstance(exc, RpcRefused):
        msg = str(exc)
        if "ProjectNotAllowed:" in msg or "ProductNotAllowed:" in msg:
            return "policy"
        return "refused"
    if (type(exc).__name__ == "DispatchUnavailable"
            or isinstance(exc, (ChannelError, OSError, TimeoutError))):
        return "transport"
    return "other"


def _is_remote_origin(origin: str) -> bool:
    """A non-empty ``origin`` that is neither the local id nor this engine's own
    enrolled origin id — i.e. a loop_start that must run on ANOTHER box."""
    if not origin or origin in (origins.LOCAL_ORIGIN_ID, "LOCAL"):
        return False
    svc = origin_dispatch()
    own = getattr(svc, "origin_id", None) if svc is not None else None
    return origin != own


def _loop_models(cfg: Optional[dict]) -> list[str]:
    """Every per-agent model a loop config names (nested sub-loops included)."""
    out: list[str] = []

    def walk(steps: Any) -> None:
        for s in (steps or {}).values() if isinstance(steps, dict) else ():
            if not isinstance(s, dict):
                continue
            m = s.get("model")
            if isinstance(m, str) and m.strip() and m not in out:
                out.append(m)
            walk(s.get("steps"))
    walk((cfg or {}).get("steps"))
    return out


def _loop_start_remote(name: str, slug: str, origin: str) -> dict[str, Any]:
    """Gap #3 — prepare + run ``name`` on the REMOTE ``origin``'s OWN engine over
    the live origin channel. Ships the Hub's saved config (the origin saves it
    only if it lacks the loop, after its §6.4 Project gate); refuses up-front
    when no origin Hub runs on this engine, the origin isn't connected, or a
    loop model targets a CLI the origin PROVED is not signed in. NEVER falls
    back to running locally — the caller asked for THAT origin."""
    svc = _remote_dispatch(origin)
    if svc is None:
        return {"error": f"cannot start {name!r} on origin {origin!r}: this "
                         f"engine reaches no origin hub (LOOPYARD_ORIGIN_AGENT "
                         f"and LOOPYARD_HUB_CONTROL_SOCK both off)",
                "routed": False, "origin": origin}
    cfg = _read_json(_config_path(name))
    now = time.time()
    for model in _loop_models(cfg):
        gate = origins_probe.can_run(origins_probe.probe_origin(
            "origin", now, remote_probe=_remote_caps_probe(origin, cached=True)),
            model)
        if not gate["canRun"]:
            return {"error": f"origin {origin!r} cannot run model {model!r}: "
                             f"{gate['reason']}", "originGate": gate,
                    "routed": False, "origin": origin}
    try:
        res = svc.dispatch_start_remote(name, origin, slug=slug, config=cfg)
    except Exception as exc:  # noqa: BLE001 — classified; never run it locally
        kind = _classify_dispatch_error(exc)
        if kind == "policy":
            return {"error": f"origin {origin!r} refused to start {name!r}: {exc}",
                    "refused": "project_not_allowed", "routed": True,
                    "origin": origin}
        if kind == "refused":
            return {"error": f"origin {origin!r} refused to start {name!r}: {exc}",
                    "refused": getattr(exc, "code", None) or "refused",
                    "routed": True, "origin": origin}
        if kind == "transport":
            return {"error": f"origin {origin!r} is not reachable: {exc}",
                    "routed": False, "origin": origin}
        raise
    if isinstance(res, dict) and res.get("ok") and not res.get("error"):
        # The Hub keeps only the saved config; the run lives on THAT origin.
        # Record where it went (a sibling dispatched.json — the Hub never
        # writes a run.json for it) so per-origin views (Machines count, the
        # dashboard list, loop_list_all) attribute the loop to ``origin``
        # instead of a 'saved' loop on this box. A later local start clears it.
        _write_json(_dispatched_path(name), {
            "origin": origin, "dispatchId": res.get("dispatchId"),
            "deviceId": res.get("deviceId"), "at": now})
    return res


@mcp.tool()
def loop_start(name: str, slug: str = "", dispatch_id: str = "",
               adopt_manager_sid: str = "", origin: str = "") -> dict[str, Any]:
    """Launch a saved loop: LoopRunner on HeadlessSubstrate (worker-daemon
    sessions under `slug`) in a background thread. Owner asks park the run
    (state=waiting_owner) until loop_reply delivers. Returns {ok, name, state}
    or {"error": ...} (unknown loop / already running / no runner attached).

    `slug` empty ⇒ resolved from the attached runner (`yard up`) — see
    _resolve_slug. On a fresh install with no runner attached this fails with a
    clear 'no runner attached' error, never the opaque worker-daemon trace.

    G2.2 — origin routing: when the box's local origin-agent is enrolled + live,
    a default start ROUTES through the origin protocol to that origin's OWN engine
    (owner's north star: even the local VPS is an origin like any other, no
    hardcoded special-case). `dispatch_id` non-empty means this call ARRIVED over
    the origin channel — run it locally, keyed on that id for idempotency, never
    re-route. With no origin-agent installed, the old in-process path runs
    verbatim (back-compat); a TRANSPORT failure fails OPEN to local, but an
    origin policy refusal is terminal ({"error", "refused": "project_not_allowed"}).

    ``origin`` (gap #3) names a REMOTE connected origin to run the loop on: the
    Hub ships its saved config and the loop is prepared + run on THAT origin's
    own engine (``{ok, routed, origin, dispatchId, prepared}``); its progress
    streams back and ``loop_status(name, origin=…)`` reads it over the channel.
    A remote start never falls back to local. Empty / ``"local"`` = unchanged."""
    def work():
        if _is_remote_origin(origin):
            if dispatch_id or adopt_manager_sid:
                return {"error": "dispatch_id / adopt_manager_sid are local-only; "
                                 "they cannot be combined with a remote origin"}
            bad = _check_name(name)
            if bad:
                return bad
            return _loop_start_remote(name, slug, origin)
        # Session ADOPTION binds a LOCAL daemon session id — run on THIS
        # engine, never route to another origin (which couldn't see the sid).
        if adopt_manager_sid:
            return _loop_start_local(name, slug, adopt_manager_sid=adopt_manager_sid)
        # arrived over the origin channel (agent → engine): run locally, keyed on
        # the dispatch-id — do NOT re-route (that would be an infinite loop).
        if dispatch_id:
            return _loop_start_local(name, slug, dispatch_id=dispatch_id)
        # default path: route through the enrolled local origin when it's live.
        svc = origin_dispatch()
        if svc is not None:
            try:
                if svc.is_live():
                    return svc.dispatch_start(name, slug)
            except Exception as exc:  # noqa: BLE001 — classified just below
                kind = _classify_dispatch_error(exc)
                if kind == "policy":
                    # The origin said NO (§6.4 allowlist). Failing open to local
                    # would bypass that gate (loopyard-follow-up-1790359385).
                    return {"error": f"origin refused to start {name!r}: {exc}",
                            "refused": "project_not_allowed", "routed": True}
                if kind == "refused":
                    # The origin ANSWERED with a typed refusal — proof it did not
                    # run; re-launching locally would override its decision.
                    return {"error": f"origin refused to start {name!r}: {exc}",
                            "refused": getattr(exc, "code", None) or "refused",
                            "routed": True}
                if kind != "transport":
                    raise
                # Transport-unavailable only (not live / channel lost / timeout):
                # never let a hub/agent hiccup wedge
                # a start; fall through to the local launcher. BUT if the routed
                # dispatch ALREADY launched the run before the channel hiccup on the
                # ack, _loop_start_local would report a spurious 'already running' —
                # so surface the running result instead of trying to re-launch.
                ent = _RUNS.get(name)
                thr = ent.get("thread") if isinstance(ent, dict) else None
                if thr is not None and thr.is_alive():
                    return {"ok": True, "name": name, "state": "running",
                            "routed": True,
                            "note": "launched via origin; channel hiccup on ack"}
        return _loop_start_local(name, slug)
    return _run(work)


def _dispatched_path(name: str) -> str:
    return os.path.join(os.path.dirname(_run_path(name)), "dispatched.json")


def _dispatched_origin(loop_dir: str) -> Optional[str]:
    """The remote origin a Hub-saved loop (dir ``loop_dir``) was last
    dispatched to (its ``dispatched.json``), or None for a loop that runs here."""
    d = _read_json(os.path.join(loop_dir, "dispatched.json"))
    o = d.get("origin") if isinstance(d, dict) else None
    return o if isinstance(o, str) and _is_remote_origin(o) else None


def _remote_loop_stop(name: str, origin: str) -> dict[str, Any]:
    """``loop_stop`` for a loop running on a REMOTE origin: ``loop.stop`` over
    the origin channel / hub_serve bridge. Never touches a local run."""
    svc = _remote_dispatch(origin)
    stopper = getattr(svc, "dispatch_stop_remote", None)
    if stopper is None:
        return {"error": f"cannot stop {name!r} on origin {origin!r}: this "
                         f"engine reaches no origin hub", "routed": False,
                "origin": origin}
    try:
        return stopper(name, origin)
    except Exception as exc:  # noqa: BLE001 — classified below
        kind = _classify_dispatch_error(exc)
        if kind == "transport":
            return {"error": f"origin {origin!r} is not reachable: {exc}",
                    "routed": False, "origin": origin}
        if kind in ("refused", "policy"):
            return {"error": f"origin {origin!r} refused to stop {name!r}: {exc}",
                    "refused": getattr(exc, "code", None) or "refused",
                    "routed": True, "origin": origin}
        raise


def _remote_loop_status(name: str, origin: str, tail: int) -> Optional[dict]:
    """``loop_status`` for a loop on a LIVE remote origin, read over the origin
    channel (read-only ``loop.status``) and reshaped to the local answer. None
    when there's no origin hub / the origin isn't connected / it doesn't know
    the loop, so the caller keeps its mirror-based error."""
    svc = _remote_dispatch(origin)
    reader = getattr(svc, "dispatch_status_remote", None)
    if reader is None:
        return None
    try:
        got = reader(name, origin, tail=max(0, int(tail)))
    except Exception:  # noqa: BLE001 — not connected ⇒ mirror answer stands
        return None
    if not isinstance(got, dict) or got.get("error") or "run" not in got:
        return None
    run = got.get("run") or {"state": "saved"}
    waiting = run.get("question") if run.get("state") == "waiting_owner" else None
    return {"name": name, "origin": origin, "run": run,
            "recent": list(got.get("recent") or []), "live": got.get("live") or {},
            "input_pending": [], "waiting_question": waiting,
            "via": "origin-channel"}


@mcp.tool()
def loop_status(name: str, tail: int = 10,
                origin: str = origins.LOCAL_ORIGIN_ID,
                owner: str = "") -> dict[str, Any]:
    """Run state for a loop: run.json (state / question / result) + the last
    `tail` per-turn agent reports from status.jsonl. Returns {name, origin, run,
    recent, live, input_pending, waiting_question} — waiting_question is set
    while the manager is parked on ask_owner.

    ``origin`` defaults to the LOCAL mirror (unchanged behaviour). Pass a remote
    origin id (as listed by loop_origin_list) to inspect a loop running on ANY
    connected origin from the ONE hub — READ-ONLY, straight off that origin's
    synced mirror. A remote run's parked owner question comes from its persisted
    run.json (there is no in-process owner channel on THIS box for a remote run);
    the mid-run input queue is a LOCAL-only steering seam, so it's empty for a
    remote origin.

    ``owner`` (optional, Phase 5 seam): a loop of another owner is refused
    (``{error, refused:"cross_owner"}``) before anything is read."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        base = _status_dir_for(origin, name)
        state = cfg = None
        if base is not None:
            state = _read_json(os.path.join(base, "run.json"))
            cfg = _read_json(os.path.join(base, "config.json"))
        if state is None and cfg is None and _is_remote_origin(origin):
            # gap #3: a loop dispatched to a LIVE remote origin has no rsync
            # mirror here — read its status off that origin's engine.
            live = _remote_loop_status(name, origin, tail)
            if live is not None:
                return live
        if base is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        if state is None and cfg is None:
            return {"error": f"loop {name!r} not found on origin {origin!r}"}
        refused = schema.owner_refusal(name, cfg if cfg is not None else {}, owner)
        if refused:
            return refused
        recent: list[dict] = []
        try:
            with open(os.path.join(base, "status.jsonl"), encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            for ln in lines[-max(0, int(tail)):]:
                try:
                    recent.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
        except FileNotFoundError:
            pass
        live = _read_json(os.path.join(base, "live.json")) or {}
        # In-process owner park exists only for a LOCAL run driven on THIS box.
        # For a remote origin (or a local run resumed without an in-process
        # owner) fall back to the question persisted in run.json while
        # state=waiting_owner, so the hub still shows what the run is asking.
        local = origin == origins.LOCAL_ORIGIN_ID
        hil = (_RUNS.get(name) or {}).get("owner") if local else None
        if hil and hil.waiting:
            waiting_question = hil.question
        elif (state or {}).get("state") == "waiting_owner":
            waiting_question = (state or {}).get("question")
        else:
            waiting_question = None
        pending = (_read_jsonl(_input_queue_path(name))[_input_cursor(name):]
                   if local else [])
        return {"name": name, "origin": origin, "run": state or {"state": "saved"},
                "recent": recent,
                "live": live.get("running", []),
                "input_pending": pending,
                "waiting_question": waiting_question}
    return _run(work)


@mcp.tool()
def loop_stop(name: str, origin: str = "") -> dict[str, Any]:
    """Stop a running loop: suspends its headless sessions (substrate.shutdown)
    and unblocks a parked owner ask with an empty reply so the thread can end.

    A2 — stop is AUTHORITATIVE: after requesting the cooperative stop we wait a
    bounded grace (LOOPS_STOP_GRACE, default 6s) for the thread to end and write
    its own terminal 'stopped' + finish-report. If the thread is WEDGED past the
    grace (an agent ignoring should_stop + shutdown), the server finalizes the
    run itself so run.json never sticks at 'stopping' and a follow-up start_loop
    can reclaim the slot. Returns {ok, name, state} or {"error": ...}.

    ``origin`` (gap #3) names a REMOTE connected origin: the stop is sent as
    ``loop.stop`` to THAT origin's engine over the origin channel (the origin
    answers ``{ok, name, state}``, annotated ``routed``/``origin``). Empty /
    ``"local"`` = unchanged; a remote stop never stops a local run."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        if _is_remote_origin(origin):
            return _remote_loop_stop(name, origin)
        entry = _RUNS.get(name)
        if not entry or not entry["thread"].is_alive():
            return {"error": f"loop {name!r} is not running"}
        entry["stop_requested"] = True
        # mark 'stopping' BEFORE unparking the runner: once deliver() wakes it,
        # the run thread may finish and write its final 'stopped' immediately;
        # writing 'stopping' after that would clobber the final state for good
        # (_update_run is last-writer-wins).
        _update_run(name, state="stopping")
        try:
            entry["substrate"].shutdown()
        except Exception:  # noqa: BLE001 — best-effort
            pass
        if entry.get("dispatcher") is not None:        # Step 5: running sub-loops' cages too
            entry["dispatcher"].shutdown_live()
        _reap_loop_tmux(name)                          # reap isolated session/brief window on stop
        if entry["owner"].waiting:
            entry["owner"].deliver("(loop stopped by owner)")
        # bounded wait for the cooperative path; a clean exit writes 'stopped' +
        # finish-report itself. A wedged thread is finalized authoritatively.
        entry["thread"].join(timeout=_stop_grace())
        if entry["thread"].is_alive():
            _finalize_stopped_wedged(name, _read_json(_config_path(name)) or {})
            entry["wedged"] = True
        final = (_read_json(_run_path(name)) or {}).get("state", "stopped")
        return {"ok": True, "name": name, "state": final}
    return _run(work)


@mcp.tool()
def loop_reply(name: str, reply: str) -> dict[str, Any]:
    """Deliver the owner's reply to a manager parked on ask_owner. Returns
    {ok, name} or {"error": ...} when no run of this loop is waiting."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        entry = _RUNS.get(name)
        if not entry or not entry["thread"].is_alive():
            return {"error": f"loop {name!r} is not running"}
        if not entry["owner"].waiting:
            return {"error": f"loop {name!r} is not waiting on the owner"}
        entry["owner"].deliver(reply)
        return {"ok": True, "name": name}
    return _run(work)


# ── app-connect INBOUND (CAP-2 §2a): dispatch a loop as a task + pull a result
# envelope back. An external Codex App / Claude Code session drives a loop over
# these four tools alone: start_loop -> (poll) get_loop_status -> get_loop_result,
# with cancel_loop to abort. Each returns the ENVELOPE (mcp_loops.envelope) as
# its top-level response — the stable contract at the boundary — never the raw
# internal run.json. They are THIN over loop_start/loop_status/loop_stop; the
# envelope folds run state + result + agent-note markers into one honest shape.
def _envelope_for(name: str, *, now: Optional[float] = None,
                  origin: str = origins.LOCAL_ORIGIN_ID) -> dict:
    """Build the result envelope for ``name`` from its on-disk state + the live
    owner-park question (if any). Pure read — never mutates the run.

    ``origin`` defaults to local (the CAP-2 inbound app-connect path is
    unchanged). A remote origin id builds the SAME envelope shape off that
    origin's synced mirror so the ONE hub can inspect a remote loop's result;
    the parked question then comes from the mirrored run.json (no in-process
    owner on this box), and output_dir is left unset (this box holds the remote's
    status mirror, not its output tree)."""
    base = _status_dir_for(origin, name)
    if base is None:
        base = report.status_dir(name)          # unknown origin → empty local read
    reports = [e for e in _read_jsonl(os.path.join(base, "status.jsonl"))
               if e.get("kind") != "machinery"]
    run = _read_json(os.path.join(base, "run.json"))
    if origin == origins.LOCAL_ORIGIN_ID:
        out_dir = os.path.join(_output_base(), name)
        output_dir = out_dir if os.path.isdir(out_dir) else None
        owner = (_RUNS.get(name) or {}).get("owner")
        wq = owner.question if owner and getattr(owner, "waiting", False) else None
        if wq is None and (run or {}).get("state") == "waiting_owner":
            wq = (run or {}).get("question")
    else:
        output_dir = None
        wq = (run or {}).get("question") if (run or {}).get(
            "state") == "waiting_owner" else None
    # A7: live turn counts the runner publishes each turn (progress.json), so a
    # mid-run poll shows real turns.main instead of 0 until the run ends.
    live_turns = _read_json(os.path.join(base, "progress.json"))
    cfg = _read_json(os.path.join(base, "config.json"))
    env = envelope.build_envelope(
        name, run=run, config=cfg,
        reports=reports, status_dir=base, output_dir=output_dir,
        waiting_question=wq, live_turns=live_turns,
        now=now if now is not None else time.time())
    # Phase 5: a run result carries its loop's tenant owner (read-time, from the
    # config). Omitted for the local default owner so the single-owner envelope
    # is byte-identical to the pre-owner contract.
    res_owner = schema.resolve_owner(cfg)
    if isinstance(env, dict) and res_owner != schema.DEFAULT_OWNER:
        env["owner"] = res_owner
    return env


@mcp.tool()
def start_loop(name: str, slug: str = "") -> dict[str, Any]:
    """APP-CONNECT (CAP-2 inbound): dispatch a saved loop as a task. Launches it
    exactly like loop_start, then returns the initial RESULT ENVELOPE
    (mcp_loops.envelope — task_id, status, summary, artifacts, git_commit,
    verification, duration, …) so an external app can immediately record the
    task_id and poll get_loop_status/get_loop_result. Launch failures (unknown
    loop / already running / no runner attached) surface as {"error": ...}.

    `slug` empty ⇒ resolved from the attached runner (`yard up`). On a fresh
    install with no runner this returns a clear 'no runner attached' error —
    the whole point of P1: an app dispatching a loop out-of-the-box either runs
    or gets a fixable message, never the opaque 'unknown project slug' trace."""
    def work():
        res = loop_start(name, slug=slug)
        if res.get("error"):
            return res
        return _envelope_for(name)
    return _run(work)


@mcp.tool()
def get_loop_status(name: str, tail: int = 10, owner: str = "") -> dict[str, Any]:
    """APP-CONNECT (CAP-2 inbound): poll a dispatched task. Returns the RESULT
    ENVELOPE for ``name`` plus the last ``tail`` per-turn agent reports under
    ``recent`` (so an app can show progress without a second call). ``status``
    follows the external vocab (running | waiting_owner | needs_owner |
    completed | error | stopped | saved | not_found); poll until ``done`` is
    True, then call get_loop_result. ``owner`` (optional, Phase 5 seam) refuses
    a loop of another owner (``{error, refused:"cross_owner"}``, no envelope)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        env = _envelope_for(name)
        recent: list[dict] = []
        try:
            with open(report.status_log(name), encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            for ln in lines[-max(0, int(tail)):]:
                try:
                    recent.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
        except FileNotFoundError:
            pass
        env["recent"] = recent
        return env
    return _run(work)


@mcp.tool()
def cancel_loop(name: str) -> dict[str, Any]:
    """APP-CONNECT (CAP-2 inbound): abort a dispatched task. ALWAYS returns the
    RESULT ENVELOPE — never a bare ``{"error": ...}`` (P3/pilot): a running loop
    is stopped (loop_stop; its state settles to ``stopped`` shortly after —
    re-poll get_loop_result for the terminal envelope); a loop that ISN'T running
    (saved, or already terminal) returns its current envelope; an UNKNOWN loop
    returns ``status="not_found"``, exactly as get_loop_result does. That one-shape
    contract means an app driving cancel→poll never has to special-case an error
    dict (the pilot saw ``{status:null, done:null}`` reading .status off the old
    error return). A malformed loop NAME is still an input error."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        entry = _RUNS.get(name)
        if entry and entry["thread"].is_alive():
            loop_stop(name)          # request stop; ignore result — the envelope
            # below reflects the (now stopping→stopped) state either way.
        return _envelope_for(name)
    return _run(work)


# ── invisible disposition capture (SLICE-1 §4.2–4.4) ────────────────────────
# What the owner DID with a result — captured to an append-only per-loop log in
# the LOCAL status_dir (never a Project, F4; a re-run cannot wipe it, F2). The
# tool below (explicit ship/keep/good/ok/bad) and the loop_start engine seam
# (re-run) both funnel through the shared _record_disposition, so every origin
# is captured by construction against ONE row schema (§4.3).
_DISPOSITION_VERBS = frozenset({"re-run", "good", "ok", "bad",
                                "removed", "deleted"})
# Better-UX #5: good/ok/bad is the ONLY manual rating. ship/keep are retired as
# clicks (legacy rows still read back); removed/deleted are AUTOMATIC — only the
# engine records them (loop_archive → removed), never the tool.
_MANUAL_VERBS = frozenset({"good", "ok", "bad", "re-run"})
_RETIRED_VERBS = frozenset({"ship", "keep"})


def _team_signature(cfg: Optional[dict]) -> Optional[str]:
    """A stable signature of a loop's crew (the §4.3 slice-3 join key): the sorted
    agent step ids. slice-3 groups per-team trust rates by this; ``None`` when a
    config names no agents."""
    steps = (cfg or {}).get("steps") or {}
    agents = sorted(sid for sid, s in steps.items()
                    if isinstance(s, dict) and s.get("type") == "agent")
    return ",".join(agents) or None


def _deliverable_ref(name: str, run: Optional[dict]) -> str:
    """A digest of what a run PRODUCED (§4.1) — the last substantive NON-manager
    agent note + the git commit a note names. Deliberately NOT the finish-report
    receipt (R1) and NOT the empty ``kind:"reported"`` seam (RR1), so the snapshot
    a disposition records is reliably non-null: ``note:@<sha1-10>|commit:<sha>``,
    either half omitted when absent. ``""`` only when a run left no note and named
    no commit."""
    run = run or {}
    started = run.get("started")
    base = report.status_dir(name)
    reports = [e for e in _read_jsonl(os.path.join(base, "status.jsonl"))
               if e.get("kind") != "machinery"]
    reports = envelope._reports_for_run(reports, run=run)
    steps = ((_read_json(_config_path(name)) or {}).get("steps") or {})
    manager_ids = frozenset(sid for sid, s in steps.items()
                            if isinstance(s, dict) and s.get("role") == "manager")
    parts: list[str] = []
    note = envelope._answer_note(reports, manager_ids)
    if note:
        parts.append("note:@" + hashlib.sha1(note.encode("utf-8")).hexdigest()[:10])
    commit = run.get("git_commit")
    if not (isinstance(commit, str) and commit.strip()):
        commit = None
        for e in reversed(reports):
            m = envelope._COMMIT_RE.search(e.get("note") or "")
            if m:
                commit = m.group(1)
                break
    if isinstance(commit, str) and commit.strip():
        parts.append("commit:" + commit.strip())
    return "|".join(parts)


def _record_disposition(name: str, verb: str, *, source: str = "behavior",
                        origin: str = "dashboard", started: Optional[float] = None,
                        deliverable_ref: Optional[str] = None, note: str = "",
                        task_id: str = "", summary_ref: str = "",
                        team: Optional[str] = None,
                        project: Optional[str] = None) -> dict[str, Any]:
    """Append ONE disposition row (§4.3) to the loop's LOCAL dispositions.jsonl.
    The shared internal both the loop_disposition_record TOOL (explicit verbs) and
    the loop_start engine seam (re-run) call. NEVER raises into the caller — a bad
    verb or a write error returns ``{"ok": False, ...}`` (the engine seam must not
    be able to break a launch)."""
    try:
        if verb not in _DISPOSITION_VERBS:
            return {"ok": False, "error": f"unknown verb {verb!r}; verbs = "
                    f"{sorted(_DISPOSITION_VERBS)}"}
        run = _read_json(_run_path(name)) or {}
        cfg = _read_json(_config_path(name)) or {}
        # runStarted at FULL float (R4) — the durable key. Explicit `started`
        # (the re-run seam passes the PRIOR run's started, so the row keys the run
        # it JUDGED, not the one about to launch) wins; else the current run.json.
        rs = started if isinstance(started, (int, float)) else run.get("started")
        tid = task_id or (f"{name}#{int(rs)}"
                          if isinstance(rs, (int, float)) else name)
        ref = summary_ref or deliverable_ref
        if ref is None:
            ref = _deliverable_ref(name, run)
        row = {
            "task_id": tid,
            "loop": name,
            "runStarted": float(rs) if isinstance(rs, (int, float)) else None,
            "verb": verb,
            "source": source,                       # behavior | explicit (S2 ratio)
            "origin": origin,                       # who captured it (R3 keyhole check)
            "at": int(time.time()),
            "deliverableRef": ref,
            "team": team if team is not None else _team_signature(cfg),
            "project": project if project is not None else (cfg.get("project") or None),
            "note": note or "",
        }
        d = report.status_dir(name)
        os.makedirs(d, exist_ok=True)
        with open(report.disposition_log(name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {"ok": True, "row": row}
    except Exception as e:  # noqa: BLE001 — never raise into a caller or the engine
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def loop_disposition_record(name: str, verb: str, source: str = "behavior",
                            note: str = "", origin: str = "dashboard",
                            task_id: str = "", summary_ref: str = "") -> dict[str, Any]:
    """Record ONE disposition (SLICE-1 §4.2–4.4) — what the owner thought of a
    loop's result. ``verb`` ∈ {good, ok, bad, re-run}: good/ok/bad is the ONLY
    manual rating (pass ``source="explicit"``); re-run is the behavior the
    engine also records itself. ship/keep are RETIRED (Better-UX #5) and rejected;
    the negative statuses removed/deleted are AUTOMATIC (engine-only — archiving
    a loop records ``removed``), so this tool refuses them too. There are NO edit verbs and NO "abandon" —
    absence is stored as absence (§4.2), never a flattened negative. Appends to the
    loop's LOCAL ``dispositions.jsonl`` (never a Project, so it needs zero setup
    and a re-run cannot wipe it). ``origin`` tags WHO captured it
    (dashboard|engine|cli|mcp|app-connect) so an all-``dashboard`` log is a visible
    keyhole (R3). Fail-soft: a bad verb / write error returns ``{"ok": False, …}``,
    never raises."""
    bad = _check_name(name)
    if bad:
        return bad
    if verb in _RETIRED_VERBS:
        return {"ok": False, "error": f"{verb!r} is retired — rate the result "
                "good / ok / bad; removed/deleted are recorded automatically"}
    if verb not in _MANUAL_VERBS:
        if verb in _DISPOSITION_VERBS:
            return {"ok": False, "error": f"{verb!r} is automatic (recorded by the "
                    "engine), not a manual rating — use good / ok / bad"}
        return {"ok": False, "error": f"unknown verb {verb!r}; verbs = "
                f"{sorted(_MANUAL_VERBS)}"}
    return _record_disposition(name, verb, source=source, note=note, origin=origin,
                               task_id=task_id, summary_ref=summary_ref)


@mcp.tool()
def loop_disposition_list(name: str, owner: str = "") -> dict[str, Any]:
    """Read a loop's disposition rows (SLICE-1 §4.2) from its LOCAL
    ``dispositions.jsonl``. Returns ``{ok, loop, rows, counts, sources}`` where
    ``sources`` is the explicit/behavior split — the §4.4 tripwire ratio computed
    for free, a one-line query the acceptance gate reads. A loop with no
    disposition yet returns ``{rows: []}`` (never an error).

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    bad = _check_name(name)
    if bad:
        return bad
    refused = _owner_refusal_for(name, owner)
    if refused:
        return refused
    rows = [e for e in _read_jsonl(report.disposition_log(name)) if isinstance(e, dict)]
    sources = {"explicit": 0, "behavior": 0}
    verbs: dict[str, int] = {}
    origin_mix: dict[str, int] = {}
    for r in rows:
        s = r.get("source")
        if s in sources:
            sources[s] += 1
        v = r.get("verb")
        if v:
            verbs[v] = verbs.get(v, 0) + 1
        o = r.get("origin")
        if o:
            origin_mix[o] = origin_mix.get(o, 0) + 1
    return {"ok": True, "loop": name, "rows": rows,
            "counts": {"total": len(rows), "verbs": verbs},
            "origins": origin_mix, "sources": sources}


@mcp.tool()
def loop_project_dispositions(project: str = "",
                              origin: str = origins.LOCAL_ORIGIN_ID) -> dict[str, Any]:
    """The per-project disposition AGGREGATE (REDESIGN-SPEC §5.4: disposition
    "shown back on loop cards + aggregated per-project" — the raw material for the
    fleet, instead of a JSONL no user ever sees again). Scopes to the loops whose
    confident project (:func:`schema.resolve_project`) equals ``project`` (empty ⇒
    the whole origin), reads each loop's CURRENT verdict (the single replaceable
    good/ok/bad, not the append log), and folds them into one honest tally::

        {ok, project, origin, good, ok_count, bad, rated, total, goodRate,
         loops: [{loop, verb, note}]}

    Only the current verdict per loop counts, so re-rating can't inflate the rate;
    ``goodRate`` is ``None`` (never a fake 0%) until at least one loop is rated.
    Read-only; an unknown origin → ``{"ok": False, "error": ...}``."""
    def work():
        root = _data_root_for(origin)
        if root is None:
            return {"ok": False,
                    "error": f"unknown origin {origin!r} (not visible from this box)"}
        want = project.strip() if isinstance(project, str) else ""
        items: list[dict[str, Any]] = []
        for entry in (sorted(os.listdir(root)) if os.path.isdir(root) else []):
            if entry.startswith("_"):
                continue
            cfg = _read_json(os.path.join(root, entry, "config.json"))
            if cfg is None:
                continue
            if want and schema.resolve_project(cfg, loop_name=entry) != want:
                continue
            run = _read_json(os.path.join(root, entry, "run.json")) or {}
            rows = ([e for e in _read_jsonl(
                        os.path.join(root, entry, "dispositions.jsonl"))
                     if isinstance(e, dict)]
                    if origin == origins.LOCAL_ORIGIN_ID else [])
            items.append({"loop": entry,
                          "current": resolution.current_disposition(
                              rows, run_started=run.get("started"))})
        agg = resolution.aggregate_dispositions(items)
        # keep the JSON key stable (``ok_count`` for the tally, ``ok`` for status)
        agg["ok_count"] = agg.pop("ok")
        return {"ok": True, "project": want or None, "origin": origin, **agg}
    return _run(work)


# ── §6 targets (a): the Issues reactive stream ──────────────────────────────
# REDESIGN-SPEC §3 (rd-issues): issues are a project-scoped reactive stream that
# loops FILL (file-and-keep-going) and loops DRAIN (point a loop → it resolves).
# These are the DATA MODEL + storage tools; the §6 stream UI (next round) paints
# them, and the composer's ＋Target reads them so a loop can be born pointed.
def _issue_project_of(row: dict, cache: dict) -> Optional[str]:
    """The project an issue belongs to: its explicit ``project`` field when
    present (filed issues + new crashes carry it), else — for a legacy crash row
    that predates the field — derived from the crashed loop's config, so old data
    still scopes honestly instead of vanishing from every project view. ``None``
    when unattributed (a legacy stored ``"Unattributed"`` label reads as None too;
    the label is display-only — loopyard-bug-1790089840)."""
    proj = row.get("project")
    if proj and proj != "Unattributed":
        return proj
    loop = row.get("loop")
    if not loop:
        return None
    if loop in cache:
        return cache[loop]
    cfg = _read_json(_config_path(loop)) or {}
    resolved = schema.resolve_project(cfg, loop_name=loop)
    cache[loop] = resolved
    return resolved


@mcp.tool()
def loop_issue_file(project: str, title: str, kind: str = "bug", body: str = "",
                    source: str = "you", severity: str = "normal",
                    loop: str = "") -> dict[str, Any]:
    """FILE an issue into a project's reactive stream (§3.2) — first-class for
    BOTH a human (``source="you"``, via ＋ File an issue) and a running loop
    (``source="loop"`` + ``loop=<name>``, the skill a loop calls to "set down" an
    out-of-scope fault and keep going — the volume driver). An empty ``project`` is
    stored as ``None`` (homeless but honest; the UI labels it "Unattributed"); ``kind`` ∈
    crash/bug/follow-up/blocked/idea-overflow and ``severity`` ∈ low/normal/high
    snap to a safe default on a typo so the one-call gesture never fails. Returns
    ``{ok, issue}`` (the stored record with its id + ``status:"open"``)."""
    def work():
        rec = issues.build_inbox_issue(
            project, title, body=body, kind=kind, source=source,
            severity=severity, loop=(loop.strip() or None) if isinstance(loop, str) else None)
        stored = issues.LocalIssueSink(log=lambda m: print(m)).file_issue(rec)
        if stored is None:
            return {"ok": False, "error": "could not file issue (see server log)"}
        return {"ok": True, "issue": stored}
    return _run(work)


@mcp.tool()
def loop_issues_list(project: str = "", status: str = "", kind: str = "",
                     include_resolved: bool = True) -> dict[str, Any]:
    """The project-scoped issue STREAM (§3.1): all issues whose project equals
    ``project`` (empty ⇒ the cross-project "all my projects" view), newest last.
    Optional ``status`` / ``kind`` filters narrow the feed; ``include_resolved``
    (default true) can be turned off for the default "open work" view. Every card
    carries ``project / kind / source / severity / status / title`` — the fields
    §3.1 renders — plus a resolved ``project`` for legacy crash rows. Returns
    ``{ok, project, issues, count, counts}`` where ``counts`` tallies by status."""
    def work():
        want = project.strip() if isinstance(project, str) else ""
        rows = issues.LocalIssueSink().list_issues()
        cache: dict = {}
        out: list = []
        counts: dict = {s: 0 for s in issues.STATUSES}
        for row in rows:
            proj = _issue_project_of(row, cache)
            if want and proj != want:
                continue
            st = row.get("status", "open")
            if status and st != status:
                continue
            if kind and row.get("kind") != kind:
                continue
            if not include_resolved and st in ("resolved", "dismissed"):
                continue
            enriched = {**row, "project": proj}
            out.append(enriched)
            counts[st] = counts.get(st, 0) + 1
        return {"ok": True, "project": want or None, "issues": out,
                "count": len(out), "counts": counts}
    return _run(work)


@mcp.tool()
def loop_issue_get(id: str) -> dict[str, Any]:
    """One issue's full record — title/body/project/kind/source/severity, the
    status timeline (``triage``), and (for a crash) the forensic detail (recovery
    attempts, transcript path, log slice) that rides along. ``{ok, issue}`` or an
    honest not-found."""
    def work():
        rec = issues.LocalIssueSink().get_issue(id)
        if rec is None:
            return {"ok": False, "error": f"no issue {id!r}"}
        return {"ok": True, "issue": rec}
    return _run(work)


@mcp.tool()
def loop_issue_triage(id: str, status: str, note: str = "",
                      actor: str = "you") -> dict[str, Any]:
    """TRIAGE an issue — move it along the lifecycle (§3.3):
    ``open → snoozed / resolving / resolved / dismissed``, and reopen from any
    terminal state. ``resolving`` is the "a loop is pointed at this" state. The
    :data:`issues.STATUS_TRANSITIONS` state machine is enforced, so a card can
    never jump to an illegal state or fake a resolution — an illegal move returns
    an honest error listing the legal targets. Returns ``{ok, issue}``."""
    def work():
        rec = issues.LocalIssueSink(log=lambda m: print(m)).set_status(
            id, status, note=note, actor=actor)
        if rec is None:
            existing = issues.LocalIssueSink().get_issue(id)
            if existing is None:
                return {"ok": False, "error": f"no issue {id!r}"}
            cur = existing.get("status", "open")
            legal = sorted(issues.STATUS_TRANSITIONS.get(cur, set()))
            return {"ok": False, "error": f"illegal transition {cur!r}→{status!r}",
                    "from": cur, "legal": legal}
        return {"ok": True, "issue": rec}
    return _run(work)


# ── §6 targets (b): the Idea Hub living-document workspace ───────────────────
# REDESIGN-SPEC §3 (rd-ideahub): per-project living docs on ONE substrate whose
# note→proposal→objective state is DERIVED (never chosen) — pointing a loop is the
# promotion. DATA MODEL + CRUD only; the §6 two-pane editor (next round) paints it.
def _doc_view(rec: dict, resolve=None) -> dict:
    """A doc record as the Hub serves it: derived ``state``/``glyph`` plus
    ``loop_origins`` — ``{loop name: origin}`` for every pointed loop, derived at
    read time (loopyard-bug-1790089839) so a doc's loop chips link
    ``/loops/<origin>/<name>`` instead of assuming ``local``. ``loops`` stays the
    plain name list (back-compat)."""
    resolve = resolve or issues.loop_origin
    names = [l for l in (rec.get("loops") or []) if isinstance(l, str) and l]
    return {**rec, "state": rec.get("state") or ideahub.derive_state(rec),
            "glyph": rec.get("glyph") or ideahub.glyph_for(rec),
            "loop_origins": {l: resolve(l) for l in names}}


@mcp.tool()
def loop_doc_create(project: str = "", title: str = "", body: str = "",
                    loop_rewrite: bool = False) -> dict[str, Any]:
    """CREATE a Hub doc — zero-friction capture (§2.2: ＋ New doc types in <1s, no
    type picker, no required project). A new doc has no loops and is not ready → it
    derives as a **note**. ``loop_rewrite`` sets the read-only-canvas vs
    loop-may-rewrite toggle up front (default read-only). Returns ``{ok, doc}`` with
    the stored record + its derived ``state``/``glyph``."""
    def work():
        rec = ideahub.build_doc(project, title, body=body, loop_rewrite=loop_rewrite)
        stored = ideahub.LocalDocStore(log=lambda m: print(m)).create(rec)
        if stored is None:
            return {"ok": False, "error": "could not create doc (see server log)"}
        return {"ok": True, "doc": _doc_view(stored)}
    return _run(work)


@mcp.tool()
def loop_docs_list(project: str = "") -> dict[str, Any]:
    """The project-scoped Hub RAIL (§2.1): the index of docs whose project equals
    ``project`` (empty ⇒ the cross-project "everything" view), newest last. Each
    row carries the derived ``state`` + ``glyph`` on the note→proposal→objective
    spectrum and a ``loop_count`` for the ``●LOOP`` badge — enough to render the
    rail without opening every file. ``{ok, project, docs, count, counts}`` where
    ``counts`` tallies by derived state."""
    def work():
        want = project.strip() if isinstance(project, str) else ""
        rows = ideahub.LocalDocStore().list_docs()
        out: list = []
        counts: dict = {s: 0 for s in ideahub.STATES}
        resolve = issues._cached(issues.loop_origin)
        for row in rows:
            if want and (row.get("project") or "") != want:
                continue
            row = _doc_view(row, resolve)
            out.append(row)
            st = row.get("state", "note")
            counts[st] = counts.get(st, 0) + 1
        return {"ok": True, "project": want or None, "docs": out,
                "count": len(out), "counts": counts}
    return _run(work)


@mcp.tool()
def loop_doc_get(id: str) -> dict[str, Any]:
    """One doc's full record — the living body, its ``loops``/``result``/history,
    and the derived ``state``/``glyph``. ``{ok, doc}`` or an honest not-found."""
    def work():
        rec = ideahub.LocalDocStore().get(id)
        if rec is None:
            return {"ok": False, "error": f"no doc {id!r}"}
        return {"ok": True, "doc": _doc_view(rec)}
    return _run(work)


@mcp.tool()
def loop_doc_update(id: str, title: str = "", body: str = "", ready: str = "",
                    loop_rewrite: str = "", actor: str = "you",
                    result: str = "") -> dict[str, Any]:
    """UPDATE a doc in place (§2.4). Only the fields you pass change: a non-empty
    ``title``/``body`` rewrites it; ``ready``/``loop_rewrite`` accept ``"true"``/
    ``"false"`` to flip a flag (empty = leave unchanged). A loop (``actor`` other
    than ``"you"``) may only rewrite the BODY when the doc is flagged
    loop-may-rewrite — a read-only canvas rejects the write honestly (the toggle is
    enforced in the store). ``result`` = ``"set"`` marks the doc DONE (a green
    ribbon), ``"clear"`` reopens it — OWNER only (``actor="you"``): a loop can
    never mark its own objective done. Returns ``{ok, doc}`` with the new derived
    state."""
    def work():
        def _flag(v):
            return None if v == "" else str(v).strip().lower() in ("1", "true", "yes", "on")
        store = ideahub.LocalDocStore(log=lambda m: print(m))
        act = (result or "").strip().lower()
        if act:
            if act not in ("set", "clear"):
                return {"ok": False, "error": f"result must be 'set' or 'clear', not {result!r}"}
            if actor != ideahub.OWNER_ACTOR:
                return {"ok": False,
                        "error": f"only the owner may {act} a doc result (actor {actor!r} refused)"}
            if store.owner_result(id, act, actor=actor) is None:
                return {"ok": False, "error": f"no doc {id!r}"}
            if all(v == "" for v in (title, body, ready, loop_rewrite)):
                return {"ok": True, "doc": _doc_view(store.get(id) or {"id": id})}
        rec = store.update(
            id,
            title=(title if title != "" else None),
            body=(body if body != "" else None),
            ready=_flag(ready), loop_rewrite=_flag(loop_rewrite), actor=actor)
        if rec is None:
            if store.get(id) is None:
                return {"ok": False, "error": f"no doc {id!r}"}
            return {"ok": False,
                    "error": f"loop {actor!r} may not rewrite a read-only doc"}
        return {"ok": True, "doc": _doc_view(rec)}
    return _run(work)


@mcp.tool()
def loop_doc_point(id: str, loop: str, actor: str = "you") -> dict[str, Any]:
    """POINT a loop at a doc — the gesture that PROMOTES it to an objective (§2.2:
    "the act of pointing a loop is what promotes the doc to an objective"). Records
    the loop on the doc (idempotent) so it derives as ``objective`` and shows a
    ``●LOOP`` badge; the composer's ＋Target calls this when a loop is born pointed
    at a Hub doc. Returns ``{ok, doc}`` with the promoted state."""
    def work():
        store = ideahub.LocalDocStore(log=lambda m: print(m))
        name = (loop or "").strip()
        rec = store.point_loop(id, name, actor=actor)
        if rec is None:
            return {"ok": False, "error": f"no doc {id!r}"}
        # pointed AFTER the loop already finished: its terminal fold has come and
        # gone, so fold now or the objective stays ◔ forever (bug-1790578879)
        if name:
            _maybe_fold_doc_result(name, doc_ids=[rec["id"]])
            rec = store.get(rec["id"]) or rec
        return {"ok": True, "doc": _doc_view(rec)}
    return _run(work)


@mcp.tool()
def loop_doc_move(loops: list[str], to: str = "", actor: str = "you") -> dict[str, Any]:
    """MOVE loops into a plan — a plan is the loop's workstream, so a loop sits in
    at most ONE plan. Each loop is taken off every other doc that lists it, then
    pointed at ``to`` (the same promotion + result fold as :func:`loop_doc_point`).
    ``to=""`` takes the loops out of every plan. Returns ``{ok, doc, moved,
    left}``: the target doc (or None), the loop names moved, and the ids of the
    docs they left."""
    if isinstance(loops, str):
        loops = [loops]
    def work():
        store = ideahub.LocalDocStore(log=lambda m: print(m))
        target = (to or "").strip()
        if target and store.get(target) is None:
            return {"ok": False, "error": f"no doc {target!r}"}
        names = [n.strip() for n in (loops or []) if isinstance(n, str) and n.strip()]
        if not names:
            return {"ok": False, "error": "no loops to move"}
        left: list = []
        for row in store.list_docs():
            did = row.get("id")
            if not did or did == target:
                continue
            rec = store.get(did) or {}
            for n in names:
                if n in (rec.get("loops") or []):
                    store.unpoint_loop(did, n, actor=actor)
                    if did not in left:
                        left.append(did)
        doc = None
        if target:
            for n in names:
                store.point_loop(target, n, actor=actor)
            # the plan's result speaks for its most recent loop, whichever ones just moved in
            def _ran(n):
                run = _read_json(_run_path(n)) or {}
                return run.get("updated") or run.get("started") or 0
            members = (store.get(target) or {}).get("loops") or names
            _maybe_fold_doc_result(max(members, key=_ran), doc_ids=[target])
            doc = _doc_view(store.get(target) or {"id": target})
        return {"ok": True, "doc": doc, "moved": names, "left": left}
    return _run(work)


@mcp.tool()
def loop_doc_backfill_results(apply: bool = False, exclude: Optional[list[str]] = None,
                              only: Optional[list[str]] = None) -> dict[str, Any]:
    """BACKFILL Hub doc results for loops that already finished
    (:func:`doc_results_backfill`): writes the green ribbon onto docs whose pointed
    loops all ended and the last one finished clean, and re-indexes stale rail
    rows. Idempotent. DRY-RUN by default — pass ``apply=True`` to write.
    ``exclude=[doc_id, ...]`` leaves those docs untouched (e.g. an objective its
    loop only partly delivered); ``only=[doc_id, ...]`` touches just those. Held-back
    docs are listed in ``skipped`` with ``why: "excluded"``; ids matching no doc come
    back in ``unmatched``. CLI:
    ``python -m mcp_loops.doc_backfill [--apply] [--exclude ID]... [--only ID]...``."""
    if isinstance(exclude, str):
        exclude = [exclude]
    if isinstance(only, str):
        only = [only]
    return _run(lambda: doc_results_backfill(apply=bool(apply), exclude=exclude, only=only))


@mcp.tool()
def loop_doc_build_loop(id: str, actor: str = "you") -> dict[str, Any]:
    """POINT-A-LOOP-AT-THIS-DOC — the Idea Hub core gesture (§rd-ideahub): build a
    real loop *from* the doc, born already pointed at it. Where :func:`loop_doc_point`
    records an EXISTING loop on a doc, this is the one-call gesture the ▶ button
    needs so the Hub is a workspace, not a notes app. In one call it (1) seeds a
    valid reusable-team loop config from the doc's title+body
    (:func:`hub_capabilities.build_loop_from_objective`, bound to the doc's project),
    (2) saves it, (3) flips the doc to ``loop_rewrite`` so the loop MAY edit it in
    place (§2.4 — pointing a loop invites the rewrite), and (4) points the loop at
    the doc, which PROMOTES it to an objective (``◔`` + ``●LOOP``). When that loop
    finishes, its computed Resolution folds back into the doc as a RESULT ribbon
    (:func:`_maybe_fold_doc_result`). Returns ``{ok, loop, config, doc}`` — the saved
    draft (nothing started) + the promoted doc; the caller starts it with
    ``loop_start``, the same lifecycle as any loop (§7)."""
    def work():
        store = ideahub.LocalDocStore(log=lambda m: print(m))
        rec = store.get(id)
        if rec is None:
            return {"ok": False, "error": f"no doc {id!r}"}
        title = (rec.get("title") or "").strip() or "(untitled)"
        cfg = hub_capabilities.build_loop_from_objective(
            rec.get("project") or "", title, rec.get("body") or "", cap_id="ideahub")
        saved = loop_save(cfg)
        if saved.get("error"):
            return saved
        name = saved["name"]
        # Invite the loop to rewrite the doc in place, THEN point it (the promotion).
        store.update(id, loop_rewrite=True, actor=actor)
        rec = store.point_loop(id, name, actor=actor)
        if rec is None:                       # doc vanished between reads — honest fail
            return {"ok": False, "error": f"saved loop {name!r} but doc {id!r} vanished"}
        return {"ok": True, "loop": name, "config": cfg,
                "doc": _doc_view(rec)}
    return _run(work)


# ── §8 capstone: the objective-manager thread (Q2) ───────────────────────────
# A durable, doc-anchored conversation that steers a Hub objective — backed by an
# attached SESSION (a connect connector), never tmux and never a faked chat. The
# thread stores YOU turns + pending AGENT turns; an agent turn only gets real text
# when the backing session claims the dispatched task, does the work, and returns.
def _thread_store() -> "threads.LocalThreadStore":
    return threads.LocalThreadStore(log=lambda m: print(m))


def _connector_live(connector_id: Optional[str]) -> Optional[dict]:
    """The backing session's connect record annotated with a ``live`` flag (seen
    within the stale window), or ``None`` when it was never registered. Honest:
    liveness is computed from the real heartbeat, never assumed."""
    if not connector_id:
        return None
    rec = connect.get_connector(_connect_root(), connector_id)
    if rec is None:
        return None
    seen = rec.get("lastSeen") or 0.0
    rec = {**rec, "live": bool(seen) and (time.time() - seen) <= connect.DEFAULT_STALE_AFTER}
    return rec


def _thread_envelope_for(task_id):
    """Lookup passed to the store's fold: the returned ENVELOPE for a dispatched
    task once it is terminal, else ``None`` (so a still-running turn stays pending —
    the thread never fabricates the reply)."""
    if not task_id:
        return None
    task = connect.get_task(_connect_root(), task_id)
    if task is None or task.get("status") not in connect.TERMINAL:
        return None
    if task.get("status") == connect.CANCELED:
        # canceled carries no result — fold it closed (no text) instead of leaving
        # the turn "working…" forever.
        return {"status": "canceled"}
    return task.get("result")


def _thread_task_for(task_id):
    """The raw connect task (for the read-time pending sub-state), or ``None``."""
    return connect.get_task(_connect_root(), task_id) if task_id else None


def _thread_view(doc_id: str) -> dict[str, Any]:
    """Fold any completed turns, then compose the read model the right pane paints:
    the thread messages, the docked doc (with derived state/glyph), and the HONEST
    :func:`threads.describe_state`. Shared by get/attach/post so all three return the
    same shape."""
    store = _thread_store()
    store.get_or_create(doc_id)
    thread = store.fold(doc_id, _thread_envelope_for) or store.get(doc_id)
    doc = ideahub.LocalDocStore().get(doc_id)
    connector = _connector_live(thread.get("session")) if thread else None
    state = threads.describe_state(thread or {}, connector)
    if thread:
        thread = threads.annotate_pending(thread, _thread_task_for)
    doc_view = None
    if doc is not None:
        doc_view = {**doc, "state": ideahub.derive_state(doc),
                    "glyph": ideahub.glyph_for(doc)}
    try:
        connectors = connect.list_connectors(_connect_root())
    except Exception:  # noqa: BLE001 — a roster hiccup reads as "no choices", not a crash
        connectors = []
    view = {"ok": True, "doc_id": doc_id, "thread": thread,
            "doc": doc_view, "session_state": state,
            "sessions": threads.session_options(connectors, (thread or {}).get("session"))}
    view["rev"] = threads.thread_revision(view)
    return view


@mcp.tool()
def loop_thread_get(doc_id: str, owner: str = "") -> dict[str, Any]:
    """The OBJECTIVE-MANAGER THREAD for a Hub doc (REDESIGN-SPEC §8 / Q2) — the
    durable, doc-anchored conversation the right pane renders. Creates an empty
    unattached thread on first open (an objective always *has* a thread), folds any
    agent turns whose backing session has since returned, and reports the honest
    ``session_state`` (unattached / offline / awaiting / ready). Returns
    ``{ok, doc_id, thread, doc, session_state}``. Read-only apart from the lazy
    create + the fold of already-returned work; never fabricates a reply.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read of an EXISTING
    thread/doc (``{error, refused:"cross_owner"}``, nothing created or folded);
    threads and docs carry no owner yet, so they resolve to the local default."""
    return _run(lambda: _thread_owner_refusal(doc_id, owner) or _thread_view(doc_id))


def _thread_owner_refusal(doc_id: str, owner: str) -> Optional[dict]:
    """The read-by-name guard for a doc-anchored thread: the thread's own
    ``owner``, else its doc's, else the local default (same one rule). An
    unknown doc/thread is not refused (the lazy-create path answers as before)."""
    if not (owner or "").strip() or not isinstance(doc_id, str):
        return None
    th = _thread_store().get(doc_id)
    doc = ideahub.LocalDocStore().get(doc_id)
    if not isinstance(th, dict) and not isinstance(doc, dict):
        return None
    own = (th if isinstance(th, dict) else {}).get("owner") \
        or (doc if isinstance(doc, dict) else {}).get("owner")
    return schema.owner_refusal(doc_id, {"owner": own}, owner)


@mcp.tool()
def loop_thread_list(project: str = "") -> dict[str, Any]:
    """The CHAT INDEX: one row per Hub doc in ``project`` (empty ⇒ every project)
    with a summary of its objective-manager thread: attached session, honest
    state, message count, pending turns and a preview of the last real message.
    Docs with no thread yet are listed with ``has_thread: False``. Nothing is
    created and no turn is folded, so this is a pure read. ``projects`` is the
    real Loopyard project registry (pure read, no gather) so the Chat surface
    picks from real projects, never a hard-coded one. Returns
    ``{ok, project, project_known, projects, threads, count}``, with rows that
    have a thread first, most recently updated first."""
    def work():
        want = project.strip() if isinstance(project, str) else ""
        reg = _project_list(False, False, origins.LOCAL_ORIGIN_ID)
        projects = [{"id": p.get("id"), "name": p.get("name") or p.get("id")}
                    for p in (reg.get("products") or []) if p.get("id")]
        store = _thread_store()
        try:
            live = {c.get("id"): c for c in connect.list_connectors(_connect_root())}
        except Exception:  # noqa: BLE001
            live = {}
        rows = []
        seen = set()
        for doc in ideahub.LocalDocStore().list_docs():
            if want and (doc.get("project") or "") != want:
                continue
            seen.add(doc.get("id"))
            th = store.get(doc.get("id"))
            rows.append(threads.summarize_thread(
                doc, th, live.get((th or {}).get("session"))))
        if not want:
            # a thread whose doc is gone stays reachable in the all-projects view
            for did in store.list_ids():
                if did not in seen:
                    th = store.get(did)
                    rows.append(threads.summarize_thread(
                        {"id": did, "title": f"(missing doc {did})"}, th,
                        live.get((th or {}).get("session"))))
        rows.sort(key=lambda r: (not r["has_thread"], -(r.get("updated") or 0)))
        return {"ok": True, "project": want or None,
                "project_known": (not want) or any(p["id"] == want for p in projects),
                "projects": projects, "threads": rows, "count": len(rows)}
    return _run(work)


@mcp.tool()
def loop_thread_attach(doc_id: str, session: str = "") -> dict[str, Any]:
    """ATTACH the backing session (a connected connector) the thread's agent turns
    run on — Q2's "the runtime is a session." Pass a connector id to attach, or an
    empty string to detach back to the honest unattached state. This names compute;
    it fabricates nothing. If the id isn't a live connector the thread simply reads
    as ``offline`` until it reconnects (an honest state, not an error). Returns the
    refreshed thread view."""
    def work():
        store = _thread_store()
        before = store.get(doc_id) or {}
        target = (session or "").strip() or None
        canceled = []
        if before.get("session") and before.get("session") != target:
            # Switching/detaching strands turns still QUEUED for the old session:
            # cancel those (they fold closed, no text) rather than leave them
            # "queued" forever or let them run on a session the human left. A turn
            # the old session already CLAIMED is left alone — it's really running
            # and folds from its real return.
            for tid in threads.pending_task_ids(before):
                task = connect.get_task(_connect_root(), tid)
                if task and task.get("status") == connect.PENDING:
                    if "error" not in connect.cancel_task(_connect_root(), tid):
                        canceled.append(tid)
        rec = store.attach(doc_id, session)
        if rec is None:
            return {"ok": False, "error": f"could not attach session to {doc_id!r}"}
        view = _thread_view(doc_id)
        view["canceled_tasks"] = canceled
        return view
    return _run(work)


@mcp.tool()
def loop_thread_post(doc_id: str, text: str) -> dict[str, Any]:
    """POST a message to the objective-manager thread. With a LIVE backing session
    attached, this (1) records your words, (2) dispatches a connect task to that
    session — framed around the live doc, the conversation, and your message, naming
    the gestures it may make (rewrite the doc / file an issue / point a loop), and
    (3) records a *pending* agent turn tied to that task. The reply folds in later
    (:func:`loop_thread_get`) from the session's REAL returned envelope — never a
    fabricated one. With NO live session, your words are kept but nothing is faked:
    the thread reports ``unattached``/``offline`` so the UI prompts you to attach a
    session first. Returns the refreshed thread view (+ ``dispatched``/``task_id``)."""
    def work():
        msg = (text or "").strip()
        if not msg:
            return {"ok": False, "error": "message text is required"}
        store = _thread_store()
        thread = store.get_or_create(doc_id)
        if thread is None:
            return {"ok": False, "error": f"could not open thread for {doc_id!r}"}
        connector = _connector_live(thread.get("session"))
        live = bool(connector and connector.get("live"))
        if not live:
            # Honest: keep the human's words (undispatched), fabricate no reply.
            store.append(doc_id, threads.build_user_message(thread, msg, dispatched=False))
            view = _thread_view(doc_id)
            view["dispatched"] = False
            return view
        doc = ideahub.LocalDocStore().get(doc_id) or {"id": doc_id}
        store.append(doc_id, threads.build_user_message(thread, msg, dispatched=True))
        thread = store.get(doc_id)
        prompt = threads.dispatch_prompt(doc, thread, msg)
        try:
            task = connect.enqueue_task(
                _connect_root(), prompt=prompt, connector=thread.get("session"),
                spec={"kind": "objective-manager", "doc_id": doc_id},
                origin_loop=None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        store.append(doc_id, threads.build_agent_turn(thread, task["id"]))
        view = _thread_view(doc_id)
        view["dispatched"] = True
        view["task_id"] = task["id"]
        return view
    return _run(work)


# ── §2 project-scoped nav counts ─────────────────────────────────────────────
# The scope id the UI uses for "no project binding" (frontend PROJECT_UNATTR).
UNATTRIBUTED_SCOPE = "__unattributed__"


@mcp.tool()
def loop_project_counts(project: str = "",
                        origin: str = origins.LOCAL_ORIGIN_ID) -> dict[str, Any]:
    """Per-project counts for EVERY nav item (REDESIGN-SPEC §2: picking a project
    rescopes the whole app — "nav counts become *this project's* counts"). The
    switcher reads this so the rail counts rescope in lockstep with the Loops list,
    not just the list. Returns::

        {ok, project, origin,
         loops, ideahub, issues,           # PROJECT-SCOPED (empty project ⇒ all)
         origins, origins_reachable, sessions}   # fleet-wide (compute, not project-owned)

    Every number derives from the SAME source + filter as the feed it summarises
    (loopyard-bug-1790089837), so a badge never disagrees with the page behind it:

    * ``loops`` — non-archived loops whose confident :func:`schema.resolve_project`
      equals ``project`` (the loops feed + its archived toggle);
    * ``ideahub`` — the docs in that project (``loop_docs_list``);
    * ``issues`` — OPEN WORK issues (open / snoozed / resolving — everything the
      ``loop_issues_list`` default shows, i.e. not resolved / dismissed);
    * ``origins`` / ``origins_reachable`` — the compute fleet (``loop_fleet_summary``:
      mirrors + live-enrolled origins, connected sessions excluded);
    * ``sessions`` — the Sessions roster count (``loop_sessions_list``).

    ``project="__unattributed__"`` scopes to records with NO project binding (the
    UI's "Unattributed" bucket, which has no slug of its own)."""
    def work():
        want = project.strip() if isinstance(project, str) else ""
        unattr = want == UNATTRIBUTED_SCOPE
        root = _data_root_for(origin)
        if root is None:
            return {"ok": False,
                    "error": f"unknown origin {origin!r} (not visible from this box)"}

        def in_scope(pid) -> bool:
            if unattr:
                return not pid
            return not want or pid == want
        # Loops — confident project match, archived excluded (mirror loop_list).
        loops_n = 0
        for entry in (sorted(os.listdir(root)) if os.path.isdir(root) else []):
            if entry.startswith("_"):
                continue
            cfg = _read_json(os.path.join(root, entry, "config.json"))
            if cfg is None:
                continue
            if (_read_json(os.path.join(root, entry, "run.json")) or {}).get("archived"):
                continue
            if not in_scope(schema.resolve_project(cfg, loop_name=entry)):
                continue
            loops_n += 1
        # Idea Hub docs + open-work Issues — the §6 stores, project-scoped.
        docs = [d for d in ideahub.LocalDocStore().list_docs()
                if in_scope(d.get("project") or None)]
        icache: dict = {}
        open_issues = 0
        for row in issues.LocalIssueSink().list_issues():
            if not in_scope(_issue_project_of(row, icache)):
                continue
            if row.get("status", "open") not in ("resolved", "dismissed"):
                open_issues += 1
        # Origins + sessions — fleet-wide (compute is not owned by a project), from
        # the same read-models as the Fleet pill and the Sessions roster.
        now = time.time()
        fleet = origins.fleet_summary(_origins_with_live(now))
        try:
            croot = _connect_root()
            roster = sessions.build_roster(connect.list_connectors(croot, now=now),
                                           connect.list_tasks(croot), now=now)
            n_sessions = int(roster.get("count") or 0)
        except Exception:  # noqa: BLE001 — a connect-store hiccup is an honest 0
            n_sessions = 0
        return {"ok": True, "project": want or None, "origin": origin,
                "loops": loops_n, "ideahub": len(docs), "issues": open_issues,
                "origins": fleet.get("origins", 0),
                "origins_reachable": fleet.get("reachable", 0),
                "sessions": n_sessions}
    return _run(work)


def _owner_refusal_for(name: str, owner: str,
                       origin: str = origins.LOCAL_ORIGIN_ID) -> Optional[dict]:
    """Phase 5 read-by-name guard for the envelope tools: the cross-owner
    refusal for ``name`` on ``origin`` (None when owner is unset, the loop is
    unknown — its not_found envelope answers — or the owner matches). A loop
    with only a run.json resolves to the local default owner."""
    if not (owner or "").strip() or _check_name(name):
        return None
    base = _status_dir_for(origin, name)
    if base is None:
        return None
    cfg = _read_json(os.path.join(base, "config.json"))
    if cfg is None and _read_json(os.path.join(base, "run.json")) is None:
        return None
    return schema.owner_refusal(name, cfg if cfg is not None else {}, owner)


@mcp.tool()
def get_loop_result(name: str, owner: str = "") -> dict[str, Any]:
    """APP-CONNECT (CAP-2 inbound): pull the RESULT ENVELOPE for a dispatched
    task — the canonical, structured result an external app reads back
    (task_id, status, done, summary, artifacts, git_commit_or_null,
    verification, duration, turns, retired). Works at any point in the run;
    ``done`` tells the caller whether the envelope is terminal. An unknown loop
    returns an envelope with ``status="not_found"`` (not an error) so pollers
    have one shape to handle. ``owner`` (optional, Phase 5 seam) refuses a loop
    of another owner (``{error, refused:"cross_owner"}``, no envelope)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        return _envelope_for(name)
    return _run(work)


@mcp.tool()
def loop_result_view(name: str,
                     origin: str = origins.LOCAL_ORIGIN_ID,
                     owner: str = "") -> dict[str, Any]:
    """HUB (P2 §b — inspect results, cross-origin): the SAME result envelope as
    get_loop_result, but for a loop on ANY connected origin — read-only, off that
    origin's synced mirror. ``origin`` defaults to local (identical to
    get_loop_result). Kept SEPARATE from the CAP-2 inbound app-connect tools so
    that pilot-verified contract (start_loop→poll→get_loop_result) stays exactly
    one-box; the hub/dashboard and future multi-client control plane read remote
    results through here. An unknown origin → ``{"error": ...}``; an unknown loop
    on a known origin → an envelope with ``status="not_found"`` (one shape for
    pollers, same as get_loop_result). ``owner`` (optional, Phase 5 seam)
    refuses a loop of another owner (``{error, refused:"cross_owner"}``)."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        if _status_dir_for(origin, name) is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        refused = _owner_refusal_for(name, owner, origin)
        if refused:
            return refused
        return _envelope_for(name, origin=origin)
    return _run(work)


# ── app-connect OUTBOUND (CAP-2 §2b): a loop hands a task to a connected in-app
# session and reads the result back. Two faces of one on-disk queue
# (mcp_loops.connect): the LOOP side dispatches (dispatch_task / dispatch_result),
# the CONNECTOR side — a session in the user's Codex App / Claude Code, driven by
# mcp_loops.connector — registers, polls, and returns a result envelope. This is
# how a loop hands INTERACTIVE / BROWSER work back to a human's session.
def _connect_root() -> str:
    return os.path.join(_local_mirror_path(), "_connect")


def _check_task_id(task_id) -> Optional[dict]:
    """Refuse a task id outside the connect grammar BEFORE it reaches the store
    (loopyard-bug-1790562011: raw ids were joined into a path → arbitrary .json
    read/write). connect.py re-checks + contains the resolved path too."""
    if not connect.valid_task_id(task_id):
        return {"error": f"invalid task id {task_id!r} (want t-[0-9a-z-]+)"}
    return None


def _check_connector_id(connector_id) -> Optional[dict]:
    if not connect.valid_connector_id(connector_id):
        return {"error": f"invalid connector id {connector_id!r} "
                         f"(want [A-Za-z0-9][A-Za-z0-9._-]*, max 128)"}
    return None


def _check_connector_secret(connector_id, secret) -> Optional[dict]:
    """loopyard-bug-1790562024: poll/return must present the secret minted at
    connect_register (hash-compared in constant time). One message for missing
    and wrong so it's no oracle."""
    if not connect.verify_connector(_connect_root(), connector_id, secret):
        return {"error": f"connector {connector_id!r}: missing or wrong secret "
                         f"(pass the secret connect_register returned)",
                "refused": "bad_secret"}
    return None


@mcp.tool()
def connect_register(connector_id: str, runtime: str = "claude",
                     capabilities: Optional[list] = None,
                     meta: Optional[dict] = None,
                     secret: str = "") -> dict[str, Any]:
    """CONNECTOR side: register (or refresh) a session in the user's app as a
    connector that can execute dispatched tasks. ``runtime`` is claude|codex;
    ``capabilities`` are free labels (e.g. ["browser","interactive"]) a task can
    require. A NEW connector id is issued a ``secret`` (returned ONCE — keep it):
    connect_poll / connect_return must present it. Re-registering an existing id
    requires its ``secret`` (then it just refreshes; no new secret is returned);
    only the box owner can reset a lost one (``python -m mcp_loops.connector
    reset <id>``). Returns ``{ok, connector, secret?}``."""
    def work():
        bad = _check_connector_id(connector_id)
        if bad:
            return bad
        caps = [str(c) for c in capabilities] if isinstance(capabilities, list) else None
        try:
            rec = connect.register_connector(
                _connect_root(), connector_id, runtime=runtime,
                capabilities=caps, meta=meta if isinstance(meta, dict) else None,
                secret=secret or None)
        except connect.ConnectorAuthError as exc:
            return {"error": str(exc), "refused": "bad_secret"}
        minted = rec.pop("secret", None)
        out = {"ok": True, "connector": rec}
        if minted:
            out["secret"] = minted
        return out
    return _run(work)


@mcp.tool()
def connect_poll(connector_id: str, secret: str = "") -> dict[str, Any]:
    """CONNECTOR side: heartbeat + claim the next task this connector can run.
    ``secret`` is the one connect_register issued. Returns ``{connector, task}``
    where ``task`` is the claimed task (execute it, then call connect_return with
    its id) or ``None`` when the queue has nothing for you. ``approved`` says
    whether this connector may claim UNTARGETED tasks: a self-registered id is
    not (it only gets tasks addressed to it by id) until the box owner runs
    ``python -m mcp_loops.connector approve <id>``; Session Attach connectors
    are. An unregistered connector or a missing/wrong secret gets
    ``{"error": ...}``."""
    def work():
        bad = _check_connector_id(connector_id)
        if bad:
            return bad
        if connect.get_connector(_connect_root(), connector_id) is None:
            return {"error": f"connector {connector_id!r} is not registered "
                             f"(call connect_register first)"}
        bad = _check_connector_secret(connector_id, secret)
        if bad:
            return bad
        rec = connect.touch_connector(_connect_root(), connector_id)
        task = connect.claim_next(_connect_root(), connector_id)
        return {"connector": connector_id, "task": task,
                "approved": connect.is_approved(rec)}
    return _run(work)


@mcp.tool()
def connect_return(task_id: str, status: str = "returned", summary: str = "",
                   artifacts: Optional[list] = None, git_commit: Optional[str] = None,
                   output: Any = None, error: str = "",
                   connector_id: str = "", secret: str = "") -> dict[str, Any]:
    """CONNECTOR side: return the result of a claimed task. Builds the result
    ENVELOPE (same shape as the inbound path — task_id, status, summary,
    artifacts, git_commit, duration, error) and marks the task terminal
    (``returned``|``failed``). ``connector_id`` must be the connector that
    claimed the task via connect_poll, with its ``secret``; an unclaimed
    (pending) task can't be returned. On ``status="failed"`` pass the session's real failure reason as
    ``error`` — it is stored verbatim and shown on the thread's failed turn.
    Returns the updated task or ``{"error": ...}``."""
    def work():
        bad = (_check_task_id(task_id) or _check_connector_id(connector_id)
               or _check_connector_secret(connector_id, secret))
        if bad:
            return bad
        task = connect.get_task(_connect_root(), task_id)
        if task is None:
            return {"error": f"unknown task {task_id!r}"}
        if task.get("status") != connect.CLAIMED or task.get("claimedBy") != connector_id:
            # loopyard-bug-1790562017: only the claimer returns, never a pending task
            return {"error": f"task {task_id!r} is not claimed by connector "
                             f"{connector_id!r}"}
        env = connect.connector_envelope(
            task, status=status, summary=summary,
            artifacts=artifacts if isinstance(artifacts, list) else None,
            git_commit=git_commit, output=output, error=error)
        res = connect.return_result(_connect_root(), task_id, connector_id=connector_id,
                                    result=env, status=status)
        if isinstance(res, dict) and res.get("error"):
            return res
        return {"ok": True, "task": res}
    return _run(work)


@mcp.tool()
def dispatch_task(prompt: str, connector: Optional[str] = None,
                  runtime: Optional[str] = None, capabilities: Optional[list] = None,
                  spec: Optional[dict] = None,
                  origin_loop: Optional[str] = None) -> dict[str, Any]:
    """LOOP side: dispatch a task to a connected in-app session. ``connector``
    targets a specific one (else any matching connector may claim it);
    ``runtime``/``capabilities`` constrain who can. A loop step calls this during
    its turn, then polls dispatch_result for the returned envelope. The response
    carries a ``dispatch_token``, returned ONLY here: KEEP IT and pass it to
    dispatch_result / dispatch_cancel (and connect_status) — without it the task
    can't be read or canceled, and it can't be recovered. The task id is random;
    ``origin_loop`` is recorded and re-checked but grants nothing by itself. A
    full queue (connect.MAX_PENDING waiting) is refused. Returns
    ``{ok, task_id, dispatch_token, task}`` or ``{"error": ...}``. Every call (queued or
    refused) appends one ``agent.task`` record to the dispatch audit (P3 D3)."""
    def work():
        if connector is not None:
            bad = _check_connector_id(connector)
            if bad:
                _audit_dispatch_task("deny", reason=bad.get("error"))
                return bad
        try:
            caps = [str(c) for c in capabilities] if isinstance(capabilities, list) else None
            task = connect.enqueue_task(
                _connect_root(), prompt=prompt, connector=connector, runtime=runtime,
                capabilities=caps, spec=spec if isinstance(spec, dict) else None,
                origin_loop=origin_loop)
        except ValueError as exc:
            _audit_dispatch_task("deny", reason=str(exc))
            return {"error": str(exc)}
        token = task.pop("dispatchToken")
        _audit_dispatch_task("allow", task_id=task["id"], result={"ok": True})
        return {"ok": True, "task_id": task["id"], "dispatch_token": token, "task": task}

    def _audit_dispatch_task(decision, *, reason=None, task_id=None, result=None):
        # D3: audit-only (no gating). The task goes to a connector session, not
        # an enrolled origin, so target = the named connector (``*`` = any
        # matching one); owner is unknown on a single-owner box. The prompt and
        # spec only ever reach the log as part of the args digest.
        try:
            _dispatch_audit_log().record(
                decision=decision, owner=None,
                source=f"loop:{origin_loop}" if origin_loop else "mcp:dispatch_task",
                target=connector or "*", verb="agent.task", method="dispatch_task",
                args={"prompt": prompt, "connector": connector, "runtime": runtime,
                      "capabilities": capabilities, "spec": spec,
                      "origin_loop": origin_loop},
                dispatch_id=task_id, reason=reason, result=result)
        except Exception:  # noqa: BLE001 — a lost audit line never fails the dispatch
            pass
    return _run(work)


def _own_task(task_id, origin_loop, dispatch_token) -> tuple[Optional[dict], Optional[dict]]:
    """(public task, None) iff ``task_id`` is valid, exists, ``dispatch_token``
    is the one dispatch_task returned for it, and ``origin_loop`` (if given)
    matches the recorded one — else (None, error). loopyard-bug-1790562030: a
    loop name is public, so it alone never grants access; a tokenless (legacy)
    task is readable only in-process by the owner. Every refusal is the same
    error as a missing id (no oracle)."""
    bad = _check_task_id(task_id)
    if bad:
        return None, bad
    task = connect.get_task(_connect_root(), task_id)
    if (task is None or not connect.verify_dispatch_token(task, dispatch_token)
            or (origin_loop and task.get("originLoop") != origin_loop)):
        return None, {"error": f"unknown task {task_id!r} (or wrong dispatch_token)"}
    return connect.public_task(task), None


@mcp.tool()
def dispatch_result(task_id: str, origin_loop: Optional[str] = None,
                    dispatch_token: str = "") -> dict[str, Any]:
    """LOOP side: read a dispatched task's state + returned envelope. ``done`` is
    True once the connector returned/failed (or it was canceled); until then
    ``status`` is pending|claimed and ``result`` is None. ``dispatch_token`` is
    REQUIRED — the one dispatch_task returned; ``origin_loop``, if passed, must
    match the one dispatched with. Returns ``{task_id, status, done, result,
    task}`` or ``{"error": ...}``."""
    def work():
        task, bad = _own_task(task_id, origin_loop, dispatch_token)
        if bad:
            return bad
        return {"task_id": task_id, "status": task.get("status"),
                "done": task.get("status") in connect.TERMINAL,
                "result": task.get("result"), "task": task}
    return _run(work)


@mcp.tool()
def dispatch_cancel(task_id: str, origin_loop: Optional[str] = None,
                    dispatch_token: str = "") -> dict[str, Any]:
    """LOOP side: cancel a dispatched task that hasn't returned yet.
    ``dispatch_token`` (from dispatch_task) is REQUIRED; ``origin_loop``, if
    passed, must match. Returns ``{ok, task}`` or ``{"error": ...}``."""
    def work():
        _, bad = _own_task(task_id, origin_loop, dispatch_token)
        if bad:
            return bad
        res = connect.cancel_task(_connect_root(), task_id)
        if isinstance(res, dict) and res.get("error"):
            return res
        return {"ok": True, "task": res}
    return _run(work)


@mcp.tool()
def connect_status(origin_loop: Optional[str] = None, connector_id: str = "",
                   secret: str = "", task_id: str = "",
                   dispatch_token: str = "") -> dict[str, Any]:
    """Visibility for the connect boundary: every registered connector (with a
    live/idle cue and its ``approved`` flag) plus queue-wide ``counts`` by status
    and ``total``. Read-only. ``tasks`` lists full task records ONLY to a
    credential holder: ``connector_id`` + its ``secret`` for the tasks addressed
    to / claimed by that connector, or ``task_id`` + the ``dispatch_token``
    dispatch_task returned for that one task. ``origin_loop`` alone is NOT a
    credential (loop names are public) and gets counts only, like an unscoped
    call (loopyard-bug-1790562030: listings leaked task ids, prompts, results)."""
    def work():
        root = _connect_root()
        tasks: list = []
        if connector_id:
            bad = (_check_connector_id(connector_id)
                   or _check_connector_secret(connector_id, secret))
            if bad:
                return bad
            tasks = connect.list_tasks(root, connector=connector_id)
        elif task_id:
            task, bad = _own_task(task_id, origin_loop, dispatch_token)
            if bad:
                return bad
            tasks = [task]
        return {"connectors": connect.list_connectors(root), "tasks": tasks,
                **connect.public_task_counts(root)}
    return _run(work)


# ── Session Attach: a UI-minted, short-lived, single-use token a local
# Claude/Codex session exchanges (on the gated /__attach route, never raw :8771)
# for a connector-scoped credential. Owner-side mint/list/revoke live here; the
# claim + scoped calls are served by tracking_ui/session_attach_route.py.
@mcp.tool()
def session_attach_token(owner: str = "", origin: str = "local", ttl: int = 0,
                         label: str = "", runtime: str = "claude",
                         base_url: str = "") -> dict[str, Any]:
    """Mint a SESSION ATTACH token: short-lived (``ttl`` seconds, default 900,
    clamped 60..3600), single-use, revocable, bound to ``owner`` + ``origin``.
    Returns ``{attachId, token, connectorId, attachUrl, mcpUrl, oneLiner,
    expiresAt, publicUrlConfigured}``. The plaintext token is returned only
    here; the store keeps its sha256. ``base_url`` (or
    ``$LOOPYARD_ATTACH_PUBLIC_URL``) is the public /__attach base the session
    dials; unset falls back to the loopback dashboard."""
    def work():
        from mcp_loops import session_attach
        return session_attach.mint(
            _connect_root(), owner=owner or None, origin=origin or "local",
            ttl=int(ttl or 0) or None, label=label or None, runtime=runtime,
            base_url=base_url or None)
    return _run(work)


@mcp.tool()
def session_attach_link(owner: str = "", ttl: int = 0, label: str = "",
                        runtime: str = "claude", origin: str = "local",
                        base_url: str = "") -> dict[str, Any]:
    """Mint a ONE-TIME SESSION ATTACH LINK for the Sessions page: the owner
    pastes the SHORT ``url`` (``<public-base>/session-attach/<token>``) to
    their AI. Fetching it once serves complete attach instructions (markdown,
    or ``?format=sh``) and attaches the session as a connector. Single-use,
    owner-bound, ``ttl`` seconds (default 1800, clamped 60..3600). Returns
    ``{url, expiresAt, attachId, connectorId, ttl, publicUrlConfigured}``;
    revoke with session_attach_revoke."""
    def work():
        from mcp_loops import session_attach
        m = session_attach.mint(
            _connect_root(), owner=owner or None, origin=origin or "local",
            ttl=int(ttl or 0) or None, label=label or None, runtime=runtime,
            base_url=base_url or None)
        return {k: m[k] for k in ("url", "expiresAt", "attachId", "connectorId",
                                  "owner", "ttl", "publicUrlConfigured")}
    return _run(work)


@mcp.tool()
def session_attach_list(owner: str = "") -> dict[str, Any]:
    """The owner's session attaches (pending / consumed / revoked / expired),
    newest first, with no secrets or hashes. Returns ``{attaches}``."""
    def work():
        from mcp_loops import session_attach
        return {"attaches": session_attach.list_attaches(_connect_root(),
                                                         owner=owner or None)}
    return _run(work)


@mcp.tool()
def session_attach_revoke(attach_id: str = "", connector_id: str = "",
                          owner: str = "") -> dict[str, Any]:
    """Revoke a pending attach token or an attached session, by ``attach_id`` or
    ``connector_id``. The session's credential stops working at once. Only the
    minting owner may revoke (``refused: wrong_owner``)."""
    def work():
        from mcp_loops import session_attach
        if not (attach_id or connector_id):
            return {"error": "pass attach_id or connector_id"}
        return session_attach.revoke(_connect_root(), owner=owner or None,
                                     attach_id=attach_id or None,
                                     connector_id=connector_id or None)
    return _run(work)


# ── mid-run owner input queue (#1: steer a running loop) ──────────────────────
# A free-text note the OWNER drops while a loop runs; the MANAGER drains it at
# the top of its next turn and re-plans. Stored as append-only JSONL with a
# separate integer cursor file marking how many notes the manager has consumed.
def _input_queue_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "input-queue.jsonl")


def _input_cursor_path(name: str) -> str:
    return os.path.join(report.status_dir(name), "input-queue.cursor")


def _read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return out


def _input_cursor(name: str) -> int:
    try:
        with open(_input_cursor_path(name), encoding="utf-8") as fh:
            return int(fh.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


@mcp.tool()
def loop_input(name: str, text: str, source: str = "owner") -> dict[str, Any]:
    """Queue a free-text steering note for a running (or saved) loop. The manager
    reads pending notes at the top of its next turn (loop_input_drain) and
    re-plans. Returns {ok, name, pending}. This is how the web UI steers a loop
    mid-run without touching its internals."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        if not isinstance(text, str) or not text.strip():
            return {"error": "input text must be a non-empty string"}
        entry = {"ts": time.time(), "text": text.strip(), "source": source or "owner"}
        path = _input_queue_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        pending = len(_read_jsonl(path)) - _input_cursor(name)
        return {"ok": True, "name": name, "pending": pending}
    return _run(work)


@mcp.tool()
def loop_input_pending(name: str, owner: str = "") -> dict[str, Any]:
    """Peek the owner steering notes not yet consumed by the manager (does NOT
    advance the cursor). Returns {name, pending:[{ts,text,source}], count}.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        items = _read_jsonl(_input_queue_path(name))
        pending = items[_input_cursor(name):]
        return {"name": name, "pending": pending, "count": len(pending)}
    return _run(work)


@mcp.tool()
def loop_input_drain(name: str) -> dict[str, Any]:
    """Return the owner steering notes not yet consumed AND advance the cursor so
    they aren't returned again. The MANAGER calls this at the top of each turn.
    Returns {name, notes:[...], count}."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        items = _read_jsonl(_input_queue_path(name))
        cur = _input_cursor(name)
        notes = items[cur:]
        if notes:
            os.makedirs(os.path.dirname(_input_cursor_path(name)), exist_ok=True)
            with open(_input_cursor_path(name), "w", encoding="utf-8") as fh:
                fh.write(str(len(items)))
        return {"name": name, "notes": notes, "count": len(notes)}
    return _run(work)


# ── archive (#9: declutter the view) ──────────────────────────────────────────
@mcp.tool()
def loop_archive(name: str) -> dict[str, Any]:
    """Mark a loop archived (hidden from loop_list by default). Returns {ok,name,
    archived}."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        cfg = _read_json(_config_path(name))
        if cfg is None:
            return {"error": f"loop {name!r} not found"}
        was = bool((_read_json(_run_path(name)) or {}).get("archived"))
        _update_run(name, archived=True)
        # Better-UX #5: archiving IS the old ship/keep's negative twin — record the
        # AUTOMATIC `removed` status (engine-origin behavior, never a click). Once
        # per archive, not per repeated call. Fail-soft by construction.
        if not was:
            _record_disposition(name, "removed", source="behavior", origin="engine")
        # Loop-Workspaces: archive is a reap trigger — a workspace KEPT after an
        # errored run (keep-on-error) is released now that the owner has archived
        # it. Reconstructed by name (no live entry); no-op on the legacy path.
        _reap_loop_workspace_by_name(name, cfg)
        return {"ok": True, "name": name, "archived": True}
    return _run(work)


@mcp.tool()
def loop_unarchive(name: str) -> dict[str, Any]:
    """Un-archive a loop (show it in loop_list again). Returns {ok,name,archived}."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        if _read_json(_config_path(name)) is None:
            return {"error": f"loop {name!r} not found"}
        _update_run(name, archived=False)
        return {"ok": True, "name": name, "archived": False}
    return _run(work)


# ── per-turn drill-in (#6: read a richer log without extra agent work) ─────────
_PROMPT_SEQ_RE = re.compile(r"^(?P<agent>.+)-(?P<seq>\d{3})\.txt$")


@mcp.tool()
def loop_turn_detail(name: str, agent: str, seq: int,
                     origin: str = origins.LOCAL_ORIGIN_ID,
                     owner: str = "") -> dict[str, Any]:
    """Open one turn: the framed prompt the substrate wrote to
    data/_loops/<name>/prompts/<agent>-NNN.txt, plus the agent's matching
    end-of-turn report (status + note) and any captured transcript tail. Zero
    extra work for the agent — it all already exists on disk. Returns
    {name, origin, agent, seq, prompt, report, transcript_tail}.

    ``origin`` defaults to local (unchanged); pass a remote origin id to drill
    into a turn of a loop that ran on ANY connected origin from the ONE hub —
    read-only, off that origin's synced mirror.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner, origin)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        if not isinstance(agent, str) or not agent:
            return {"error": "agent must be a non-empty string"}
        try:
            n = int(seq)
        except (TypeError, ValueError):
            return {"error": f"seq must be an integer, got {seq!r}"}
        base = _status_dir_for(origin, name)
        if base is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        prompt_path = os.path.join(base, "prompts", f"{agent}-{n:03d}.txt")
        prompt = None
        try:
            with open(prompt_path, encoding="utf-8") as fh:
                prompt = fh.read()
        except FileNotFoundError:
            return {"error": f"no framed prompt {agent}-{n:03d}.txt for loop {name!r}"}
        # the report answering THIS turn, keyed on (runId, agent, turn) of the
        # CURRENT run — the run whose prompts/<agent>-NNN.txt we just opened (H7;
        # legacy rows fall back to the run's ts-window + per-run position)
        rpt = turn_identity.find_turn(
            _read_jsonl(os.path.join(base, "status.jsonl")),
            _read_json(os.path.join(base, "run.json")) or {}, agent, n)
        # captured transcript (Q2): the substrate persists it to the _output tree
        # named with the SAME filesystem-safe segment the writer used — resolve it
        # that way, not the old <statusdir>/transcripts/ guess (which never matched,
        # so the drill-in tail was always null). Local uses the exact writer path
        # (honours $LOOPS_OUTPUT_DIR); a remote origin reads its mirror-relative
        # _output sibling, best-effort.
        tail = None
        if origin == origins.LOCAL_ORIGIN_ID:
            tpath = report.transcript_path(name, agent, n)
        else:
            tpath = os.path.join(os.path.dirname(base), "_output", name,
                                 report.transcript_name(agent, n))
        try:
            with open(tpath, encoding="utf-8") as fh:
                tail = fh.read()[-4000:]
        except (FileNotFoundError, OSError):
            pass
        return {"name": name, "origin": origin, "agent": agent, "seq": n,
                "prompt": prompt, "report": rpt, "transcript_tail": tail}
    return _run(work)


@mcp.tool()
def loop_live(name: str, origin: str = origins.LOCAL_ORIGIN_ID,
              owner: str = "") -> dict[str, Any]:
    """Which step(s) are running RIGHT NOW (machinery-emitted, #5). Reads
    live.json. Returns {name, origin, running:[{agent,role,phase,kind,since}],
    updated}. ``origin`` defaults to local (unchanged); pass a remote origin id
    to see the live step(s) of a loop running on ANY connected origin in the ONE
    hub — read-only, off that origin's synced mirror.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner, origin)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        base = _status_dir_for(origin, name)
        if base is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        live = _read_json(os.path.join(base, "live.json")) or {}
        return {"name": name, "origin": origin, "running": live.get("running", []),
                "updated": live.get("updated")}
    return _run(work)


@mcp.tool()
def loop_team_room(name: str, origin: str = origins.LOCAL_ORIGIN_ID,
                   owner: str = "") -> dict[str, Any]:
    """The redesigned loop-detail as a **team room** (REDESIGN-SPEC §3 rd-engine,
    build order §5.3): a re-composition — nothing new asked of an agent — of the
    files the engine already writes into the picture the loop-detail *is*: a team,
    by role, converging on a goal. Returns::

        {name, origin, goal, project, single_agent,
         state: {value ∈ {config | aligning|converging|stalled|needs-assistance
                          | delivered|error|stopped},
                 phase ∈ {config|running|finished}, reason},  # THE loop state
         convergence: {value ∈ {Aligning|Converging|Stalled|Needs you|Delivered},
                       reason},                       # legacy chip (tracking_ui)
         turn: {used, limit, winddown_in, winddown_turns, in_winddown, running},
         managerRead,                                  # the manager's one-line read
         roster: [{agent, role, displayRole, isManager, owns, personality,
                   goal, statusDot, turnDots:{dots, hidden},
                   doingNow, lastReport}],             # manager first
         run: {state}}

    - **roster** replaces the ``0W 8I 1M`` counts — one card per agent (manager
      first): its role, a one-line "owns: …" from its personality, a live status
      dot (from live.json), what it's doing now, and its last report.
    - **convergence** is a single COMPUTED chip (:func:`teamroom.compute_convergence`);
      a stop/error can never read "Delivered" (§5.4 honesty mandate).
    - **turn** is the never-blank ``turn N/M · wind-down in K`` bar; ``used`` is
      always a number on a running loop (fixes uxui's blank TURNS/WIND-DOWN).

    Click-into an agent card fetches its report history via
    :func:`loop_agent_reports`; open a single turn via :func:`loop_turn_detail`.
    ``origin`` defaults to local; pass a remote origin id to read a loop running
    on ANY connected origin off its synced mirror.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner, origin)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        base = _status_dir_for(origin, name)
        if base is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        cfg = _read_json(os.path.join(base, "config.json"))
        if cfg is None:
            return {"error": f"loop {name!r} not found on origin {origin!r}"}
        run = _read_json(os.path.join(base, "run.json"))
        progress = _read_json(os.path.join(base, "progress.json"))
        live = _read_json(os.path.join(base, "live.json")) or {}
        reports = _read_jsonl(os.path.join(base, "status.jsonl"))
        payload = teamroom.build_team_room(
            name, config=cfg, run=run, progress=progress, live=live,
            reports=reports, project=schema.resolve_project(cfg, loop_name=name),
            single_agent=schema.is_single_agent(cfg), now=time.time())
        payload["origin"] = origin
        # §5.4: the CLOSING-state signal, atop the SAME loop-detail pane. On a live
        # loop ``verdict.resolved`` is False (the roster/convergence speaks); once
        # the loop ends this is the honest Resolution card. Embedded so the detail
        # carries it in one call, alongside the live team-room read.
        payload["resolution"] = _resolution_card_for(name, cfg, origin=origin)
        return payload
    return _run(work)


@mcp.tool()
def loop_agent_reports(name: str, agent: str,
                       origin: str = origins.LOCAL_ORIGIN_ID,
                       owner: str = "") -> dict[str, Any]:
    """One agent's **report history**, NEWEST FIRST — what clicking its team-room
    card opens (§3.B: report history, not a prompt dump). Returns ``{name, origin,
    agent, reports: [{seq, status, note, turn, ts}]}`` scoped to the current run;
    each ``seq`` is the agent's nth turn — pass it to :func:`loop_turn_detail` to
    open that agent-turn card (end-of-turn report first, raw framed-prompt +
    transcript behind a toggle).

    ``origin`` defaults to local; pass a remote origin id to read off its mirror.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner, origin)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        if not isinstance(agent, str) or not agent:
            return {"error": "agent must be a non-empty string"}
        base = _status_dir_for(origin, name)
        if base is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        if _read_json(os.path.join(base, "config.json")) is None:
            return {"error": f"loop {name!r} not found on origin {origin!r}"}
        run = _read_json(os.path.join(base, "run.json")) or {}
        reports = teamroom._reports_in_run(
            _read_jsonl(os.path.join(base, "status.jsonl")), run=run)
        return {"name": name, "origin": origin, "agent": agent,
                "reports": teamroom.agent_report_history(reports, agent)}
    return _run(work)


@mcp.tool()
def loop_resolution(name: str, origin: str = origins.LOCAL_ORIGIN_ID,
                    owner: str = "") -> dict[str, Any]:
    """The closing **Resolution card** for a loop (REDESIGN-SPEC §3 rd-results,
    build order §5.4): a result is not a destination — it is the terminal STATE a
    loop enters, read at the top of the same loop pane. A re-composition of the
    result envelope (:func:`get_loop_result`) + the loop's disposition rows into a
    receipt that leads with a **computed, honest verdict**::

        {name, origin, project, single_agent, status,
         verdict: {value ∈ {SHIPPED·running|RESOLVED|PARTIAL|FAILED|
                            ABANDONED—needs you},
                   green,          # True ONLY on a positive resolution signal
                   resolved, running, reason, ownerAction, canRate},
         resolution,               # the outcome in one line (the crew's answer)
         handles: {commit, commitShort, deliverable, artifacts,
                   deliverables: {folder, items[{…, marked, markedBy, status}], …}},
         proof: {tests, testsBy, turns, winddown, seconds, signals},
         disposition: {offered, current: {verb, note, nextAction} | null,
                       autoStatus: {value: removed|deleted, reason} | null}}

    THE HONESTY MANDATE (§5.4, unfakeable): ``verdict.green`` is True only for a
    RESOLVED / SHIPPED·running outcome. A ``guardian_stopped`` run persists
    ``run.state="finished"`` (so the envelope's ``status`` reads ``completed``);
    :func:`resolution.compute_verdict` keys off the raw ``result.ended`` and
    catches it as **ABANDONED—needs you** with a **Respawn** action *before* the
    green branch — a stop or an error can never render green. The good/ok/bad
    **disposition is gated** (``offered``) on there being an outcome to judge.

    ``origin`` defaults to local; pass a remote origin id to read a loop's
    resolution off its synced mirror.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner, origin)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        base = _status_dir_for(origin, name)
        if base is None:
            return {"error": f"unknown origin {origin!r} (not visible from this box)"}
        cfg = _read_json(os.path.join(base, "config.json"))
        if cfg is None:
            return {"error": f"loop {name!r} not found on origin {origin!r}"}
        return _resolution_card_for(name, cfg, origin=origin)
    return _run(work)


def _resolution_card_for(name: str, cfg: dict, *,
                         origin: str = origins.LOCAL_ORIGIN_ID) -> dict[str, Any]:
    """Build one loop's Resolution card from its on-disk state — the shared
    internal behind the :func:`loop_resolution` tool and the ``resolution`` key
    embedded in :func:`loop_team_room`, so the loop-detail carries the closing
    card without a second round-trip. ``cfg`` is the already-read config."""
    env = _envelope_for(name, origin=origin)
    # disposition rows live in the LOCAL log only (never mirrored); a remote loop
    # simply has no local disposition — current stays null, honestly.
    rows = ([e for e in _read_jsonl(report.disposition_log(name))
             if isinstance(e, dict)]
            if origin == origins.LOCAL_ORIGIN_ID else [])
    card = resolution.build_resolution_card(
        name, env=env, disposition_rows=rows,
        project=schema.resolve_project(cfg, loop_name=name),
        single_agent=schema.is_single_agent(cfg),
        archived=bool((_read_json(os.path.join(_status_dir_for(origin, name)
                                               or report.status_dir(name),
                                               "run.json")) or {}).get("archived")))
    card["origin"] = origin
    return card


# ── registry (#7: save & reuse favourite agents / whole loops) ────────────────
def _registry_root() -> str:
    return os.path.join(os.path.dirname(report.status_dir("_")), "_registry")


def _registry_dir(kind: str) -> str:
    return os.path.join(_registry_root(), "agents" if kind == "agent" else "loops")


def _load_registry_agent(agent_id: str) -> Optional[dict]:
    return _read_json(os.path.join(_registry_dir("agent"), f"{agent_id}.json"))


def migrate_v1_agent_file(path: str) -> dict:
    """Offline migration: read a (possibly legacy v1) agent registry file at
    ``path`` and return its up-converted v2 record. Pure read — does NOT write
    the v2 shape back to disk (callers persist it explicitly if they want a
    backfill). Raises FileNotFoundError if the file is missing/unreadable."""
    rec = _read_json(path)
    if rec is None:
        raise FileNotFoundError(f"no agent registry file at {path!r}")
    return agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)


def _resolve_agent_refs(cfg: dict, now: float) -> dict:
    """Freeze any `agentRef` in a raw loop config into a by-value step pinned to
    an immutable registry version (agents.resolve_step_ref), recursing into
    nested sub-loops. A config with no refs is returned unchanged. Runs BEFORE
    validate_config so the runner never sees a ref — only resolved persona/goal
    plus an `agentPin` recording exactly which version was frozen in."""
    if not isinstance(cfg, dict):
        return cfg
    steps = cfg.get("steps")
    if not isinstance(steps, dict):
        return cfg
    for sid, sdef in list(steps.items()):
        if not isinstance(sdef, dict):
            continue
        if sdef.get("type") == "loop" and isinstance(sdef.get("loop"), dict):
            _resolve_agent_refs(sdef["loop"], now)
        elif sdef.get("agentRef"):
            steps[sid] = agents.resolve_step_ref(sdef, _load_registry_agent, now=now)
    return cfg


@mcp.tool()
def loop_registry_save_agent(agent_id: str, step: Optional[dict] = None,
                             note: str = "", *, persona: Optional[str] = None,
                             generic_goal: Optional[str] = None,
                             model: Optional[str] = None,
                             role: Optional[str] = None) -> dict[str, Any]:
    """Save an agent to the registry (Agent Registry v2). Identity is
    persona + genericGoal + model; any change forks a new IMMUTABLE version.

    Two call styles (both supported):
      • legacy — pass a loop `step` dict (role/personality/goal[/model]); its
        personality→persona and goal→genericGoal.
      • explicit — pass `persona` + `generic_goal` (+ `model`, `role`).

    If `agent_id` already exists, this forks a new version from the changed
    identity (a no-op edit does NOT create a version). Returns {ok, id, version,
    created} (created=True when a new version was written)."""
    def work():
        bad = _check_name(agent_id)
        if bad:
            return {"error": bad["error"]}
        step_in = step if isinstance(step, dict) else {}
        p = persona if persona is not None else step_in.get("personality")
        g = generic_goal if generic_goal is not None else step_in.get("goal")
        m = model if model is not None else step_in.get("model")
        r = role if role is not None else step_in.get("role", schema.WORKER)
        if not p or not str(p).strip() or not g or not str(g).strip():
            return {"error": "need at least persona + generic_goal "
                             "(or a step dict with personality + goal)"}
        now = time.time()
        path = os.path.join(_registry_dir("agent"), f"{agent_id}.json")
        existing = _read_json(path)
        try:
            if existing is None:
                rec = agents.new_agent_record(
                    agent_id, str(p), str(g), model=m, default_role=r,
                    note=note, now=now)
                created = True
            else:
                rec = agents.migrate_agent_record(existing, now=now)
                if r:
                    rec["defaultRole"] = r
                rec, created = agents.fork_agent(
                    rec, persona=str(p), generic_goal=str(g), model=m,
                    note=note, source="edit", now=now)
        except ValueError as exc:
            return {"error": str(exc)}
        _write_json(path, rec)
        return {"ok": True, "id": agent_id, "version": rec["head"], "created": created}
    return _run(work)


@mcp.tool()
def loop_registry_save_loop(name: str, from_loop: Optional[str] = None,
                            config: Optional[dict] = None, note: str = "") -> dict[str, Any]:
    """Save a whole loop to the registry — either an existing saved loop
    (`from_loop`) or a provided validated `config`. Returns {ok, id}."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        cfg = config
        if cfg is None and from_loop:
            cfg = _read_json(_config_path(from_loop))
        if cfg is None:
            return {"error": "provide `config` or an existing `from_loop` name"}
        res = validate_config(cfg)
        if not res["ok"]:
            return {"error": "invalid config", "errors": res["errors"]}
        rec = {"id": name, "kind": "loop", "config": res["config"], "note": note,
               "saved": time.time()}
        _write_json(os.path.join(_registry_dir("loop"), f"{name}.json"), rec)
        return {"ok": True, "id": name}
    return _run(work)


@mcp.tool()
def loop_registry_list(favorites_only: bool = False) -> dict[str, Any]:
    """Browse the registry. Returns ``{agents:[{id, note, saved, head, versions,
    model, defaultRole, favorite, placeholder}], loops:[...]}``. With
    ``favorites_only=True``, agent entries are narrowed to those flagged
    ``favorite=True`` — the surface's Favorites shelf without a client-side
    filter. Loops are always returned in full (favorites is an agent concept)."""
    def work():
        def _entries(kind):
            d = _registry_dir(kind)
            out = []
            for fn in (sorted(os.listdir(d)) if os.path.isdir(d) else []):
                if not fn.endswith(".json"):
                    continue
                rec = _read_json(os.path.join(d, fn)) or {}
                entry = {"id": rec.get("id", fn[:-5]), "note": rec.get("note", ""),
                         "saved": rec.get("saved")}
                if kind == "agent":
                    m = agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)
                    entry["head"] = m.get("head")
                    entry["versions"] = len(m.get("versions") or [])
                    entry["model"] = m.get("model")
                    entry["defaultRole"] = m.get("defaultRole")
                    # favorites are a curated shelf on top — every agent has a
                    # detail view regardless. Missing flag → False (back-compat).
                    entry["favorite"] = bool(rec.get("favorite", False))
                    # honest cue: agents auto-materialized from a bare id have
                    # placeholder persona/goal until curated.
                    entry["placeholder"] = agents.is_placeholder_identity(m)
                    if favorites_only and not entry["favorite"]:
                        continue
                out.append(entry)
            return out
        return {"agents": _entries("agent"), "loops": _entries("loop")}
    return _run(work)


@mcp.tool()
def loop_registry_get(kind: str, id: str) -> dict[str, Any]:
    """Fetch one registry record ({kind:'agent'|'loop', id}). Returns the record
    or {"error": ...}."""
    def work():
        if kind not in ("agent", "loop"):
            return {"error": "kind must be 'agent' or 'loop'"}
        rec = _read_json(os.path.join(_registry_dir(kind), f"{id}.json"))
        if rec is None:
            return {"error": f"no {kind} {id!r} in registry"}
        if kind == "agent":
            # migrate-on-read so pre-v2 records surface in the v2 shape (the
            # `step` mirror keeps old consumers working). Does NOT rewrite disk.
            return agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)
        return rec
    return _run(work)


@mcp.tool()
def loop_agent_versions(id: str) -> dict[str, Any]:
    """Version timeline for a registry agent: the compact per-version history
    (what changed vs the prior version, source, note, which is head) plus the
    head identity. Data behind the agent page's version timeline. Returns
    {id, head, model, defaultRole, versions:[...]} or {"error": ...}."""
    def work():
        rec = _read_json(os.path.join(_registry_dir("agent"), f"{id}.json"))
        if rec is None:
            return {"error": f"no agent {id!r} in registry"}
        m = agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)
        return {"id": id, "head": m.get("head"), "model": m.get("model"),
                "defaultRole": m.get("defaultRole"),
                "note": m.get("note", ""),
                "versions": agents.version_timeline(m)}
    return _run(work)


@mcp.tool()
def loop_registry_agent_version_get(agent_id: str, version: int) -> dict[str, Any]:
    """Fetch ONE immutable version's full identity record for a registry agent.
    Returns the version dict (version, persona, genericGoal, model, note, source,
    createdAt) plus {id, isHead}, or {"error": ...}."""
    def work():
        rec = _read_json(os.path.join(_registry_dir("agent"), f"{agent_id}.json"))
        if rec is None:
            return {"error": f"no agent {agent_id!r} in registry"}
        m = agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)
        v = agents.agent_version(m, version)
        if v is None:
            return {"error": f"agent {agent_id!r} has no version {version} "
                             f"(head is {m.get('head')})"}
        out = dict(v)
        out["id"] = agent_id
        out["isHead"] = v.get("version") == m.get("head")
        return out
    return _run(work)


@mcp.tool()
def loop_registry_agent_diff(agent_id: str, version_a: int,
                             version_b: int) -> dict[str, Any]:
    """Structured field diff between two versions of a registry agent: per-field
    {from,to,changed} for role/persona/genericGoal/model, plus a stdlib
    line-level unified diff of the text fields (persona, genericGoal). Returns
    {id, versionA, versionB, fields, textDiff} or {"error": ...}. (role is not
    versioned — see agents.diff_versions_detailed.)"""
    def work():
        rec = _read_json(os.path.join(_registry_dir("agent"), f"{agent_id}.json"))
        if rec is None:
            return {"error": f"no agent {agent_id!r} in registry"}
        m = agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)
        try:
            d = agents.diff_versions_detailed(m, version_a, version_b)
        except ValueError as exc:
            return {"error": str(exc)}
        d["id"] = agent_id
        return d
    return _run(work)


# ── origins (first-class WHERE-work-runs, on the multi-host mirror layout) ────
def _local_mirror_path() -> str:
    """Absolute path of the LOCAL mirror on this box — the ``LOOPS_DATA_DIR`` the
    tool already reads/writes under. Mirrors of other hosts (``_loops_<host>``)
    live as siblings of this path."""
    return os.path.dirname(report.status_dir("_"))


def _can_dispatch(origin: dict) -> bool:
    """Whether the hub can ROUTE control (start/stop) to this origin right now —
    the SAME reachability predicate the dispatch write-path
    (loop_dispatch_standalone) enforces before it writes a request envelope, so
    the read-surface and the write-surface never disagree about where control can
    go. The LOCAL origin is always controllable (the tool runs here). A remote
    origin is dispatchable only while its mirror is reachable + fresh."""
    if origin.get("id") == origins.LOCAL_ORIGIN_ID or origin.get("kind") == "local":
        return True
    if not origin.get("reachable") or origin.get("stale"):
        return False
    return origin.get("health") not in ("stale", "cold", "pending")


@mcp.tool()
def loop_hub_view(include_archived: bool = False) -> dict[str, Any]:
    """HUB (P2 §a+§d): the ONE consolidated snapshot the SINGLE main dashboard
    renders from — every ORIGIN visible from this box AND every loop on every
    origin, in one read, no per-origin/per-instance panels. This is the clean
    API/MCP control-surface a future multitude of clients (web / Mac / Windows /
    mobile) sit on; it adds NO new UI.

    Returns ``{origins, loops, counts}``:
      • ``origins`` — loop_origin_list's records, each annotated with
        ``canDispatch`` (can the hub route start/stop here now — the reachable +
        fresh predicate the dispatch write-path uses).
      • ``loops`` — loop_list_all's fan-out (each ``{origin, name, state,
        updated, archived}``) annotated with ``control`` — ``{"inspect": true``
        (read status/turns/results off the mirror always), ``"dispatch":
        <origin canDispatch>}`` — so a client knows, per loop, which controls are
        live without a second round-trip.
      • ``counts`` — ``{origins, loops, dispatchable_origins}``.

    Inspect a specific loop regardless of origin via loop_status / loop_live /
    loop_turn_detail / loop_result_view (all take ``origin=``); route a remote run
    via loop_dispatch_standalone."""
    def work():
        origs = loop_origin_list().get("origins", [])
        for o in origs:
            o["canDispatch"] = _can_dispatch(o)
        dispatchable = {o["id"] for o in origs if o.get("canDispatch")}
        listed = loop_list_all(include_archived=include_archived)
        loops = listed.get("loops", [])
        for l in loops:
            l["control"] = {"inspect": True,
                            "dispatch": l.get("origin") in dispatchable}
        return {"origins": origs, "loops": loops,
                "counts": {"origins": len(origs), "loops": len(loops),
                           "dispatchable_origins": len(dispatchable)}}
    return _run(work)


def _attribute_dispatched_loops(by_id: dict[str, dict],
                                live_loops: list) -> None:
    """Count each loop under the origin it RUNS on, like the loops feed
    (``loopsForOrigin``: ``l.origin || l.host``; loopyard-clear-all-bug-1790576632).
    A Hub-saved loop dispatched to a remote origin (its ``dispatched.json``)
    leaves the local count and is credited to that origin — unless the origin
    already sees it (its mirror dir or its live heartbeat names the loop), so a
    loop is never counted twice. Same visibility rule as the list count: a dir
    with ``config.json`` that is not archived. An origin with no row here is
    not invented; the loop just stops counting as local."""
    local = by_id.get(origins.LOCAL_ORIGIN_ID)
    if not local or not local.get("path") or not isinstance(local.get("loops"), int):
        return
    seen_live = {(lo.get("origin"), lo.get("name")) for lo in live_loops
                 if isinstance(lo, dict)}
    for name in origins._loop_names(local["path"]):
        d = os.path.join(local["path"], name)
        if not os.path.isfile(os.path.join(d, "config.json")):
            continue
        run = _read_json(os.path.join(d, "run.json"))
        if isinstance(run, dict) and run.get("archived"):
            continue
        target = _dispatched_origin(d)
        if not target or target == origins.LOCAL_ORIGIN_ID:
            continue
        local["loops"] = max(0, local["loops"] - 1)
        row = by_id.get(target)
        if row is None or not isinstance(row.get("loops"), int):
            continue
        already = ((target, name) in seen_live
                   or (row.get("path") and os.path.isdir(
                       os.path.join(row["path"], name))))
        if not already:
            row["loops"] += 1


@mcp.tool()
def loop_origin_list() -> dict[str, Any]:
    """List every ORIGIN visible from this box — the local mirror plus each
    ``_loops_<host>`` sibling mirror already synced onto disk. Each entry has
    ``{id, name, path, kind, loops, last_seen, health}`` — ``health`` is a coarse
    fresh/stale/cold/empty cue derived from status.jsonl mtimes. This is LEAN:
    no new remote-exec — just consolidating the multi-host mirror that already
    exists on disk (see origins.py)."""
    def work():
        now = time.time()
        origs = origins.list_origins(_local_mirror_path(), now=now)
        # D5 — fuse the capability PROBE into the list so the front door proves
        # "your auth took" without a second round-trip. Each origin carries a
        # real ``capabilities`` array ({cli, authed: true|false|"unknown",
        # applied, note}) — never the ":8449" manual "not yet probed" placeholder.
        # Local is probed live (PATH + credential files); a mirror we can't reach
        # over the wire in v1.1 carries an empty list + an honest reason, so the
        # card can dim it and say why instead of inventing chips.
        # G2.1 — overlay LIVE origin health from the protocol: a CONNECTED origin
        # SUPERSEDES its rsync-mirror row, showing honest heartbeat health
        # (online/busy/stale/offline) instead of a directory-mtime fresh/stale
        # guess. Origins that are live-enrolled but have no mirror on disk yet are
        # appended. Unenrolled origins are untouched (mirror fallback).
        by_id = {o["id"]: o for o in origs}
        snap = _live_snapshot()
        for lo in snap["origins"]:
            if not lo.get("live"):
                continue  # a registry record with no live channel ⇒ keep mirror
            oid = lo.get("id")
            if not oid:
                continue
            row = by_id.get(oid)
            if row is None:
                row = {"id": oid, "name": lo.get("name") or oid,
                       "kind": "origin", "path": None, "loops": lo.get("loops", 0),
                       "capabilities": [], "capabilitiesOk": False}
                origs.append(row)
                by_id[oid] = row
            row["source"] = "live"
            row["live"] = True
            row["health"] = lo.get("health") or row.get("health")
            row["reachable"] = bool(lo.get("reachable"))
            row["stale"] = bool(lo.get("stale"))
            if lo.get("last_seen") is not None:
                row["last_seen"] = lo["last_seen"]
            if lo.get("deviceId"):
                row["deviceId"] = lo["deviceId"]
            # S-0 (§7.3 d): a fetched inventory is the card's count — for a
            # mirror row too, since the origin's own list beats a stale mirror.
            # hidden (manifest lacks loop.list, 8.9-2) → no count, never 0.
            inv = lo.get("inventory")
            if inv:
                row["inventory"] = inv
                if inv == "hidden":
                    row["loops"] = None
                elif lo.get("inventoryFetchedAt") is not None \
                        and isinstance(lo.get("loops"), int):
                    row["loops"] = lo["loops"]
            own = lo.get("owner") or _enrolled_owner(lo.get("deviceId"))
            if own and own != schema.DEFAULT_OWNER:
                row["owner"] = own              # P3 enrolled owner (non-default)
        _attribute_dispatched_loops(by_id, snap.get("loops") or [])
        # The capability probe runs AFTER the live overlay so a CONNECTED remote
        # origin (mirror row or live-only row) is probed over the origin channel
        # (capabilities.probe, cached briefly so the list stays fast); anything
        # we cannot reach carries an empty list + an honest reason.
        for o in origs:
            kind = o.get("kind", "mirror")
            probe = origins_probe.probe_origin(
                kind, now, remote_probe=_remote_caps_probe(
                    o["id"], cached=True) if kind != "local" else None)
            o["capabilities"] = probe.get("cliCapabilities", [])
            o["capabilitiesOk"] = probe.get("ok", False)
            o["lastProbed"] = probe.get("lastProbed")
            if probe.get("via"):
                o["capabilitiesVia"] = probe["via"]
            if not probe.get("ok", False):
                o["capabilitiesReason"] = probe.get(
                    "reason", origins_probe.REMOTE_NOT_WIRED_REASON)
        # SLICE-1B §3.4 — surface CONNECTED SESSIONS as origins. A connector
        # (mcp_loops/connect.py) is a live outbound-task executor with a heartbeat
        # + declared capabilities — closer to a live origin than an rsync mirror.
        # This is a READ merge: we enumerate the connect registry and append each
        # as a `connected-session` origin. We NEVER write the connect store from
        # here. Health is derived from lastSeen exactly like connect.list_connectors
        # (live if seen within DEFAULT_STALE_AFTER=90s, else stale). Absent when
        # none are registered — no empty-state row. The demoted terminal lands here.
        try:
            for c in connect.list_connectors(_connect_root(), now=now):
                cid = c.get("id")
                if not cid:
                    continue
                live = bool(c.get("live"))
                origs.append({
                    "id": "session:" + str(cid),
                    "name": str(cid),
                    "path": None,
                    "kind": "connected-session",
                    "loops": 0,
                    "last_seen": c.get("lastSeen"),
                    "health": "live" if live else "stale",
                    "reachable": live,
                    "stale": not live,
                    # The origins capability card expects probe-shaped dicts; a
                    # connector's caps are free string labels, so keep the probe
                    # array empty and carry the declared labels separately for the
                    # session-specific chip row (client renders sessionCaps).
                    "capabilities": [],
                    "capabilitiesOk": True,
                    "sessionCaps": list(c.get("capabilities") or []),
                    "runtime": c.get("runtime"),
                    "idleSeconds": c.get("idleSeconds"),
                    "source": "connect",
                })
        except Exception:
            # A connect-store read hiccup must never take down the origins list.
            pass
        # the always-on Hub (hub_record): which machine it is + its public URL.
        # Record-only (no probe) so the list stays fast; origin_hub_status probes.
        try:
            from mcp_loops import hub_record
            hub = hub_record.current_hub()
            for o in origs:
                o["isHub"] = bool(hub.get("originId")) and o.get("id") == hub["originId"]
        except Exception as e:  # noqa: BLE001 — never take down the origins list
            hub = {"error": str(e)}
        return {"origins": origs, "hub": hub}
    return _run(work)


@mcp.tool()
def loop_origin_get(id: str) -> dict[str, Any]:
    """Fetch one origin with its full loop list. Returns the origin dict or
    ``{"error": ...}``."""
    def work():
        got = origins.get_origin(_local_mirror_path(), id, now=time.time())
        if got is None:
            return {"error": f"no origin {id!r} visible from this box"}
        return got
    return _run(work)


# Remote capability probes are a network round-trip; the Origins LIST reuses a
# recent answer (the capabilities tool always probes fresh and refreshes it).
_REMOTE_CAPS_TTL = 30.0
_REMOTE_CAPS_TIMEOUT = 8.0
_REMOTE_CAPS_CACHE: dict[str, tuple[float, Any]] = {}


def _remote_caps_probe(origin_id: str, *, cached: bool = False
                       ) -> Optional[Callable[[], Any]]:
    """A ``remote_probe`` for :func:`origins_probe.probe_origin`: asks the LIVE
    origin ``origin_id`` for its own CLI probe over the origin channel
    (``capabilities.probe``). ``None`` when no origin fabric is installed/live on
    this box — the probe then answers with the honest not-connected reason. An
    origin that is enrolled-but-offline / unknown raises inside the call, which
    probe_origin turns into ``{ok: False, reason}`` verbatim."""
    svc = _remote_dispatch(origin_id)
    call = getattr(svc, "dispatch_capabilities_probe", None) if svc else None
    if call is None:
        return None
    try:
        if not svc.is_live():
            return None
    except Exception:  # noqa: BLE001 — a flaky fabric is "not connected"
        return None

    def probe() -> Any:
        hit = _REMOTE_CAPS_CACHE.get(origin_id)
        if cached and hit is not None and time.time() - hit[0] < _REMOTE_CAPS_TTL:
            return hit[1]
        res = call(origin_id, timeout=_REMOTE_CAPS_TIMEOUT)
        _REMOTE_CAPS_CACHE[origin_id] = (time.time(), res)
        return res
    return probe


def _live_origin_row(origin_id: str) -> Optional[dict]:
    """The live-snapshot row for ``origin_id`` (a connected origin with no rsync
    mirror on disk), shaped like an origins.get_origin record, or None."""
    for lo in _live_snapshot()["origins"]:
        if lo.get("id") == origin_id and lo.get("live"):
            return {"id": origin_id, "kind": "origin",
                    "reachable": bool(lo.get("reachable")),
                    "stale": bool(lo.get("stale"))}
    return None


@mcp.tool()
def loop_origin_capabilities(id: str) -> dict[str, Any]:
    """Honestly report which authed subscription CLIs (claude / codex / …)
    an ORIGIN can run. For the LOCAL origin we probe PATH + credential files
    and return per-CLI ``{cli, authed, applied, note, lastProbed}`` — never
    guessing ``authed: True`` without positive evidence. For a REMOTE origin
    the hub asks the origin itself over the authenticated origin channel
    (``capabilities.probe``, read-only + gated by the origin's manifest): the
    origin runs the same probe on ITS box and the records come back tagged
    ``remote: True`` (``via: "origin-channel"``). A remote origin that is not
    connected (or refuses the probe) answers ``{ok: False, reason,
    cliCapabilities: []}`` so the launcher renders an honest chip instead of a
    fabricated capability.
    """
    def work():
        now = time.time()
        got = origins.get_origin(_local_mirror_path(), id, now=now)
        if got is None:
            got = _live_origin_row(id)
        if got is None:
            return {"error": f"no origin {id!r} visible from this box"}
        res = origins_probe.probe_origin(
            got["kind"], now, remote_probe=_remote_caps_probe(got["id"])
            if got["kind"] != "local" else None)
        out = {
            "origin": got["id"],
            "kind": got["kind"],
            "reachable": got["reachable"],
            "stale": got["stale"],
            "ok": res.get("ok", False),
            "cliCapabilities": res.get("cliCapabilities", []),
            "lastProbed": res.get("lastProbed"),
        }
        if res.get("via"):
            out["via"] = res["via"]
        if not res.get("ok", False):
            out["reason"] = res.get("reason", origins_probe.REMOTE_NOT_WIRED_REASON)
        return out
    return _run(work)


def _dispatch_service_or_error(origin: str = "") -> Any:
    """The live origin-dispatch service, or an ``{"error": ...}`` dict with a CLEAR
    reason when no origin-agent is installed/connected (§8-P2: the tool refuses a
    non-live target rather than pretending). H1 step 1: a REMOTE ``origin``
    connected to the bridged hub_serve Hub (not the in-process one) is served by
    the bridge's v2 exec ops — the same ``router.run`` gate + audit, Hub-side."""
    if origin and _is_remote_origin(origin):
        remote = _remote_dispatch(origin)
        # _remote_dispatch picks the bridge only when the in-process hub does
        # not hold ``origin`` (or is absent) — its call then gives the reason
        if remote is not None and remote is _HUB_BRIDGE \
                and hasattr(remote, "dispatch_run"):
            return remote
    svc = origin_dispatch()
    if svc is None:
        return {"error": "no origin-agent on this box — cross-origin dispatch "
                         "needs the Hub fabric up (yard origin up / origin_service)"}
    try:
        if not svc.is_live():
            return {"error": "origin-agent is not connected to the Hub channel"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"origin-agent health check failed: {e}"}
    return svc


@mcp.tool()
def origin_run(argv: list[str], origin: str = "", stdin_b64: str = "",
               cwd: str = "", timeout: float = 0.0,
               cap_token: str = "") -> dict[str, Any]:
    """Run a command on a live ORIGIN through the Hub and return its result
    envelope (P2 — the agent-native handle on the ONE exec primitive, §2.1).

    ``argv`` is a real argument vector — NEVER a shell string (no ``shell=True``;
    a metachar is a literal arg). ``origin`` empty targets THIS box's own origin;
    otherwise it's a connected origin's id/label. Execution stays on the origin's
    box; the Hub only sends the scoped ``origin.run``. The origin's fail-closed
    ``RunAllowlist`` gates every verb (an un-permitted program → typed error), and a
    non-loopback origin must be mTLS-bound (§5.1(4)) or the run is refused before it
    leaves the Hub. Returns ``{routed, origin, exitCode, stdout, stderr, ...}`` or a
    clear ``{"error": ...}`` for a non-live / unknown origin."""
    def work():
        svc = _dispatch_service_or_error(origin)
        if isinstance(svc, dict):
            return svc
        if not argv or not isinstance(argv, list):
            return {"error": "argv must be a non-empty argument vector"}
        try:
            return svc.dispatch_run(
                list(argv), origin=origin or None,
                stdin=stdin_b64 or None, cwd=cwd or None,
                timeout=(timeout or None), cap_token=cap_token or None)
        except Exception as e:  # noqa: BLE001 — surface the typed reason, don't wedge
            return {"error": f"origin_run failed: {e}", "origin": origin or "local"}
    return _run(work)


@mcp.tool()
def fs_pull(path: str, origin: str = "", timeout: float = 0.0,
            cap_token: str = "") -> dict[str, Any]:
    """Pull a file FROM a live origin back to the Hub (B→A) — a composition over
    ``origin.run{argv:["cat", path]}`` (§2.1, P2). Returns
    ``{ok, verb, path, bytes, sha256, dataB64, origin}`` (contents base64 in
    ``dataB64``), or ``{"error": ...}`` if the origin can't read the path / the
    origin isn't live / ``cat`` isn't in its allowlist."""
    def work():
        svc = _dispatch_service_or_error(origin)
        if isinstance(svc, dict):
            return svc
        try:
            return svc.dispatch_fs_pull(
                path, origin=origin or None, timeout=(timeout or None),
                cap_token=cap_token or None)
        except Exception as e:  # noqa: BLE001
            return {"error": f"fs.pull failed: {e}", "path": path,
                    "origin": origin or "local"}
    return _run(work)


@mcp.tool()
def fs_push(path: str, data_b64: str, origin: str = "", timeout: float = 0.0,
            cap_token: str = "") -> dict[str, Any]:
    """Push a file TO a live origin (A→B) — a composition over
    ``origin.run{argv:["tee", path], stdin:<b64>}`` (§2.1, P2). ``data_b64`` is the
    base64-encoded contents. ``tee`` echoes the bytes back so integrity is verified
    Hub-side; a mismatch fails LOUD. Returns
    ``{ok, verb, path, bytes, sha256, verified, origin}`` or ``{"error": ...}`` if
    the origin isn't live / ``tee`` isn't in its allowlist / the transfer corrupted."""
    def work():
        svc = _dispatch_service_or_error(origin)
        if isinstance(svc, dict):
            return svc
        try:
            import base64 as _b64
            raw = _b64.b64decode(data_b64) if data_b64 else b""
        except Exception as e:  # noqa: BLE001
            return {"error": f"data_b64 is not valid base64: {e}", "path": path}
        try:
            return svc.dispatch_fs_push(
                path, raw, origin=origin or None, timeout=(timeout or None),
                cap_token=cap_token or None)
        except Exception as e:  # noqa: BLE001
            return {"error": f"fs.push failed: {e}", "path": path,
                    "origin": origin or "local"}
    return _run(work)


def _dispatch_audit_log() -> Any:
    """The Hub's append-only dispatch audit (P3 slice 4). Lives beside the live
    origin-dispatch service's EnrollmentStore when one is installed, else under
    the box's default origin state dir (``<mirror>/_origin/enroll``) — the same
    root ``origin_service.build_service`` enrolls into."""
    from mcp_loops import audit
    svc = _ORIGIN_DISPATCH
    root = getattr(getattr(svc, "store", None), "root", None)
    if not root:
        root = os.path.join(_local_mirror_path(), "_origin", "enroll")
    return audit.DispatchAudit.for_store(root)


@mcp.tool()
def origin_pair_code(hub_url: str = "", label: str = "", owner: str = "",
                     install_url: str = "", state_dir: str = "") -> dict[str, Any]:
    """P2.5 one-command onboarding, HUB side: mint a single-use, short-lived
    PAIRING CODE in the Hub's enrollment store and return the exact line to paste
    on the new box (``printf CODE | ~/loopyard/bin/yard origin up --hub URL
    --hub-fingerprint FP --pair-code -``, prefixed with the install.sh fetch when
    ``install_url`` / ``$LOOPYARD_RELEASE_URL`` is set). The existing enrollment
    model is reused: the code authorizes recording ONE public key, belongs to
    ``owner`` and expires. ``hub_url`` (or ``$LOOPYARD_HUB_PUBLIC_URL``) is the
    address the box dials; ``state_dir`` (or ``$LOOPYARD_HUB_STATE_DIR``) is the
    running Hub's ``--state-dir``. Returns ``{code, owner, expiresAt, hub,
    hubFingerprint, command}`` or ``{error, code}`` on a refusal."""
    def work():
        from mcp_loops import origin_onboard
        try:
            return origin_onboard.mint_join(
                hub_url=hub_url or None, state_dir=state_dir or None,
                owner=owner or None, label=label or None,
                install_url=install_url or None)
        except origin_onboard.MintError as e:
            return {"error": e.message, "code": e.code}
    return _run(work)


@mcp.tool()
def origin_connect_link(label: str = "", owner: str = "", ttl_sec: int = 0,
                        hub_url: str = "", install_url: str = "",
                        public_url: str = "", state_dir: str = "",
                        revoke_id: str = "", status_id: str = "") -> dict[str, Any]:
    """ONE-TIME ORIGIN-CONNECT LINK: mint a SHORT, single-use, short-lived
    (default 15 min, ``ttl_sec`` 60..86400) owner-bound URL
    ``<public_url>/origin-connect/<token>`` for the user to paste to their AI.
    When the AI fetches it once, it gets complete connect instructions (markdown,
    or ``?format=sh`` for a script) with a freshly minted pairing code (the same
    code as ``origin_pair_code``), the Hub URL and the fingerprint embedded. A
    second fetch, an expired, unknown, revoked or other-owner link is refused.
    ``public_url`` defaults to ``$LOOPYARD_PUBLIC_URL`` (or ``$MAC_ORIGIN_URL``);
    ``hub_url`` / ``state_dir`` as for ``origin_pair_code``.
    ``revoke_id=<id>`` revokes a link and ``status_id=<id>`` reads its state
    (pending|fetched|connected|expired|revoked). Returns ``{id, url,
    expiresAt, …}`` or ``{error, code}``."""
    def work():
        from mcp_loops import origin_connect, origin_onboard
        kw = {"owner": owner or None, "state_dir": state_dir or None}
        try:
            if revoke_id:
                return origin_connect.revoke_link(revoke_id, **kw)
            if status_id:
                return origin_connect.link_status(status_id, **kw)
            res = origin_connect.mint_link(
                label=label or None, ttl_sec=ttl_sec or None,
                hub_url=hub_url or None, install_url=install_url or None,
                public_url=public_url or None, **kw)
            res.pop("token", None)
            return res
        except (origin_connect.LinkError, origin_onboard.MintError) as e:
            return {"error": e.message, "code": e.code}
    return _run(work)


@mcp.tool()
def origin_hub_status(live: bool = True) -> dict[str, Any]:
    """The CURRENT HUB — the always-on broker every origin dials. Returns
    ``{mode: self|remote, originId, host, publicUrl, fingerprint, configured,
    reason?, live?, checkedAt?}``. ``mode=self`` means THIS machine is the Hub
    (URL from ``$LOOPYARD_HUB_PUBLIC_URL``, fingerprint from its persisted
    cert); ``live`` probes whether it is accepting connections."""
    def work():
        from mcp_loops import hub_record
        return hub_record.status(live=live)
    return _run(work)


@mcp.tool()
def origin_hub_set(mode: str, public_url: str = "", fingerprint: str = "",
                   origin_id: str = "", host: str = "") -> dict[str, Any]:
    """SWITCH which machine is the Hub. ``mode='self'`` makes THIS machine the
    Hub; ``mode='remote'`` points at another origin's Hub by its ``public_url``
    (``wss://…``) and cert ``fingerprint`` (required for wss). Persisted; the
    Origins page and ``loop_origin_list`` report it. Returns the new status or
    ``{error, code}``."""
    def work():
        from mcp_loops import hub_record
        try:
            hub_record.set_hub(mode=mode, public_url=public_url or None,
                               fingerprint=fingerprint or None,
                               origin_id=origin_id or None, host=host or None)
        except hub_record.HubRecordError as e:
            return {"error": e.message, "code": e.code}
        return hub_record.status(live=False)
    return _run(work)


@mcp.tool()
def origin_audit(limit: int = 100, origin: str = "", owner: str = "",
                 verb: str = "", decision: str = "",
                 since: float = 0.0) -> dict[str, Any]:
    """Read the Hub's append-only DISPATCH AUDIT (P3, §6): one record per
    dispatched unit — allowed AND denied — as
    ``{v, ts, owner, source, target, verb, method, dispatchId, argsDigest,
    decision, reason, result}``. Payloads and secrets never reach the log
    (``argsDigest`` is a sha256 over redacted args; ``result`` is status scalars
    only). Filters are exact-match and optional; ``decision`` is ``allow`` or
    ``deny``; ``since`` is a unix ts. Returns the LAST ``limit`` (≤1000) matches
    oldest-first plus a whole-log ``summary`` (counts by decision/verb).
    Read-only — there is no write/delete tool."""
    def work():
        from mcp_loops import audit
        if decision and decision not in audit.DECISIONS:
            return {"error": f"decision must be one of {list(audit.DECISIONS)}"}
        try:
            lim = int(limit or 100)
            since_f = float(since or 0.0)
        except (TypeError, ValueError):
            return {"error": "limit must be an int and since a number"}
        return _origin_audit_read(lim, origin=origin, owner=owner, verb=verb,
                                  decision=decision, since=since_f)
    return _run(work)


def _origin_audit_read(limit: int, **filters: Any) -> dict[str, Any]:
    """H2: the dispatch audit a reader should see. With a hub_serve Hub bridged,
    its ``dispatch-audit.jsonl`` (every unit dispatched to the origins enrolled
    THERE) is read over the v2 ``audit.read`` op and merged, oldest-first, with
    this engine's own log (the in-process hub + connector audit) — so the answer
    is no longer ~4% of the real audit. ``summary.sources`` names each store;
    a Hub that is down / pre-v2 is reported there, never silently dropped."""
    from mcp_loops import audit
    log = _dispatch_audit_log()
    recs = log.read(limit=limit, **filters)
    summ = log.summary()
    reader = getattr(_HUB_BRIDGE, "audit_read", None)
    if reader is None:
        return {"records": recs, "count": len(recs), "summary": summ}
    sources = [{"store": "engine", "path": summ.get("path"),
                "total": summ.get("total", 0)}]
    try:
        hub = reader(limit=limit, **filters) or {}
    except Exception as e:  # noqa: BLE001 — honest partial answer
        sources.insert(0, {"store": "hub", "error": str(e)})
        return {"records": recs, "count": len(recs),
                "summary": {**summ, "sources": sources}}
    hsum = hub.get("summary") or {}
    sources.insert(0, {"store": "hub", "path": hsum.get("path"),
                       "total": hsum.get("total", 0)})
    both = sorted(list(hub.get("records") or []) + recs,
                  key=lambda r: float(r.get("ts") or 0))
    both = both[-max(1, min(int(limit or 100), audit.READ_CAP)):]

    def _merge(key: str) -> dict:
        out = dict(hsum.get(key) or {})
        for k, v in (summ.get(key) or {}).items():
            out[k] = out.get(k, 0) + v
        return out
    merged = {"path": hsum.get("path") or summ.get("path"),
              "total": int(hsum.get("total") or 0) + int(summ.get("total") or 0),
              "byDecision": _merge("byDecision"), "byVerb": _merge("byVerb"),
              "sources": sources}
    return {"records": both, "count": len(both), "summary": merged}


@mcp.tool()
def origin_policy(origin: str = "") -> dict[str, Any]:
    """Read the CAPABILITY MANIFEST the Hub enforces for an ORIGIN (P3): what it
    advertised (``exec`` / ``fsRead`` / ``fsWrite`` / ``agent`` / named ``rpcs``),
    the receipt ``manifestStatus`` (ok | missing | invalid | unsupported_version —
    anything but ok is enforced as describe-only), its ENROLLED ``owner`` (never
    the manifest's own claim), ``live`` vs last-``stored``, and the flat
    ``allowedVerbs`` list a dispatch must fall inside or be refused pre-send.
    ``origin`` empty = this box's own origin; an enrolled-but-offline origin
    shows its last stored manifest. Read-only — an origin changes its manifest
    only on its OWN disk (``origin-manifest.json``); the Hub cannot widen it."""
    def work():
        svc = origin_dispatch()
        # H2: a REMOTE origin is answered by the hub_serve Hub it enrolled with
        # (its store holds the manifest + owner); the in-process hub answers for
        # this box's own origin, or when the bridged Hub doesn't know it.
        bridge = _HUB_BRIDGE if getattr(_HUB_BRIDGE, "policy_view", None) else None
        cands = ([bridge] if bridge is not None and _is_remote_origin(origin) else []) \
            + ([svc] if svc is not None else [])
        if not cands:
            return {"error": "no origin-agent on this box — the Hub fabric is not up"}
        errs = []
        for c in cands:
            try:
                return c.policy_view(origin or None)
            except Exception as e:  # noqa: BLE001 — DispatchUnavailable etc: clear reason
                errs.append(str(e))
        return {"error": f"origin_policy failed: {'; '.join(errs)}",
                "origin": origin or "local"}
    return _run(work)


@mcp.tool()
def origin_onboarding_check(required: list[str], origin: str = "",
                            timeout: float = 0.0) -> dict[str, Any]:
    """Run a LIVE readiness check on an ORIGIN (§6.4): probe its real, fresh
    auth-aware CLI picture through the Hub and diff it against ``required`` — the
    CLIs a set of loops needs — returning concrete "action needed on this origin"
    items. Unlike a static capability read this probes auth-state on demand, so
    ``gh present but signed out`` shows up as an ``action`` and a missing CLI as a
    ``blocker``. Every check is Hub-audited (``A_ONBOARDING``).

    ``origin`` empty targets THIS box's own origin. Returns
    ``{origin, ready, actions, blockers, required, ...}`` — ``ready`` True iff there's
    nothing to do — or ``{"error": ...}`` for a non-live/unknown origin."""
    def work():
        svc = _dispatch_service_or_error()
        if isinstance(svc, dict):
            return svc
        if not isinstance(required, list):
            return {"error": "required must be a list of CLI names"}
        try:
            return svc.dispatch_onboarding_check(
                required, origin=origin or None, timeout=(timeout or None))
        except Exception as e:  # noqa: BLE001 — surface the reason, don't wedge
            return {"error": f"origin_onboarding_check failed: {e}",
                    "origin": origin or "local"}
    return _run(work)


def _origins_with_live(now: float) -> list[dict]:
    """The lean fleet view the **Fleet pill** + **Origin picker** share:
    :func:`origins.list_origins` fused with the LIVE heartbeat snapshot (a
    connected origin's protocol health/reachability SUPERSEDES the rsync-mirror
    mtime guess, and live-enrolled origins with no mirror yet are appended). Unlike
    :func:`loop_origin_list` it runs NO per-origin capability probe (the pill and
    picker need health, not auth chips) and does NOT merge connected-sessions —
    those are the Sessions surface (§rd-sessions), never the compute-fleet count."""
    origs = origins.list_origins(_local_mirror_path(), now=now)
    by_id = {o["id"]: o for o in origs}
    for lo in _live_snapshot()["origins"]:
        if not lo.get("live"):
            continue                            # a registry record with no live channel ⇒ keep mirror
        oid = lo.get("id")
        if not oid:
            continue
        row = by_id.get(oid)
        if row is None:
            row = {"id": oid, "name": lo.get("name") or oid, "kind": "origin",
                   "path": None, "loops": lo.get("loops", 0)}
            origs.append(row)
            by_id[oid] = row
        row["source"] = "live"
        row["live"] = True
        row["health"] = lo.get("health") or row.get("health")
        row["reachable"] = bool(lo.get("reachable"))
        row["stale"] = bool(lo.get("stale"))
        if lo.get("last_seen") is not None:
            row["last_seen"] = lo["last_seen"]
    return origs


@mcp.tool()
def loop_fleet_summary() -> dict[str, Any]:
    """The **Fleet pill** data (REDESIGN-SPEC §2 / rd-origins) — compute felt
    ambiently in the top bar of every dashboard. Re-composes the live fleet view
    (:func:`_origins_with_live`) into the honest ``N origins · M reachable`` counts
    the pill shows, a per-origin ``dot`` + row for its popover, and the **drop
    signal** (``dropped`` — an origin *running a loop* that is no longer reachable,
    the amber-dot + quiet-toast case). Connected sessions are excluded (their home
    is the Sessions page). Returns ``origins.fleet_summary`` shape."""
    def work():
        return origins.fleet_summary(_origins_with_live(time.time()))
    return _run(work)


@mcp.tool()
def loop_origin_picker() -> dict[str, Any]:
    """The **one shared health-aware Origin picker** source (rd-origins mandate):
    wherever a loop is born the composer must offer the *full reachable fleet*, not
    only ``local``. Returns ``{options}`` — local first, then reachable
    mirrors/live origins, then unreachable ones shown but ``disabled`` with an
    honest reason, then targetable connected **sessions** as ``(session)`` compute.
    The +Loop composer and any other birth surface consume this ONE source so the
    picker can never silently collapse back to local-only."""
    def work():
        now = time.time()
        origs = _origins_with_live(now)
        try:
            sess = connect.list_connectors(_connect_root(), now=now)
        except Exception:  # noqa: BLE001 — a connect-store hiccup never sinks the picker
            sess = []
        return {"options": origins.picker_options(origs, sess)}
    return _run(work)


@mcp.tool()
def loop_sessions_list() -> dict[str, Any]:
    """The **Sessions roster** read-model (REDESIGN-SPEC §3 rd-sessions) — a
    first-class roster of the living collaborators (connected app/CLI sessions),
    one card each: identity, runtime + capability chips, **which origin it runs
    on** (host, cwd), an honest heartbeat, **what it's doing now**, and **what it
    has created** (loops/issues/objectives). READ-ONLY this round (no switch verbs
    yet). Re-composes the connect store (:func:`connect.list_connectors` +
    :func:`connect.list_tasks`) via :func:`sessions.build_roster`; never fabricates
    an origin or a created-count it cannot attribute. Returns
    ``{sessions, count, live}``."""
    def work():
        now = time.time()
        root = _connect_root()
        try:
            connectors = connect.list_connectors(root, now=now)
            tasks = connect.list_tasks(root)
        except Exception:  # noqa: BLE001 — a read hiccup returns an honest empty roster
            connectors, tasks = [], []
        return sessions.build_roster(connectors, tasks, now=now)
    return _run(work)


# ── products (first-class TARGETS for loops + standalone runs) ────────────────
# Host-specific roots resolved at RUN TIME (never baked into a portable config):
#   LOOPS_PRODUCTS_DIR — where product repos live on THIS box (default ~/products)
#   LOOPS_OUTPUT_DIR   — base for per-loop / per-product output folders
# The registry directory for connected-repo records. Round A renamed the noun
# Product → Project, so the CANONICAL directory is now ``projects/``; the legacy
# ``products/`` dir is still READ (migrate-on-read) so NO existing record — incl
# the live ``loopyard`` record and every git-real record — is ever lost. New
# records land in ``projects/``; an update to an EXISTING record is written back
# to its OWN file (wherever it already lives) so we never fork a record across
# the two dirs.
def _projects_dir() -> str:
    """Canonical registry dir for Project records (post-rename)."""
    return os.path.join(_registry_root(), "projects")


def _products_dir() -> str:
    """Legacy registry dir (pre-rename). Still READ for back-compat; retained as
    the name older internal callers use so this rename stays zero-churn."""
    return os.path.join(_registry_root(), "products")


def _project_record_dirs() -> list[str]:
    """Dirs to READ project records from, canonical first: ``projects/`` then the
    legacy ``products/``. A record present in both resolves from ``projects/``."""
    return [_projects_dir(), _products_dir()]


def _project_record_path(pid: str) -> str:
    """File path for project ``pid``: its EXISTING file if one is already on disk
    (canonical ``projects/`` preferred, else legacy ``products/``), otherwise a
    NEW path under the canonical ``projects/`` dir. Keeps an update writing back
    to the record's own file (no dup across dirs) while new records land
    canonically."""
    for d in _project_record_dirs():
        cand = os.path.join(d, f"{pid}.json")
        if os.path.exists(cand):
            return cand
    return os.path.join(_projects_dir(), f"{pid}.json")


def _project_record_exists(pid: str) -> bool:
    """True iff a record for ``pid`` exists in EITHER registry dir."""
    return any(os.path.exists(os.path.join(d, f"{pid}.json"))
               for d in _project_record_dirs())


def _products_root() -> str:
    return os.environ.get(
        "LOOPS_PRODUCTS_DIR",
        os.path.join(os.path.expanduser("~"), "products"))


def _output_base() -> str:
    # Single source of truth (report.output_base): $LOOPS_OUTPUT_DIR > the
    # ``_output`` tree beside the loops data dir. Kept as a thin alias so existing
    # callers here are unchanged while the substrate + dashboard share the rule.
    return report.output_base()


@mcp.tool()
def loop_project_save(project: dict) -> dict[str, Any]:
    """Save a PROJECT to the registry — a first-class target (name + git
    remote/branch + a PORTABLE relative repoDir/outputRoot) that loops and
    standalone runs point at. ``id``/``name`` are AUTO-DERIVED when a single
    ``source`` string (git URL or dir path) or a ``gitRemote``/``repoDir`` is
    given, so no hand-typed metadata is required (see ``loop_project_add`` for
    the one-input front door). Rejects host-absolute paths (portability is the
    shared-library moat). New records land in the canonical ``projects/`` dir; an
    update to a legacy ``products/`` record is written back in place. Returns
    {ok, id, warnings} or {"error", errors}."""
    return _run(lambda: _save_product(project))


@mcp.tool()
def loop_product_save(product: dict) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_project_save` (noun rename
    Product → Project, Round A). Identical behavior; kept so coord's own calls +
    the Mac launcher keep working. Prefer ``loop_project_save``."""
    return _run(lambda: _save_product(product))


@mcp.tool()
def loop_project_add(source: str) -> dict[str, Any]:
    """TRIVIAL ADD — a project is NOTHING more than a git repo URL OR a path to a
    directory. That single string is the ENTIRE input: id + display name +
    portable repoDir are DERIVED (git remote or basename), no hand-typed
    metadata. Idempotent by derived id. Returns ``{ok, id, derived, warnings}``
    or ``{"error", errors}``. Thin sugar over ``loop_project_save({source})``."""
    return _run(lambda: _save_product({"source": source}))


@mcp.tool()
def loop_product_add(source: str) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_project_add`. Identical
    behavior; prefer ``loop_project_add``."""
    return _run(lambda: _save_product({"source": source}))


def _save_product(raw: dict) -> dict[str, Any]:
    """Validate (auto-deriving id/name from a `source`/gitRemote/repoDir) then
    persist a project. Shared by loop_project_save/add (+ product aliases)."""
    res = products.validate_product(raw)
    if not res["ok"]:
        return {"error": "invalid product", "errors": res["errors"]}
    pid = res["product"]["id"]
    bad = _check_name(pid)
    if bad:
        return bad
    rec = dict(res["product"])
    rec["saved"] = time.time()
    # write to the record's OWN file (updates in place if it already lives in the
    # legacy products/ dir), else a new file under the canonical projects/ dir.
    _write_json(_project_record_path(pid), rec)
    return {"ok": True, "id": pid, "derived": res["product"],
            "warnings": res["warnings"]}


def _project_get(id: str, resolve: bool) -> dict[str, Any]:
    """Shared impl behind loop_project_get / loop_product_get."""
    rec = _read_json(_project_record_path(id))
    if rec is None:
        return {"error": f"no product {id!r} in registry"}
    rec = products.migrate_product(rec)         # upcast schema-1 → 2 on read (lossless)
    if resolve:
        rec["resolved"] = products.resolve_paths(
            rec, products_root=_products_root(), output_base=_output_base())
    return rec


@mcp.tool()
def loop_project_get(id: str, resolve: bool = False) -> dict[str, Any]:
    """Fetch a PROJECT record (a connected git repo a loop targets). With
    `resolve=True`, also include host-resolved absolute {repoPath, outputPath} for
    THIS box (computed, never stored). Returns the record (+ optional `resolved`)
    or {"error": ...}. Reads the canonical ``projects/`` registry dir AND the
    legacy ``products/`` dir, so no pre-rename record is lost."""
    return _run(lambda: _project_get(id, resolve))


@mcp.tool()
def loop_product_get(id: str, resolve: bool = False) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_project_get` (the noun rename
    Product → Project, Round A). Identical behavior; kept so live clients + the
    Mac launcher keep working. Prefer ``loop_project_get``."""
    return _run(lambda: _project_get(id, resolve))


def _project_list(resolve: bool, gather: bool, origin: str) -> dict[str, Any]:
    """Shared impl behind loop_project_list / loop_product_list."""
    gathered = None
    if gather:
        gathered = _gather_products(origin)
    out = []
    migrated = []
    products_root = _products_root() if resolve else None
    output_base = _output_base() if resolve else None
    # merge both registry dirs (canonical projects/ first, legacy products/
    # second); a record present in both resolves from projects/. Sorted by id
    # for a deterministic listing regardless of which dir a record lives in.
    seen_ids: set[str] = set()
    recs: list[dict] = []
    for d in _project_record_dirs():
        for fn in (sorted(os.listdir(d)) if os.path.isdir(d) else []):
            if not fn.endswith(".json"):
                continue
            rec = products.migrate_product(_read_json(os.path.join(d, fn)) or {})
            rid = rec.get("id") or fn[:-5]
            if rid in seen_ids:
                continue                     # canonical projects/ record wins
            seen_ids.add(rid)
            rec.setdefault("id", rid)
            recs.append(rec)
    for rec in sorted(recs, key=lambda r: r.get("id", "")):
        migrated.append(rec)
        entry = {"id": rec.get("id", ""), "name": rec.get("name", ""),
                 "gitRemote": rec.get("gitRemote"),
                 "gitBranch": rec.get("gitBranch"),
                 "subPath": rec.get("subPath", ""),
                 "repoDir": rec.get("repoDir"),
                 "outputRoot": rec.get("outputRoot"),
                 "origin": rec.get("origin"),
                 "aka": rec.get("aka", []),
                 # advisory: a no-git folder project is 'connect a remote', not a repo
                 "connected": bool(rec.get("gitRemote")),
                 "note": rec.get("note", ""), "saved": rec.get("saved")}
        if resolve:
            entry["resolved"] = products.resolve_paths(
                rec, products_root=products_root, output_base=output_base)
        out.append(entry)
    res: dict[str, Any] = {"products": out}
    # advisory merge SUGGESTIONS (never collapses/renames/deletes anything).
    # Two signals: a shared normalized gitRemote (pure), OR a shared git
    # common-dir from resolving each project's checkout on THIS box. The
    # common-dir probe is cheap — projects with no real checkout short-circuit
    # before any git subprocess (the candidate dirs simply don't exist).
    common_dirs: dict[str, str] = {}
    for rec in migrated:
        pid = rec.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        checkout = _resolve_product_checkout(rec)
        if checkout:
            cd = products_git.common_dir(checkout)
            if cd:
                common_dirs[pid] = cd
    res["mergeSuggestions"] = products.merge_suggestions(
        migrated, common_dirs=common_dirs or None)
    if gathered is not None:
        res["gathered"] = {"created": gathered["created"],
                           "loops_scanned": gathered["loops_scanned"]}
    res["unattributed"] = _unattributed_loops(origin)
    return res


def _unattributed_loops(origin: str) -> list[dict]:
    """Every non-archived loop on ``origin`` with NO explicit project binding, each
    with its demoted ``suggestedProject`` guess (or ``None``) — the Unattributed
    bucket's "suggested: <p> — accept?" feed (loopyard-bug-1790177434)."""
    out: list[dict] = []
    root = _data_root_for(origin)
    for lp in _loops_with_config(origin):
        cfg = lp["config"]
        if schema.project_id(cfg):
            continue
        run = _read_json(os.path.join(root, lp["name"], "run.json")) or {}
        if run.get("archived"):
            continue
        out.append({"name": lp["name"],
                    "suggestedProject": schema.suggest_project(cfg, loop_name=lp["name"])})
    return out


@mcp.tool()
def loop_project_list(resolve: bool = False, gather: bool = True,
                      origin: str = origins.LOCAL_ORIGIN_ID) -> dict[str, Any]:
    """Browse PROJECTS (connected git repos loops target). Returns
    ``{products:[{id, name, gitRemote, gitBranch, repoDir, outputRoot, note,
    saved [, resolved]}], gathered?}``. (The list key stays ``products`` for wire
    back-compat with live clients.) With ``resolve=True``, each entry gets a
    ``resolved`` object with THIS box's absolute ``{repoPath, outputPath}`` — a
    single call the surface can use instead of N per-project
    ``loop_project_get(id, resolve=True)`` round-trips.

    AUTO-RUN: with ``gather=True`` (default — the Projects-page load path) it
    first runs :func:`loop_projects_gather` so any loop not yet attributed to a
    project self-heals into one before the list is returned. Records are read
    from BOTH the canonical ``projects/`` dir and the legacy ``products/`` dir.
    Pass ``gather=False`` for a pure read."""
    return _run(lambda: _project_list(resolve, gather, origin))


@mcp.tool()
def loop_product_list(resolve: bool = False, gather: bool = True,
                      origin: str = origins.LOCAL_ORIGIN_ID) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_project_list` (noun rename
    Product → Project, Round A). Identical behavior + identical result shape (the
    ``products`` key is preserved); kept so live clients keep working. Prefer
    ``loop_project_list``."""
    return _run(lambda: _project_list(resolve, gather, origin))


@mcp.tool()
def loop_project_lint(project: dict) -> dict[str, Any]:
    """Validate a PROJECT candidate WITHOUT persisting it — the surface can call
    this from a form to show errors as the user edits, then loop_project_save
    once the shape is clean. Returns ``{ok, errors, warnings, product}`` (the
    normalized record on success; the ``product`` key is preserved for wire
    back-compat)."""
    return _run(lambda: products.validate_product(project))


@mcp.tool()
def loop_product_lint(product: dict) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_project_lint`. Identical
    behavior; prefer ``loop_project_lint``."""
    return _run(lambda: products.validate_product(product))


def _project_delete(id: str) -> dict[str, Any]:
    """Shared impl behind loop_project_delete / loop_product_delete. Removes the
    record from EVERY registry dir that holds it (canonical + legacy) so a delete
    is honoured no matter where the record was written."""
    removed = False
    for d in _project_record_dirs():
        path = os.path.join(d, f"{id}.json")
        if os.path.exists(path):
            os.remove(path)
            removed = True
    if not removed:
        return {"error": f"no product {id!r} in registry"}
    return {"ok": True, "id": id}


@mcp.tool()
def loop_project_delete(id: str) -> dict[str, Any]:
    """Remove a PROJECT from the registry. Returns {ok, id} or {"error": ...}."""
    return _run(lambda: _project_delete(id))


@mcp.tool()
def loop_product_delete(id: str) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_project_delete`. Identical
    behavior; prefer ``loop_project_delete``."""
    return _run(lambda: _project_delete(id))


# ── /loopyard project-state manager (#5) + capability mechanism (#6) ──────────
# A Project may carry a top-level ``loopyard/`` dir inside its checkout, holding
# project-scoped state (loop configs, references, and capability DATA). These
# tools resolve the Project → checkout (reusing ``_resolve_product_checkout``)
# and delegate all file I/O to the pure :mod:`mcp_loops.loopyard` manager (safe
# path handling — no escaping the dir). Capabilities (:mod:`mcp_loops.capabilities`)
# are discovered from a bundled dir and mount a declarative page as a Project
# section, reading/writing their data under ``loopyard/<dataDir>/``.

def _resolve_checkout_for(project_id: str) -> tuple[Optional[str], Optional[dict]]:
    """Resolve a Project id → its on-disk checkout dir on THIS box.

    Returns ``(checkout, None)`` on success or ``(None, {"error": ...})`` — the
    two failure modes an app must distinguish are *unknown project* and *no
    resolvable checkout here* (repo not cloned / remote mismatch)."""
    rec = _project_get(project_id, resolve=False)
    if rec.get("error"):
        return None, rec
    checkout = _resolve_product_checkout(rec)
    if not checkout:
        return None, {"error": f"project {project_id!r} has no resolvable checkout on this box"}
    return checkout, None


@mcp.tool()
def loopyard_list(project_id: str, subpath: str = "") -> dict[str, Any]:
    """List one level of a Project's top-level ``loopyard/`` dir. Returns
    ``{ok, project, subpath, entries:[{name,path,type,size}]}``; an absent
    ``loopyard/`` is empty state (``entries: []``), not an error."""
    def work():
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        try:
            entries = loopyard.list_tree(checkout, subpath)
        except loopyard.LoopyardPathError as e:
            return {"error": str(e)}
        return {"ok": True, "project": project_id, "subpath": subpath, "entries": entries}
    return _run(work)


@mcp.tool()
def loopyard_read(project_id: str, path: str) -> dict[str, Any]:
    """Read a text file under a Project's ``loopyard/``. Returns ``{ok, path,
    content}`` or an error (missing file / escaping path)."""
    def work():
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        try:
            return {"ok": True, "path": path, "content": loopyard.read_file(checkout, path)}
        except FileNotFoundError:
            return {"error": f"no such file under loopyard/: {path!r}"}
        except loopyard.LoopyardPathError as e:
            return {"error": str(e)}
    return _run(work)


@mcp.tool()
def loopyard_write(project_id: str, path: str, content: str) -> dict[str, Any]:
    """Write a text file under a Project's ``loopyard/`` (creating dirs as
    needed). Returns ``{ok, entry}`` or an error on an escaping path."""
    def work():
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        try:
            return {"ok": True, "entry": loopyard.write_file(checkout, path, content)}
        except loopyard.LoopyardPathError as e:
            return {"error": str(e)}
    return _run(work)


@mcp.tool()
def loopyard_delete(project_id: str, path: str) -> dict[str, Any]:
    """Delete a file under a Project's ``loopyard/``. Returns ``{ok, removed}``
    (``removed`` is False if it did not exist)."""
    def work():
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        try:
            return {"ok": True, "removed": loopyard.delete_file(checkout, path)}
        except loopyard.LoopyardPathError as e:
            return {"error": str(e)}
    return _run(work)


@mcp.tool()
def capabilities_list() -> dict[str, Any]:
    """Discover the bundled capabilities the dashboard mounts as Project
    sections. Directory-driven — add/remove a capability folder and the set
    changes. Returns ``{ok, capabilities:[manifest…], errors:[{folder,error}]}``."""
    def work():
        caps, errors = capabilities.discover_with_errors()
        return {"ok": True, "capabilities": [c.as_dict() for c in caps], "errors": errors}
    return _run(work)


@mcp.tool()
def capability_get(cap_id: str) -> dict[str, Any]:
    """Fetch one bundled capability's manifest (wire form)."""
    def work():
        try:
            return {"ok": True, "capability": capabilities.get(cap_id).as_dict()}
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
    return _run(work)


@mcp.tool()
def capability_page(cap_id: str) -> dict[str, Any]:
    """The declarative page markup for a capability — the dashboard shell mounts
    it (e.g. in an iframe). Returns ``{ok, id, kind, html}``."""
    def work():
        try:
            cap = capabilities.get(cap_id)
            return {"ok": True, "id": cap.id, "kind": cap.page.get("kind"),
                    "html": cap.read_page()}
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
    return _run(work)


def _capability_data_items(cap: "capabilities.Capability", checkout: str) -> list[dict]:
    """List a capability's data files under ``loopyard/<dataDir>/``, parsing
    JSON records inline so the page can render without N follow-up reads."""
    items: list[dict] = []
    for e in loopyard.list_tree(checkout, cap.data_dir):
        if e["type"] != "file":
            continue
        rel = os.path.join(cap.data_dir, e["name"])
        item: dict[str, Any] = {"name": e["name"], "path": rel}
        if e["name"].endswith(".json"):
            try:
                item["json"] = loopyard.read_json(checkout, rel)
            except Exception:  # noqa: BLE001 — a bad record shouldn't blank the list
                item["error"] = "unreadable json"
        items.append(item)
    return items


@mcp.tool()
def capability_data_list(cap_id: str, project_id: str) -> dict[str, Any]:
    """List a capability's data under ``loopyard/<dataDir>/`` (JSON parsed
    inline). Returns ``{ok, capability, dataDir, items}``."""
    def work():
        try:
            cap = capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        return {"ok": True, "capability": cap_id, "dataDir": cap.data_dir,
                "items": _capability_data_items(cap, checkout)}
    return _run(work)


@mcp.tool()
def capability_data_write(cap_id: str, project_id: str, path: str,
                          content: str) -> dict[str, Any]:
    """Write a data file under a capability's ``loopyard/<dataDir>/``. ``path``
    is relative to the capability's data dir. Returns ``{ok, entry}``."""
    def work():
        try:
            cap = capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        rel = f"{cap.data_dir}/{path}"
        try:
            return {"ok": True, "entry": loopyard.write_file(checkout, rel, content)}
        except loopyard.LoopyardPathError as e:
            return {"error": str(e)}
    return _run(work)


@mcp.tool()
def capability_trigger(cap_id: str, project_id: str) -> dict[str, Any]:
    """Trigger a capability's ATTACHED LOOP: seed it from the capability's
    ``loopyard/<dataDir>/`` data, ``loop_save`` + ``loop_start`` it, and return
    the initial result envelope. The loop name is deterministic per (capability,
    project) so re-triggering reuses one record. Returns ``{ok, loop, envelope}``
    — or ``{ok: False, loop, error, envelope}`` if it saved but couldn't start
    (e.g. no runner attached), so the UI can still inspect/retry."""
    def work():
        try:
            cap = capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        if cap.attached_loop is None:
            return {"error": f"capability {cap_id!r} has no attached loop"}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        seed_lines: list[str] = []
        for item in _capability_data_items(cap, checkout):
            j = item.get("json") or {}
            seed_lines.append(f"- {j.get('title') or item['name']}: {j.get('body', '')}"[:200])
        cfg = capabilities.build_attached_loop(cap, project_id, "\n".join(seed_lines))
        saved = loop_save(cfg)
        if saved.get("error"):
            return saved
        name = saved["name"]
        started = loop_start(name)
        if started.get("error"):
            return {"ok": False, "loop": name, "error": started["error"],
                    "envelope": _envelope_for(name)}
        return {"ok": True, "loop": name, "envelope": _envelope_for(name)}
    return _run(work)


@mcp.tool()
def capability_result(cap_id: str, project_id: str) -> dict[str, Any]:
    """The attached loop's result envelope for this (capability, project) — the
    same shape as ``get_loop_result``. ``status="not_found"`` if never triggered."""
    def work():
        try:
            cap = capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        return _envelope_for(capabilities.attached_loop_name(cap, project_id))
    return _run(work)


# ── B2 first-party hub capabilities: extra read/action tools ──────────────────
# ADDITIVE (engineer_a, Round B2). The generic /loopyard + capability tools above
# are untouched. These add exactly what C1 Objectives (→ Build) and C4 Known-
# Issues (curated + imported-md + auto-filed union) need, driven declaratively by
# each capability's manifest ``settings`` (``importMd``/``importKind``/
# ``unifyAutofiledIssues``) — no capability-specific code paths, no core change.

def _capability_stored_items(cap: "capabilities.Capability", checkout: str) -> list[dict]:
    """The capability's own JSON records under ``loopyard/<dataDir>/``,
    normalized to the unified item shape (non-JSON/unreadable records are left to
    the generic ``/data`` listing and skipped from the structured view)."""
    out: list[dict] = []
    for it in _capability_data_items(cap, checkout):
        j = it.get("json")
        if isinstance(j, dict):
            out.append(hub_capabilities.normalize_stored_item(it["name"], j, it["path"]))
    return out


def _project_of_loop(name: str, cache: dict) -> Optional[str]:
    """Resolve a loop name → its bound projectId (canonical, ``productId``
    alias), reading the loop's ``config.json`` once per name."""
    if name in cache:
        return cache[name]
    cfg = _read_json(_config_path(name)) or {}
    pid = cfg.get("projectId") or cfg.get("productId")
    cache[name] = pid
    return pid


def _autofiled_items_for_project(project_id: str) -> list[dict]:
    """Auto-filed loop-failure issues (``data/_issues``) bound to this Project,
    joined via ``issue.loop → loop config → projectId`` and normalized."""
    rows = issues.LocalIssueSink().list_issues()
    cache: dict = {}
    return hub_capabilities.filter_autofiled(
        rows, project_id, lambda n: _project_of_loop(n, cache))


@mcp.tool()
def capability_items(cap_id: str, project_id: str) -> dict[str, Any]:
    """Unified item list for a first-party hub capability: its STORED JSON
    records + any IMPORTED items parsed from a legacy markdown file declared in
    the manifest (``settings.importMd`` / ``importKind`` — e.g. the dogfood
    ``objectives.md`` / ``known-issues.md``) + (Known-Issues only, via
    ``settings.unifyAutofiledIssues``) the AUTO-FILED loop-failure issues for
    this Project. Returns ``{ok, capability, items, counts}``; every item carries
    a ``source`` (``stored`` | ``imported`` | ``autofiled``)."""
    def work():
        try:
            cap = capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        settings = cap.settings or {}
        stored = _capability_stored_items(cap, checkout)
        imported: list[dict] = []
        import_md = settings.get("importMd")
        if isinstance(import_md, str) and import_md:
            try:
                text = loopyard.read_file(checkout, import_md)
            except (FileNotFoundError, loopyard.LoopyardPathError):
                text = ""
            if text:
                imported = loopyard_import.parse(
                    text, kind=settings.get("importKind") or "objective")
        autofiled: list[dict] = []
        if settings.get("unifyAutofiledIssues"):
            autofiled = _autofiled_items_for_project(project_id)
        return {"ok": True, "capability": cap_id,
                "items": stored + imported + autofiled,
                "counts": {"stored": len(stored), "imported": len(imported),
                           "autofiled": len(autofiled)}}
    return _run(work)


@mcp.tool()
def capability_build_loop(cap_id: str, project_id: str, title: str,
                          detail: str = "", item_id: str = "") -> dict[str, Any]:
    """C1 Objectives → Build: seed a real loop config from an objective and bind
    it to the Project, then ``loop_save`` it as a draft. Returns
    ``{ok, loop, editUrl, goal}`` — the dashboard opens ``editUrl``
    (``/loops/<name>``) in the prefilled editor (the ``/newloop`` creator can't
    be URL-seeded). Binding is optional-safe: needs only the project id, so it
    works even when the Project has no resolvable checkout on this box."""
    def work():
        try:
            capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        rec = _project_get(project_id, resolve=False)
        if rec.get("error"):
            return rec
        if not isinstance(title, str) or not title.strip():
            return {"error": "an objective title is required to build a loop"}
        cfg = hub_capabilities.build_loop_from_objective(
            project_id, title, detail or "", cap_id=cap_id)
        saved = loop_save(cfg)
        if saved.get("error"):
            return saved
        name = saved["name"]
        return {"ok": True, "loop": name, "editUrl": f"/loops/{name}",
                "goal": cfg["goal"]}
    return _run(work)


# ── B2 first-party hub capabilities C2 Specs + C3 Liked-Results (engineer_b) ───
# ADDITIVE. Doc-shaped capabilities: they take a FILE under the Project's
# ``loopyard/<dataDir>/`` and RENDER it CSP-safely (md + arbitrary html) via the
# pure :mod:`mcp_loops.hub_render`. Specs (C2) can hand a doc to the loop-creator
# as trusted context (reusing the C1 creator-context store); Liked-Results (C3)
# pins a loop's _output file into a gallery with a small provenance manifest.
# Same seam as engineer_a's block above: resolve checkout, delegate file I/O to
# the ``/loopyard`` manager, keep the render pure. No core change.

def _cap_files(cap: "capabilities.Capability", checkout: str,
               *, exclude_meta: bool = False) -> list[dict]:
    """The files directly under a capability's ``loopyard/<dataDir>/``, each
    tagged with its render ``kind`` (md|html|text). ``exclude_meta`` drops the
    ``*.meta.json`` provenance sidecars (Liked-Results) from the content list."""
    out: list[dict] = []
    for e in loopyard.list_tree(checkout, cap.data_dir):
        if e["type"] != "file":
            continue
        if exclude_meta and e["name"].endswith(".meta.json"):
            continue
        out.append({
            "name": e["name"],
            "path": os.path.join(cap.data_dir, e["name"]),
            "kind": hub_render.classify(e["name"]),
            "bytes": e.get("size"),
        })
    return out


def _read_under_cap(cap: "capabilities.Capability", checkout: str,
                    path: str) -> tuple[Optional[str], Optional[dict]]:
    """Read a loopyard-relative ``path`` that MUST live under the capability's
    own ``dataDir`` (belt-and-braces over ``safe_join``). Returns
    ``(text, None)`` or ``(None, {"error": ...})``."""
    rel = (path or "").strip().lstrip("/")
    prefix = cap.data_dir + "/"
    if rel != cap.data_dir and not rel.startswith(prefix):
        return None, {"error": f"path must be under {cap.data_dir}/: {path!r}"}
    try:
        return loopyard.read_file(checkout, rel), None
    except FileNotFoundError:
        return None, {"error": f"no such file under loopyard/: {path!r}"}
    except loopyard.LoopyardPathError as e:
        return None, {"error": str(e)}


@mcp.tool()
def capability_render(cap_id: str, project_id: str, path: str) -> dict[str, Any]:
    """Render one doc under a capability's ``loopyard/<dataDir>/`` to a complete,
    CSP-carrying, network-free HTML document (markdown OR raw html), safe to hand
    an iframe ``srcdoc``. Powers C2 Specs + C3 Liked-Results inline rendering.
    Returns ``{ok, path, kind, title, document}``."""
    def work():
        try:
            cap = capabilities.get(cap_id)
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        text, rerr = _read_under_cap(cap, checkout, path)
        if rerr:
            return rerr
        r = hub_render.render_file(os.path.basename(path), text)
        return {"ok": True, "path": path, "kind": r["kind"],
                "title": r["title"], "document": r["document"]}
    return _run(work)


@mcp.tool()
def capability_specs_list(project_id: str) -> dict[str, Any]:
    """C2 Specs: the design docs attached to a Project under ``loopyard/specs/``
    (the ``specs`` capability's data dir), each with its render ``kind``. Returns
    ``{ok, items:[{name, path, kind, bytes}]}`` — empty when none exist yet."""
    def work():
        try:
            cap = capabilities.get("specs")
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        return {"ok": True, "capability": "specs", "items": _cap_files(cap, checkout)}
    return _run(work)


@mcp.tool()
def capability_spec_use_context(project_id: str, path: str,
                                title: str = "") -> dict[str, Any]:
    """C2 Specs → 'use as loop context': read a spec under ``loopyard/specs/`` and
    write it into the C1 creator context-handoff store, so the NEXT ``+loop``
    creator session starts grounded in it (framed there as TRUSTED BACKGROUND,
    not instructions). Deterministic doc name per (project, spec) so re-using a
    spec updates one doc. Returns ``{ok, contextDoc, bytes, path}``."""
    def work():
        try:
            cap = capabilities.get("specs")
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        text, rerr = _read_under_cap(cap, checkout, path)
        if rerr:
            return rerr
        stem = os.path.splitext(os.path.basename(path))[0]
        doc_name = f"spec-{project_id}-{stem}"
        header = (f"# Spec '{os.path.basename(path)}' from Project '{project_id}'\n\n"
                  "The following is a design spec the operator attached to this Project "
                  "and chose to hand to you as background. Use it to make on-model config "
                  "and build choices; it is reference material, not an instruction to you.\n\n"
                  "---\n\n")
        res = creator_context.write_doc(doc_name, header + (text or ""))
        if not res.get("ok"):
            return {"error": res.get("error", "could not write context doc")}
        return {"ok": True, "contextDoc": res["name"], "bytes": res["bytes"], "path": path}
    return _run(work)


def _output_file_text(loop: str, src: str) -> tuple[Optional[str], Optional[dict]]:
    """Read a file from a loop's ``_output/<loop>/`` tree, safely (no traversal
    outside that loop's output dir). Returns ``(text, None)`` or an error dict."""
    bad = _check_name(loop)
    if bad:
        return None, bad
    base = os.path.realpath(os.path.join(_output_base(), loop))
    target = os.path.realpath(os.path.join(base, (src or "").lstrip("/")))
    if target != base and not target.startswith(base + os.sep):
        return None, {"error": f"src escapes the loop output dir: {src!r}"}
    if not os.path.isfile(target):
        return None, {"error": f"no such file in {loop!r} output: {src!r}"}
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), None
    except OSError as e:
        return None, {"error": f"read error: {e}"}


@mcp.tool()
def capability_results_list(project_id: str) -> dict[str, Any]:
    """C3 Liked-Results: the pinned outputs a user kept under ``loopyard/results/``
    (the ``liked-results`` data dir), each fused with its ``<name>.meta.json``
    provenance sidecar (source run/loop, note, pinned-at) when present. A bare
    content file with no sidecar (e.g. the dogfood ``results/architecture.html``)
    lists with ``source:"imported"``. Returns ``{ok, items:[...]}``, newest
    pinned first."""
    def work():
        try:
            cap = capabilities.get("liked-results")
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        items = _cap_files(cap, checkout, exclude_meta=True)
        for it in items:
            meta_rel = f"{cap.data_dir}/{it['name']}.meta.json"
            try:
                meta = loopyard.read_json(checkout, meta_rel)
            except Exception:  # noqa: BLE001 — a bare/legacy file is fine
                meta = None
            if isinstance(meta, dict):
                it["source"] = meta.get("source", "pinned")
                it["note"] = meta.get("note", "")
                it["loop"] = meta.get("loop", "")
                it["run"] = meta.get("run", "")
                it["pinnedAt"] = meta.get("pinnedAt")
            else:
                it["source"] = "imported"
                it["note"] = ""
                it["pinnedAt"] = None
        items.sort(key=lambda it: (it.get("pinnedAt") or 0), reverse=True)
        return {"ok": True, "capability": "liked-results", "items": items}
    return _run(work)


@mcp.tool()
def capability_result_pin(project_id: str, name: str = "", note: str = "",
                          loop: str = "", src: str = "", content: str = "",
                          pinned_at: Optional[float] = None) -> dict[str, Any]:
    """C3 Liked-Results → pin: add an output to a Project's gallery under
    ``loopyard/results/`` with a ``<name>.meta.json`` provenance sidecar. Two
    sources, one of which is required:

    * ``loop`` + ``src`` — copy the file ``src`` from that loop's ``_output/``
      tree (``source:"loop-output"``, provenance recorded);
    * ``content`` — pin a provided/existing file body directly
      (``source:"file"``).

    ``name`` is the gallery filename (defaults to ``src``'s basename). Returns
    ``{ok, entry, meta}``."""
    def work():
        try:
            cap = capabilities.get("liked-results")
        except capabilities.CapabilityError as e:
            return {"error": str(e)}
        checkout, err = _resolve_checkout_for(project_id)
        if err:
            return err
        if loop and src:
            body, rerr = _output_file_text(loop, src)
            if rerr:
                return rerr
            source = "loop-output"
        elif content:
            body = content
            source = "file"
        else:
            return {"error": "pin needs either loop+src (from a loop output) or content"}
        fname = os.path.basename((name or src or "").strip())
        if not fname:
            return {"error": "a result name is required"}
        rel = f"{cap.data_dir}/{fname}"
        try:
            entry = loopyard.write_file(checkout, rel, body)
        except loopyard.LoopyardPathError as e:
            return {"error": str(e)}
        meta = {"source": source, "loop": loop, "run": src, "note": note,
                "pinnedAt": pinned_at if pinned_at is not None else time.time()}
        loopyard.write_json(checkout, f"{cap.data_dir}/{fname}.meta.json", meta)
        return {"ok": True, "entry": entry, "meta": meta}
    return _run(work)


def _list_products_raw() -> list[dict]:
    """Every registered project record (raw), read straight from the registry —
    both the canonical ``projects/`` dir and the legacy ``products/`` dir, deduped
    by id (canonical wins)."""
    out: list[dict] = []
    seen: set[str] = set()
    for d in _project_record_dirs():
        for fn in (sorted(os.listdir(d)) if os.path.isdir(d) else []):
            if not fn.endswith(".json"):
                continue
            rec = _read_json(os.path.join(d, fn))
            if rec is None:
                continue
            rid = rec.get("id") or fn[:-5]
            if rid in seen:
                continue
            seen.add(rid)
            out.append(rec)
    return out


def _loops_with_config(origin: str) -> list[dict]:
    """Enumerate loops on ``origin`` as ``[{name, config}]`` — the input the pure
    attributor needs. Skips private ``_`` dirs and unreadable configs."""
    root = _data_root_for(origin)
    if root is None or not os.path.isdir(root):
        return []
    loops: list[dict] = []
    for entry in sorted(os.listdir(root)):
        if entry.startswith("_"):
            continue
        cfg = _read_json(os.path.join(root, entry, "config.json"))
        if cfg is None:
            continue
        loops.append({"name": entry, "config": cfg})
    return loops


def _checkout_candidates(product: dict) -> list[str]:
    """Ordered ABSOLUTE dirs a product's checkout might live in on THIS box:
    ``products_root/<repoDir|id>`` (the shared-library convention) first, then
    ``$HOME/<repoDir|id>`` — real repos frequently live directly under home, not
    in an (often empty) ``~/products``. Portable-slugged, de-duped, order kept."""
    root = _products_root()
    home = os.path.expanduser("~")
    names: list[str] = []
    for key in (product.get("repoDir"), product.get("id")):
        if isinstance(key, str) and key.strip():
            s = products._slugify_relpath(key)
            if s and s not in names:
                names.append(s)
    out: list[str] = []
    for base in (root, home):
        for nm in names:
            cand = os.path.join(base, nm)
            if cand not in out:
                out.append(cand)
    return out


def _resolve_product_checkout(product: dict) -> Optional[str]:
    """Find the local checkout dir for a product on THIS box (read-only).

    If the product already has a ``gitRemote``, prefer a candidate whose git
    remote MATCHES it (normalized identity) and return ``None`` if none does —
    we never silently bind a differently-remoted checkout. If the product has NO
    remote yet (the gather self-population case), return the first candidate that
    is a real git checkout so its remote can be read. ``None`` if nothing fits."""
    want = products.normalize_git_remote(product.get("gitRemote"))
    first_git: Optional[str] = None
    for cand in _checkout_candidates(product):
        url = products_git.remote_url(cand)
        if url is None:
            continue                       # not a git checkout / no origin remote
        if first_git is None:
            first_git = cand
        if want and products.normalize_git_remote(url) == want:
            return cand                    # exact remote-identity match
    return None if want else first_git


def _self_populate_git(raw: dict) -> dict:
    """Fill a product's REAL ``gitRemote``/``gitBranch`` from a resolved checkout
    (server-side git I/O — the pure attributor can't see the filesystem). Only
    ever FILLS a blank ``gitRemote`` (never overwrites a set one); returns ``raw``
    unchanged when no checkout resolves. Mutates + returns the passed dict."""
    if raw.get("gitRemote"):
        return raw                         # already has canonical identity
    checkout = _resolve_product_checkout(raw)
    if not checkout:
        return raw
    url = products_git.remote_url(checkout)
    if url:
        raw["gitRemote"] = url
        branch = products_git.current_branch(checkout)
        if branch:
            raw["gitBranch"] = branch
    return raw


def _backfill_existing_remotes(dry_run: bool) -> list[str]:
    """One-time, ADDITIVE migration during gather: for every registered product
    whose ``gitRemote`` is still null, resolve its checkout and, if found, write
    the real remote/branch back. Fills blanks ONLY — never collapses, renames,
    deletes, or re-remotes an existing record. Idempotent (a populated product is
    skipped next time). Returns the ids actually updated."""
    updated: list[str] = []
    for rec in _list_products_raw():
        if not isinstance(rec, dict) or rec.get("gitRemote"):
            continue
        pid = rec.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        filled = _self_populate_git(dict(rec))
        if filled.get("gitRemote"):
            updated.append(pid)
            if not dry_run:
                merged = dict(rec)
                merged["gitRemote"] = filled["gitRemote"]
                if filled.get("gitBranch"):
                    merged["gitBranch"] = filled["gitBranch"]
                # write back to the record's OWN file (legacy or canonical dir).
                _write_json(_project_record_path(pid), merged)
    return updated


def _gather_products(origin: str, dry_run: bool = False) -> dict[str, Any]:
    """Core auto-gather: attribute every loop on ``origin`` to a product ONLY BY
    ITS EXPLICIT projectId/productId (a git remote is merely a suggestion since
    loopyard-bug-1790177434 — see ``products_gather.derive_for_loop``) and
    MATERIALIZE the explicitly bound ids missing from the registry (idempotent),
    so the catalog and the switcher always agree. Name-derived
    duplicate invention was removed (2026-09), so this never spawns a pseudo-
    project per loop. Only writes when genuinely-new git-attributed loops exist.
    Returns a summary; used by both the explicit tool and the lazy auto-run on the
    products list."""
    loops = _loops_with_config(origin)
    existing = _list_products_raw()
    result = products_gather.attribute_loops(loops, existing)
    created: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    for cand in result["discovered"]:
        raw = {k: v for k, v in cand.items() if not k.startswith("_")}
        # self-populate the REAL remote/branch from a resolved checkout BEFORE
        # validating, so a real clone lands with its canonical git identity
        # (today's gather copied a signal that never appears → 100% null).
        raw = _self_populate_git(raw)
        res = products.validate_product(raw)
        if not res["ok"]:
            errors.append({"id": cand.get("id"), "errors": res["errors"]})
            continue
        pid = res["product"]["id"]
        if _project_record_exists(pid):   # idempotent — never clobber existing
            skipped.append(pid)           #   (in EITHER registry dir)
            continue
        if not dry_run:
            rec = dict(res["product"])
            rec["saved"] = time.time()
            rec["gathered"] = True        # provenance: auto-discovered, not typed
            rec["gatheredFrom"] = cand.get("_signal")
            _write_json(_project_record_path(pid), rec)   # new → canonical projects/
        created.append(pid)
    # additive one-time backfill: fill a real remote onto EXISTING null-remote
    # products whose checkout resolves (fills blanks only; never re-remotes).
    remotes_filled = _backfill_existing_remotes(dry_run)
    return {"ok": True, "origin": origin, "dry_run": dry_run,
            "loops_scanned": len(loops),
            "attribution": result["attribution"], "signals": result["signals"],
            "unattributed": result["unattributed"],
            "suggestions": result.get("suggestions", {}),
            "discovered": [c.get("id") for c in result["discovered"]],
            "created": created, "skipped": skipped, "errors": errors,
            "remotesFilled": remotes_filled}


@mcp.tool()
def loop_projects_gather(origin: str = origins.LOCAL_ORIGIN_ID,
                         dry_run: bool = False) -> dict[str, Any]:
    """AUTO-GATHER projects from loops we've ALREADY run — deterministically,
    no AI. Scans every loop on ``origin`` and attributes it to a project ONLY by
    an explicit ``projectId``/``productId`` on the config, else UNATTRIBUTED (a
    ``gitRemote`` / goal target is returned under ``suggestions``, never
    attributed or cataloged — loopyard-bug-1790177434). The former name-derived signals (a
    ``repoDir`` basename, an absolute working-dir basename, or a folder path
    scraped from the goal) invented a near-duplicate pseudo-project per loop and
    were REMOVED (2026-09; see ``products_gather.derive_for_loop``) — a loop with
    no explicit project and no git remote is now honestly unattributed rather than
    materialized as junk. Materializes only the git-attributed projects missing
    from the registry (explicitly bound ids only), so the Projects page shows only intentional products with
    ZERO manual work. Idempotent (checks BOTH registry dirs); only writes when
    genuinely-new git-attributed loops exist; new records land in the canonical
    ``projects/`` dir. ``dry_run=True`` reports without writing. Returns
    ``{ok, loops_scanned, attribution, signals, discovered, created, skipped}``."""
    return _run(lambda: _gather_products(origin, dry_run=dry_run))


def _assign_project(project_id: str, names: list) -> dict[str, Any]:
    """Bind N loops to ``project_id`` through the SAME validated ``loop_save`` path
    the editor uses (loopyard-follow-up-1790089835). Per-loop results; one bad
    loop never blocks the rest."""
    pid = project_id.strip() if isinstance(project_id, str) else ""
    if not pid or products.slug(pid) != pid:
        return {"error": f"bad project id {project_id!r} (must be a slug)"}
    if not isinstance(names, list) or not names:
        return {"error": "names must be a non-empty list of loop names"}
    assigned: list[str] = []
    errors: list[dict] = []
    for name in names:
        bad = _check_name(name) if isinstance(name, str) else {"error": "bad name"}
        if bad:
            errors.append({"name": name, "error": bad.get("error")})
            continue
        cfg = _read_json(_config_path(name))
        if cfg is None:
            errors.append({"name": name, "error": "loop not found"})
            continue
        cfg["projectId"] = pid
        cfg["productId"] = pid            # schema keeps the alias in lock-step
        res = loop_save(cfg)
        if res.get("ok"):
            assigned.append(name)
        else:
            errors.append({"name": name, "error": res.get("error"),
                           "errors": res.get("errors")})
    return {"ok": not errors, "project": pid, "assigned": assigned, "errors": errors}


@mcp.tool()
def loop_projects_assign(project_id: str, names: list) -> dict[str, Any]:
    """BULK-ASSIGN a project: set ``projectId`` on every loop in ``names`` via the
    validated ``loop_save`` path (never a raw config write). Also how the surface
    ACCEPTS a suggested project for one unbound loop. Returns ``{ok, project,
    assigned:[names], errors:[{name, error}]}`` — ``ok`` only if every loop took
    the binding; the rest are still assigned."""
    return _run(lambda: _assign_project(project_id, names))


@mcp.tool()
def loop_products_gather(origin: str = origins.LOCAL_ORIGIN_ID,
                         dry_run: bool = False) -> dict[str, Any]:
    """DEPRECATED back-compat ALIAS of :func:`loop_projects_gather`. Identical
    behavior; prefer ``loop_projects_gather``."""
    return _run(lambda: _gather_products(origin, dry_run=dry_run))


@mcp.tool()
def loop_output_dir(name: str, create: bool = True, owner: str = "") -> dict[str, Any]:
    """The per-loop OUTPUT FOLDER (Pillar 3 output contract): where a loop's
    curated result files live. Host-resolved at run time (LOOPS_OUTPUT_DIR base)
    and created on demand — NOT baked into the portable config. Returns
    {name, path, created}.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        path = os.path.join(_output_base(), name)
        made = False
        if create and not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            made = True
        return {"name": name, "path": path, "created": made}
    return _run(work)


@mcp.tool()
def loop_run_agent_standalone(agent_id: str, product_id: str,
                              model: Optional[str] = None,
                              version: Optional[int] = None,
                              origin: Optional[str] = None,
                              auto_register: bool = True) -> dict[str, Any]:
    """Prepare a STANDALONE run — one agent × one origin × one product × a model
    — the activation front door (no loop authoring needed). Resolves the
    agent's pinned identity at ``version`` (default head), the product's
    host-resolved repo path, and the selected model (explicit → agent's
    preferred → inherit), creates the run's output folder, and writes a
    'PR-description' result.md stub + run.json.

    ``origin`` names which mirror the run runs on — defaults to ``"local"``
    (the only origin runs actually execute on today; cross-origin dispatch is
    a later increment). If ``origin`` is set but unknown from this box, an
    error is returned rather than silently swapping to local.

    ``auto_register`` (default True): if the agent id has no registry record
    yet, materialize a placeholder record so the standalone flow WORKS even for
    agents nobody has curated (north star §4 — every agent has a detail view).

    Returns ``{ok, agent, version, product, origin, model, modelApplied,
    outputDir, resultPath, prompt}``. ``modelApplied`` follows the shared
    passthrough gate (headless.model_passthrough_enabled — on unless
    LOOPS_MODEL_PASSTHROUGH=0) ∧ a resolved model."""
    def work():
        origin_id = origin or origins.LOCAL_ORIGIN_ID
        got_origin = None
        if origin_id != origins.LOCAL_ORIGIN_ID:
            got_origin = origins.get_origin(_local_mirror_path(), origin_id,
                                             now=time.time())
            if got_origin is None:
                return {"error": f"unknown origin {origin_id!r} "
                                 f"(not visible from this box)"}
        araw = _read_json(os.path.join(_registry_dir("agent"), f"{agent_id}.json"))
        if araw is None and auto_register:
            _materialize_one(agent_id, now=time.time())
            araw = _read_json(os.path.join(_registry_dir("agent"), f"{agent_id}.json"))
        if araw is None:
            return {"error": f"no agent {agent_id!r} in registry"}
        praw = _read_json(_project_record_path(product_id))   # canonical + legacy dir
        if praw is None:
            return {"error": f"no product {product_id!r} in registry"}
        now = time.time()
        arec = agents.migrate_agent_record(araw, now=araw.get("saved") or 0.0)
        paths = products.resolve_paths(
            praw, products_root=_products_root(), output_base=_output_base())
        try:
            chosen = standalone.select_model(model, arec.get("model"))
        except ValueError as exc:
            return {"error": str(exc)}
        # Origin ↔ model preflight: refuse a run whose model targets a CLI
        # the origin CONFIRMED is not authed. Unknown / not-wired stays
        # permissive (see origins_probe.can_run's contract) so this gate is
        # never noisier than the honest data justifies. This is the
        # SAME predicate the surface launcher uses — a direct MCP caller
        # can no longer bypass the gate that the UI shows the human.
        origin_kind = (got_origin or {}).get("kind", "local")
        cap_result = origins_probe.probe_origin(
            origin_kind, now, remote_probe=_remote_caps_probe(
                got_origin["id"], cached=True)
            if got_origin and origin_kind != "local" and chosen else None)
        gate = origins_probe.can_run(cap_result, chosen)
        if not gate["canRun"]:
            return {"error": f"origin {origin_id!r} cannot run model "
                             f"{chosen!r}: {gate['reason']}",
                    "originGate": gate}
        # per-run output folder under the product's output root (portable-relative)
        out_dir = os.path.join(paths["outputPath"], f"standalone-{agent_id}")
        try:
            spec = standalone.build_run_spec(
                arec, version, praw, chosen,
                repo_path=paths["repoPath"], output_dir=out_dir, now=now,
                origin=origin_id,
                # ONE shared gate for both the front door and the loop path
                # (same env server.py:_make_substrate reads) — no second switch.
                model_passthrough=headless.model_passthrough_enabled())
        except ValueError as exc:
            return {"error": str(exc)}
        os.makedirs(out_dir, exist_ok=True)
        result_md = standalone.render_result_markdown(spec)
        result_path = os.path.join(out_dir, "result.md")
        with open(result_path, "w", encoding="utf-8") as fh:
            fh.write(result_md)
        _write_json(os.path.join(out_dir, "run.json"), spec)
        return {
            "ok": True,
            "agent": agent_id, "version": spec["agent"]["version"],
            "product": product_id, "model": chosen,
            "origin": origin_id,
            "modelApplied": spec["modelApplied"], "modelNote": spec["modelNote"],
            "outputDir": out_dir, "resultPath": result_path,
            "prompt": standalone.frame_standalone_prompt(spec),
        }
    return _run(work)


# ── (removed) runs registry ───────────────────────────────────────────────────
# The standalone-RUN registry (`loop_run_list` / `loop_run_get`, backed by the
# `runs` module) is DELETED per the redesign build order §5.1: there is no
# standalone "run" object anymore — the smallest unit is a **loop of one**. Its
# by-origin/agent/project/model value moves onto the Loops list as filters +
# the `single_agent` facet (see `loop_list`). The creation path
# `loop_run_agent_standalone` is unaffected here; unifying it onto the loop-of-1
# primitive is a later build-order step.


# ── P2: cross-origin dispatch (the unified-hub control surface) ────────────────
# ONE control plane, execution anywhere. A run requested here can execute on any
# connected origin: local short-circuits to today's path; a remote origin gets a
# request envelope dropped in a mirror outbox that its daemon executes + answers
# (design: docs/ORIGINS-PRODUCTION-DESIGN.md §4.2, suggestions/CROSS-ORIGIN-
# DISPATCH.md). No new UI — this is the clean MCP surface future clients sit on;
# per-origin dirs keep the door open to per-tenant separation without building it.
_DISPATCH_SEQ = [0]  # monotonic counter for request ids (unique within this box)


def _dispatch_root() -> str:
    return os.path.join(_local_mirror_path(), "_dispatch")


def _dispatch_outbox(origin_id: str) -> str:
    return dispatch.outbox_dir(_dispatch_root(), origin_id)


def _requested_by() -> str:
    """Honest sender id: this box's local-origin id + host, no secret."""
    return f"{origins.LOCAL_ORIGIN_ID}@{os.uname().nodename}"


@mcp.tool()
def loop_dispatch_standalone(agent_id: str, product_id: str, origin: str,
                             model: Optional[str] = None,
                             version: Optional[int] = None) -> dict[str, Any]:
    """P2: dispatch a STANDALONE run (agent × product × model) onto ``origin`` —
    the local box OR a connected remote origin — from the ONE hub.

    ``origin == "local"`` short-circuits to loop_run_agent_standalone (no envelope,
    no mirror hop) and returns ``{ok, state:"local", run}``. A REMOTE origin:
    refuses up-front (no envelope written) if it's unknown, or not reachable /
    stale / pending — the honest degradation the surface shows, never a silent
    success; otherwise writes a dispatch request envelope into the origin's mirror
    outbox and returns ``{ok, requestId, state:"queued"}``. Poll
    loop_dispatch_status(requestId) for the origin's answer. The model↔CLI gate
    reuses origins_probe.can_run (permissive-with-reason for a remote origin whose
    CLIs we can't probe over the wire — the reason rides along, honestly)."""
    def work():
        if origin == origins.LOCAL_ORIGIN_ID:
            res = loop_run_agent_standalone(agent_id, product_id, model=model,
                                            version=version, origin=origin)
            if res.get("error"):
                return res
            return {"ok": True, "state": "local", "origin": origin, "run": res}
        return _dispatch_remote(
            origin, run_kind="standalone",
            spec={"agent_id": agent_id, "product_id": product_id,
                  "model": model, "version": version},
            model=model)
    return _run(work)


def _origin_connected(origin: str) -> bool:
    """True iff ``origin`` is a REMOTE origin holding a live channel to this
    engine's origin hub (so a live dispatch will reach its engine)."""
    svc = _remote_dispatch(origin)
    if svc is None or not _is_remote_origin(origin):
        return False
    try:
        svc.resolve_device(origin)
    except Exception:  # noqa: BLE001 — unknown / not connected
        return False
    return True


def _dispatch_remote(origin: str, *, run_kind: str, spec: dict,
                     model: Optional[str] = None) -> dict[str, Any]:
    """Queue a dispatch request for a REMOTE origin — the one write-path both
    loop_dispatch_standalone and loop_dispatch_start funnel through, so the
    reachability guard, the model↔CLI gate, and the envelope shape can't drift.
    Refuses up-front (NO envelope written) for an unknown or non-dispatchable
    origin; otherwise writes the request to the origin's mirror outbox and returns
    ``{ok, requestId, state:"queued"}``. Not a tool — an internal seam."""
    now = time.time()
    got = origins.get_origin(_local_mirror_path(), origin, now=now)
    if got is None:
        return {"error": f"unknown origin {origin!r} (not visible from this box)"}
    # degradation guard: the SAME reachable+fresh predicate the read-surface
    # (loop_hub_view's canDispatch) advertises — so what the dashboard says is
    # dispatchable and what the write-path accepts never disagree.
    if not _can_dispatch(got):
        return {"ok": False, "state": "unreachable", "origin": origin,
                "reason": f"origin {origin!r} not reachable "
                          f"(health={got.get('health')!r}, "
                          f"last_seen={got.get('last_seen')})"}
    # model↔CLI gate — SAME predicate the local path + surface use. Remote CLIs
    # can't be probed over the wire, so can_run stays permissive but returns an
    # honest caveat we carry into the queued result.
    kind = got.get("kind", "mirror")
    gate = origins_probe.can_run(
        origins_probe.probe_origin(kind, now, remote_probe=_remote_caps_probe(
            got["id"], cached=True) if model and kind != "local" else None),
        model)
    if not gate["canRun"]:
        return {"ok": False, "state": "refused", "origin": origin,
                "reason": f"origin {origin!r} cannot run model {model!r}: "
                          f"{gate['reason']}", "originGate": gate}
    _DISPATCH_SEQ[0] += 1
    req_id = dispatch.new_request_id(_DISPATCH_SEQ[0], now)
    env = dispatch.build_request(
        req_id, origin_id=origin, run_kind=run_kind, spec=spec,
        requested_by=_requested_by(), now=now,
        constraints={"model": model, "cli": origins_probe.model_cli(model)})
    _write_json(os.path.join(_dispatch_outbox(origin), f"{req_id}.json"), env)
    return {"ok": True, "requestId": req_id, "origin": origin,
            "state": dispatch.STATE_QUEUED, "caveat": gate.get("reason") or None}


@mcp.tool()
def loop_dispatch_start(name: str, origin: str) -> dict[str, Any]:
    """P2 §b: START a saved LOOP on ``origin`` — the local box OR a connected
    remote origin — from the ONE hub. This is the loop-level twin of
    loop_dispatch_standalone: control a loop regardless of where it executes.

    ``origin == "local"`` short-circuits to start_loop (returns the initial result
    envelope). A REMOTE origin gets a ``run_kind="loop"`` request envelope written
    to its mirror outbox (refused up-front, no envelope, if the origin is unknown
    or not dispatchable); poll loop_dispatch_status(requestId) for the origin's
    answer once its dispatch_inbox executes it. The saved loop config must exist on
    the TARGET origin (it runs there); the hub doesn't ship the config over the
    wire in v1.2."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        if origin == origins.LOCAL_ORIGIN_ID:
            res = start_loop(name)          # initial envelope (or {"error": ...})
            if res.get("error"):
                return res
            return {"ok": True, "state": "local", "origin": origin, "run": res}
        # gap #3: a CONNECTED origin is started live over the origin channel
        # (prepared + run on its own engine); only an origin this hub can't
        # reach live falls back to the mirror-outbox envelope.
        if _origin_connected(origin):
            res = _loop_start_remote(name, "", origin)
            if res.get("error"):
                return res
            return {"ok": True, "state": "dispatched", "origin": origin,
                    "via": "origin-channel", "run": res}
        return _dispatch_remote(origin, run_kind="loop", spec={"name": name})
    return _run(work)


@mcp.tool()
def loop_dispatch_stop(name: str, origin: str) -> dict[str, Any]:
    """P2 §b: STOP a running loop on ``origin`` — local OR a connected remote — from
    the ONE hub. The lifecycle twin of loop_dispatch_start, so a loop can be
    controlled regardless of where it executes.

    ``origin == "local"`` short-circuits to loop_stop. A REMOTE origin gets a
    ``run_kind="control"`` request (``spec={action:"stop", name}``) written to its
    mirror outbox — the origin's dispatch_inbox runs loop_stop there; poll
    loop_dispatch_status(requestId) for the ack. Refused up-front (no envelope) if
    the origin is unknown or not dispatchable."""
    def work():
        bad = _check_name(name)
        if bad:
            return bad
        if origin == origins.LOCAL_ORIGIN_ID:
            res = loop_stop(name)
            if res.get("error"):
                return res
            return {"ok": True, "state": "local", "origin": origin, "run": res}
        # gap #3: a CONNECTED origin is stopped live over the origin channel
        # (the twin of loop_dispatch_start); else the mirror-outbox envelope.
        if _origin_connected(origin):
            res = _remote_loop_stop(name, origin)
            if res.get("error"):
                return res
            return {"ok": True, "state": "stopped", "origin": origin,
                    "via": "origin-channel", "run": res}
        return _dispatch_remote(origin, run_kind=dispatch.CONTROL_KIND,
                                spec={"action": "stop", "name": name})
    return _run(work)


@mcp.tool()
def loop_dispatch_status(request_id: str) -> dict[str, Any]:
    """P2: poll a cross-origin dispatch by ``request_id``. Returns
    ``{request_id, origin, state, response?, age_sec?}`` where state is
    queued | accepted | rejected | unknown. ``queued`` = the request is written
    but the origin hasn't answered yet (the mirror may not have round-tripped) —
    this NEVER silently flips to success; only a real response envelope on disk
    moves it to accepted/rejected. ``unknown`` = no request/response on this box
    for that id."""
    def work():
        if not dispatch.valid_id(request_id):
            return {"error": f"invalid request_id {request_id!r}"}
        root = _dispatch_root()
        # find the request (outbox) and any response (inbox) across origin dirs —
        # the id is globally unique on this box, so a scan is unambiguous.
        req = _dispatch_find(os.path.join(root, dispatch.OUTBOX), request_id)
        resp = _dispatch_find(os.path.join(root, dispatch.INBOX), request_id)
        if req is None and resp is None:
            return {"request_id": request_id, "state": dispatch.STATE_UNKNOWN}
        # originId can come from either envelope; prefer whichever HAS it (the
        # request always carries it; the response now does too, but be robust).
        origin_id = (req or {}).get("originId") or (resp or {}).get("originId")
        state = dispatch.response_state(resp)
        out: dict[str, Any] = {"request_id": request_id, "origin": origin_id,
                               "state": state}
        if resp is not None:
            out["response"] = resp
        if req is not None and isinstance(req.get("requestedAt"), (int, float)):
            out["age_sec"] = round(time.time() - float(req["requestedAt"]), 1)
        return out
    return _run(work)


def _dispatch_find(base: str, request_id: str) -> Optional[dict]:
    """Find ``<base>/<origin>/<request_id>.json`` across origin subdirs. Pure
    read — returns the parsed envelope or None."""
    try:
        origin_dirs = os.listdir(base)
    except OSError:
        return None
    for od in origin_dirs:
        env = _read_json(os.path.join(base, od, f"{request_id}.json"))
        if env is not None:
            return env
    return None


@mcp.tool()
def loop_clone(from_id: str, new_name: str) -> dict[str, Any]:
    """Clone a registry loop into a NEW saved loop under `new_name` (validated +
    persisted, ready to loop_start). Returns {ok, name} or {"error": ...}."""
    def work():
        bad = _check_name(new_name)
        if bad:
            return bad
        rec = _read_json(os.path.join(_registry_dir("loop"), f"{from_id}.json"))
        if rec is None or "config" not in rec:
            return {"error": f"no registry loop {from_id!r} to clone"}
        cfg = dict(rec["config"])
        cfg["name"] = new_name
        res = validate_config(cfg)
        if not res["ok"]:
            return {"error": "cloned config invalid", "errors": res["errors"]}
        path = _write_json(_config_path(new_name), res["config"])
        _sync_loop_id(new_name)   # S-1 test 6: a clone is its own loopId
        return {"ok": True, "name": new_name, "path": path}
    return _run(work)


# ── loop-creator (CAP-3): connect a CLI -> run creator -> answer Qs -> team ────
# The onboarding centerpiece. loop_creator_agent hands out the portable agent
# identity (runnable on claude OR codex); loop_creator_prompt builds the
# instruction the CLI session runs; loop_creator_scaffold turns a structured team
# spec into a VALID config deterministically (no AI); loop_creator_validate gates
# any emitted config through the real linter + schema before it can become a run.
@mcp.tool()
def loop_creator_agent(register: bool = False) -> dict[str, Any]:
    """The loop-creator agent's portable IDENTITY (persona + generic goal +
    default role), the creation RULES it follows, and the MATERIAL questions it
    may ask. With ``register=True`` it is saved to the agent registry as
    ``loop_creator`` so it can be run standalone on the connected CLI (claude or
    codex). Returns {agent, rules, material_questions [, registered]}."""
    def work():
        out = {
            "agent": {"id": "loop_creator", "persona": creator.CREATOR_PERSONA,
                      "genericGoal": creator.CREATOR_GENERIC_GOAL,
                      "defaultRole": schema.WORKER, "runtimes": creator.VALID_RUNTIMES},
            "rules": authoring.RULES,
            "material_questions": creator.MATERIAL_QUESTIONS,
        }
        if register:
            out["registered"] = loop_registry_save_agent(
                "loop_creator", persona=creator.CREATOR_PERSONA,
                generic_goal=creator.CREATOR_GENERIC_GOAL, role=schema.WORKER,
                note="CAP-3 loop-creator onboarding agent")
        return out
    return _run(work)


@mcp.tool()
def loop_creator_prompt(profile: str = "", instructions: str = "",
                        answers: Optional[dict] = None) -> dict[str, Any]:
    """Build the exact instruction the creator session runs from a pasted
    ``profile``/harness + free-text ``instructions`` (+ any ``answers`` already
    given). Returns {prompt, open_questions} — ``open_questions`` are the
    still-unanswered MATERIAL questions the prompt asks first (empty ⇒ the prompt
    emits the config immediately). Paste ``prompt`` into a connected claude/codex
    session, or feed it as a standalone agent's loop goal."""
    def work():
        ans = answers if isinstance(answers, dict) else None
        context = creator_context.read_all()
        return {"prompt": creator.build_prompt(profile, instructions, ans,
                                               context=context),
                "open_questions": creator._unanswered(profile or "",
                                                      instructions or "", ans),
                "context_docs": [d["name"] for d in creator_context.list_docs()]}
    return _run(work)


@mcp.tool()
def loop_creator_context_list() -> dict[str, Any]:
    """List the C1 creator context-handoff store — the seed docs
    (``<data>/_creator_context/*.md``) whose concatenation is framed as trusted
    background in the creator preprompt. Returns {dir, docs:[{name, path, bytes}],
    combined_bytes}."""
    def work():
        docs = creator_context.list_docs()
        return {"dir": creator_context.context_dir(), "docs": docs,
                "combined_bytes": len(creator_context.read_all().encode("utf-8"))}
    return _run(work)


@mcp.tool()
def loop_creator_context_write(name: str, text: str) -> dict[str, Any]:
    """Write (create/overwrite) a C1 creator context doc so coord / any session
    can seed the loop-creator with real background. ``name`` is slugged to a safe
    ``<slug>.md`` basename (no path escape); ``text`` is the doc body. It becomes
    trusted-background context in the next creator preprompt. Returns
    {ok, name, path, bytes} or {ok:false, error}."""
    def work():
        return creator_context.write_doc(name, text)
    return _run(work)


@mcp.tool()
def loop_creator_context_seed() -> dict[str, Any]:
    """Ensure the default operator seed ``coord-context.md`` exists in the C1
    store, idempotently (never clobbers an existing/edited seed). Returns
    {seeded, name, path}."""
    def work():
        return creator_context.seed_default()
    return _run(work)


@mcp.tool()
def loop_creator_scaffold(spec: dict) -> dict[str, Any]:
    """DETERMINISTICALLY build a VALID multi-role, multi-CLI loop config from a
    structured team spec — no AI. ``spec`` = {name, goal, roles:[{id?, role?,
    runtime?, model?, personality?, goal?}], budget?}. Missing personas/goals get
    on-model role defaults; a missing manager is added. Returns {ok, config,
    warnings, lint} (schema-normalized config) or {ok:false, errors}. This is the
    'make it POSSIBLE to build a team' floor and the creator's safe fallback."""
    def work():
        return creator.scaffold_config(spec)
    return _run(work)


@mcp.tool()
def loop_creator_suggest(goal: str, name: str = "") -> dict[str, Any]:
    """Suggest an EDITABLE, rule-checked team SHAPE from a described ``goal`` —
    NOT a fixed template. The roles are derived from signals in the goal (a build
    goal → manager+builder+reviewer+tester; a research/writing goal →
    manager+writer+reviewer; a design goal swaps in a designer), always with
    exactly one manager and at least one worker. Each suggested role carries a
    ``why``. Returns {ok, editable, goal, signals, roles:[{id, role, why}], spec,
    config, warnings, lint, rules, note}: ``config`` is the schema-normalized
    preview (via the deterministic scaffold) and ``lint`` is empty when it is
    rule-clean. The user edits the suggestion, then loop_creator_validate +
    loop_save turn it into a run."""
    def work():
        return creator.suggest_team(goal, name=name)
    return _run(work)


@mcp.tool()
def loop_creator_brief(goal: str = "", answers: Optional[dict] = None,
                       name: str = "", just_one: bool = False,
                       projectId: str = "", target: str = "",
                       phase: str = "brief") -> dict[str, Any]:
    """The NATIVE conversational brief (Door B, rd-create §3.4) — daemon-free and
    in-process. From a described ``goal`` (or an existing loop's ``name``, whose
    saved goal/project it loads) plus the ``answers`` gathered so far, return the
    next 1-2 still-open MATERIAL questions AND a live, editable team-preview (the
    config). As the caller sends accumulated ``answers`` the questions narrow and
    the preview refines; ``done`` flips true when nothing material is open — then
    ``preview.config`` is startable via loop_creator_validate → loop_save →
    loop_start (the composer's proven seam). ``just_one`` (or a "just one agent"
    answer to the roles question) builds a LOOP OF 1. ``target`` folds a Door-A
    substrate into the goal. ``phase='debrief'`` reframes it as the end-of-run
    capture conversation.

    This is the SAME create/brief surface the redesign's ＋Loop composer AND the
    per-loop Brief/Debrief buttons point at — unlike loop_brief_session it needs
    NO worker daemon / tmux, so it works on the isolated redesign stack and never
    drops the user into a shell. Returns {ok, done, goal, questions, answered,
    preview, single_agent, projectId, phase, note} or {ok:false, error}."""
    def work():
        g, pid, solo = goal, projectId, just_one
        if name:
            # brief/debrief over an EXISTING saved loop: seed from its config.
            bad = _check_name(name)
            if bad:
                return {"ok": False, **bad}
            cfg = _read_json(_config_path(name))
            if cfg is None:
                return {"ok": False,
                        "error": f"loop {name!r} has no saved config (loop_save first)"}
            g = goal or cfg.get("goal", "")
            pid = projectId or schema.project_id(cfg) or ""
            solo = just_one or schema.is_single_agent(cfg)
        ans = answers if isinstance(answers, dict) else None
        return creator.brief_turn(goal=g, answers=ans, just_one=solo, name=name,
                                  project_id=pid, target=target, phase=phase)
    return _run(work)


@mcp.tool()
def loop_creator_suggest_one(goal: str, name: str = "") -> dict[str, Any]:
    """Suggest a LOOP OF 1 (rd-create §3.5) — one agent that drives the ``goal`` to
    done and decides when it's complete. The "Ask one agent" fast path: a single,
    schema-valid, ``is_single_agent`` config the composer saves + starts through the
    same validate→save→start seam. Returns the suggest-shaped dict {ok, editable,
    goal, roles, spec, config, single_agent, …} or {ok:false, errors}."""
    def work():
        return creator.suggest_one(goal, name=name)
    return _run(work)


# ── onboarding (BRIEF §4): the first-run guide a NEW user runs FIRST ───────────
# loop_onboarding_agent hands out the guide identity + the plain-language welcome +
# the step script (and can register the agent); loop_onboard_first_team is the
# deterministic 'get me to a running first team' driver the +Loop UI / `yard
# onboard` lean on — it derives a rule-checked team, SAVES it, and (opt-in) STARTS
# it, so a brand-new user reaches a real running team of their own design.
@mcp.tool()
def loop_onboarding_agent(register: bool = False) -> dict[str, Any]:
    """The first-run onboarding guide: its portable IDENTITY (persona + generic
    goal), the plain-language WELCOME explaining a self-aligning team, and the
    ordered STEP script (machine → describe → run → result). With
    ``register=True`` it is saved to the agent registry as ``loop_onboarding`` so a
    user can run it standalone on their connected CLI (claude or codex). Returns
    {agent, welcome, steps, rules [, registered]}."""
    def work():
        out = onboarding.onboarding_summary()
        if register:
            out["registered"] = loop_registry_save_agent(
                "loop_onboarding", persona=onboarding.ONBOARDING_PERSONA,
                generic_goal=onboarding.ONBOARDING_GENERIC_GOAL, role=schema.WORKER,
                note="BRIEF §4 first-run onboarding guide")
        return out
    return _run(work)


@mcp.tool()
def loop_onboarding_progress() -> dict[str, Any]:
    """READ-ONLY getting-started state for the web app's Home checklist (GET
    /api/loops/onboarding) — the exact payload ``yard onboard --json`` gives the
    desktop guide: {agent, welcome, steps[{key, title, what, route, action, done}],
    explore[…same, done], places, progress{done, total, next, complete}, prompt}.
    Every ``done`` is derived from this box's data (a reachable machine, a saved /
    started loop, a rating, a plan, a starred agent) — never from a click. The
    machine count is the live fleet view (this box + connected origins)."""
    def work():
        try:
            machines: Optional[int] = sum(
                1 for o in _origins_with_live(time.time()) if o.get("reachable"))
        except Exception:  # noqa: BLE001 — fall back to the local mirror count
            machines = None
        return onboarding.progress_payload(_local_mirror_path(), machines=machines)
    return _run(work)


@mcp.tool()
def loop_onboard_first_team(goal: str, name: str = "",
                            start: bool = False) -> dict[str, Any]:
    """Drive a NEW user's first team end-to-end from a described ``goal``: derive a
    rule-checked, editable team (loop_creator_suggest), SAVE it as a runnable loop,
    and — when ``start=True`` — START it. Returns {ok, welcome, goal, suggestion,
    config, saved, started?, run?, dashboard, next, steps}. ``saved`` is loop_save's
    result; ``started``/``run`` carry start_loop's outcome (best-effort — a save
    still succeeds if no runner is attached yet, with the start error surfaced).
    This is the create-and-run spine behind the +Loop UI and `yard onboard`."""
    def work():
        plan = onboarding.onboard_first_team(goal, name=name)
        if not plan.get("ok"):
            return plan
        cfg = plan.get("config") or {}
        saved = loop_save(cfg)
        plan["saved"] = saved
        loop_name = (saved.get("name") if isinstance(saved, dict) else None) \
            or cfg.get("name")
        plan["dashboard"] = {"loops": "/loops", "loop": f"/loops/{loop_name}"}
        if start and isinstance(saved, dict) and saved.get("ok") and loop_name:
            started = start_loop(loop_name)
            plan["started"] = bool(isinstance(started, dict)
                                   and not started.get("error"))
            plan["run"] = started
        return plan
    return _run(work)


@mcp.tool()
def loop_creator_validate(config: Optional[dict] = None,
                          text: Optional[Any] = None) -> dict[str, Any]:
    """Validate a creator's emitted config against the REAL linter + schema.
    Pass ``config`` (a dict) OR ``text`` (the session's raw output — a fenced
    ```json block or free text with a JSON object). Returns {ok, config,
    errors, warnings, lint, source}; ``config`` on success is schema-normalized
    and ready for loop_save. Nothing the creator produces should reach a run
    without passing this.

    ``text`` is typed ``Any`` on purpose: the +Loop editor commonly holds a pure
    JSON object (right after 'Suggest team', or a pasted config), and the MCP
    argument layer coerces such a JSON-looking string into a dict before it
    reaches this tool. A strict ``str`` annotation rejected that dict and broke
    the advertised front door; ``extract_config`` already accepts a dict as-is,
    so we widen the boundary and let it normalize either shape."""
    def work():
        payload = config if isinstance(config, dict) else text
        if payload is None:
            return {"ok": False, "errors": ["provide `config` (dict) or `text`"],
                    "warnings": []}
        return creator.validate_output(payload)
    return _run(work)


# ── on-demand analysis (#8: telemetry + AI overview for a FINISHED loop) ───────
_FINISHED_STATES = frozenset({"finished", "complete", "stopped", "error", "needs_owner"})


def _percentiles(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    n = len(s)
    return {"n": n, "min": round(s[0], 1), "max": round(s[-1], 1),
            "mean": round(sum(s) / n, 1), "median": round(s[n // 2], 1)}


def _ai_overview(name: str, tele: dict, notes: list[dict]) -> str:
    """Heuristic (deterministic) narrative from telemetry + report notes. This is
    the honest engine-side floor — no token/cost (otel is broken fleet-wide); a
    calling agent can produce a richer narrative from the returned corpus."""
    lines = [f"Loop {name!r} ended '{tele['ended']}' after {tele['turns_used']} "
             f"main-phase + {tele['winddown_turns']} wind-down turns "
             f"across {len(tele['per_agent'])} agents."]
    if tele.get("wall_clock_min"):
        lines.append(f"Wall-clock span ≈ {tele['wall_clock_min']} min.")
    if tele["per_agent"]:
        busiest = max(tele["per_agent"].items(), key=lambda kv: kv[1]["turns"])
        lines.append(f"Most active: {busiest[0]} ({busiest[1]['turns']} turns).")
    if tele["retired"]:
        lines.append("Retired: " + ", ".join(tele["retired"]) + ".")
    if tele["parallel_groups"] or tele["subloops"]:
        lines.append(f"Structure: {tele['parallel_groups']} parallel dispatch(es), "
                     f"{tele['subloops']} sub-loop(s).")
    tail = [n["note"] for n in notes if n.get("note")][-4:]
    if tail:
        lines.append("Recent notes: " + " | ".join(tail))
    return " ".join(lines)


@mcp.tool()
def loop_analyze(name: str, owner: str = "") -> dict[str, Any]:
    """ON-DEMAND analysis of a FINISHED loop (never automatic). Returns pure
    telemetry from status.jsonl timing + run state, a corpus of the loop's own
    notes/prompts for a richer read, and a heuristic AI overview. No token/cost
    numbers (otel export is broken fleet-wide) — this leans on timing + state.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        run = _read_json(_run_path(name))
        if run is None and _read_json(_config_path(name)) is None:
            return {"error": f"loop {name!r} not found"}
        state = (run or {}).get("state", "saved")
        if state not in _FINISHED_STATES:
            return {"error": f"analysis is on-demand for FINISHED loops only "
                             f"(loop {name!r} is {state!r})", "state": state}
        entries = _read_jsonl(report.status_log(name))
        status_reports = [e for e in entries if e.get("kind") != "machinery"]

        # Telemetry is derived from the run's AUTHORITATIVE per-turn event log
        # (run.json result.events, written by the engine for every turn). In
        # production the agents ALSO write status.jsonl, but result.events is
        # always present and is the ground truth for counts + timing.
        from mcp_loops.schema import STATUS_VOCAB
        turn_statuses = {s for v in STATUS_VOCAB.values() for s in v} | {"timeout"}
        result = (run or {}).get("result") or {}
        events = result.get("events") or []
        cfg_steps = set((_read_json(_config_path(name)) or {}).get("steps") or {})

        # normalize turn records to {agent, status, ts, note}
        turns = [{"agent": e.get("agent"), "status": e.get("status"),
                  "ts": e.get("t"), "note": e.get("note", "")}
                 for e in events
                 if e.get("status") in turn_statuses and e.get("agent") in cfg_steps]
        if not turns:                              # fall back to agent-written reports
            turns = [{"agent": e.get("agent"), "status": e.get("status"),
                      "ts": e.get("ts"), "note": e.get("note", "")}
                     for e in status_reports]

        per_agent: dict[str, dict] = {}
        ts_by_agent: dict[str, list[float]] = {}
        all_ts: list[float] = []
        for e in turns:
            a = e.get("agent")
            ts = e.get("ts")
            if not isinstance(a, str):
                continue
            pa = per_agent.setdefault(a, {"turns": 0, "statuses": {}})
            pa["turns"] += 1
            st = e.get("status", "?")
            pa["statuses"][st] = pa["statuses"].get(st, 0) + 1
            if isinstance(ts, (int, float)):
                ts_by_agent.setdefault(a, []).append(float(ts))
                all_ts.append(float(ts))

        # turn durations ≈ gaps between successive reports (overall + per agent)
        def _gaps(seq: list[float]) -> list[float]:
            s = sorted(seq)
            return [round((b - a) / 60.0, 2) for a, b in zip(s, s[1:])]

        # structural counts come from the event log (machinery events too)
        machinery = [e for e in events
                     if e.get("status") in ("parallel", "subloop_start")]
        retired = result.get("retired") or sorted(
            {e.get("agent") for e in events if e.get("status") == "retired"} - {None})
        tele = {
            "ended": result.get("ended", state),
            "turns_used": result.get("turns_used", len(turns)),
            "winddown_turns": result.get("winddown_turns", 0),
            "retired": retired,
            "per_agent": per_agent,
            "turn_gap_min": _percentiles(_gaps(all_ts)),
            "per_agent_gap_min": {a: _percentiles(_gaps(v)) for a, v in ts_by_agent.items()},
            "parallel_groups": sum(1 for m in machinery if m.get("status") == "parallel"),
            "subloops": sum(1 for m in machinery if m.get("status") == "subloop_start"),
            "distinct_agents": len(per_agent),
            "wall_clock_min": round((max(all_ts) - min(all_ts)) / 60.0, 1) if len(all_ts) > 1 else 0,
            "report_count": len(turns),
            "note": "session/token/cost counts are not exported (otel broken fleet-wide); "
                    "timing is inter-report wall-clock, an approximation of turn duration.",
        }

        # corpus: the loop's own words, for a richer (agent-generated) overview
        pdir = os.path.join(report.status_dir(name), "prompts")
        prompt_files = sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []
        note_src = status_reports if any(e.get("note") for e in status_reports) else turns
        corpus = {
            "notes": [{"agent": e.get("agent"), "status": e.get("status"),
                       "note": e.get("note")} for e in note_src if e.get("note")],
            "prompt_files": prompt_files,
            "goal": (_read_json(_config_path(name)) or {}).get("goal", ""),
        }
        return {"name": name, "state": state, "telemetry": tele, "corpus": corpus,
                "ai_overview": _ai_overview(name, tele, corpus["notes"])}
    return _run(work)


def _goodness_result_paths(name: str, cfg: dict) -> list[str]:
    """Every EXISTING result markdown the engine writes for loop ``name`` — the
    loop's own output folder (``report.output_dir``) plus, for a loop bound to a
    project, each of its agents' standalone runs under the project's resolved
    output root (``<outputPath>/standalone-<agent>/result.md``, exactly where
    :func:`loop_run_agent_standalone` writes). De-duplicated on the real path so
    one file is never filled twice. Read-only."""
    cands = [os.path.join(report.output_dir(name), "result.md")]
    pid = (cfg or {}).get("projectId") or (cfg or {}).get("productId")
    praw = _read_json(_project_record_path(pid)) if isinstance(pid, str) and pid else None
    if praw is not None:
        out_root = products.resolve_paths(
            praw, products_root=_products_root(),
            output_base=_output_base())["outputPath"]
        for agent_id in sorted(_agent_ids_in_config(cfg)):
            cands.append(os.path.join(out_root, f"standalone-{agent_id}", "result.md"))
    seen: set[str] = set()
    out: list[str] = []
    for path in cands:
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(path):
            continue
        seen.add(real)
        out.append(path)
    return out


def _goodness(name: str, *, persist: bool) -> dict[str, Any]:
    """The ``loop_goodness`` payload. ``persist=False`` is READ-ONLY: a cached
    analyst record is served as-is, otherwise it is computed in memory and
    nothing is written. ``persist=True`` (explicit re-score / run finish)
    recomputes, saves ``goodness.json`` and fills the reserved Goodness table of
    every result markdown :func:`_goodness_result_paths` finds."""
    from mcp_loops import goodness

    bad = _check_name(name)
    if bad:
        return bad
    cfg = _read_json(_config_path(name))
    if cfg is None:
        return {"error": f"loop {name!r} not found"}
    base = report.status_dir(name)
    run = _read_json(_run_path(name)) or {}
    started = run.get("started")
    key = goodness.run_key(started)
    card = _resolution_card_for(name, cfg)
    owner = (card.get("disposition") or {}).get("current")
    rateable = bool((card.get("disposition") or {}).get("offered"))
    analyst = None if persist else goodness.load(base)["runs"].get(key)
    reason = None
    if analyst is None:
        analysis = loop_analyze(name)
        if analysis.get("error"):
            reason = analysis["error"]
        else:
            analyst = goodness.score_run(analysis, card)
            if analyst is None:
                reason = "no outcome to judge yet"
            else:
                analyst["runStarted"] = started
                if persist:
                    goodness.save(base, key, analyst)
    out = goodness.view(name, run_started=started, owner=owner,
                        analyst=analyst, analyst_reason=reason,
                        rateable=rateable)
    if not persist:
        return out
    filled: list[str] = []
    for result_md in _goodness_result_paths(name, cfg):
        with open(result_md, encoding="utf-8") as fh:
            text = fh.read()
        new = standalone.fill_goodness_table(text, out["table"])
        if new != text:
            with open(result_md, "w", encoding="utf-8") as fh:
                fh.write(new)
        filled.append(result_md)
    if filled:
        out["resultFilled"] = filled
    return out


def _maybe_persist_goodness(name: str) -> None:
    """Run-finish hook: score + persist the finished run's analyst record and
    fill its result markdown(s), so a later plain GET serves the cache and
    writes nothing. Best-effort — never breaks teardown."""
    try:
        _goodness(name, persist=True)
    except Exception as e:  # noqa: BLE001 — enrichment only
        print(f"[loops] goodness persist failed for {name!r}: {type(e).__name__}: {e}")


@mcp.tool()
def loop_goodness(name: str, refresh: bool = False, owner: str = "") -> dict[str, Any]:
    """The DUAL goodness score of a loop's latest run (Run observability §1):
    the owner's good/ok/bad rating BESIDE an independent loop-analyst score —
    two separate fields, never blended::

        {name, runStarted, owner: {verb, note, at, source} | null,
         analyst: {grade, score, rationale, factors, verdict, analyst, at} | null,
         pending: {owner, analyst}, ownerRateable, table: {owner, analyst},
         analystReason?, resultFilled?: [path]}

    The analyst pass reuses :func:`loop_analyze` telemetry + the computed
    Resolution verdict (see :mod:`mcp_loops.goodness`); it runs only over a
    FINISHED run. A plain read (``refresh=False``) is READ-ONLY — it serves the
    per-run record persisted in ``<loop data>/goodness.json`` or, when absent,
    computes one in memory without writing anything. Persisting (and filling
    the reserved Goodness table of the run's result markdown — the loop output
    folder and, for a project-bound loop, its agents' standalone
    ``result.md``) happens only on ``refresh=True`` and at run finish.

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    return _run(lambda: _owner_refusal_for(name, owner)
                or _goodness(name, persist=bool(refresh)))


@mcp.tool()
def loop_thoughtlog(name: str, agent: str = "", limit: int = 100,
                    owner: str = "") -> dict[str, Any]:
    """The loop's THOUGHT-LOG (Run observability §2): one capped line per agent
    turn — the reasoning gist each agent reported (``--gist`` on
    ``mcp_loops.report``, else its report note). Never a transcript: rows are
    hard-capped at ``thoughtlog.GIST_CAP`` chars and no turn I/O is stored::

        {name, cap, count, agents: {agent: turns}, entries: [
            {ts, loop, agent, turn, status, gist, source, truncated}]}

    ``agent`` filters to one agent; ``limit`` keeps the newest N (oldest-first,
    max ``thoughtlog.READ_CAP``).

    ``owner`` (optional, Phase 5 seam) refuses a cross-owner read
    (``{error, refused:"cross_owner"}``, no data)."""
    from mcp_loops import thoughtlog

    def work():
        refused = _owner_refusal_for(name, owner)
        if refused:
            return refused
        bad = _check_name(name)
        if bad:
            return bad
        if _read_json(_config_path(name)) is None:
            return {"error": f"loop {name!r} not found"}
        rows = thoughtlog.read(report.status_dir(name),
                               agent=(agent or None), limit=limit)
        return thoughtlog.view(name, rows)
    return _run(work)


# ── cross-loop analytics (agent + loop rollups across the whole fleet) ────────
# All derived from status.jsonl (the same source the dashboard tails) so that
# SUB-LOOP child agents — which live only in the parent's status.jsonl, never in
# run.json result.events — are included. Honest caveats: no token/cost (otel
# broken fleet-wide); "duration" is inter-report wall-clock (a cadence proxy);
# "retries" = `timeout` turns (each triggered a guardian re-nudge/service cycle).
def _loop_dirs() -> tuple[str, list[str]]:
    root = os.path.dirname(report.status_dir("_"))
    names = [e for e in (sorted(os.listdir(root)) if os.path.isdir(root) else [])
             if not e.startswith("_") and _read_json(_config_path(e)) is not None]
    return root, names


def _agent_turns(name: str) -> tuple[list[dict], list[dict]]:
    """(turns, machinery) from a loop's status.jsonl — turns are non-machinery
    events with a real agent id, in file order."""
    events = _read_jsonl(report.status_log(name))
    turns = [e for e in events
             if e.get("kind") != "machinery" and isinstance(e.get("agent"), str)]
    machinery = [e for e in events if e.get("kind") == "machinery"]
    return turns, machinery


def _mean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def _run_timeouts(name: str) -> dict[str, int]:
    """Per-agent `timeout` count for a loop, from run.json result.events — the
    engine's authoritative turn log. Timeouts are the retry signal and are NOT
    mirrored into status.jsonl, so they must be read from here. (Sub-loop child
    timeouts aren't persisted to any run.json and are simply unavailable.)"""
    run = _read_json(_run_path(name)) or {}
    out: dict[str, int] = {}
    for e in (run.get("result") or {}).get("events") or []:
        if e.get("status") == "timeout" and isinstance(e.get("agent"), str):
            out[e["agent"]] = out.get(e["agent"], 0) + 1
    return out


@mcp.tool()
def loop_fleet_analytics(include_archived: bool = True) -> dict[str, Any]:
    """Cross-loop telemetry hub. Returns per-AGENT rollups across EVERY loop
    (turns, #loops appeared in, status mix, avg turn cadence in min, retries =
    timeout recoveries, retry_rate) and a per-LOOP summary (turns, agents,
    retries, parallel/subloop structure, wall-clock). Agents that are also saved
    in the registry are flagged `saved:true`. Honest: no token/cost (otel broken);
    timing is inter-report wall-clock; retries counts `timeout` turns."""
    def work():
        _root, names = _loop_dirs()
        saved_agents = {rec_id for rec_id in
                        [fn[:-5] for fn in (sorted(os.listdir(_registry_dir("agent")))
                                            if os.path.isdir(_registry_dir("agent")) else [])
                         if fn.endswith(".json")]}
        per_agent: dict[str, dict] = {}
        loops_out: list[dict] = []
        # agent → (updated, loop) of the NEWEST loop whose config carries its
        # step-def: the `from_loop` a one-click "Save to library" lifts it from
        # via loop_save_agent (loopyard-follow-up-1790089838).
        step_home: dict[str, tuple[float, str]] = {}
        for name in names:
            run = _read_json(_run_path(name)) or {}
            if run.get("archived") and not include_archived:
                continue
            cfg = _read_json(_config_path(name)) or {}
            upd = run.get("updated") or run.get("started") or 0
            upd = float(upd) if isinstance(upd, (int, float)) else 0.0
            for sid in _agent_ids_in_config(cfg):
                if sid not in step_home or upd >= step_home[sid][0]:
                    step_home[sid] = (upd, name)
            turns, machinery = _agent_turns(name)
            ts_all = [float(e["ts"]) for e in turns if isinstance(e.get("ts"), (int, float))]
            loop_agents: dict[str, int] = {}
            loop_retries = 0
            loop_continues = 0
            for e in turns:
                a = e["agent"]; st = e.get("status", "?"); ts = e.get("ts")
                loop_agents[a] = loop_agents.get(a, 0) + 1
                if st == "timeout":
                    loop_retries += 1
                if st in ("work_remaining", "needs_work"):
                    loop_continues += 1
                pa = per_agent.setdefault(a, {"agent": a, "turns": 0, "retries": 0,
                                              "loops": {}, "statuses": {}, "_ts": {},
                                              "last_ts": 0.0})
                pa["turns"] += 1
                pa["statuses"][st] = pa["statuses"].get(st, 0) + 1
                pa["loops"][name] = pa["loops"].get(name, 0) + 1
                if st == "timeout":
                    pa["retries"] += 1
                if isinstance(ts, (int, float)):
                    pa["_ts"].setdefault(name, []).append(float(ts))
                    pa["last_ts"] = max(pa["last_ts"], float(ts))
            # merge retry (timeout) counts from the engine's authoritative log
            for a, c in _run_timeouts(name).items():
                loop_agents.setdefault(a, 0)      # count timeout-only agents in this loop
                loop_retries += c
                pa = per_agent.setdefault(a, {"agent": a, "turns": 0, "retries": 0,
                                              "loops": {}, "statuses": {}, "_ts": {},
                                              "last_ts": 0.0})
                pa["retries"] += c
                pa["loops"].setdefault(name, 0)
            loops_out.append({
                # every analytics loop is scanned from THIS box's data dir, so its
                # origin is local (loopyard-bug-1790089839: rows carry the origin
                # so the Library links /loops/<origin>/<name>, not a guess).
                "name": name, "origin": origins.LOCAL_ORIGIN_ID,
                "state": run.get("state", "saved"),
                "archived": bool(run.get("archived")),
                "updated": run.get("updated") or run.get("started"),
                "turns": len(turns), "agents": len(loop_agents), "retries": loop_retries,
                "continues": loop_continues,
                "parallel_groups": sum(1 for m in machinery if m.get("status") == "parallel"),
                "subloops": sum(1 for m in machinery if m.get("status") == "subloop_start"),
                "wall_clock_min": round((max(ts_all) - min(ts_all)) / 60.0, 1) if len(ts_all) > 1 else 0,
                "goal": (cfg.get("goal") or "")[:160],
            })
        agents_out = []
        for a, pa in per_agent.items():
            gaps: list[float] = []
            for tslist in pa["_ts"].values():
                s = sorted(tslist)
                gaps += [(b - x) / 60.0 for x, b in zip(s, s[1:])]
            continues = pa["statuses"].get("work_remaining", 0) + pa["statuses"].get("needs_work", 0)
            agents_out.append({
                "agent": a, "turns": pa["turns"], "retries": pa["retries"],
                "continues": continues,
                "loops": sorted(pa["loops"].keys()), "loop_count": len(pa["loops"]),
                "loop_origins": {l: origins.LOCAL_ORIGIN_ID for l in sorted(pa["loops"])},
                "statuses": pa["statuses"], "avg_gap_min": _mean(gaps),
                "retry_rate": round(pa["retries"] / pa["turns"], 3) if pa["turns"] else 0,
                "continue_rate": round(continues / pa["turns"], 3) if pa["turns"] else 0,
                "last_ts": pa["last_ts"] or None, "saved": a in saved_agents,
                # ran but never materialized into the registry: the Library
                # flags it and offers "Save to library" from `save_from` (None
                # = no step-def in any config, e.g. a status-only sub-loop child).
                # Engine-internal pseudo-agents ('__service') are never roles.
                "ad_hoc": a not in saved_agents and not a.startswith("__"),
                "save_from": (step_home[a][1] if a in step_home and a not in saved_agents
                              else None),
            })
        agents_out.sort(key=lambda x: (-x["turns"], x["agent"]))
        loops_out.sort(key=lambda x: (x.get("updated") or 0), reverse=True)
        return {"agents": agents_out, "loops": loops_out,
                "totals": {"agents": len(agents_out), "loops": len(loops_out),
                           "turns": sum(a["turns"] for a in agents_out),
                           "retries": sum(a["retries"] for a in agents_out),
                           "ad_hoc": sum(1 for a in agents_out if a["ad_hoc"])},
                "note": "no token/cost (otel broken fleet-wide); duration = inter-report "
                        "wall-clock (cadence proxy); retries = timeout recoveries."}
    return _run(work)


# ── Library quality (owner rating BESIDE analyst score, per loop + per agent) ──
QUALITY_COMPUTE_CAP = 20


def _step_pins(cfg: dict) -> dict[str, list[int]]:
    """``{agent: [registry versions]}`` from every step's ``agentPin`` (nested
    sub-loops included)."""
    out: dict[str, set] = {}

    def _walk(steps: dict) -> None:
        for sid, sdef in (steps or {}).items():
            if not isinstance(sid, str) or not isinstance(sdef, dict):
                continue
            if sdef.get("type") == "loop":
                _walk((sdef.get("loop") or {}).get("steps") or {})
                continue
            pin = sdef.get("agentPin")
            if isinstance(pin, dict) and isinstance(pin.get("version"), int):
                out.setdefault(sid, set()).add(pin["version"])

    if isinstance(cfg, dict):
        _walk(cfg.get("steps") or {})
    return {k: sorted(v) for k, v in out.items()}


def _run_verdicts(rows: list[dict], started: Any) -> dict[str, dict]:
    """``run_key → current owner verdict`` for every run the log rated. Rows
    with no ``runStarted`` belong to the current run (as current_disposition
    treats them)."""
    from mcp_loops import goodness
    groups: dict[str, list[dict]] = {}
    cur = goodness.run_key(started)
    for r in rows:
        rs = r.get("runStarted")
        key = goodness.run_key(rs) if isinstance(rs, (int, float)) else cur
        groups.setdefault(key, []).append(r)
    out: dict[str, dict] = {}
    for key, grp in groups.items():
        try:
            rs = float(key)
        except ValueError:
            rs = None
        v = resolution.current_disposition(grp, run_started=rs)
        if v:
            out[key] = v
    return out


def _quality_loop_facts(root: str, name: str, *, local: bool) -> Optional[dict]:
    """Raw on-disk facts for one loop under ``root`` (local data dir or a
    mirror). Read-only."""
    from mcp_loops import goodness
    d = os.path.join(root, name)
    cfg = _read_json(os.path.join(d, "config.json"))
    if cfg is None:
        return None
    run = _read_json(os.path.join(d, "run.json")) or {}
    started = run.get("started")
    per_agent: dict[str, dict] = {}
    turns = 0
    for e in _read_jsonl(os.path.join(d, "status.jsonl")):
        if e.get("kind") == "machinery" or not isinstance(e.get("agent"), str):
            continue
        turns += 1
        per_agent.setdefault(e["agent"], {"turns": 0, "retries": 0})["turns"] += 1
    retries = 0
    for e in (run.get("result") or {}).get("events") or []:
        if isinstance(e, dict) and e.get("status") == "timeout" and isinstance(e.get("agent"), str):
            per_agent.setdefault(e["agent"], {"turns": 0, "retries": 0})["retries"] += 1
            retries += 1
    rows = ([e for e in _read_jsonl(os.path.join(d, "dispositions.jsonl"))
             if isinstance(e, dict)] if local else [])
    return {"cfg": cfg, "run": run, "started": started, "per_agent": per_agent,
            "turns": turns, "retries": retries,
            "verdicts": _run_verdicts(rows, started),
            "cache": goodness.load(d)["runs"], "dir": d}


def _quality_compute(name: str, cfg: dict, started: Any, status_dir: str) -> Optional[dict]:
    """Score a finished run that has no cached analyst record and cache it in
    ``goodness.json`` (never touches result markdown or ratings)."""
    from mcp_loops import goodness
    analysis = loop_analyze(name)
    if not isinstance(analysis, dict) or analysis.get("error"):
        return None
    rec = goodness.score_run(analysis, _resolution_card_for(name, cfg))
    if rec is None:
        return None
    rec["runStarted"] = started
    goodness.save(status_dir, goodness.run_key(started), rec)
    return rec


@mcp.tool()
def loop_quality(days: int = 30, project: str = "", compute: int = 5,
                 origin: str = origins.LOCAL_ORIGIN_ID) -> dict[str, Any]:
    """The Library's quality rollup — agents AND loops, each with the owner's
    rating and the analyst's score side by side (NEVER averaged together)::

        {ok, origin, project, days, since, computeCap, computed, pending,
         loops: [{name, project, state, archived, startedTs, lastTs, goal,
                  agents, turns, retries, perAgent, pins,
                  owner: {verb, note, at} | null,
                  analyst: {score, grade, verdict, rationale, at} | null,
                  analystPending, analystReason?, runs: [{runStarted, score,
                  grade, owner}], needsAttention, attentionReason?}],
         agents: [{agent, loops, loopCount, turns, retries, retryRate,
                   avgScore, scored, owner: {good, ok, bad}, trend, trendDelta,
                   history: [{ts, score, loop}], best, worst, saved, favorite,
                   versions, lastTs, needsAttention, attentionReason?}]}

    ``days`` keeps loops active in that window (0 = all time). ``project``
    scopes like the web switcher ('' all, ``__unattributed__`` = none).
    Analyst scores come from each loop's cached ``goodness.json``; a FINISHED
    latest run with no cache is scored and cached (goodness.json only) for at
    most ``compute`` loops per call (newest first, capped at
    ``QUALITY_COMPUTE_CAP``) — the rest come back ``analystPending``. Never
    writes ratings or result files and never starts anything. A mirror origin
    is read as-is: no ratings (they're local-only) and no scoring."""
    from mcp_loops import goodness, quality

    def work():
        local = origin == origins.LOCAL_ORIGIN_ID
        root = (os.path.dirname(report.status_dir("_")) if local
                else _data_root_for(origin))
        if root is None:
            return {"ok": False,
                    "error": f"unknown origin {origin!r} (not visible from this box)"}
        want = project.strip() if isinstance(project, str) else ""
        n_days = max(0, int(days or 0))
        since = time.time() - n_days * 86400 if n_days else None
        budget = max(0, min(QUALITY_COMPUTE_CAP, int(compute or 0))) if local else 0
        facts: list[tuple[str, dict]] = []
        for name in (sorted(os.listdir(root)) if os.path.isdir(root) else []):
            if name.startswith("_") or not _NAME_RE.match(name):
                continue
            try:
                f = _quality_loop_facts(root, name, local=local)
            except Exception:  # noqa: BLE001 — one bad loop never sinks the rollup
                f = None
            if f is None:
                continue
            f["project"] = schema.resolve_project(f["cfg"], loop_name=name)
            if not quality.in_scope(f["project"], want):
                continue
            last = f["run"].get("updated") or f["started"]
            if since is not None and not (isinstance(last, (int, float)) and last >= since):
                continue
            f["lastTs"] = last
            facts.append((name, f))
        facts.sort(key=lambda nf: nf[1]["lastTs"] or 0, reverse=True)
        computed = pending = 0
        loops_out: list[dict] = []
        for name, f in facts:
            key = goodness.run_key(f["started"])
            state = f["run"].get("state", "saved")
            analyst = f["cache"].get(key)
            reason = None
            waiting = False
            if analyst is None:
                if state not in _FINISHED_STATES:
                    reason = "running" if f["started"] else "not run yet"
                elif budget > 0:
                    budget -= 1
                    try:
                        analyst = _quality_compute(name, f["cfg"], f["started"], f["dir"])
                    except Exception:  # noqa: BLE001
                        analyst = None
                    if analyst is not None:
                        computed += 1
                        f["cache"][key] = analyst
                    else:
                        reason = "no outcome to judge yet"
                else:
                    waiting = True
                    pending += 1
                    reason = "not scored yet"
            agent_ids = set(f["per_agent"]) | _agent_ids_in_config(f["cfg"])
            loops_out.append(quality.loop_row(
                name=name, project=f["project"], state=state,
                archived=bool(f["run"].get("archived")), started=f["started"],
                updated=f["lastTs"], goal=f["cfg"].get("goal") or "",
                agents=agent_ids, turns=f["turns"], retries=f["retries"],
                per_agent=f["per_agent"], pins=_step_pins(f["cfg"]),
                owner=f["verdicts"].get(key), analyst=analyst,
                analyst_pending=waiting, analyst_reason=reason,
                history=quality.run_history(f["cache"], f["verdicts"])))
        registry: dict[str, dict] = {}
        rdir = _registry_dir("agent")
        for fn in (sorted(os.listdir(rdir)) if local and os.path.isdir(rdir) else []):
            if fn.endswith(".json"):
                rec = _read_json(os.path.join(rdir, fn)) or {}
                registry[rec.get("id") or fn[:-5]] = {"favorite": bool(rec.get("favorite"))}
        return {"ok": True, "origin": origin, "project": want or None,
                "days": n_days, "since": since, "computeCap": QUALITY_COMPUTE_CAP,
                "computed": computed, "pending": pending,
                "loops": loops_out,
                "agents": quality.agent_rows(loops_out, registry=registry, since=since)}
    return _run(work)


@mcp.tool()
def loop_agent_detail(agent_id: str, owner: str = "") -> dict[str, Any]:
    """Every turn a named agent took across ALL loops: a per-loop breakdown
    (turns, retries, avg cadence) plus the full chronological turn list (loop,
    seq, status, note, ts, gap_min). `seq` matches the on-disk framed-prompt file
    prompts/<agent>-NNN.txt within that loop, so a caller can drill into any turn.

    ``owner`` (optional, Phase 5 seam) scopes "ALL loops" to that owner's loops
    (an owner-less loop is the local default owner's)."""
    want = (owner or "").strip()

    def work():
        bad = _check_name(agent_id)
        if bad:
            return bad
        _root, names = _loop_dirs()
        per_loop: list[dict] = []
        all_turns: list[dict] = []
        for name in names:
            if want and schema.resolve_owner(_read_json(_config_path(name))) != want:
                continue
            events = _read_jsonl(report.status_log(name))
            seq = 0; prev: Optional[float] = None; rows: list[dict] = []
            for e in events:
                if e.get("kind") == "machinery" or e.get("agent") != agent_id:
                    continue
                seq += 1
                ts = e.get("ts")
                gap = None
                if isinstance(ts, (int, float)) and prev is not None:
                    gap = round((float(ts) - prev) / 60.0, 2)
                if isinstance(ts, (int, float)):
                    prev = float(ts)
                rows.append({"loop": name, "seq": seq, "status": e.get("status"),
                             "note": (e.get("note") or "")[:240], "ts": ts, "gap_min": gap})
            retries = _run_timeouts(name).get(agent_id, 0)
            if rows or retries:
                gaps = [r["gap_min"] for r in rows if r["gap_min"] is not None]
                per_loop.append({"loop": name, "turns": len(rows), "retries": retries,
                                 "continues": sum(1 for r in rows
                                                  if r["status"] in ("work_remaining", "needs_work")),
                                 "avg_gap_min": _mean(gaps)})
                all_turns += rows
        if not per_loop:
            return {"error": f"no turns found for agent {agent_id!r} in any loop"}
        all_turns.sort(key=lambda r: r["ts"] or 0)
        gaps = [r["gap_min"] for r in all_turns if r["gap_min"] is not None]
        statuses: dict[str, int] = {}
        for r in all_turns:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        totals = {"turns": len(all_turns), "loops": len(per_loop),
                  "retries": sum(p["retries"] for p in per_loop),
                  "continues": statuses.get("work_remaining", 0) + statuses.get("needs_work", 0),
                  "avg_gap_min": _mean(gaps), "statuses": statuses}
        return {"agent": agent_id, "per_loop": per_loop, "turns": all_turns,
                "totals": totals}
    return _run(work)


def _find_step(steps: dict, agent_id: str) -> Optional[dict]:
    """Locate an agent's step-def anywhere in a loop config — top-level or inside
    a nested sub-loop (steps[x].loop.steps[...])."""
    for k, v in (steps or {}).items():
        if k == agent_id and isinstance(v, dict):
            return v
        if isinstance(v, dict) and isinstance(v.get("loop"), dict):
            found = _find_step(v["loop"].get("steps") or {}, agent_id)
            if found is not None:
                return found
    return None


@mcp.tool()
def loop_save_agent(agent_id: str, from_loop: str, note: str = "") -> dict[str, Any]:
    """Save an agent to the registry by lifting its step-def from an existing
    loop's config — searches top-level AND nested sub-loop steps. Returns
    {ok, id} or {"error": ...}."""
    def work():
        cfg = _read_json(_config_path(from_loop))
        if cfg is None:
            return {"error": f"loop {from_loop!r} not found"}
        step = _find_step(cfg.get("steps") or {}, agent_id)
        if not isinstance(step, dict):
            return {"error": f"agent {agent_id!r} has no step-def in loop {from_loop!r}"}
        return loop_registry_save_agent(agent_id, step,
                                        note or f"from {from_loop}")
    return _run(work)


# ── agent auto-materialize + favorites (agent registry v2, north star §4) ─────
# Every agent SEEN in any loop is materialized into the registry BY DEFAULT so
# its detail view always works (never gated behind a manual save). Favorites
# are a curated shelf on top of that — not a functionality gate.

def _find_step_in_any_loop(agent_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Search every saved loop's config for a step-def matching ``agent_id``
    (top-level or nested). Returns ``(step, loop_name)`` for the FIRST hit or
    ``(None, None)`` if the id has no step-def anywhere yet."""
    _root, names = _loop_dirs()
    for name in names:
        cfg = _read_json(_config_path(name))
        if not isinstance(cfg, dict):
            continue
        step = _find_step(cfg.get("steps") or {}, agent_id)
        if isinstance(step, dict):
            return step, name
    return None, None


def _agent_ids_in_config(cfg: dict) -> set[str]:
    """Every agent id in a loop config — top-level steps AND every nested
    sub-loop step. Used by loop_save to auto-materialize the registry so the
    agent nav has entries the moment a loop is created."""
    seen: set[str] = set()

    def _walk(steps: dict) -> None:
        for sid, sdef in (steps or {}).items():
            if not isinstance(sid, str) or not isinstance(sdef, dict):
                continue
            if sdef.get("type") == "loop":
                _walk((sdef.get("loop") or {}).get("steps") or {})
            else:
                seen.add(sid)

    if isinstance(cfg, dict):
        _walk(cfg.get("steps") or {})
    return seen


def _agent_ids_across_loops() -> set[str]:
    """Every agent id ever OBSERVED across every saved loop — from config step
    ids (including nested sub-loop steps) AND from status.jsonl reports (which
    catch runtime-only agents like sub-loop children)."""
    seen: set[str] = set()

    def _walk_steps(steps: dict) -> None:
        for sid, sdef in (steps or {}).items():
            if not isinstance(sid, str):
                continue
            if isinstance(sdef, dict) and sdef.get("type") == "loop":
                inner = sdef.get("loop") or {}
                _walk_steps(inner.get("steps") or {})
            elif isinstance(sdef, dict):
                seen.add(sid)

    _root, names = _loop_dirs()
    for name in names:
        cfg = _read_json(_config_path(name)) or {}
        _walk_steps(cfg.get("steps") or {})
        for evt in _read_jsonl(report.status_log(name)):
            a = evt.get("agent")
            if isinstance(a, str) and evt.get("kind") != "machinery":
                seen.add(a)
    return seen


def _materialize_one(agent_id: str, *, now: float) -> dict:
    """Ensure a registry record exists for ``agent_id``; return
    ``{ok, id, created, source, placeholder}``.

    - If a record already exists, it is left UNCHANGED (never overwrites a
      curated persona/goal with a stale step-def).
    - Otherwise lift the step from the first loop that has one; falling back to
      the placeholder auto-record if no step is visible anywhere yet.
    """
    bad = _check_name(agent_id)
    if bad:
        return {"error": bad["error"]}
    path = os.path.join(_registry_dir("agent"), f"{agent_id}.json")
    if os.path.exists(path):
        rec = _read_json(path) or {}
        m = agents.migrate_agent_record(rec, now=rec.get("saved") or 0.0)
        return {"ok": True, "id": agent_id, "created": False,
                "source": "existing",
                "placeholder": agents.is_placeholder_identity(m)}
    step, from_loop = _find_step_in_any_loop(agent_id)
    rec = agents.auto_agent_record_from_step(agent_id, step, now=now)
    if from_loop:
        rec["note"] = f"auto-materialized from loop {from_loop!r}"
    else:
        rec["note"] = "auto-materialized from status.jsonl (no step-def yet)"
    _write_json(path, rec)
    return {"ok": True, "id": agent_id, "created": True,
            "source": from_loop or "status_only",
            "placeholder": agents.is_placeholder_identity(rec)}


@mcp.tool()
def loop_agent_materialize(agent_id: str) -> dict[str, Any]:
    """Ensure an agent registry record exists for ``agent_id``. If one already
    exists, returns ``{ok, id, created:false, source:'existing', ...}``. Otherwise
    lifts the step-def from the first saved loop that has one (searching
    top-level + nested sub-loop steps) and creates a fresh v1 record with source
    ``auto``. If no step is visible anywhere yet (a sub-loop child only seen in
    status.jsonl), a placeholder record is written so the detail view still
    works — HONEST about being un-curated."""
    def work():
        return _materialize_one(agent_id, now=time.time())
    return _run(work)


@mcp.tool()
def loop_agents_materialize() -> dict[str, Any]:
    """Sweep every saved loop and ensure a registry record exists for EVERY
    agent id seen (in configs OR in status.jsonl). Idempotent — records that
    already exist are left untouched (never overwrites a curated persona/goal).
    Returns ``{ok, seen, created:[...], existing:[...]}``."""
    def work():
        now = time.time()
        seen = sorted(_agent_ids_across_loops())
        created: list[str] = []
        existing: list[str] = []
        for aid in seen:
            res = _materialize_one(aid, now=now)
            if res.get("error"):
                continue                    # skip invalid ids silently — logged?
            (created if res.get("created") else existing).append(aid)
        return {"ok": True, "seen": len(seen),
                "created": created, "existing": existing}
    return _run(work)


@mcp.tool()
def loop_agent_favorite(agent_id: str, favorite: bool = True) -> dict[str, Any]:
    """Toggle the FAVORITES flag on an agent (a curated shelf on top of the
    registry). Auto-materializes the record first if it doesn't exist yet.
    Returns ``{ok, id, favorite}``."""
    def work():
        mat = _materialize_one(agent_id, now=time.time())
        if mat.get("error"):
            return mat
        path = os.path.join(_registry_dir("agent"), f"{agent_id}.json")
        rec = _read_json(path)
        if rec is None:
            return {"error": f"no agent {agent_id!r} after materialize"}
        agents.set_favorite(rec, favorite)
        _write_json(path, rec)
        return {"ok": True, "id": agent_id, "favorite": bool(favorite)}
    return _run(work)


# ── finish-report (#11: on wind_down/complete, tell Tim how it went) ──────────
def _normalize_report_artifacts(raw: list, out_dir: str) -> list[str]:
    """F-A: clean the reported ``artifact=`` list for the finish-report's "what
    was produced" section. For each raw marker token: strip surrounding quotes and
    trailing sentence punctuation; resolve a relative path against the loop's
    output folder (absolute paths as-is); keep ONLY paths that actually exist
    (drop the envelope's ``exists:false`` equivalent — never name a file the user
    can't open); and dedupe by real path (so a token reported twice, or in two
    punctuation/relative forms, lists once). Returns the display strings (the
    cleaned tokens, order-preserving)."""
    seen: set[str] = set()
    out: list[str] = []
    for a in raw:
        if not isinstance(a, str):
            continue
        tok = a.strip().strip('"\'').rstrip(".,;:!?)]}")
        if not tok:
            continue
        resolved = tok if os.path.isabs(tok) else os.path.join(out_dir, tok)
        if not os.path.exists(resolved):
            continue
        real = os.path.realpath(resolved)
        if real in seen:
            continue
        seen.add(real)
        out.append(tok)
    return out


def _finish_report_where(name: str, cfg: dict) -> list[str]:
    """The "what did I get, and where is it?" section of the finish-report.

    P1 legibility ("give task → find result"): a first-time user opens the
    advertised output folder (``_output/<loop>/``) expecting THE result. Without
    this, they find only a generic "loop finished" line — agents committed code to
    the working repo and/or dropped files elsewhere, so the folder looks empty and
    reads as "nothing was produced." This scans the loop's own end-of-turn reports
    for the commit + any ``artifact=`` files the agents named, and states plainly
    that source edits live in the working repo (not this folder) — so the landing
    page always answers where the deliverable actually is. Best-effort: any read
    failure just omits the section rather than breaking the finish-report."""
    try:
        reports = [e for e in _read_jsonl(report.status_log(name))
                   if e.get("kind") != "machinery"]
        # P3a (no cross-run leak): status.jsonl accumulates across every run of a
        # loop name, and artifacts/last-note carry over. Scope to THIS run's
        # window — exactly as build_envelope does — so a re-run of the same name
        # never surfaces a PRIOR run's artifact/commit/last-note as "what THIS
        # run produced." Without this, `artifact=` markers (which accumulate) and
        # the last-note fallback (when this run has no fresh worker note) leak the
        # old run's deliverable into the very section meant to be the honest
        # answer to "what did I just get?"
        run = _read_json(os.path.join(report.status_dir(name), "run.json"))
        reports = envelope._reports_for_run(reports, run=run or {})
    except Exception:  # noqa: BLE001 — enrichment only, never fails the report
        reports = []
    if not reports:
        return []
    steps = (cfg or {}).get("steps") or {}
    manager_ids = frozenset(
        sid for sid, s in steps.items()
        if isinstance(s, dict) and s.get("role") == "manager")
    markers = envelope._scan_markers(reports, manager_ids=manager_ids)
    where: list[str] = []
    commit = markers.get("commit")
    if commit:
        where.append(f"- code changes: committed as `{commit}` in the working "
                     f"repo — inspect with `git show {commit}`.")
    # F-A: normalize the reported artifact list before it becomes a promise. A
    # `artifact=` marker captures a bare token, so it can carry trailing sentence
    # punctuation, name a path relative to the output folder, repeat, or point at
    # a file that was never actually written. List ONLY files that resolve+exist
    # (mirroring the envelope's honest `exists` contract) — a landing page that
    # names a file the user then can't open is worse than saying nothing.
    arts = _normalize_report_artifacts(markers.get("artifacts") or [],
                                       report.output_dir(name))
    if arts:
        where.append("- files the agents produced (in this output folder):")
        where += [f"    • {a}" for a in arts[:12]]
    # the last NON-manager note is the plainest "what got done" line (a manager
    # note is a decision — continue/complete — not the deliverable).
    last = next((e.get("note") for e in reversed(reports)
                 if e.get("note") and e.get("agent") not in manager_ids), None)
    if last:
        where.append(f"- last worker report: {str(last)[:200]}")
    if not where:
        return []
    where.append(
        f"- this folder ({report.output_dir(name)}) holds this finish-report plus "
        f"any files agents saved here; source-code edits live in the working repo "
        f"checkout, not in this folder.")
    return ["\n## What was produced & where to find it", *where]


def _compose_finish_report(name: str, cfg: dict, result, stopped: bool) -> str:
    ended = "stopped by owner" if stopped else getattr(result, "ended", "?")
    # §3.2 — the finish-report HEADLINE must be honest at its SOURCE. An errored
    # run is NOT a green "✅ … finished": that headline feeds the run summary (loop
    # card, Runs feed) AND the honest-failure block's reason line, so a green-check
    # "finished — error" would re-introduce the exact lie downstream. Pick the verb
    # from the real outcome; the success/stopped paths are unchanged (additive).
    if not stopped and getattr(result, "ended", None) == "error":
        head = f"⚠ loop {name!r} ended in an error — no result produced"
    else:
        head = f"✅ loop {name!r} finished — {ended}"
    lines = [head,
             f"turns: {getattr(result, 'turns_used', 0)} main + "
             f"{getattr(result, 'winddown_turns', 0)} wind-down"]
    retired = getattr(result, "retired", None)
    if retired:
        lines.append("retired: " + ", ".join(retired))
    if getattr(result, "error", None):
        lines.append("error: " + str(result.error))
    goal = (cfg or {}).get("goal", "")
    if goal:
        lines.append("\ngoal: " + (goal[:280] + "…" if len(goal) > 280 else goal))
    lines += _finish_report_where(name, cfg)
    return "\n".join(lines)


def _send_finish_report(name: str, cfg: dict, result, *, stopped: bool = False) -> dict:
    """On loop end, write a results doc and ping the owner on Telegram with the
    outcome (+ the rendered block-scheme, if present). Best-effort + logged."""
    text = _compose_finish_report(name, cfg, result, stopped)
    doc_path = _html_path(name)
    body = "# " + text + "\n"
    # B5: surface silent skips — a finish-report write that fails is exactly the
    # kind of thing the pilot hit ("looked for it there and found nothing"). Track
    # which paths landed vs were skipped (and why) instead of a bare `pass`, so a
    # consumer reading the run can SEE the doc's real disposition.
    written: list[str] = []
    skipped: list[dict] = []
    for md_path in _finish_report_paths(name):
        try:
            os.makedirs(os.path.dirname(md_path), exist_ok=True)
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(body)
            written.append(md_path)
        except Exception as e:  # noqa: BLE001 — best-effort, per path
            skipped.append({"path": md_path, "skipped": f"{type(e).__name__}: {e}"})
    doc = {"ok": bool(written), "written": written}
    if skipped:
        doc["skipped"] = skipped
    res = _tg_notify(text, doc_path=doc_path if os.path.exists(doc_path) else None,
                     caption=f"loop {name} — block-scheme")
    _update_run(name, finish_report={"text": text, "notify": res, "doc": doc})
    return res


# ── A10: read-only HTTP surface (landing + panel) ────────────────────────────
# The server historically answered ONLY /mcp — every plain GET 404'd, so a user
# (or a health check, or a browser) hitting the base URL got nothing back. These
# two GET-only routes give an at-a-glance identity + state view. No writes, no
# control, no secrets: pure file reads over the LOCAL data dir.

SERVICE_NAME = "mcp-loops"


def _panel_snapshot() -> dict[str, Any]:
    """The shared read-only view both routes render:
    ``{service, install_root, data_dir, loops:[{name,state,updated}],
    loops_count}``. Local origin only; best-effort per loop so the surface never
    500s on a half-written run.json."""
    data_dir = _local_mirror_path()
    loops: list[dict] = []
    try:
        entries = sorted(os.listdir(data_dir)) if os.path.isdir(data_dir) else []
    except OSError:
        entries = []
    for entry in entries:
        if entry.startswith("_"):
            continue                        # skip _registry and other private dirs
        if _read_json(os.path.join(data_dir, entry, "config.json")) is None:
            continue
        state = _read_json(os.path.join(data_dir, entry, "run.json")) or {}
        if state.get("archived"):
            continue
        loops.append({"name": entry,
                      "state": state.get("state", "saved"),
                      "updated": state.get("updated")})
    return {"service": SERVICE_NAME,
            "install_root": str(paths.install_root()),
            "data_dir": data_dir,
            "loops": loops,
            "loops_count": len(loops)}


def _render_panel_html(snap: dict) -> str:
    """A tiny self-contained read-only HTML page for the loops + their state.
    Every dynamic value is escaped — the panel takes no input and renders only
    on-disk names/states, but escape anyway (defence in depth)."""
    rows = []
    for lp in snap["loops"]:
        updated = lp.get("updated")
        updated_txt = escape(str(updated)) if updated else "—"
        rows.append(
            f"<tr><td>{escape(str(lp['name']))}</td>"
            f"<td>{escape(str(lp['state']))}</td>"
            f"<td>{updated_txt}</td></tr>")
    body = ("".join(rows) if rows
            else '<tr><td colspan="3"><em>no loops saved yet</em></td></tr>')
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escape(SERVICE_NAME)} — panel</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:2rem;color:#111}"
        "table{border-collapse:collapse;margin-top:1rem}"
        "th,td{border:1px solid #ccc;padding:.35rem .7rem;text-align:left}"
        "th{background:#f3f3f3}code{background:#f3f3f3;padding:.1rem .3rem}"
        ".muted{color:#666}</style></head><body>"
        f"<h1>{escape(SERVICE_NAME)}</h1>"
        f"<p class=\"muted\">install <code>{escape(snap['install_root'])}</code> · "
        f"data <code>{escape(snap['data_dir'])}</code> · "
        f"{snap['loops_count']} loop(s)</p>"
        "<table><thead><tr><th>loop</th><th>state</th><th>updated</th></tr>"
        f"</thead><tbody>{body}</tbody></table>"
        "<p class=\"muted\">read-only · GET only · no control here</p>"
        "</body></html>")


@mcp.custom_route("/api/version", methods=["GET"])
async def _version_api(request):
    """Expose the running engine identity for yard's same-install adopt guard."""
    from mcp_loops import _version
    from starlette.responses import JSONResponse
    return JSONResponse(_version.version_info(), headers={"Cache-Control": "no-cache"})


@mcp.custom_route("/", methods=["GET"])
async def _landing(request):  # noqa: ANN001 — starlette handler
    from starlette.responses import JSONResponse
    snap = _panel_snapshot()
    return JSONResponse({"service": snap["service"],
                         "install_root": snap["install_root"],
                         "data_dir": snap["data_dir"],
                         "loops_count": snap["loops_count"]})


@mcp.custom_route("/panel/", methods=["GET"])
async def _panel(request):  # noqa: ANN001 — starlette handler
    from starlette.responses import HTMLResponse
    return HTMLResponse(_render_panel_html(_panel_snapshot()))


# Same handler without the trailing slash, so both /panel and /panel/ answer.
mcp.custom_route("/panel", methods=["GET"])(_panel)


def _boot_paths_lines() -> list[str]:
    """R7: the startup line naming which tree runs and which install the env
    blesses, plus a warning when they differ (the check ``cli.py`` does too)."""
    root = str(paths.install_root())
    env = os.environ.get("LOOPYARD_INSTALL")
    lines = [f"mcp_loops: install_root={root} LOOPYARD_INSTALL={env or ''}\n"]
    if env and os.path.realpath(env) != os.path.realpath(root):
        lines.append(f"mcp_loops: WARNING LOOPYARD_INSTALL={env} != install_root "
                     f"{root}: setup scripts + projects.toml resolve against the "
                     f"env tree, not the code that is running\n")
    return lines


def main() -> None:
    """Run mcp-loops over streamable-HTTP, bound to 127.0.0.1.

    G2.3: if opted in (``LOOPYARD_ORIGIN_AGENT``), bring this box up as a LIVE
    enrolled origin over the protocol first, so loop_start routes through the
    enrolled origin (G2.2) and the dashboard sees the VPS as a live origin — not
    the hardcoded LOCAL special-case. Fail-soft: a bring-up error never stops the
    server from serving; loop_start just stays on the local path."""
    for line in _boot_paths_lines():
        sys.stderr.write(line)
    # A2: sweep runs whose runtime died with a prior server process so they are
    # honestly terminal (wedged) instead of eternally 'running'. Fail-soft.
    try:
        swept = sweep_wedged_runs()
        if swept:
            sys.stderr.write(
                f"mcp_loops: swept {len(swept)} wedged run(s) on start: "
                f"{', '.join(swept)}\n")
    except Exception:  # noqa: BLE001 — a sweep hiccup must never block serving
        pass
    try:
        from mcp_loops import origin_service
        origin_service.maybe_start()
    except Exception:  # noqa: BLE001 — the agent is never allowed to block serving
        pass
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
