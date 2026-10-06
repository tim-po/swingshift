"""Connect — OUTBOUND app-connect (CAP-2 §2b): a loop step hands a task to a
connected in-app session (the user's own Codex App / Claude Code) and reads the
returned result envelope back.

This is the mirror of the INBOUND path (``mcp_loops.envelope`` + the
``start_loop``/``get_loop_result`` tools). Inbound lets an app drive a loop;
OUTBOUND lets a loop drive the app — so a loop can hand INTERACTIVE / BROWSER
work (a login, a click-through, a human judgment) back to a session a person is
sitting in front of, then continue once the answer comes back.

The whole mechanism is a small on-disk TASK QUEUE + CONNECTOR REGISTRY, pure and
file-backed (same fail-soft posture as the input-queue). Two parties:

  * the LOOP side enqueues a task (:func:`enqueue_task`) and later reads the
    result (:func:`get_task`) — surfaced as the ``dispatch_task`` /
    ``dispatch_result`` MCP tools.
  * the CONNECTOR (a session running inside the user's app) registers
    (:func:`register_connector`), polls for the next task matching what it can
    run (:func:`claim_next`), executes it, and returns a result envelope
    (:func:`return_result`) — surfaced as ``connect_register`` / ``connect_poll``
    / ``connect_return`` and driven by ``mcp_loops.connector``.

Storage under ``root`` (the server points this at ``data/_loops/_connect``)::

    connectors/<connector_id>.json   {id, runtime, capabilities, registeredAt, lastSeen, meta,
                                      secretHash, approved}
    tasks/<task_id>.json             {id, status, connector, runtime, capabilities,
                                      prompt, spec, originLoop, dispatchTokenHash,
                                      createdAt, claimedAt, claimedBy, returnedAt, result}

    pending/<createdAt-ms>-<task_id>  empty marker per waiting task (claim index)

Task status: pending -> claimed -> returned | failed  (or canceled from pending
/claimed, or expired->canceled by :func:`prune_tasks`). Task ids are random;
the dispatch_result/dispatch_cancel tools require the per-task dispatch token
enqueue_task returns once (``originLoop`` is a secondary check). Everything is a plain read-modify-write with atomic file replace; the
loops server is the single writer per box (same model as the run registry), so
no cross-process lock is needed. Nothing here spawns work or touches the
network — the executing is done by whoever holds the connector session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Optional

# task status vocabulary
PENDING = "pending"
CLAIMED = "claimed"
RETURNED = "returned"
FAILED = "failed"
CANCELED = "canceled"

TERMINAL = frozenset({RETURNED, FAILED, CANCELED})

# a connector unseen for this long is reported stale (it stopped polling)
DEFAULT_STALE_AFTER = 90.0

# queue hygiene (loopyard-bug-1790562036): terminal tasks are deleted after
# TERMINAL_TTL, a task nobody claimed within PENDING_TTL is expired (canceled),
# and at most MAX_PENDING tasks may wait at once. The reaper runs from
# enqueue_task at most every PRUNE_EVERY seconds.
TERMINAL_TTL = 7 * 24 * 3600.0
PENDING_TTL = 24 * 3600.0
MAX_PENDING = 500
PRUNE_EVERY = 300.0


# ── ids + paths ────────────────────────────────────────────────────────────────
# Ids are caller-controlled (MCP tool args) and become file names, so they are
# held to a strict grammar before any path join (loopyard-bug-1790562011: a raw
# ``../../_hub/_identity/device`` task_id read the device private key). Task ids
# accept the legacy ordinal form ``t-0001-abc123`` and longer random tokens;
# connector ids match the server's loop-name grammar (``sa-<attach>`` included).
_TASK_ID_RE = re.compile(r"t-[0-9a-z][0-9a-z-]{3,63}")
_CONNECTOR_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def valid_task_id(task_id: Any) -> bool:
    return isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id) is not None


def valid_connector_id(connector_id: Any) -> bool:
    return (isinstance(connector_id, str)
            and _CONNECTOR_ID_RE.fullmatch(connector_id) is not None)


def _connectors_dir(root: str) -> str:
    return os.path.join(root, "connectors")


def _tasks_dir(root: str) -> str:
    return os.path.join(root, "tasks")


def _pending_dir(root: str) -> str:
    return os.path.join(root, "pending")


def _safe_path(root: str, sub: str, ident: Any, valid) -> Optional[str]:
    """``<root>/<sub>/<ident>.json`` iff ``ident`` passes ``valid`` AND the fully
    resolved path is a direct child of the resolved ``<root>/<sub>`` — so neither
    a crafted id nor a symlinked file / symlinked ``sub`` dir can escape the
    store. None when refused."""
    if not valid(ident):
        return None
    path = os.path.join(root, sub, f"{ident}.json")
    expected = os.path.join(os.path.realpath(root), sub)
    if os.path.dirname(os.path.realpath(path)) != expected:
        return None
    return path


def _task_path(root: str, task_id: Any) -> Optional[str]:
    return _safe_path(root, "tasks", task_id, valid_task_id)


def _connector_path(root: str, connector_id: Any) -> Optional[str]:
    return _safe_path(root, "connectors", connector_id, valid_connector_id)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # 0600 from creation: connector records carry a secret hash, tasks carry
    # prompts/results meant only for the owner's sessions
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)                  # an older tmp left with wider perms
    os.replace(tmp, path)                 # atomic: a reader never sees a partial file


def _list_json(dirpath: str) -> list[dict]:
    out: list[dict] = []
    for fn in (sorted(os.listdir(dirpath)) if os.path.isdir(dirpath) else []):
        if fn.endswith(".json") and not os.path.islink(os.path.join(dirpath, fn)):
            rec = _read_json(os.path.join(dirpath, fn))
            if isinstance(rec, dict):
                out.append(rec)
    return out


# ── connector registry ─────────────────────────────────────────────────────────
# Each connector is bound to a per-connector SECRET minted at first registration
# (loopyard-bug-1790562024: ids were self-asserted, so anyone could register as
# any connector and drain its interactive tasks). Only a sha256 of the secret is
# stored (``secretHash``, file 0600); the plaintext is returned exactly once, to
# the registrant. The server's connect_poll/connect_return require it
# (:func:`verify_connector`); re-registering an existing id requires it too,
# else only the box owner can rotate it (:func:`reset_connector_secret`, a local
# on-disk operation — see ``python -m mcp_loops.connector reset``).
#
# A secret only proves "same registrant as before", not "trusted": anyone can
# mint a fresh id + secret. So a connector also carries ``approved`` — False for
# a self-registered id, set only by the owner's local :func:`approve_connector`
# (``python -m mcp_loops.connector approve``) or by a Session Attach binding
# (``approved=True`` from the in-process caller; never from the MCP tool or
# ``meta``). UNTARGETED tasks are claimable only by approved connectors; a task
# addressed to a connector by id still reaches it (see :func:`_matches`).
class ConnectorAuthError(ValueError):
    """A registration refused because the id is held by another secret."""


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _mint_secret() -> str:
    return "cs_" + secrets.token_urlsafe(32)


def _public_connector(rec: Optional[dict]) -> Optional[dict]:
    if rec is None:
        return None
    return {k: v for k, v in rec.items() if k != "secretHash"}


def _secret_ok(rec: Optional[dict], secret: Any) -> bool:
    stored = (rec or {}).get("secretHash")
    if not isinstance(stored, str) or not stored or not isinstance(secret, str) or not secret:
        return False
    return hmac.compare_digest(stored, _secret_hash(secret))


def verify_connector(root: str, connector_id: str, secret: Any) -> bool:
    """True iff ``connector_id`` is registered with a secret and ``secret`` is it
    (constant-time compare). A legacy record with no secret never verifies — it
    must re-register to be issued one."""
    path = _connector_path(root, connector_id)
    return _secret_ok(_read_json(path) if path else None, secret)


def register_connector(root: str, connector_id: str, *, runtime: str = "claude",
                       capabilities: Optional[list[str]] = None,
                       meta: Optional[dict] = None,
                       secret: Optional[str] = None,
                       approved: Optional[bool] = None,
                       now: Optional[float] = None) -> dict:
    """Register (or refresh) a connector — a session in the user's app that can
    execute dispatched tasks. A NEW id (or a legacy record that predates secrets)
    is issued a fresh secret, returned once as ``rec["secret"]``. Re-registering
    an id that already has a secret requires presenting it (``secret``); it then
    keeps its ``registeredAt`` and secret and refreshes ``lastSeen``/runtime/
    capabilities (no ``secret`` key in the result). Raises ValueError on an id
    outside the connector grammar, :class:`ConnectorAuthError` on a missing/wrong
    secret for a held id. ``approved`` is for trusted in-process callers only
    (Session Attach): None keeps the stored flag (False for a new id)."""
    now = time.time() if now is None else now
    path = _connector_path(root, connector_id)
    if path is None:
        raise ValueError(f"invalid connector id {connector_id!r}")
    existing = _read_json(path) or {}
    minted = None
    if existing.get("secretHash"):
        if not _secret_ok(existing, secret):
            raise ConnectorAuthError(
                f"connector {connector_id!r} is already registered; present its "
                f"secret to re-register (or the owner resets it locally)")
        secret_hash = existing["secretHash"]
    else:
        minted = _mint_secret()
        secret_hash = _secret_hash(minted)
    rec = {
        "id": connector_id,
        "runtime": runtime or "claude",
        "capabilities": sorted(set(capabilities or [])),
        "registeredAt": existing.get("registeredAt", now),
        "lastSeen": now,
        "meta": meta if isinstance(meta, dict) else existing.get("meta", {}),
        "secretHash": secret_hash,
        "approved": (existing.get("approved") is True) if approved is None else bool(approved),
    }
    _write_json(path, rec)
    out = _public_connector(rec)
    if minted:
        out["secret"] = minted
    return out


def reset_connector_secret(root: str, connector_id: str) -> str:
    """OWNER-ONLY rotation (a direct on-disk call — never exposed as an MCP tool):
    issue a new secret for an existing connector, invalidating the old one.
    Returns the new plaintext secret. Raises ValueError on a bad/unknown id."""
    path = _connector_path(root, connector_id)
    rec = _read_json(path) if path else None
    if rec is None:
        raise ValueError(f"unknown connector {connector_id!r}")
    minted = _mint_secret()
    rec["secretHash"] = _secret_hash(minted)
    _write_json(path, rec)
    return minted


def approve_connector(root: str, connector_id: str, *, approved: bool = True) -> dict:
    """OWNER-ONLY (a direct on-disk call — never exposed as an MCP tool): allow
    (or, ``approved=False``, stop allowing) a connector to claim UNTARGETED
    tasks. Returns the public record. Raises ValueError on a bad/unknown id."""
    path = _connector_path(root, connector_id)
    rec = _read_json(path) if path else None
    if rec is None:
        raise ValueError(f"unknown connector {connector_id!r}")
    rec["approved"] = bool(approved)
    _write_json(path, rec)
    return _public_connector(rec)


def is_approved(connector: Optional[dict]) -> bool:
    return bool(connector) and connector.get("approved") is True


def touch_connector(root: str, connector_id: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Heartbeat a connector's ``lastSeen`` (called on every poll). Returns the
    updated record, or None if the connector isn't registered."""
    now = time.time() if now is None else now
    path = _connector_path(root, connector_id)
    rec = _read_json(path) if path else None
    if rec is None:
        return None
    rec["lastSeen"] = now
    _write_json(path, rec)
    return _public_connector(rec)


