"""``yard`` — the Loopyard host CLI. **The box is the source of truth; the web
is a mirror of it.**

Origins are added HOST-SIDE with this tool. The web app never accepts a key,
token, secret, or credential — Principle P3: *"they hold your keys, meter your
tokens, and rent you compute"* is the enemy, so the surface has no vault. You
run ``yard connect <host>`` on the box; the web polls and shows the new origin
card on the next cycle.

Usage::

    yard connect <host> [--address ADDR]   register an origin on THIS box
    yard disconnect <host>                  forget an origin (only if unsynced)
    yard origins                            list origins this box can see
    yard update [--check] [--token -]       upgrade this install in place from its portal/release
    yard runtime use codex --trusted-workspace  save the provider for this install
    yard runtime show                       show saved provider and effective routing
    yard help                               this text

An **origin** on disk is a ``_loops_<host>`` mirror directory sitting beside the
local ``_loops`` mirror (see :mod:`mcp_loops.origins`). ``yard connect`` just
creates that directory and drops a small ``.origin.json`` marker recording the
host, an optional address, and when it was connected — no secrets, ever. The
data root is ``LOOPS_DATA_DIR`` (default ``<install>/data/_loops``); override
with ``--data-dir`` or the env var.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from mcp_loops import diagnostics, origins, paths, runner_registry, services

# Host names become a ``_loops_<host>`` directory, so keep them to a safe,
# path-fragment-free charset. First char alnum; then alnum plus . _ -.
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Belt-and-suspenders for P3: the CLI must NEVER become the secret-entry path
# the web refuses to be. Any arg that looks like a credential flag is rejected.
_SECRET_RE = re.compile(r"key|token|secret|password|passwd|credential", re.I)

# P3 (pilot finding): a credential embedded in a URL — ``scheme://user:pass@host``
# or bare ``user:pass@host`` — is a secret too. ``yard connect --address`` must
# reject it exactly like a ``--token`` flag: the box authenticates its own CLIs
# and never carries a peer's password. Requires the ``:`` so a plain
# ``user@host`` (ssh-style, no secret) is still allowed.
_USERINFO_RE = re.compile(r"[^\s:/@]+:[^\s:/@]+@")

_RESERVED_HOSTS = {origins.LOCAL_ORIGIN_ID}  # "local" is the box itself


def _root(data_dir: Optional[str] = None) -> str:
    """The LOCAL mirror path (``data/_loops``) — mirrors of other hosts live as
    ``_loops_<host>`` siblings of it. Resolution matches the server/report so
    ``yard`` writes exactly where the daemon reads."""
    return paths.resolve_data_dir(data_dir)


def _mirror_path(root: str, host: str) -> str:
    """Absolute path of the ``_loops_<host>`` mirror dir for ``host``."""
    return os.path.join(os.path.dirname(root), f"{origins.MIRROR_PREFIX}{host}")


def _err(msg: str) -> int:
    print(f"yard: {msg}", file=sys.stderr)
    return 2


def _reject_secrets(args: list[str]) -> Optional[int]:
    """If any argument smells like a credential, refuse — the box never takes
    secrets through this door either (P3). Returns an exit code to bail on, or
    None to proceed.

    The ONE exemption is ``--token -`` / ``--token=-``: ``-`` names stdin, so
    the flag carries no secret on argv (``yard update --token -``). A literal
    value after ``--token`` is still refused."""
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--token=-" or (a == "--token" and args[i + 1:i + 2] == ["-"]):
            skip_next = a == "--token"
            continue
        if _SECRET_RE.search(a) or _USERINFO_RE.search(a):
            return _err(
                "refusing a secret-looking argument. Origins never carry keys, "
                "tokens, passwords, or user:pass@host URLs — the box "
                "authenticates its own CLIs. `yard connect <host>` takes only a "
                "host name and optional --address.")
    return None


def _valid_host(host: str) -> Optional[str]:
    """Return an error string if ``host`` is not a usable origin id, else None."""
    if not host:
        return "connect needs a host name, e.g. `yard connect vps-a`."
    if host in _RESERVED_HOSTS:
        return f"'{host}' is reserved for this box — pick another host name."
    if not _HOST_RE.match(host):
        return (f"'{host}' isn't a valid host name (use letters, digits, and "
                ". _ - only).")
    return None


def _parse_connect(rest: list[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse ``<host> [--address ADDR]``. Returns ``(host, address, error)``."""
    host: Optional[str] = None
    address: Optional[str] = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--address", "--addr"):
            if i + 1 >= len(rest):
                return None, None, "--address needs a value."
            address = rest[i + 1]
            i += 2
            continue
        if tok.startswith("--address="):
            address = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.startswith("-"):
            return None, None, f"unknown option {tok!r}."
        if host is None:
            host = tok
            i += 1
            continue
        return None, None, f"unexpected extra argument {tok!r}."
    return host, address, None


def cmd_connect(rest: list[str], *, data_dir: Optional[str] = None,
                now: Optional[float] = None) -> int:
    """Register an origin on THIS box: create its ``_loops_<host>`` mirror dir
    and write the ``.origin.json`` marker. Idempotent — re-connecting the same
    host refreshes the address and keeps the original ``connectedAt``."""
    host, address, err = _parse_connect(rest)
    if err:
        return _err(err)
    verr = _valid_host(host or "")
    if verr:
        return _err(verr)
    assert host is not None
    root = _root(data_dir)
    parent = os.path.dirname(root)
    if not parent or not os.path.isdir(parent):
        return _err(f"data root's parent {parent!r} doesn't exist — is "
                    "LOOPS_DATA_DIR set correctly?")
    mirror = _mirror_path(root, host)
    marker_path = os.path.join(mirror, origins.ORIGIN_MARKER)
    existed = os.path.exists(marker_path)
    connected_at = now if now is not None else time.time()
    if existed:  # keep the original connect time on a re-connect
        prior = origins._read_marker(mirror) or {}
        connected_at = prior.get("connectedAt", connected_at)
    os.makedirs(mirror, exist_ok=True)
    marker = {
        "host": host,
        "address": address,
        "connectedAt": connected_at,
        "source": "yard",
    }
    with open(marker_path, "w", encoding="utf-8") as fh:
        json.dump(marker, fh, ensure_ascii=False, indent=2)
    verb = "re-connected" if existed else "connected"
    where = f" ({address})" if address else ""
    print(f"{verb} origin '{host}'{where}")
    print(f"  mirror: {mirror}")
    print("  the web mirror will show this origin on its next poll — no secret "
          "entry needed.")
    return 0


