# Session Attach — one-time link

The twin of the origin connect link, for **sessions/connectors**. The owner
clicks *Generate one-time link* on the Sessions page. They get a SHORT url and
paste only that url to their AI (Claude, GPT, Codex, …). The AI fetches it and
receives complete attach instructions. Following them attaches the AI's own
session as a connector (see [CONNECT.md](CONNECT.md)), with no further human
steps.

## Flow

1. **Mint.** The MCP tool `session_attach_link(owner, ttl=900)` →
   `{url, expiresAt, attachId, connectorId}`. The url is
   `<public-base>/session-attach/sat_<id>_<secret>`. The token is single-use,
   owner-bound and short-lived (default 15 min, clamped 60..3600 s). The store
   (`<connect_root>/attach/attaches.json`, `fcntl`-locked) keeps only
   `sha256(token)`.
2. **Fetch.** `GET /session-attach/<token>` atomically consumes the token,
   registers connector `sa-<id>` (owner pinned in `meta`) and mints a
   scoped session credential `sac_…`. It answers `text/markdown` with the
   credential, the exact
   `claude mcp add --transport http mcp-loops <public-base>/__attach/mcp --header 'Authorization: Bearer sac_…'`
   line, a generic `mcpServers` JSON (Codex/Cursor/other clients), the
   `connect_register` call (MCP and a plain-HTTP `curl` equivalent), the
   `connect_poll → work → connect_return` loop and the same steps as one sh
   script. `?format=sh` returns only the runnable POSIX sh (`sh -n` clean).
   Either format counts as THE consuming fetch. `HEAD` validates without
   consuming (for link previews). Responses are `no-store`, `noindex`,
   `no-referrer`.
3. **Work.** The session calls `POST /__attach/mcp` (minimal MCP, JSON
   responses) with its bearer credential.

## Refusals (fail closed)

| link (`GET`/`HEAD /session-attach/<t>`) | status | body contains |
|---|---|---|
| unknown / malformed token | 404 | `(unknown)` / `(malformed)` |
| expired | 410 | `(expired)` |
| already used | 409 | `(consumed)` |
| revoked | 403 | `(revoked)` |
| token of another owner (on an owner-bound face) | 403 | `(wrong_owner)`, and the token is NOT consumed |

On `/__attach/mcp` the credential may call ONLY `connect_register` /
`connect_poll` (always for its own connector) and `connect_return` (only for
tasks it claimed). It may also call `start_loop` (only the owner's own loops)
and `get_loop_status` / `get_loop_result` (only loops this session started).
Any other tool → 403 `tool_not_in_scope`. Another owner's loop → 403
`not_your_loop`. A foreign connector id → 403 `wrong_connector`. A
revoked/expired/unknown credential → 401. The link token is not a credential
(401). Raw `:8771` is never referenced or proxied.

Revoke: `session_attach_revoke(attach_id | connector_id, owner)`. It works on a
pending link (the link then answers 403 revoked) or on a live session (the
credential then gets 401).

## Dashboard (owner) API — Sessions page "Attach a session"

`tracking_ui/session_attach_api.py`, mounted in `tracking_ui/loops_dashboard.py`
(which also mounts the link + `/__attach` routes via `build_routes`):

| route | body / query | returns |
|---|---|---|
| `POST /api/loops/sessions/attach-link` | `{ttl?, label?, runtime?, owner?}` | `{url, expiresAt, attachId, connectorId, ttl, runtime, publicUrlConfigured}` |
| `GET /api/loops/sessions/attaches` | `?owner=` | `{attaches: [{attachId, connectorId, state, secondsLeft, attached, live, …}], now}` |
| `POST /api/loops/sessions/attach/revoke` | `{attachId \| connectorId, owner?}` | `{ok, attach}` · 403 `wrong_owner` · 404 unknown |

`state` is `pending | consumed | revoked | expired`; `attached` = consumed;
`live` = consumed AND its connector heartbeated recently (the roster row is live).
The mint uses `$LOOPYARD_ATTACH_PUBLIC_URL`, else the host the dashboard was
reached on + `/__attach`. It never uses raw :8771. No secret or hash is ever returned by list.

## Deploy step (NOT done by this change)

The code ships in `tracking_ui/session_attach_route.py`
(`build_routes(root_fn, owner_of_request=…, public_base=…)`). To make the link
reachable, the public dashgate (`bot-swarm/tracking_ui/gate.py`) must:

1. add `*session_attach_route.build_routes(server._connect_root, owner_of_request=<the face's owner>)`
   to its route list, beside the `/mac-origin/{token}` routes;
2. add `/session-attach/` and `/__attach/` to the gate-exempt prefix check
   (`path.startswith("/__gate/") or path.startswith("/mac-origin/")`). Each
   route authenticates its own bearer secret, exactly like `/mac-origin/`;
3. set `LOOPYARD_ATTACH_PUBLIC_URL=https://<public-host>/__attach` in the gate's
   and engine's env, so minted urls and served instructions carry the public
   host. If it is unset, the mint falls back to loopback
   (`http://127.0.0.1:8811`), and the served doc uses the host the link was
   fetched on.

The self-host `loops_dashboard` already mounts all of the routes above, so a
restart of that service is enough there. Only `gate.py` (bot-swarm, not in this
repo) needs steps 1–2.

None of this restarts or changes a running service until an operator deploys it.
