"""Session Attach — a UI-minted, short-lived, single-use token a user hands to a
local Claude/Codex session so it can attach to the Hub as a CONNECTOR, safely.

It mirrors the origin pairing code (:func:`mcp_loops.origin_onboard.mint_join` /
``EnrollmentStore.issue_pairing_code``). The owner mints a code and gets back
the exact line to paste. The code is short-lived, single-use and bound to its
owner, and it only authorizes ONE narrow thing:

  1. **mint** (:func:`mint`): the owner gets ``{attachId, token, attachUrl,
     oneLiner, expiresAt, connectorId}``. Only ``sha256(token)`` is stored. The
     token lasts ``ttl`` seconds (default 15 min, clamped to 60..3600).
  2. **claim** (:func:`claim`): the remote session presents the token once. The
     token is CONSUMED. The session is registered as connector ``connectorId``
     (:func:`mcp_loops.connect.register_connector`, owner in ``meta``) and
     receives a SESSION CREDENTIAL. The credential is a second secret, also
     stored hashed, and it is scoped to that connector + owner and revocable.
  3. **every later call** is authenticated with the credential
     (:func:`authenticate`). :func:`authorize` then gates it to the SESSION
     SCOPE: the connector protocol for its OWN connector, plus
     save/start/status/result for loops owned by this session (status/result
     remain limited to loops this session started).
     Any other engine tool is refused.

Every refusal is fail-closed and has a distinct reason. ``status`` is the HTTP
code the attach route answers with: 401 = the secret itself is not good
(missing / malformed / unknown / expired / consumed / revoked); 403 = the
secret is good but the act is out of bounds (wrong owner / wrong origin / tool
outside the scope / someone else's loop, task or connector).

Storage: ``<connect_root>/attach/attaches.json``, one record per mint. It is
read-modify-written under an ``fcntl`` lock, so the MCP server (minting) and the
gate route (claiming, in another process) never race a token into two claims.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import time
from typing import Any, Callable, Iterator, Optional

from mcp_loops import connect
from mcp_loops.devices import DEFAULT_OWNER

DEFAULT_TTL = 900            # 15 minutes: long enough to paste, short enough to leak little
MIN_TTL, MAX_TTL = 60, 3600
#: how long a session credential lives after a claim (revocable any time before).
DEFAULT_CRED_TTL = 7 * 24 * 3600
#: the public base the remote session dials (e.g. https://hub.example/__attach).
PUBLIC_URL_ENV = "LOOPYARD_ATTACH_PUBLIC_URL"
#: the loopback fallback when no public base is configured (a local session only).
LOOPBACK_BASE = "http://127.0.0.1:8811/__attach"

TOKEN_PREFIX = "sat_"        # the single-use attach token
CRED_PREFIX = "sac_"         # the session credential it is exchanged for
_SECRET_RE = re.compile(r"^(sat|sac)_([a-z0-9]{12})_([A-Za-z0-9_\-]{32,})$")

# token lifecycle
PENDING, CONSUMED, REVOKED, EXPIRED = "pending", "consumed", "revoked", "expired"

#: the tools a session credential may call. Everything else → 403.
#: Attached sessions are owner-scoped loop authors: they may create/update a
#: loop for their owner, start it, and inspect runs they started. They still
#: cannot reach arbitrary Hub or filesystem tools.
SCOPED_TOOLS = frozenset({
    "connect_register", "connect_poll", "connect_return",
    "loop_save",
    "start_loop", "get_loop_status", "get_loop_result",
})


class AttachRefused(Exception):
    """A fail-closed refusal: ``reason`` is a stable code, ``status`` the HTTP
    status (401 bad secret / 403 out of bounds)."""

    def __init__(self, reason: str, status: int, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.status = status
        self.message = message or reason

    def to_dict(self) -> dict:
        return {"error": self.message, "refused": self.reason, "status": self.status}


# ── store ─────────────────────────────────────────────────────────────────────
def _store_path(root: str) -> str:
    return os.path.join(root, "attach", "attaches.json")


@contextlib.contextmanager
def _locked(root: str) -> Iterator[dict]:
    """Yield the whole store under an exclusive lock; it is written back
    atomically on a clean exit."""
    path = _store_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                data = {}
            if not isinstance(data, dict) or not isinstance(data.get("attaches"), dict):
                data = {"attaches": {}}
            yield data
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _read(root: str) -> dict:
    try:
        with open(_store_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    a = data.get("attaches") if isinstance(data, dict) else None
    return a if isinstance(a, dict) else {}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _owner(owner: Optional[str]) -> str:
    return (owner or "").strip() or DEFAULT_OWNER


def _parse(secret: Optional[str], kind: str) -> str:
    """The attachId carried in a well-formed secret of ``kind`` (sat|sac)."""
    if not secret or not isinstance(secret, str):
        raise AttachRefused("missing", 401, "no attach credential presented")
    m = _SECRET_RE.match(secret.strip())
    if not m or m.group(1) != kind:
        raise AttachRefused("malformed", 401, "attach credential is malformed")
    return m.group(2)


def token_state(rec: dict, now: float) -> str:
    """The token's lifecycle state. Expiry is derived at read time, so a record
    never needs a sweeper to stop working."""
    st = rec.get("status") or PENDING
    if st == PENDING and now >= float(rec.get("expiresAt") or 0):
        return EXPIRED
    return st


# ── one-liner ─────────────────────────────────────────────────────────────────
def one_liner(base_url: str, token: str, *, runtime: str = "claude",
              server_name: str = "loopyard-session") -> str:
    """The line the user pastes into the session's shell. It exchanges the token
    for a session credential (the token goes in through a header on stdin, never
    argv), then registers the scoped MCP endpoint with that credential."""
    base = base_url.rstrip("/")
    claim = (f"printf 'Authorization: Bearer %s\\n' {shlex.quote(token)} "
             f"| curl -fsS -X POST -H @- -H 'Content-Type: application/json' "
             f"-d {shlex.quote(json.dumps({'runtime': runtime}))} "
             f"{shlex.quote(base + '/claim')}")
    pick = "python3 -c 'import json,sys;print(json.load(sys.stdin)[\"credential\"])'"
    cli = "codex" if runtime == "codex" else "claude"
    add = (f"{cli} mcp add --transport http {server_name} "
           f"{shlex.quote(base + '/mcp')} --header \"Authorization: Bearer $CRED\"")
    return f"CRED=$({claim} | {pick}) && {add}"


# ── the short link ────────────────────────────────────────────────────────────
LINK_PATH = "/session-attach"


def site_base(attach_base: str) -> str:
    """The site root behind an ``…/__attach`` base (the link lives beside it)."""
    b = attach_base.rstrip("/")
    return b[: -len("/__attach")] if b.endswith("/__attach") else b


def link_url(attach_base: str, token: str) -> str:
    """``<public-base>/session-attach/<token>`` — the ONE thing the user pastes."""
    return f"{site_base(attach_base)}{LINK_PATH}/{token}"


#: how the GET link answers each refusal (plain text, distinct per reason).
LINK_STATUS = {"missing": 404, "malformed": 404, "unknown": 404, "expired": 410,
               "consumed": 409, "revoked": 403, "wrong_owner": 403,
               "wrong_origin": 403}


# ── mint ──────────────────────────────────────────────────────────────────────
def mint(root: str, *, owner: Optional[str] = None, origin: str = "local",
         ttl: Optional[int] = None, label: Optional[str] = None,
         runtime: str = "claude", base_url: Optional[str] = None,
         now: Optional[float] = None) -> dict:
    """Mint a single-use attach token for ``owner`` targeting ``origin``. Returns
    the only copy of the plaintext token; the store keeps its hash."""
    now = time.time() if now is None else now
    ttl = DEFAULT_TTL if ttl in (None, 0) else int(ttl)
    ttl = max(MIN_TTL, min(MAX_TTL, ttl))
    runtime = runtime if runtime in ("claude", "codex") else "claude"
    attach_id = secrets.token_hex(6)
    token = f"{TOKEN_PREFIX}{attach_id}_{secrets.token_urlsafe(24)}"
    connector_id = f"sa-{attach_id}"
    rec = {
        "attachId": attach_id, "tokenHash": _hash(token),
        "owner": _owner(owner), "origin": (origin or "local").strip() or "local",
        "label": (label or "").strip() or None, "runtime": runtime,
        "connectorId": connector_id, "status": PENDING,
        "createdAt": now, "expiresAt": now + ttl,
        "consumedAt": None, "revokedAt": None,
        "credHash": None, "credExpiresAt": None, "loops": [],
    }
    with _locked(root) as data:
        data["attaches"][attach_id] = rec
    configured = (base_url or os.environ.get(PUBLIC_URL_ENV) or "").strip()
    base = (configured or LOOPBACK_BASE).rstrip("/")
    return {
        "attachId": attach_id, "token": token, "connectorId": connector_id,
        "owner": rec["owner"], "origin": rec["origin"], "runtime": runtime,
        "expiresAt": rec["expiresAt"], "ttl": ttl,
        "url": link_url(base, token),
        "attachUrl": base + "/claim", "mcpUrl": base + "/mcp",
        "publicUrlConfigured": bool(configured),
        "oneLiner": one_liner(base, token, runtime=runtime),
    }


def _check_token(rec: Any, token: str, now: float, expect_owner: Optional[str],
                 expect_origin: Optional[str]) -> dict:
    if not isinstance(rec, dict) or not hmac.compare_digest(
            rec.get("tokenHash") or "", _hash(token.strip())):
        raise AttachRefused("unknown", 401, "attach token is not recognised")
    st = token_state(rec, now)
    if st == EXPIRED:
        raise AttachRefused("expired", 401, "attach token has expired; mint a new one")
    if st == CONSUMED:
        raise AttachRefused("consumed", 401, "attach token was already used")
    if st == REVOKED:
        raise AttachRefused("revoked", 401, "attach token was revoked")
    if expect_owner is not None and _owner(expect_owner) != rec["owner"]:
        raise AttachRefused("wrong_owner", 403,
                            "attach token does not belong to this owner")
    if expect_origin is not None and (expect_origin or "local") != rec["origin"]:
        raise AttachRefused("wrong_origin", 403,
                            "attach token was minted for a different origin")
    return rec


def check(root: str, token: str, *, expect_owner: Optional[str] = None,
          expect_origin: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Validate ``token`` WITHOUT consuming it (a HEAD / link preview). Returns
    the public record or raises :class:`AttachRefused`."""
    now = time.time() if now is None else now
    rec = _read(root).get(_parse(token, "sat"))
    return public(_check_token(rec, token, now, expect_owner, expect_origin), now)


