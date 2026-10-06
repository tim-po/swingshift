"""Session manager — list, pause, resume, spawn Claude tmux sessions.

Each function is a pure worker action callable. The worker runs as almdudleer
and has access to the user's tmux server via the default socket.

Session ID (SID) format: ``S-<user>-<window>-p<pane_id_no_pct>``
  e.g. ``S-almdudleer-spec5-smoke-p2``
"""
from __future__ import annotations

import glob
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PaneInfo:
    pane_id: str      # e.g. %2
    window: str
    pid: str
    cwd: str
    command: str


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper so tests can monkeypatch subprocess.run."""
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


# ---------------------------------------------------------------------------
# CAP-1: multi-CLI runtime — which coding CLI a spawned pane launches, and how
# its per-session model is applied.
# ---------------------------------------------------------------------------

from mcp_loops import runtimes as cli_runtimes

SUPPORTED_RUNTIMES = cli_runtimes.SUPPORTED_RUNTIMES


def _normalize_runtime(runtime: str | None) -> str:
    """Validate + default the runtime. None/'' → 'claude' (today's behaviour).

    Raises ActionError on anything outside SUPPORTED_RUNTIMES so a typo fails
    loudly at spawn rather than silently launching the wrong CLI."""
    if runtime is None or not str(runtime).strip():
        return "claude"
    r = str(runtime).strip().lower()
    if r not in SUPPORTED_RUNTIMES:
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"spawn: unsupported runtime {runtime!r} — expected one of {list(SUPPORTED_RUNTIMES)}")
    return r


def _build_cli_command(runtime: str | None, model: str | None,
                       role_caps_settings: str | None = None) -> str:
    """Build the interactive CLI invocation for a spawned pane.

    * claude → ``claude --dangerously-skip-permissions [--model <id>]``
    * codex  → ``codex --dangerously-bypass-approvals-and-sandbox [--model <id>]``
      (``--dangerously-bypass…`` is codex's skip-permissions equivalent; the box
      is already externally isolated per the worker's deployment.)

    ``model`` is shell-quoted. Both CLIs accept ``--model``/``-m``. With
    ``runtime`` unset and ``model`` unset the claude branch reproduces the exact
    pre-CAP-1 command string.

    NOTE codex also needs its working dir marked trusted, or it halts on a
    first-visit "Do you trust this directory?" gate that ``--dangerously-bypass``
    does NOT clear. That is handled out-of-band by :func:`_ensure_codex_trusted`
    (a config-file write) rather than a ``-c`` flag — codex's ``-c`` dotted-key
    parser mishandles a quoted path segment, so the persisted-config path (exactly
    what codex writes when a human answers "yes") is the robust mechanism.

    Role caps: ``role_caps_settings`` (a written ``--settings`` file, see
    :func:`_write_role_caps`) REPLACES ``--dangerously-skip-permissions`` with
    ``--permission-mode dontAsk --settings <file>``. codex has no equivalent, so
    a capped codex spawn is refused (fail closed, never silently uncapped)."""
    rt = _normalize_runtime(runtime)
    if role_caps_settings and rt != "claude":
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: role caps are not supported for runtime {rt!r}")
    if rt != "claude":
        # Keep Codex's historical PATH lookup; the registry owns its flags.
        executable = (os.environ.get("LOOPS_CODEX_BIN") or "codex") if rt == "codex" else None
        return shlex.join(cli_runtimes.argv(rt, model, executable=executable))
    cmd = (f"{shlex.quote(os.environ.get('LOOPS_CLAUDE_BIN') or 'claude')} "
           f"{_claude_permission_args(role_caps_settings)}")
    if model is not None and str(model).strip():
        cmd += f" --model {shlex.quote(str(model).strip())}"
    return cmd


def _claude_permission_args(role_caps_settings: str | None) -> str:
    """Today's blanket bypass, or the role-capped ``dontAsk`` + settings file."""
    if not role_caps_settings:
        return "--dangerously-skip-permissions"
    return f"--permission-mode dontAsk --settings {shlex.quote(str(role_caps_settings))}"


def _role_caps_enabled() -> bool:
    """``LOOPS_ROLE_CAPS=0`` (or false/off/no) on the daemon restores today's
    command too — mirrors ``mcp_loops.role_profiles.caps_enabled``."""
    v = os.environ.get("LOOPS_ROLE_CAPS", "")
    return v.strip().lower() not in ("0", "false", "off", "no")


def _write_role_caps(cfg: Any, slug: str, window: str, role: str | None,
                     profile: Any) -> str:
    """Write the engine-built role profile (``mcp_loops.role_profiles``) to
    ``<data>/<slug>/role_caps/<window>-<role>.settings.json`` — outside the
    pane's cwd and never under ~/.claude — and return the path. The dict (not a
    path) travels in the spawn request so a daemon on another host still gets
    the caps. Any malformed profile or write error raises: fail closed."""
    from bot_squad_worker.actions import ActionError
    if not isinstance(profile, dict) or not isinstance(profile.get("permissions"), dict):
        raise ActionError("spawn: role_caps must be a settings object with 'permissions'")
    name = f"{window}-{role or 'agent'}"
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise ActionError(f"spawn: invalid role_caps file name {name!r}")
    path = Path(cfg.data_dir) / slug / "role_caps" / f"{name}.settings.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(profile, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise ActionError(f"spawn: could not write role caps settings: {e}") from e
    return str(path)


_ISOLATION_OFF = ("off", "0", "false", "no", "none")


def _isolation_enabled() -> bool:
    """``LOOPS_ISOLATION=off`` (or 0/false/no/none) on the daemon spawns and
    resumes panes uncaged even when the engine sent a ``sandbox`` spec —
    mirrors ``mcp_loops.sandbox.resolve_mode``."""
    return os.environ.get("LOOPS_ISOLATION", "").strip().lower() not in _ISOLATION_OFF


def _sandbox_module(verb: str):
    from bot_squad_worker.actions import ActionError
    try:
        from mcp_loops import sandbox
    except ImportError as e:
        raise ActionError(f"{verb}: loop cage requested but mcp_loops.sandbox is "
                          f"not importable ({e}) — refusing to start the pane uncaged") from e
    return sandbox


def _sandbox_policy(verb: str, spec: Any):
    """Validate the engine's ``sandbox`` spec and make sure the loop slice exists
    with its limits READ BACK (``ensure_slice``). Without that check a missing
    slice would be created implicitly by ``systemd-run --slice`` with no limits.
    Any failure raises: never an uncaged pane."""
    from bot_squad_worker.actions import ActionError
    sandbox = _sandbox_module(verb)
    try:
        policy = sandbox.from_spec(spec)
    except (ValueError, KeyError, TypeError) as e:
        raise ActionError(f"{verb}: invalid sandbox spec: {e}") from e
    if policy.slice:
        try:
            sandbox.ensure_slice(policy)
        except Exception as e:  # noqa: BLE001 — any slice failure fails closed
            raise ActionError(f"{verb}: loop cage slice {policy.slice} unavailable: {e} "
                              "— refusing to start the pane uncaged") from e
    return sandbox, policy


def _write_sandbox_spec(cfg: Any, slug: str, window: str, spec: dict) -> str:
    """Persist the spec to ``<data>/<slug>/sandbox/<window>.json`` so
    :func:`resume` restarts the pane caged (the md records the path)."""
    from bot_squad_worker.actions import ActionError
    if not re.match(r"^[A-Za-z0-9_.-]+$", str(window)):
        raise ActionError(f"spawn: invalid sandbox spec file name {window!r}")
    path = Path(cfg.data_dir) / slug / "sandbox" / f"{window}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise ActionError(f"spawn: could not write sandbox spec: {e}") from e
    return str(path)


def _cage_nonce() -> str:
    """Scope-unit suffix: unique per pane start (a resume must not collide with
    a lingering scope of the previous pane). A seam for goldens."""
    return secrets.token_hex(4)


# codex persists per-project trust in ~/.codex/config.toml as:
#   [projects."<abs path>"]
#   trust_level = "trusted"
_CODEX_CONFIG = Path(os.path.expanduser("~/.codex/config.toml"))


def _ensure_codex_trusted(cwd: str, config_path: Path | None = None) -> bool:
    """Idempotently mark ``cwd`` trusted in codex's config so a spawned codex
    session boots straight to its composer instead of the first-visit directory-
    trust gate (which the ``--dangerously-bypass`` flag does NOT clear).

    Writes the SAME ``[projects."<cwd>"]`` / ``trust_level = "trusted"`` block
    codex itself writes when a human answers "yes". Additive + idempotent: if a
    block for this exact path already exists it is left untouched (never override a
    human's explicit choice). Returns True if the path is trusted afterwards.

    Best-effort: on any I/O error returns False (the spawn still proceeds; codex
    will just show the trust gate, which is visible + recoverable, not silent)."""
    path = config_path or _CODEX_CONFIG
    header = f'[projects."{cwd}"]'
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if header in existing:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if existing.endswith("\n") or not existing else "\n"
        block = f'{sep}\n{header}\ntrust_level = "trusted"\n'
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(block)
        return True
    except OSError:
        return False


def list_panes() -> list[PaneInfo]:
    """Return all tmux panes across all sessions/windows for this user.

    Returns an empty list if tmux is not running or no panes exist.
    """
    fmt = "#{pane_id}|#{window_name}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}"
    result = _run(["tmux", "list-panes", "-a", "-F", fmt])
    if result.returncode != 0:
        return []
    panes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        panes.append(PaneInfo(
            pane_id=parts[0],
            window=parts[1],
            pid=parts[2],
            cwd=parts[3],
            command=parts[4],
        ))
    return panes


def compute_sid(user: str, window: str, pane_id: str) -> str:
    """Compute ``S-<user>-<window>-p<pane_id_no_pct>``.

    pane_id typically looks like ``%2``; we strip the ``%``.
    """
    pane_no_pct = pane_id.lstrip("%")
    return f"S-{user}-{window}-p{pane_no_pct}"


# A sid is only window-name + pane-id, and tmux restarts its pane ids at %0 when
# the server restarts — while every loop numbers its windows from 90. So after a
# tmux-server restart an OLD sid string (still held by some loop's engine) can
# match a NEW pane that belongs to a different loop/agent, and inject_input would
# type that loop's turn into the wrong agent instead of failing "no live pane"
# (which the engine re-resolves with a fresh spawn). The registry binds every sid
# the daemon issues to the pane's pid: an issued sid is never re-issued to a
# different pane (the window is renamed ``<window>r<n>`` instead), and a live
# pane whose pid disagrees with the binding does not resolve for that sid.

def _sid_binding_path(cfg: Any, sid: str) -> Path:
    return Path(cfg.data_dir) / "_sid_registry" / f"{sid}.json"


def _read_sid_binding(cfg: Any, sid: str) -> dict | None:
    try:
        rec = json.loads(_sid_binding_path(cfg, sid).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def sid_binding_matches(cfg: Any, sid: str, pane: PaneInfo) -> bool:
    """True unless ``sid`` was issued to a DIFFERENT pane (pid mismatch). An
    unregistered sid (pre-registry session, or a pane the daemon didn't spawn)
    keeps the legacy window+pane-id match."""
    rec = _read_sid_binding(cfg, sid)
    return rec is None or str(rec.get("pane_pid")) == str(pane.pid)


# Pruning. A binding only matters while some holder may still name its sid, so:
#  - archive_session drops it at once — the archived session md stays behind and
#    claim_sid treats a suspended/archived md as a tombstone (also after an
#    unarchive, which leaves it suspended), so the sid is still never re-issued;
#  - a sweep (throttled, run from claim_sid) drops bindings whose pane pid is dead,
#    that no live (non-archived) session md names, and that are older than
#    SID_REGISTRY_TTL_S — an engine holding a sid through a tmux restart keeps its
#    session md, so the binding that guards it survives.
SID_REGISTRY_TTL_S = 7 * 24 * 3600
SID_SWEEP_INTERVAL_S = 3600
_last_sid_sweep = 0.0


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(str(pid)), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False
    return True


def _sid_session_mds(cfg: Any, sid: str) -> list[dict]:
    """Every project's session md for ``sid`` (a sid is unique across slugs)."""
    out = []
    for p in Path(cfg.data_dir).glob(f"*/sessions/{glob.escape(sid)}.md"):
        meta = _read_session_metadata(p)
        if meta is not None:
            out.append(meta)
    return out


def _is_archived(meta: dict) -> bool:
    return str(meta.get("archived", "")).lower() == "true"


def _is_tombstone(meta: dict) -> bool:
    """A session md that still owns its sid although no pane is bound to it."""
    return _is_archived(meta) or meta.get("status") == "suspended"


def release_sid(cfg: Any, sid: str) -> None:
    """Drop ``sid``'s registry binding (best-effort)."""
    try:
        _sid_binding_path(cfg, sid).unlink()
    except OSError:
        pass


def sweep_sid_registry(cfg: Any, now: float | None = None) -> list[str]:
    """Remove bindings that can no longer guard anything (see above); returns the
    pruned sids. Best-effort: unreadable entries are left alone."""
    now = time.time() if now is None else now
    pruned: list[str] = []
    try:
        entries = list((Path(cfg.data_dir) / "_sid_registry").glob("*.json"))
    except OSError:
        return pruned
    for path in entries:
        sid = path.stem
        rec = _read_sid_binding(cfg, sid)
        if rec is None:
            continue
        try:
            at = float(rec.get("at") or path.stat().st_mtime)
        except (OSError, TypeError, ValueError):
            continue
        if now - at < SID_REGISTRY_TTL_S or _pid_alive(rec.get("pane_pid")):
            continue
        if any(not _is_archived(m) for m in _sid_session_mds(cfg, sid)):
            continue
        release_sid(cfg, sid)
        pruned.append(sid)
    return pruned


def _maybe_sweep_sid_registry(cfg: Any) -> None:
    global _last_sid_sweep
    now = time.time()
    if now - _last_sid_sweep < SID_SWEEP_INTERVAL_S:
        return
    _last_sid_sweep = now
    sweep_sid_registry(cfg, now)


def claim_sid(cfg: Any, user: str, pane: PaneInfo) -> tuple[str, PaneInfo]:
    """Issue the sid for a freshly created pane, never one already bound to an
    earlier pane (or left by a suspended/archived session md): on a collision the window is
    renamed ``<window>r<n>`` (tmux target-safe) until the sid is unused. Records
    the binding; returns the sid and the (possibly renamed) pane. Best-effort on
    I/O — a registry write failure degrades to the legacy sid, never fails the
    spawn."""
    _maybe_sweep_sid_registry(cfg)
    window, n = pane.window, 0
    while True:
        sid = compute_sid(user, window, pane.pane_id)
        rec = _read_sid_binding(cfg, sid)
        if rec is None:
            if not any(_is_tombstone(m) for m in _sid_session_mds(cfg, sid)):
                break
        elif str(rec.get("pane_pid")) == str(pane.pid):
            break
        n += 1
        window = f"{pane.window}r{n}"
    if window != pane.window:
        if _run(["tmux", "rename-window", "-t", pane.pane_id, window]).returncode != 0:
            return compute_sid(user, pane.window, pane.pane_id), pane
        pane = PaneInfo(pane_id=pane.pane_id, window=window, pid=pane.pid,
                        cwd=pane.cwd, command=pane.command)
    try:
        path = _sid_binding_path(cfg, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sid": sid, "pane_id": pane.pane_id,
                                    "pane_pid": str(pane.pid), "window": window,
                                    "at": time.time()}))
    except OSError:
        pass
    return sid, pane


def discover_claude_uuid(cwd: str, user_home: str) -> str | None:
    """Return the UUID (filename stem) of the most recent .jsonl for this cwd.

    Encodes cwd using the standard claude path-encoding:
      ``cwd.replace('/', '-').lstrip('-')``
    Returns None if no project dir or no .jsonl files exist.
    """
    encoded = cwd.replace("/", "-").lstrip("-")
    proj_dir = Path(user_home) / ".claude" / "projects" / encoded
    if not proj_dir.exists():
        return None
    jsonl_files = list(proj_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    # Latest by mtime → that's the active session
    latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
    return latest.stem  # filename without .jsonl = UUID


def _get_user_home() -> str:
    """Return the home directory for the current user."""
    return str(Path.home())


def _session_file(data_dir: Path, slug: str, sid: str) -> Path:
    return data_dir / slug / "sessions" / f"{sid}.md"


def _write_session_metadata(path: Path, meta: dict) -> None:
    """Write a session metadata file with YAML frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            if v:
                lines.append(f"{k}: [{', '.join(str(i) for i in v)}]")
            else:
                lines.append(f"{k}: []")
        elif v is None:
            lines.append(f"{k}: ~")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines))


def _read_session_metadata(path: Path) -> dict | None:
    """Parse YAML-like frontmatter from a session metadata file.

    Returns None if the file doesn't exist or has no frontmatter.
    """
    if not path.exists():
        return None
    text = path.read_text()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fm = parts[1].strip()
    meta: dict = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        # Parse list values like [T-0042, T-0043]
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [x.strip() for x in inner.split(",")] if inner else []
        elif v == "~" or v == "null":
            meta[k] = None
        else:
            meta[k] = v
    return meta


def runtime_for_sid(cfg: Any, sid: str) -> str:
    """CAP-1: the runtime ('claude'|'codex') recorded for a session's SID.

    spawn() stamps ``runtime`` into the session md; this reads it back so the
    turn-2+ inject path can use the right per-CLI ready/running markers. The SID
    carries no slug, so we scan each known project's sessions dir. Defaults to
    'claude' when unknown (no md, no field, unreadable) — the safe historical
    behaviour."""
    data_dir = Path(cfg.data_dir)
    for slug in getattr(cfg, "projects", {}) or {}:
        try:
            meta = _read_session_metadata(_session_file(data_dir, slug, sid))
        except OSError:
            meta = None
        if meta and meta.get("runtime"):
            rt = str(meta["runtime"]).strip().lower()
            if rt in SUPPORTED_RUNTIMES:
                return rt
    return "claude"


def _scan_linked_tasks(data_dir: Path, slug: str, sid: str, claude_uuid: str | None) -> list[str]:
    """Scan backlog T-*.md files for tasks with a matching session field."""
    backlog_dir = data_dir / slug / "backlog"
    if not backlog_dir.exists():
        return []
    linked = []
    for task_file in sorted(backlog_dir.glob("T-*.md")):
        meta = _read_session_metadata(task_file)
        if meta is None:
            continue
        session_val = meta.get("session", "")
        if session_val and (session_val == sid or (claude_uuid and session_val == claude_uuid)):
            task_id = task_file.stem  # T-0042
            linked.append(task_id)
    return linked


def _get_current_user() -> str:
    """Return the OS username."""
    import getpass
    return getpass.getuser()


def _ensure_project_tmux_session(slug: str, cwd: str) -> None:
    """Ensure a long-lived tmux session named after the project exists.

    Per the active-context-manager model: one tmux session per project,
    panes/windows live inside it. Survives across spawn/resume cycles.
    Caller must guarantee the session is created before any new-window.
    """
    has = _run(["tmux", "has-session", "-t", slug])
    if has.returncode == 0:
        return
    # Create detached; -n _init parks a placeholder window we never use for
    # claude. claude windows are added via tmux new-window -t <slug>:.
    _run([
        "tmux", "new-session", "-d",
        "-s", slug,
        "-c", cwd,
        "-n", "_init",
    ])


# ---------------------------------------------------------------------------
# Session manager actions
# ---------------------------------------------------------------------------

def session_is_halted(cfg: Any, slug: str, sid: str) -> bool:
    """T-0006: read the `halted: true` flag from a session's frontmatter."""
    meta_file = _session_file(cfg.data_dir, slug, sid)
    if not meta_file.exists():
        return False
    meta = _read_session_metadata(meta_file)
    if not meta:
        return False
    return str(meta.get("halted", "")).lower() == "true"


def set_halt(cfg: Any, slug: str, sid: str, halted: bool) -> dict:
    """T-0006: set/clear the halt flag on a session's frontmatter.

    Halt is advisory at the protocol layer: peer_send still delivers, but
    the response surfaces `halted: {sid: bool}` so coord knows the recipient
    isn't expected to act until the halt clears. Use this when an expert
    finishes a phase-gated task and the coord wants to enforce a review
    pause before dispatching the next task.

    Returns: {ok: true, sid, halted, changed: bool}
    """
    from bot_squad_worker.actions import ActionError
    meta_file = _session_file(cfg.data_dir, slug, sid)
    if not meta_file.exists():
        raise ActionError(f"set_halt: no session metadata for SID {sid!r}")
    meta = _read_session_metadata(meta_file) or {}
    prev = str(meta.get("halted", "")).lower() == "true"
    new_value = bool(halted)
    if prev == new_value:
        return {"ok": True, "sid": sid, "halted": new_value, "changed": False}
    meta["halted"] = "true" if new_value else "false"
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "halted": new_value, "changed": True}


def list_sessions(cfg: Any, slug: str) -> list[dict]:
    """List all Claude sessions for a project (active + paused).

    Active sessions are discovered via tmux list-panes.
    Paused sessions are read from data/<slug>/sessions/*.md.
    """
    # GAP 1 symmetry: a workspace-detached session (unregistered slug) must still be
    # listable. Without a registered project there is no shared repo_path to match
    # active panes by — paused/suspended sessions still list from the md dir, and the
    # repo-based active-pane match below simply finds nothing (guarded for None).
    project = cfg.projects.get(slug)  # may be None for a workspace-detached session
    repo_path = Path(project.repo_path) if project is not None else None
    # Dereference symlinks so symlinked dev clones (e.g. signal_tracker/dev
    # → signal_tracker_mgmt) match panes whose cwd is the real target.
    try:
        repo_real = repo_path.resolve() if repo_path is not None else None
    except OSError:
        repo_real = repo_path
    data_dir = cfg.data_dir
    user = _get_current_user()
    user_home = _get_user_home()

    # --- Discover active panes ---
    active_sids: set[str] = set()
    rows: list[dict] = []

    try:
        panes = list_panes()
    except Exception:
        panes = []

    import re as _re_cmd
    _claude_version_re = _re_cmd.compile(r"^\d+\.\d+\.\d+$")
    for pane in panes:
        pane_cwd = Path(pane.cwd) if pane.cwd else None
        # Accept top-level "claude" plus the version-named binaries Claude Code
        # uses for agent-teams subagents, e.g. "2.1.139" — these are spawned
        # from ~/.local/share/claude/versions/<ver> and tmux reports the basename.
        if pane.command != "claude" and not _claude_version_re.match(pane.command):
            continue
        if pane_cwd is None:
            continue
        try:
            pane_real = pane_cwd.resolve()
        except OSError:
            pane_real = pane_cwd
        try:
            match = (
                pane_cwd == repo_path
                or pane_cwd.is_relative_to(repo_path)
                or pane_real == repo_real
                or pane_real.is_relative_to(repo_real)
            )
            if not match:
                continue
        except (ValueError, TypeError):
            continue

        sid = compute_sid(user, pane.window, pane.pane_id)
        active_sids.add(sid)

        claude_uuid = discover_claude_uuid(pane.cwd, user_home)
        linked_tasks = _scan_linked_tasks(data_dir, slug, sid, claude_uuid)

        # Check for last_prompt_at via .claude/last_user_prompt_ts mtime
        last_prompt_at = None
        prompt_ts_file = repo_path / ".claude" / "last_user_prompt_ts"
        if prompt_ts_file.exists():
            try:
                last_prompt_at = prompt_ts_file.stat().st_mtime
            except OSError:
                pass

        # Check if there's an existing metadata file with started_at + task_id
        session_file = _session_file(data_dir, slug, sid)
        started_at = None
        task_id: str | None = None
        role: str | None = None
        initiative: str = ""
        # Default to "active" for live panes; if the md frontmatter says
        # paused (Ctrl-C'd but pane left open) reflect that — otherwise
        # the UI shows every live pane as active even when the user paused it.
        live_status = "active"
        if session_file.exists():
            existing = _read_session_metadata(session_file)
            if existing:
                started_at = existing.get("started_at")
                tid = existing.get("task_id")
                if tid and tid != "~":
                    task_id = tid
                rv = existing.get("role")
                if rv and rv != "~":
                    role = rv
                init_val = existing.get("initiative")
                if init_val and init_val != "~":
                    initiative = init_val
                md_status = existing.get("status", "")
                if md_status == "paused":
                    live_status = "paused"

        # Phase 9: extras for multi-binding. Empty list when unset.
        extra_task_ids: list[str] = []
        extra_initiatives: list[str] = []
        paused_at_meta: Any = None
        archived_flag = False
        owner_meta: str = ""  # T-0080 — UI-username owner stamp; "" = legacy
        if session_file.exists():
            existing = _read_session_metadata(session_file)
            if existing:
                etids = existing.get("extra_task_ids")
                if isinstance(etids, list):
                    extra_task_ids = [t for t in etids if t and t != "~"]
                einits = existing.get("extra_initiatives")
                if isinstance(einits, list):
                    extra_initiatives = [i for i in einits if i and i != "~"]
                paused_at_meta = existing.get("paused_at")
                archived_flag = str(existing.get("archived", "")).lower() == "true"
                own_val = existing.get("owner")
                if own_val and own_val != "~":
                    owner_meta = str(own_val)

        # T-0006: halt flag (false by default; set via set_halt action)
        halted_flag = False
        if session_file.exists():
            existing = _read_session_metadata(session_file)
            if existing:
                halted_flag = str(existing.get("halted", "")).lower() == "true"
        rows.append({
            "sid": sid,
            "status": live_status,
            "window": pane.window,
            "cwd": pane.cwd,
            "started_at": started_at,
            "last_prompt_at": last_prompt_at,
            "claude_uuid": claude_uuid,
            "task_id": task_id,
            "role": role,
            "initiative": initiative,
            "linked_tasks": linked_tasks,
            "extra_task_ids": extra_task_ids,
            "extra_initiatives": extra_initiatives,
            "paused_at": paused_at_meta,
            "suspended_at": None,
            "archived": archived_flag,
            "owner": owner_meta,
            "halted": halted_flag,
        })

    # --- Non-active sessions from metadata files ---
    # Surface: explicitly paused/suspended sessions AND zombies (md says
    # "active" but the pane is gone — e.g. user closed the tmux window).
    # Anything with no live pane and a claude_uuid is resurrectable.
    sessions_dir = data_dir / slug / "sessions"
    if sessions_dir.exists():
        for meta_file in sorted(sessions_dir.glob("*.md")):
            meta = _read_session_metadata(meta_file)
            if meta is None:
                continue
            sid = meta.get("sid", meta_file.stem)
            if sid in active_sids:
                continue  # listed as active above
            status = meta.get("status", "")
            # Display status: keep "paused" only if pane is still alive;
            # otherwise anything with no pane is "suspended" (resurrectable).
            display_status = "suspended"
            if status == "paused":
                # Pane is gone (we already filtered out alive SIDs) — treat as suspended.
                display_status = "suspended"
            elif status == "suspended":
                display_status = "suspended"
            elif status == "active":
                # Zombie: registry says active but pane is gone.
                display_status = "suspended"
            else:
                # Unknown status — surface as suspended so user can resurrect.
                display_status = "suspended"
            md_task_id = meta.get("task_id")
            if md_task_id == "~":
                md_task_id = None
            md_role = meta.get("role")
            if not md_role or md_role == "~":
                md_role = None
            md_initiative = meta.get("initiative")
            if not md_initiative or md_initiative == "~":
                md_initiative = ""
            md_extra_tids = meta.get("extra_task_ids") or []
            if not isinstance(md_extra_tids, list):
                md_extra_tids = []
            md_extra_tids = [t for t in md_extra_tids if t and t != "~"]
            md_extra_inits = meta.get("extra_initiatives") or []
            if not isinstance(md_extra_inits, list):
                md_extra_inits = []
            md_extra_inits = [i for i in md_extra_inits if i and i != "~"]
            md_archived = str(meta.get("archived", "")).lower() == "true"
            md_owner_val = meta.get("owner")
            md_owner = str(md_owner_val) if (md_owner_val and md_owner_val != "~") else ""
            md_halted = str(meta.get("halted", "")).lower() == "true"  # T-0006
            rows.append({
                "sid": sid,
                "status": display_status,
                "window": meta.get("window", ""),
                "cwd": meta.get("cwd", ""),
                "started_at": meta.get("started_at"),
                "last_prompt_at": meta.get("suspended_at") or meta.get("paused_at"),
                "claude_uuid": meta.get("claude_uuid"),
                "task_id": md_task_id,
                "role": md_role,
                "initiative": md_initiative,
                "linked_tasks": meta.get("linked_tasks") or [],
                "extra_task_ids": md_extra_tids,
                "extra_initiatives": md_extra_inits,
                "paused_at": meta.get("paused_at"),
                "suspended_at": meta.get("suspended_at"),
                "archived": md_archived,
                "owner": md_owner,
                "halted": md_halted,
            })

    return rows


def pause(cfg: Any, slug: str, sid: str) -> dict:
    """Pause a running Claude session — INTERRUPT ONLY.

    Sends Ctrl-C to the pane so Claude stops whatever it's doing and returns
    to its prompt. The pane stays open; the user can type into it directly,
    or click Resume in the UI to re-mark status active. To FREE RESOURCES,
    use suspend() instead.
    """
    project = cfg.projects.get(slug)
    if project is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: unknown project slug {slug!r}")

    user = _get_current_user()
    data_dir = cfg.data_dir

    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    if target_pane is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"pause: no active pane found for SID {sid!r}")

    # Update registry status; preserve everything else.
    meta_file = _session_file(data_dir, slug, sid)
    existing = _read_session_metadata(meta_file) or {}
    existing["status"] = "paused"
    existing["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_session_metadata(meta_file, existing)

    # Just the interrupt — no /exit, no kill-pane.
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "C-c", ""])
    return {"ok": True, "paused": True}


