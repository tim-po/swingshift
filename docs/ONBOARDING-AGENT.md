# Onboarding — getting started, and the onboarding agent

**Status: the getting-started checklist is BUILT** (web Home, desktop guide panel,
CLI). The conversational hand-off (an agent that talks the user through adding
machines and fixing CLI logins) is still design — see the last section.

## The one idea

The manual quickstart (`docs/QUICKSTART.md`) ends at **"a machine is connected."**
Everything after that should feel like being *guided*, not like reading more docs.
New users get a short, plain getting-started checklist that ticks itself as they
actually do each thing, and that points at exactly the right page for each step.

Minimize manual friction to the first connection; **guide the rest.**

---

## One source of truth

Every surface renders the same payload from `mcp_loops/onboarding.py`:

| Constant | What it is |
|----------|------------|
| `WELCOME` | The one-paragraph, plain-words explanation of a Loopyard team. |
| `STEPS` | The four getting-started steps (below). Each has a stable `key`, `title`, `what`, the web-UI `route` it happens on, and a button label (`action`). |
| `EXPLORE` | Optional next steps, unlocked once the four are done: *Write your first plan* (`/plans`), *Connect a second computer* (`/machines/computers`), *Star a favorite agent* (`/roles`). |
| `PLACES` | The key pages a new user is pointed at: Home, New loop, Loops, Plans, Machines, Sessions. |

`progress_payload(root)` adds an engine-derived `done` to every step and explore
item plus `progress: {done, total, next, complete}`, and the prompt a CLI session
runs the guide with. It is served three ways — no surface keeps a copy:

- **Web app, Home** — `GET /api/loops/onboarding` (`tracking_ui/onboarding_api.py`,
  read-only) → MCP tool `loop_onboarding_progress` → `progress_payload`. Typed
  client: `onboardingApi` / `checklistModel` in `frontend/packages/api/src/onboarding.ts`.
- **Desktop app, guide panel** — `yard onboard --json` (`mcp_loops/yard.py`,
  `cmd_onboard`), read by `frontend/apps/desktop/guide.mjs`, which vets each route
  (plain in-app paths only) but keeps no step/route/place tables of its own.
- **CLI** — `python -m mcp_loops.yard onboard` prints the welcome and the steps.

`mcp_loops/tests/test_onboarding_surfaces.py` parses the web route table
(`frontend/apps/web/src/views.ts`, including `subpaths` and `redirect` aliases)
and fails if any step, explore item or place points at a route that doesn't exist
or is only a legacy redirect.

## The four steps

| Key | Title | Route | Done when (from real data, never a click) |
|-----|-------|-------|--------------------------------------------|
| `machine` | Connect a machine | `/machines/computers` | a reachable machine exists (the one running Loopyard counts), or any loop has run |
| `describe` | Describe what you want done | `/newloop` | a loop is saved (`config.json`) |
| `run` | Review the team and start | `/newloop` | a run has started (`run.json` past `saved`) |
| `result` | See the result and rate it | `/loops` | a result is rated good / ok / bad (`dispositions.jsonl`) |

Later facts imply earlier loop steps (a rating implies a run, a run implies a saved
loop and a machine). Explore items tick from the data dir too: a plan in
`_ideahub/`, two reachable machines, a starred agent in `_registry/agents/`.

## How each surface shows it

- **Web Home** (`frontend/apps/web/src/views/overview`). With no loops yet, the
  checklist *is* the page: numbered steps with their explanation, the next step's
  button is the page's one primary action, and a "1 of 4" progress bar. Once loops
  exist it becomes a compact card at the top of the normal Home until the four are
  done; then the explore items take its place, quieter, until they're done too.
  "Hide checklist" is remembered (`home.checklist.hidden`). If the endpoint is
  unavailable (older engine), first-run Home falls back to a single "New loop".
- **Contextual tips** (`components/Coachmark.tsx`, one at a time, once each):
  the New loop prompt (`newloop.prompt`), "More ways to start" (`newloop.more`),
  and the Home checklist card (`home.checklist`).
- **Desktop guide panel** (`frontend/apps/desktop/guide.html`). Docks beside the
  web UI the first time this machine is connected: the welcome, the steps with
  their buttons and "2 of 4", the explore items once the four are done, and the
  key places. It re-reads `yard onboard --json` every few seconds while open.

---

## The onboarding agent (the conversational guide)

The same script is also a portable agent identity:

- `loop_onboarding_agent(register=true)` (`mcp_loops/server.py`) hands out the
  guide's persona + generic goal, the `WELCOME` and the steps, and can register it
  as `loop_onboarding` so it runs on the user's own connected CLI (claude or
  codex). Its tool shape is frozen by the `:8771` contract, so it returns the bare
  script; progress lives in `loop_onboarding_progress`.
- `build_onboarding_prompt()` assembles the prompt that session runs — the same
  four steps, with the tools behind each (`loop_creator_suggest` → `loop_save` →
  `start_loop` → `loop_disposition_record`).
- `loop_onboard_first_team(goal, name?, start?)` does suggest → validate → save →
  optionally start in one call — the "just get me running" path.

### Adding machines: "action needed on this machine"

Each new machine's CLIs need to be present **and logged in** before a loop can use
them. `origin_onboarding_check(required, origin?, timeout?)` (`mcp_loops/server.py`)
runs a live, auth-aware readiness probe and returns `blockers` (a CLI is missing),
`actions` (present but signed out, e.g. `gh`) and `ready`. The guide's job per
machine is to explain each item in plain words, offer the exact fix, and re-check
until `ready`.

## Still to build

| Piece | Where |
|-------|-------|
| Start the conversational guide automatically when the first machine connects (fleet snapshot `_origins_with_live` + a first-run flag) | new wiring |
| Per-machine "action needed — do it, or ask the guide" list over `origin_onboarding_check` | Machines page |

---

*See also: `docs/QUICKSTART.md` (the manual part this hands off from),
`docs/SETUP.md` (full setup), `docs/CONNECT.md` (the app-connect contract),
`docs/CREATOR.md` (`loop_creator_suggest`).*
