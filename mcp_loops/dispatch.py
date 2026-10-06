"""Cross-origin dispatch envelope — the portable request/response shape (P2).

The unified hub turns "read-only mirror" into a control surface: a loop/standalone
run requested on ONE box (the hub) can execute on ANOTHER connected origin. The
request travels as ONE JSON blob so the hub, the local daemon, and the remote
daemon all speak the same shape; nothing host-specific survives the wire (design:
docs/ORIGINS-PRODUCTION-DESIGN.md §4.2, suggestions/CROSS-ORIGIN-DISPATCH.md).

This module is PURE — it builds and validates envelopes, no I/O. The server writes
the request under ``data/_dispatch/outbox/<originId>/`` (which rsyncs to the target
origin); the target's ``dispatch_inbox`` executes it and writes a response under
``data/_dispatch/inbox/<originId>/`` (which rsyncs back). Neither side invents
state it can't see — an undelivered request stays ``queued``, never fake-green.

No secrets ever ride the envelope: model ids / agent slugs / product refs are
fine; credentials live on the origin and never travel (same posture as the origin
markers ``yard connect`` writes).
"""

from __future__ import annotations

import os
import re
from typing import Optional

SCHEMA = 1

# On-disk layout of the dispatch channel, RELATIVE to the dispatch root
# (``<LOOPS_DATA_DIR>/_dispatch``). Defined here — the one shared contract module
# — so the hub (writes outbox) and the remote executor (reads outbox, writes
# inbox) can never disagree on where a request/response lives. Pure path joins,
# no I/O.
OUTBOX = "outbox"
INBOX = "inbox"
DONE_SUFFIX = ".done"


def outbox_dir(dispatch_root: str, origin_id: str) -> str:
    return os.path.join(dispatch_root, OUTBOX, origin_id)


def inbox_dir(dispatch_root: str, origin_id: str) -> str:
    return os.path.join(dispatch_root, INBOX, origin_id)

# request kinds the hub can dispatch. "standalone" (agent × product × model) and
# "loop" (a saved multi-agent loop) are EXECUTABLE runs; "control" is a lifecycle
# verb on a loop already on the target origin (spec.action, e.g. "stop") — a start
# creates a run, a stop acts on one. All three ride the SAME envelope + queue +
# executor path so there is one code path, not three.
RUN_KINDS = ("standalone", "loop")
CONTROL_KIND = "control"
REQUEST_KINDS = RUN_KINDS + (CONTROL_KIND,)
# lifecycle actions a "control" request may carry (spec.action).
CONTROL_ACTIONS = ("stop",)

# response states a status poll can report. queued = written, not yet answered;
# accepted/rejected = the origin round-tripped a response; expired = too old to
# still be waiting on (the caller decides the ttl). "unknown" = we have neither a
# request nor a response on disk for this id.
STATE_QUEUED = "queued"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"
STATE_UNKNOWN = "unknown"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def new_request_id(seq: int, now: float) -> str:
    """A dispatch request id: ``disp-<int(now)>-<seq>``. Deterministic given its
    inputs (the server passes a monotonic counter + the wall clock) so it's
    testable and collision-safe within a box without a random source."""
    return f"disp-{int(now)}-{int(seq)}"


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def build_request(request_id: str, *, origin_id: str, run_kind: str, spec: dict,
                  requested_by: str, now: float,
                  constraints: Optional[dict] = None) -> dict:
    """Assemble a dispatch REQUEST envelope. Pure — the caller persists it. Raises
    ValueError on a malformed id / kind so a bad request never reaches the wire."""
    if not valid_id(request_id):
        raise ValueError(f"invalid request_id {request_id!r}")
    if run_kind not in REQUEST_KINDS:
        raise ValueError(f"invalid run kind {run_kind!r} (want {REQUEST_KINDS})")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a dict")
    return {
        "schema": SCHEMA,
        "kind": "dispatch",
        "requestId": request_id,
        "originId": origin_id,
        "requestedAt": float(now),
        "requestedBy": requested_by,
        "run": {"kind": run_kind, "spec": spec},
        "constraints": dict(constraints or {}),
    }


def validate_request(env: object) -> dict:
    """Validate a request envelope's SHAPE (not its semantics). Returns
    ``{ok, errors}`` — used by the remote executor before it acts on anything
    that arrived over the mirror, so a corrupt/foreign blob is dropped, not run."""
    errors: list[str] = []
    if not isinstance(env, dict):
        return {"ok": False, "errors": ["envelope is not an object"]}
    if env.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if env.get("kind") != "dispatch":
        errors.append("kind must be 'dispatch'")
    if not valid_id(env.get("requestId")):
        errors.append("requestId missing/invalid")
    if not env.get("originId"):
        errors.append("originId missing")
    if not env.get("requestedBy"):
        errors.append("requestedBy missing")
    run = env.get("run")
    if not isinstance(run, dict):
        errors.append("run missing")
    else:
        if run.get("kind") not in REQUEST_KINDS:
            errors.append(f"run.kind must be one of {REQUEST_KINDS}")
        if not isinstance(run.get("spec"), dict):
            errors.append("run.spec must be an object")
    return {"ok": not errors, "errors": errors}


def build_response(request_id: str, *, accepted: bool, now: float,
                   origin_id: Optional[str] = None,
                   run_id: Optional[str] = None,
                   reason: Optional[str] = None) -> dict:
    """Assemble a one-shot dispatch RESPONSE envelope. ``accepted=False`` REQUIRES
    a reason (honesty: a refusal always says why); ``accepted=True`` carries the
    origin-local ``runId`` the hub then tracks. ``origin_id`` makes the response
    SELF-DESCRIBING (which origin answered) so a consumer holding only the response
    still knows the origin — the hub's status no longer has to cross-reference the
    request to fill it in."""
    if not accepted and not reason:
        raise ValueError("a rejected response must carry a reason")
    return {
        "schema": SCHEMA,
        "kind": "dispatch_response",
        "requestId": request_id,
        "originId": origin_id,
        "accepted": bool(accepted),
        "runId": run_id if accepted else None,
        "reason": reason,
        "originAcknowledgedAt": float(now),
    }


def response_state(response: Optional[dict]) -> str:
    """Map a response envelope onto a status-poll state. None ⇒ still queued."""
    if not isinstance(response, dict):
        return STATE_QUEUED
    return STATE_ACCEPTED if response.get("accepted") else STATE_REJECTED
