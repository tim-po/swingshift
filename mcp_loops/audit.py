"""Dispatch audit log (HUB-FABRIC-SPEC §2 / §5 P3 / §6 — "every unit audited").

ONE append-only jsonl record per dispatched unit of work — allowed AND denied —
written at the Hub's single enforcement choke point (pre-send deny, post-result
allow). It is evidence, so it is deliberately small and secret-free:

    {v, ts, owner, source, target, verb, method, dispatchId,
     argsDigest, decision: "allow"|"deny", reason, result}

- ``argsDigest`` is ``sha256:<hex>`` of the canonical JSON of the unit's
  *identifying* args (argv / path / loop name / rpc params) with every payload
  or secret-shaped key REDACTED first (stdin, data, content, env values,
  tokens, passwords…). The full payload never reaches the log.
- ``result`` is whitelisted to status scalars (exitCode/ok/timedOut/bytes/
  sha256/error class) — never stdout/stderr/file contents.
- ``reason`` is a short, one-line, capped string (the typed refusal code).

Lives at ``<EnrollmentStore root>/dispatch-audit.jsonl`` — beside, and separate
from, the enrollment ``audit.jsonl`` (which keeps its transport-level kinds).

Append-only: the writer only ever opens with ``"a"`` and emits one ``\\n``-
terminated line per record under an exclusive ``flock`` (so concurrent writers
never interleave a line). There is no update/delete API. Writing is fail-soft
by default (a lost log line must not crash a dispatch — same convention as
``EnrollmentStore.audit``); pass ``strict=True`` to surface write errors.
Reading tolerates torn/garbage lines.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import time
from collections import deque
from typing import Any, Iterable, Optional

try:  # POSIX advisory lock; absent on Windows origins — append is still atomic-ish there
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
AUDIT_FILENAME = "dispatch-audit.jsonl"

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISIONS = (DECISION_ALLOW, DECISION_DENY)

# Canonical verb vocabulary (spec §2 work envelope). Named rpcs are "rpc:<name>".
V_EXEC = "exec"
V_FS_READ = "fs.read"
V_FS_WRITE = "fs.write"
V_AGENT_START = "agent.start"
V_AGENT_STOP = "agent.stop"
V_AGENT_TASK = "agent.task"      # dispatch_task → connected session (tool-level only)
RPC_PREFIX = "rpc:"

READ_CAP = 1000                  # max rows one read returns
REASON_CAP = 200                 # chars
FIELD_CAP = 200                  # chars for owner/source/target/method/ids
REDACTED = "<redacted>"

# Keys whose VALUES are payloads or secrets — replaced before digesting.
# Matched case-insensitively on the key, anywhere in the args tree.
_REDACT_KEYS = frozenset({
    "stdin", "stdin_b64", "stdinb64", "data", "data_b64", "datab64", "content",
    "contents", "body", "payload", "env", "environ", "captoken", "cap_token",
    "token", "secret", "password", "passwd", "apikey", "api_key", "authorization",
    "cookie", "privatekey", "private_key",
})
# Any key containing one of these substrings is also treated as secret.
_REDACT_SUBSTRINGS = ("secret", "token", "password", "passwd", "apikey", "api_key",
                      "private")

# The ONLY result keys that may be recorded (status, never output).
_RESULT_KEYS = ("exitCode", "ok", "timedOut", "bytes", "sha256", "verified",
                "routed", "error", "code")


# wire method -> canonical verb (origin.run is exec unless a caller — fs_ops —
# says it's really fs.read/fs.write via :func:`dispatch_context`).
_METHOD_VERBS = {
    "origin.run": V_EXEC,
    "loop.start": V_AGENT_START,
    "loop.stop": V_AGENT_STOP,
}


def verb_for_method(method: Any) -> str:
    """The audit verb for a wire method: exec / agent.start / agent.stop, else
    ``rpc:<method>`` (named reads such as ``rpc:loop.list``)."""
    m = str(method or "")
    return _METHOD_VERBS.get(m) or (RPC_PREFIX + (m or "unknown"))


# ── per-unit context (who / why) carried down to the choke point ─────────────
# The Hub's choke point (ChannelServer.call_rpc) knows target+method+args but not
# WHO asked or what the unit semantically is (fs.pull is origin.run{cat}). Callers
# up the stack (DispatchRouter, fs_ops) set these in a ContextVar instead of
# widening every call_rpc signature (which test fakes also implement). Nested
# contexts merge: an inner non-None value overrides, None keeps the outer one.
_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "dispatch_audit_ctx", default={})


@contextlib.contextmanager
def dispatch_context(*, owner: Optional[str] = None, source: Optional[str] = None,
                     verb: Optional[str] = None):
    merged = dict(_CTX.get())
    for k, v in (("owner", owner), ("source", source), ("verb", verb)):
        if v is not None:
            merged[k] = v
    tok = _CTX.set(merged)
    try:
        yield merged
    finally:
        _CTX.reset(tok)


def current_context() -> dict:
    return dict(_CTX.get())


def record_unit(store_root: Optional[str], *, decision: str, target: Optional[str],
                method: Optional[str], args: Any = None,
                dispatch_id: Optional[str] = None, reason: Optional[str] = None,
                result: Any = None, default_source: str = "hub") -> Optional[dict]:
    """Hub/origin hook: append ONE record for a dispatched unit, filling
    owner/source/verb from :func:`dispatch_context`. Fail-soft — a missing store
    or any error returns None and never breaks the dispatch."""
    if not store_root:
        return None
    try:
        ctx = _CTX.get()
        return DispatchAudit.for_store(store_root).record(
            decision=decision, owner=ctx.get("owner"),
            source=ctx.get("source") or default_source, target=target,
            verb=ctx.get("verb") or verb_for_method(method), method=method,
            args=args, dispatch_id=dispatch_id, reason=reason, result=result)
    except Exception:  # noqa: BLE001 — a lost audit line beats a crashed dispatch
        return None


def audit_path(store_root: str) -> str:
    """Where the dispatch audit lives for a given EnrollmentStore root."""
    return os.path.join(os.path.abspath(store_root), AUDIT_FILENAME)


# ── digest / sanitising ───────────────────────────────────────────────────────
def _is_secret_key(key: Any) -> bool:
    k = str(key).lower()
    return k in _REDACT_KEYS or any(s in k for s in _REDACT_SUBSTRINGS)


def redact(args: Any) -> Any:
    """Deep-copy ``args`` with payload/secret-shaped keys' values replaced by
    :data:`REDACTED` (lists/tuples recurse; scalars pass through)."""
    if isinstance(args, dict):
        return {str(k): (REDACTED if _is_secret_key(k) else redact(v))
                for k, v in args.items()}
    if isinstance(args, (list, tuple)):
        return [redact(v) for v in args]
    if isinstance(args, (bytes, bytearray)):
        return REDACTED  # raw bytes are always payload
    return args


def canonical(args: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, non-JSON types via ``str``."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def args_digest(args: Any) -> str:
    """``sha256:<hex>`` of the canonical, REDACTED args. Equal identifying args ⇒
    equal digest (lets an auditor match a unit without the log holding it);
    changing only a payload/secret value does NOT change the digest."""
    return "sha256:" + hashlib.sha256(
        canonical(redact(args)).encode("utf-8")).hexdigest()


def _cap(value: Any, cap: int = FIELD_CAP) -> Optional[str]:
    if value is None:
        return None
    line = " ".join(str(value).split())
    return line if len(line) <= cap else line[: cap - 1] + "…"


def sanitize_result(result: Any) -> Optional[dict]:
    """Keep only whitelisted status scalars; drop stdout/stderr/data/etc."""
    if result is None:
        return None
    if not isinstance(result, dict):
        return {"ok": bool(result)}
    out: dict = {}
    for k in _RESULT_KEYS:
        if k not in result:
            continue
        v = result[k]
        if isinstance(v, (bool, int, float)) or v is None:
            out[k] = v
        else:
            out[k] = _cap(v)
    return out


# ── write ─────────────────────────────────────────────────────────────────────
def make_record(*, owner: Optional[str], source: Optional[str],
                target: Optional[str], verb: str, decision: str,
                args: Any = None, method: Optional[str] = None,
                dispatch_id: Optional[str] = None, reason: Optional[str] = None,
                result: Any = None, now: Optional[float] = None) -> dict:
    """Build (but don't write) one validated audit record."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    if not verb:
        raise ValueError("verb is required")
    return {
        "v": SCHEMA_VERSION,
        "ts": float(time.time() if now is None else now),
        "owner": _cap(owner),
        "source": _cap(source),
        "target": _cap(target),
        "verb": _cap(verb),
        "method": _cap(method),
        "dispatchId": _cap(dispatch_id),
        "argsDigest": args_digest(args),
        "decision": decision,
        "reason": _cap(reason, REASON_CAP),
        "result": sanitize_result(result),
    }


class DispatchAudit:
    """Append-only writer/reader over one ``dispatch-audit.jsonl``."""

    def __init__(self, path: str, *, strict: bool = False):
        self.path = os.path.abspath(path)
        self.strict = strict

    @classmethod
    def for_store(cls, store_root: str, **kw: Any) -> "DispatchAudit":
        return cls(audit_path(store_root), **kw)

    def append(self, rec: dict) -> bool:
        """Append one record as a single line. Returns True if written."""
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # O_APPEND: every write lands at EOF even with concurrent writers.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return True
        except OSError:
            if self.strict:
                raise
            return False

    def record(self, **kw: Any) -> dict:
        """Build + append one record (see :func:`make_record`); returns it."""
        rec = make_record(**kw)
        self.append(rec)
        return rec

    def allow(self, **kw: Any) -> dict:
        return self.record(decision=DECISION_ALLOW, **kw)

    def deny(self, **kw: Any) -> dict:
        return self.record(decision=DECISION_DENY, **kw)

    # ── read ──────────────────────────────────────────────────────────────────
    def _iter(self) -> Iterable[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except OSError:
            return

    def read(self, *, limit: int = 100, origin: str = "", owner: str = "",
             verb: str = "", decision: str = "", since: float = 0.0) -> list[dict]:
        """Newest-last slice of matching records, at most ``limit`` (≤ READ_CAP)
        rows — the LAST ``limit`` matches. Empty filters match everything."""
        limit = max(1, min(int(limit or 100), READ_CAP))
        out: deque = deque(maxlen=limit)
        for rec in self._iter():
            if origin and rec.get("target") != origin:
                continue
            if owner and rec.get("owner") != owner:
                continue
            if verb and rec.get("verb") != verb:
                continue
            if decision and rec.get("decision") != decision:
                continue
            if since and float(rec.get("ts") or 0) < since:
                continue
            out.append(rec)
        return list(out)

    def summary(self) -> dict:
        """Counts by decision and verb (for a tool/REST header)."""
        total = 0
        by_decision: dict[str, int] = {}
        by_verb: dict[str, int] = {}
        for rec in self._iter():
            total += 1
            d = str(rec.get("decision"))
            by_decision[d] = by_decision.get(d, 0) + 1
            v = str(rec.get("verb"))
            by_verb[v] = by_verb.get(v, 0) + 1
        return {"path": self.path, "total": total,
                "byDecision": by_decision, "byVerb": by_verb}