# ── claim (token → credential) ────────────────────────────────────────────────
def claim(root: str, token: str, *, expect_owner: Optional[str] = None,
          expect_origin: Optional[str] = None, runtime: Optional[str] = None,
          capabilities: Optional[list] = None, meta: Optional[dict] = None,
          cred_ttl: int = DEFAULT_CRED_TTL, now: Optional[float] = None) -> dict:
    """Consume ``token`` (once), register the session as its connector and
    return ``{credential, connectorId, attachId, owner, origin,
    credentialExpiresAt}``. Raises :class:`AttachRefused` on any bad token.

    ``expect_owner`` / ``expect_origin`` are what the route serving the claim is
    bound to. A token minted for a different owner or origin is refused and is
    NOT consumed, so the rightful owner's session can still use it."""
    now = time.time() if now is None else now
    attach_id = _parse(token, "sat")
    with _locked(root) as data:
        rec = data["attaches"].get(attach_id)
        _check_token(rec, token, now, expect_owner, expect_origin)
        cred = f"{CRED_PREFIX}{attach_id}_{secrets.token_urlsafe(32)}"
        m = dict(meta) if isinstance(meta, dict) else {}
        # the store's binding wins over anything the session claims about itself
        m.update(owner=rec["owner"], origin=rec["origin"], attachId=attach_id,
                 attachedVia="session_attach")
        try:
            reg = connect.register_connector(
                root, rec["connectorId"], runtime=runtime or rec.get("runtime") or "claude",
                capabilities=[str(c) for c in capabilities] if isinstance(capabilities, list) else None,
                meta=m, approved=True, now=now)   # owner-minted binding = trusted
        except connect.ConnectorAuthError:
            # the id is held by a connector this attach didn't mint — refuse and
            # leave the token unconsumed (the store isn't written on a raise)
            raise AttachRefused("connector_taken", 403,
                                "the connector id is already registered")
        # The connector secret stays server-side (0600 store, stripped by
        # public()): the gated route presents it on the session's behalf, so the
        # session itself never holds a credential usable on raw :8771.
        rec.update(status=CONSUMED, consumedAt=now, credHash=_hash(cred),
                   credExpiresAt=now + int(cred_ttl), connectorSecret=reg["secret"])
    return {"ok": True, "credential": cred, "connectorId": rec["connectorId"],
            "attachId": attach_id, "owner": rec["owner"], "origin": rec["origin"],
            "credentialExpiresAt": rec["credExpiresAt"],
            "scope": sorted(SCOPED_TOOLS)}