def suspend(cfg: Any, slug: str, sid: str) -> dict:
    """Suspend a Claude session — close the pane to free resources.

    The registry md is preserved (with claude_uuid). Use resume() to
    resurrect: a new tmux window is spawned with ``claude --resume <uuid>``.
    """
    # GAP 1 symmetry: a session spawned with a detached workspace + an unregistered
    # slug must be tear-down-able. suspend never reads `project` beyond this point
    # (it operates on the session md + live panes), so proceed when it's absent.
    project = cfg.projects.get(slug)  # may be None for a workspace-detached session

    user = _get_current_user()
    user_home = _get_user_home()
    data_dir = cfg.data_dir

    panes = list_panes()
    target_pane: PaneInfo | None = None
    for pane in panes:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            target_pane = pane
            break

    meta_file = _session_file(data_dir, slug, sid)
    existing = _read_session_metadata(meta_file) or {}

    # If no live pane, treat as already suspended — just normalise the md.
    if target_pane is None:
        existing["status"] = "suspended"
        existing.setdefault("started_at", "~")
        existing.setdefault("task_id", "~")
        existing.setdefault("claude_uuid", existing.get("claude_uuid", "~"))
        _write_session_metadata(meta_file, existing)
        return {"ok": True, "suspended": True, "already_gone": True}

    claude_uuid = existing.get("claude_uuid")
    if not claude_uuid or claude_uuid == "~":
        claude_uuid = discover_claude_uuid(target_pane.cwd, user_home)
    started_at = existing.get("started_at") or "~"
    task_id = existing.get("task_id") or "~"
    linked_tasks = _scan_linked_tasks(data_dir, slug, sid, claude_uuid)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # T-0080: preserve owner field across suspend/resume so per-user
    # listing filters keep working after a session is suspended.
    owner_val = existing.get("owner") or "~"
    # Start from the EXISTING md and overlay the suspend fields — never rebuild it
    # from a fixed key set. The rebuild dropped `role` (so a resumed/Debriefed
    # briefed manager came back ROLELESS: sleep_expert 400 "has no role", never a
    # first-class manager again — loopyard-bug-1790190791), and with it
    # role_caps_settings / sandbox_spec / workspace_id / runtime, so resume()
    # silently relaunched a capped/caged pane uncapped + uncaged.
    meta: dict = dict(existing)
    meta.update({
        "sid": sid,
        "status": "suspended",
        "window": target_pane.window,
        "cwd": target_pane.cwd,
        "claude_uuid": claude_uuid if claude_uuid else "~",
        "task_id": task_id,
        "started_at": started_at,
        "suspended_at": now,
        "linked_tasks": linked_tasks,
        "owner": owner_val,
    })
    _write_session_metadata(meta_file, meta)

    # Graceful exit then force-kill if needed.
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "C-c", ""])
    time.sleep(0.3)
    _run(["tmux", "send-keys", "-t", target_pane.pane_id, "/exit", "Enter"])

    deadline = time.time() + 10.0
    while time.time() < deadline:
        ids_now = {p.pane_id for p in list_panes()}
        if target_pane.pane_id not in ids_now:
            break
        time.sleep(0.5)
    else:
        _run(["tmux", "kill-pane", "-t", target_pane.pane_id])

    return {"ok": True, "suspended": True}


