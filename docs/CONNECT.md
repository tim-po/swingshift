# Connect — two-way app-connect for Loopyard (CAP-2)

Loopyard connects a **CLI/app session** (your Codex App or Claude Code) and a
**loop** in both directions:

- **INBOUND** — your app *drives a loop*: dispatch work to a loop and pull a
  structured result back.
- **OUTBOUND** — a loop *drives your app*: a loop step hands a task (a login, a
  browser click-through, a human judgment) to a session you're sitting in, then
  continues once you return the answer.

Both directions speak the **same result-envelope language** — `task_id`,
`status`, `summary`, `artifacts`, `git_commit`, `duration` — so you learn one
shape and use it everywhere.

All tools live on the `mcp-loops` MCP server. Register it in your session once:

```
claude mcp add --transport http mcp-loops "http://127.0.0.1:8771/mcp"
```

---

## INBOUND — your app dispatches work to a loop

Four tools. Start a saved loop, poll it, pull the result.

| Tool | What it does |
|------|--------------|
| `start_loop(name, slug?)` | Launch a saved loop as a task; returns the initial envelope (records `task_id`). |
| `get_loop_status(name, tail?)` | Poll — returns the envelope + the last `tail` per-turn reports under `recent`. |
| `get_loop_result(name)` | Pull the canonical result envelope at any time. |
| `cancel_loop(name)` | Abort a running loop; returns the envelope. |

**The result envelope:**

```json
{
  "task_id": "build_review_fix#1789474210",
  "loop": "build_review_fix",
  "status": "completed",
  "done": true,
  "summary": "✅ loop finished — complete …",
  "artifacts": [{"kind": "block_scheme", "path": "…/config.html", "exists": true}],
  "git_commit": "a1b2c3d",
  "verification": {"ended": "complete", "error": null, "tests": "passed:42/42", "tests_by": "tester", "signals": ["tester"]},
  "duration": {"seconds": 812.4, "started": 1789.., "ended_at": 1789..},
  "turns": {"main": 5, "winddown": 1},
  "retired": [],
  "waiting_question": null
}
```

**`turns` is a live count.** `turns.main` (main-phase turns) and `turns.winddown`
(the finalize tail) update **every turn while the loop runs** — poll
`get_loop_status` and you'll see them climb, not sit at `0` until the end. Once the
loop is terminal the counts are the run's authoritative totals. (A stopped/finished
run's totals are stable; a fresh `start_loop` of the same name resets them to `0`.)

`status` is one of: `saved · running · waiting_owner · needs_owner · completed ·
error · stopped · not_found`. Poll `get_loop_status` until **`done` is true**,
then read `get_loop_result`. An unknown loop returns `status:"not_found"` (not an
error) so a poller has one shape to handle.

