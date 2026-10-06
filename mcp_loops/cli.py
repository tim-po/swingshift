"""CLI for the loop layer — local config commands + mcp-loops server shim.

Local (no server needed):
  python -m mcp_loops.cli validate  <config.json>
  python -m mcp_loops.cli summary   <config.json>
  python -m mcp_loops.cli render    <config.json> [out.html]
  python -m mcp_loops.cli dryrun    <config.json>

Server tools (shell shim so a coordinator TURN — headless claude, no native
MCP mid-run — can use mcp-loops; same shell-shim pattern as the other MCP CLIs
on the box). ANY server-registered
tool name works — the loop_* names AND the doc-blessed public ones
(start_loop / get_loop_status / cancel_loop / get_loop_result):
  python -m mcp_loops.cli <tool> ['<json-args>']
e.g.
  python -m mcp_loops.cli loop_list '{}'
  python -m mcp_loops.cli start_loop '{"name":"dash-polish"}'
  python -m mcp_loops.cli loop_reply '{"name":"dash-polish","reply":"ship it"}'

The server hit is $MCP_LOOPS_URL — set it so a fresh install reaches ITS OWN
server, not the host's. With MCP_LOOPS_URL UNSET the shim REFUSES (prints
{"error": "MCP_LOOPS_URL not set — …"}) rather than silently hitting the host's
:8771 default. Bad JSON args or an unreachable server print
{"error": ..., "url": ...}, never a traceback.

`render` writes the HTML block-scheme (defaults to <config>.html) and prints the
path — the coordinator then ships JSON + HTML to Telegram via tg_send_document.
`dryrun` executes the config on a fake substrate (no live sessions) and prints
the turn-by-turn trace, so you can watch the control flow before wiring agents.
Exit code is non-zero when a config fails validation.
"""

from __future__ import annotations

import json
import os
import sys

from mcp_loops.render import render_to_file
from mcp_loops.schema import summarize, validate_config


def _dryrun(raw: dict) -> int:
    from mcp_loops.runner import FakeSubstrate, LoopRunner

    res = validate_config(raw)
    if not res["ok"]:
        print(json.dumps({"error": "invalid config", "errors": res["errors"]},
                         indent=2, ensure_ascii=False))
        return 1
    sub = FakeSubstrate()  # defaults: worker completed / input satisfied / manager continue
    result = LoopRunner(raw, sub, owner_channel=lambda q: "(dry-run auto-reply)").run()
    icon = {"round": "  ", "briefing": "» ", "winddown": "◇ "}
    for e in result.events:
        kind = ""
        print(f"{icon.get(e.phase, '  ')}{e.phase:9} {e.agent:14} {e.role:14} "
              f"{e.status:16} {e.note}")
    print("─" * 60)
    print(f"ended={result.ended}  main_turns={result.turns_used}  "
          f"winddown_turns={result.winddown_turns}  retired={result.retired}")
    return 0


