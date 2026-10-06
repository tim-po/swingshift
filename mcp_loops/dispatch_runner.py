"""Dispatch runner — turns a received dispatch request into a REAL run (P2).

This is the ``runner`` the origin-side ``bot_squad_worker.dispatch_inbox`` calls
for each request the hub queued: it maps the portable request spec onto the SAME
server code path a LOCAL caller uses — ``standalone`` → loop_run_agent_standalone,
``loop`` → start_loop. No new execution path; cross-origin dispatch reuses the one
that already works, so there's a single behaviour to reason about.

Kept separate from dispatch_inbox (which is the transport/queue mechanics, pure of
policy) so the executor stays injectable + testable: dispatch_inbox never imports
the server; the daemon wires THIS runner in. Returns the ``{ok, run_id?|reason?}``
shape dispatch_inbox expects; never raises (a failure becomes an honest reason).
"""

from __future__ import annotations

from typing import Any


def run_request(spec: dict, env: dict) -> dict[str, Any]:
    """Execute one dispatch request on THIS (origin) box and return
    ``{ok: True, run_id}`` or ``{ok: False, reason}``. ``spec`` is
    ``env["run"]["spec"]``; ``env["run"]["kind"]`` selects the path.

    Imports the server lazily so the worker package doesn't hard-depend on
    mcp_loops at import time (only the box that actually executes runs needs it)."""
    from mcp_loops import server

    kind = (env.get("run") or {}).get("kind")
    if not isinstance(spec, dict):
        return {"ok": False, "reason": "spec is not an object"}

    if kind == "standalone":
        agent_id = spec.get("agent_id")
        product_id = spec.get("product_id")
        if not agent_id or not product_id:
            return {"ok": False, "reason": "standalone spec needs agent_id + product_id"}
        res = server.loop_run_agent_standalone(
            agent_id, product_id, model=spec.get("model"),
            version=spec.get("version"), origin="local")
        if res.get("error"):
            return {"ok": False, "reason": res["error"]}
        # a standalone run's stable locator is its output dir / result path.
        run_id = res.get("resultPath") or res.get("outputDir") or f"std-{agent_id}"
        return {"ok": True, "run_id": run_id}

    if kind == "loop":
        name = spec.get("name")
        if not name:
            return {"ok": False, "reason": "loop spec needs a name"}
        res = server.start_loop(name)       # runs locally on THIS origin
        if res.get("error"):
            return {"ok": False, "reason": res["error"]}
        # the inbound-envelope task_id is the loop run's stable id.
        return {"ok": True, "run_id": res.get("task_id") or name}

    if kind == "control":
        action = spec.get("action")
        name = spec.get("name")
        if action == "stop":
            if not name:
                return {"ok": False, "reason": "stop spec needs a name"}
            res = server.loop_stop(name)    # stops the run on THIS origin
            if res.get("error"):
                return {"ok": False, "reason": res["error"]}
            return {"ok": True, "run_id": name}
        return {"ok": False, "reason": f"unsupported control action {action!r}"}

    return {"ok": False, "reason": f"unsupported run kind {kind!r}"}