def get_connector(root: str, connector_id: str) -> Optional[dict]:
    path = _connector_path(root, connector_id)
    return _public_connector(_read_json(path) if path else None)


def list_connectors(root: str, *, now: Optional[float] = None,
                    stale_after: float = DEFAULT_STALE_AFTER) -> list[dict]:
    """Every registered connector, each annotated with a ``live`` flag (seen
    within ``stale_after`` seconds) and ``idleSeconds``. Sorted most-recent
    first."""
    now = time.time() if now is None else now
    out = [_public_connector(c) for c in _list_json(_connectors_dir(root))]
    for c in out:
        seen = c.get("lastSeen") or 0.0
        c["idleSeconds"] = round(now - seen, 1) if seen else None
        c["live"] = bool(seen) and (now - seen) <= stale_after
    out.sort(key=lambda c: c.get("lastSeen") or 0.0, reverse=True)
    return out


# ── task queue ──────────────────────────────────────────────────────────────────
class QueueFullError(ValueError):
    """enqueue_task refused: MAX_PENDING tasks are already waiting."""


def _new_task_id() -> str:
    """An unguessable task id (128 random bits). Ids were an ordinal + prompt
    hash (``t-0001-abc123``), so any caller could walk the id space and read
    every loop's tasks (loopyard-bug-1790562030)."""
    return "t-" + secrets.token_hex(16)