def resume(cfg: Any, slug: str, sid: str) -> dict:
    """Resume a Claude session — handles paused, suspended, and zombie cases.

    - status=paused with live pane → just clear paused status; user types in tmux.
    - status=paused with no live pane → resurrect (window was closed externally).
    - status=suspended → resurrect (new window + ``claude --resume <uuid>``).
    - status=active with no live pane (zombie) → resurrect.
    - status=active with live pane → error (use pause/suspend first).
    """
    # GAP 1 symmetry: a workspace-detached session (unregistered slug) resumes into
    # its recorded workspace cwd (via _resolve_resume_cwd), not the slug repo, so
    # `project` may be absent here.
    project = cfg.projects.get(slug)  # may be None for a workspace-detached session

    data_dir = cfg.data_dir
    user = _get_current_user()

    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: no metadata found for SID {sid!r}")

    # Resume relaunches ONLY in the session's recorded cwd (its loop worktree, for
    # a workspaces session). If that workspace was reaped by the engine (or now
    # holds a different provision's tree) _resolve_resume_cwd FAILS CLOSED with an
    # ActionError — there is no fallback to the project repo; re-run the loop to
    # re-provision. Legacy sessions record repo_path, which resumes as before.
    recorded_cwd = meta.get("cwd")
    cwd = _resolve_resume_cwd(project, recorded_cwd, meta.get("workspace_id"))
    if recorded_cwd and cwd != recorded_cwd:
        # The resolver only ever returns the recorded cwd (or raises); a mismatch
        # is a resolver regression — surface it rather than relaunching the pane
        # elsewhere and rewriting meta['cwd'] to mask the move.
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"resume: session {sid!r} recorded cwd {recorded_cwd!r} but resolved "
            f"{cwd!r} — refusing to relaunch in a different dir")
    window = meta.get("window", "claude")
    claude_uuid = meta.get("claude_uuid")
    status = meta.get("status", "")

    # Is the original pane still alive?
    panes_now = list_panes()
    live_pane = None
    for pane in panes_now:
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            live_pane = pane
            break

    if live_pane is not None:
        if status == "paused":
            # Just clear paused: user types in the tmux pane to continue.
            meta["status"] = "active"
            meta.pop("paused_at", None)
            _write_session_metadata(meta_file, meta)
            return {"ok": True, "sid": sid, "in_place": True}
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"resume: session {sid!r} has a live pane and is not paused — "
            f"nothing to do. Pause or suspend it first if you meant to restart."
        )

    recorded_runtime = _normalize_runtime(meta.get("runtime"))
    if cli_runtimes.selection(recorded_runtime)[0] != recorded_runtime:
        from bot_squad_worker.actions import ActionError
        raise ActionError("resume: provider routing changed; start a new loop turn using saved loop state")
    if recorded_runtime != "claude":
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"resume: {recorded_runtime} session {sid!r} has no persisted provider conversation ID; "
            "start a new loop turn to restore context from the saved loop state")

    # One tmux session per project — create lazily, never killed.
    _ensure_project_tmux_session(slug, cwd)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # Spawn new window inside the project's tmux session. Use bash -lc so
    # the user's profile is sourced — claude lives in ~/.local/bin which is
    # NOT on the systemd-default PATH the worker inherits.
    # --dangerously-skip-permissions: agent-team sessions cannot pause and
    # ask the human at night; settings.json permissions.allow doesn't cover
    # writes to .claude/ which are needed for the task_id marker. The
    # stakeholder has explicitly opted into this risk class.
    # Role caps: a pane spawned capped restarts capped. A recorded settings file
    # that has vanished fails the resume closed rather than reopening bypass.
    caps = meta.get("role_caps_settings") if _role_caps_enabled() else None
    if caps and not os.path.isfile(str(caps)):
        from bot_squad_worker.actions import ActionError
        raise ActionError(
            f"resume: role caps settings {caps!r} for {sid!r} are missing — "
            "refusing to restart it uncapped")
    perm = _claude_permission_args(caps)
    if claude_uuid and claude_uuid != "~":
        cmd = f"claude {perm} --resume {claude_uuid}"
    else:
        cmd = f"claude {perm}"
    # Loop cage: a pane spawned caged restarts caged (slice re-verified); a
    # recorded spec that has vanished or is unreadable fails the resume closed.
    spec_path = meta.get("sandbox_spec") if _isolation_enabled() else None
    if spec_path and spec_path != "~":
        from bot_squad_worker.actions import ActionError
        try:
            spec = json.loads(Path(str(spec_path)).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ActionError(
                f"resume: sandbox spec {spec_path!r} for {sid!r} is unreadable ({e}) — "
                "refusing to restart it uncaged") from e
        sb, policy = _sandbox_policy("resume", spec)
        cmd = sb.wrap_shell(cmd, policy, agent=str(window), nonce=_cage_nonce())

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{slug}:",
        "-n", window,
        "-c", cwd,
        "bash", "-lc", cmd,
    ])
    if result.returncode != 0:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"resume: tmux new-window failed: {result.stderr}")

    # Discover new pane
    time.sleep(0.5)
    post_panes = list_panes()
    new_panes = [p for p in post_panes if p.pane_id not in pre_panes]
    if not new_panes:
        # Fallback: find by window name
        new_panes = [p for p in post_panes if p.window == window]
    if not new_panes:
        from bot_squad_worker.actions import ActionError
        raise ActionError("resume: could not locate new pane after tmux new-window")

    # Take the one with the highest numeric pane ID (most recently created)
    new_pane = max(new_panes, key=lambda p: int(p.pane_id.lstrip("%")) if p.pane_id.lstrip("%").isdigit() else 0)
    new_sid, new_pane = claim_sid(cfg, user, new_pane)

    # Update metadata
    meta["status"] = "active"
    meta["sid"] = new_sid
    meta["window"] = new_pane.window   # claim_sid may have renamed it (sid collision)
    # The recorded cwd is never rewritten: the pane relaunched exactly there (a
    # vanished/moved recorded cwd raised above instead). Only old metadata that
    # recorded no cwd at all gets the repo_path it actually relaunched in.
    if not recorded_cwd:
        meta["cwd"] = cwd
    # Keep the recorded identity in step with where the pane actually relaunched:
    # a verified workspace re-reads its own token; a legacy repo_path has none. This
    # prevents a later resume from checking a stale token against the new cwd.
    meta["workspace_id"] = _read_workspace_identity(cwd)
    meta.pop("paused_at", None)
    new_meta_file = _session_file(data_dir, slug, new_sid)
    _write_session_metadata(new_meta_file, meta)

    # Remove old metadata file if SID changed
    if new_sid != sid and meta_file.exists():
        meta_file.unlink()

    return {"ok": True, "sid": new_sid}


