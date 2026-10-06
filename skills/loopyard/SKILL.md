---
name: loopyard
description: Orchestrate agent LOOPS on Loopyard the way an expert coordinator does — author a valid multi-role team, run it, and drive it from your session via the result envelope. Load this when the user asks you to build/run a loop or team, coordinate agents on a codebase, or connect a session to a running loop.
---

# Loopyard orchestrator (coord-in-a-box)

Loopyard builds and runs **agent LOOPS**: small teams of AI agents (claude/codex
CLI sessions) that collaborate to a goal on a real codebase, with **deterministic
verification**. A loop = a validated config (`name`, `goal`, `budget`, `steps`,
`stepOrder`) driven by an engine over a substrate (a headless worker daemon that
spawns CLI sessions in tmux). Your job as orchestrator: shape a good team, write a
sharp goal, run it, and read the honest result.

This skill makes you competent to do that end-to-end. Follow it in order:
**the loop model → author a config → run it → drive it from a session.**

---

## 1. The loop model — what a good team is

- **Exactly ONE `manager`** (the orchestrator). It reads the goal, decomposes it
  into sharp *independent* slices, assigns them, integrates only committed +
  verified work, and finalizes. More than one manager is a schema error.
- **`worker`s** — usually **1–2**. Each owns one concern; **one writer per
  concern** so two workers never fight over the same files. A worker writes real
  code, verifies it runs, keeps the suite green, commits clearly.
- **`input_provider`s** (critics / testers). With `inputMode: "evidence"` (the
  default, and what you want) they must **actually run/test** the work and report
  concrete evidence — never eyeball it. A run is not "done" on a narrative, only
  on deterministic proof. The main phase ends early when no input provider still
  needs work.
- **`stepOrder`** — the per-round rotation, typically `[manager, ...workers,
  tester]`. A **nested list** inside stepOrder = a **parallel group** that may run
  concurrently (the manager decides per round). The manager must appear in
  stepOrder or the north star never runs.
- **Budget:**
  - `budget.turnLimit` — total main-phase turns across all agents.
  - `budget.managerFinalizeSteps` — manager turns granted in wind-down (default 4).
  - per-step `maxTurnMinutes` — how long the substrate waits for a turn (defaults
    by role: worker ~40, manager ~22, input ~28).
  - per-step `maxRepeatTurns` — how many times a step may repeat in place (worker ~3).

## 2. The reusable-team pattern (the core discipline)

Split **identity** from **operation**:

- An agent's **`personality`** describes durable behavior; its **`goal`** is
  loop-agnostic and references *"the loop goal"* generically. Written this way,
  the same agent can be reused by any loop.
- **ALL specifics — the product, repos, paths, and acceptance — live in the
  TOP-LEVEL `goal`**, which is enriched each round with feedback. The top-level
  goal carries the work; the agents carry the roles.

## 3. Authoring rules (enforced by `loop_lint` — keep them clean)

The linter is advisory (findings, not hard failures) but treat a clean lint as the
bar. Its rules:

- **`no-todos-in-identity`** — an agent's persona/goal must not contain todos,
  checklists, or step-by-step tasks (`- [ ]`, `TODO/FIXME/TBD`, `Step 1…`,
  `1. …`). Task steps belong in the loop goal.
- **`product-detail-in-loop-goal`** — no product/path specifics (filenames like
  `foo.py`, URLs, absolute-ish paths) in an agent's persona/goal. Put them in the
  loop goal so the agent stays reusable.
- **`generic-goal`** — an agent goal should reference *"the loop goal"* / *"the
  north star"*; a long goal that never does is probably this-loop-specific.

Write a **sharp top-level goal** with **EXPLICIT, DETERMINISTIC acceptance** (a
tester can check it: "suite passes N/N", "feature does X end-to-end") and an
**isolation/safety block** (see §6). Structural invariants — exactly one manager,
valid stepOrder, roles — are enforced hard by the schema; the lint rules above are
about staying on-model so the team is reusable.

