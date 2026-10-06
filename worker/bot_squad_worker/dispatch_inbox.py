"""Remote dispatch executor — the origin side of cross-origin dispatch (P2).

On a connected origin, this drains the dispatch OUTBOX the hub wrote (and rsync
delivered) under ``<LOOPS_DATA_DIR>/_dispatch/outbox/<originId>/``, executes each
request via an injected ``runner`` callback, and writes a one-shot RESPONSE into
``.../inbox/<originId>/`` (which rsyncs back to the hub). Processed requests are
marked ``.done`` so they run exactly once.

Honesty + safety posture (matches suggestions/CROSS-ORIGIN-DISPATCH.md):
  * A malformed / non-dispatch blob is REJECTED with a reason, not executed —
    a corrupt file on the mirror can never trigger a run.
  * An UNKNOWN SENDER (``requestedBy`` not in the allowlist, when one is given)
    is rejected + recorded — the origin refuses foreign senders.
  * The executor never raises on one bad request; it records the failure and
    moves on, so one poison file can't wedge the whole queue.
  * No secrets are read or written here — only the portable envelope.

Pure of policy: the caller injects ``runner(spec, env) -> {ok, run_id?|reason?}``
(the real daemon wires it to the same standalone/loop code path the LOCAL path
uses) and ``now`` (so it's deterministic + testable without spawning anything).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable, Optional

from mcp_loops import dispatch


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# The env var naming the id(s) THIS box is known by in the hub's outbox (the
# hub writes ``outbox/<originId>/`` — for a mirror origin that is the
# ``_loops_<host>`` suffix). Comma-separated; unset ⇒ only ``local``.
SELF_ORIGIN_ENV = "LOOPS_DISPATCH_ORIGIN_ID"
DEFAULT_SELF_ORIGIN = "local"


def self_origin_ids(environ: Optional[dict] = None) -> tuple[str, ...]:
    """The outbox subdirs this box may drain. Fail-closed: with nothing
    configured only ``local`` qualifies — which the hub never queues into (a
    local dispatch short-circuits) — so an unconfigured poller runs nothing
    rather than every origin's requests (H1b)."""
    raw = (os.environ if environ is None else environ).get(SELF_ORIGIN_ENV) or ""
    ids = tuple(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    return ids or (DEFAULT_SELF_ORIGIN,)


def _pending_files(outbox_root: str,
                   self_origin_ids: Iterable[str]) -> list[tuple[str, str]]:
    """``(origin_id, abspath)`` for every un-processed request envelope in THIS
    origin's own outbox subdir(s) (skips already-``.done`` entries). Another
    origin's subdir is never read — its requests are not ours to run, and they
    are left untouched (not marked done) so the hub still sees them queued."""
    out: list[tuple[str, str]] = []
    for origin_id in sorted(set(self_origin_ids)):
        if not dispatch.valid_id(origin_id):
            continue
        d = os.path.join(outbox_root, origin_id)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if name.endswith(".json") and not name.endswith(dispatch.DONE_SUFFIX):
                out.append((origin_id, os.path.join(d, name)))
    return out


def process_outbox(dispatch_root: str, *, runner: Callable[[dict, dict], dict],
                   now: float,
                   requested_by_allow: Optional[list[str]] = None,
                   self_origin_ids: Iterable[str] = (DEFAULT_SELF_ORIGIN,)) -> list[dict]:
    """Drain the dispatch outbox once. For each pending request: validate, apply
    the sender allowlist, execute via ``runner``, write a response into the inbox,
    and mark the request ``.done``. Returns a summary list — one
    ``{request_id, origin, accepted, reason?}`` per processed request (most useful
    for logging/tests). Never raises on a single bad request.

    ``dispatch_root`` is ``<LOOPS_DATA_DIR>/_dispatch``. ``runner(spec, env)`` runs
    the request and returns ``{ok: True, run_id: ...}`` or ``{ok: False, reason:
    ...}``. ``requested_by_allow`` (optional) restricts which hub senders are
    honoured; None ⇒ accept any (single-tenant default). ``self_origin_ids``
    names the outbox subdir(s) addressed to THIS box — every other origin's
    requests are skipped (H1b: never run another box's loop here)."""
    outbox_root = os.path.join(dispatch_root, dispatch.OUTBOX)
    summaries: list[dict] = []
    for origin_id, path in _pending_files(outbox_root, self_origin_ids):
        env = _read_json(path)
        # the hub names each file <requestId>.json, so the filename stem is the
        # id even when a corrupt blob's body lacks one — keeps every rejection
        # traceable + pollable by the hub.
        env_id = (env or {}).get("requestId")
        stem = os.path.splitext(os.path.basename(path))[0]
        req_id = env_id if dispatch.valid_id(env_id) else stem
        # 1) shape validation — a corrupt/foreign blob is rejected, never run.
        check = dispatch.validate_request(env)
        if not check["ok"]:
            summaries.append(_finish(
                dispatch_root, origin_id, path, req_id, accepted=False, now=now,
                reason="invalid dispatch envelope: " + "; ".join(check["errors"])))
            continue
        # 1b) addressee — a request whose body names another origin (misfiled
        # into our dir) is refused, never run on this box.
        if env.get("originId") != origin_id:
            summaries.append(_finish(
                dispatch_root, origin_id, path, req_id, accepted=False, now=now,
                reason=f"request addressed to origin {env.get('originId')!r}, "
                       f"not this origin ({origin_id!r})"))
            continue
        # 2) sender allowlist — the origin refuses unknown senders.
        sender = env.get("requestedBy")
        if requested_by_allow is not None and sender not in requested_by_allow:
            summaries.append(_finish(
                dispatch_root, origin_id, path, req_id, accepted=False, now=now,
                reason=f"sender {sender!r} not in this origin's allowlist"))
            continue
        # 3) execute via the injected runner — isolate its failures.
        try:
            result = runner(env["run"]["spec"], env) or {}
        except Exception as exc:  # noqa: BLE001 — one bad run must not wedge the queue
            result = {"ok": False, "reason": f"runner raised: {type(exc).__name__}: {exc}"}
        if result.get("ok"):
            summaries.append(_finish(
                dispatch_root, origin_id, path, req_id, accepted=True, now=now,
                run_id=result.get("run_id")))
        else:
            summaries.append(_finish(
                dispatch_root, origin_id, path, req_id, accepted=False, now=now,
                reason=result.get("reason") or "runner declined (no reason given)"))
    return summaries


def _finish(dispatch_root: str, origin_id: str, req_path: str,
            request_id: Optional[str], *, accepted: bool, now: float,
            run_id: Optional[str] = None, reason: Optional[str] = None) -> dict:
    """Write the response envelope into the inbox and mark the request done.
    A request with no usable id still gets marked done (renamed) so it isn't
    re-read forever — but no response is written (there's nothing to key it to)."""
    if dispatch.valid_id(request_id):
        resp = dispatch.build_response(request_id, accepted=accepted, now=now,
                                       origin_id=origin_id, run_id=run_id,
                                       reason=reason)
        _write_json(os.path.join(dispatch.inbox_dir(dispatch_root, origin_id),
                                 f"{request_id}.json"), resp)
    try:
        os.replace(req_path, req_path + dispatch.DONE_SUFFIX)
    except OSError:
        pass
    return {"request_id": request_id, "origin": origin_id,
            "accepted": accepted, "reason": reason}


def poll_forever(dispatch_root: str, *, runner: Callable[[dict, dict], dict],
                 clock: Callable[[], float], sleep: Callable[[float], None],
                 interval: float = 5.0,
                 requested_by_allow: Optional[list[str]] = None,
                 self_origin_ids: Iterable[str] = (DEFAULT_SELF_ORIGIN,),
                 should_stop: Callable[[], bool] = lambda: False,
                 log: Callable[[str], Any] = print) -> None:
    """Poll the outbox on ``interval`` until ``should_stop()``. Thin loop over
    ``process_outbox`` — clock/sleep are injected so a test can drive it without
    real time. Logs each processed request (accepted/rejected + reason)."""
    while not should_stop():
        for s in process_outbox(dispatch_root, runner=runner, now=clock(),
                                requested_by_allow=requested_by_allow,
                                self_origin_ids=self_origin_ids):
            verb = "accepted" if s["accepted"] else "rejected"
            log(f"[dispatch_inbox] {verb} {s['request_id']} from origin "
                f"{s['origin']}" + (f": {s['reason']}" if s.get("reason") else ""))
        sleep(interval)


def _default_dispatch_root() -> str:
    """``<LOOPS_DATA_DIR>/_dispatch`` — the same root the hub's server uses.
    Resolves the data root through ``mcp_loops.paths`` so a fresh install lands
    under ``<install>/data/_loops`` (A1: no ``~/bot-swarm`` leak), falling back to
    ``$LOOPS_DATA_DIR`` / cwd if the loops package isn't importable here."""
    root = os.environ.get("LOOPS_DATA_DIR")
    if not root:
        try:
            from mcp_loops import paths
            root = paths.resolve_data_dir()
        except Exception:  # noqa: BLE001 — worker may run without mcp_loops on path
            root = os.path.join(os.getcwd(), "data", "_loops")
    return os.path.join(os.path.abspath(root), "_dispatch")


def main(argv: Optional[list] = None) -> int:
    """Run the dispatch poller on THIS origin with the REAL runner
    (mcp_loops.dispatch_runner) — the entrypoint a daemon/systemd unit invokes.
    Wired lazily so importing this module never pulls in the server."""
    import time as _time
    from mcp_loops import dispatch_runner
    interval = float(os.environ.get("LOOPS_DISPATCH_INTERVAL", "5"))
    allow_env = os.environ.get("LOOPS_DISPATCH_ALLOW")
    allow = [s.strip() for s in allow_env.split(",") if s.strip()] if allow_env else None
    selves = self_origin_ids()
    root = _default_dispatch_root()
    print(f"[dispatch_inbox] polling {root} every {interval}s for origin "
          f"{','.join(selves)}" + (f" (allow={allow})" if allow else "")
          + ("" if os.environ.get(SELF_ORIGIN_ENV) else
             f" — set {SELF_ORIGIN_ENV} to this box's hub-side origin id"))
    poll_forever(root, runner=dispatch_runner.run_request,
                 clock=_time.time, sleep=_time.sleep, interval=interval,
                 requested_by_allow=allow, self_origin_ids=selves)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
