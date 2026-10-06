# Loop lifecycle — briefing → main → wind-down

QoL Round 2 (engine semantics) reworks the two ends of a loop's life so the owner
is really in the loop at the start and finished work is not re-done at the end.
The state machine lives in `mcp_loops/runner.py`; every rule here is unit-tested
against `FakeSubstrate` in `mcp_loops/tests/test_runner.py`.

A loop runs three phases in order:

```
briefing  ──►  main phase  ──►  wind-down
(off-budget)   (turnLimit)      (guaranteed tail, off the turnLimit)
```

---

## Briefing — `briefingStart` (E1)

The briefing happens **before any worker runs** and is **off the turn budget**.
Historically the substrate auto-briefed the manager with a canned owner↔manager
handshake in which the owner never actually participated. `briefingStart` makes
that a real choice — a **separate axis** from `briefingMode` (which controls the
per-round prompt channel, not loop start):

| `briefingStart` | behavior |
| --------------- | -------- |
| `auto` (default) | The substrate auto-briefs the manager (`brief_manager`). Identical to prior behavior — existing configs are unchanged. |
| `interactive` | A **real owner↔manager exchange** before the first worker turn: the manager runs a briefing turn and may ask the owner clarifying questions (`ask_owner`); each question is relayed over the existing `OwnerChannel` and the owner's reply (`loop_reply`) is fed back, until the manager stops asking (any non-ask status) or the safety cap (`_MAX_BRIEFING_ASKS`, 8) is hit. |
| `none` | **Skip briefing entirely** — straight into the main phase, no briefing turn or log. |

Config:

```jsonc
{
  "briefingStart": "interactive"   // interactive | auto | none  (default: auto)
}
```

Notes:
- Interactive briefing turns go through the substrate's `run_turn` under
  `phase="briefing"` and do **not** consume `turnLimit` — briefing stays
  off-budget, exactly like the auto path.
- The manager's briefing prompt tells it to `ask_owner` for anything genuinely
  unclear, or `continue` to begin the main phase.
- Validated in `schema.py` (`briefingStart` must be `interactive|auto|none`).

Tests: `test_briefing_none_skips_briefing_entirely`,
`test_briefing_auto_is_default_and_calls_brief_manager`,
`test_briefing_interactive_owner_roundtrip_before_first_worker`.

---

## Main phase

Unchanged this round. Rounds run one `stepOrder` pass each while `turnLimit`
collective turns remain; repeat-in-place, input retirement, and the manager's
`ask_owner` / `wind_down` / `complete` decisions all behave as before. The main
phase ends early when no input agent still needs work.

### Turn accounting in the feeds

`turnLimit` is checked **between rounds**, so the round in flight when the budget
runs out finishes: `result.turns_used` (main-phase turns) can end a few turns
past `turnLimit` (e.g. 23 with a limit of 22). Wind-down turns are counted
separately in `result.winddown_turns` and never against the limit.

The dashboard loop list (`GET /api/loops`) exposes this without ever showing an
over-limit fraction:

| field | meaning |
|---|---|
| `turns_used` | main-phase turns against `turnLimit`, **capped at it** (`turns_used/turnLimit` ≤ 1) |
| `winddown_turns` | wind-down turns (the tail beyond the budget) |
| `turns` | raw `{main, winddown, limit, budgeted, over}`; `over = max(0, main - limit)` |
| `result` | finished runs only: `{ended, verdict, green, resolution, commit, turns_used, winddown_turns}` with `turns_used` capped at `turnLimit` like the row (raw counts live in the row's `turns`) and the Resolution card's verdict; `null` while live |

While a run is live the counts come from the runner's per-turn `progress.json`
(ignored if it predates the current run's `started`).

---

## Wind-down — manager-gated (E2)

Wind-down is the **guaranteed tail** beyond `turnLimit` (skipped only when the
guardian gave up or the run was cooperatively stopped). It used to force a fixed
toll regardless of state: a triage turn, **2 full worker passes** with a manager
consolidation after each, and a final report. That fixed toll is exactly why
loops **spun at the end re-doing already-finished work**.

Now wind-down is **manager-gated**:

1. Wind-down **always begins** with exactly **one manager operational-check turn**
   (the safety-check always runs).
2. If the manager judges every deliverable **operational** — reports `complete`
   (or any signal other than an explicit `needs_work`) — the loop **ends there**.
   **Minimum wind-down = 1 manager turn.** No worker passes, no re-doing finished
   work. That single turn doubles as the final report.
3. Only an explicit **`needs_work`** signal expands wind-down into **one worker
   pass** (the roster) followed by a manager consolidation / re-check. This
   repeats until the manager signals operational, **bounded by
   `managerFinalizeSteps`** so a stuck `needs_work` cannot spin.

Signal parsing (`_winddown_needs_work`): a worker pass is triggered **only** when
the manager's status is `needs_work` (or the note says so). Every other outcome —
`complete`, `wind_down`, `operational`, `continue`, or an unrecognized/missing
status — is treated as **operational**. This is backward-safe: a manager (or
substrate) that never learned the new protocol simply ends wind-down in one turn
instead of paying the old 2-pass toll.

The manager's wind-down prompt states the gate explicitly: report `complete` if
every deliverable is operational (loop ends now), or `needs_work` with a one-line
note on what remains (workers get one pass, then you are re-asked).

Tests: `test_winddown_operational_ends_in_one_manager_turn` (1 turn, 0 worker
passes), `test_winddown_needs_work_runs_one_pass_then_ends` (1 pass + end),
`test_winddown_needs_work_is_bounded_by_manager_budget` (no spin),
`test_happy_path_early_finish_then_winddown` (operational default → 1 turn).

---

## Nested loops — first-class (E3)

A `loop` step (a step with `type: "loop"` inlining a full child config) used to
run **inside** the parent: the child shared the parent's substrate, persistence
sink, and guardian, its every turn bled into the parent's `status.jsonl`, and it
showed up as one opaque parent turn with no run of its own. E3 makes a nested loop
an **independent first-class loop** with a **task-in / results-out** contract.

### What changes

A first-class sub-loop gets its **own everything**:

- its **own on-disk identity** — `config.json`, `run.json`, `status.jsonl`, output
  folder, and block-scheme, under its own status dir;
- its **own run/status/state** — it appears **separately** in `loop_list` /
  `loop_list_all` and therefore in the dashboard loops view, with its own live
  state (`running` → `finished`/`error`/`stopped`);
- its **own guardian** — re-nudge → service-agent recovery for a wedged child
  agent, exactly like a top-level loop (recovery events land in the **child's**
  `status.jsonl`, not the parent's).

The **parent** no longer runs the child's turns. It **dispatches a task** to the
child and **receives the child's result envelope + final status back** — the
same round-2 result envelope (`envelope.build_envelope`), turned inward. The
parent's `status.jsonl` records a **single dispatch→result link event** naming the
child loop (`subloop_dispatch` then `subloop_result`), **never the child's
internal turns**. The returned envelope is also carried on the parent's
`LoopResult.subloops` (and persisted in the parent `run.json` result), so a caller
reading the parent gets each child's outcome without walking the child's log.

### Naming & linking

The child's on-disk loop name is `` `<parent>.<step-id>` `` (e.g. a `sub` step in
loop `demo` → `demo.sub`; nesting chains: `demo.sub.inner`). This is flat (one
directory under the data root, so `loop_list` sees it) and self-linking. The
child's `run.json` also carries `parent`, `parentStep`, `kind: "subloop"`, and
`configName` (the config's authored name, preserved for reference).

### The seam

The engine stays substrate-agnostic. `LoopRunner` takes an optional injected
`SubloopDispatcher`:

- **no dispatcher (default)** → the original **inline** behavior (child shares the
  parent's substrate/persist/guardian). Kept for pure-logic tests and any direct
  `LoopRunner` use; **existing nested configs still complete unchanged**.
- **dispatcher injected** → **first-class** dispatch. The live server
  (`server.FirstClassSubloopDispatcher`) is the real implementation: it owns the
  run.json / status-dir / `HeadlessSubstrate` plumbing the pure engine
  deliberately does not, spins the child as its own loop (recursively, so
  grandchildren are first-class too — bounded by `schema.MAX_LOOP_DEPTH`), and
  returns the envelope. `server._run_loop_thread` injects it for every live run.

A sub-loop step still counts as **one parent turn**, and the child runs
**synchronously** inside that turn under a distinct loop name, so it never races
the parent's run.json / status writes. The child inherits the parent's owner
channel and cooperative stop (stopping the parent stops the child; a child
manager's `ask_owner` relays to the same owner).

### Backward-compatibility

Existing nested configs still validate and complete. Direct `LoopRunner` users
without a dispatcher keep the inline semantics. The only observable change for a
live run is the desired one: the child is now a **separate** loop entry with its
own state instead of a set of turns folded into the parent's log.

Tests (`test_runner.py`): `test_subloop_runs_and_counts_one_parent_turn` (inline
default), `test_subloop_firstclass_dispatches_and_returns_envelope` (dispatch +
link event + envelope on the parent result + one-turn accounting),
`test_subloop_firstclass_dispatch_failure_is_contained`. Integration
(`test_server.py`, real LoopRunner + thread + run.json on a fake substrate):
`test_subloop_is_firstclass_separate_visibility` (child is a separate `loop_list`
entry with its own `run.json` linking the parent; parent receives the envelope;
parent log has the link, not the child's turns) and
`test_subloop_child_guardian_recovers` (child worker times out once, the child's
own guardian recovers, child still finishes).
