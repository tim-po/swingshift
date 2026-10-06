"""Web Terminal backend (QoL R4 / T1) — a gate-authed PTY over a WebSocket.

A real shell on THIS box (the LOCAL origin) reachable from the dashboard's Terminal
surface. This is the biggest attack surface in the product (arbitrary shell over the
web), so the posture is deliberately narrow — see docs/TERMINAL.md:

  * GATE-AUTHED, Tim-only. The WS is NOT a gate-exempt route: gate.build_application()
    verifies the same `dash_sess` cookie on the handshake and REFUSES an unauthed /
    bad-cookie connect BEFORE this handler ever runs (pre-accept close -> 403). This
    module assumes it is only reached for an authenticated session.
  * LOCAL origin only. `?origin=` other than "local" is rejected this round.
  * One PTY per socket, a max concurrent-terminals CAP, and an idle timeout.
  * Clean teardown: on disconnect / close / child-exit we kill ONLY this socket's own
    child process group (its own session) and close the master fd — no orphan pids,
    no leaked fds, never a broad pattern.
  * Audited: open/close is logged (who, when, why).

Wire contract (authoritative in docs/TERMINAL.md), matching the T2 front-end:
  client -> server  BINARY = raw stdin bytes ; TEXT = JSON {"type":"resize","cols","rows"}
  server -> client  BINARY = raw stdout bytes ; TEXT = JSON {"type":"hello"|"exit"|"error", ...}
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import struct
import sys
import termios
import time
from urllib.parse import urlsplit

from starlette.websockets import WebSocket

# ── bounds (the security envelope) ───────────────────────────────────────────
MAX_TERMINALS = int(os.environ.get("WEBTERM_MAX", "4"))     # concurrent ptys, whole process
IDLE_TIMEOUT_S = int(os.environ.get("WEBTERM_IDLE_S", str(15 * 60)))  # no client INPUT -> reap
READ_CHUNK = 65536
_WATCH_TICK_S = 15          # idle-watchdog resolution
_KILL_GRACE_S = 2.0        # SIGHUP -> wait -> SIGKILL

# concurrent-terminal counter. asyncio is single-threaded, so a plain int mutated
# without an await between read and write is race-free.
_active = 0
_seq = 0                    # per-open id for the audit trail


# ── handshake Origin check (loopyard-bug-1790562043) ─────────────────────────
# WebSockets are exempt from the same-origin policy, so without this ANY web page
# the user visits could open ws://127.0.0.1:<port>/ws/terminal and drive a shell
# (cross-site WebSocket hijack). A browser always sends Origin on the handshake and
# page script cannot forge it, so we require one that is ours. Checked BEFORE
# ws.accept() so a refused handshake never spawns anything (close pre-accept -> 403).
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _allowed_origins() -> set[str]:
    """Extra trusted origins, e.g. the gate's public host when it differs from the
    Host the dashboard sees: LOOPYARD_DASH_ALLOWED_ORIGINS="https://a.example,..."."""
    raw = os.environ.get("LOOPYARD_DASH_ALLOWED_ORIGINS", "")
    return {o for o in (_norm_origin(x) for x in raw.split(",")) if o}


def _norm_origin(value: str) -> str:
    """'HTTPS://Host:443/' -> 'https://host' ; '' on anything unparseable."""
    try:
        u = urlsplit(value.strip())
        host, port = u.hostname, u.port
    except ValueError:
        return ""
    scheme = (u.scheme or "").lower()
    if not scheme or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port and port != _DEFAULT_PORT.get(scheme):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _netloc(scheme: str, hostport: str) -> tuple[str, int | None]:
    """('https', 'Host:443') -> ('host', None) — default ports dropped."""
    try:
        u = urlsplit(f"//{hostport.strip()}")
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return "", None
    if port == _DEFAULT_PORT.get(scheme):
        port = None
    return host, port


def _origin_allowed(ws: WebSocket, strict_host: bool = False) -> bool:
    """True iff the handshake's Origin is this dashboard's own (or allowlisted).

    * explicit allowlist (LOOPYARD_DASH_ALLOWED_ORIGINS) -> ok
    * a loopback page origin (the dashboard itself, or the Vite dev proxy on another
      loopback port) -> ok
    * Origin host:port == the request's Host (same-origin; covers the gate on its
      public host) -> ok. With ``strict_host`` (the standalone, un-gated dashboard)
      the Host must additionally be loopback / the bind host / allowlisted, so a
      DNS-rebound attacker name that resolves to 127.0.0.1 is still refused.
    Missing / "null" / unparseable Origin -> refused.
    """
    origin = ws.headers.get("origin", "")
    norm = _norm_origin(origin) if origin and origin != "null" else ""
    if not norm:
        return False
    allow = _allowed_origins()
    if norm in allow:
        return True
    scheme = norm.split("://", 1)[0]
    o_host, o_port = _netloc(scheme, norm.split("://", 1)[1])
    if o_host.strip("[]") in _LOOPBACK:
        return True
    h_host, h_port = _netloc(scheme, ws.headers.get("host", ""))
    if not h_host or (o_host, o_port) != (h_host, h_port):
        return False
    if not strict_host:
        return True
    bind = os.environ.get("LOOPYARD_DASH_HOST", "127.0.0.1").lower()
    allow_hosts = {_netloc(a.split("://", 1)[0], a.split("://", 1)[1])[0] for a in allow}
    return h_host.strip("[]") in _LOOPBACK or h_host == bind or h_host in allow_hosts


def _audit(event: str, **fields) -> None:
    """Structured open/close audit line (journald-captured, QoL-1 observability
    style — never logs terminal CONTENT, only lifecycle facts)."""
    rec = {"webterm": event, "ts": round(time.time(), 3), **fields}
    print("[webterm] " + json.dumps(rec, sort_keys=True), flush=True)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 1000))
    cols = max(1, min(int(cols), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _sane_cwd() -> str:
    for c in (os.environ.get("HOME"), "/tmp"):
        if c and os.path.isdir(c):
            return c
    return os.getcwd()


_TMUX_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _session_argv(session: str, runtime: str, target: str = "") -> list[str]:
    """The command a PTY runs for a given session kind. ``shell`` (default) is a
    login-interactive shell — the Phase-2a web terminal. ``creator`` runs the
    QoL-R5 loop-creator launcher (mcp_loops.creator_launch): it prints the seeded
    creator preprompt then hands off to an interactive session. The creator
    module is invoked via THIS interpreter so it resolves the same install.
    ``attach`` is the §rd-sessions "Open" verb — it attaches THIS web terminal to a
    live local tmux session named ``target`` (exact match). Honest by construction:
    if no such tmux session exists tmux prints its own "can't find session" and the
    pane drops to a shell with a clear note — we never fake a takeover of a session
    that isn't really there."""
    shell = os.environ.get("SHELL") or "/bin/bash"
    if session == "creator":
        return [sys.executable, "-m", "mcp_loops.creator_launch",
                "--runtime", runtime]
    if session == "attach" and target and _TMUX_TARGET_RE.match(target):
        # Attach to the exact tmux session; on failure, say so honestly and drop
        # to an interactive shell so the pane is still usable (never a blank hang).
        note = (f'echo "[no live tmux session \'{target}\' on this box — '
                f'it may be an app session, not a local terminal]"')
        return [shell, "-lc",
                f'tmux attach-session -t "={target}" || ({note}; exec "{shell}" -i)']
    return [shell, "-i"]