_IDENTITY_TEMPLATES_DIR = Path(__file__).parent / "templates" / "identity"


def _seed_expert_layout(cfg: Any, slug: str, role: str) -> None:
    """Bot-swarm: ensure ``data/<slug>/experts/<role>/`` exists with
    identity.md and memory/MEMORY.md. Idempotent — existing files are
    not overwritten.

    The identity.md is sourced from
    ``bot_squad_worker/templates/identity/<role>.md`` if present;
    otherwise a stub is written. Curated templates exist for well-known
    roles (coordinator, planner); novel expert roles get the stub and
    are expected to author their own identity on first use.
    """
    base = Path(cfg.data_dir) / slug / "experts" / role
    (base / "memory").mkdir(parents=True, exist_ok=True)
    identity = base / "identity.md"
    if not identity.exists():
        template = _IDENTITY_TEMPLATES_DIR / f"{role}.md"
        if template.is_file():
            identity.write_text(template.read_text())
        else:
            identity.write_text(
                f"# {role}\n\n"
                "TODO: describe this expert's responsibility sphere, "
                "out-of-sphere boundaries, and working agreements.\n"
            )
    memory_index = base / "memory" / "MEMORY.md"
    if not memory_index.exists():
        memory_index.write_text(
            f"# {role} — memory index\n\n"
            "Pointers to topical memory files in this directory. Update on "
            "sleep.\n"
        )


def _write_task_initiative_if_absent(backlog_dir: Path, task_id: str, initiative: str) -> bool:
    """T-0038: stamp `initiative: <basename>` into the task md's frontmatter
    if no `initiative:` field is present.

    Existing-wins: if the task already has an initiative (even a different
    one), the file is left alone — the operator's manual classification is
    authoritative.

    Returns True iff the file was modified.
    """
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        return False
    path = matches[0]
    try:
        text = path.read_text()
    except OSError:
        return False
    m = re.match(r"\A---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return False
    fm_block = m.group(1)
    body = m.group(2)
    fm_lines = fm_block.splitlines()
    for ln in fm_lines:
        if ln.lstrip().startswith("initiative:"):
            return False  # already set — don't clobber
    # Insert after the `status:` line for stable ordering; if no status line,
    # append at the end of frontmatter.
    insert_at = len(fm_lines)
    for i, ln in enumerate(fm_lines):
        if ln.lstrip().startswith("status:"):
            insert_at = i + 1
            break
    fm_lines.insert(insert_at, f"initiative: {initiative}")
    new_fm = "\n".join(fm_lines)
    content = f"---\n{new_fm}\n---\n{body}"
    if not body.startswith("\n"):
        content = f"---\n{new_fm}\n---\n\n{body}"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, path)
    return True


# tmux's answer when the pane (its process exited → window closed) or the whole
# server (restart) is gone — as opposed to a transient capture failure.
_PANE_GONE_MARKERS = ("can't find pane", "can't find window", "can't find session",
                      "no server running", "error connecting to")


def _capture_pane_ex(pane_id: str) -> str | None:
    """Snapshot a pane's visible text; None when the pane is definitely GONE,
    '' on any other failure."""
    res = _run(["tmux", "capture-pane", "-p", "-t", pane_id])
    if res.returncode == 0:
        return res.stdout
    err = (getattr(res, "stderr", "") or "").lower()
    return None if any(m in err for m in _PANE_GONE_MARKERS) else ""


def _capture_pane(pane_id: str) -> str:
    """Best-effort snapshot of a pane's visible text ('' on failure)."""
    return _capture_pane_ex(pane_id) or ""


# Per-runtime pane markers. "ready" = an idle-footer hint proving the REPL input
# box is up; "running" = a turn is clearly in flight (so the Enter landed). Both
# fall back to the version-agnostic "content went stable" / best-effort paths, so
# an unknown build still works — the markers just make delivery faster + surer.
_READY_MARKERS = {
    # claude REPL-up hints. "? for shortcuts" is the classic idle footer, but newer
    # builds (e.g. v2.1.218) can sit on a "Welcome back / What's new" splash whose
    # footer instead reads "bypass permissions on (shift+tab to cycle)" with a
    # "Try \"…\"" composer placeholder — none of which contain "for shortcuts", so
    # the old single marker missed readiness and the first prompt raced the boot.
    "claude": ("for shortcuts", "bypass permissions", "shift+tab to cycle", 'Try "'),
    # codex TUI composer/banner strings (observed on v0.154.0): the boot banner
    # ("OpenAI Codex", "YOLO mode", "/model to change") and the empty-composer
    # placeholder ("Ask Codex to do anything").
    "cursor": ("Cursor Agent", "Add a follow-up", "Ask anything"),
    "codex":  ("Ask Codex", "OpenAI Codex", "YOLO mode", "/model to change"),
}
_RUNNING_MARKERS = {
    "claude": ("esc to interrupt", "tokens ·", "↓ ", "✻", "Baked", "Cogitat", "Crunch", "Work"),
    "cursor": ("esc to stop", "Esc to stop", "Thinking", "Running"),
    "codex":  ("esc to interrupt", "Esc to interrupt", "working", "Working", "thinking", "Thinking", "tokens"),
}

