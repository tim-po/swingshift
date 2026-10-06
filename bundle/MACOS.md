# macOS origin onboarding — what is proven here, what the owner runs on a Mac

HUB-FABRIC P2.5 (3). A Mac joins with **the same CLI path** as a Linux box: the
macOS bundle ships the same `mcp_loops/yard.py`, so the Hub's minted line runs
the same headless `yard origin up` (yard start → pair once → detached daemon →
wait connected). The desktop app's Connect button runs that same line
(`frontend/apps/desktop`, see "The app" below).

## App-first flow (the recommended path for a new user)

Most of onboarding now happens inside the app. The terminal runbook further down
is the fallback and the way the owner validates the pieces separately.

1. **Install and open the app.** The first screen checks the prerequisites and
   shows one row each for tmux, git, Claude Code, the Claude sign-in and the
   Loopyard engine. Each missing item has a fix button: `brew install tmux`,
   `xcode-select --install`, the official Claude installer, `claude auth login`,
   or the join link's `install.sh`. Safe fixes run in the app and stream their
   progress into the row. Nothing asks you to open Terminal. **Sign in to
   Claude** runs `claude auth login` with a stdin pipe: the row shows an "Open
   sign-in page" button for the URL the CLI prints and, when the CLI asks
   "Paste code here", a code field whose value the app writes to that CLI's
   stdin. The sign-in state is read with `claude auth status --json`, falling
   back to bare `claude auth status` on a CLI that rejects `--json`.
2. **Paste the join link** from the Hub
   (`loopyard://join?hub=…&fp=…&code=…[&install=…]`). The Hub owner gets it
   from `yard hub pair-code`, which prints it under the terminal line, or from
   the `joinLink` field of `--json` and the `origin_pair_code` tool. It fills the Hub URL,
   fingerprint and pairing code fields, and the code is never shown. The app
   reads the certificate the Hub presents and shows whether it matches. A
   mismatch blocks Connect. With no fingerprint you must click "Trust this
   fingerprint". The old terminal line from `yard hub pair-code` also works in
   the same field.
3. **Click Connect.** The app runs `yard origin up` itself, with the code on
   stdin. Under the pill, a progress strip shows *Starting Loopyard on this
   computer → Pairing with the Hub → Opening your workspace*, with the seconds
   elapsed. Status is polled every 500 ms during the join. The moment
   `yard origin status --json` reads connected, the window switches to the web
   UI. The connect screen paints before the app touches the engine, so the
   window never waits on a slow CLI start.
4. **The onboarding guide docks beside the UI** the first time you connect. It
   is the existing `loop_onboarding_agent`, read through `yard onboard --json`:
   the guide's persona and welcome, then the steps describe → suggest → save →
   run → watch. A step is ticked only when the engine reports it done in that
   payload (the app re-reads it every 8 s while the guide is open), not when you
   click. "What the guide will do in a Claude session" shows the agent's own
   prompt, with a Copy button. Each
   **Go** button moves the web UI to where that step happens (New loop, Loops),
   and "Where things are" links Origins and Sessions. ✕ hides the guide, "Don't
   show on start" is remembered, and **Guide ▸ Onboarding guide**
   (⌘⇧G) brings it back.

## Proven on Linux (no Mac needed)

| what | how | evidence |
|---|---|---|
| both Mac bundles cross-assemble | `python3 -m mcp_loops.origin_bundle build --target macos-arm64\|macos-x86_64 --out DIR` | rc 0, 35 wheels checked, 0 min-OS tag failures |
| native code is Mach-O for the target, with no host ELF | `scripts/macos_bundle_smoke.py <tarball>` reads the Mach-O headers | 14/14 Mach-O arm64 (and 14/14 x86_64), 0 ELF |
| same CLI | the smoke checks that `yard.py`, `origin_client.py` and `origin_onboard.py` in the tarball are byte-identical to `git show <gitSha>:…`, and that the P2.5 verbs are present | 13/13 checks pass per arch, ending in `MAC_BUNDLE_SMOKE_OK` |
| `install.sh` on a Mac | with `uname` faked as Darwin/arm64, plus the stock macOS tool shape (`shasum`, no `sha256sum`), install.sh fetches exactly `loopyard-origin-macos-<arch>.tar.gz`, verifies it and installs `<dir>/bin/yard`; the other arch never takes it | `test_onboarding_cross_platform.py`, plus the smoke run on the real bundle |
| desktop seam | the Connect button runs `yard origin up --hub … [--hub-fingerprint …] --pair-code -` with the code on stdin; the connected state comes from `yard origin status --json`; Disconnect runs `yard origin down` | vitest in `frontend/apps/desktop` (`originAgent*.test.mjs`), including tests against the real CLI |
| app-first onboarding | the prerequisite rows and the exact command each fix runs; the join link → form → exact `yard origin up` argv; the Hub cert check against a real loopback TLS server; the guide panel's content from the real `yard onboard --json`, its step → route table, and its remembered dismiss; the minted `joinLink` round-tripped Python ⇄ app against the real engine; paint-before-engine boot order, async status reads, the 500 ms join cadence and the progress strip | vitest in `frontend/apps/desktop` (`prereqs`, `joinLink`, `joinLink.py`, `hubPeek`, `connect`, `guide`, `timeToUi`), plus `mcp_loops/tests/test_onboard_json.py` and `test_origin_join_link.py` |

