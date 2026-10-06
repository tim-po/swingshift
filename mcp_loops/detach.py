"""detach — A2: spawn a long-lived service that OUTLIVES its launching shell.

The bug (pilot-b cold-start report): ``scripts/loopyard-quickstart.sh`` started
the MCP server + worker daemon with a bare ``nohup … &``. ``nohup`` only ignores
SIGHUP — the child stays in the launching shell's SESSION and process group, so
when the parent is a long-lived interactive shell (Claude Code / a tmux pane
under a session / a CI step) that shell's teardown takes the child with it: the
server + poller die and in-flight loops wedge (run.json.state==running, no
finish-report). The worker daemon survived only because it re-execs itself into
its own session; the server/poller did not.

The fix is the shell analogue of ``start_new_session=True`` (which
``yard._spawn_detached`` already uses correctly for the poller): put the child in
its OWN new session so it is a session leader, detached from the launcher's
controlling terminal + process group. ``setsid(1)`` would do it on Linux but is
NOT in the base macOS system, and the quickstart must work on macOS AND Linux —
so we detach in Python (``os.setsid`` via ``subprocess`` ``start_new_session``,
present on both) and expose it as a tiny CLI the shell calls.

WINDOWS (HUB-FABRIC §6.2) — ``start_new_session`` is a POSIX session-leader move
(``os.setsid``) and is a **no-op on Windows**, which has no session model. The
cross-platform origin client (``loopyard origin up``) brings the agent + worker up
headless on Windows too, so it needs a Windows-native detach: spawn with
``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` so the child has no console and is
in its own process group — the SCM/service-free equivalent of a session leader,
surviving the launching console's teardown. :func:`spawn_detached` selects the
right path by platform; :func:`platform_detach_kind` names which one runs (so the
choice is observable + unit-testable without spawning a real Windows process).

Usage (from the quickstart)::

    SERVER_PID=$("$PY" -m mcp_loops.detach --log LOG --pidfile PID -- \\
                    "$PY" -m mcp_loops.server)

It spawns the command fully detached (new session, stdin=/dev/null, stdout+stderr
appended to LOG, close_fds), writes the child's pid to PIDFILE and to stdout, and
exits 0 immediately. The child now survives the launching shell.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional


def platform_detach_kind(platform: Optional[str] = None) -> str:
    """Name the detach strategy for a platform (``sys.platform`` by default):
    ``"windows"`` for a Windows process-group/no-console spawn, ``"posix"`` for
    the ``os.setsid`` session-leader spawn used on Linux + macOS. Pure + testable
    so the OS-specific branch below is asserted without spawning a real process on
    the other OS."""
    plat = sys.platform if platform is None else platform
    return "windows" if plat.startswith("win") else "posix"


def _detach_popen_kwargs() -> dict:
    """The platform-specific ``Popen`` kwargs that make the child outlive the
    launching shell/console. POSIX: ``start_new_session`` (os.setsid → session
    leader). Windows: ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` creationflags
    (no inherited console, own process group) since ``start_new_session`` is a
    no-op there (§6.2)."""
    if platform_detach_kind() == "windows":
        # DETACHED_PROCESS: no console inherited/created. NEW_PROCESS_GROUP: the
        # child leads its own group so a Ctrl-Break to the launcher's group does
        # not reach it. Together these are the Windows analogue of a new session.
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flags}
    return {"start_new_session": True}  # os.setsid in the child → its own session


def spawn_detached(cmd: list[str], *, log_path: str,
                   cwd: Optional[str] = None,
                   env: Optional[dict] = None) -> int:
    """Start ``cmd`` fully detached from the caller's controlling terminal/console
    + process group, so it outlives the launching shell on macOS, Linux **and**
    Windows (§6.2). stdin is /dev/null; stdout+stderr append to ``log_path``.
    Returns the child pid. This is the single detach primitive the server / worker
    / origin-agent bring-up all funnel through; the OS-specific mechanism is chosen
    by :func:`platform_detach_kind`."""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    logf = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — the child owns it
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            close_fds=True,
            **_detach_popen_kwargs(),
        )
    finally:
        # the child dup'd the fd; the parent can drop its copy.
        logf.close()
    return proc.pid


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_loops.detach",
        description="Spawn a command fully detached so it outlives this shell.")
    parser.add_argument("--log", required=True, help="append stdout+stderr here")
    parser.add_argument("--pidfile", help="write the child pid here")
    parser.add_argument("--cwd", help="working directory for the child")
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="-- COMMAND ARGS… (everything after -- is the child)")
    args = parser.parse_args(argv)

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("no command given (use: … -- CMD ARGS)")

    pid = spawn_detached(cmd, log_path=args.log, cwd=args.cwd)
    if args.pidfile:
        try:
            with open(args.pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(pid) + "\n")
        except OSError as e:
            print(f"detach: could not write pidfile {args.pidfile}: {e}",
                  file=sys.stderr)
    print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