# A brand-new repo trips claude's first-visit FOLDER-TRUST gate ("Is this a project
# you trust? 1. Yes …") BEFORE the composer — so on a FRESH install every loop's
# first spawn stalls on it (the prompt is typed onto the gate + dropped). We accept
# it (Enter = the default "1. Yes, I trust"), which is consistent with the
# --dangerously-skip-permissions posture the spawn already opts into. Detected by
# text so no shared ~/.claude.json write is needed. (codex is pre-trusted via
# _ensure_codex_trusted, so this is effectively the claude analogue.)
_TRUST_GATE_MARKERS = ("Is this a project you trust", "trust this folder",
                       "Yes, I trust")


def _wait_pane_ready(pane_id: str, timeout: float = 30.0, poll: float = 0.4,
                     runtime: str = "claude") -> bool:
    """Block until the CLI's REPL input box is ready to accept keystrokes.

    A fixed sleep used to guess this; when the CLI booted slower than the guess
    the initial prompt was typed onto the not-yet-loaded splash and silently
    dropped, so the agent sat idle on the welcome screen forever. We instead
    poll the pane: ready when the interactive footer hint appears (per-runtime
    marker), or (version-agnostic fallback) when the pane content has gone stable
    and non-empty. A folder-trust gate seen along the way is accepted (Enter) so a
    fresh repo doesn't block the composer. Best-effort — returns True if a ready
    signal was seen, False on timeout (the caller still sends, but the verify-retry
    below is the guard)."""
    deadline = time.time() + timeout
    ready_hints = _READY_MARKERS.get(runtime, _READY_MARKERS["claude"])
    prev, stable = None, 0
    time.sleep(0.5)  # let the window/pane come up at all
    while time.time() < deadline:
        cur = _capture_pane_ex(pane_id)
        if cur is None:                          # the CLI died at boot: never ready
            return False
        if any(t in cur for t in _TRUST_GATE_MARKERS):   # first-visit trust gate
            # accept the default ("Yes, I trust this folder") + keep waiting for the
            # composer that loads after. Reset stability — the screen just changed.
            _run(["tmux", "send-keys", "-t", pane_id, "Enter"])
            time.sleep(1.0)
            prev, stable = None, 0
            continue
        if any(h in cur for h in ready_hints):   # runtime's idle footer hint
            time.sleep(0.3)
            return True
        if cur and cur == prev:               # boot animation has settled
            stable += 1
            if stable >= 2:
                time.sleep(0.3)
                return True
        else:
            stable = 0
        prev = cur
        time.sleep(poll)
    return False


def _deliver_prompt(pane_id: str, prompt: str, attempts: int = 5,
                    wait_ready: bool = True, runtime: str = "claude",
                    max_seconds: float = 150.0) -> bool:
    """Type a prompt into the CLI's input box and submit it, VERIFYING the text
    actually landed before pressing Enter — retrying if it wasn't ready. Two-phase
    send (text, SETTLE, standalone Enter) because tmux wraps text as a bracketed
    paste whose embedded newline is not a submit; an Enter fired before the paste
    settles is swallowed, leaving the prompt unsent (agent idles forever).

    ``wait_ready`` = poll for the interactive footer first (True for a fresh spawn
    booting the CLI; False for an already-live REPL being handed its next turn via
    inject_input — the pane is already interactive). ``runtime`` selects the
    per-CLI readiness/running markers.

    ``max_seconds`` bounds the WHOLE effort (default 150s, under a fresh-spawn's
    raised spawn_timeout). Both failure modes of the spawn inject race are timing,
    not a wrong key, so delivery stays patient to a deadline instead of giving up
    early: MODE 1 = text dropped (composer not accepting yet) → re-type with
    backoff; MODE 2 = text present but Enter not yet honored (a slow claude boot
    can take ~2min before the composer submits) → keep pressing Enter until a turn
    is verified RUNNING or the deadline hits. A pre-ready Enter is a harmless no-op
    (observed: it neither submits nor corrupts the buffer), so pressing patiently
    is safe. Returns True once a turn is running; False if the deadline passes
    unsubmitted (the guardian re-nudges) — never a fake 'best effort' True."""
    probe = ""
    stripped = prompt.strip()
    if stripped:
        probe = stripped.splitlines()[0][:24]
    deadline = time.time() + max(1.0, max_seconds)
    # A FRESH spawn (wait_ready) is bounded by the DEADLINE, not a fixed attempt
    # count: a very slow claude boot can keep dropping the paste past 8 tries
    # (~68s), so an attempt cap gives up before the composer is ready. The turn-2+
    # inject path (already-live REPL) stays bounded by `attempts` — drops are rare
    # there and we don't want to block the loop.
    attempt = 0
    while time.time() < deadline:
        if not wait_ready and attempt >= max(1, attempts):
            break
        attempt += 1
        if wait_ready:
            _wait_pane_ready(pane_id, runtime=runtime)
        _run(["tmux", "send-keys", "-t", pane_id, prompt])
        # Wait for the pasted text to fully SETTLE before pressing Enter. tmux sends
        # multi-line text as a bracketed paste; an Enter that arrives while the paste
        # is still streaming is absorbed as a newline instead of submitting, leaving
        # the prompt in the box unsent (agent idles forever). Poll until the pane stops
        # changing (paste done), then submit.
        time.sleep(1.0)
        prev = None
        for _ in range(20):                       # up to ~8s for the paste to settle
            cur = _capture_pane_ex(pane_id)
            if cur is None:
                return False                      # pane gone: nothing to deliver to
            if cur == prev:
                break
            prev = cur
            time.sleep(0.4)
        if probe and probe not in (prev or ""):
            # Text was DROPPED. On a fresh spawn this is the killer race: a newer
            # claude build shows a "What's new" splash whose footer already matches
            # the ready-marker, so we type onto a composer that isn't accepting
            # keystrokes yet and the text vanishes. The composer starts taking input
            # a bit LATER (observed ~40-60s from boot). So clear the line and retry
            # with a GROWING backoff — a later attempt lands once the box is truly
            # ready. The old fixed 1s retry burned all attempts inside the splash
            # window and gave up ('typed too early, gave up too soon'), which is why
            # a fresh install needed a manual Enter per pane. Turn-2+ inject
            # (wait_ready=False) keeps the snappy 1s retry — that REPL is already live.
            _run(["tmux", "send-keys", "-t", pane_id, "C-u"])
            time.sleep(min(2.0 * (attempt + 1), 8.0) if wait_ready else 1.0)
            continue
        # Text has settled — submit, and VERIFY a turn actually started. Retry Enter
        # PATIENTLY until a turn is clearly running (MODE 2): a freshly-booted claude
        # can take up to ~2min before its composer honors Enter, and an Enter that
        # lands before then is a harmless no-op. On a FRESH spawn keep pressing to the
        # overall deadline (this is what removes the manual-Enter babysitting); on the
        # turn-2+ inject path (wait_ready=False) the REPL is already live, so a short
        # 30s window is plenty and we don't want to block the loop.
        RUNNING = _RUNNING_MARKERS.get(runtime, _RUNNING_MARKERS["claude"])
        submit_deadline = deadline if wait_ready else min(deadline, time.time() + 30.0)
        while time.time() < submit_deadline:
            _run(["tmux", "send-keys", "-t", pane_id, "Enter"])
            time.sleep(2.5)
            snap = _capture_pane_ex(pane_id)
            if snap is None:
                # The pane VANISHED (claude exited / tmux server restarted): report
                # unsubmitted NOW — pressing Enter at a dead pane until the deadline
                # only stalls spawn_session (150s) / inject_input (30s) before the
                # engine's liveness probe can respawn it.
                return False
            if any(m in snap for m in RUNNING):
                return True                       # a turn is running → submitted
        return False                              # deadline hit, unsubmitted → guardian re-nudges
    _run(["tmux", "send-keys", "-t", pane_id, "Enter"])  # last resort
    return False


def _deliver_initial_prompt(pane_id: str, prompt: str, attempts: int = 8,
                            runtime: str = "claude") -> bool:
    """Deliver the FIRST prompt to a freshly-spawned pane (waits for the CLI to
    finish booting first). Thin wrapper over the shared _deliver_prompt. More
    attempts than the inject path (8 vs 5): a fresh CLI's composer can drop
    keystrokes for the first ~minute of boot, so delivery retries with a growing
    backoff until the text sticks — the total stays under the 90s spawn_timeout."""
    return _deliver_prompt(pane_id, prompt, attempts=attempts, wait_ready=True,
                           runtime=runtime)


def _require_repo_dir(project: Any, verb: str) -> str:
    """The slug's ``repo_path`` as a str, validated to be an existing directory.

    The legacy (non-workspace) spawn path uses ``repo_path`` as the pane cwd;
    ``tmux new-window -c`` on a missing dir silently starts the pane elsewhere,
    so a misconfigured or moved repo is refused here, loudly."""
    from bot_squad_worker.actions import ActionError
    repo_path = str(project.repo_path)
    if not os.path.isdir(repo_path):
        raise ActionError(
            f"{verb}: project repo_path {repo_path!r} is not an existing directory "
            "— fix the slug's repo_path in projects.toml")
    return repo_path


def _resolve_spawn_cwd(project: Any, workspace: str | None) -> str:
    """The working dir a spawned session runs in.

    Default (legacy / back-compat): the slug's ``projects.toml`` ``repo_path``.

    mcp_loops loop-workspaces (P1): when the engine passes an explicit
    ``workspace`` — an already-provisioned ephemeral git worktree for the loop —
    THAT is the cwd, **detached** from the slug's ``repo_path``. The slug is then
    used only for tmux grouping + data-dir resolution; the loop builds in its own
    worktree. The workspace must be an absolute, existing directory (the engine
    provisioned it before spawning); a bad value fails fast rather than silently
    falling back to the shared repo. ``workspace`` unset ⇒ legacy, except that
    the slug's ``repo_path`` must itself be an existing directory: a misconfigured
    or missing repo fails with a clear ActionError instead of launching a pane
    toward a bad cwd."""
    if not workspace:
        return _require_repo_dir(project, "spawn")
    from bot_squad_worker.actions import ActionError
    if not os.path.isabs(workspace):
        raise ActionError(
            f"spawn: workspace must be an absolute path, got {workspace!r}")
    if not os.path.isdir(workspace):
        raise ActionError(
            f"spawn: workspace {workspace!r} is not an existing directory")
    return workspace


# Engine-managed identity token dropped by mcp_loops.workspaces.provision into
# every loop worktree (mirror of workspaces.WORKSPACE_ID_FILE — the packages don't
# import each other, so the filename is the shared contract). Its per-provision
# nonce lets resume tell a workspace that is STILL the one this session recorded
# from one recreated at the same path for a different run.
_LOOP_WORKSPACE_ID_FILE = ".loop-workspace-id"


def _read_workspace_identity(path: str) -> str | None:
    try:
        tok = (Path(path) / _LOOP_WORKSPACE_ID_FILE).read_text("utf-8").strip()
    except OSError:
        return None
    return tok or None