**Not provided by Linux (the owner must run these):** that dyld and Gatekeeper load
the Mach-O runtime, and a live `yard origin up` from a Mac to a Hub, followed by a
dispatched unit.

## Owner runbook (on the Mac)

### 0. Prerequisites (once)

```sh
xcode-select --install            # git (or: brew install git)
brew install tmux                 # the worker substrate; yard start refuses without it
curl -fsSL https://claude.ai/install.sh | bash   # → ~/.local/bin/claude, logged in once: `claude`
```

### 1. Get the bundle onto the Mac

Nothing is published yet, and this loop never publishes a release. Build it on
the build host at the commit you want, then copy three files across:

```sh
# build host
python3 -m mcp_loops.origin_bundle build --target macos-arm64 --out /tmp/mac   # Intel: macos-x86_64
python3 scripts/macos_bundle_smoke.py /tmp/mac/loopyard-origin-macos-arm64.tar.gz   # expect MAC_BUNDLE_SMOKE_OK
scp /tmp/mac/loopyard-origin-macos-arm64.tar.gz{,.sha256} install.sh  you@mac:~/Downloads/
```

```sh
# Mac
cd ~/Downloads && sh install.sh --tarball ./loopyard-origin-macos-arm64.tar.gz
# → loopyard-install: verified … / installed Loopyard 0.0.0+g<sha> into /Users/<you>/loopyard
~/loopyard/bin/yard origin status
# → "origin status: no daemon has run on this box"   ← proves the Mach-O runtime loads (dyld OK)
```

Once a release host exists, the minted line carries the fetch itself
(`curl -fsSL <url>/install.sh | sh && …`). `curl` sets no quarantine attribute,
so Gatekeeper does not get involved.

If a browser or AirDrop was used to copy the tarball, clear the quarantine
attribute before installing:
`xattr -dr com.apple.quarantine ~/Downloads/loopyard-origin-*.tar.gz`.

If a binary is killed on launch (`Killed: 9`, an arm64 signature problem), record
it and re-sign ad hoc: `codesign --force -s - ~/loopyard/runtime/bin/python3.13`.

### 2. Mint the join line (on the Hub host)

```sh
yard hub pair-code --hub-url wss://<hub-host>:<port> --label "Tim's Mac"
# or the MCP tool: origin_pair_code(hub_url="wss://<hub-host>:<port>", label="Tim's Mac")
```

The Hub must run with TLS (`python -m mcp_loops.hub_serve --tls --host <addr> --port <p>`)
and be reachable from the Mac.

### 3. Join (on the Mac): paste the line, as-is

```sh
printf '%s\n' <CODE> | ~/loopyard/bin/yard origin up --hub wss://<hub-host>:<port> --hub-fingerprint <FP> --pair-code -
```

Expected: rc 0, `paired → device dev_…`, `origin up → wss://… (mtls-pinned)`,
`state: connected`. If you want the dispatch check in step 4, add
`--allow-run uname`.

Then check:
- `~/loopyard/bin/yard origin status` shows `connected`.
- Re-running the same line prints `origin already up … leaving it` with rc 0.
- On the Hub, the device list (`loop_origin_list` / Devices) shows the Mac as live.

### 4. One dispatched unit

From the Hub: `origin_run(argv=["uname","-sm"], origin="<device id or label>")`.
Expected: `exitCode 0`, `stdout "Darwin arm64"`, plus an audit record on the Hub.

### 5. Error paths

Run these **before** step 3, or after `~/loopyard/bin/yard origin down`: while
an origin is live, `up` is a no-op and returns `already up` with rc 0.

| case | command | expected |
|---|---|---|
| bad pair code | `printf 'BADBADBA\n' \| ~/loopyard/bin/yard origin up --hub wss://… --hub-fingerprint <FP> --pair-code -` | rc 2 `Hub refused the pairing claim: pairing_failed: unknown pairing code` |
| Hub unreachable | the same line with a wrong port | rc 2 `hub_unreachable: cannot reach the Hub at …` |
| no claude | before installing claude (step 0), or with `~/.local/bin/claude` temporarily renamed | non-zero `yard start: claude CLI not found — install it or set LOOPS_CLAUDE_BIN` |

### 6. The app (the app-first flow above, on a real Mac)

```sh
cd frontend && npm ci && npm run --workspace @loopyard/desktop dist:mac   # → apps/desktop/dist-app/*.dmg (unsigned beta)
```

Open the app (right-click → Open the first time) and follow "App-first flow"
above: fix any red prerequisite rows from the app, paste a freshly minted join
link (codes are single-use), check the fingerprint row reads "matches ✓" and
click **Connect**. The pill shows `Joining…` with the three-stage progress
strip ticking through, then `Connected ✓`. The window
switches to the web UI with the onboarding guide docked on the right. Click
**Go** on "Describe your goal": the web UI should move to New loop.

While it is connected, `~/loopyard/bin/yard origin status` in Terminal shows the
same `connected`, because it is one daemon. **Disconnect** is exactly
`yard origin down`. Quitting the app leaves the origin serving, the same as
closing the terminal after the CLI `up`.

The app finds the CLI at `~/loopyard/bin/yard`; set `LOOPYARD_YARD` to override.
If you pasted a bad code, the connect screen shows the CLI's own error line.

**Report back:** the stdout/rc of steps 1, 3, 4 and 5, plus screenshots of the
prerequisite rows, `Connected ✓`, and the web UI with the guide docked.
