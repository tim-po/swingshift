# Loop-creator — build a team by describing it (CAP-3)

The loop-creator is Loopyard's onboarding agent. You describe your team and what
you want built; it emits a **valid, multi-role, multi-CLI loop config**. The flow:

```
connect a CLI  →  run the creator  →  answer only the material questions  →  team ready  →  point it at tasks
```

It runs on **your own connected CLI** — claude or codex — because its identity
carries no fixed model or runtime. And everything it emits is checked against the
**real linter + schema** before it can become a run, so a first-time config can't
be subtly broken.

## Tools

| Tool | What it does |
|------|--------------|
| `loop_creator_agent(register?)` | The creator's portable identity (persona + generic goal), the creation RULES, and the material questions. `register=true` saves it to the agent registry as `loop_creator` so it can run standalone on claude/codex. |
| `loop_creator_prompt(profile, instructions, answers?)` | Builds the exact instruction the CLI session runs: frames your pasted profile + ask, lists the still-unanswered material questions, restates the rules, and pins the JSON output contract. |
| `loop_creator_scaffold(spec)` | **Deterministic, no AI** — turns a structured team spec into a valid config. The floor that makes building a team *possible*, and the creator's safe fallback. |
| `loop_creator_validate(config? / text?)` | Validates an emitted config (dict, or the session's raw fenced/free-text output) against `schema.validate_config` + `authoring.lint_config`. Returns a schema-normalized config ready for `loop_save`. |

## Two ways to create a team

### 1. Let the agent do it (freehand → validated)

Register and run the creator on your connected CLI:

```
loop_creator_agent(register=true)                       # -> agent "loop_creator" in the registry
loop_creator_prompt(profile="<paste your harness/roster>",
                    instructions="build my repo; independent reviewer on codex")
# -> paste `prompt` into your claude/codex session (or run loop_creator standalone with it as the goal)
```

The session asks only the **material** questions it can't already infer, then
emits one fenced ```json config. Gate it before running:

```
loop_creator_validate(text="<the session's output>")    # -> {ok, config, errors, warnings, lint}
loop_save(<that config>)                                 # if ok
loop_start("<name>")
```

### 2. Scaffold it deterministically (structured → validated)

When you already know the shape, skip the LLM — hand a spec straight to the
scaffold. This is the multi-CLI team in one call:

```
loop_creator_scaffold({
  "name": "build-review-fix",
  "goal": "Build feature X in repo Y; review independently; fix; prove green.",
  "roles": [
    {"id": "boss",     "role": "manager", "runtime": "claude"},
    {"id": "writer",   "role": "worker",  "runtime": "claude", "host_class": "mac-local"},
    {"id": "reviewer", "role": "worker",  "runtime": "codex", "host_class": "agent-vps"}
  ],
  "budget": {"turnLimit": 12}
})
# -> {ok:true, config:{…normalized…}, warnings, lint:[]}
```

Missing personas/goals get on-model role defaults; a missing manager is added for
you (with a warning). The result is schema-normalized and save-able as-is. An
optional per-step **`host_class`** (alias `venue`) records which host/venue a step
runs on when a team spans machines — see below.

## Profile fields are surfaced, not dropped

Paste a rich profile and the creator **honors its policy/placement fields instead
of silently dropping them**. When any of these appear in your profile, the prompt
calls them out with how to honor each — map it to a config field, or carry it into
the loop goal:

- `security_policy`, `workspace_classes`, `capability_passthrough`
- `git_policy.base_sha_required`, `failure_policy.retry_allowed_when`
- `host_classes` → the creator asks *which steps run on which host/venue* (e.g.
  this Mac vs an agent-VPS) and sets a **per-step `host_class`** (alias `venue`).
  It's recorded on the step and shown in the schematic; it's advisory today (the
  scheduler doesn't route on it yet), so nothing about your placement is lost.

## The material questions

The creator asks only what would actually change the config, and skips anything
your profile/instructions already answered:

- **goal** — the north-star loop goal (required; the manager holds it)
- **roles** — one manager, how many workers, any reviewer/input_provider
- **runtimes** — claude or codex per role (the point of a multi-CLI team)
- **models** — a specific model per role, or inherit the host default
- **target** — the product/repo or concrete task (lives in the loop goal)
- **budget** — roughly how many collective turns per run
- **host_class** — *(only when the profile lists `host_classes`)* which steps run
  on which host/venue → sets a per-step `host_class`

## The creation rules (kept on-model)

Identity is loop-agnostic; task detail lives in the loop goal:

- Persona/goal must not contain todos, checklists, or step-by-step tasks.
- An agent's goal must reference *the loop goal* generically, so any loop can
  reuse the agent.
- Product/feature/path specifics belong in the loop goal, not in an agent's
  identity.

`loop_creator_validate` returns any lint findings alongside the config so you can
tidy identity before the first run — they're advisory, never a hard failure.