## 4. Lifecycle

```
briefing  ──►  main phase  ──►  wind-down
(off-budget)   (turnLimit)      (guaranteed tail, off the turnLimit)
```

- **Briefing** (`briefingStart`): `auto` (default — substrate briefs the manager),
  `interactive` (a real owner↔manager exchange; the manager may `ask_owner` and the
  owner's `loop_reply` feeds back, before any worker runs), or `none` (skip).
  Briefing is **off the turn budget**.
- **Main phase**: rounds run one `stepOrder` pass each while `turnLimit` collective
  turns remain — repeat-in-place, input retirement, and the manager's `ask_owner` /
  `wind_down` / `complete` decisions. Ends early when no input agent needs work.
- **Wind-down** (manager-gated): **always** one manager operational-check turn. If
  every deliverable is operational → the loop ends there (**minimum = 1 turn**, no
  re-doing finished work). Only an explicit **`needs_work`** expands it into one
  worker pass + re-check, bounded by `managerFinalizeSteps`.
- A **guardian** recovers wedged turns (re-nudge → service-agent → give-up),
  visible as 🛡 events in `status.jsonl`. Nested loops are **first-class**
  (task-in / envelope-out; their own run/status/guardian).

## 5. The tools

**Author + run** (on the `mcp-loops` server):

| Tool | Use |
|------|-----|
| `loop_save(config)` | Validate + persist a config. |
| `loop_lint(config)` | Advisory findings: `{ok, errors, warnings, lints, rules}`. **Lint before you save.** |
| `loop_start(name, slug?)` | Launch a saved loop as a run. |
| `loop_status(name, tail?)` | Live state + last `tail` per-turn reports. |
| `loop_stop(name)` | Cooperative stop. |

**Drive a loop from a session — the app-connect four + the ENVELOPE:**

| Tool | Use |
|------|-----|
| `start_loop(name, slug?)` | Launch; returns the initial envelope (record `task_id`). |
| `get_loop_status(name, tail?)` | Poll; envelope + `recent` turn reports. |
| `get_loop_result(name)` | The canonical terminal envelope. |
| `cancel_loop(name)` | Abort; returns the envelope. |

The **result envelope** (one shape everywhere):
`task_id · loop · status · done · summary · artifacts · git_commit · verification · duration · turns`.
Poll on **`done == true`** — it is true for every terminal outcome (`completed` /
`error` / `stopped`), so you never enumerate statuses. `turns.main` / `turns.winddown`
climb live while it runs.

**Honest result markers** — the envelope harvests these from an agent's free-text
end-of-turn note (no marker, no claim):
`commit=<hex>` → `git_commit`, `tests=passed:N/N` → `verification.tests` (**counted
only from a non-manager agent** — a manager can't self-certify green),
`artifact=<path>` → an artifact (each carries an `exists` flag).

**The loop-creator** (`loop_creator_*`) — turn a *described* team into a config:
- `loop_creator_scaffold(spec)` — **deterministic, no AI**; a structured spec →
  a valid normalized config. Use this when you already know the shape.
- `loop_creator_agent(register?)` / `loop_creator_prompt(profile, instructions,
  answers?)` — the freehand path: run the creator on your CLI, it asks only the
  material questions, emits one fenced config.
- `loop_creator_validate(config? | text?)` — gate an emitted config against the
  real schema + linter before `loop_save`.

**When to use which:** write the config **by hand** (§8) for a small, well-understood
team — fastest, fully in your control. Use **`scaffold`** when you have a structured
role list but don't want to hand-write personas. Use the **freehand creator** to
onboard a stranger who is describing their team in prose.

## 6. Isolation / safety — bake into EVERY loop goal

Your loop runs **inside the live server**. A careless loop can kill production.
Every top-level goal must include:

- Work in an **isolated git worktree** and commit there — never the live tree.
- **Never restart/stop production services and never kill other sessions.**
- **Process-kill safety:** unique high ports; kill **only your own** pid/port;
  **never** `pkill -f` / `killall` / broad kills (a prior loop killed production
  that way).
- Run tests/servers from the **worktree cwd** (the editable install resolves
  `import mcp_loops` by CWD; the three suites — `mcp_loops`, `worker`,
  `tracking_ui` — share the `tests` package name, so **run each separately**).
- **No client data / production / money / external messages.**
- **Verify with deterministic evidence, never a narrative.**

## 7. Connect a session

Register the server in your CLI session once:

```
claude mcp add --transport http mcp-loops "http://127.0.0.1:8771/mcp"
```

Or use the CLI shim with `MCP_LOOPS_URL` set (it **requires** the env var, and the
`-P` flag stops Python from importing a sibling `mcp_loops/` by accident):

```
export MCP_LOOPS_URL="http://127.0.0.1:<your-port>/mcp"
python -P -m mcp_loops.cli start_loop      '{"name":"<name>"}'
python -P -m mcp_loops.cli get_loop_status '{"name":"<name>"}'
```

## 8. Worked example — author, run, read

A reusable **build → independent-review → test → fix** team. The full config ships
next to this skill at `references/example-team.json` and **passes `loop_lint`
clean** (a test asserts it). The shape:

```json
{
  "name": "build-review-fix",
  "goal": "<the north star: the feature, target repo, EXPLICIT deterministic acceptance, and the §6 isolation/safety block — enriched each round>",
  "substrate": "headless",
  "inputMode": "evidence",
  "budget": {"turnLimit": 12, "managerFinalizeSteps": 4},
  "steps": {
    "manager":  {"type": "agent", "role": "manager",        "personality": "...", "goal": "Drive the loop goal to a proven result; integrate only committed+verified work; ask the owner only when genuinely blocked."},
    "builder":  {"type": "agent", "role": "worker",         "personality": "...", "goal": "Advance the loop goal one proven increment per turn; embed commit + test markers; never claim the unverified."},
    "reviewer": {"type": "agent", "role": "worker",  "runtime": "codex", "personality": "...", "goal": "Independently review the increment produced toward the loop goal; report concrete findings to fix."},
    "tester":   {"type": "agent", "role": "input_provider", "personality": "...", "goal": "Actually run the suite each turn; needs_work while anything falls short, satisfied only when the loop goal is proven with a real test marker."}
  },
  "stepOrder": ["manager", "builder", "reviewer", "tester"]
}
```

Note the pattern: agent goals are **loop-agnostic** (they say *"the loop goal"*);
all specifics live in the top-level `goal`; the reviewer runs on **codex** so review
is independent of the claude writer; the tester is `input_provider` under
`inputMode: "evidence"`.

**Run it and read the result (from a connected session):**

```
loop_lint(<config>)                      # -> {ok:true, lints:[]}  (fix findings first)
loop_save(<config>)                      # persist
start_loop("build-review-fix")           # -> envelope, status "running", note task_id
# poll every few seconds until done:
get_loop_status("build-review-fix")      # -> "running" … then "completed", turns climbing
get_loop_result("build-review-fix")      # -> terminal envelope: summary, git_commit,
                                         #    verification.tests (from the tester), artifacts
```

**Bind a real Project** so the team targets a real checkout instead of a
goal-path guess: set `projectId` on the config (the linter warns `unlinked`
otherwise). A bound Project may carry a `/loopyard/` directory of project-scoped
state and capabilities — see `docs/CAPABILITIES.md`.

---

**Checklist before you run:** one manager · workers own disjoint concerns ·
testers are `input_provider` + `inputMode:"evidence"` · agent goals are generic ·
top-level goal has deterministic acceptance + the §6 safety block · `loop_lint`
returns `lints:[]` · budget matches the work size.
