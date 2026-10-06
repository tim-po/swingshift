# Loopyard setup — from a connected CLI to a running team

Loopyard lets you build a reusable, multi-CLI agent team and point it at real
work. The onboarding path is:

```
0. bring a runner up   →   1. connect a CLI   →   2. run the creator   →   3. team ready   →   4. point it at tasks
```

## 0. Bring it up

**Fresh clone? One headless command does everything** — a dedicated venv with the
pinned deps, the loops MCP server started in the background, a runner attached,
**and the worker daemon that actually runs agents**. No prompts, no file edits,
safe to re-run:

```
scripts/loopyard-quickstart.sh          # from the repo you just cloned
```

It prints `MCP_LOOPS_URL`, `LOOPS_DATA_DIR`, and `LOOPYARD_INSTALL`
copy-paste-ready at the end; export those and you can drive loops **and run them
hands-free** from a shell immediately (see §1). Override the port with
`LOOPYARD_PORT`, the data root with `LOOPS_DATA_DIR`, the venv with
`LOOPYARD_VENV`. Uses `uv` when present, else stdlib `venv`.

**The worker daemon is part of the quickstart now** (round-4 B1). It runs in
*user-worker* mode — tmux session ops only, no scheduler — which is exactly what
loops need to `spawn_session`, so its dep set is a small subset of the full
worker (no Telegram/AutoGen). The daemon is a *tracked* service: `yard status`
shows it, `yard down` stops it by its own pid. If it can't come up, the quickstart
**fails loudly** with the log path — you never hit a bare `ConnectError` from
`start_loop`. (The full `./install.sh` still exists for a complete bot-swarm box
with the dashboard + coordinator scheduler; the quickstart is the Loopyard-only
hands-free path.)

### …or just attach a runner (if the server/venv already exist)

A **runner** is what actually executes loops on this box. If you already have a
venv + running server, just attach one — it registers a first-class default slug
and a project the worker daemon can spawn under, with **zero manual config
editing**:

```
python -m mcp_loops.yard up            # from your install dir
python -m mcp_loops.yard status        # confirm what's attached
```

After this, `start_loop("<name>")` **works with no slug**. Without a runner,
`start_loop` fails with a clear `no runner attached — run \`yard up\`` (never an
opaque worker-daemon trace). Options: `--slug`, `--repo`, `--python`, `--sock`
override the defaults; `--no-poller` skips the dispatch poller; `yard down`
stops the tracked services and detaches. The runner marker lives at
`$LOOPS_DATA_DIR/runner.json` and carries **no secrets**.

**It stays up between sessions.** `yard up` also starts the **origin dispatch
poller** as a *detached* background service (so a connected origin's queued
requests actually drain) and records it — plus the server — so you can come back
later and see what's alive:

```
python -m mcp_loops.yard status        # runner + services (running/stopped/crashed) + pid/port/health
```

`yard status` health-probes the server URL (`$MCP_LOOPS_URL`, else
`$MCP_LOOPS_HOST/PORT`) and reports whether it's reachable; if a service crashed,
re-running `yard up` brings it back (it's idempotent — a live process is left
as-is). Detached processes survive the shell that started them; `yard down` stops
them by their **own pid** (never a broad `pkill`).

## 1. Connect a CLI

Register the `mcp-loops` server in the CLI session you want to drive Loopyard
from — claude or codex both work:

```
claude mcp add --transport http mcp-loops "http://127.0.0.1:8771/mcp"
```

`8771` is only the default — if your server runs on another port, use that URL
here (and see §0 / `yard status` for the port it actually bound).

**No native MCP? Drive it from a shell.** Any tool is reachable through the CLI
shim — the four public names included — with `$MCP_LOOPS_URL` pointing at *your*
server. **`MCP_LOOPS_URL` is required:** with it unset the shim **refuses** (prints
`{"error":"MCP_LOOPS_URL not set — run scripts/loopyard-quickstart.sh…"}`) rather
than silently hitting the host's `:8771`. The quickstart prints the exact export;
copy-paste it (or set it yourself):

```
export MCP_LOOPS_URL="http://127.0.0.1:<your-port>/mcp"
export LOOPYARD_INSTALL="/path/to/your/clone"   # the quickstart prints this
python -P -m mcp_loops.cli start_loop '{"name":"<name>"}'
python -P -m mcp_loops.cli get_loop_status '{"name":"<name>"}'
```

**Run it from any directory** — the `-P` flag keeps a stray `./mcp_loops` in your
cwd from shadowing your real install (round-4 B3: from `~/bot-swarm` or any dir
that has a sibling `mcp_loops/`, a plain `python -m mcp_loops.cli` would silently
load *that* package instead). With `LOOPYARD_INSTALL` exported, the CLI also warns
loudly on stderr if it ever loads a different install than yours, naming both
paths. Bad JSON or an unreachable server print `{"error": ..., "url": ...}` (the
URL actually hit), never a traceback.