def _resolve_resume_cwd(project: Any, recorded_cwd: str | None,
                        recorded_ws_id: str | None = None) -> str:
    """The working dir a *resumed* session relaunches in.

    A session records its cwd at spawn time (the slug's ``repo_path`` for a
    legacy session, or the engine-provisioned loop worktree for a
    loop-workspaces session). Resume normally reattaches to THAT recorded cwd.

    A loop-workspaces worktree is **ephemeral and identity-bound**: the engine
    REAPS its working dir at a terminal state while keeping the branch, and a
    re-run RECREATES a *fresh* worktree (new identity token) at the same path.
    A session must therefore resume into a recorded workspace ONLY when that dir
    still exists AND still carries the same identity token it recorded. If the
    workspace was reaped, or the path now holds a DIFFERENT provision's tree, we
    **fail closed** — raising rather than silently resurrecting the loop in the
    shared project checkout (an isolation leak) or inside another run's live tree
    (corruption). The remedy is to re-run the loop, which re-provisions cleanly.

    Back-compat: a legacy session records ``cwd == repo_path`` (or nothing),
    which is not a detached workspace, so this returns ``repo_path`` — provided
    it still exists. A vanished repo, or a recorded shared-repo cwd that no longer
    exists because the slug's ``repo_path`` moved, raises a clear ActionError
    naming the paths: resume never relaunches somewhere other than where the
    session recorded, and never rewrites the recorded cwd to hide that."""
    from bot_squad_worker.actions import ActionError
    repo_path = str(project.repo_path) if project is not None else None
    # Legacy / non-workspace session: cwd was the shared repo (or unset). Nothing
    # to isolate — resume in the shared checkout exactly as before. A workspace-
    # detached session (no registered project) has no shared repo to fall back to;
    # it always recorded its workspace cwd, handled by the detached branch below.
    if not recorded_cwd or recorded_cwd == repo_path:
        if repo_path is None:
            raise ActionError(
                "resume: session for an unregistered slug recorded no workspace "
                "cwd to resume into. Re-run the loop to provision a fresh workspace.")
        if not os.path.isdir(repo_path):
            raise ActionError(
                f"resume: recorded cwd {recorded_cwd or repo_path!r} (the project "
                f"repo_path) no longer exists — refusing to relaunch elsewhere. Fix "
                "the slug's repo_path in projects.toml or restore the checkout.")
        return repo_path
    # A DETACHED loop workspace was recorded. It must still be on disk AND still be
    # the same provisioned workspace (identity token) or we refuse.
    if os.path.isdir(recorded_cwd):
        if not recorded_ws_id:
            # Pre-identity workspace session (no token recorded): existence is the
            # only signal available — trust it, as before.
            return recorded_cwd
        if _read_workspace_identity(recorded_cwd) == recorded_ws_id:
            return recorded_cwd  # verified same provision → safe to reattach
    elif not recorded_ws_id and repo_path is not None:
        # No identity token and the recorded dir is gone: a pre-identity workspace
        # that was reaped OR a legacy session whose slug repo_path has since moved.
        # Either way resume must not land in the (new) repo_path; name both paths
        # so the discrepancy is visible.
        raise ActionError(
            f"resume: recorded cwd {recorded_cwd!r} no longer exists (the project "
            f"repo_path is now {repo_path!r}) — refusing to relaunch in a different "
            "dir or rewrite the recorded cwd. Restore the recorded dir, or re-run "
            "the loop / spawn a fresh session.")
    raise ActionError(
        f"resume: loop workspace {recorded_cwd!r} was reaped or no longer matches "
        f"its provisioned identity — refusing to resume into the shared project "
        f"checkout (isolation). Re-run the loop to provision a fresh workspace.")


def spawn(
    cfg: Any,
    slug: str,
    window: str,
    initial_prompt: str | None = None,
    task_id: str | None = None,
    initiative: str | None = None,
    owner: str | None = None,
    role: str | None = None,
    model: str | None = None,
    runtime: str | None = None,
    loops_data_dir: str | None = None,
    workspace: str | None = None,
    role_caps: dict | None = None,
    sandbox: dict | None = None,
) -> dict:
    """Spawn a new coding-CLI session in the project's repo.

    Opens a new tmux window, starts the selected CLI (no resume), and
    optionally sends an initial_prompt after a short delay.

    CAP-1 (multi-CLI runtime):
      * ``runtime`` selects the CLI — ``"claude"`` (default / None) or
        ``"codex"``. Any other value is rejected.
      * ``model`` is the per-session model id applied to that CLI
        (``claude --model <id>`` / ``codex --model <id>``). None → the CLI's
        host-default model (unchanged behaviour).
    Both are additive: with neither set the launched command is byte-for-byte
    what it was before this change.

    If task_id is provided, writes ``.claude/task_id`` in the project's
    repo *before* spawning so the SessionStart hook links the new session
    to that backlog task automatically.

    If initiative is provided (a filename under vision/initiatives/), the
    SessionStart hook is told via the BOT_SQUAD_INITIATIVE env var to use
    that file instead of the project's global active_initiative. Lets the
    stakeholder spawn multiple TLs on different initiatives in parallel.

    If owner is provided (T-0080), the spawned session md gets stamped
    with ``owner: <username>`` so per-user listing filters can scope
    results without relying on the SID linux_user prefix. The owner is
    the UI username from the JWT claims, not the linux_user.

    If role is provided (bot-swarm), the spawned session is bound to a
    persistent expert role (e.g. ``coordinator``, ``planner``,
    ``backend-expert``). The role is dropped as ``.claude/role`` and
    forwarded via BOT_SQUAD_ROLE so the SessionStart hook stamps it into
    the session md frontmatter. The expert directory layout under
    ``data/<slug>/experts/<role>/`` is seeded if absent.

    If role_caps is provided (role-caps: the engine's per-role permission
    profile for a manager / input_provider loop step), the pane runs claude
    with ``--permission-mode dontAsk --settings <file>`` instead of
    ``--dangerously-skip-permissions`` and the file is recorded in the session
    md so :func:`resume` restarts it capped. Absent (or ``LOOPS_ROLE_CAPS=0``)
    ⇒ the command is byte-for-byte today's.

    If sandbox is provided (loop cage, ``mcp_loops.sandbox``), the pane's CLI
    runs inside the loop's transient slice + exec stage (no AF_UNIX, NNP,
    Landlock signal scope); the spec is persisted and recorded in the session
    md as ``sandbox_spec`` so :func:`resume` restarts it caged. Absent (or
    ``LOOPS_ISOLATION=off`` on the daemon) ⇒ the command is byte-for-byte today's.
    """
    # GAP 1 (slug-optional when a workspace is provided): a loop-workspaces spawn
    # supplies an explicit engine-provisioned ``workspace`` and consumes the slug
    # ONLY for tmux grouping + data-dir — the project's ``repo_path`` is never read
    # in this path (see _resolve_spawn_cwd, which returns the workspace unchanged and
    # never touches ``project`` when a workspace is present). So when a workspace is
    # present the slug need NOT be a registered projects.toml entry: a loop needs
    # only its projectId (which the engine worktrees from) and a loop-name namespace.
    # ``project`` stays None and the slug string is used verbatim for the tmux session
    # + data-dir paths below. With NO workspace the legacy contract is byte-for-byte:
    # an unknown slug still fails fast and clearly with the same message as before.
    project = cfg.projects.get(slug)
    if project is None and not workspace:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: unknown project slug {slug!r}")

    # cwd: the slug's repo_path (legacy) OR the engine-provided loop worktree
    # (loop-workspaces P1) — detached from the slug's repo_path. All marker drops
    # (.claude/role, .claude/task_id) and the pane cwd follow THIS dir, so the
    # SessionStart hook (which runs in the pane's cwd) finds them wherever the
    # session actually builds. With no workspace, cwd == repo_path (unchanged).
    cwd = _resolve_spawn_cwd(project, workspace)
    cwd_path = Path(cwd)
    user = _get_current_user()

    # CAP-1: validate the runtime up-front so a bad value fails before any tmux /
    # marker side-effects (rather than after a half-built session).
    _normalize_runtime(runtime)  # preserve the daemon ActionError contract
    runtime, model = cli_runtimes.selection(runtime, model)

    if role is not None:
        role = role.strip()
        if not role or not re.match(r"^[A-Za-z0-9_.-]+$", role):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid role {role!r}")
        _seed_expert_layout(cfg, slug, role)
        marker_dir = cwd_path / ".claude"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "role").write_text(role)

    # Drop the task_id marker so SessionStart picks it up.
    if task_id:
        marker_dir = cwd_path / ".claude"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "task_id").write_text(task_id.strip())

    # T-0038: when both a task and an initiative are known at spawn time,
    # stamp the initiative onto the task md so it's queryable without grep
    # (board group-by, filter, etc.). Existing initiative wins — operator
    # classification is authoritative.
    if task_id and initiative:
        try:
            _write_task_initiative_if_absent(
                cfg.data_dir / slug / "backlog",
                task_id.strip(),
                initiative.strip(),
            )
        except OSError:
            # Best-effort: the spawn itself is the source of truth; the task
            # md stamp is a convenience for the UI.
            pass

    # One tmux session per project — create lazily, never killed.
    _ensure_project_tmux_session(slug, cwd)

    # Snapshot existing pane IDs
    pre_panes = {p.pane_id for p in list_panes()}

    # bash -lc so claude (in ~/.local/bin) is on PATH — the worker's
    # systemd env does not include the user's local bin directory.
    # --dangerously-skip-permissions: see resume() rationale above.
    # BOT_SQUAD_INITIATIVE: per-session initiative override (Phase 4).
    # BOT_SQUAD_OWNER (T-0080): per-session owner stamp picked up by the
    # SessionStart hook and written into the SessionMd frontmatter.
    env_prefix_parts: list[str] = []
    if initiative:
        # T-0007: accept either the .md filename OR a bare ID (e.g. "I-0002");
        # resolve bare IDs by globbing the project's initiatives dir. Security:
        # reject anything with a slash or .. regardless of form.
        clean = initiative.strip()
        if "/" in clean or ".." in clean:
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: invalid initiative {initiative!r} — must not contain '/' or '..'"
            )
        if not clean.endswith(".md"):
            init_dir = cfg.data_dir / slug / "initiatives"
            matches = sorted(init_dir.glob(f"{clean}-*.md")) if init_dir.exists() else []
            if not matches:
                known = (
                    sorted(p.name for p in init_dir.glob("*.md"))
                    if init_dir.exists() else []
                )
                from bot_squad_worker.actions import ActionError
                if known:
                    raise ActionError(
                        f"spawn: initiative {initiative!r} not found in {slug}/initiatives. "
                        f"Known: {known}. Pass the .md filename or the bare ID prefix (e.g. 'I-0002')."
                    )
                raise ActionError(
                    f"spawn: initiative {initiative!r} not found and {slug}/initiatives is empty."
                )
            if len(matches) > 1:
                from bot_squad_worker.actions import ActionError
                raise ActionError(
                    f"spawn: initiative {initiative!r} ambiguous — matches "
                    f"{[m.name for m in matches]}. Use the full .md filename."
                )
            clean = matches[0].name
        env_prefix_parts.append(f"BOT_SQUAD_INITIATIVE={shlex.quote(clean)}")
    if owner:
        # Username sanity: alnum + _.- only. The username is API-provided
        # (JWT claim) but we still defence-in-depth-validate before shoving
        # it into a shell env-var assignment.
        owner_clean = owner.strip()
        if not owner_clean or not re.match(r"^[A-Za-z0-9_.-]+$", owner_clean):
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: invalid owner {owner!r}")
        env_prefix_parts.append(f"BOT_SQUAD_OWNER={shlex.quote(owner_clean)}")
    if role:
        env_prefix_parts.append(f"BOT_SQUAD_ROLE={shlex.quote(role)}")
    # Propagate the install root so the SessionStart hook resolves its
    # config + worker socket against this install, not whatever bot-squad
    # the hook's default points at. data_dir is ``<root>/data``, so the
    # install root is data_dir.parent. Equivalent to config_dir.parent on
    # the real Config; works for both Config and test SimpleNamespaces
    # (which only stub ``data_dir``).
    install_root = str(Path(cfg.data_dir).parent)
    env_prefix_parts.insert(0, f"BOT_SQUAD={shlex.quote(install_root)}")
    # mcp_loops P1c: export the loops data dir the server resolved so the agent's
    # `mcp_loops.report` writes end-of-turn status into the SAME dir the server
    # tails (fresh-install split-brain fix). Absolute-only — a relative value
    # would resolve against the pane cwd, re-opening the split we're closing.
    if loops_data_dir:
        if not os.path.isabs(loops_data_dir):
            from bot_squad_worker.actions import ActionError
            raise ActionError(
                f"spawn: loops_data_dir must be an absolute path, got {loops_data_dir!r}")
        env_prefix_parts.append(f"LOOPS_DATA_DIR={shlex.quote(loops_data_dir)}")
    # R24: agent-side mcp_loops CLIs resolve the same state root as this worker.
    if os.environ.get("LOOPYARD_HOME"):
        env_prefix_parts.append(
            f"LOOPYARD_HOME={shlex.quote(os.environ['LOOPYARD_HOME'])}")
    env_prefix = (" ".join(env_prefix_parts) + " ") if env_prefix_parts else ""
    # CAP-1: pick the CLI + apply the per-session model. Validated here (not the
    # daemon action) so any spawn caller — loop substrate, direct action — is held
    # to the same contract. `runtime`/`model` unset ⇒ byte-for-byte the old command.
    # For codex, pre-trust the working dir (config write) so it boots to its
    # composer rather than the first-visit trust gate.
    role_caps_settings = None
    if role_caps is not None and _role_caps_enabled():
        if runtime != "claude":
            from bot_squad_worker.actions import ActionError
            raise ActionError(f"spawn: role caps are not supported for runtime {runtime!r}")
        role_caps_settings = _write_role_caps(cfg, slug, window, role, role_caps)
    if runtime == "codex":
        _ensure_codex_trusted(cwd)
    cli_cmd = _build_cli_command(runtime, model, role_caps_settings)
    sandbox_spec = None
    if sandbox is not None and _isolation_enabled():
        sb, policy = _sandbox_policy("spawn", sandbox)
        sandbox_spec = _write_sandbox_spec(cfg, slug, window, sb.to_spec(policy))
        cli_cmd = sb.wrap_shell(cli_cmd, policy, agent=str(window), nonce=_cage_nonce())
    shell_cmd = f"{env_prefix}{cli_cmd}"

    result = _run([
        "tmux", "new-window", "-d",
        "-t", f"{slug}:",
        "-n", window,
        "-c", cwd,
        "bash", "-lc", shell_cmd,
    ])
    if result.returncode != 0:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"spawn: tmux new-window failed: {result.stderr}")

    # Discover new pane
    time.sleep(0.5)
    post_panes = list_panes()
    new_panes = [p for p in post_panes if p.pane_id not in pre_panes]
    if not new_panes:
        new_panes = [p for p in post_panes if p.window == window]
    if not new_panes:
        from bot_squad_worker.actions import ActionError
        raise ActionError("spawn: could not locate new pane after tmux new-window")

    new_pane = max(new_panes, key=lambda p: int(p.pane_id.lstrip("%")) if p.pane_id.lstrip("%").isdigit() else 0)
    new_sid, new_pane = claim_sid(cfg, user, new_pane)

    # Bot-swarm: write a session md skeleton so the dashboard sees the
    # new pane immediately. The SessionStart hook will enrich it with
    # claude_uuid + started_at when/if it fires (it reads existing
    # fields and preserves them). Without this, sessions where the hook
    # doesn't write — e.g. when Claude Code doesn't pass session_id in
    # the hook event JSON — would be invisible to list_sessions / the
    # dashboard until manually fixed.
    if role:
        try:
            md_path = _session_file(Path(cfg.data_dir), slug, new_sid)
            md_path.parent.mkdir(parents=True, exist_ok=True)
            if not md_path.exists():
                _write_session_metadata(md_path, {
                    "sid": new_sid,
                    "status": "active",
                    "window": new_pane.window,
                    "cwd": cwd,
                    # Loop-Workspaces identity: the token the engine stamped into the
                    # provisioned worktree (None for a legacy repo_path cwd). Resume
                    # checks it so a reaped/recreated workspace fails closed instead
                    # of resurrecting the loop in the shared checkout.
                    "workspace_id": _read_workspace_identity(cwd),
                    "claude_uuid": None,
                    "task_id": task_id if task_id else None,
                    "role": role,
                    # CAP-1: record which CLI this pane runs so the dashboard /
                    # inject path can tell a codex session from a claude one.
                    "runtime": runtime,
                    "model": model if model else None,
                    "initiative": initiative if initiative else None,
                    "extra_task_ids": [],
                    "extra_initiatives": [],
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "owner": owner if owner else None,
                    # role caps: only stamped when capped, so an uncapped md is
                    # unchanged; resume() reads it to restart capped.
                    **({"role_caps_settings": role_caps_settings}
                       if role_caps_settings else {}),
                    # loop cage: only stamped when caged (md otherwise unchanged).
                    **({"sandbox_spec": sandbox_spec} if sandbox_spec else {}),
                })
        except OSError:
            # Best-effort: the hook will write it on first user prompt
            # if this path failed (permissions, race, etc.).
            pass

    # R19: record WHICH daemon spawned this pane (both workers share the
    # default tmux server, so this line is the only discriminator).
    print(f"[spawn] slug={slug} window={new_pane.window} pane={new_pane.pane_id} "
          f"cwd={cwd} config={getattr(cfg, 'config_dir', None)} pid={os.getpid()}",
          file=sys.stderr, flush=True)

    # Send initial prompt if provided. Wait for claude's input box to be ready
    # and verify the text landed before submitting (see _deliver_initial_prompt)
    # — a fixed 2s sleep here used to drop the prompt onto the splash screen when
    # claude booted slower than the guess, leaving the agent idle forever.
    if initial_prompt:
        _deliver_initial_prompt(new_pane.pane_id, initial_prompt, runtime=runtime)

    return {"ok": True, "sid": new_sid}