# PENDING INDEX: ``pending/<createdAt ms, zero-padded>-<task_id>`` empty marker
# files, one per waiting task, so claim_next/the cap read only waiting tasks in
# creation order instead of parsing every task file ever written. Markers are
# advisory — claim_next re-checks the task record and drops stale ones; a
# missing index dir (a store from before it existed) is rebuilt by one scan.
def _marker_name(task: dict) -> str:
    ms = int(max(0.0, float(task.get("createdAt") or 0.0)) * 1000)
    return f"{ms:016d}-{task['id']}"


def _marker_task_id(name: str) -> Optional[str]:
    _, _, tid = name.partition("-")
    return tid if valid_task_id(tid) else None


def _index_add(root: str, task: dict) -> None:
    d = _pending_dir(root)
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, _marker_name(task)), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)


def _index_remove(root: str, task: dict) -> None:
    try:
        os.unlink(os.path.join(_pending_dir(root), _marker_name(task)))
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        pass


def _pending_markers(root: str) -> list[str]:
    """Sorted (oldest first) pending markers, rebuilding the index once if the
    store predates it."""
    d = _pending_dir(root)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        for t in _list_json(_tasks_dir(root)):
            if t.get("status") == PENDING and valid_task_id(t.get("id")):
                _index_add(root, t)
    return sorted(n for n in os.listdir(d) if _marker_task_id(n))