# ── authenticate (credential → session record) ────────────────────────────────
def authenticate(root: str, credential: Optional[str], *,
                 now: Optional[float] = None) -> dict:
    """The session record behind a credential, or :class:`AttachRefused`. A
    plaintext attach token is not a credential (``malformed``)."""
    now = time.time() if now is None else now
    attach_id = _parse(credential, "sac")
    rec = _read(root).get(attach_id)
    if not isinstance(rec, dict) or not rec.get("credHash") or not hmac.compare_digest(
            rec["credHash"], _hash(credential.strip())):
        raise AttachRefused("unknown", 401, "session credential is not recognised")
    if rec.get("status") == REVOKED:
        raise AttachRefused("revoked", 401, "session was revoked")
    if now >= float(rec.get("credExpiresAt") or 0):
        raise AttachRefused("expired", 401, "session credential has expired; re-attach")
    return rec


# ── revoke / list ─────────────────────────────────────────────────────────────
def revoke(root: str, *, owner: Optional[str] = None, attach_id: Optional[str] = None,
           connector_id: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Revoke a pending token OR a live session (its credential stops working at
    once). Only the minting owner may revoke. Returns the public record or
    ``{error, refused}``."""
    now = time.time() if now is None else now
    with _locked(root) as data:
        rec = None
        if attach_id:
            rec = data["attaches"].get(attach_id)
        elif connector_id:
            rec = next((r for r in data["attaches"].values()
                        if isinstance(r, dict) and r.get("connectorId") == connector_id), None)
        if not isinstance(rec, dict):
            return {"error": "unknown attach", "refused": "unknown", "status": 404}
        if _owner(owner) != rec["owner"]:
            return {"error": "attach does not belong to this owner",
                    "refused": "wrong_owner", "status": 403}
        if rec.get("status") != REVOKED:
            rec.update(status=REVOKED, revokedAt=now)
        return {"ok": True, "attach": public(rec, now)}


def public(rec: dict, now: float) -> dict:
    """A record with the hashes stripped, plus its derived state."""
    out = {k: v for k, v in rec.items()
           if k not in ("tokenHash", "credHash", "connectorSecret")}
    out["state"] = token_state(rec, now)
    out["secondsLeft"] = (max(0, round(float(rec["expiresAt"]) - now))
                          if out["state"] == PENDING else 0)
    return out


def list_attaches(root: str, *, owner: Optional[str] = None,
                  now: Optional[float] = None) -> list[dict]:
    """The owner's attaches, newest first. Another owner's rows are never
    returned."""
    now = time.time() if now is None else now
    me = _owner(owner)
    rows = [public(r, now) for r in _read(root).values()
            if isinstance(r, dict) and r.get("owner") == me]
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
    return rows


def note_loop(root: str, attach_id: str, loop_name: str) -> None:
    """Record a loop this session started, so it may later read that loop's
    status and result (and no other loop)."""
    with _locked(root) as data:
        rec = data["attaches"].get(attach_id)
        if isinstance(rec, dict) and loop_name not in rec.setdefault("loops", []):
            rec["loops"].append(loop_name)


# ── scope gate ────────────────────────────────────────────────────────────────
def authorize(sess: dict, tool: str, args: Optional[dict], *,
              task_lookup: Optional[Callable[[str], Optional[dict]]] = None,
              loop_owner: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    """Gate one tool call by an authenticated session. Returns the args to call
    the engine with (the connector id and owner are pinned to the session's
    own) or raises :class:`AttachRefused` (403).

    ``task_lookup(task_id)`` → the connect task (to prove this session claimed
    it). ``loop_owner(name)`` → the loop's resolved owner, or None if there is
    no such loop."""
    a: dict[str, Any] = dict(args or {})
    cid, owner = sess["connectorId"], sess["owner"]
    if tool not in SCOPED_TOOLS:
        raise AttachRefused("tool_not_in_scope", 403,
                            f"{tool!r} is not available to an attached session")
    if tool in ("connect_register", "connect_poll"):
        given = a.get("connector_id")
        if given not in (None, "", cid):
            raise AttachRefused("wrong_connector", 403,
                                "a session may only act as its own connector")
        a["connector_id"] = cid
        a["secret"] = sess.get("connectorSecret") or ""
        if tool == "connect_register":
            meta = a.get("meta") if isinstance(a.get("meta"), dict) else {}
            meta.update(owner=owner, origin=sess.get("origin"),
                        attachId=sess["attachId"], attachedVia="session_attach")
            a["meta"] = meta
        return a
    if tool == "connect_return":
        task = task_lookup(str(a.get("task_id") or "")) if task_lookup else None
        if not isinstance(task, dict) or task.get("claimedBy") != cid:
            raise AttachRefused("not_your_task", 403,
                                "a session may only return tasks it claimed")
        a["connector_id"] = cid           # the engine re-checks claimedBy against this
        a["secret"] = sess.get("connectorSecret") or ""
        return a
    if tool == "loop_save":
        cfg = a.get("config")
        wrapped = (isinstance(cfg, dict) and "name" not in cfg
                   and set(cfg) == {"config"}
                   and isinstance(cfg.get("config"), dict))
        target = cfg.get("config") if wrapped else cfg
        if not isinstance(target, dict):
            # Let the engine return its normal validation response for malformed
            # configs, while still requiring a dict before we can pin ownership.
            return a
        name = str(target.get("name") or "")
        existing = loop_owner(name) if name and loop_owner else None
        if existing is not None and existing != owner:
            raise AttachRefused("not_your_loop", 403,
                                "a session may only save loops for its owner")
        explicit = target.get("owner")
        if explicit not in (None, "", owner):
            raise AttachRefused("not_your_loop", 403,
                                "a session may only save loops for its owner")
        pinned = dict(target)
        pinned["owner"] = owner
        a["config"] = {"config": pinned} if wrapped else pinned
        return a
    name = str(a.get("name") or "")
    if not name:
        raise AttachRefused("bad_request", 403, "a loop name is required")
    if tool == "start_loop":
        lo = loop_owner(name) if loop_owner else None
        if lo is None or lo != owner:
            # an unknown loop and another owner's loop answer the same: no oracle
            raise AttachRefused("not_your_loop", 403,
                                "no such loop for this session's owner")
        return a
    # get_loop_status / get_loop_result: only loops THIS session started
    if name not in (sess.get("loops") or []):
        raise AttachRefused("not_your_loop", 403,
                            "a session may only read loops it started")
    a["owner"] = owner
    return a


# ── the instructions served at the link ───────────────────────────────────────
SERVER_NAME = "mcp-loops"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _sh(mcp_url: str, claimed: dict, runtime: str) -> str:
    q = shlex.quote
    reg = json.dumps({"runtime": runtime, "meta": {"attachedFrom": "session-attach-link"}})
    return f"""#!/bin/sh
# Loopyard session attach: registers the scoped mcp-loops server and announces
# this session as connector {claimed['connectorId']}. The one-time link that
# served this script is now used up; the credential below is scoped to the
# connector protocol and to loops this session starts.
set -eu
LOOPYARD_MCP_URL={q(mcp_url)}
LOOPYARD_ATTACH_CRED={q(claimed['credential'])}
LOOPYARD_CONNECTOR={q(claimed['connectorId'])}

# rpc TOOL JSON_ARGS: one MCP tools/call against the gated endpoint
rpc() {{
  printf '{{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{{"name":"%s","arguments":%s}}}}' "$1" "$2" |
    curl -fsS -X POST \\
      -H "Authorization: Bearer $LOOPYARD_ATTACH_CRED" \\
      -H 'Content-Type: application/json' \\
      -H 'Accept: application/json, text/event-stream' \\
      --data-binary @- "$LOOPYARD_MCP_URL"
}}

# 1. register the MCP server so future sessions get the tools natively
if command -v claude >/dev/null 2>&1; then
  claude mcp add --transport http {SERVER_NAME} "$LOOPYARD_MCP_URL" \\
    --header "Authorization: Bearer $LOOPYARD_ATTACH_CRED" ||
    echo "note: 'claude mcp add' failed (an existing '{SERVER_NAME}' entry?); the HTTP calls below still attach you" >&2
fi

# 2. announce this session as a connector (it shows as live on the Sessions page)
rpc connect_register {q(reg)}
echo

# 3. first poll: a task (JSON) or null. Keep polling every ~10s; after doing a
#    task, return it with: rpc connect_return '{{"task_id":"<id>","status":"returned","summary":"..."}}'
rpc connect_poll '{{}}'
echo
echo "attached as connector $LOOPYARD_CONNECTOR" >&2
"""


def instructions(mcp_url: str, claimed: dict, *, fmt: str = "md",
                 runtime: str = "claude") -> str:
    """The complete, self-contained attach document for an AI session that just
    fetched its one-time link. ``claimed`` is :func:`claim`'s result (it carries
    the scoped credential). ``fmt="sh"`` is the runnable POSIX-sh variant."""
    script = _sh(mcp_url, claimed, runtime)
    if fmt == "sh":
        return script
    cid, cred = claimed["connectorId"], claimed["credential"]
    q = shlex.quote
    add = (f"claude mcp add --transport http {SERVER_NAME} {q(mcp_url)} "
           f"--header {q('Authorization: Bearer ' + cred)}")
    generic = json.dumps({"mcpServers": {SERVER_NAME: {
        "type": "http", "url": mcp_url,
        "headers": {"Authorization": f"Bearer {cred}"}}}}, indent=2)
    rpc = (f"curl -fsS -X POST {q(mcp_url)} \\\n"
           f"  -H {q('Authorization: Bearer ' + cred)} \\\n"
           f"  -H 'Content-Type: application/json' \\\n"
           f"  -d '{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\","
           f"\"params\":{{\"name\":\"connect_register\",\"arguments\":"
           f"{{\"runtime\":\"{runtime}\"}}}}}}'")
    tools = ", ".join(f"`{t}`" for t in sorted(SCOPED_TOOLS))
    return f"""# Attach this session to Loopyard