def _creator_env() -> dict:
    """Extra env for the creator session so its child imports the SAME mcp_loops
    the dashgate is running — not a stale installed copy that predates this
    feature. Puts the running mcp_loops' install root on PYTHONPATH (harmless in
    prod where it's already importable; essential in a worktree/test run)."""
    try:
        import mcp_loops
        root = os.path.dirname(os.path.dirname(os.path.abspath(mcp_loops.__file__)))
    except Exception:  # noqa: BLE001
        return {}
    existing = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": root + ((os.pathsep + existing) if existing else "")}


async def _spawn_pty(rows: int, cols: int, argv: list[str] | None = None,
                     extra_env: dict | None = None):
    """openpty + an interactive process in its OWN session (setsid) so its pgid
    == child pid and we can later kill exactly (and only) this session's group.
    ``argv`` defaults to a login-interactive shell; the '+loop' creator session
    passes the creator-launcher argv (and ``extra_env``) instead. Returns
    (proc, master_fd, pgid)."""
    master_fd, slave_fd = os.openpty()
    _set_winsize(master_fd, rows, cols)

    def _child_setup():
        os.setsid()                                   # own session + process group
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)  # slave = controlling tty
        except OSError:
            pass

    if not argv:
        argv = [os.environ.get("SHELL") or "/bin/bash", "-i"]
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env.pop("PROMPT_COMMAND", None)
    if extra_env:
        env.update(extra_env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            preexec_fn=_child_setup, cwd=_sane_cwd(), env=env, close_fds=True,
        )
    finally:
        os.close(slave_fd)                            # parent keeps only the master
    os.set_blocking(master_fd, False)
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = proc.pid
    return proc, master_fd, pgid


