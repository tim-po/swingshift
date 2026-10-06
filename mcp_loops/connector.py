"""Connector — the small client a user runs INSIDE their Codex App / Claude Code
to receive tasks a loop dispatches to them (CAP-2 §2b, OUTBOUND executor side).

The connector is deliberately tiny: it does not execute anything itself. It
REGISTERS the session as available, POLLS for the next task a loop dispatched,
hands that task's prompt to whoever is driving the session (the human, or the
agent the session is running), and RETURNS the result envelope. The "doing" is
the app session's job — that's the whole point: a loop hands interactive /
browser work back to a place a person (or their agent) can actually do it.

Two ways to use it:

  * As an AGENT in the app — you don't need this file at all: call the MCP tools
    directly — ``connect_register`` once, then ``connect_poll`` / (do the work) /
    ``connect_return`` each task. See ``docs/CONNECT.md``.

  * As a SCRIPT — run this module. It talks to the loops server over the same
    streamable-HTTP MCP endpoint the coordinator uses (``mcp_loops.client``):

      python -m mcp_loops.connector register my-laptop --runtime codex --cap browser
      python -m mcp_loops.connector poll     my-laptop            # claim + print one task
      python -m mcp_loops.connector return    t-0001-ab12 --connector my-laptop --summary "did it"
      python -m mcp_loops.connector run       my-laptop --runtime codex   # register+poll loop
      python -m mcp_loops.connector reset     my-laptop            # OWNER, on the server box

  Registration issues a per-connector SECRET (once); poll/return must present
  it. The CLI keeps it in ``~/.loopyard/connectors/<id>.secret`` (0600; override
  the dir with ``LOOPYARD_CONNECTOR_SECRETS``, or pass ``--secret`` /
  ``LOOPYARD_CONNECTOR_SECRET``). A lost secret is rotated by the box owner with
  ``reset`` (a local on-disk operation — never exposed over MCP).

  ``run`` drives a register→poll→execute→return loop with an injectable
  ``handler(task) -> dict`` (default echoes the prompt for a human to fill in);
  a real integration passes a handler that actually performs the task.

The client is injectable so tests exercise the whole flow against the on-disk
queue with no server or network.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Optional


def _default_client():
    from mcp_loops.client import LoopsMCP
    return LoopsMCP()


def default_secret_dir() -> str:
    return (os.environ.get("LOOPYARD_CONNECTOR_SECRETS")
            or os.path.join(os.path.expanduser("~"), ".loopyard", "connectors"))


def _secret_file(secret_dir: str, connector_id: str) -> str:
    from mcp_loops import connect
    if not connect.valid_connector_id(connector_id):
        raise ValueError(f"invalid connector id {connector_id!r}")
    return os.path.join(secret_dir, f"{connector_id}.secret")


def load_secret(secret_dir: str, connector_id: str) -> Optional[str]:
    try:
        with open(_secret_file(secret_dir, connector_id), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def save_secret(secret_dir: str, connector_id: str, secret: str) -> str:
    path = _secret_file(secret_dir, connector_id)
    os.makedirs(secret_dir, mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret + "\n")
    os.replace(path + ".tmp", path)
    return path


class Connector:
    """A registered executor session. Thin over the connect_* MCP tools. Holds
    the connector secret registration issued (``secret``); with ``secret_dir``
    set it is also loaded from / persisted to ``<secret_dir>/<id>.secret`` so a
    later process (the one-shot CLI) can present it."""

    def __init__(self, connector_id: str, *, runtime: str = "claude",
                 capabilities: Optional[list[str]] = None, client: Any = None,
                 secret: Optional[str] = None, secret_dir: Optional[str] = None):
        self.id = connector_id
        self.runtime = runtime
        self.capabilities = list(capabilities or [])
        self.client = client if client is not None else _default_client()
        self.secret_dir = secret_dir
        self.secret = secret or (load_secret(secret_dir, connector_id) if secret_dir else None)

    def register(self, meta: Optional[dict] = None) -> dict:
        res = self.client.connect_register(
            self.id, runtime=self.runtime, capabilities=self.capabilities, meta=meta,
            secret=self.secret)
        minted = res.get("secret") if isinstance(res, dict) else None
        if minted:
            self.secret = minted
            if self.secret_dir:
                save_secret(self.secret_dir, self.id, minted)
        return res

    def poll(self) -> Optional[dict]:
        """Claim the next task for this connector, or None if the queue is empty.
        Raises on a transport error (server down) — the run loop catches it."""
        res = self.client.connect_poll(self.id, secret=self.secret)
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(res["error"])
        return (res or {}).get("task")

    def return_result(self, task_id: str, *, status: str = "returned",
                      summary: str = "", artifacts: Optional[list] = None,
                      git_commit: Optional[str] = None, output: Any = None) -> dict:
        return self.client.connect_return(
            task_id, status=status, summary=summary, artifacts=artifacts,
            git_commit=git_commit, output=output, connector_id=self.id,
            secret=self.secret)

    def run(self, handler: Optional[Callable[[dict], dict]] = None, *,
            interval: float = 3.0, max_iterations: Optional[int] = None,
            sleep: Callable[[float], None] = time.sleep,
            log: Callable[[str], None] = print) -> int:
        """Register, then poll→execute→return until stopped (or ``max_iterations``
        empty+busy cycles elapse — bounded so tests terminate). ``handler(task)``
        returns a dict with any of {status, summary, artifacts, git_commit,
        output}; the default marks the task returned with the prompt echoed for a
        human to complete. Returns the number of tasks handled."""
        handler = handler or _echo_handler
        reg = self.register()
        if isinstance(reg, dict) and reg.get("error"):
            log(f"[connector {self.id}] register refused: {reg['error']}")
            return 0
        log(f"[connector {self.id}] registered ({self.runtime}, "
            f"caps={self.capabilities or '-'}); polling every {interval}s")
        handled = 0
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            try:
                task = self.poll()
            except RuntimeError as exc:                # transport/registration issue
                log(f"[connector {self.id}] poll failed: {exc}")
                sleep(interval)
                continue
            if not task:
                sleep(interval)
                continue
            log(f"[connector {self.id}] claimed {task['id']}: {task['prompt'][:80]}")
            try:
                out = handler(task) or {}
                status = out.get("status", "returned")
            except Exception as exc:                   # noqa: BLE001 — a bad handler fails the task, not the connector
                out = {"summary": f"handler error: {type(exc).__name__}: {exc}"}
                status = "failed"
            self.return_result(task["id"], status=status,
                               summary=out.get("summary", ""),
                               artifacts=out.get("artifacts"),
                               git_commit=out.get("git_commit"),
                               output=out.get("output"))
            handled += 1
            log(f"[connector {self.id}] returned {task['id']} ({status})")
        return handled


def _echo_handler(task: dict) -> dict:
    """Default handler: echo the prompt back as the output. A real integration
    replaces this with code (or a human/agent) that actually does the task."""
    return {"status": "returned",
            "summary": f"echo: {task.get('prompt', '')[:120]}",
            "output": {"prompt": task.get("prompt"), "spec": task.get("spec")}}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="mcp_loops.connector",
                                 description="CAP-2 outbound connector — receive loop-dispatched tasks in your app.")
    ap.add_argument("--secret", default=os.environ.get("LOOPYARD_CONNECTOR_SECRET"),
                    help="the connector secret (default: read from the secret dir)")
    ap.add_argument("--secret-dir", default=None,
                    help="where connector secrets are kept (default ~/.loopyard/connectors)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="register this session as a connector")
    p_reg.add_argument("connector_id")
    p_reg.add_argument("--runtime", default="claude")
    p_reg.add_argument("--cap", action="append", default=[], dest="caps",
                       help="a capability label (repeatable)")

    p_poll = sub.add_parser("poll", help="claim + print the next task (one-shot)")
    p_poll.add_argument("connector_id")

    p_ret = sub.add_parser("return", help="return the result of a claimed task")
    p_ret.add_argument("task_id")
    p_ret.add_argument("--connector", required=True, dest="connector_id",
                       help="the connector id that claimed the task")
    p_ret.add_argument("--status", default="returned", choices=["returned", "failed"])
    p_ret.add_argument("--summary", default="")
    p_ret.add_argument("--commit", default=None)
    p_ret.add_argument("--artifact", action="append", default=[], dest="artifacts")

    p_reset = sub.add_parser("reset", help="OWNER: rotate a connector's secret on this box's store")
    p_reset.add_argument("connector_id")
    p_reset.add_argument("--root", default=None,
                         help="connect store root (default: the loops server's)")

    p_appr = sub.add_parser("approve", help="OWNER: let a connector claim untargeted tasks")
    p_appr.add_argument("connector_id")
    p_appr.add_argument("--revoke", action="store_true",
                        help="withdraw approval (it keeps tasks addressed to it by id)")
    p_appr.add_argument("--root", default=None,
                        help="connect store root (default: the loops server's)")

    p_run = sub.add_parser("run", help="register + poll loop (echo handler)")
    p_run.add_argument("connector_id")
    p_run.add_argument("--runtime", default="claude")
    p_run.add_argument("--cap", action="append", default=[], dest="caps")
    p_run.add_argument("--interval", type=float, default=3.0)
    p_run.add_argument("--max-iterations", type=int, default=None)

    args = ap.parse_args(argv)
    sdir = args.secret_dir or default_secret_dir()

    def conn(cid: str, **kw) -> Connector:
        return Connector(cid, secret=args.secret, secret_dir=sdir, **kw)

    if args.cmd == "reset":
        from mcp_loops import connect
        if args.root:
            root = args.root
        else:
            from mcp_loops import server
            root = server._connect_root()
        secret = connect.reset_connector_secret(root, args.connector_id)
        path = save_secret(sdir, args.connector_id, secret)
        print(json.dumps({"ok": True, "connector": args.connector_id, "secret_file": path}))
        return 0

    if args.cmd == "approve":
        from mcp_loops import connect
        if args.root:
            root = args.root
        else:
            from mcp_loops import server
            root = server._connect_root()
        rec = connect.approve_connector(root, args.connector_id, approved=not args.revoke)
        print(json.dumps({"ok": True, "connector": args.connector_id,
                          "approved": rec["approved"]}))
        return 0

    if args.cmd == "register":
        c = conn(args.connector_id, runtime=args.runtime, capabilities=args.caps)
        res = c.register()
        if isinstance(res, dict) and res.get("secret"):
            res = {**res, "secret": "(saved to "
                   f"{_secret_file(sdir, args.connector_id)})"}
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.cmd == "poll":
        c = conn(args.connector_id)
        task = c.poll()
        print(json.dumps(task, ensure_ascii=False) if task else "(no task)")
        return 0
    if args.cmd == "return":
        c = conn(args.connector_id)   # only the claiming connector may return
        res = c.return_result(args.task_id, status=args.status, summary=args.summary,
                              git_commit=args.commit, artifacts=args.artifacts or None)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.cmd == "run":
        c = conn(args.connector_id, runtime=args.runtime, capabilities=args.caps)
        c.run(interval=args.interval, max_iterations=args.max_iterations)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