You are an AI session. Your owner sent you this link so that **you attach
yourself** to their Loopyard Hub as a connector. Do the steps below in order,
yourself, without asking the user for anything else. This link was single-use
and is now spent. The credential below is the only secret you need; keep it out
of chat transcripts you share.

- connector id: `{cid}`
- owner: `{claimed['owner']}`
- MCP endpoint (token-gated): `{mcp_url}`
- credential: `{cred}` (expires {_iso(claimed['credentialExpiresAt'])})

## 1. Register the mcp-loops MCP server

Claude Code:

```sh
{add}
```

Any other MCP client (Codex, Cursor, a generic `mcpServers` config), as a
streamable-HTTP server with a bearer header:

```json
{generic}
```

A newly added server's tools usually load on your NEXT session start. To attach
**now**, do step 2 over plain HTTP.

## 2. Announce yourself: `connect_register`

Call the `connect_register` tool with `{{"runtime": "{runtime}"}}` (your connector
id is pinned by the credential). Without the MCP tools loaded, the same call over
HTTP is:

```sh
{rpc}
```

After this call you show up as a live session on the owner's Sessions page.

## 3. Work loop: `connect_poll` → do it → `connect_return`

1. Call `connect_poll` (no arguments). It returns `{{"task": null}}` or a task
   object with an `id` and a `prompt`.
2. If there is a task, do the work its `prompt` describes.
3. Call `connect_return` with `{{"task_id": "<id>", "status": "returned",
   "summary": "<what you did>"}}` (or `"status": "failed"` plus `"error"`).
4. Poll again. Wait about 10 seconds between empty polls; every poll also
   counts as your heartbeat.

## What this credential can do

Only these tools: {tools}. `loop_save` creates or updates a loop for your owner;
`start_loop` then runs one of your owner's loops. `get_loop_status` /
`get_loop_result` work only for loops this session started.
Every other engine tool, and anything belonging to another owner, is refused
(HTTP 403). The owner can revoke this session at any time; after that every call
returns 401.

## All of the above as one script

The same steps as runnable POSIX sh. Save it and run `sh attach.sh`:

```sh
{script}```
"""
