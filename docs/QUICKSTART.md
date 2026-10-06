> **Codex/Cursor beta:** after installing, run
> `~/loopyard/bin/yard runtime use codex --trusted-workspace` (or `cursor`),
> sign in through that CLI, then `~/loopyard/bin/yard start`.
> See [the current install guide](release/INSTALL.md). The saved setting replaces
> the older Claude-only prerequisite below. Trusted mode does not enforce roles.

# Quickstart — from a fresh clone to your first CLI connected

Welcome. This is the shortest path to a running **Loopyard** you can drive: bring
up the machine, open the app, and connect your first CLI. That's it — three
steps, all copy-pasteable. **From there, the app's Getting started checklist
takes over** (on Home) and walks you through the rest: describing what you want
done, starting your first team, and rating the result. So this page stays
deliberately short: we only need to get you to that first connection.

> **What "beta" means here.** You self-host the whole thing — the Hub, the
> dashboard, and the machine that runs the agents — all on your own box, with your
> own CLIs and keys. There's **no login and no paywall** in the beta. Nothing here
> talks to a hosted service.

Every command below is the real command from this repo, with the file it comes
from noted so you can check it yourself.

There are two ways in. If you were given the **installer** (a release host URL,
or a `loopyard-origin-<os>-<arch>.tar.gz` bundle), use **Fastest path** right
below; you don't need a clone, Python or npm. If you have a **git clone** of this
repo, skip to "Before you start".

---

## Fastest path — the installer (no clone)

This is the path the timed 5-minute acceptance runs step for step
(`scripts/phase_b_acceptance.sh --timed`, Leg T in `scripts/testing/phase_b_box.sh`):
a clean Ubuntu 24.04 box, non-root user, install to first finished loop in well
under 300 s.

**0. Prerequisites (once).** The bundle ships its own Python runtime; you need:

```bash
# Linux (Debian/Ubuntu; also WSL2 — see bundle/WINDOWS-WSL2.md)
sudo apt-get update && sudo apt-get install -y curl git tmux ca-certificates
# macOS (see bundle/MACOS.md) — install Homebrew first if `brew` is missing: https://brew.sh
xcode-select --install ; brew install tmux
# the agent CLI, then run `claude` once to log in (browser)
curl -fsSL https://claude.ai/install.sh | bash
```

`yard start` checks for `tmux`, `git` and `claude` and names **every** missing one
in a single message with the install command, so you won't find them one by one.

**1. Install** into `~/loopyard` (`install.sh`, header). The published `install.sh`
has its release host built in, so the one-liner needs nothing else:

```bash
curl -fsSL https://<release-host>/install.sh | sh
```

Offline, or handed a bundle file? Put the `.tar.gz` next to its `.sha256` and
`.sig` and run `sh install.sh --tarball ./loopyard-origin-<os>-<arch>.tar.gz`.

> **Pre-release caveat.** `install.sh` only installs a bundle that carries a
> signature from the release key pinned inside it (`docs/ops/RELEASE-SIGNING.md`).
> Until a signed v1 release is published, it refuses with exit code 3 and says why.
> If you hit that, stop and ask whoever gave you the installer; don't work
> around it.

It ends with `start it:  ~/loopyard/bin/yard start`.

**2. Start it:**

```bash
~/loopyard/bin/yard start
```

This brings up the server, the worker, a runner and the dashboard, then prints
`▶ OPEN LOOPYARD:  http://127.0.0.1:8811/app/` and the two `export` lines
(`MCP_LOOPS_URL`, `LOOPS_DATA_DIR`) for driving it from a shell. Copy those
exports into your shell. `~/loopyard/bin/yard status` checks it, and
`~/loopyard/bin/yard down` stops it.

**3. Open the app** at `http://127.0.0.1:8811/app/` (its **New loop** button
starts a team). A release bundle ships the web app there. A bundle built without
it (`"webApp": null` in `~/loopyard/BUNDLE.json`) serves only the legacy dashboard
at `http://127.0.0.1:8811/` (its **＋ Loop** button); `yard start` and `yard onboard`
print whichever address your bundle has.

**4. Start your first team.** `~/loopyard/bin/yard onboard` prints the steps and
the exact one-shot command for your install:

```bash
~/loopyard/runtime/bin/python3 -I -m mcp_loops.cli loop_onboard_first_team \
  '{"goal":"write a haiku about tidy code","start":true}'
```

The loop's result shows up on the dashboard (and at
`http://127.0.0.1:8811/api/loops/<name>`), and its files land in the loop's output
folder under `~/loopyard/data/_loops/_output/<name>/`.

To connect a CLI session to the Hub instead, see Step 3 below, using the
`MCP_LOOPS_URL` that `yard start` printed.

---

## Before you start

You need:

- **A clone of this repo** (the directory that holds this `docs/` folder).
- **Python 3** (the bring-up script builds its own isolated venv — it uses `uv`
  if you have it, otherwise the stdlib `venv`).