def respawn_expert(cfg: Any, slug: str, sid: str) -> dict:
    """Kill a swarm expert's pane and spawn a fresh one with the same
    role and window name. Bot-swarm-only — bot-squad sessions without a
    role are rejected.

    Drains any unread mail in the old SID's inbox into the role's
    holding inbox before killing, so messages addressed to the old SID
    aren't orphaned. The new pane drains the holding inbox on its first
    ``inbox_read``.

    Clears the role's ``sleep_mark`` — it was a byte offset into the
    old jsonl which no longer applies to the new session.

    Deletes the old session md to avoid zombie listings.

    Returns ``{"ok": True, "old_sid": ..., "new_sid": ..., "role": ...,
                "drained_lines": int, "window": str}``.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker import intersession as _I

    if slug not in cfg.projects:
        raise ActionError(f"respawn_expert: unknown slug {slug!r}")

    sess_path = _session_file(Path(cfg.data_dir), slug, sid)
    meta = _read_session_metadata(sess_path)
    if meta is None:
        raise ActionError(f"respawn_expert: no session md for {sid!r}")
    role = meta.get("role")
    if not role or role == "~":
        raise ActionError(
            f"respawn_expert: session {sid!r} has no role; bot-squad "
            "sessions cannot respawn this way"
        )

    # Extract window name from SID. Format: S-<user>-<window>-p<pane>.
    m = re.match(r"^S-([^-]+)-(.+)-p\d+$", sid)
    if not m:
        raise ActionError(f"respawn_expert: cannot parse sid {sid!r}")
    window = m.group(2)

    # Drain unread mail in old SID's inbox → role's holding inbox.
    drained = _I.drain_unread_to_holding(cfg, slug, sid, role)

    # Kill the tmux window. Best-effort — if the pane is already gone,
    # tmux returns non-zero; we proceed to spawn anyway.
    _run(["tmux", "kill-window", "-t", f"{slug}:{window}"])

    # Clear the sleep mark for this role; it's a byte offset into the
    # old session's jsonl which is no longer in play.
    mark_path = Path(cfg.data_dir) / slug / "experts" / role / "sleep_mark"
    if mark_path.exists():
        mark_path.unlink()

    # Delete the old session md so list_sessions doesn't show a zombie.
    if sess_path.exists():
        sess_path.unlink()

    # Spawn a fresh pane with the same role + window.
    spawn_result = spawn(cfg, slug, window, role=role)

    return {
        "ok": True,
        "old_sid": sid,
        "new_sid": spawn_result["sid"],
        "role": role,
        "window": window,
        "drained_lines": drained,
    }


# ---------------------------------------------------------------------------
# Phase 9: multi-binding helpers
# ---------------------------------------------------------------------------

def _full_task_set(meta: dict) -> set[str]:
    """Return {primary, *extras} of task IDs for a session md frontmatter."""
    out: set[str] = set()
    tid = meta.get("task_id")
    if tid and tid != "~":
        out.add(tid)
    extras = meta.get("extra_task_ids") or []
    if isinstance(extras, list):
        for t in extras:
            if t and t != "~":
                out.add(t)
    return out


def _full_initiative_set(meta: dict) -> set[str]:
    """Return {primary, *extras} of initiative basenames for a session md."""
    out: set[str] = set()
    init = meta.get("initiative")
    if init and init != "~":
        out.add(init)
    extras = meta.get("extra_initiatives") or []
    if isinstance(extras, list):
        for i in extras:
            if i and i != "~":
                out.add(i)
    return out


def _find_owner(
    data_dir: Path,
    slug: str,
    *,
    task_id: str | None = None,
    initiative: str | None = None,
) -> str | None:
    """Scan all session mds; return SID of the session that already holds the
    given task_id or initiative (primary or extras). None if free.
    """
    sess_dir = data_dir / slug / "sessions"
    if not sess_dir.exists():
        return None
    for md in sorted(sess_dir.glob("*.md")):
        meta = _read_session_metadata(md)
        if meta is None:
            continue
        sid = meta.get("sid", md.stem)
        if task_id and task_id in _full_task_set(meta):
            return sid
        if initiative and initiative in _full_initiative_set(meta):
            return sid
    return None


def bind_task(cfg: Any, slug: str, sid: str, task_id: str) -> dict:
    """Append task_id to a dev session's extra_task_ids.

    Validates: session exists, session is a dev (has primary task_id), task
    file exists, task isn't already bound elsewhere. Sends a peer notification.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bind_task: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"bind_task: no session metadata for SID {sid!r}")

    primary = meta.get("task_id")
    if not primary or primary == "~":
        raise ActionError(f"bind_task: session {sid!r} is not a dev session (no primary task_id)")

    backlog_dir = data_dir / slug / "backlog"
    matches = sorted(backlog_dir.glob(f"{task_id}-*.md"))
    if not matches:
        raise ActionError(f"bind_task: task not found: {task_id}")

    if task_id == primary or task_id in (meta.get("extra_task_ids") or []):
        # Already bound to this session — idempotent success.
        extras = [t for t in (meta.get("extra_task_ids") or []) if t and t != "~"]
        return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "already_bound": True}

    owner = _find_owner(data_dir, slug, task_id=task_id)
    if owner is not None and owner != sid:
        raise ActionError(f"bind_task: task {task_id} already bound to {owner}")

    extras = list(meta.get("extra_task_ids") or [])
    extras = [t for t in extras if t and t != "~"]
    extras.append(task_id)
    meta["extra_task_ids"] = extras
    _write_session_metadata(meta_file, meta)

    # T-0038: if the dev's session carries an initiative, propagate it to
    # the newly-bound task md (existing-wins). Lets multi-binding keep the
    # task-to-initiative graph consistent without operator intervention.
    sess_init = meta.get("initiative")
    if sess_init and sess_init != "~":
        try:
            _write_task_initiative_if_absent(backlog_dir, task_id, sess_init)
        except OSError:
            pass

    # Read the task title for a friendlier message.
    title = ""
    try:
        task_meta = _read_session_metadata(matches[0])
        if task_meta:
            title = str(task_meta.get("title") or "").strip()
    except Exception:
        pass

    text = (
        f"[BIND_TASK from stakeholder] Also work on {task_id}"
        + (f": {title}" if title else "")
        + f". Read data/{slug}/backlog/{matches[0].name} for scope."
    )
    try:
        from bot_squad_worker import intersession as _is
        _is.send(cfg, slug, "stakeholder", sid, text)
    except Exception:
        # Peer notify is best-effort; the binding itself is the source of truth.
        pass

    return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras}