**Every run reaches a terminal state.** When a loop ends — `complete`/`wind_down`
or a `cancel_loop`/`loop_stop` — its `status` settles to a terminal value
(`completed` / `stopped` / `error`, `done:true`) and a **`finish-report.md`** lands
in the loop's output folder (surfaced as a `finish_report` artifact). Stop is
authoritative: even an unresponsive agent won't leave a run stuck `running`, and
re-dispatching the same loop with `start_loop` always starts a **fresh** task (its
envelope never carries the previous run's summary). If you re-dispatch the instant
a cancel is still settling you may briefly get `loop … is still stopping, retry in
a moment` — poll once and retry.

#### Terminal states — the definitive set

There are **two vocabularies**; know which one you're reading:

| Layer | Field | Terminal set | Reliable "is it over?" check |
|-------|-------|--------------|------------------------------|
| **Envelope** (app-connect boundary) | `status` | `completed` · `error` · `stopped` | **`done == true`** ← use this |
| **`run.json`** (raw on-disk state) | `state` | `finished` · `complete` · `error` · `stopped` | `state ∈ {finished, complete, error, stopped}` |

- The envelope's **`done`** flag is the single signal you should poll on. It is
  `true` for **every** terminal outcome (success, error, or stop) — so you never
  have to enumerate outcomes or guess which `status` string means "over".
- If you read `run.json` directly (instead of the envelope), the terminal
  `state` set is **`{finished, complete, error, stopped}`**. Do **not** test only
  for `finished`: a run that ended in error lands on `state:"error"`, and a
  `cancel_loop`/`loop_stop` lands on `state:"stopped"` — both are terminal.
  `finished` folds to envelope `completed` (or `error` if the run's `ended` was an
  error); `stopped` is what a cancel produces. `mcp_loops.envelope.RUN_TERMINAL`
  (and `is_terminal_run_state(state)`) is the authoritative in-code definition.

**Typical flow (from an app session):**

```
start_loop("build_review_fix")            # -> envelope, status "running", note task_id
# poll every few seconds:
get_loop_status("build_review_fix")       # -> status "running" … then "completed"
get_loop_result("build_review_fix")       # -> terminal envelope: summary, git_commit, verification
```

### Result markers — how a loop proves what it did

The envelope's `git_commit`, `verification.tests`, and reported `artifacts` are
harvested from **opt-in markers** an agent embeds in its end-of-turn note (a free
one-liner it already writes). This is the honest bridge from an LLM turn to a
machine-readable fact — no marker, no claim:

```
commit=<7-40 hex>        -> git_commit          (from ANY agent's note)
tests=passed:42/42       -> verification.tests   (any non-space token; e.g. tests=green)
artifact=path/or/abs     -> an artifact (repeatable; relative resolves under the output dir)
```

Example note a tester agent reports:
`suite green commit=abc1234 tests=passed:42/42 artifact=out/report.txt`

**No manager-sourced green (A5).** A `tests=` marker is counted **only from a
non-manager agent** — the tester/worker that actually ran the suite. A manager
gives decisions (`continue` / `wind_down` / `complete`), so a manager note that
happens to say `tests=green` does **not** flip `verification.tests`; it stays
`null` until a real tester reports it, and `verification.tests_by` names the agent
that did. `commit=` and `artifact=` are still harvested from any agent.

An explicit `git_commit` field on the run wins over a marker. Missing signals
stay `null` with an honest `tests_note` — a run that proved nothing can't look
verified. Every artifact carries an `exists` flag; a claimed-but-absent path
reads `exists:false` rather than being silently asserted.

---

## OUTBOUND — a loop dispatches work to your app

A loop step hands a task to a **connector**: a session running inside your app
that registers, polls, executes, and returns a result. Two faces of one queue.

**Loop side** (a step calls these during its turn):

| Tool | What it does |
|------|--------------|
| `dispatch_task(prompt, connector?, runtime?, capabilities?, spec?, origin_loop?)` | Enqueue a task for a connected session; returns a random, unguessable `task_id` and a `dispatch_token` (returned **once** — keep it). Refused when 500 tasks are already pending. |
| `dispatch_result(task_id, dispatch_token, origin_loop?)` | Read the task state + returned envelope (`done` once it's back). Requires the task's `dispatch_token`; a wrong/missing token, or an `origin_loop` other than the one dispatched with, reads as `unknown task`. |
| `dispatch_cancel(task_id, dispatch_token, origin_loop?)` | Cancel a task that hasn't returned (same token + `origin_loop` checks). |

**Dispatch tokens.** Loop names are public (`loop_list`), so `origin_loop` is
recorded metadata and a secondary check, never a credential. Each task gets a
random `dt_…` token; only its sha256 is stored in the task file, and the
dispatch tools compare it in constant time. A task created before tokens
existed has none and can't be read or canceled over the tools — only by the
box owner in-process (`mcp_loops.connect.get_task` / `cancel_task`).

Queue hygiene: a task nobody claims within 24h is expired (`canceled`,
`expired: true`); finished tasks are deleted 7 days after they end.

**Connector side** (your app session):

| Tool | What it does |
|------|--------------|
| `connect_register(connector_id, runtime?, capabilities?, meta?, secret?)` | Register/refresh this session as an executor. A new id is issued a `secret` (returned **once**); re-registering an existing id requires that `secret`. |
| `connect_poll(connector_id, secret)` | Heartbeat + claim the next task you can run (`task` is `null` when idle). |
| `connect_return(task_id, connector_id, secret, status?, summary?, artifacts?, git_commit?, output?)` | Return the result envelope; marks the task terminal. Only the connector that claimed the task (via `connect_poll`) may return it; a still-pending task can't be returned. |

`connect_status(origin_loop?, connector_id?, secret?, task_id?, dispatch_token?)`
shows every connector (live/idle, `approved`) and queue-wide task
`counts`/`total`. Task records (`tasks`) are listed only to a credential
holder: `connector_id` + `secret` → the tasks addressed to or claimed by that
connector; `task_id` + `dispatch_token` → that one task. Unscoped or with
`origin_loop` alone, `tasks` is empty — no ids, prompts or results.

**Connector secrets.** The connector id alone proves nothing, so registration
binds it to a per-connector secret: only its sha256 is stored (connector file
mode 0600) and poll/return compare it in constant time. A lost secret can only
be rotated by the box owner, locally: `python -m mcp_loops.connector reset
<id>` (writes the new one to `~/.loopyard/connectors/<id>.secret`). A connector
registered before secrets existed has none; its first `connect_register` after
the upgrade issues one. Only the connector that *claimed* a task (via
`connect_poll`) may `connect_return` it, and a still-pending task can't be
returned. Sessions attached via Session Attach never see the secret — the
gated `/__attach` route presents it for them.

**Connector approval.** A secret only proves "same registrant as before" —
anyone can register a fresh id and get a secret. So a self-registered connector
starts **unapproved** (`approved: false` in its record and in every
`connect_poll` reply): it still receives tasks **addressed to it by id**
(`dispatch_task(connector="<id>")`, e.g. chat-thread replies), but never
*untargeted* tasks. Those go only to approved connectors. The box owner approves
locally: `python -m mcp_loops.connector approve <id>` (`--revoke` withdraws it);
Session Attach connectors are approved at claim, since the owner minted the
attach token. Approval survives a re-register and can't be set through
`connect_register`, its `meta`, or any other MCP tool. **Upgrade note:** connectors registered before
this change are unapproved until the owner approves them.

**Matching:** a task may target a specific `connector`, require a `runtime`
(claude|codex), and/or require `capabilities` (free labels like `browser`,
`interactive`). A connector only claims tasks whose runtime matches its own and
whose required capabilities are a subset of its own. Untargeted tasks go to any
*approved* matching connector, oldest first (FIFO).

### Driving the connector

**As an agent in your app** — no extra tooling; call the MCP tools directly:

```
connect_register("my-laptop", runtime="codex", capabilities=["browser"])
#   -> {ok, connector, secret: "cs_…"}   keep the secret; it is shown once
# loop:
connect_poll("my-laptop", secret="cs_…")  # -> {task: {...}} or {task: null}
#   … do the task the prompt describes (log in, click, judge) …
connect_return("t-0001-ab12", connector_id="my-laptop", secret="cs_…",
               summary="logged in", git_commit="abc1234")
```

**As a script** — run the bundled connector (register → poll → execute →
return loop):

```
python -m mcp_loops.connector run my-laptop --runtime codex --cap browser
# or one-shot:
python -m mcp_loops.connector register my-laptop --runtime codex --cap browser
python -m mcp_loops.connector poll     my-laptop            # claim + print one task
python -m mcp_loops.connector return   t-0001-ab12 --connector my-laptop --summary "did it" --commit abc1234
```

Pass a real `handler(task) -> {status, summary, artifacts, git_commit, output}`
to `Connector.run(...)` to actually perform tasks; the default handler echoes the
prompt for a human to complete. A handler that raises marks the task `failed`
(with the error in `summary`) — the connector keeps running.

**Loop-side round-trip:**

```
dispatch_task("open the portal and confirm the banner", runtime="codex",
              capabilities=["browser"], origin_loop="onboarding")   # -> task_id, dispatch_token
# poll:
dispatch_result(task_id, dispatch_token=dispatch_token, origin_loop="onboarding")   # status pending -> claimed -> done:true with the returned envelope
```

---

## One contract, both ways

Inbound `get_loop_result` and outbound `dispatch_result` both hand back an
envelope keyed by `task_id` / `status` / `summary` / `artifacts` / `git_commit`
/ `duration`. Whether your app is driving a loop or a loop is driving your app,
you record a `task_id`, poll until `done`, and read one structured result.