def prune_tasks(root: str, *, now: Optional[float] = None,
                terminal_ttl: Optional[float] = None,
                pending_ttl: Optional[float] = None) -> dict:
    """Reap the queue: a PENDING task older than ``pending_ttl`` is expired
    (status ``canceled``, ``expired: True``) so its waiter gets an answer; a
    terminal task whose ``returnedAt`` is older than ``terminal_ttl`` is deleted.
    Returns ``{expired, deleted}`` counts."""
    now = time.time() if now is None else now
    terminal_ttl = TERMINAL_TTL if terminal_ttl is None else terminal_ttl
    pending_ttl = PENDING_TTL if pending_ttl is None else pending_ttl
    expired = deleted = 0
    for task in _list_json(_tasks_dir(root)):
        path = _task_path(root, task.get("id"))
        if path is None:
            continue
        status = task.get("status")
        if status == PENDING and now - (task.get("createdAt") or 0.0) > pending_ttl:
            task.update(status=CANCELED, returnedAt=now, expired=True)
            _write_json(path, task)
            _index_remove(root, task)
            expired += 1
        elif status in TERMINAL and now - (task.get("returnedAt")
                                           or task.get("createdAt") or 0.0) > terminal_ttl:
            try:
                os.unlink(path)
                deleted += 1
            except OSError:
                pass
    try:
        _write_json(os.path.join(root, "prune.json"), {"at": now})
    except OSError:
        pass
    return {"expired": expired, "deleted": deleted}


def _maybe_prune(root: str, now: float) -> None:
    last = (_read_json(os.path.join(root, "prune.json")) or {}).get("at")
    if not isinstance(last, (int, float)) or not (0 <= now - last < PRUNE_EVERY):
        prune_tasks(root, now=now)


# ── dispatch tokens (loopyard-bug-1790562030) ─────────────────────────────────
# A loop name is public (loop_list), so ``originLoop`` alone can't prove who is
# asking for a task. enqueue_task mints a random per-task token, returns it ONCE
# and persists only its sha256; the dispatch_result/dispatch_cancel tools
# require it. Task records handed to anyone (connectors, status views) go
# through :func:`public_task`, which drops the hash.
def _mint_dispatch_token() -> str:
    return "dt_" + secrets.token_urlsafe(32)


def public_task(task: Optional[dict]) -> Optional[dict]:
    """A task record minus its ``dispatchTokenHash`` — the shape every caller
    outside this module (connectors, status/dispatch tools) sees."""
    if not isinstance(task, dict):
        return task
    return {k: v for k, v in task.items() if k != "dispatchTokenHash"}