async def _teardown(proc, master_fd, pgid, loop) -> None:
    """Kill ONLY this socket's own child group, reap it, close the master fd.
    Idempotent and exception-safe."""
    try:
        loop.remove_reader(master_fd)
    except (OSError, ValueError):
        pass
    if proc.returncode is None:
        try:
            os.killpg(pgid, signal.SIGHUP)            # own pgid ONLY — never broad
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
        except asyncio.TimeoutError:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
            except asyncio.TimeoutError:
                pass
    try:
        os.close(master_fd)
    except OSError:
        pass


async def terminal_ws(ws: WebSocket, *, strict_host: bool = False) -> None:
    """PTY-over-WebSocket endpoint. Auth is enforced at the gate layer BEFORE this
    runs (unauthed handshakes never reach here). Local origin only. The handshake
    Origin must be this dashboard's own (see _origin_allowed) — refused pre-accept."""
    global _active, _seq
    if not _origin_allowed(ws, strict_host=strict_host):
        _audit("refused", reason="origin", origin=ws.headers.get("origin", "")[:200],
               host=ws.headers.get("host", "")[:200],
               peer=ws.client.host if ws.client else "?")
        await ws.close(code=1008)                     # pre-accept -> HTTP 403
        return
    origin = ws.query_params.get("origin", "local")
    if origin != "local":
        await ws.accept()
        await ws.send_text(json.dumps({"type": "error", "message": "only the local origin is supported"}))
        await ws.close(code=1008)
        return

    # session kind: "shell" (Phase-2a web terminal) | "creator" (QoL-R5 '+loop'
    # interactive loop-creator) | "attach" (§rd-sessions "Open" — attach to a live
    # local tmux session named ``target``). runtime picks the CLI the creator launches.
    session = ws.query_params.get("session", "shell")
    if session not in ("shell", "creator", "attach"):
        session = "shell"
    runtime = ws.query_params.get("runtime", "claude")
    if runtime not in ("claude", "codex", "cursor"):
        runtime = "claude"
    # tmux target for the "attach" kind — sanitized; anything unsafe falls back to a
    # plain shell rather than a fabricated attach (honest, never an injected command).
    target = ws.query_params.get("target", "")
    if session == "attach" and not _TMUX_TARGET_RE.match(target or ""):
        session = "shell"

    peer = ws.client.host if ws.client else "?"
    if _active >= MAX_TERMINALS:
        await ws.accept()
        await ws.send_text(json.dumps({"type": "error", "message": "too many terminals open — try again shortly"}))
        await ws.close(code=1013)
        _audit("refused", reason="cap", cap=MAX_TERMINALS, peer=peer)
        return

    await ws.accept()
    _seq += 1
    tid = _seq
    loop = asyncio.get_running_loop()
    last_input = [time.monotonic()]

    try:
        proc, master_fd, pgid = await _spawn_pty(
            rows=24, cols=80, argv=_session_argv(session, runtime, target),
            extra_env=_creator_env() if session == "creator" else None)
    except Exception as e:  # noqa: BLE001 — surface the failure to the client, don't 500 silently
        await ws.send_text(json.dumps({"type": "error", "message": f"could not start a shell: {e}"}))
        await ws.close(code=1011)
        _audit("error", id=tid, peer=peer, error=str(e))
        return

    _active += 1
    _audit("open", id=tid, peer=peer, pid=proc.pid, active=_active,
           session=session, runtime=(runtime if session == "creator" else None))
    exit_code = None

    # pty -> client: add_reader pushes readable chunks into a queue; a sender task
    # awaits the queue and relays. b"" is the EOF sentinel (child exited / pty closed).
    out_q: asyncio.Queue = asyncio.Queue()

    def _on_readable():
        try:
            data = os.read(master_fd, READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""                                # EIO on Linux when the child is gone
        out_q.put_nowait(data)
        if not data:
            try:
                loop.remove_reader(master_fd)
            except (OSError, ValueError):
                pass

    loop.add_reader(master_fd, _on_readable)

    async def _pump_out():
        while True:
            data = await out_q.get()
            if not data:                              # EOF (child exited / pty closed)
                return
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001 — client vanished mid-write; end cleanly
                return

    async def _pump_in():
        while True:
            msg = await ws.receive()
            t = msg["type"]
            if t == "websocket.disconnect":
                return
            b = msg.get("bytes")
            if b is not None:
                last_input[0] = time.monotonic()
                try:
                    os.write(master_fd, b)            # raw stdin
                except OSError:                       # pty gone (EIO) — child died
                    return
                continue
            text = msg.get("text")
            if text is None:
                continue
            try:
                ctl = json.loads(text)
            except (ValueError, TypeError):
                continue
            if ctl.get("type") == "resize":
                last_input[0] = time.monotonic()
                try:
                    _set_winsize(master_fd, ctl.get("rows", 24), ctl.get("cols", 80))
                except OSError:
                    pass

    async def _watchdog():
        tick = max(0.2, min(_WATCH_TICK_S, IDLE_TIMEOUT_S))   # responsive to a low cap
        while True:
            await asyncio.sleep(tick)
            if time.monotonic() - last_input[0] > IDLE_TIMEOUT_S:
                return "idle"

    reason = "closed"
    try:
        await ws.send_text(json.dumps({"type": "hello", "cols": 80, "rows": 24}))
        out_t = asyncio.create_task(_pump_out())
        in_t = asyncio.create_task(_pump_in())
        watch_t = asyncio.create_task(_watchdog())
        tasks = [out_t, in_t, watch_t]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # attribute the end reason to whichever pump/watchdog finished first
        if watch_t in done and not watch_t.cancelled() and watch_t.result() == "idle":
            reason = "idle"
        elif in_t in done:
            reason = "client_gone"
        elif out_t in done:
            reason = "child_exited"
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # retrieve any exception a finished pump raised (dead socket/pty) so it is
        # not reported as "Task exception was never retrieved".
        for t in done:
            if not t.cancelled():
                t.exception()
    except Exception as e:  # noqa: BLE001
        reason = "error"
        _audit("pump_error", id=tid, error=str(e))
    finally:
        exit_code = proc.returncode
        await _teardown(proc, master_fd, pgid, loop)
        # tell the client the session is over (best-effort; the socket may be gone)
        try:
            await ws.send_text(json.dumps({"type": "exit", "code": proc.returncode}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        _active -= 1
        _audit("close", id=tid, peer=peer, pid=proc.pid,
               exit_code=exit_code, reason=reason, active=_active)


async def terminal_ws_local(ws: WebSocket) -> None:
    """The standalone (no gate, no cookie) dashboard's /ws/terminal: same handler,
    plus the DNS-rebinding Host check — the loopback bind is its only other guard."""
    await terminal_ws(ws, strict_host=True)