def register_session_role(cfg: Any, slug: str, sid: str, role: str,
                          loop: str | None = None) -> dict:
    """Bind a persistent ROLE (+ owning loop) to an EXISTING session's md.

    The engine calls this when it ADOPTS a session it did not spawn this run —
    an owner-briefed / Debrief-resurrected manager (``S-claude-brief-<loop>-pNNN``).
    Such a session can reach the daemon roleless (its md was rebuilt without
    ``role`` by an older suspend, or it predates role stamping), and every
    role-gated op (sleep_expert, respawn_expert, resume-as-agent) then refuses it:
    ``session has no role`` — it is never a first-class manager past turn 1
    (loopyard-bug-1790190791). Stamping the role here makes it one.

    Idempotent. Only the ``role`` / ``loop`` keys change; everything else in the
    md is preserved. Raises ActionError on an invalid role, or when the sid has
    neither an md nor a live pane (nothing to register — the engine respawns).

    Returns ``{"ok": True, "sid", "role", "loop", "changed": bool}``.
    """
    from bot_squad_worker.actions import ActionError

    role = (role or "").strip()
    if not role or not re.match(r"^[A-Za-z0-9_.-]+$", role):
        raise ActionError(f"register_session_role: invalid role {role!r}")
    meta_file = _session_file(Path(cfg.data_dir), slug, sid)
    meta = _read_session_metadata(meta_file) if meta_file.exists() else None
    if meta is None:
        user = _get_current_user()
        pane = next((p for p in list_panes()
                     if compute_sid(user, p.window, p.pane_id) == sid), None)
        if pane is None:
            raise ActionError(
                f"register_session_role: no session md and no live pane for SID {sid!r}")
        meta = {"sid": sid, "status": "active", "window": pane.window, "cwd": pane.cwd,
                "claude_uuid": None, "task_id": None,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    prev_role = meta.get("role")
    prev_loop = meta.get("loop")
    changed = prev_role != role or (bool(loop) and prev_loop != loop)
    if changed:
        meta["role"] = role
        if loop:
            meta["loop"] = loop
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        _write_session_metadata(meta_file, meta)
    _seed_expert_layout(cfg, slug, role)
    return {"ok": True, "sid": sid, "role": role,
            "loop": meta.get("loop") or None, "changed": changed}


def archive_session(cfg: Any, slug: str, sid: str) -> dict:
    """Mark a session as archived in its frontmatter.

    Rules:
      - session must exist
      - session must be in 'suspended' state (no live pane). Active or
        paused sessions can't be archived — suspend first.

    Idempotent: archiving an already-archived session returns ok=True.
    """
    from bot_squad_worker.actions import ActionError

    # GAP 1 symmetry: archive operates on the session md only; a workspace-detached
    # session (unregistered slug) must be archivable. Proceed when project is absent.
    project = cfg.projects.get(slug)  # may be None for a workspace-detached session

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"archive_session: no metadata for SID {sid!r}")

    # The session must have no live pane. Check tmux directly so we can't
    # rely on a stale md status flag.
    user = _get_current_user()
    for pane in list_panes():
        if compute_sid(user, pane.window, pane.pane_id) == sid:
            raise ActionError(
                f"archive_session: {sid!r} still has a live pane — suspend first"
            )

    status = meta.get("status", "")
    if status not in ("suspended", "paused", "active"):
        # paused/active here mean stale md flags (we already verified no
        # live pane), so allow the archive — normalise to suspended first.
        pass

    meta["status"] = "suspended"
    meta["archived"] = "true"
    _write_session_metadata(meta_file, meta)
    release_sid(cfg, sid)   # the archived md now keeps claim_sid off this sid
    return {"ok": True, "sid": sid, "archived": True}


def unarchive_session(cfg: Any, slug: str, sid: str) -> dict:
    """Clear the archived flag on a session md."""
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unarchive_session: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unarchive_session: no metadata for SID {sid!r}")

    if "archived" in meta:
        meta.pop("archived", None)
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "archived": False}


def bind_initiative(cfg: Any, slug: str, sid: str, initiative: str) -> dict:
    """Append initiative basename to a TL session's extra_initiatives.

    Validates: session exists, session is a TL (no primary task_id),
    initiative file exists, initiative isn't already bound elsewhere.
    Sends a peer notification.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bind_initiative: unknown project slug {slug!r}")

    # T-0007: accept bare ID or .md filename; reject only path-traversal.
    clean = (initiative or "").strip()
    if not clean or "/" in clean or ".." in clean:
        raise ActionError(
            f"bind_initiative: invalid initiative {initiative!r} — must not contain '/' or '..'"
        )

    data_dir = cfg.data_dir
    init_dir = data_dir / slug / "initiatives"
    if not clean.endswith(".md"):
        matches = sorted(init_dir.glob(f"{clean}-*.md")) if init_dir.exists() else []
        if not matches:
            known = (
                sorted(p.name for p in init_dir.glob("*.md"))
                if init_dir.exists() else []
            )
            raise ActionError(
                f"bind_initiative: initiative {initiative!r} not found in {slug}/initiatives. "
                f"Known: {known}. Pass the .md filename or the bare ID prefix."
            )
        if len(matches) > 1:
            raise ActionError(
                f"bind_initiative: initiative {initiative!r} ambiguous — matches "
                f"{[m.name for m in matches]}."
            )
        clean = matches[0].name

    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"bind_initiative: no session metadata for SID {sid!r}")

    primary_task = meta.get("task_id")
    if primary_task and primary_task != "~":
        raise ActionError(f"bind_initiative: session {sid!r} is a dev session, not a teamlead")

    init_path = data_dir / slug / "vision" / "initiatives" / clean
    if not init_path.exists():
        raise ActionError(f"bind_initiative: initiative not found: {clean}")

    primary_init = meta.get("initiative")
    if clean == primary_init or clean in (meta.get("extra_initiatives") or []):
        extras = [i for i in (meta.get("extra_initiatives") or []) if i and i != "~"]
        return {"ok": True, "sid": sid, "initiative": clean, "extras": extras, "already_bound": True}

    owner = _find_owner(data_dir, slug, initiative=clean)
    if owner is not None and owner != sid:
        raise ActionError(f"bind_initiative: initiative {clean} already bound to {owner}")

    extras = list(meta.get("extra_initiatives") or [])
    extras = [i for i in extras if i and i != "~"]
    extras.append(clean)
    meta["extra_initiatives"] = extras
    _write_session_metadata(meta_file, meta)

    text = (
        f"[BIND_INITIATIVE from stakeholder] Also coordinate {clean}. "
        f"Read data/{slug}/vision/initiatives/{clean} for context."
    )
    try:
        from bot_squad_worker import intersession as _is
        _is.send(cfg, slug, "stakeholder", sid, text)
    except Exception:
        pass

    return {"ok": True, "sid": sid, "initiative": clean, "extras": extras}


def unbind_task(cfg: Any, slug: str, sid: str, task_id: str) -> dict:
    """Remove a task binding from a dev session.

    If `task_id` matches the session's primary task_id, the call fails —
    the primary is the session's identity. Removes from extra_task_ids
    otherwise. Idempotent if the task isn't bound.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unbind_task: unknown project slug {slug!r}")

    data_dir = cfg.data_dir
    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unbind_task: no session metadata for SID {sid!r}")

    primary = meta.get("task_id")
    if primary == task_id:
        raise ActionError(
            f"unbind_task: {task_id} is the primary task of {sid!r}; "
            "cannot unbind the session's identity"
        )

    extras = list(meta.get("extra_task_ids") or [])
    extras = [t for t in extras if t and t != "~"]
    if task_id not in extras:
        return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "changed": False}
    extras = [t for t in extras if t != task_id]
    meta["extra_task_ids"] = extras
    _write_session_metadata(meta_file, meta)
    return {"ok": True, "sid": sid, "task_id": task_id, "extras": extras, "changed": True}


def unbind_initiative(cfg: Any, slug: str, sid: str, initiative: str) -> dict:
    """Remove an initiative from a TL session's bindings.

    If the initiative matches the primary `initiative` field, that field is
    cleared (leaving extras intact). Otherwise the value is filtered out of
    `extra_initiatives`. Returns the resulting extras list.

    Idempotent: unbinding a not-present initiative returns ok=True.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"unbind_initiative: unknown project slug {slug!r}")

    # T-0007: accept bare ID or .md filename. For unbind, we resolve bare IDs
    # against the existing extras list so the filter actually matches.
    clean = (initiative or "").strip()
    if not clean or "/" in clean or ".." in clean:
        raise ActionError(
            f"unbind_initiative: invalid initiative {initiative!r} — must not contain '/' or '..'"
        )

    data_dir = cfg.data_dir
    if not clean.endswith(".md"):
        init_dir = data_dir / slug / "initiatives"
        matches = sorted(init_dir.glob(f"{clean}-*.md")) if init_dir.exists() else []
        if len(matches) == 1:
            clean = matches[0].name
        # else: leave bare; will simply not match an extras entry (no-op unbind).

    meta_file = _session_file(data_dir, slug, sid)
    meta = _read_session_metadata(meta_file)
    if meta is None:
        raise ActionError(f"unbind_initiative: no session metadata for SID {sid!r}")

    primary_init = meta.get("initiative")
    extras = list(meta.get("extra_initiatives") or [])
    extras = [i for i in extras if i and i != "~"]

    changed = False
    if primary_init == clean:
        meta["initiative"] = "~"
        changed = True
    if clean in extras:
        extras = [i for i in extras if i != clean]
        meta["extra_initiatives"] = extras
        changed = True

    if changed:
        _write_session_metadata(meta_file, meta)

    return {"ok": True, "sid": sid, "initiative": clean, "extras": extras, "changed": changed}