def verify_dispatch_token(task: Optional[dict], token: Any) -> bool:
    """True iff ``task`` was minted with a dispatch token and ``token`` is it
    (constant-time compare). A legacy task with no stored hash never verifies —
    only the owner's in-process path (:func:`get_task`) can read it."""
    stored = (task or {}).get("dispatchTokenHash")
    if not isinstance(stored, str) or not stored or not isinstance(token, str) or not token:
        return False
    return hmac.compare_digest(stored, _secret_hash(token))


def enqueue_task(root: str, *, prompt: str, connector: Optional[str] = None,
                 runtime: Optional[str] = None,
                 capabilities: Optional[list[str]] = None,
                 spec: Optional[dict] = None, origin_loop: Optional[str] = None,
                 now: Optional[float] = None) -> dict:
    """Enqueue an OUTBOUND task for a connected session to execute. ``connector``
    targets a specific registered connector (else any matching one may claim it);
    ``runtime``/``capabilities`` constrain which connector may claim it;
    ``origin_loop`` is recorded as metadata. Returns the task record (status
    ``pending``, hash stripped) plus ``dispatchToken`` — the raw per-task token,
    returned only here, that the dispatch_result/dispatch_cancel tools require. Raises ValueError on
    an empty prompt, :class:`QueueFullError` when MAX_PENDING tasks wait."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("task prompt must be a non-empty string")
    now = time.time() if now is None else now
    _maybe_prune(root, now)
    if len(_pending_markers(root)) >= MAX_PENDING:
        raise QueueFullError(f"connect queue is full ({MAX_PENDING} tasks pending); "
                             f"retry once connectors drain it")
    tid = _new_task_id()
    token = _mint_dispatch_token()
    rec = {
        "id": tid, "status": PENDING,
        "connector": connector, "runtime": runtime,
        "capabilities": sorted(set(capabilities or [])),
        "prompt": prompt.strip(), "spec": spec if isinstance(spec, dict) else None,
        "originLoop": origin_loop,
        "dispatchTokenHash": _secret_hash(token),
        "createdAt": now, "claimedAt": None, "claimedBy": None,
        "returnedAt": None, "result": None,
    }
    path = _task_path(root, tid)
    if path is None:                      # unreachable for minted ids; belt and braces
        raise ValueError(f"minted task id {tid!r} failed validation")
    _write_json(path, rec)
    _index_add(root, rec)
    return {**public_task(rec), "dispatchToken": token}


def _matches(task: dict, connector: dict) -> bool:
    """Can ``connector`` run ``task``? Target id (if set) must match — and an
    UNTARGETED task needs an approved connector (a self-registered id must not
    drain work meant for the owner's sessions); a task runtime requirement must
    equal the connector's runtime; required capabilities must be a subset of
    the connector's."""
    if task.get("connector"):
        if task["connector"] != connector["id"]:
            return False
    elif not is_approved(connector):
        return False
    if task.get("runtime") and task["runtime"] != connector.get("runtime"):
        return False
    need = set(task.get("capabilities") or [])
    have = set(connector.get("capabilities") or [])
    return need.issubset(have)


def claim_next(root: str, connector_id: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Atomically claim the oldest PENDING task this connector can run, marking
    it ``claimed``. Returns the claimed task, or None if none match. The
    connector must be registered (its capabilities gate the match); an unknown
    connector claims nothing."""
    now = time.time() if now is None else now
    connector = get_connector(root, connector_id)
    if connector is None:
        return None
    for name in _pending_markers(root):
        path = _task_path(root, _marker_task_id(name))
        task = _read_json(path) if path else None
        if not isinstance(task, dict) or task.get("status") != PENDING:
            try:                          # stale marker: task gone or moved on
                os.unlink(os.path.join(_pending_dir(root), name))
            except OSError:
                pass
            continue
        if _matches(task, connector):
            task["status"] = CLAIMED
            task["claimedAt"] = now
            task["claimedBy"] = connector_id
            _write_json(path, task)
            _index_remove(root, task)
            return public_task(task)
    return None


def return_result(root: str, task_id: str, *, connector_id: Optional[str] = None,
                  result: Optional[dict] = None,
                  status: str = RETURNED, now: Optional[float] = None) -> dict:
    """Attach the connector's result to a claimed task and mark it terminal
    (``returned`` or ``failed``). Only the connector that CLAIMED the task may
    return it, and only while it is ``claimed`` — a PENDING task can't be
    pre-empted with a forged envelope (loopyard-bug-1790562017). Returns the
    updated task, or ``{"error": ...}``."""
    now = time.time() if now is None else now
    path = _task_path(root, task_id)
    if path is None:
        return {"error": f"invalid task id {task_id!r}"}
    task = _read_json(path)
    if task is None:
        return {"error": f"unknown task {task_id!r}"}
    if task.get("status") != CLAIMED:
        return {"error": f"task {task_id!r} is {task.get('status')!r}, not claimed"}
    if not connector_id or task.get("claimedBy") != connector_id:
        return {"error": f"task {task_id!r} was not claimed by connector {connector_id!r}"}
    if status not in (RETURNED, FAILED):
        return {"error": f"return status must be {RETURNED!r} or {FAILED!r}"}
    task["status"] = status
    task["returnedAt"] = now
    task["result"] = result if isinstance(result, dict) else {"raw": result}
    _write_json(path, task)
    return public_task(task)


def cancel_task(root: str, task_id: str, *, now: Optional[float] = None) -> dict:
    """Cancel a task that hasn't returned yet. Returns the updated task or
    ``{"error": ...}``."""
    now = time.time() if now is None else now
    path = _task_path(root, task_id)
    if path is None:
        return {"error": f"invalid task id {task_id!r}"}
    task = _read_json(path)
    if task is None:
        return {"error": f"unknown task {task_id!r}"}
    if task.get("status") in TERMINAL:
        return {"error": f"task {task_id!r} already {task.get('status')!r}"}
    task["status"] = CANCELED
    task["returnedAt"] = now
    _write_json(path, task)
    _index_remove(root, task)
    return public_task(task)


def get_task(root: str, task_id: str) -> Optional[dict]:
    """The raw stored record (incl. ``dispatchTokenHash``) — the owner's
    in-process view. Never hand it to a tool caller unfiltered."""
    path = _task_path(root, task_id)
    return _read_json(path) if path else None


def list_tasks(root: str, *, connector: Optional[str] = None,
               status: Optional[str] = None,
               origin_loop: Optional[str] = None) -> list[dict]:
    """Enumerate tasks, newest first, optionally filtered by target connector,
    status, or originating loop."""
    out = _list_json(_tasks_dir(root))
    if connector is not None:
        out = [t for t in out if t.get("connector") == connector
               or t.get("claimedBy") == connector]
    if status is not None:
        out = [t for t in out if t.get("status") == status]
    if origin_loop is not None:
        out = [t for t in out if t.get("originLoop") == origin_loop]
    out.sort(key=lambda t: t.get("createdAt") or 0.0, reverse=True)
    return [public_task(t) for t in out]


def public_task_counts(root: str) -> dict:
    """The queue as an UNAUTHENTICATED caller may see it: per-status counts and a
    total — no ids, prompts, results or origin loops (loopyard-bug-1790562030:
    a full listing handed out every task id + originLoop, defeating the
    dispatch_result scoping). Returns ``{counts, total}``."""
    counts: dict[str, int] = {}
    tasks = _list_json(_tasks_dir(root))
    for t in tasks:
        s = t.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
    return {"counts": counts, "total": len(tasks)}


def connector_envelope(task: dict, *, status: str, summary: str = "",
                       artifacts: Optional[list] = None,
                       git_commit: Optional[str] = None,
                       output: Any = None, error: Optional[str] = None,
                       now: Optional[float] = None) -> dict:
    """Build the RESULT ENVELOPE a connector returns — the SAME envelope language
    as the inbound path (task_id, status, summary, artifacts, git_commit,
    duration) so both directions speak one contract. ``duration`` is measured
    from claim to return. ``error`` is the session's own failure reason, kept
    verbatim (``None`` when it gave none — never invented). Pure — the caller
    persists it via :func:`return_result`."""
    now = time.time() if now is None else now
    claimed = task.get("claimedAt")
    seconds = round(now - claimed, 1) if isinstance(claimed, (int, float)) else None
    return {
        "task_id": task.get("id"),
        "connector": task.get("claimedBy") or task.get("connector"),
        "status": status,
        "summary": summary or "",
        "artifacts": list(artifacts or []),
        "git_commit": git_commit if git_commit else None,
        "output": output,
        "error": (str(error).strip() or None) if error else None,
        "duration": {"seconds": seconds, "claimedAt": claimed, "returnedAt": now},
    }