- **At least one agent CLI installed and logged in** — `claude` or `codex`. This
  is the "CLI" you'll connect; Loopyard drives *your* CLIs with *your* keys.

One thing worth knowing up front: **clone into a short path** (e.g. `~/loopyard`),
not a deeply nested folder. The worker binds a Unix socket, and Linux caps that
path length — a very deep clone fails loudly with `AF_UNIX path too long`. A normal
home-dir clone is nowhere near the limit. (See `docs/SETUP.md`, "Known limits —
install path depth".)

---

## Step 1 — Bring up this machine (the Hub)

From the root of your clone, run the one bring-up command:

```bash
scripts/loopyard-quickstart.sh
```

That's the whole setup. It is **headless, idempotent, and safe to re-run** — no
prompts, no file edits. In one shot it brings up
(`scripts/loopyard-quickstart.sh`, header + steps):

1. a dedicated venv with the pinned dependencies,
2. the **loops MCP server** in the background (this is the Hub your CLI talks to),
3. the **worker daemon** that actually runs agent sessions,
4. a **runner** attached (`python -m mcp_loops.yard up`) so loops can start with
   no extra config,
5. and it prints, copy-paste-ready, the three values you'll want:

```
MCP_LOOPS_URL      # e.g. http://127.0.0.1:8771/mcp  — your Hub's address
LOOPS_DATA_DIR     # where your loops + results live
LOOPYARD_INSTALL   # the path to this clone
```

The server binds `127.0.0.1:8771` by default (`docs/DEV-RUNTIME.md`; override the
port with `LOOPYARD_PORT`, which the bring-up honors —
`scripts/loopyard-quickstart.sh` header + `PORT="${LOOPYARD_PORT:-8771}"`, also in
`docs/SETUP.md`). If anything can't come up, the script **fails loudly**
with the exact log path — you won't be left guessing.

Check it's healthy any time:

```bash
python -m mcp_loops.yard status
```

This shows the runner and every tracked service with its pid, port, and a live
health probe of the server (`docs/SETUP.md` §0; `mcp_loops/yard.py`,
the `status` subcommand). It stays up between shell sessions, so you can close
your terminal
and come back.

---

## Step 2 — Open the app

**It's already running.** Step 1 brought the web server up for you and printed
its address as `▶ OPEN LOOPYARD:` near the end of its output
(`mcp_loops/yard.py`, `start`). It serves on `http://127.0.0.1:8811` by default
(`tracking_ui/loops_dashboard.py` — `LOOPYARD_DASH_PORT` defaults to `8811`,
`MCP_LOOPS_URL` to `http://127.0.0.1:8771/mcp`).

The app lives at **`http://127.0.0.1:8811/app/`**. Build it once from your clone
(it isn't checked in):

```bash
cd frontend && npm ci && npm run build
```

(Set `LOOPYARD_WEB_APP_DEFAULT=1` before Step 1 to make `/` open it too.) Its
**Home** page is your **Getting started** checklist:

1. **Connect a machine** — this one already counts once Step 1 is done.
2. **Describe what you want done** — in **New loop**, in a sentence or two.
3. **Review the team and start** — see who's on it and why, then start.
4. **See the result and rate it** — watch it in **Loops**, open the result, rate
   it good, ok or bad.

Each step ticks itself when it has actually happened (not when you click), and
its button opens the right page. After the four, a few optional next steps show
up (write a plan, connect a second computer, star a favorite agent). "Hide
checklist" puts it away for good.

It's a localhost-only view — keep the port bound to localhost; don't expose it.

> **Didn't come up?** The bring-up says so loudly and points at
> `data/_logs/dashboard.log`; the CLI and Hub still work without it. You can start
> the web server by hand any time — it's idempotent, so a running one is left
> as-is: `python -m tracking_ui.loops_dashboard`.

> **On a remote machine?** If your Loopyard runs on a VPS, tunnel the port to your
> laptop over SSH rather than exposing it:
> `ssh -L 8811:127.0.0.1:8811 you@your-box`, then open `http://127.0.0.1:8811/app/`
> locally.

---

## Step 3 — Connect your first CLI  ← the finish line

Now tell your agent CLI where the Hub is. Register the `mcp-loops` server in the
CLI session you want to drive Loopyard from — `claude` and `codex` both work
(`docs/SETUP.md` §1; `docs/CONNECT.md`):

```bash
claude mcp add --transport http mcp-loops "http://127.0.0.1:8771/mcp"
```

Use whatever URL the bring-up printed for `MCP_LOOPS_URL` if your port differs
from the `8771` default.

**No native MCP support in your CLI?** You can drive every tool from a shell
instead, using the bundled CLI shim (`docs/SETUP.md` §1;
`python -m mcp_loops.cli`). Export the address the bring-up printed, then call any
tool by name:

```bash
export MCP_LOOPS_URL="http://127.0.0.1:8771/mcp"   # the bring-up prints this
export LOOPYARD_INSTALL="/path/to/your/clone"       # …and this
python -P -m mcp_loops.cli get_loop_status '{"name":"anything"}'
```

(`MCP_LOOPS_URL` is required — with it unset the shim refuses rather than guessing
a host. The `-P` flag keeps a stray local `mcp_loops/` from shadowing your real
install.)

**That's the manual part done.** Your first CLI is connected to the Hub.

---

## You're connected — the checklist takes it from here

The moment your first CLI can reach the Hub, you don't need to hand-follow docs
anymore: open the app's **Home** and follow **Getting started** (Step 2). The same
steps are available from the terminal:

```bash
python -m mcp_loops.yard onboard          # add --json for the machine-readable form
```

This explains in plain language what a Loopyard team is and lists the same four
steps (`mcp_loops/yard.py`, `cmd_onboard`; the steps live in
`mcp_loops/onboarding.py`, the one source of truth for the app, the desktop app
and the CLI).

From your connected CLI session you can also ask the onboarding agent directly —
it's a first-class tool on the Hub:

```
loop_onboarding_agent(register=true)
```

That hands your CLI the guide's identity and the same walkthrough (connect a
machine → describe what you want done → review the team and start → see the
result and rate it), so the rest happens *inside* the conversation instead of in
a doc (`mcp_loops/server.py`, `loop_onboarding_agent`).

Prefer to skip straight to a running team? One call derives a team from a
plain-English goal, saves it, and starts it (`mcp_loops/yard.py` onboard help;
`mcp_loops/server.py`, `loop_onboard_first_team`):

```bash
python -P -m mcp_loops.cli loop_onboard_first_team \
  '{"goal":"build and ship a URL shortener with tests","start":true}'
```

Then watch it unfold in **Loops** in the app you opened in Step 2.

> **Want to bring a *second* machine into the team?** The easiest way is
> **Machines → Computers** in the app, which makes a one-time connect link. Or,
> from the other machine, join your Hub with a single command
> (`mcp_loops/yard.py`, the `origin up` subcommand):
>
> ```bash
> python -m mcp_loops.yard origin up --hub URL
> ```
>
> To pair a remote machine securely use
> `python -m mcp_loops.yard origin pair --hub URL --hub-fingerprint FP --pair-code -`
> (the code is read from stdin or `$LOOPYARD_PAIR_CODE`, never argv). See
> **[ONBOARDING-AGENT.md](ONBOARDING-AGENT.md)** for how getting started and the
> onboarding guide work.

---

## If something isn't right

- **`start_loop` says no runner attached** → run `python -m mcp_loops.yard up`
  from your install dir, then retry (`docs/SETUP.md` §0).
- **A service crashed** → re-run `scripts/loopyard-quickstart.sh` (or
  `python -m mcp_loops.yard up`);
  both are idempotent and a live process is left as-is (`docs/SETUP.md` §0).
- **`AF_UNIX path too long`** in `data/_logs/worker.log` → your clone path is too
  deep; re-clone somewhere shorter (`docs/SETUP.md`, install-path-depth note).
- **Codex-specific gotchas** (sandbox, model id) → see `docs/SETUP.md`, "Known
  limits — codex runtime".
- **A loop won't start: "claude is installed but not logged in"** → run `claude`
  once in a terminal and log in (or `claude auth login`), then start it again.
- **Reporting a bug** → `~/loopyard/bin/yard diagnostics` writes one
  `loopyard-diagnostics-<time>.tar.gz` (the service log tails, versions, and your
  config with secrets, tokens and keys redacted) under `~/loopyard/data/_diag/`
  and prints its path. Look it over, then attach it. `--out DIR` puts it elsewhere.

You're set. Enjoy your team.

## Uninstall

```bash
~/loopyard/bin/yard down     # stop the server, worker, dashboard and the tmux session it started
rm -rf ~/loopyard            # code AND state: config/, data/ (loops, results, logs), workspace/
```

That's everything the installer created. If you installed with `--dir D` or set
`LOOPYARD_HOME`, remove that directory instead (or as well). Want to keep your
loops and results? Copy `~/loopyard/config`, `~/loopyard/data` and
`~/loopyard/workspace` somewhere first. Recorded paths are absolute, so put
them back only into a fresh install at the same path (with it stopped).

What stays: the `claude` CLI and its login (`~/.local/bin/claude`, `~/.claude/`,
including the session history of the agents your loops ran). Those belong to
Claude Code, not Loopyard; remove them with Claude Code's own uninstall if you
want them gone. The `tmux`/`git` packages stay too.

---

*Deeper references: `docs/SETUP.md` (full setup), `docs/CONNECT.md` (driving loops
+ the result envelope), `docs/CREATOR.md` (build a team from a brief),
`docs/ONBOARDING-AGENT.md` (getting started + the onboarding guide).*
