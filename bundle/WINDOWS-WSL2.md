# Windows origin onboarding — design (WSL2, PHASE-B D3)

HUB-FABRIC P2.5 (3), Windows half. **Status: design plus a static/behavioural smoke.
Nothing here has run on a real Windows PC.**

## Decision

Windows-native is **not** a target. The worker substrate is tmux, and agents run
as POSIX processes. A Windows PC joins by running the **Linux bundle inside a
WSL2 distro**, driven by the **same CLI line** a Linux box pastes. There is no
Windows code path in the engine and no `.exe` bundle; `origin_bundle` still
refuses `windows-amd64` with `WINDOWS_REFUSAL`, and `install.sh` refuses
MINGW, MSYS and CYGWIN with a WSL2 pointer.

```
Windows PC
 └─ join-origin.ps1  (thin: checks WSL2 + distro, feeds the line on stdin)
     └─ wsl.exe -d Ubuntu-24.04 -- sh -s   ◄── stdin: the Hub's minted join line
         └─ [curl …/install.sh | sh &&] printf CODE | ~/loopyard/bin/yard origin up --hub wss://… --hub-fingerprint FP --pair-code -
             └─ yard start (engine + worker + runner, inside WSL) → pair → detached daemon → connected
```

Inside WSL, `uname -s` is `Linux`, so `install.sh` fetches the linux-x86_64
bundle (or linux-aarch64 on Windows-on-ARM). From there on it is the Linux
acceptance path, byte for byte.

## Why each piece

| concern | design |
|---|---|
| **One CLI path** | The wrapper never parses the line into its own command. It hands the Hub's line verbatim to `sh -s` inside the distro. Status is `yard origin status` and disconnect is `yard origin down`, both run in the distro. |
| **Pair code off argv** | The line reaches WSL on **stdin** (`$line \| wsl.exe -d D -- sh -s`), so it is never in `wsl.exe`'s command line, which Task Manager and WMI can read. Inside WSL, `printf` is a shell builtin feeding `--pair-code -` (the P5 rule). |
| **Don't burn a code on a box that can't run** | A preflight runs in the distro before the line is sent: `tmux`, `git` and `curl` present, and `claude` on PATH or at `~/.local/bin/claude`. If it fails, the wrapper exits 2 with the exact `apt-get` / installer command and the code stays unspent. |
| **Error paths** | No WSL, a missing distro, and a WSL1 distro each exit 2 with the fixing command (`wsl --install -d …`, `wsl --set-version … 2`). A non-join line exits 2 before anything runs. yard's own non-zero exit is passed through (bad code rc 2, unreachable Hub rc 2), and yard's one-line reason is printed. |
| **Staying up** (WSL shuts the VM down when idle; a PC reboots) | `-KeepAlive` registers a per-user **logon Scheduled Task**: `wsl.exe -d D -- sh -lc "~/loopyard/bin/yard origin up --hub URL; exec sleep infinity"`. `origin up` is idempotent and the box is already paired, so no code is needed: a live origin is left alone and a dead one comes back. `sleep infinity` holds the distro open so the detached daemon is not reaped. |
| **Networking** | The origin only **dials out** to the Hub (wss), and WSL2's default NAT allows outbound connections. No port forwarding, firewall rule or mirrored networking is needed. The Hub sees the PC's address. |
| **Encoding** | `WSL_UTF8=1` makes `wsl --list --verbose` print UTF-8 instead of UTF-16. Stray NULs are stripped anyway. The join line is ASCII, so the default `$OutputEncoding` of Windows PowerShell 5.1 is safe. |
| **Desktop app** | Out of scope for the beta; the macOS shell is the app. A Windows build of the same Electron face would set `LOOPYARD_YARD` to a two-line `yard.cmd` shim (`wsl.exe -d D -- ~/loopyard/bin/yard %*`), and **stdin is inherited through `wsl.exe`**, so `--pair-code -` works unchanged. That is not built, and it would need its own smoke. |

## Owner steps (on a Windows 10 22H2+ / 11 PC)

```powershell
# admin PowerShell, once
wsl --install -d Ubuntu-24.04          # reboot if asked; open "Ubuntu 24.04" once and create your user
```
```sh
# inside Ubuntu, once
sudo apt-get update && sudo apt-get install -y tmux git curl
curl -fsSL https://claude.ai/install.sh | bash && ~/.local/bin/claude   # log in once
```
```powershell
# normal PowerShell: join (paste the line minted by `yard hub pair-code` / origin_pair_code)
powershell -ExecutionPolicy Bypass -File join-origin.ps1 -KeepAlive
powershell -ExecutionPolicy Bypass -File join-origin.ps1 -Status     # → origin status: connected
```

Until a release host exists, copy the linux-x86_64 bundle, its `.sha256` and
`install.sh` into the distro, then run `sh install.sh --tarball …` there first.
The minted line without the `curl` prefix then works unchanged.

Verification is the same as for the Mac (bundle/MACOS.md §3–5):
- The device shows up in the Hub registry.
- `origin_run(["uname","-sr"])` returns `Linux …-microsoft-standard-WSL2`, plus an audit record.
- A bad code gives rc 2.
- After a reboot, the logon task brings the origin back to `connected`.

## Static smoke (what was actually run, on Linux)

| check | how | result |
|---|---|---|
| `install.sh` refuses MINGW64/MSYS/CYGWIN with a WSL2 pointer; a WSL2 `uname` (Linux x86_64/aarch64) takes the linux bundle | `mcp_loops/tests/test_onboarding_cross_platform.py` (faked `uname`, file:// release) | in the gate |
| bundle builder refuses `windows-amd64` | same test file | in the gate |
| `join-origin.ps1` parses and behaves: refusals for no WSL / no distro / WSL1 / bad line, preflight blocks the join (code unspent), the line is sent verbatim on stdin, the code is on no argv and in no output, rc is passed through, `-Status` / `-Down` run the same verbs, and `-KeepAlive` emits the re-join command | `scripts/windows/smoke_join_origin.sh` in `mcr.microsoft.com/powershell:lts-ubuntu-22.04` (pwsh 7.4) with a stub `wsl.exe` | 15/15, ending in `WIN_WSL_SMOKE_OK` |

**Not proven:**
- real `wsl.exe` and its stdin plumbing
- Windows PowerShell 5.1 (the smoke ran pwsh 7.4)
- `Register-ScheduledTask` (absent on Linux; the wrapper prints the command instead)
- the WSL idle-shutdown behaviour
- a live join from a PC
