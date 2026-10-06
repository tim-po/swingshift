"""Coordinator side of the Mac-origin control channel.

Enqueue a shell command for the Mac agent to run, and block for its result.
Usage (from the VPS):
    python -m tracking_ui.mac_origin_ctl run "uname -a"
    python -m tracking_ui.mac_origin_ctl send "long thing &"     # fire-and-forget
    python -m tracking_ui.mac_origin_ctl log                     # last results
    python -m tracking_ui.mac_origin_ctl stop                    # tell the agent to exit
The queue files live under data/_mac_origin/<active-token>/.
"""
from __future__ import annotations

import json
import sys
import time

from tracking_ui.mac_origin import ROOT, _active_token, _tdir


def _cmds_path(d):
    return d / "cmds.jsonl"


def _enqueue(cmd: str, timeout: int = 900) -> int:
    tok = _active_token()
    if not tok:
        print("no active token (data/_mac_origin/token missing)", file=sys.stderr)
        sys.exit(2)
    d = _tdir(tok)
    n = 0
    try:
        with open(_cmds_path(d), encoding="utf-8") as fh:
            n = sum(1 for ln in fh if ln.strip())
    except FileNotFoundError:
        pass
    cid = n + 1
    with open(_cmds_path(d), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": cid, "cmd": cmd, "timeout": timeout}) + "\n")
    return cid


def _await(cid: int, wait: int = 900) -> dict | None:
    tok = _active_token()
    d = _tdir(tok)
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            with open(d / "results.jsonl", encoding="utf-8") as fh:
                for ln in fh:
                    if not ln.strip():
                        continue
                    r = json.loads(ln)
                    if r.get("id") == cid:
                        return r
        except FileNotFoundError:
            pass
        time.sleep(2)
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    op = sys.argv[1]
    if op == "run":
        cmd = sys.argv[2]
        to = int(sys.argv[3]) if len(sys.argv) > 3 else 900
        cid = _enqueue(cmd, to)
        r = _await(cid, wait=to + 60)
        if r is None:
            print(f"[timeout waiting for cmd #{cid}] (agent connected? check ~/loopyard/agent.log)")
            return
        print(f"rc={r.get('rc')}")
        if r.get("stdout"):
            print("--- stdout ---\n" + r["stdout"])
        if r.get("stderr"):
            print("--- stderr ---\n" + r["stderr"])
    elif op == "send":
        print("queued cmd #" + str(_enqueue(sys.argv[2])))
    elif op == "stop":
        print("queued STOP #" + str(_enqueue("__STOP__")))
    elif op == "log":
        tok = _active_token()
        try:
            lines = (_tdir(tok) / "results.jsonl").read_text().splitlines()[-10:]
            for ln in lines:
                r = json.loads(ln)
                print(f"#{r.get('id')} rc={r.get('rc')} {(r.get('stdout') or '')[:100]!r}")
        except FileNotFoundError:
            print("(no results yet)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