A team can mix runtimes: e.g. a claude writer with an **independent codex
reviewer**. Check what a host can actually run with `loop_origin_capabilities`
(honest per-CLI authed/applied cues — it never guesses).

## 2. Run the loop-creator

Describe your team and what you want built; the creator emits a valid config.

```
loop_creator_agent(register=true)
loop_creator_prompt(profile="<paste your harness/roster>",
                    instructions="<what you want built; which roles on which CLI>")
```

Answer only the **material** questions it asks, then validate + save what it
emits:

```
loop_creator_validate(text="<the session's output>")   # gate through the real schema+linter
loop_save(<the validated config>)
```

Already know the shape? Skip the LLM and scaffold deterministically in one call —
see **[CREATOR.md](CREATOR.md)** for `loop_creator_scaffold`.

## 3. Team ready

You now have a saved, multi-role, multi-CLI loop. Inspect it:

```
loop_get("<name>")        # normalized config + run state
loop_render("<name>")     # block-scheme
```

## 4. Point it at tasks — and connect it to your app

Start a run and pull a structured result back:

```
start_loop("<name>")            # dispatch the team as a task
get_loop_status("<name>")       # poll until done
get_loop_result("<name>")       # the result envelope: summary, git_commit, verification
```

A loop can also hand interactive/browser work **back to a session you're sitting
in** — register a connector, and loop steps dispatch tasks to it. Both directions
of app-connect (drive a loop / a loop drives your app) are one contract, covered
in **[CONNECT.md](CONNECT.md)**.

## Known limits — codex runtime (verify per host)

Codex support is real, but two host-dependent gotchas are worth knowing before a
codex step runs (both observed on the pilot box, codex-cli 0.154.0):

- **Sandbox.** Codex's default read-only sandbox uses `bwrap` (bubblewrap); on a
  host where `bwrap` can't set up its network namespace (here it failed with
  `RTM_NEWADDR`), a sandbox-respecting codex is blind. Loopyard's codex runtime
  therefore launches codex with `--dangerously-bypass-approvals-and-sandbox` — the
  worker box is already externally isolated — so multi-CLI loops run fine. On a
  locked-down host, confirm a trivial `codex exec "say ok"` returns before relying
  on a codex step.
- **Model.** Codex under a ChatGPT-account auth only accepts that account's model
  (here `gpt-6-astra`); other ids (`gpt-5*`, `o4-mini`, …) return HTTP 400. Pin the
  reviewer's `model` to the id your `codex` login actually serves, or omit `model`
  to inherit the account default. `loop_origin_capabilities` reports honest
  per-CLI authed/applied cues.

## Known limits — install path depth (worker socket)

The worker daemon binds a unix domain socket under
`<install>/data/_sock/user-<you>.sock`. Linux caps a unix socket path at **108
bytes**, so if you clone into a very deep directory the daemon can't bind and
`loopyard-quickstart.sh` fails **loudly** — you'll see, in
`<install>/data/_logs/worker.log`:

```
OSError: AF_UNIX path too long
```

This is not a bug in the daemon — the path is simply too long. **Fix:** clone into
a shorter path (e.g. `~/loopyard` rather than a deeply nested projects tree) and
re-run the quickstart. A normal home-dir install is nowhere near the limit; the
MCP server and CLI are unaffected (they use TCP, not a socket).

## Diagnostics and uninstall

- `yard diagnostics [--out DIR]` writes one tarball for a bug report: the log
  tails from `data/_logs` + `data/_run` + the origin log, `versions.json`
  (Loopyard/bundle, Python, OS, `claude`/`tmux`/`git` versions, the resolved
  paths), the service registry and the `*.toml`/`*.json` config. Every line goes
  through a redactor first (values of secret-named keys, `Authorization`/`Cookie`
  headers, URL passwords, token-shaped strings), and key material (`*.pem`,
  `secret.txt`, credential files) is never read. It lands in `data/_diag/` with
  mode 0600, and the command prints its path.
- Uninstall: `yard down`, then remove the install root (`~/loopyard`, or your
  `--dir`) and `$LOOPYARD_HOME` if you set one. A clone install keeps its state
  in `<clone>/config`, `<clone>/data` and `<clone>/workspace` (or
  `$LOOPS_DATA_DIR`). The `claude` CLI and `~/.claude/` aren't Loopyard's, so they
  stay. Details: `docs/QUICKSTART.md` "Uninstall".

## Where things live

- **[WHATS-NEW-pilot-a.md](WHATS-NEW-pilot-a.md)** — what shipped in this release, with evidence.
- **[CREATOR.md](CREATOR.md)** — build a team by describing it (CAP-3).
- **[CONNECT.md](CONNECT.md)** — two-way app-connect + the result envelope (CAP-2).
- Multi-CLI runtime (per-step claude|codex + model) is CAP-1; capability probing
  is `loop_origin_capabilities`.