def _warn_if_foreign_install() -> None:
    """B3 (cwd trap): if this process loaded ``mcp_loops`` from an install OTHER
    than the one the user set up, say so LOUDLY on stderr and name both paths —
    instead of silently running a *sibling* install's CLI.

    The trap: from ``~/bot-swarm`` (or any dir with a ``mcp_loops/`` in it), a
    plain ``python -m mcp_loops.cli`` resolves the package from the cwd, not the
    user's install. ``loopyard-quickstart.sh`` exports ``LOOPYARD_INSTALL`` and
    prints a ``python -P`` invocation (``-P`` keeps the cwd off ``sys.path``, so
    the ``.pth``-registered install always wins). This check is the belt to that
    suspenders: whenever the loaded install disagrees with ``LOOPYARD_INSTALL``,
    it names exactly which tree ran and how to fix it. Best-effort — no env var,
    no warning; never raises."""
    blessed = os.environ.get("LOOPYARD_INSTALL")
    if not blessed:
        return
    try:
        import mcp_loops
        loaded = os.path.dirname(os.path.dirname(os.path.abspath(mcp_loops.__file__)))
        if os.path.realpath(loaded) != os.path.realpath(blessed):
            sys.stderr.write(
                f"mcp_loops: loaded from {loaded} but LOOPYARD_INSTALL={blessed} "
                "— you are running a DIFFERENT install than the one you set up "
                "(likely a ./mcp_loops in your cwd). Run from any dir with "
                "`python -P -m mcp_loops.cli ...` (the -P keeps cwd off sys.path), "
                f"or set PYTHONPATH={blessed}.\n")
    except Exception:  # noqa: BLE001 — identity hint must never break the CLI
        return


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _leaf_errors(exc: BaseException):
    """Yield the real causes under a transport failure: the MCP client wraps a
    refused connection as ``__cause__`` → ``ExceptionGroup`` → ``httpx.ConnectError``."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        subs = getattr(e, "exceptions", None)
        if isinstance(subs, (list, tuple)):
            stack.extend(subs)
        else:
            yield e
        stack.extend([e.__cause__, e.__context__])


def _is_unreachable(exc: BaseException) -> bool:
    """True when the server simply isn't there (refused / connect timeout) — as
    opposed to a server that answered with an error."""
    import httpx
    return any(isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout,
                              ConnectionError))
               for e in _leaf_errors(exc))


def _unreachable_hint(url: str) -> str:
    """F4 (first-5-minutes friction): say what's wrong and the one command that
    fixes it, instead of ``ExceptionGroup: unhandled errors in a TaskGroup``."""
    from mcp_loops import paths
    yard = os.path.join(str(paths.install_root()), "bin", "yard")
    return (f"cannot reach the Loopyard server at {url}: is it running? "
            f"Start it with `{yard} start` (check with `{yard} status`); if it "
            "is running elsewhere, set MCP_LOOPS_URL to that server's /mcp URL.")


def _mcp_shim(tool: str, argv: list[str]) -> int:
    """Forward ANY server-registered tool call to the mcp-loops server.

    Routes generically (LoopsMCP.call) so the doc-blessed public names
    (start_loop/get_loop_status/cancel_loop/get_loop_result) work exactly like
    the loop_* names — no per-tool wrapper. Fail-soft: a bad JSON arg or an
    unreachable server prints ``{"error": ..., "url": ...}`` (never a traceback),
    and the URL actually hit is always surfaced so a fresh user can see whether
    they reached their own install or the host's."""
    from mcp_loops.client import LoopsMCP, LoopsMCPError, LoopsURLUnset, url_unset_hint

    # B2: refuse to silently hit the host's :8771 when MCP_LOOPS_URL is unset —
    # print a copy-paste fix instead of quietly talking to the wrong server.
    try:
        client = LoopsMCP(require_env=True)
    except LoopsURLUnset:
        print(json.dumps({"error": url_unset_hint()}))
        return 1
    if argv:
        try:
            args = json.loads(argv[0])
        except (ValueError, TypeError) as e:
            print(json.dumps({"error": f"invalid JSON args for {tool!r}: {e}",
                              "url": client.url}))
            return 1
        if not isinstance(args, dict):
            print(json.dumps({"error": f"args for {tool!r} must be a JSON object",
                              "url": client.url}))
            return 1
    else:
        args = {}
    try:
        result = client.call(tool, args)
    except LoopsMCPError as e:
        msg = _unreachable_hint(client.url) if _is_unreachable(e) else str(e)
        print(json.dumps({"error": msg, "url": client.url}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


# The four LOCAL commands that operate on a config FILE without a server. Any
# other first arg is treated as a server tool NAME and routed through the shim —
# so start_loop/get_loop_status/cancel_loop/get_loop_result (and every loop_*
# name) reach the server instead of dying as an "unknown command"/FileNotFound.
_LOCAL_CMDS = {"validate", "summary", "render", "dryrun"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _warn_if_foreign_install()
    if not argv:
        print(__doc__)
        return 2
    if argv[0] not in _LOCAL_CMDS:
        # Not a local file command → a server tool call (loop_* or a public name).
        return _mcp_shim(argv[0], argv[1:])
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    try:
        raw = _load(path)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"cannot load {path}: {type(e).__name__}: {e}"}))
        return 1

    if cmd == "validate":
        res = validate_config(raw)
        print(json.dumps({"ok": res["ok"], "errors": res["errors"],
                          "warnings": res["warnings"]}, indent=2, ensure_ascii=False))
        return 0 if res["ok"] else 1

    if cmd == "summary":
        res = validate_config(raw)
        print(json.dumps(summarize(res["config"]), indent=2, ensure_ascii=False))
        return 0 if res["ok"] else 1

    if cmd == "dryrun":
        return _dryrun(raw)

    if cmd == "render":
        out = argv[2] if len(argv) > 2 else os.path.splitext(path)[0] + ".html"
        render_to_file(raw, out)
        res = validate_config(raw)
        status = "ok" if res["ok"] else f"DRAFT ({len(res['errors'])} errors)"
        print(json.dumps({"rendered": out, "status": status,
                          "errors": res["errors"]}, ensure_ascii=False))
        return 0 if res["ok"] else 1

    print(json.dumps({"error": f"unknown command {cmd!r}"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