def cmd_disconnect(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """Forget an origin. Refuses to delete a mirror that has synced loops — we
    remove the marker and the dir only when it holds nothing but our marker, so
    ``yard disconnect`` can never nuke real synced state."""
    host = rest[0] if rest else ""
    verr = _valid_host(host)
    if verr:
        return _err(verr)
    root = _root(data_dir)
    mirror = _mirror_path(root, host)
    if not os.path.isdir(mirror):
        return _err(f"no origin '{host}' on this box.")
    loops = origins._loop_names(mirror)
    if loops:
        return _err(f"'{host}' has {len(loops)} synced loop(s) — refusing to "
                    "delete real state. Remove them first if you really mean to.")
    marker_path = os.path.join(mirror, origins.ORIGIN_MARKER)
    if os.path.exists(marker_path):
        os.remove(marker_path)
    # Only rmdir if now genuinely empty (never force — be conservative).
    try:
        os.rmdir(mirror)
    except OSError:
        pass
    print(f"disconnected origin '{host}'")
    return 0


def cmd_origins(rest: list[str], *, data_dir: Optional[str] = None,
                now: Optional[float] = None) -> int:
    """Print the origins this box can see — the same list the web mirrors."""
    root = _root(data_dir)
    t = now if now is not None else time.time()
    origs = origins.list_origins(root, now=t)
    if not origs:
        print("no origins yet — `yard connect <host>` to add one.")
        return 0
    for o in origs:
        health = o.get("health", "?")
        extra = ""
        if o.get("address"):
            extra = f"  {o['address']}"
        print(f"  {o['id']:<16} {o.get('kind',''):<7} {health:<8} "
              f"loops={o.get('loops', 0)}{extra}")
    return 0


# ── runner: the daemon binding a fresh install dispatches loops onto (P1) ──────
_UP_OPTS = {"--slug", "--repo", "--python", "--sock", "--display"}
_UP_FLAGS = {"--no-poller"}     # A6: boolean switches (no value)


def _parse_up(rest: list[str]) -> tuple[Optional[dict], Optional[str]]:
    """Parse ``yard up`` options into a dict, or ``(None, error)``. Value opts in
    ``_UP_OPTS`` take an argument; flags in ``_UP_FLAGS`` are booleans."""
    opts: dict[str, object] = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _UP_FLAGS:
            opts[tok[2:]] = True
            i += 1
            continue
        if tok.startswith("--") and "=" in tok and tok.split("=", 1)[0] in _UP_OPTS:
            k, v = tok.split("=", 1)
            opts[k[2:]] = v
            i += 1
            continue
        if tok in _UP_OPTS:
            if i + 1 >= len(rest):
                return None, f"{tok} needs a value."
            opts[tok[2:]] = rest[i + 1]
            i += 2
            continue
        return None, f"unexpected argument {tok!r} (try `yard help`)."
    return opts, None


def _toml_str(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _project_block(slug: str, repo: str, display: str) -> str:
    """A MINIMAL, valid ``[projects.<slug>]`` entry (config.Project required
    keys) for a fresh install's local runner — no deploy targets, no Telegram
    (it's a local execution slug, not a shipped bot)."""
    return (
        f"\n[projects.{slug}]\n"
        f'slug = "{_toml_str(slug)}"\n'
        f'display_name = "{_toml_str(display)}"\n'
        f'repo_path = "{_toml_str(repo)}"\n'
        'deploy_branch = "main"\n'
        'master_branch = "main"\n'
        "deploy_targets = []\n"
        'tg_chat = ""\n')


def _seed_project(install_root: str, slug: str, repo: str, display: str) -> str:
    """Best-effort: ensure the worker daemon's ``config/projects.toml`` has a
    project for ``slug`` so ``spawn_session`` resolves it (no manual editing —
    P1a). Returns 'created' | 'exists' | 'skipped'. Never raises."""
    if not install_root:
        return "skipped"
    return _seed_project_in(os.path.join(install_root, "config"), slug, repo, display)


def _seed_project_in(config_dir: str, slug: str, repo: str, display: str) -> str:
    """:func:`_seed_project` against an explicit config dir (``yard start`` seeds
    ``paths.config_dir()``, which follows ``$LOOPYARD_HOME``)."""
    toml_path = os.path.join(config_dir, "projects.toml")
    try:
        existing = ""
        if os.path.exists(toml_path):
            with open(toml_path, encoding="utf-8") as fh:
                existing = fh.read()
            if re.search(rf"^\[projects\.{re.escape(slug)}\]", existing, re.M):
                return "exists"
        os.makedirs(config_dir, exist_ok=True)
        with open(toml_path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(_project_block(slug, repo, display))
        return "created"
    except OSError:
        return "skipped"


def _seed_secrets(install_root: str) -> str:
    """Best-effort: ensure ``config/secrets.toml`` EXISTS so the worker daemon's
    Config.load succeeds on a fresh box (it requires the file, but every key in it
    is optional — a local runner needs no telegram/bot secrets). Without this a
    truly fresh install boots DEGRADED (FileNotFoundError: secrets.toml) even
    after projects.toml is seeded — the same 'zero manual editing' promise (P1).
    Returns 'created' | 'exists' | 'skipped'. Never raises. Writes NO secrets."""
    if not install_root:
        return "skipped"
    config_dir = os.path.join(install_root, "config")
    path = os.path.join(config_dir, "secrets.toml")
    if os.path.exists(path):
        return "exists"
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Loopyard fresh install — a local runner needs no secrets.\n"
                     "# Add [telegram_bots] / [telegram_user] here only if you wire\n"
                     "# this box up to Telegram; every key is optional.\n")
        return "created"
    except OSError:
        return "skipped"


# ── A6: long-lived services (persistence + `yard status` + origin poller) ──────
def _spawn_detached(cmd: list, *, env: dict, log_path: str,
                    cwd: Optional[str] = None) -> int:
    """Start ``cmd`` as a DETACHED background process that outlives this shell
    (its own new session, so a session ending doesn't SIGHUP it). stdout+stderr
    append to ``log_path``; stdin is /dev/null. Returns the child pid. This is
    the seam tests replace to avoid real spawns. Delegates to the ONE detach
    primitive (:func:`mcp_loops.detach.spawn_detached`) so the poller, the MCP
    server, and the origin-agent all share A2's macOS+Linux new-session discipline."""
    from mcp_loops import detach
    return detach.spawn_detached(cmd, log_path=log_path, cwd=cwd, env=env)


def _probe_url(url: str, timeout: float = 1.5) -> bool:
    """Best-effort liveness probe of the MCP server: is something answering on
    its host:port? A raw TCP connect (no deps, no HTTP semantics needed — the
    streamable-HTTP endpoint 404s a bare GET, which still proves it's up)."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _server_url() -> str:
    """The URL a caller would drive this box's server on — MCP_LOOPS_URL wins,
    else built from MCP_LOOPS_HOST/PORT (the same env the server binds to)."""
    url = os.environ.get("MCP_LOOPS_URL")
    if url:
        return url
    host = os.environ.get("MCP_LOOPS_HOST", "127.0.0.1")
    port = os.environ.get("MCP_LOOPS_PORT", "8771")
    return f"http://{host}:{port}/mcp"


def _adopt_server(data_dir: Optional[str], now_val: float) -> None:
    """Record the MCP server that ``loopyard-quickstart.sh`` started (its
    ``_run/mcp-server.pid``) into the service registry, so ``yard status`` can
    report it even though yard doesn't start the server itself. Best-effort."""
    pidfile = os.path.join(services.run_dir(data_dir), "mcp-server.pid")
    try:
        with open(pidfile, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return
    port = os.environ.get("MCP_LOOPS_PORT")
    existing = services.read_all(data_dir).get("server") or {}
    # keep the original startedAt if we've seen this same pid before
    started = existing.get("startedAt", now_val) if existing.get("pid") == pid else now_val
    services.record("server", pid=pid, now=started,
                    port=int(port) if port and port.isdigit() else None,
                    log=os.path.join(services.run_dir(data_dir), "mcp-server.log"),
                    data_dir=data_dir)


def cmd_up(rest: list[str], *, data_dir: Optional[str] = None,
           now: Optional[float] = None,
           spawn: Optional[Callable[..., int]] = None) -> int:
    """Bring a runner up on THIS box: register a first-class DEFAULT slug (NOT
    'coord') so ``start_loop`` with no slug works out-of-the-box, seed the worker
    daemon's project + a minimal secrets.toml for that slug, and write the runner
    marker the loops server reads. Idempotent."""
    opts, err = _parse_up(rest)
    if err:
        return _err(err)
    now_val = now if now is not None else time.time()
    slug = opts.get("slug") or runner_registry.DEFAULT_RUNNER_SLUG
    verr = runner_registry.valid_slug(slug)
    if verr:
        return _err(verr)
    root = _root(data_dir)                       # the _loops dir
    parent = os.path.dirname(root)               # <install>/data
    install_root = os.path.dirname(parent)       # <install>
    repo = os.path.abspath(opts.get("repo") or os.getcwd())
    python = opts.get("python") or sys.executable
    sock = opts.get("sock") or os.path.join(parent, "_sock", "worker.sock")
    display = opts.get("display") or slug.capitalize()
    os.makedirs(root, exist_ok=True)
    seeded = _seed_project(install_root, slug, repo, display)
    secrets_seeded = _seed_secrets(install_root)
    flags = runner_registry.isolation_flags(python, str(paths.install_root()))
    runner_registry.attach(slug, sock=sock, repo=repo, python=python,
                           data_dir=data_dir, python_flags=flags, now=now_val)
    print(f"runner up: slug '{slug}' → repo {repo}")
    print(f"  socket: {sock}")
    print(f"  python: {python}")
    print(f"  marker: {runner_registry.marker_path(data_dir)}")
    if seeded == "created":
        print(f"  seeded config/projects.toml with project '{slug}'.")
    elif seeded == "exists":
        print(f"  project '{slug}' already in config/projects.toml — left as-is.")
    else:
        print("  (no config dir resolvable — if the worker daemon doesn't know "
              f"slug '{slug}', add it to its projects.toml.)")
    if secrets_seeded == "created":
        print("  seeded a minimal config/secrets.toml (no secrets — so the daemon "
              "boots).")
    # A6: record the server quickstart started (for `yard status`) + auto-start
    # the origin dispatch poller so a connected origin's queued requests DRAIN
    # (round-2 wired the queue but never started the poller). Idempotent.
    _adopt_server(data_dir, now_val)
    if not opts.get("no-poller"):
        # the poller must run where the mcp_loops PACKAGE lives (paths.install_root,
        # from __file__) so `python -m mcp_loops.poller` imports — NOT the
        # data-derived install root, which for an out-of-tree data dir holds no code.
        _start_poller(data_dir, root, python, str(paths.install_root()), now_val,
                      spawn=spawn or _spawn_detached)
    print("`start_loop` with no slug now dispatches onto this runner.")
    return 0


def _start_poller(data_dir: Optional[str], root: str, python: str,
                  install_root: str, now_val: float,
                  *, spawn: Callable[..., int], flags: tuple = (),
                  base_env: Optional[dict] = None) -> None:
    """Start (or re-attach to) the origin dispatch poller as a tracked, detached
    service. Idempotent: if the recorded pid is alive, leave it. The poller runs
    against the SAME data root (via $LOOPS_DATA_DIR) so it drains the right box."""
    existing = services.read_all(data_dir).get("poller")
    if existing and services.is_alive(existing.get("pid")):
        print(f"  dispatch poller already running (pid {existing['pid']}) — left as-is")
        return
    log_path = os.path.join(services.run_dir(data_dir), "poller.log")
    env = {**(os.environ if base_env is None else base_env), "LOOPS_DATA_DIR": root}
    cmd = [python, *flags, "-m", "mcp_loops.poller"]
    try:
        pid = spawn(cmd, env=env, log_path=log_path, cwd=install_root)
    except Exception as exc:  # noqa: BLE001 — never fail `yard up` on a spawn hiccup
        print(f"  note: could not start the dispatch poller ({exc}); start it "
              f"manually: LOOPS_DATA_DIR={root} {python} -m mcp_loops.poller")
        return
    services.record("poller", pid=pid, cmd=cmd, log=log_path,
                    now=now_val, data_dir=data_dir)
    print(f"  dispatch poller up (pid {pid}) — connected origins will drain")


# ── B2: `yard start` — the ONE bring-up (PHASE-B-SPEC §1.8) ────────────────────
# A Python port of loopyard-quickstart.sh §3-§6b; the quickstart now only builds
# its venv and execs this. Every external effect goes through an injectable seam
# (spawn / probe / alive / health / run / which / isolation / sleep / clock) so
# tests drive it without real processes.
_START_VALUE_OPTS = ("--hub", "--port", "--dash-port")
_START_FLAGS = ("--no-dashboard", "--resume")
# Never passed to a spawned service: the env forms of -I/-P would be inherited by
# the worker's tmux server and hence by every agent pane (spec §1.8 step 3).
_SPAWN_ENV_DROP = ("PYTHONSAFEPATH", "PYTHONNOUSERSITE",
                   "LOOPYARD_ENROLL_TOKEN", "LOOPYARD_INSTALL_TOKEN")
# Never set for the engine: LOOPS_SLUG outranks runner.json; owner Telegram is
# off for a standalone origin (D8).
_ENGINE_ENV_DROP = ("LOOPS_SLUG", "LOOPS_OWNER_BOT", "LOOPS_OWNER_CHAT")
_SOCK_MAX = 104                 # AF_UNIX sun_path cap (macOS; Linux is 108)
_READY_CEILING_S = 300.0        # absolute ceiling; death of the pid fails first
_READY_POLL_S = 0.25
START_RECORD = "start.json"


def _parse_start(rest: list[str]) -> tuple[Optional[dict], Optional[str]]:
    opts: dict[str, object] = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _START_FLAGS:
            opts[tok[2:]] = True
            i += 1
            continue
        key, eq, val = tok.partition("=")
        if key in _START_VALUE_OPTS:
            if not eq:
                if i + 1 >= len(rest):
                    return None, f"{tok} needs a value."
                val = rest[i + 1]
                i += 1
            opts[key[2:]] = val
            i += 1
            continue
        return None, f"unexpected argument {tok!r} (try `yard help`)."
    for k in ("port", "dash-port"):
        if k in opts and not str(opts[k]).isdigit():
            return None, f"--{k} must be a number."
    return opts, None


def _port_open(host: str, port: int) -> bool:
    """TCP port-open probe — the engine has no /health route, so readiness is a
    connect, never an HTTP request (spec §1.8 step 3)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _worker_health(sock: str) -> int:
    """0 = healthy, 2 = up but degraded (config not loaded), 1 = not answering.
    Same probe as the quickstart's worker_healthy()."""
    if not os.path.exists(sock):
        return 1
    try:
        import httpx
        tr = httpx.HTTPTransport(uds=sock)
        with httpx.Client(transport=tr, base_url="http://w", timeout=3) as c:
            body = c.get("/health").json()
        return 0 if body.get("ok") else 2
    except Exception:  # noqa: BLE001 — any failure is "not answering"
        return 1


def _log_tail(path: str, n: int = 15) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return ""


def _fail(msg: str, log_path: Optional[str] = None) -> int:
    print(f"yard start: {msg}", file=sys.stderr)
    if log_path:
        print(f"  log: {log_path}", file=sys.stderr)
        tail = _log_tail(log_path)
        if tail:
            print("  --- last lines ---", file=sys.stderr)
            sys.stderr.write(tail if tail.endswith("\n") else tail + "\n")
    return 3


_CLAUDE_INSTALL_HINT = ("curl -fsSL https://claude.ai/install.sh | bash, then run "
                        "`claude` once to log in (or set LOOPS_CLAUDE_BIN)")


def _missing_prereqs_message(missing: list[str],
                             platform: Optional[str] = None) -> str:
    """F3 (first-5-minutes): name EVERY missing prerequisite in one message,
    with the per-OS install line, so a bare box needs one fix-and-rerun, not
    one per tool."""
    platform = platform or sys.platform
    lines = [f"missing prerequisites: {', '.join(missing)} — install "
             f"{'it' if len(missing) == 1 else 'them'} and re-run:"]
    pkgs = [t for t in missing if t not in ("claude", "codex", "cursor-agent")]
    if pkgs:
        if platform == "darwin":
            lines.append(f"  brew install {' '.join(pkgs)}")
        else:
            lines.append(f"  sudo apt install -y {' '.join(pkgs)}   "
                         f"(Debian/Ubuntu/WSL2; macOS: brew install {' '.join(pkgs)})")
    if "claude" in missing:
        lines.append(f"  claude CLI not found — {_CLAUDE_INSTALL_HINT}")
    if "codex" in missing:
        lines.append("  install Codex CLI, then run `codex login` (or set LOOPS_CODEX_BIN)")
    if "cursor-agent" in missing:
        lines.append("  install Cursor CLI, then run `cursor-agent login` (or set LOOPS_CURSOR_BIN)")
    return "\n".join(lines)


def _resolve_claude(which: Callable[[str], Optional[str]]) -> Optional[str]:
    """D9: $LOOPS_CLAUDE_BIN > PATH > ~/.local/bin/claude, as an executable
    absolute path, or None."""
    for cand in (os.environ.get("LOOPS_CLAUDE_BIN"), which("claude"),
                 os.path.expanduser("~/.local/bin/claude")):
        if cand:
            cand = os.path.abspath(os.path.expanduser(cand))
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def _login_shell_claude(run: Callable[..., "subprocess.CompletedProcess"]) -> str:
    try:
        out = run(["bash", "-lc", "command -v claude"], capture_output=True,
                  text=True, timeout=20)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _seed_workspace(ws: str) -> str:
    """M1: ``<S>/workspace`` as a git repo on ``master`` with one empty commit,
    so the engine can provision loop worktrees on a fresh box. Returns
    'created' | 'exists'. Raises CalledProcessError on a git failure."""
    from mcp_loops import workspaces
    if workspaces.is_git_checkout(ws):
        return "exists"
    os.makedirs(ws, exist_ok=True)
    git = ["git", "-C", ws]
    subprocess.run([*git, "init", "-q", "-b", "master"], check=True,
                   capture_output=True)
    subprocess.run([*git, "-c", "user.name=loopyard", "-c",
                    "user.email=loopyard@localhost", "commit", "-q",
                    "--allow-empty", "-m", "loopyard origin workspace"],
                   check=True, capture_output=True)
    return "created"


def _spawn_env(extra: dict, drop: tuple = ()) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _SPAWN_ENV_DROP and k not in drop}
    env.update(extra)
    return env


def _wait_ready(ready: Callable[[], bool], pid: int, *,
                alive: Callable[[int], bool], sleep: Callable[[float], None],
                clock: Callable[[], float]) -> str:
    """Process-aware wait: 'ok' once ready; 'died' as soon as ``pid`` exits;
    'timeout' only at the absolute ceiling (a wedged-but-alive process)."""
    deadline = clock() + _READY_CEILING_S
    while True:
        if ready():
            return "ok"
        if not alive(pid):
            return "ok" if ready() else "died"
        if clock() >= deadline:
            return "timeout"
        sleep(_READY_POLL_S)


DESKTOP_MANAGED = "DESKTOP_MANAGED"


def _desktop_managed_refusal(verb: str) -> Optional[int]:
    """RC-7: a code tree the desktop app copied (it carries a ``DESKTOP_MANAGED``
    marker) is read-only code; its state lives in the app-data LOOPYARD_HOME the
    app passes. Run by hand without it, ``yard start`` / ``origin up`` would write
    config/data/workspace INTO the code copy — refuse (exit 2) before any write."""
    if not (paths.install_root() / DESKTOP_MANAGED).is_file():
        return None
    if os.environ.get(paths.ENV_HOME, "").strip():
        return None
    return _err(f"{verb}: this Loopyard tree is managed by the desktop app "
                f"({DESKTOP_MANAGED}); LOOPYARD_HOME must be set. Start it from the "
                f"Loopyard app, or set LOOPYARD_HOME to that app's data folder.")


def _server_version(host: str, port: int) -> dict:
    """Read an existing engine's identity without following redirects."""
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f"http://{host}:{port}/api/version", timeout=3) as response:
        info = json.load(response)
    return info if isinstance(info, dict) else {}


def cmd_start(rest: list[str], *, data_dir: Optional[str] = None,
              now: Optional[float] = None,
              spawn: Optional[Callable[..., int]] = None,
              probe: Optional[Callable[[str, int], bool]] = None,
              alive: Optional[Callable[[int], bool]] = None,
              health: Optional[Callable[[str], int]] = None,
              run: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
              which: Optional[Callable[[str], Optional[str]]] = None,
              isolation: Optional[Callable[[str, str], list]] = None,
              sleep: Optional[Callable[[float], None]] = None,
              clock: Optional[Callable[[], float]] = None,
              python: Optional[str] = None,
              user: Optional[str] = None) -> int:
    """One-command bring-up (spec §1.8, D4): preflight → config + default
    project → engine → worker → runner → poller (+ dashboard). Hub OFF unless
    ``--hub``. Idempotent: a service that already answers is left as-is.
    Exit 3 on any preflight/readiness failure, with one actionable line."""
    import shutil
    refused = _desktop_managed_refusal("start")
    if refused is not None:
        return refused
    opts, err = _parse_start(rest)
    if err:
        return _err(err)
    spawn = spawn or _spawn_detached
    probe = probe or _port_open
    alive = alive or services.is_alive
    health = health or _worker_health
    run = run or subprocess.run
    which = which or shutil.which
    isolation = isolation or runner_registry.isolation_flags
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    now_val = now if now is not None else time.time()
    python = python or sys.executable
    user = user or _current_user()

    root = str(paths.install_root())            # the code tree (cwd of every spawn)
    state = paths.state_root()                  # config/ data/ workspace/
    home_set = bool(os.environ.get(paths.ENV_HOME))
    droot = paths.resolve_data_dir(data_dir)    # <S>/data/_loops unless overridden
    data_parent = os.path.dirname(droot)
    run_dir = os.path.join(data_parent, "_run")
    log_dir = os.path.join(data_parent, "_logs")
    record_path = os.path.join(run_dir, START_RECORD)
    try:
        with open(record_path, encoding="utf-8") as fh:
            prior = json.load(fh)
    except (OSError, ValueError):
        prior = {}
    if not isinstance(prior, dict):
        prior = {}
    if opts.get("resume"):
        # install.sh step 6: restart with the shape the previous start recorded.
        if prior:
            for k in ("hub", "port", "dash-port"):
                if prior.get(k) and k not in opts:
                    opts[k] = str(prior[k])
            if prior.get("no-dashboard"):
                opts["no-dashboard"] = True
        # `yard update` recorded what was running before install.sh's `yard
        # down`: bring back exactly that stack, nothing more (S3).
        plan = _read_restore_plan(data_dir)
        if plan is not None:
            was = set(plan["running"])
            if not was & set(_STACK_SERVICES):
                print("  the local stack was not running before the update — "
                      "leaving it stopped")
                return 0
            if "dashboard" not in was:
                opts["no-dashboard"] = True
    host = "127.0.0.1"
    port = int(opts.get("port") or os.environ.get("MCP_LOOPS_PORT") or 8771)
    dash_port = int(opts.get("dash-port") or os.environ.get("LOOPYARD_DASH_PORT")
                    or 8811)
    config = str(paths.config_dir())
    workspace = str(state / "workspace")
    sock = str(state / "data" / "_sock" / f"user-{user}.sock")

    # ── 1. preflight — nothing is started until every check passes ─────────────
    missing = [tool for tool in ("tmux", "git") if not which(tool)]
    from mcp_loops import runtimes, runtime_policy
    try:
        provider_env = runtime_policy.environment()
        cli_runtime, _ = runtimes.selection()
    except ValueError as exc:
        return _fail(str(exc))
    cli_name = runtimes.RUNTIMES[cli_runtime].binary
    cli_bin = (_resolve_claude(which) if cli_runtime == "claude" else
               which(os.environ.get(f"LOOPS_{cli_runtime.upper()}_BIN") or cli_name))
    if not cli_bin:
        missing.append(cli_name)
    if missing:
        return _fail(_missing_prereqs_message(missing))
    if provider_env:
        isolation_mode = os.environ.get("LOOPS_ISOLATION", provider_env.get("LOOPS_ISOLATION", "best-effort"))
        if cli_runtime != "claude" and isolation_mode != "off":
            return _fail("Codex/Cursor cannot run inside the Claude-specific loop cage yet. "
                         "Trusted-workspace mode requires LOOPS_ISOLATION=off; "
                         "remove the conflicting isolation override or use Claude.")
        try:
            runtime_policy.check_login(cli_runtime, cli_bin, run=run)
        except ValueError as exc:
            return _fail(str(exc))
    if cli_runtime == "claude" and not _login_shell_claude(run):
        print(f"  claude not on bash login PATH; pinned LOOPS_CLAUDE_BIN={cli_bin}")
    if len(sock) >= _SOCK_MAX:
        return _fail(f"state path is too deep — the worker socket path is "
                     f"{len(sock)} chars ({sock}) but the OS caps AF_UNIX socket "
                     f"paths at ~{_SOCK_MAX}. Use a shorter install dir or "
                     f"LOOPYARD_HOME (e.g. ~/loopyard).")
    if not (droot + os.sep).startswith(str(state) + os.sep):
        print(f"  NOTE — data root {droot} is OUTSIDE the state root {state} "
              f"(LOOPS_DATA_DIR / --data-dir); loops will live there.")
    flags = isolation(python, root)
    if flags != ["-I"]:
        return _fail("this interpreter cannot import Loopyard without cwd; run "
                     "bin/yard or scripts/loopyard-quickstart.sh")

    # Check identity before creating state or attaching to an existing engine.
    if probe(host, port):
        try:
            identity = _server_version(host, port)
            matches = all(isinstance(identity.get(key), str) and identity[key]
                          and os.path.realpath(identity[key]) == os.path.realpath(expected)
                          for key, expected in (("installRoot", root), ("stateRoot", str(state))))
        except (OSError, ValueError):
            matches = False
        if not matches:
            return _fail(f"refusing to adopt server at http://{host}:{port}: "
                         "foreign or unverifiable install/state root; choose a different "
                         "--port or run yard from that server's installation")

    # ── 2. config + default project (M1) ───────────────────────────────────────
    for d in (config, droot, os.path.join(data_parent, "_sock"), run_dir, log_dir):
        os.makedirs(d, exist_ok=True)
    try:
        ws_state = _seed_workspace(workspace)
    except (OSError, subprocess.CalledProcessError) as exc:
        return _fail(f"could not create the default workspace {workspace}: {exc}")
    slug = runner_registry.DEFAULT_RUNNER_SLUG
    proj_state = _seed_project_in(config, slug, workspace, slug.capitalize())
    print(f"  config: {config} (project '{slug}' {proj_state}; workspace "
          f"{ws_state})")

    shared = {**runtime_policy.spawn_environment(python, which, provider_env),
              "LOOPS_DATA_DIR": droot, "LOOPYARD_INSTALL": root}
    shared.setdefault(f"LOOPS_{cli_runtime.upper()}_BIN", cli_bin)
    if provider_env:
        print(f"  provider: {cli_runtime}; permissions: "
              f"{'trusted workspace (roles are not enforced)' if shared.get('LOOPS_ROLE_CAPS') == '0' else 'role profiles'}")
    if home_set:
        shared[paths.ENV_HOME] = str(state)
    url = f"http://{host}:{port}/mcp"

    # ── 3. engine ──────────────────────────────────────────────────────────────
    engine_log = os.path.join(log_dir, "mcp-server.log")
    if probe(host, port):
        print(f"  server already listening on {url} — leaving it")
    else:
        env = _spawn_env({**shared, "MCP_LOOPS_HOST": host,
                          "MCP_LOOPS_PORT": str(port), "LOOPS_WORKSPACES": "1",
                          "LOOPS_DEFAULT_PROJECT": slug}, drop=_ENGINE_ENV_DROP)
        if opts.get("hub"):
            env["LOOPYARD_ORIGIN_HUB_URL"] = str(opts["hub"])
        cmd = [python, "-I", "-u", "-m", "mcp_loops.server"]
        pid = spawn(cmd, env=env, log_path=engine_log, cwd=root)
        res = _wait_ready(lambda: probe(host, port), pid, alive=alive,
                          sleep=sleep, clock=clock)
        if res != "ok":
            why = ("server process exited during startup" if res == "died" else
                   f"server pid {pid} still alive after "
                   f"{int(_READY_CEILING_S)}s — wedged startup")
            return _fail(f"server failed to listen on {url} ({why})", engine_log)
        services.record("server", pid=pid, now=now_val, port=port, cmd=cmd,
                        log=engine_log, data_dir=droot)
        print(f"  server up (pid {pid}) at {url}")

    # ── 4. worker daemon (user-worker mode) ────────────────────────────────────
    worker_log = os.path.join(log_dir, "worker.log")
    if health(sock) == 0:
        print(f"  worker daemon already healthy on {sock} — leaving it")
    else:
        if os.path.exists(sock):
            os.unlink(sock)                     # a stale/dead socket
        env = _spawn_env({**shared, "BOT_SQUAD_MODE": "user-worker",
                          "LOG_LEVEL": "warning"})
        cmd = [python, "-I", "-u", "-m", "bot_squad_worker", "--config", config]
        pid = spawn(cmd, env=env, log_path=worker_log, cwd=root)
        res = _wait_ready(lambda: health(sock) == 0, pid, alive=alive,
                          sleep=sleep, clock=clock)
        if res != "ok":
            degraded = " (the daemon is UP but DEGRADED: config could not be " \
                       "loaded)" if health(sock) == 2 else ""
            return _fail(f"worker daemon not started{degraded}; start_loop "
                         f"cannot spawn agents until it is running.", worker_log)
        services.record("worker", pid=pid, now=now_val, cmd=cmd,
                        log=worker_log, data_dir=droot)
        print(f"  worker daemon up (pid {pid}) on {sock}")

    # ── 5. runner ──────────────────────────────────────────────────────────────
    # `yard down` detaches (removes runner.json); re-attaching after a down/up or
    # an upgrade keeps the original attachedAt so runner.json is byte-identical.
    attached_at = now_val
    if runner_registry.read(droot) is None and isinstance(
            prior.get("attachedAt"), (int, float)):
        attached_at = prior["attachedAt"]
    marker = runner_registry.attach(slug, sock=sock, repo=root, python=python,
                                    data_dir=droot, python_flags=flags,
                                    now=attached_at)
    print(f"  runner '{slug}' attached → {sock}")

    # ── 6. poller + dashboard ──────────────────────────────────────────────────
    _start_poller(droot, droot, python, root, now_val, spawn=spawn,
                  flags=("-I", "-u"), base_env=_spawn_env(shared))
    dash_url = f"http://{host}:{dash_port}"
    if opts.get("no-dashboard"):
        dash_url = ""
    elif probe(host, dash_port):
        print(f"  dashboard already listening on {dash_url} — leaving it")
    else:
        dash_log = os.path.join(log_dir, "dashboard.log")
        env = _spawn_env({**shared, "MCP_LOOPS_URL": url,
                          "LOOPYARD_DASH_HOST": host,
                          "LOOPYARD_DASH_PORT": str(dash_port)})
        cmd = [python, "-I", "-u", "-m", "tracking_ui.loops_dashboard"]
        pid = spawn(cmd, env=env, log_path=dash_log, cwd=root)
        if _wait_ready(lambda: probe(host, dash_port), pid, alive=alive,
                       sleep=sleep, clock=clock) == "ok":
            services.record("dashboard", pid=pid, port=dash_port, now=now_val,
                            cmd=cmd, log=dash_log, data_dir=droot)
            print(f"  dashboard up (pid {pid}) → open {dash_url}")
        else:
            # soft: the CLI + /panel/ still work without the SPA.
            print(f"  note: dashboard failed to listen on {dash_url} — the "
                  f"+Loop UI is unavailable (log: {dash_log})")
            dash_url = ""

    # ── 8. record the shape for install.sh's restart, then print ───────────────
    shape = {"hub": opts.get("hub"), "no-dashboard": bool(opts.get("no-dashboard")),
             "port": port, "dash-port": dash_port,
             "attachedAt": marker.get("attachedAt")}
    tmp = record_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(shape, fh, indent=2)
    os.replace(tmp, record_path)
    # F8: a tarball install has its own `<root>/bin/yard` — show that, not the
    # long runtime-interpreter form.
    yard = _bundle_yard() or f"{shlex.quote(python)} -I -m mcp_loops.yard"
    print("\nLoopyard is up and ready to run agents hands-free.\n")
    if dash_url:
        print(f"  ▶ OPEN LOOPYARD:  {dash_url}{_web_app_path()}")   # /app/ when the bundle ships it (1.11)
    print(f"  server: {url}  ({'Hub ' + str(opts['hub']) if opts.get('hub') else 'Hub OFF'})")
    print(f"  data:   {droot}")
    print(f"  status: {yard} status")
    print(f"  stop:   {yard} down")
    cli = f"{shlex.quote(python)} -I -m mcp_loops.cli"
    print("\nTo drive it from this shell (from ANY directory — `-I` keeps a stray "
          "./mcp_loops off sys.path):")
    print(f"  export MCP_LOOPS_URL={shlex.quote(url)}")
    print(f"  export LOOPS_DATA_DIR={shlex.quote(droot)}")
    print(f"  {cli} loop_list '{{}}'")
    print(f"  {cli} start_loop '{{\"name\":\"<saved-loop>\"}}'")
    return 0


def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return str(os.getuid())


def _tmux_session_exists(slug: str) -> bool:
    """True if a tmux session named exactly ``slug`` is live. False on any tmux
    error or if tmux isn't installed — teardown must never fail on this."""
    try:
        r = subprocess.run(["tmux", "has-session", "-t", f"={slug}"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tmux_session_pane_cwds(slug: str) -> list[str]:
    """Working dirs of ALL panes in the exact-named tmux session, via
    ``list-panes``. Uses ``list-panes`` — NOT ``display-message`` — because the
    worker's sessions are DETACHED (``new-session -d``), and
    ``display-message -p -F '#{pane_current_path}'`` returns EMPTY for a session
    with no attached client, which made the ownership check always fail (the reap
    never fired on a real box). ``list-panes`` reads the real pane data whether or
    not a client is attached. Empty list on any tmux error / absent session."""
    try:
        # -s: EVERY pane in the session (all windows), not just the current
        # window's panes — the worker parks each agent in its own window, so a
        # foreign pane could hide in a window other than the active one.
        r = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", f"={slug}",
             "-F", "#{pane_current_path}"],
            capture_output=True, timeout=5, text=True)
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def _tmux_session_owned_by(slug: str, repo: Optional[str]) -> bool:
    """True only if the exact-named tmux session demonstrably belongs to THIS
    install — EVERY one of its panes has a working dir inside this install's
    ``repo`` (the cwd ``yard up`` records and ``_ensure_project_tmux_session``
    creates the session with; agent windows are spawned with the same ``-c``
    cwd). Conservative AND provably safe on a SHARED box: the default slug
    ``loopyard`` is non-unique and the worker REUSES a pre-existing same-named
    session, so one session can hold panes from ANOTHER install; requiring ALL
    panes to be under OUR repo guarantees no foreign pane is present before we
    kill the whole session. No repo, no readable panes, or ANY pane outside the
    repo → False (we do NOT kill). Stronger than an active-pane-only check, which
    could kill a shared session whenever our pane merely happened to be active."""
    if not repo:
        return False
    cwds = _tmux_session_pane_cwds(slug)
    if not cwds:
        return False
    try:
        rp = os.path.realpath(repo)
    except OSError:
        return False
    for cwd in cwds:
        try:
            pp = os.path.realpath(cwd)
        except OSError:
            return False
        if not (pp == rp or pp.startswith(rp + os.sep)):
            return False
    return True


def _reap_project_tmux_session(slug: str, repo: Optional[str]) -> bool:
    """Kill THIS install's per-project tmux session (the worker parks agent panes
    in a session named after the runner slug). Reaps ONLY when the session is
    proven to belong to this install (:func:`_tmux_session_owned_by`) — the exact
    slug is NOT enough because the default slug ``loopyard`` is shared across
    installs on a box and the session may have been created (and be in use) by a
    DIFFERENT install. Best-effort; returns True only if an owned session existed
    and the kill succeeded."""
    if not slug or not _tmux_session_exists(slug):
        return False
    if not _tmux_session_owned_by(slug, repo):
        return False
    try:
        r = subprocess.run(["tmux", "kill-session", "-t", f"={slug}"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_down(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """Stop the tracked services (poller, and the adopted server) by signalling
    THEIR recorded pids only — never a broad pattern (isolation rule 3) — and
    detach the runner. After this, ``start_loop`` with no slug fails CLEARLY
    ('no runner attached — run `yard up`') until brought up again."""
    # Read the attached slug + repo BEFORE detaching — the worker names its
    # per-project tmux session after the slug, and detach() clears the marker.
    marker = runner_registry.read(data_dir)
    slug = marker.get("slug") if marker else None
    repo = marker.get("repo") if marker else None
    had_worker = False
    signalled = []
    for svc in services.describe(data_dir):
        name = svc["name"]
        if name == "worker":
            had_worker = True
        if svc["state"] == services.RUNNING:
            services.stop(name, data_dir)
            signalled.append(svc["pid"])
            print(f"stopped service '{name}' (pid {svc['pid']}).")
        else:
            services.mark_stopped(name, data_dir)
    # B2: return only once the signalled pids are gone, so an immediate
    # `yard start` (install.sh's upgrade swap) never finds the OLD engine still
    # holding its port and "leaves it". Bounded; a straggler is reported.
    for pid in _await_exit(signalled):
        print(f"note: pid {pid} is still shutting down.")
    if runner_registry.detach(data_dir):
        print("runner detached — `start_loop` with no slug will now say 'no "
              "runner attached' until `yard up` again.")
    else:
        print("no runner was attached.")
    # F-C: stopping the worker daemon (pid) does NOT kill the tmux session it
    # spawned for this install's project — agent panes would linger silently.
    # Reap that ONE session so `yard down` is a clean teardown with nothing
    # orphaned. TWO safety gates, both required:
    #   1. a tracked `worker` service (a bare runner marker — tests, a never-run
    #      install — never reaches tmux at all); and
    #   2. the session is PROVEN to belong to this install (its cwd is inside our
    #      repo). The default slug `loopyard` is shared across installs on a box
    #      and the worker REUSES a same-named session, so the name alone can point
    #      at ANOTHER install's live session — ownership, not the name, is what
    #      makes the kill safe.
    if slug and had_worker:
        if _reap_project_tmux_session(slug, repo):
            print(f"reaped tmux session '{slug}' (agent panes this install "
                  f"spawned).")
        elif repo and _tmux_session_exists(slug):
            # Session exists but we did NOT prove it ours, so we left it running.
            # Say WHY honestly — don't claim "another install" when really we just
            # couldn't read its panes (which would silently orphan the user's own
            # session with a misleading reason).
            cwds = _tmux_session_pane_cwds(slug)
            if not cwds:
                print(f"note: a tmux session named '{slug}' exists but its panes' "
                      f"working dirs couldn't be read to confirm it's THIS "
                      f"install's — leaving it alone. If it's yours, close it "
                      f"with: tmux kill-session -t ={slug}")
            else:
                print(f"note: a tmux session named '{slug}' exists but not all of "
                      f"its panes are inside this install ({repo}) — leaving it "
                      f"alone (it belongs to, or is shared with, another install). "
                      f"Close it manually if it is yours.")
    return 0


def _await_exit(pids: list, timeout: float = 15.0) -> list:
    """Wait (bounded) for ``pids`` to exit; returns the ones still alive. A pid
    that is OUR un-reaped child (a zombie still answers ``kill 0``) is reaped."""
    deadline = time.monotonic() + timeout
    left = [p for p in pids if isinstance(p, int)]
    while left:
        for pid in list(left):
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            if not services.is_alive(pid):
                left.remove(pid)
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return left


def _fmt_uptime(seconds: Optional[float]) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def _bundle_yard() -> Optional[str]:
    """``<root>/bin/yard`` when this is a tarball (bundle) install — marked by
    BUNDLE.json at the install root — else None (a git clone)."""
    root = paths.install_root()
    if (root / "BUNDLE.json").is_file():
        return shlex.quote(str(root / "bin" / "yard"))
    return None


def _web_app_path() -> str:
    """The dashboard path to send a new user to: ``/app/`` when the new web app
    is built and enabled (mirrors tracking_ui.loops_dashboard._web_app_enabled,
    without importing starlette), else ``/`` — the legacy dashboard, which a
    tarball install without a built SPA still serves (F7: /app/ is a 404 there)."""
    if os.environ.get("LOOPYARD_WEB_APP", "1").strip().lower() in ("0", "false", "no"):
        return "/"
    return "/app/" if (paths.web_dist() / "index.html").is_file() else "/"


def cmd_status(rest: list[str], *, data_dir: Optional[str] = None,
               now: Optional[float] = None, probe: Optional[Callable[[str], bool]] = None) -> int:
    """Show the attached runner AND the tracked services (server + dispatch
    poller) with pid / port / liveness — the A6 answer to 'a returning user has
    nothing to talk to': running | stopped | crashed, plus a live health probe of
    the MCP server URL so the user knows if the surface actually answers.
    ``--json`` prints one machine-readable object instead (the desktop app's
    source for the dashboard port, R18)."""
    now_val = now if now is not None else time.time()
    if "--json" in rest:
        return _status_json(data_dir, now=now_val, probe=probe)
    m = runner_registry.read(data_dir)
    if m:
        print(f"runner: slug '{m.get('slug')}'  (attached)")
        if m.get("repo"):
            print(f"  repo:   {m['repo']}")
        if m.get("sock"):
            print(f"  socket: {m['sock']}")
        if m.get("python"):
            print(f"  python: {m['python']}")
    else:
        print("runner: no runner attached — run `yard up`.")

    svcs = services.describe(data_dir, now=now_val)
    print("services:")
    if not svcs:
        print("  (none tracked — `yard up` starts the dispatch poller)")
    for s in svcs:
        parts = [f"pid={s['pid']}"] if s.get("pid") else []
        if s.get("port"):
            parts.append(f"port={s['port']}")
        if s["state"] == services.RUNNING and s.get("uptime") is not None:
            parts.append(f"up={_fmt_uptime(s['uptime'])}")
        tail = ("  " + " ".join(parts)) if parts else ""
        print(f"  {s['name']:<8} {s['state']:<8}{tail}")

    url = _server_url()
    # Phase-B B2: an install brought up by `yard start` recorded its port; probe
    # THAT, not the :8771 default (which may be another install's server). No
    # start.json (every pre-B2 box) ⇒ unchanged.
    started = None
    if not (os.environ.get("MCP_LOOPS_URL") or os.environ.get("MCP_LOOPS_PORT")):
        try:
            with open(os.path.join(services.run_dir(data_dir), START_RECORD),
                      encoding="utf-8") as fh:
                started = int(json.load(fh)["port"])
            url = f"http://127.0.0.1:{started}/mcp"
        except (OSError, ValueError, KeyError, TypeError):
            started = None
    healthy = (probe or _probe_url)(url)
    # B2: name it honestly — if MCP_LOOPS_URL is unset this is the :8771 DEFAULT,
    # not a URL the user chose; a non-default install MUST export MCP_LOOPS_URL or
    # clients silently hit the host's server.
    from_env = bool(os.environ.get("MCP_LOOPS_URL"))
    src = ("$MCP_LOOPS_URL" if from_env else
           "recorded by `yard start`" if started else "DEFAULT — MCP_LOOPS_URL unset")
    print(f"server url: {url}  [{'reachable' if healthy else 'UNREACHABLE'}]  ({src})")
    bundle_yard = _bundle_yard()
    if bundle_yard:
        # F5 (first-5-minutes friction): a tarball install has no scripts/ and
        # no system python — the one command that works is its own bin/yard.
        if not from_env and not started:
            print(f"  (MCP_LOOPS_URL unset and no port recorded — `{bundle_yard} "
                  "start` records it so clients reach THIS box)")
        if not healthy:
            print(f"  (server not answering — start it: {bundle_yard} start)")
    else:
        if not from_env and not started:
            print("  (MCP_LOOPS_URL unset — clients default to :8771; export it from "
                  "scripts/loopyard-quickstart.sh so a fresh install reaches THIS box)")
        if not healthy:
            print("  (server not answering — re-run scripts/loopyard-quickstart.sh "
                  "or start it: python -m mcp_loops.server)")
    if "--check" in rest:
        # B2: install.sh's post-upgrade gate — server answering, no tracked
        # service crashed. Without --check the exit stays 0 (unchanged).
        crashed = [s["name"] for s in svcs if s["state"] == services.CRASHED]
        if crashed:
            print(f"  crashed: {', '.join(crashed)}")
        plan = _read_restore_plan(data_dir)
        need_server = plan is None or "server" in plan["running"]
        return 0 if (healthy or not need_server) and not crashed else 1
    return 0


def _status_url(data_dir: Optional[str]) -> str:
    """The server URL ``yard status`` probes: $MCP_LOOPS_URL/PORT, else the port
    ``yard start`` recorded, else the :8771 default (B2)."""
    url = _server_url()
    if not (os.environ.get("MCP_LOOPS_URL") or os.environ.get("MCP_LOOPS_PORT")):
        try:
            with open(os.path.join(services.run_dir(data_dir), START_RECORD),
                      encoding="utf-8") as fh:
                url = f"http://127.0.0.1:{int(json.load(fh)['port'])}/mcp"
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return url


def _status_json(data_dir: Optional[str], *, now: float,
                 probe: Optional[Callable[[str], bool]] = None) -> int:
    """``yard status --json`` (desktop contract C1): ``{live, dashPort, dashUrl,
    server: {url, ok}, services: [{name,pid,port,state,uptime}], version}``.
    ``live`` = the dashboard service is running with a recorded port. Exit 0
    even when nothing runs — the caller branches on the fields."""
    svcs = [{k: s.get(k) for k in ("name", "pid", "port", "state", "uptime")}
            for s in services.describe(data_dir, now=now)]
    dash = next((s for s in svcs if s["name"] == "dashboard"), None)
    port = dash.get("port") if dash else None
    try:
        dash_port = int(port) if port else None
    except (TypeError, ValueError):
        dash_port = None
    live = bool(dash and dash["state"] == services.RUNNING and dash_port)
    url = _status_url(data_dir)
    out = {
        "live": live,
        "dashPort": dash_port,
        "dashUrl": f"http://127.0.0.1:{dash_port}/app/" if dash_port else None,
        "server": {"url": url, "ok": bool((probe or _probe_url)(url))},
        "services": svcs,
        "version": (_bundle_meta() or {}).get("version") or _harness_version(),
    }
    print(json.dumps(out))
    return 0


def _bundle_meta() -> Optional[dict]:
    from mcp_loops._version import bundle_meta
    return bundle_meta()


def _version_line(meta: Optional[dict]) -> str:
    """RELEASE-PROCESS-SPEC §1.2: ``loopyard 0.1.0 (macos-arm64, 3f29224a)``
    from a bundle; ``loopyard <__version__> (source)`` from a checkout."""
    if meta is None:
        return f"loopyard {_harness_version()} (source)"
    sha = str(meta.get("gitSha") or "")[:8] or "?"
    return f"loopyard {meta.get('version') or '?'} ({meta.get('target') or '?'}, {sha})"


def cmd_version(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """``yard --version`` / ``yard version`` (B-2): the one stamped version."""
    print(_version_line(_bundle_meta()))
    return 0


RELEASE_SOURCE_FILE = "release-source.json"
_UPDATE_USAGE = ("usage: yard update [--check] [--force] [--allow-downgrade] [--token -]\n"
                 "  reads release.json from the portal/release host this box was "
                 "installed from and upgrades in place")


def _version_key(ver: str) -> Optional[tuple]:
    """Orderable key for ``1.2.3`` / ``1.2.3-rc1`` (a pre-release sorts before
    its release); ``None`` when ``ver`` is not dotted-numeric."""
    main, sep, pre = ver.partition("-")
    parts = main.split(".")
    if not ver or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts), (0, pre) if sep else (1, "")


def _is_downgrade(latest: str, current: str) -> bool:
    new, old = _version_key(latest), _version_key(current)
    return new is not None and old is not None and new < old


def _hub_download_token(data_dir: Optional[str]) -> Optional[dict]:
    """A fresh ``{portalUrl, downloadToken}`` bought with this box's enrolled
    hub ``lyd_`` device token (``POST /api/hubs/{hub_id}/download-token``), or
    ``None`` when the box is not portal-enrolled or the portal refuses."""
    from mcp_loops import hub_serve, origin_onboard
    state = (os.environ.get(origin_onboard.HUB_STATE_ENV)
             or hub_serve.default_state_dir(data_dir))
    return origin_onboard.portal_download_token(state)


def _release_source() -> tuple[str, str]:
    """``("portal"|"release", url)`` — where this box gets its releases:
    $LOOPYARD_PORTAL_URL > $LOOPYARD_RELEASE_URL > config/release-source.json
    (recorded at install/update; never holds a token) > BUNDLE.json releaseUrl.
    ``("", "")`` when nothing is configured."""
    for kind, env in (("portal", "LOOPYARD_PORTAL_URL"), ("release", "LOOPYARD_RELEASE_URL")):
        val = (os.environ.get(env) or "").strip()
        if val:
            return kind, val.rstrip("/")
    for cfg in dict.fromkeys((paths.config_dir(), paths.install_root() / "config")):
        try:
            src = json.loads((cfg / RELEASE_SOURCE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(src, dict):
            for kind, key in (("portal", "portalUrl"), ("release", "releaseUrl")):
                if isinstance(src.get(key), str) and src[key].strip():
                    return kind, src[key].strip().rstrip("/")
    base = str((_bundle_meta() or {}).get("releaseUrl") or "").strip()
    return ("release", base.rstrip("/")) if base else ("", "")


def _save_release_source(kind: str, url: str) -> None:
    """Remember the source (URL only, NO token) so the next ``yard update``
    needs no env. Best effort: a read-only config dir never fails an update."""
    cfg = paths.config_dir()
    try:
        cfg.mkdir(parents=True, exist_ok=True)
        tmp = cfg / (RELEASE_SOURCE_FILE + ".tmp")
        tmp.write_text(json.dumps({"portalUrl" if kind == "portal" else "releaseUrl": url},
                                  indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, cfg / RELEASE_SOURCE_FILE)
    except OSError:
        pass


def _release_get(url: str, token: Optional[str]) -> bytes:
    """GET ``url``; a bearer token is only ever sent in a header and never
    across a redirect (same rule as install.sh's authenticated fetch)."""
    import urllib.request
    _validate_portal_url(url)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    opener = (urllib.request.build_opener(NoRedirect) if token
              else urllib.request.build_opener())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return resp.read()


def _run_installer(script: Path, argv: list[str], env: dict) -> int:
    return subprocess.run(["sh", str(script), *argv], env=env).returncode


# `yard update` → install.sh (`yard down` … `yard start --resume`): the services
# running before the update, so the restart brings back exactly that set.
UPDATE_RESTORE = "update-restore.json"
_STACK_SERVICES = ("server", "worker", "poller", "dashboard")


def _restore_plan_path(data_dir: Optional[str]) -> str:
    return os.path.join(services.run_dir(data_dir), UPDATE_RESTORE)


def _read_restore_plan(data_dir: Optional[str]) -> Optional[dict]:
    try:
        with open(_restore_plan_path(data_dir), encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("running"), list):
        return None
    return plan


def _write_restore_plan(data_dir: Optional[str], running: list[str]) -> str:
    path = _restore_plan_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"running": sorted(running)}, fh)
    os.replace(path + ".tmp", path)
    return path


def _origin_restore_argv(root: Path, cmd) -> Optional[list[str]]:
    """The ``bin/yard origin up … --no-start`` that re-joins the Hub with the
    options of the recorded ``origin up --foreground`` daemon argv (same Hub,
    origin id, label, cert, --allow-run, --no-worker), leaving the local stack
    alone. ``None`` when ``cmd`` is not such an argv."""
    if not isinstance(cmd, list) or not all(isinstance(t, str) for t in cmd):
        return None
    at = next((i for i in range(len(cmd) - 1)
               if cmd[i] == "origin" and cmd[i + 1] == "up"), None)
    if at is None or "--hub" not in cmd[at:]:
        return None
    lead = []
    if "--data-dir" in cmd[:at]:
        i = cmd.index("--data-dir")
        lead = cmd[i:i + 2]
    tail = [t for t in cmd[at + 2:] if t != "--foreground"]
    return [str(root / "bin" / "yard"), *lead, "origin", "up", *tail, "--no-start"]


def _restore_origin(argv: list[str]) -> int:
    env = {k: v for k, v in os.environ.items()
           if k not in ("LOOPYARD_INSTALL_TOKEN", "LOOPYARD_ENROLL_TOKEN", PAIR_CODE_ENV)}
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL).returncode


def cmd_update(rest: list[str], *, data_dir: Optional[str] = None,
               get: Optional[Callable[[str, Optional[str]], bytes]] = None,
               run_installer: Optional[Callable[[Path, list[str], dict], int]] = None,
               mint: Optional[Callable[[Optional[str]], Optional[dict]]] = None,
               restore_origin: Optional[Callable[[list[str]], int]] = None) -> int:
    """``yard update``: upgrade this install IN PLACE from the release/portal it
    came from — fetch ``release.json``, compare with ``BUNDLE.json``, and when
    newer run that release's (signed-key-pinned) ``install.sh --dir <root>
    --version V``, which verifies, stops, swaps and restarts, keeping config/,
    data/ and workspace/. Portal mode reads the lyr_ download token from
    $LOOPYARD_INSTALL_TOKEN or ``--token -`` (stdin) — never argv — else an
    enrolled Hub buys a fresh one with its ``lyd_`` device token. An older
    release is refused unless ``--allow-downgrade``. The services running
    before the update are exactly the ones running after it: the local stack
    via install.sh's ``yard start --resume`` (which reads the restore plan),
    and a joined origin is re-joined to its Hub with the same options."""
    get = get or _release_get
    restore_origin = restore_origin or _restore_origin
    run_installer = run_installer or _run_installer
    mint = mint or _hub_download_token
    check = force = token_stdin = allow_downgrade = False
    args = list(rest)
    while args:
        a = args.pop(0)
        if a == "--check":
            check = True
        elif a == "--force":
            force = True
        elif a == "--allow-downgrade":
            allow_downgrade = True
        elif a == "--token=-":
            token_stdin = True
        elif a == "--token" and args[:1] == ["-"]:
            args.pop(0)
            token_stdin = True
        elif a in ("-h", "--help"):
            print(_UPDATE_USAGE)
            return 0
        elif a == "--token" or a.startswith("--token="):
            return _err("update: pass the download token on stdin (--token -) or in "
                        "LOOPYARD_INSTALL_TOKEN, never as an argument")
        else:
            return _err(_UPDATE_USAGE)
    root = paths.install_root()
    if (root / DESKTOP_MANAGED).is_file():
        return _err("update: this tree is managed by the desktop app. "
                    "Download the latest app from the portal release page and replace "
                    "the app after stopping your loops.")
    meta = _bundle_meta()
    if meta is None:
        print(f"loopyard harness {_harness_version()}")
        print("update: pull the latest harness on this box and restart the daemon.")
        return 0
    print(_version_line(meta))
    kind, base = _release_source()
    token = ""
    if kind == "portal":
        token = (sys.stdin.readline() if token_stdin
                 else os.environ.get("LOOPYARD_INSTALL_TOKEN", "")).strip()
    if kind in ("", "portal") and not token:
        # an enrolled Hub needs no pasted token: its lyd_ buys a fresh lyr_
        # from the portal it enrolled with (and that portal is its source)
        got = mint(data_dir)
        if got and (not kind or got.get("portalUrl") == base):
            kind, base, token = "portal", got["portalUrl"], got["downloadToken"]
    if not kind:
        return _err("update: no release source configured — set LOOPYARD_PORTAL_URL "
                    "(and LOOPYARD_INSTALL_TOKEN) from your portal's connect page, or "
                    "LOOPYARD_RELEASE_URL")
    from mcp_loops.release_urls import portal_release_url, release_url
    if kind == "portal":
        if not re.fullmatch(r"lyr_[A-Za-z0-9_-]+", token or ""):
            return _err("update: the portal needs a download token — copy a fresh one "
                        "from your portal (Connect machine) and pass it on stdin "
                        "(--token -) or as LOOPYARD_INSTALL_TOKEN")
        url_of = lambda name: portal_release_url(base, "latest", name)  # noqa: E731
    else:
        token = None
        url_of = lambda name: release_url(base, "latest", name)  # noqa: E731
    try:
        manifest = json.loads(get(url_of("release.json"), token))
        latest = str(manifest["version"]).removeprefix("v")
    except Exception as exc:  # noqa: BLE001 — any fetch/parse failure is one message
        return _err(f"update: could not read release.json from {base}: {exc}")
    current = str(meta.get("version") or "").removeprefix("v")
    if latest == current:
        print(f"already current: {current}")
        _save_release_source(kind, base)
        return 0
    if _is_downgrade(latest, current):
        if check:
            print(f"release {latest} is older than installed {current} (from {base})")
            return 0
        if not allow_downgrade:
            return _err(f"update: release {latest} is older than installed {current} — "
                        "refusing to downgrade (re-run with --allow-downgrade to force it)")
        print(f"downgrade: {current} -> {latest} (from {base})")
    else:
        print(f"update available: {current or '?'} -> {latest} (from {base})")
    if check:
        return 0
    try:
        script = get(url_of("install.sh"), token)
    except Exception as exc:  # noqa: BLE001
        return _err(f"update: could not download install.sh from {base}: {exc}")
    want = ((manifest.get("assets") or {}).get("install.sh") or {}).get("sha256")
    import hashlib
    if want and hashlib.sha256(script).hexdigest() != want:
        return _err("update: install.sh does not match release.json — refusing to run it")
    import tempfile
    env = {k: v for k, v in os.environ.items() if k != "LOOPYARD_ALLOW_UNSIGNED"}
    if kind == "portal":
        env.update(LOOPYARD_PORTAL_URL=base, LOOPYARD_INSTALL_TOKEN=token)
    else:
        env.pop("LOOPYARD_PORTAL_URL", None)
        env["LOOPYARD_RELEASE_URL"] = base
    argv = (["--dir", str(root), "--version", latest] + (["--force"] if force else [])
            + (["--allow-downgrade"] if allow_downgrade else []))
    registry = services.read_all(data_dir)
    running = [n for n, e in registry.items() if services.is_alive(e.get("pid"))]
    origin_argv = (_origin_restore_argv(root, registry[ORIGIN_SERVICE].get("cmd"))
                   if ORIGIN_SERVICE in running else None)
    plan = _write_restore_plan(data_dir, running)
    try:
        with tempfile.TemporaryDirectory(prefix="yard-update-") as tmp:
            path = Path(tmp) / "install.sh"
            path.write_bytes(script)
            rc = run_installer(path, argv, env)
    finally:
        try:
            os.remove(plan)
        except OSError:
            pass
    if rc == 0:
        _save_release_source(kind, base)
    elif rc == 4:
        print("update: loops are still running — stop them (or re-run with --force)",
              file=sys.stderr)
    if ORIGIN_SERVICE in running:
        entry = services.read_all(data_dir).get(ORIGIN_SERVICE) or {}
        if not services.is_alive(entry.get("pid")):
            ok = origin_argv is not None and restore_origin(origin_argv) == 0
            if not ok:
                how = (shlex.join(origin_argv) if origin_argv
                       else f"{root / 'bin' / 'yard'} origin up --hub <your Hub>")
                print("update: this box is no longer joined to its Hub (the origin "
                      f"did not come back after the update) — re-join: {how}",
                      file=sys.stderr)
                return rc or 3
    return rc


def cmd_diagnostics(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """6.2: one tarball of log tails + versions + redacted config for a bug
    report; prints its path. ``--out DIR`` picks where it lands."""
    out_dir = None
    if rest[:1] == ["--out"] and len(rest) == 2:
        out_dir = rest[1]
    elif rest:
        return _err("usage: yard diagnostics [--out DIR]")
    try:
        path = diagnostics.collect(data_dir=data_dir, out_dir=out_dir)
    except OSError as exc:
        return _err(f"could not write the diagnostics bundle: {exc}")
    print(f"diagnostics: {path}")
    print("  logs (tails), versions and config with secrets, tokens and keys "
          "redacted — look it over, then attach it to your bug report.")
    return 0


def _harness_version() -> str:
    ver = getattr(__import__("mcp_loops"), "__version__", None)
    return str(ver) if ver else "dev"


def _validate_portal_url(url: str) -> None:
    """Reject unusable portal configuration before starting or claiming."""
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if (parsed.scheme != "https" and not (
            os.environ.get("LOOPYARD_ALLOW_INSECURE") == "1"
            and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))):
        raise ValueError("portal URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid portal URL")


def _portal_post(url: str, token: str, body: dict) -> dict:
    """Send credentials only to the configured portal; never follow redirects."""
    import urllib.request
    _validate_portal_url(url)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "Loopyard/0.1"})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("invalid portal response")
    return result


def _portal_enroll(portal_url: str, enroll_token: str, *, fingerprint: str,
                   public_url: str, state_dir, post=None) -> dict:
    """Register → claim → provision, per control_plane.onboard.StubBringUp.

    enroll.json is private runtime configuration for the guarded dashboard's
    heartbeat/device-hash sync. The enrollment token is never persisted.
    """
    from control_plane.hub_guard import provision
    import secrets
    post = post or _portal_post
    portal_url = portal_url.rstrip("/")
    _validate_portal_url(portal_url)
    registered = post(portal_url + "/api/hubs/register", enroll_token,
        {"fingerprint": fingerprint, "public_url": public_url,
         "holder_origin_id": "local-" + fingerprint[:12], "state_version": 1})
    # Claim consumes the enrollment token. Check the fields needed to save a
    # resumable configuration before making that irreversible exchange.
    if (not isinstance(registered.get("id"), str) or not registered["id"]
            or type(registered.get("state_version")) is not int
            or registered["state_version"] < 0):
        raise ValueError("invalid portal registration response")
    claimed = post(portal_url + "/api/onboard/claim", enroll_token,
                   {"fingerprint": fingerprint})
    token = claimed.get("device_token")
    if not isinstance(token, str) or not token.startswith("lyd_"):
        raise ValueError("portal claim did not return a device token")
    if claimed.get("hub_id") != registered.get("id"):
        raise ValueError("portal claim returned a different hub")
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {"portal_url": portal_url, "hub_id": claimed["hub_id"],
              "device_token": token, "fingerprint": fingerprint,
              "public_url": public_url, "state_version": registered["state_version"]}
    temporary = state_dir / (".portal-" + secrets.token_hex(8))
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
            json.dump(config, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, state_dir / "enroll.json")
    finally:
        temporary.unlink(missing_ok=True)
    provision(state_dir / "dashboard-devices.json", token)
    return config


def _available_local_port(preferred: int) -> int:
    """Use the default when free; never take over another user's listener."""
    import socket
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cmd_enroll(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """Portal bundle bring-up; credentials are read only from the environment."""
    from mcp_loops import hub_serve, origin_onboard, runtime_policy
    from control_plane.hub_guard import provision, ENTER_PATH
    portal = os.environ.get("LOOPYARD_PORTAL_URL", "").rstrip("/")
    token = os.environ.get("LOOPYARD_ENROLL_TOKEN", "")
    if rest or not portal or not token:
        return _err("enroll needs LOOPYARD_PORTAL_URL and LOOPYARD_ENROLL_TOKEN; no arguments")
    try:
        _validate_portal_url(portal)
        guard_port = int(os.environ.get("LOOPYARD_GUARD_PORT", "8812"))
        if not 1 <= guard_port <= 65535:
            raise ValueError("invalid guard port")
        state = Path(os.environ.get(origin_onboard.HUB_STATE_ENV)
                     or hub_serve.default_state_dir(data_dir))
        saved = state / "enroll.json"
        if not os.environ.get("LOOPYARD_GUARD_PORT"):
            if saved.exists():
                from urllib.parse import urlsplit
                previous = json.loads(saved.read_text())
                if isinstance(previous, dict):
                    previous_url = urlsplit(previous.get("public_url", ""))
                    if previous_url.hostname in ("127.0.0.1", "localhost"):
                        guard_port = previous_url.port or guard_port
            else:
                guard_port = _available_local_port(guard_port)
        public_url = os.environ.get("LOOPYARD_PUBLIC_URL") or f"https://127.0.0.1:{guard_port}"
        from urllib.parse import urlsplit
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("LOOPYARD_PUBLIC_URL must be an HTTPS URL without credentials/query/fragment")
        start_args = ["--no-dashboard"]
        start_record = Path(paths.resolve_data_dir(data_dir)).parent / "_run" / START_RECORD
        if start_record.exists():
            start_args.append("--resume")
        elif not os.environ.get("MCP_LOOPS_PORT"):
            selected_port = _available_local_port(8771)
            if selected_port != 8771:
                start_args += ["--port", str(selected_port)]
        rc = cmd_start(start_args, data_dir=data_dir)
        if rc:
            return rc
        fingerprint = hub_serve.hub_fingerprint(str(state))
        saved = state / "enroll.json"
        if saved.exists():
            from control_plane.hub_guarded import load_enrollment
            config = load_enrollment(state)
            if (not isinstance(config, dict)
                    or any(not isinstance(config.get(key), str) or not config[key]
                           for key in ("portal_url", "fingerprint", "public_url",
                                       "hub_id", "device_token"))
                    or type(config.get("state_version")) is not int
                    or config["state_version"] < 0
                    or not config["device_token"].startswith("lyd_")):
                raise ValueError("invalid saved enrollment configuration")
            if (config["portal_url"] != portal or config["fingerprint"] != fingerprint
                    or config["public_url"] != public_url):
                raise ValueError("saved enrollment belongs to a different portal, hub or public URL")
            _portal_post(portal + "/api/hubs/" + config["hub_id"] + "/heartbeat",
                         config["device_token"], {"state_version": config["state_version"]})
            provision(state / "dashboard-devices.json", config["device_token"])
        else:
            config = _portal_enroll(portal, token, fingerprint=fingerprint,
                                   public_url=public_url, state_dir=state)
        from control_plane.dashboard_tls import ensure as ensure_dashboard_tls
        cert, key = ensure_dashboard_tls(state, parsed.hostname)
        if not any(s["name"] == "guarded-dashboard" and s["state"] == services.RUNNING
                   for s in services.describe(data_dir)):
            if _port_open("127.0.0.1", guard_port):
                raise ValueError("guard port is occupied by an untracked service")
            log = str(state / "guarded-dashboard.log")
            cmd = [sys.executable, "-I", "-u", "-m", "control_plane.hub_guarded",
                   "--state-dir", str(state), "--port", str(guard_port),
                   "--tls-cert", str(cert), "--tls-key", str(key)]
            env = _spawn_env({**runtime_policy.spawn_environment(sys.executable, shutil.which),
                              "LOOPS_DATA_DIR": paths.resolve_data_dir(data_dir)},
                             drop=("LOOPYARD_ENROLL_TOKEN", "LOOPYARD_INSTALL_TOKEN"))
            pid = _spawn_detached(cmd, env=env, log_path=log, cwd=str(paths.install_root()))
            services.record("guarded-dashboard", pid=pid, port=guard_port, now=time.time(),
                            cmd=cmd, log=log, data_dir=data_dir)
            if _wait_ready(lambda: _port_open("127.0.0.1", guard_port), pid,
                           alive=services.is_alive, sleep=time.sleep, clock=time.monotonic) != "ok":
                return _fail("guarded dashboard did not start", log)
        from urllib.parse import quote
        dashboard_link = portal + "/#/open/" + quote(config["hub_id"], safe="")
        print("Open your dashboard (sign-in is automatic through the portal): " + dashboard_link)
        if sys.platform == "darwin" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            try:
                import webbrowser
                webbrowser.open(dashboard_link)
            except Exception:
                pass  # The printed portal URL remains usable on headless machines.
        return 0
    except (OSError, ValueError, KeyError) as exc:
        # Never include request bodies or bearer credentials in diagnostics.
        from urllib.error import HTTPError
        if isinstance(exc, HTTPError):
            edge = " (edge security challenge)" if exc.headers.get("cf-mitigated") == "challenge" else ""
            print(f"yard enroll: portal HTTP {exc.code}{edge}; check portal access and enrollment expiry", file=sys.stderr)
        else:
            print(f"yard enroll: bring-up failed ({type(exc).__name__}); check portal configuration and local state", file=sys.stderr)
        return 3


def cmd_onboard(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """First-run guide (BRIEF §4): greet + explain a Loopyard team, then show the
    getting-started steps — the New loop page (describe → review the team →
    start) or the one-shot CLI driver. Local: prints the shared WELCOME + STEPS
    from mcp_loops.onboarding, so it works even before a runner is attached."""
    from mcp_loops import onboarding
    if "--json" in rest:
        # The desktop app's guide panel: the SAME payload the web Home checklist
        # reads (loop_onboarding_progress) — identity + welcome + steps/explore/
        # places with their routes + the guide prompt. Each item carries ``done``
        # read from THIS box's data dir (fail-soft: a store error ticks nothing),
        # so the panel ticks on real completion.
        try:
            root = _root(data_dir)
        except Exception:  # noqa: BLE001  (a store read never sinks the guide)
            root = None
        print(json.dumps(onboarding.progress_payload(root), ensure_ascii=False))
        return 0
    o = onboarding.onboarding_summary()
    print("\n" + o["welcome"] + "\n")
    print("Getting started:\n")
    for i, s in enumerate(o["steps"], 1):
        print(f"  {i}. {s['title']}\n     {s['what']}")
    # L2: report the ACTUAL dashboard port. Prefer the live recorded service (the
    # port the quickstart actually bound), then the env override, then the default —
    # so a partner who overrode LOOPYARD_DASH_PORT sees the right URL.
    dash_port = None
    try:
        for svc in services.describe(data_dir):
            if svc["name"] == "dashboard" and svc.get("port"):
                dash_port = str(svc["port"])
                break
    except Exception:  # noqa: BLE001  (never let a registry read sink onboarding)
        dash_port = None
    dash_port = dash_port or os.environ.get("LOOPYARD_DASH_PORT", "8811")
    # F6: a bundle box has no `python` on PATH — name the bundle's own
    # interpreter (bin/yard only proxies mcp_loops.yard, not the cli).
    cli = (f"{shlex.quote(sys.executable)} -I -m mcp_loops.cli" if _bundle_yard()
           else "python -P -m mcp_loops.cli")
    # F7: a tarball install may ship no built web app — then /app/ is a 404 and
    # the legacy dashboard at / (its "＋ Loop" button) is the way in.
    web = _web_app_path()
    new_btn = "“New loop”" if web == "/app/" else "“＋ Loop”"
    checklist = (" Home keeps a getting-started checklist that ticks as you go."
                 if web == "/app/" else "")
    print(
        "\nTwo ways to start — pick one:\n"
        f"  • In the app: open http://127.0.0.1:{dash_port}{web} → {new_btn}, "
        "describe what you want done, review the team, then Start."
        f"{checklist}\n"
        "  • One shot from the CLI (derives + saves + starts your first team):\n"
        f"      {cli} loop_onboard_first_team "
        "'{\"goal\":\"build and ship a URL shortener with tests\",\"start\":true}'\n"
        "\nEither way the team is YOURS — change the agents and the goal any "
        "time. Watch it run in Loops, then open the result and rate it.\n")
    return 0


_USAGE = (
    "yard — bring a runner up + add origins on the box; the web mirrors it.\n\n"
    "  yard runtime use <claude|codex|cursor> [--trusted-workspace]\n"
    "                                          persist provider + explicit permissions\n"
    "  yard runtime show                       show saved/effective provider\n"
    "  yard enroll                            Bring up this portal-enrolled bundle\n"
    "  yard onboard [--json]                   NEW HERE? what a Loopyard team is +\n"
    "                                          the getting-started steps (machine →\n"
    "                                          describe → start → rate the result)\n"
    "  yard up [--slug S] [--repo DIR] [--python P] [--sock PATH] [--no-poller]\n"
    "                                          attach a runner (DEFAULT slug so\n"
    "                                          `start_loop` works with no slug) +\n"
    "                                          start the origin dispatch poller\n"
    "  yard start [--hub URL] [--no-dashboard] [--port N] [--dash-port N] [--resume]\n"
    "                                          one-command bring-up: engine + worker\n"
    "                                          + runner + poller + dashboard, Hub OFF\n"
    "  yard down                               stop tracked services + detach runner\n"
    "  yard status [--check|--json]            runner + services (pid/port/health);\n"
    "                                          --check exits 1 unless healthy\n"
    "  yard connect <host> [--address ADDR]   register an origin on THIS box\n"
    "  yard disconnect <host>                  forget an origin (only if unsynced)\n"
    "  yard origins                            list origins this box can see\n"
    "  yard origin pair --hub URL --pair-code -   enroll THIS box on a Hub + pin\n"
    "                                          its cert (remote pairing; §6.2); the\n"
    "                                          code comes from stdin or $LOOPYARD_PAIR_CODE\n"
    "  yard origin up --hub URL [--pair-code -] [--dry-run]   bring THIS box up\n"
    "                                          as an origin on a remote Hub (§6.2)\n"
    "  yard origin status [--json]             live state the daemon published (§6.3)\n"
    "  yard origin allow-project <id>          opt ONE Project into remote dispatch\n"
    "                                          (default-deny; also --allow-project ID\n"
    "                                          on `origin up`)\n"
    "  yard origin deny-project <id>           take a Project back out\n"
    "  yard origin projects [--json]           Projects a Hub may start loops for\n"
    "  yard origin allow-run|deny-run <prog>   edit THIS box's origin.run exec\n"
    "                                          allowlist; `origin runs` lists it\n"
    "  yard origin bundle [--target SLUG]      plan/emit the cross-platform bundle (§6.5)\n"
    "  yard hub pair-code --hub-url wss://H:P [--state-dir D] [--label L] [--json]\n"
    "                                          HUB side: mint a pairing code + the line\n"
    "                                          to paste on the new box (P2.5)\n"
    "  yard version | yard --version           the installed Loopyard version\n"
    "  yard update [--check] [--token -]       upgrade this install in place from its portal/release\n"
    "  yard diagnostics [--out DIR]            one tarball (log tails, versions,\n"
    "                                          redacted config) for a bug report\n"
    "  yard help                               this text\n\n"
    "Neither runners nor origins carry keys or tokens — the box authenticates\n"
    "its own CLIs. Data root: $LOOPS_DATA_DIR (default <install>/data/_loops),\n"
    "or --data-dir."
)


def _origin_state_dir(data_dir: Optional[str]) -> str:
    """Where the origin client keeps its keypair + pinned Hub cert + agent
    pidfile/log: the ``_origin`` dir under the local mirror (same place the
    in-process LOCAL origin identity lives, so the box keeps ONE identity)."""
    return os.path.join(_root(data_dir), "_origin")


PAIR_CODE_ENV = "LOOPYARD_PAIR_CODE"

# `origin up` (headless) bookkeeping, all under the `_origin` state dir.
ORIGIN_PAIRED = "paired.json"   # {hub, deviceId, hubFingerprint} after a claim
ORIGIN_LOG = "origin.log"       # the detached serve daemon's stdout/stderr
ORIGIN_SERVICE = "origin"       # its name in the services registry (yard down/status)
_ORIGIN_UP_WAIT_S = 30.0
_LIVE_STATES = ("connected", "reconnecting", "connecting")

_PAIR_CODE_ON_ARGV = (
    "refusing a pairing code on the command line — argv is visible to every "
    "local user via `ps`. Pipe it in (`… --pair-code -`, one line on stdin; "
    "prompted without echo when stdin is a terminal) or set "
    f"${PAIR_CODE_ENV}.")


def _read_pair_code(opts: dict) -> Optional[str]:
    """Resolve the pairing code WITHOUT it ever touching argv (P5): ``--pair-code
    -`` reads it from stdin (a hidden prompt on a terminal), else
    ``$LOOPYARD_PAIR_CODE``. Returns None when neither is given."""
    if opts.get("pair_code_stdin"):
        if sys.stdin is not None and sys.stdin.isatty():
            import getpass
            code = getpass.getpass("pairing code: ")
        else:
            code = sys.stdin.readline() if sys.stdin is not None else ""
        return code.strip() or None
    return os.environ.get(PAIR_CODE_ENV, "").strip() or None


def _parse_origin_up(rest: list[str]) -> tuple[Optional[dict], Optional[str]]:
    """Parse ``origin up``/``origin pair`` flags → an opts dict, or (None, error).
    Flags: ``--hub URL`` (required), ``--dry-run``, ``--label L``,
    ``--origin-id ID``, ``--hub-cert PATH``, ``--no-worker``, ``--pair-code -``
    (claim a pairing code on the Hub first — the code itself is read from stdin
    or ``$LOOPYARD_PAIR_CODE``, never argv: P5), ``--hub-fingerprint FP``
    (confirm the Hub cert delivered at pairing against the value shown next to
    the code), ``--allow-project ID`` (repeatable; opt a Project into remote
    dispatch) and ``--all-projects`` (the ``*`` wildcard — loud warning)."""
    opts: dict = {"dry_run": False, "start_worker": True, "label": None,
                  "origin_id": "local", "hub": None, "hub_cert": None,
                  "pair_code_stdin": False, "hub_fingerprint": None,
                  "foreground": False, "no_start": False, "re_pair": False,
                  "allow_run": [], "allow_projects": [], "all_projects": False,
                  "wait": _ORIGIN_UP_WAIT_S, "start_args": []}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--dry-run", "-n"):
            opts["dry_run"] = True
            i += 1
        elif tok == "--no-worker":
            opts["start_worker"] = False
            i += 1
        elif tok in ("--foreground", "--no-start", "--re-pair"):
            opts[tok[2:].replace("-", "_")] = True
            i += 1
        elif tok == "--no-dashboard":
            opts["start_args"].append(tok)      # forwarded to `yard start`
            i += 1
        elif tok == "--all-projects":
            opts["all_projects"] = True
            i += 1
        elif tok == "--allow-project":
            if i + 1 >= len(rest):
                return None, f"{tok} needs a value."
            perr = _project_id_error(rest[i + 1])
            if perr:
                return None, perr
            opts["allow_projects"].append(rest[i + 1])
            i += 2
        elif tok in ("--port", "--dash-port", "--allow-run", "--wait"):
            if i + 1 >= len(rest):
                return None, f"{tok} needs a value."
            val = rest[i + 1]
            if tok == "--allow-run":
                opts["allow_run"].append(val)
            elif tok == "--wait":
                try:
                    opts["wait"] = float(val)
                except ValueError:
                    return None, "--wait must be a number of seconds."
            else:
                opts["start_args"] += [tok, val]
            i += 2
        elif tok == "--pair-code" or tok.startswith("--pair-code="):
            val = (tok.split("=", 1)[1] if "=" in tok
                   else (rest[i + 1] if i + 1 < len(rest) else None))
            if val is None:
                return None, "--pair-code needs '-' (read the code from stdin)."
            if val != "-":
                return None, _PAIR_CODE_ON_ARGV  # never echo the value back
            opts["pair_code_stdin"] = True
            i += 1 if "=" in tok else 2
        elif tok in ("--hub", "--label", "--origin-id", "--hub-cert",
                     "--hub-fingerprint"):
            if i + 1 >= len(rest):
                return None, f"{tok} needs a value."
            key = {"--hub": "hub", "--label": "label",
                   "--origin-id": "origin_id", "--hub-cert": "hub_cert",
                   "--hub-fingerprint": "hub_fingerprint"}[tok]
            opts[key] = rest[i + 1]
            i += 2
        else:
            return None, f"unknown flag {tok!r} for `origin up`."
    if not opts["hub"]:
        return None, "origin up requires --hub URL (e.g. --hub wss://host:port)."
    opts["pair_code"] = None  # resolved later by _read_pair_code, off argv
    return opts, None


def _run_pairing(client, opts: dict) -> int:
    """Run the remote pairing claim (§6.2) and print its result. Returns 0 on a
    successful enroll (+ pin), non-zero with a clear message on refusal. Shared by
    ``origin pair`` and ``origin up --pair-code``."""
    import asyncio

    from mcp_loops import origin_client
    try:
        result = asyncio.run(client.enroll(
            opts["pair_code"],
            expected_hub_fingerprint=opts.get("hub_fingerprint")))
    except origin_client.OriginUpError as e:
        return _err(e.message)
    _write_paired(client.config.state_dir, client.config.hub.describe(), result)
    print(f"paired → device {result['deviceId']}")
    print(f"  fingerprint: {result['fingerprint']}")
    if result["hubCertPinned"]:
        print(f"  hub cert pinned ({result['hubCertFingerprint']}) → "
              f"{client.pinned_hub_cert_path()}")
    else:
        print("  note: the Hub did not return its cert — pin it out of band before "
              "a non-loopback dial (--hub-cert PATH).")
    return 0


def _read_paired(state_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(state_dir, ORIGIN_PAIRED), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write_paired(state_dir: str, hub: str, result: dict) -> None:
    """Record a successful claim so a re-run of ``origin up --pair-code -`` with
    the (now consumed) code is a no-op instead of ``pairing code already used``."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    path = os.path.join(state_dir, ORIGIN_PAIRED)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"hub": hub, "deviceId": result.get("deviceId"),
                   "hubFingerprint": result.get("hubCertFingerprint"),
                   "pairedAt": time.time()}, fh, indent=2)
    os.replace(tmp, path)


def _already_paired(client) -> Optional[dict]:
    """The paired record iff it is for THIS Hub + THIS box's key and the trust
    material a dial needs is still on disk (a pinned cert for a secure Hub)."""
    rec = _read_paired(client.config.state_dir)
    ident = client.identity(create=False)
    if not rec or ident is None:
        return None
    if rec.get("hub") != client.config.hub.describe() \
            or rec.get("deviceId") != ident.device_id:
        return None
    if client.config.hub.secure and client.pinned_hub_cert() is None:
        return None
    return rec


def _origin_daemon_argv(opts: dict, *, data_dir: Optional[str], python: str,
                        flags: list, composed: bool) -> list:
    """The ``origin up --foreground`` argv the detached daemon runs — the SAME
    serve loop a human runs in a terminal (no second code path). The pairing code
    is never forwarded (the claim already happened in the parent)."""
    if getattr(sys, "frozen", False):
        argv = [sys.executable]
    else:
        argv = [python, *flags, "-u", "-m", "mcp_loops.yard"]
    if data_dir:
        argv += ["--data-dir", data_dir]
    argv += ["origin", "up", "--hub", opts["hub"], "--foreground",
             "--origin-id", opts["origin_id"]]
    if opts.get("label"):
        argv += ["--label", opts["label"]]
    if opts.get("hub_cert"):
        argv += ["--hub-cert", opts["hub_cert"]]
    if composed or not opts["start_worker"]:
        argv.append("--no-worker")   # `yard start` already runs THE worker (G9)
    for prog in opts.get("allow_run") or []:
        argv += ["--allow-run", prog]
    return argv


def _origin_fail(msg: str, log_path: Optional[str] = None) -> int:
    print(f"yard origin up: {msg}", file=sys.stderr)
    if log_path:
        print(f"  log: {log_path}", file=sys.stderr)
        tail = _log_tail(log_path)
        if tail:
            print("  --- last lines ---", file=sys.stderr)
            sys.stderr.write(tail if tail.endswith("\n") else tail + "\n")
    return 3


def _origin_up_headless(opts: dict, client, *, data_dir: Optional[str],
                        start: Optional[Callable[..., int]] = None,
                        spawn: Optional[Callable[..., int]] = None,
                        probe_hub: Optional[Callable] = None,
                        alive: Optional[Callable[[int], bool]] = None,
                        isolation: Optional[Callable[[str, str], list]] = None,
                        sleep: Optional[Callable[[float], None]] = None,
                        clock: Optional[Callable[[], float]] = None,
                        python: Optional[str] = None) -> int:
    """HUB-FABRIC P2.5 — ONE command from an installed box to a listed, live
    origin: ``yard start`` (engine + THE worker + runner; claude preflight) →
    already-up? done → Hub reachable? → pair (skipped when this box is already
    paired with this Hub) → detach ``origin up --foreground`` → wait for
    ``status.json`` = connected → exit 0. Idempotent; every failure is one clear
    line and a non-zero exit (2 = refused/config, 3 = bring-up failed)."""
    from mcp_loops import origin_client
    start = start or cmd_start
    spawn = spawn or _spawn_detached
    probe_hub = probe_hub or origin_client.probe_hub
    alive = alive or services.is_alive
    isolation = isolation or runner_registry.isolation_flags
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    python = python or sys.executable
    composed = not opts["no_start"]
    hub = client.config.hub
    state_dir = client.config.state_dir
    droot = paths.resolve_data_dir(data_dir)

    # ── 1. the local stack (Phase-B `yard start`, Hub OFF: we own the dial) ────
    if composed:
        rc = start(list(opts["start_args"]), data_dir=data_dir)
        if rc != 0:
            return rc
        # the "up and ready" banner must reach the screen BEFORE any join error
        # below (stderr is unbuffered; a piped stdout would print it last).
        sys.stdout.flush()

    # ── 2. idempotent: a live daemon for this Hub is left as-is ────────────────
    status_file = client.status_file()
    entry = services.read_all(droot).get(ORIGIN_SERVICE) or {}
    cur = status_file.read()
    if alive(entry.get("pid")) and cur and not cur.get("stale") \
            and cur.get("state") in _LIVE_STATES and cur.get("hub") == hub.describe():
        print(f"origin already up → {hub.describe()} ({cur['state']}; daemon pid "
              f"{entry['pid']}, device {cur.get('deviceId')}) — leaving it.")
        return 0

    # ── 3. the Hub must answer before anything is claimed or spawned ───────────
    why = probe_hub(hub)
    if why is not None:
        return _err(f"hub_unreachable: cannot reach the Hub at {hub.describe()} "
                    f"({why}). Check the URL/port and that the Hub is running.")

    # ── 4. pair (once) ─────────────────────────────────────────────────────────
    rec = None if opts["re_pair"] else _already_paired(client)
    if opts["pair_code"]:
        if rec is not None:
            print(f"already paired with {hub.describe()} as device "
                  f"{rec.get('deviceId')} — not re-claiming the code "
                  f"(--re-pair to force).")
        else:
            rc = _run_pairing(client, opts)
            if rc != 0:
                return rc
            client = type(client)(client.config)   # pick up the fresh pin
    try:
        mode = client.tls_mode()
    except origin_client.OriginUpError as e:
        return _err(e.message)
    if mode == "blocked":
        return _err(f"not paired: refusing to dial non-loopback Hub "
                    f"{hub.describe()} without a pinned cert — pass the code from "
                    f"the Hub: `echo CODE | yard origin up --hub {hub.describe()} "
                    f"--hub-fingerprint FP --pair-code -`.")
    client.identity()   # materialise the key before the daemon reads it
    if alive(entry.get("pid")):
        # a daemon for another Hub / wedged — ours by the registry. Replaced only
        # NOW, once the new Hub answered and pairing succeeded: a bad target must
        # never take down a working origin.
        services.stop(ORIGIN_SERVICE, droot)
        _await_exit([entry["pid"]])

    # ── 5. detach the serve daemon (the SAME `origin up --foreground`) ─────────
    root = str(paths.install_root())
    flags = [] if getattr(sys, "frozen", False) else isolation(python, root)
    argv = _origin_daemon_argv(opts, data_dir=data_dir, python=python,
                               flags=flags, composed=composed)
    log_path = os.path.join(state_dir, ORIGIN_LOG)
    try:
        os.remove(status_file.path)   # never mistake the last run's state for ours
    except OSError:
        pass
    env = _spawn_env({}, drop=(PAIR_CODE_ENV,))
    pid = spawn(argv, env=env, log_path=log_path, cwd=root)
    services.record(ORIGIN_SERVICE, pid=pid, now=time.time(), cmd=argv,
                    log=log_path, data_dir=droot)

    # ── 6. wait for `connected` (the exact state `yard origin status` renders) ─
    deadline = clock() + float(opts["wait"])
    last = None
    while True:
        cur = status_file.read()
        last = (cur or {}).get("state", last)
        if cur and cur.get("state") == "connected":
            break
        if cur and cur.get("state") == "error":
            services.stop(ORIGIN_SERVICE, droot)
            return _origin_fail(f"origin daemon errored: {cur.get('error')}",
                                log_path)
        if not alive(pid):
            services.mark_stopped(ORIGIN_SERVICE, droot)
            return _origin_fail(f"origin daemon (pid {pid}) exited before "
                                f"connecting (last state: {last or 'none'}).",
                                log_path)
        if clock() >= deadline:
            services.stop(ORIGIN_SERVICE, droot)
            return _origin_fail(f"origin did not connect to {hub.describe()} "
                                f"within {int(opts['wait'])}s (last state: "
                                f"{last or 'none'}) — stopped it.", log_path)
        sleep(0.2)
    worker = (services.read_all(droot).get("worker") or {}).get("pid") \
        if composed else cur.get("workerPid")
    print(f"origin up → {hub.describe()} ({mode})")
    print(f"  device:  {cur.get('deviceId')}")
    print(f"  daemon:  pid {pid} (log {log_path})")
    print(f"  worker:  {'pid ' + str(worker) if worker else 'none'}")
    print("  state:   connected — `yard origin status`; stop with "
          "`yard origin down` (or `yard down`).")
    return 0


def _failed_join_note(client, hub, data_dir: Optional[str]) -> str:
    """The trailing stderr line after a failed headless ``origin up`` whose
    ``yard start`` left the stack running. It must say what is TRUE now: a
    failed RE-TARGET (step 3/4 refused the new Hub before the old daemon was
    touched) leaves the box still joined to its previous Hub."""
    entry = services.read_all(paths.resolve_data_dir(data_dir)).get(ORIGIN_SERVICE) or {}
    cur = client.status_file().read() or {}
    if services.is_alive(entry.get("pid")) and not cur.get("stale") \
            and cur.get("state") in _LIVE_STATES:
        old = cur.get("hub") or "its previous Hub"
        if old == hub.describe():
            return (f"yard origin up: the local stack is still running and the "
                    f"origin daemon (pid {entry['pid']}) is still {cur['state']} "
                    f"to {old} — fix the above and re-run the same command "
                    f"(or `yard down`).")
        return (f"yard origin up: the local stack is still running and this box "
                f"is STILL joined to {old} (daemon pid {entry['pid']}, "
                f"{cur['state']}) — NOT switched to {hub.describe()}. Fix the "
                f"above and re-run the same command, or `yard origin down` to "
                f"leave {old}.")
    return ("yard origin up: the local stack is still running but NOT "
            "joined to a Hub — fix the above and re-run the same command "
            "(or `yard down`).")


def _cmd_origin_down(*, data_dir: Optional[str] = None) -> int:
    """``yard origin down`` — stop the detached serve daemon by ITS recorded pid
    (never a pattern). The worker/engine stay up; ``yard down`` stops them all."""
    droot = paths.resolve_data_dir(data_dir)
    entry = services.read_all(droot).get(ORIGIN_SERVICE)
    if not entry or not services.is_alive(entry.get("pid")):
        if entry:
            services.mark_stopped(ORIGIN_SERVICE, droot)
        print("origin down: no origin daemon running.")
        return 0
    services.stop(ORIGIN_SERVICE, droot)
    for pid in _await_exit([entry["pid"]]):
        print(f"note: pid {pid} is still shutting down.")
    print(f"origin down: stopped daemon pid {entry['pid']}.")
    return 0


def _cmd_origin_status(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """``yard origin status [--json]`` — render the live state the running
    ``origin up`` daemon published to ``status.json`` (§6.3). This is the exact
    surface the GUI polls: one engine, N faces. A pure read — no dial, no keypair.
    A hard-killed daemon leaves a STALE file; that is reported honestly rather than
    as a false ``connected``."""
    import json as _json

    from mcp_loops import origin_client
    as_json = "--json" in rest
    path = os.path.join(_origin_state_dir(data_dir), "status.json")
    status = origin_client.OriginStatusFile(path).read()
    if status is None:
        if as_json:
            print(_json.dumps({"state": "unknown"}))
        else:
            print("origin status: no daemon has run on this box "
                  "(no status.json).\n  `yard origin up --hub URL` to bring one up.")
        return 0
    if not status.get("workerPid"):
        # composed `origin up` runs with --no-worker: the box's ONE worker is the
        # one `yard start` spawned (G9) — report it rather than "none".
        wpid = (services.read_all(paths.resolve_data_dir(data_dir))
                .get("worker") or {}).get("pid")
        if wpid and services.is_alive(wpid):
            status["workerPid"] = wpid
    if as_json:
        print(_json.dumps(status))
        return 0
    stale = " (STALE — daemon likely not running)" if status.get("stale") else ""
    print(f"origin status: {status.get('state', 'unknown')}{stale}")
    print(f"  hub:     {status.get('hub')}")
    print(f"  device:  {status.get('deviceId')}")
    wp = status.get("workerPid")
    print(f"  worker:  {'pid ' + str(wp) if wp else 'none'}")
    if status.get("reason"):
        print(f"  reason:  {status['reason']}")
    if status.get("error"):
        print(f"  error:   {status['error']}")
    return 0


def _cmd_origin_run_worker(rest: list[str], *, runner=None) -> int:
    """BE the worker daemon — the frozen-bundle worker entry (§6.5).

    Inside a one-file bundle the worker cannot be launched as ``-m
    bot_squad_worker`` because ``sys.executable`` is the frozen exe, not a Python
    interpreter (see :meth:`OriginClient.worker_command`). The bundle instead
    carries ``bot_squad_worker`` as a collected module and spawns ``<exe> origin
    run-worker`` for it; this function forwards ``rest`` as the worker's own argv
    (e.g. ``--config PATH``) and runs its ``main()`` in-process, so a single
    self-contained artifact provides BOTH halves of an origin — the channel agent
    and the execution worker.

    ``runner`` is injectable so a test proves the argv forwarding + routing without
    importing/booting a real uvicorn worker."""
    if runner is None:
        # Deferred: importing the worker pulls uvicorn etc.; only the frozen
        # bundle (or a real bring-up) needs it, and it is a §6.5 hidden import.
        from bot_squad_worker.__main__ import main as runner
    saved_argv = sys.argv
    try:
        # The worker parses argv itself (argparse over `--config`/`--log-level`),
        # so present it a clean argv[0] + its own flags — not the `origin
        # run-worker` prefix that got us here.
        sys.argv = ["bot-squad-worker", *rest]
        return int(runner() or 0)
    finally:
        sys.argv = saved_argv


# ── §6.4 owner-side Project opt-in (the origin's dispatchable-Project allowlist) ─
_ALL_PROJECTS_WARNING = (
    "WARNING: --all-projects opts EVERY Project on this box into remote dispatch —\n"
    "  including loops bound to no Project. Anyone who can reach this origin through\n"
    "  the Hub can then define and start any loop here. Prefer `yard origin\n"
    "  allow-project <id>` for the one Project you mean; undo with\n"
    "  `yard origin deny-project --all-projects`.")


def _project_id_error(pid: str) -> Optional[str]:
    """Why ``pid`` is not a usable Project id for the opt-in verbs, else None."""
    if pid == "*":
        return ("refusing the '*' wildcard as a Project id — opting every Project "
                "in needs the explicit --all-projects flag.")
    if not _HOST_RE.match(pid or ""):
        return f"not a Project id: {pid!r}."
    return None


def _looks_like_glob_expansion(ids: list[str]) -> bool:
    """An unquoted ``*`` is expanded by the shell into the cwd's file names —
    opting those in would be a silent mis-grant, so refuse that exact shape."""
    if len(ids) < 2:
        return False
    try:
        names = sorted(n for n in os.listdir(os.getcwd()) if not n.startswith("."))
    except OSError:
        return False
    return sorted(ids) == names


def _cmd_origin_projects(verb: str, rest: list[str], *,
                         data_dir: Optional[str]) -> int:
    """``origin allow-project|deny-project|projects`` — edit/show THIS box's
    dispatchable-Project allowlist (``<_origin>/project-allowlist.json``, 0600).
    Local only: never dials, and the Hub has no write path to the file."""
    from mcp_loops import origin_client

    state_dir = _origin_state_dir(data_dir)
    path = origin_client.project_allowlist_path(state_dir)
    if verb == "projects":
        as_json = "--json" in rest
        extra = [t for t in rest if t != "--json"]
        if extra:
            return _err(f"unknown argument {extra[0]!r} for `origin projects`.")
        from mcp_loops.origin_proto.agent_core import ProjectAllowlist
        al = ProjectAllowlist(path)
        allowed = al.allowed()
        if as_json:
            print(json.dumps({"path": path, "allow": allowed,
                              "wildcard": al.wildcard}, indent=2))
            return 0
        if not allowed:
            print("no Projects opted into remote dispatch (default-deny: every "
                  "remote loop.start is refused).")
            print("  opt one in: yard origin allow-project <project-id>")
        else:
            print("Projects this origin lets a Hub start loops for:")
            for pid in allowed:
                print(f"  {pid}" + ("   (wildcard: EVERY Project + unbound loops)"
                                    if pid == "*" else ""))
            if al.wildcard:
                print(_ALL_PROJECTS_WARNING, file=sys.stderr)
        print(f"  file: {path}")
        return 0

    all_projects = "--all-projects" in rest
    ids = [t for t in rest if t != "--all-projects"]
    for pid in ids:
        perr = _project_id_error(pid)
        if perr:
            return _err(perr)
    if not ids and not all_projects:
        return _err(f"usage: yard origin {verb} <project-id>... | --all-projects")
    if verb == "allow-project" and _looks_like_glob_expansion(ids):
        return _err("those look like this directory's file names — an unquoted '*' "
                    "expanded by the shell. Nothing was changed. Name the Project "
                    "id(s) you mean, or use --all-projects for the wildcard.")
    if verb == "allow-project" and all_projects:
        print(_ALL_PROJECTS_WARNING, file=sys.stderr)
    if all_projects:
        ids.append("*")
    try:
        al = origin_client.open_project_allowlist(state_dir)
        for pid in ids:
            (al.allow_project if verb == "allow-project" else al.deny_project)(pid)
    except OSError as e:
        return _project_write_err(verb, path, e)
    if verb == "allow-project":
        print(f"allowed for remote dispatch: {', '.join(ids)}")
    else:
        print(f"denied for remote dispatch: {', '.join(ids)}")
    print(f"  now allowed: {', '.join(al.allowed()) or '(none — default-deny)'}")
    print(f"  file: {path} (a running origin picks this up on its next start "
          f"request)")
    return 0


def _project_write_err(verb: str, path: str, e: OSError) -> int:
    """A failed allowlist write: exit nonzero and say what disk now holds (the
    in-memory change was rolled back, so this is the truth)."""
    from mcp_loops.origin_proto.agent_core import ProjectAllowlist
    now = ProjectAllowlist(path).allowed()
    print(f"yard: could not write {path}: {e.strerror or e}. "
          f"`{verb}` did NOT take effect.", file=sys.stderr)
    print(f"  still allowed: {', '.join(now) or '(none — default-deny)'}",
          file=sys.stderr)
    return 1


RUN_ALLOWLIST_FILE = "run-allowlist.json"  # the file origin_service.build_service reads


def _cmd_origin_runs(verb: str, rest: list[str], *,
                     data_dir: Optional[str]) -> int:
    """``origin allow-run|deny-run|runs`` — edit/show THIS box's exec allowlist
    (``<_origin>/run-allowlist.json``, 0600): the programs ``origin.run`` may
    execute here. Local only; a write the disk refuses exits 1, never rc=0."""
    from mcp_loops.origin_proto.agent_core import RunAllowlist

    state_dir = _origin_state_dir(data_dir)
    path = os.path.join(state_dir, RUN_ALLOWLIST_FILE)
    if verb == "runs":
        as_json = "--json" in rest
        extra = [t for t in rest if t != "--json"]
        if extra:
            return _err(f"unknown argument {extra[0]!r} for `origin runs`.")
        al = RunAllowlist(path)
        if as_json:
            print(json.dumps({"path": path, "allow": al.allowed(),
                              "wildcard": al.wildcard}, indent=2))
            return 0
        print("programs origin.run may execute here: "
              f"{', '.join(al.allowed()) or '(none — default-deny)'}")
        print(f"  file: {path}")
        return 0

    progs = list(rest)
    if not progs or any(not p or p.startswith("-") for p in progs):
        return _err(f"usage: yard origin {verb} <program>...")
    if verb == "allow-run" and _looks_like_glob_expansion(progs):
        return _err("those look like this directory's file names — an unquoted '*' "
                    "expanded by the shell. Nothing was changed. Quote it ('*') "
                    "or name the program(s) you mean.")
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        al = RunAllowlist(path)
        for prog in progs:
            (al.allow_program if verb == "allow-run" else al.deny_program)(prog)
    except OSError as e:
        return _run_write_err(verb, path, e)
    print(f"{'allowed' if verb == 'allow-run' else 'denied'} for origin.run: "
          f"{', '.join(progs)}")
    print(f"  now allowed: {', '.join(al.allowed()) or '(none — default-deny)'}")
    print(f"  file: {path} (the local origin reads it when it starts)")
    return 0


def _run_write_err(verb: str, path: str, e: OSError) -> int:
    """A failed run-allowlist write: exit nonzero and say what disk now holds."""
    from mcp_loops.origin_proto.agent_core import RunAllowlist
    now = RunAllowlist(path).allowed()
    print(f"yard: could not write {path}: {e.strerror or e}. "
          f"`{verb}` did NOT take effect.", file=sys.stderr)
    print(f"  still allowed: {', '.join(now) or '(none — default-deny)'}",
          file=sys.stderr)
    return 1


def _apply_up_project_optins(opts: dict, state_dir: str) -> int:
    """Persist ``origin up --allow-project``/``--all-projects`` into the owner's
    allowlist file before the daemon starts (so the detached daemon, which reads
    the file, sees them too). Nonzero if the file could not be written."""
    from mcp_loops import origin_client
    ids = list(opts.get("allow_projects") or [])
    if opts.get("all_projects"):
        print(_ALL_PROJECTS_WARNING, file=sys.stderr)
        ids.append("*")
    if not ids:
        return 0
    path = origin_client.project_allowlist_path(state_dir)
    try:
        al = origin_client.open_project_allowlist(state_dir)
        for pid in ids:
            al.allow_project(pid)
    except OSError as e:
        return _project_write_err("origin up --allow-project", path, e)
    print(f"  projects: allowed for remote dispatch → {', '.join(al.allowed())}")
    return 0


def cmd_origin(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """``yard origin up --hub URL`` — bring THIS box up as an origin on a remote
    Hub: load/generate the device keypair, pin the Hub cert, and dial over an
    mTLS-bound channel (§6.2). ``--dry-run`` prints the plan (incl. the TLS gate +
    the cross-platform detach path) and makes no changes."""
    from mcp_loops import origin_client

    if not rest or rest[0] in ("-h", "--help", "help"):
        print("usage: yard origin up --hub URL [--pair-code -] "
              "[--hub-fingerprint FP] [--dry-run] [--label L] [--origin-id ID] "
              "[--hub-cert PATH] [--no-worker] [--allow-run PROG]... "
              "[--allow-project ID]... [--all-projects] [--wait S] [--re-pair] [--no-start] [--port N] [--no-dashboard] "
              "[--foreground]")
        print("       (default: headless — runs `yard start`, pairs once, detaches "
              "the origin daemon and returns when it is connected; idempotent)")
        print("       yard origin down   stop the detached origin daemon")
        print("       yard origin allow-project <project-id>...   opt a Project into "
              "remote dispatch (a Hub may then start its loops here)")
        print("       yard origin deny-project <project-id>...    take it back out")
        print("       yard origin projects [--json]   list the opted-in Projects "
              "(none = default-deny: every remote loop.start is refused)")
        print("       (`--all-projects` = the '*' wildcard: EVERY Project + unbound "
              "loops; loud warning — prefer one project id)")
        print("       yard origin allow-run <program>...   let origin.run execute "
              "a program here (persisted; '*' = any program)")
        print("       yard origin deny-run <program>...    take it back out")
        print("       yard origin runs [--json]   list the exec-allowed programs")
        print("       yard origin pair --hub URL [--pair-code -] "
              "[--hub-fingerprint FP] [--label L]")
        print(f"       (the pairing code is read from stdin with `--pair-code -`, "
              f"or from ${PAIR_CODE_ENV} — never from argv)")
        print("       yard origin status [--json]   "
              "show the live state the running daemon published (§6.3)")
        print("       yard origin bundle [--target SLUG] [--emit-spec|--emit-shim] "
              "[--out PATH]   plan/emit the self-contained bundle (§6.5)")
        print("       yard origin run-worker [WORKER ARGS...]   "
              "(frozen-bundle internal: BE the worker daemon, §6.5)")
        return 0
    verb, verb_rest = rest[0], rest[1:]

    # `origin status` reads what the running `origin up` daemon published — the
    # same status.json a GUI polls (§6.3). No dial, no keypair; a pure read.
    if verb == "status":
        return _cmd_origin_status(verb_rest, data_dir=data_dir)

    # `origin bundle` is a pure planner/spec-emitter for the cross-platform bundle
    # (§6.5) — no dial, no side effects unless --out is given. Same engine surfaced
    # from the same CLI (one engine, N faces).
    if verb == "bundle":
        from mcp_loops import origin_bundle
        return origin_bundle.main(verb_rest)

    # `origin run-worker` is the frozen-bundle worker entry (§6.5). Inside a
    # one-file bundle the worker cannot be reached as `-m bot_squad_worker`
    # (sys.executable is the exe), so OriginClient.worker_command() spawns
    # `<exe> origin run-worker` and THIS verb BECOMES the worker daemon in-process.
    # It is not meant for a human to type; it closes the BUNDLE_WORKER_NOTE loop.
    if verb == "run-worker":
        return _cmd_origin_run_worker(verb_rest)

    if verb == "down":
        return _cmd_origin_down(data_dir=data_dir)

    if verb in ("allow-project", "deny-project", "projects"):
        return _cmd_origin_projects(verb, verb_rest, data_dir=data_dir)

    if verb in ("allow-run", "deny-run", "runs"):
        return _cmd_origin_runs(verb, verb_rest, data_dir=data_dir)

    if verb not in ("up", "pair"):
        return _err(f"unknown `origin` verb {verb!r}. "
                    f"Try `yard origin up --hub URL`, `yard origin pair`, "
                    f"`yard origin status`, `yard origin projects`, "
                    f"`yard origin runs`, or `yard origin bundle`.")

    refused = _desktop_managed_refusal(f"origin {verb}")
    if refused is not None:
        return refused
    opts, err = _parse_origin_up(verb_rest)
    if err is not None:
        return _err(err)
    if verb == "pair" and not opts["pair_code_stdin"] \
            and not os.environ.get(PAIR_CODE_ENV, "").strip():
        opts["pair_code_stdin"] = True  # `origin pair` always needs a code
    opts["pair_code"] = _read_pair_code(opts)

    try:
        hub = origin_client.parse_hub_url(opts["hub"])
    except origin_client.OriginUpError as e:
        return _err(e.message)

    cfg = origin_client.OriginUpConfig(
        hub=hub, state_dir=_origin_state_dir(data_dir),
        origin_id=opts["origin_id"], label=opts["label"],
        start_worker=opts["start_worker"], hub_cert_path=opts["hub_cert"],
        extra_run_allowlist=list(opts["allow_run"]))
    client = origin_client.OriginClient(cfg)

    # `origin pair` is the standalone claim: enroll this box's key on the Hub +
    # pin the delivered Hub cert, nothing else. `origin up --pair-code` runs the
    # SAME claim first, then continues to bring the origin up.
    if verb == "pair":
        if not opts["pair_code"]:
            return _err("origin pair requires a pairing code (issue one on the "
                        f"Hub): pipe it to `--pair-code -` or set ${PAIR_CODE_ENV}.")
        return _run_pairing(client, opts)

    # §6.4 opt-ins given on `up` are the owner's decision on THIS box: persist
    # them first (a dry run only reports them).
    if not opts["dry_run"]:
        rc = _apply_up_project_optins(opts, cfg.state_dir)
        if rc != 0:
            return rc

    # Headless default (P2.5): yard start → pair once → detached daemon → wait
    # connected. `--foreground` is the daemon itself (and the GUI/terminal shape).
    if not opts["dry_run"] and not opts["foreground"]:
        rc = _origin_up_headless(opts, client, data_dir=data_dir)
        if rc != 0 and not opts["no_start"] and services.is_alive(
                (services.read_all(paths.resolve_data_dir(data_dir))
                 .get("worker") or {}).get("pid")):
            print(_failed_join_note(client, hub, data_dir), file=sys.stderr)
        return rc

    if opts["pair_code"] and not opts["dry_run"]:
        rc = _run_pairing(client, opts)
        if rc != 0:
            return rc
        # re-read the client so the freshly pinned Hub cert is picked up by the
        # dial that follows.
        client = origin_client.OriginClient(cfg)

    if opts["dry_run"]:
        plan = client.plan(create_identity=False)
        print(f"origin up (dry-run) → {plan['hub']}")
        print(f"  device:   {plan['deviceId']}")
        print(f"  tls:      {plan['tlsMode']}"
              + ("" if plan["pinnedHubCert"]
                 else f" (no pinned cert at {plan['pinnedHubCertPath']})"))
        print(f"  detach:   {plan['detachKind']}")
        print(f"  worker:   {'yes' if plan['startWorker'] else 'no'} "
              f"({' '.join(plan['workerCommand'])})")
        for step in plan["steps"]:
            print(f"    - {step}")
        for note in plan["notes"]:
            print(f"  note: {note}")
        would = list(opts["allow_projects"]) + (["*"] if opts["all_projects"] else [])
        if would:
            print(f"  projects: would allow {', '.join(would)} (not written: dry-run)")
        return 0

    # The mTLS gate is enforced FIRST so a live bring-up fails precisely if the box
    # is not yet paired (no pinned Hub cert) rather than dialing insecurely.
    try:
        mode = client.tls_mode()
    except origin_client.OriginUpError as e:
        return _err(e.message)
    if mode == "blocked":
        return _err(
            f"refusing to dial non-loopback Hub {hub.describe()} without a pinned "
            f"cert — run `yard origin pair --hub {hub.describe()}` "
            f"first (§6.2), or place the cert at {client.pinned_hub_cert_path()}.")
    # Live bring-up: run the long-lived FOREGROUND serve loop — assemble the RPC
    # executor, supervise the detached worker, dial the Hub over mTLS and
    # run_forever with reconnect until Ctrl-C / SIGTERM. This is the daemon behind
    # `origin up` (§6.2); the GUI wrapper drives the SAME engine (§6.3).
    print(f"origin up → {hub.describe()} ({mode})")
    print(f"  device:   {client.identity().device_id}")
    print(f"  worker:   {'yes' if opts['start_worker'] else 'no'}")
    print("  serving — Ctrl-C to stop.")
    try:
        result = client.run_foreground()
    except origin_client.OriginUpError as e:
        return _err(e.message)
    except KeyboardInterrupt:
        result = {"reason": "keyboard_interrupt"}
    print(f"origin down ({result.get('reason', 'stopped')}).")
    return 0


def cmd_hub(rest: list[str], *, data_dir: Optional[str] = None) -> int:
    """``yard hub pair-code`` — the HUB side of one-command onboarding (P2.5):
    mint a single-use pairing code in the Hub's enrollment store and print the
    exact line to paste on the new box. ``yard hub fingerprint`` prints the cert
    fingerprint a ``--tls`` Hub presents."""
    from mcp_loops import origin_onboard

    usage = ("usage: yard hub pair-code --hub-url wss://HOST:PORT [--state-dir D] "
             "[--label L] [--owner O] [--install-url URL] [--box-root DIR] [--json]\n"
             "       yard hub fingerprint [--state-dir D]\n"
             f"       (--state-dir defaults to ${origin_onboard.HUB_STATE_ENV} or "
             f"<data>/_hub; --hub-url to ${origin_onboard.HUB_PUBLIC_URL_ENV})")
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(usage)
        return 0
    verb, args = rest[0], rest[1:]
    if verb not in ("pair-code", "fingerprint"):
        return _err(f"unknown hub verb {verb!r}.\n{usage}")
    flags = {"--hub-url": "hub_url", "--state-dir": "state_dir", "--label": "label",
             "--owner": "owner", "--install-url": "install_url",
             "--box-root": "box_root"}
    opts: dict = {"json": False}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--json":
            opts["json"] = True
            i += 1
            continue
        name, eq, val = tok.partition("=")
        if name in flags:
            if not eq:
                if i + 1 >= len(args):
                    return _err(f"{name} needs a value.")
                val = args[i + 1]
                i += 1
            opts[flags[name]] = val
            i += 1
            continue
        return _err(f"unknown argument {tok!r}.\n{usage}")
    state_dir = opts.get("state_dir")
    if not state_dir and data_dir:
        from mcp_loops import hub_serve
        state_dir = hub_serve.default_state_dir(data_dir)

    if verb == "fingerprint":
        from mcp_loops import hub_serve
        root = origin_onboard.resolve_state_dir(state_dir)
        if not os.path.exists(origin_onboard._hub_cert_path(root)):
            return _err(f"no Hub cert under {root}: start the Hub with --tls first.")
        print(hub_serve.hub_fingerprint(root))
        return 0

    try:
        res = origin_onboard.mint_join(
            hub_url=opts.get("hub_url"), state_dir=state_dir,
            owner=opts.get("owner"), label=opts.get("label"),
            install_url=opts.get("install_url"),
            box_root=opts.get("box_root") or origin_onboard.DEFAULT_BOX_ROOT)
    except origin_onboard.MintError as e:
        return _err(f"{e.code}: {e.message}")
    if opts["json"]:
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0
    mins = max(1, int(round((res["expiresAt"] - time.time()) / 60)))
    print(f"pairing code {res['code']} for {res['hub']} "
          f"(single use, expires in ~{mins} min, owner {res['owner']})")
    if res["hubFingerprint"]:
        print(f"hub fingerprint: {res['hubFingerprint']}")
    print("paste this on the new box:")
    print(f"  {res['command']}")
    print("or, in the Loopyard desktop app, paste this join link:")
    print(f"  {res['joinLink']}")
    return 0


def cmd_runtime(rest, *, data_dir=None):
    from mcp_loops import runtimes, runtime_policy
    try:
        if rest in ([], ["show"]):
            print(json.dumps({"path": str(runtime_policy.policy_path()),
                              "saved": runtime_policy.load(),
                              "effectiveRuntime": runtimes.selection()[0]}, indent=2))
            return 0
        if (len(rest) not in (2, 3) or rest[0] != "use"
                or (len(rest) == 3 and rest[2] != "--trusted-workspace")):
            return _err("usage: yard runtime use <claude|codex|cursor> [--trusted-workspace] | show")
        if any(s["state"] == services.RUNNING for s in services.describe(data_dir)):
            return _err("stop this install with yard down before changing provider; saved settings were not changed")
        policy = runtime_policy.save(rest[1], trusted=len(rest) == 3)
        print(f"Saved {policy['runtime']} in {runtime_policy.policy_path()}. "
              "Existing Claude step choices route to this provider; explicit other providers remain unchanged.")
        if policy["permissions"] == "trusted-workspace":
            print("Trusted workspace: agents run with your user permissions, without Loopyard OS isolation or enforced manager/reviewer roles.")
        print("Sign in with the provider CLI, then run yard start. No credentials are stored here.")
        print("Suspended Codex/Cursor chats cannot resume natively in this beta; saved loop files remain available.")
        return 0
    except (ValueError, OSError) as exc:
        return _err(str(exc))


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Pull a global --data-dir out of anywhere in the args (before dispatch).
    data_dir: Optional[str] = None
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--data-dir":
            if i + 1 >= len(argv):
                return _err("--data-dir needs a value.")
            data_dir = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--data-dir="):
            data_dir = tok.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(tok)
        i += 1

    if not cleaned or cleaned[0] in ("help", "-h", "--help"):
        print(_USAGE)
        return 0

    cmd, rest = cleaned[0], cleaned[1:]
    if cmd in ("version", "--version", "-V"):
        return cmd_version(rest, data_dir=data_dir)

    bail = _reject_secrets(rest)
    if bail is not None:
        return bail

    if cmd == "enroll":
        return cmd_enroll(rest, data_dir=data_dir)
    if cmd == "runtime":
        return cmd_runtime(rest, data_dir=data_dir)
    if cmd in ("onboard", "welcome", "start-here"):
        return cmd_onboard(rest, data_dir=data_dir)
    if cmd == "up":
        return cmd_up(rest, data_dir=data_dir)
    if cmd == "start":
        return cmd_start(rest, data_dir=data_dir)
    if cmd in ("down", "detach"):
        return cmd_down(rest, data_dir=data_dir)
    if cmd == "status":
        return cmd_status(rest, data_dir=data_dir)
    if cmd == "connect":
        return cmd_connect(rest, data_dir=data_dir)
    if cmd == "disconnect":
        return cmd_disconnect(rest, data_dir=data_dir)
    if cmd in ("origins", "list", "ls"):
        return cmd_origins(rest, data_dir=data_dir)
    if cmd == "origin":
        return cmd_origin(rest, data_dir=data_dir)
    if cmd == "update":
        return cmd_update(rest, data_dir=data_dir)
    if cmd == "hub":
        return cmd_hub(rest, data_dir=data_dir)
    if cmd in ("diagnostics", "diag"):
        return cmd_diagnostics(rest, data_dir=data_dir)
    return _err(f"unknown command {cmd!r}. Try `yard help`.")


if __name__ == "__main__":
    raise SystemExit(main())
