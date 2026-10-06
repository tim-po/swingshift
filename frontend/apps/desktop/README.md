# @loopyard/desktop — a thin Electron shell for macOS

**Join as origin = the same CLI (P2.5).** The connect screen is a form: Hub URL,
Hub fingerprint and pairing code. Pasting one join link
(`loopyard://join?hub=…&fp=…&code=…[&install=…]`, URL-encoded) fills all of them.
The terminal join line from `yard hub pair-code` / MCP `origin_pair_code` is still
accepted as a fallback. Before Connect, the app reads the cert the Hub presents
(`hubPeek.mjs`, no data sent) and shows whether it matches the fingerprint. A
mismatch blocks Connect, and a Hub reached with no fingerprint must be trusted
explicitly first. **Connect** runs the same headless
`yard origin up --hub … [--hub-fingerprint …] --pair-code -` with the code on
stdin, never argv. The connected state is `yard origin status --json`, and
**Disconnect** is `yard origin down`. The app has no second engine and no second
daemon. Quitting the app leaves the origin serving.
- `originAgent.mjs`: the pure face (argv builders, join link + join-line parsers, fingerprint trust states, phase logic, lifecycle).
- `hubPeek.mjs`: reads a wss:// Hub's presented cert fingerprint (TLS handshake only) for the form's trust row.
- `prereqs.mjs`: the first-run setup check shown on the connect screen. It checks tmux, git, Claude Code and whether you're signed in (`claude auth status --json`), and whether the yard engine is installed. Each missing item gets a one-click fix, such as `brew install tmux`, `xcode-select --install`, the official Claude installer, `claude auth login`, or the join line's `install.sh`. The page only sends a check id. The command itself is picked in this module. `prereqSeams.mjs` holds the real process seams.
- `guide.mjs` + `guide.html` + `guidePreload.js`: the onboarding guide. The first time the origin is connected, the web UI loads at once and the existing `loop_onboarding_agent` docks on its right edge as a `WebContentsView`. Its welcome and steps come from `yard onboard --json`, and the app falls back to a short, labelled copy if the engine is older. Each step's **Go** sends only its key. `guide.mjs` maps the key to a fixed web-UI route (`/newloop`, `/loops`, …) and ticks the step. ✕ hides the guide. "Don't show on start" is remembered in `userData/onboarding-guide.json`. **Guide ▸ Onboarding guide** (⌘⇧G) reopens it.
- `boot.mjs`: the start-up order, for a fast time-to-UI. It registers the IPC handlers first, then paints the window, and only then starts the engine. A call the page makes before the engine is ready waits for it instead of failing. Status is read asynchronously (`readStatusAsync`), so a poll never freezes the window. It polls every 500 ms while a join is in flight and every 1.5 s when idle. The connect screen shows the join's stages (*Starting Loopyard → Pairing with the Hub → Opening your workspace*) with the elapsed time.
- A Hub's `yard hub pair-code` (and the `origin_pair_code` tool) now also returns `joinLink`, a `loopyard://join?…` link. `mcp_loops/origin_onboard.py` builds and parses it with the same rules as `parseJoinLink`, and `joinLink.py.test.mjs` round-trips it both ways against the real engine.
- `yardSeams.mjs`: the real child_process seams. `originAgent.cli.test.mjs` drives them against the real `yard` CLI.
- CLI lookup order: `$LOOPYARD_YARD` (one path), then `$LOOPYARD_PYTHON -m mcp_loops.yard`, then `~/loopyard/bin/yard` (install.sh), then `yard` on PATH.
- Owner steps on a Mac: `bundle/MACOS.md`.

A dead-simple desktop wrapper that loads the **same** React UI as the web app
(`apps/web`) in one `BrowserWindow`. No app logic lives here — this is packaging
only. The UI is built with a hash router and relative asset base so it runs over
`file://` offline, or you can point it at the hosted `/app` URL behind login.

## Layout
- `main.js` — the Electron main process: one window, external links open in the
  browser, no node integration in the page; wires the face to the connect screen.
- `connect.html` + `preload.js` — the first screen (Run on this Mac, or Join a Hub) and its narrow IPC bridge.
- `guide.html` + `guidePreload.js` — the docked onboarding-guide panel and its (narrower) bridge.
- `electron-builder.yml` — the macOS packaging target (dmg + zip, arm64 + x64).
- `scripts/smoke.mjs` — headless acceptance (see below).

## Run it (any host)
```sh
npm install                 # from frontend/ — installs the workspace
npm run --workspace @loopyard/desktop start           # launch Electron
```
The app never ships its own copy of the web UI (R18): in both modes (Run on this
Mac, Join a Hub) the window loads `http://127.0.0.1:<dash-port>/app/`, the port read
from `yard status --json`. Point it at a dev CLI with `LOOPYARD_YARD=/path/to/bin/yard`.

## Package the real `.app` — on a Mac (owner)
The build host here has **no macOS**, so the final `.app`/`.dmg` is produced and
verified by the owner on a Mac:
```sh
npm run --workspace @loopyard/desktop dist:mac
```
This stages the origin bundle (scripts/stage-origin.mjs) then runs `electron-builder --mac`; supply a signing identity
for notarization. `dist-app/` gets the `.dmg` + `.zip`.

## Headless acceptance (this host)
```sh
npm run --workspace @loopyard/desktop smoke
```
`smoke.mjs` proves, without a Mac: `main.js` passes `node --check`; the
`electron-builder` mac config is well-formed; nothing ships or builds a `renderer/`
and `main.js` never `loadFile`s one (R18). If `electron` is installed it also launches the
shell headlessly (xvfb) and confirms the window loaded the app entry
(`SMOKE_OK`); otherwise that one step is skipped **loudly** — never faked.
```
