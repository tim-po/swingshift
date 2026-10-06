# Installing Loopyard (closed beta)

Loopyard installs are **portal-gated**: every release asset is fetched with a
short-lived download token minted for your account, and the machine enrolls
itself into your hub directory as the last step. There is no public download
link — old `/download` links on a dashboard redirect to the portal.

> **Doc-lint.** Every command in a `sh` block below appears verbatim in the
> installer, `yard`, or a test (`tracking_ui/tests/test_install_doc_lint.py`
> enforces it; `~/loopyard/bin/` is the default install root and is stripped
> before matching). The one-liner example is checked byte-for-byte against the
> portal's own renderer (`control_plane.onboard.PortalBringUp`).

## 1. Prerequisites

Loopyard runs agents through your own Claude, Codex, or Cursor CLI login.
Install `git`, `tmux`, and your chosen provider CLI, then sign in through that
CLI. This beta supports trusted local repositories with Codex/Cursor; manager
and reviewer roles are instructions, not enforced file permissions in this mode.

macOS: install tmux with Homebrew and git with the Command Line Tools.
Linux (Debian/Ubuntu): install the `tmux` and `git` packages.

After installing Loopyard, before starting the stack, save your provider choice:

```sh
~/loopyard/bin/yard runtime use codex --trusted-workspace
~/loopyard/bin/yard runtime show
```

Use `cursor` in place of `codex` for Cursor, or `claude` without the trust flag
for Claude role profiles. Sign in first with `codex login`, `cursor-agent login`,
or `claude auth login`. `yard start` checks the selected CLI's login.
`yard runtime show` displays the saved choice; no credentials are stored there.
The choice survives shell exit and upgrades. Saved Claude steps route to the
selected replacement; explicit other providers are preserved.

To switch later, finish/stop loops, run `yard down`, change the provider, then
`yard start`. Existing conversations do not transfer between providers.

Supported targets: macOS (Apple silicon, Intel) and Linux (x86_64, aarch64).
You also need `curl` (or `wget`) and OpenSSH ≥ 8.1 (`ssh-keygen -Y verify`
checks each bundle's release signature) — both are stock on macOS 10.15+ and
current Linux.

## 2. The install journey

For Codex/Cursor, set these non-secret preferences in your terminal before
running the portal command. The installer saves them before enrollment starts
the services (use `cursor` instead of `codex` for Cursor):

```sh
export LOOPYARD_RUNTIME=codex
export LOOPYARD_TRUSTED_WORKSPACE=1
```

For a downloaded installer, the equivalent options are `--runtime codex
--trusted-workspace`. Existing saved provider settings are preserved on re-run.

1. **Sign in.** Open the Loopyard portal and choose **Sign in with Google**,
   using the account your invite was sent to.
2. **Set up on this machine.** Go to **Set up** and press
   **Set up Loopyard on this machine**. The portal mints a personalized
   one-liner and shows it with a **Copy command** button. It is single-use and
   expires after 15 minutes; press the button again for a fresh one.
3. **Run the one-liner** in a terminal on the machine you are setting up. It
   looks like this (your portal URL and tokens differ):

<!-- doc-lint: portal-one-liner -->
```sh
( f="$(mktemp "${TMPDIR:-/tmp}/loopyard-install.XXXXXX")" || exit 1; curl -fsS -H 'Authorization: Bearer lyr_EXAMPLE' https://portal.example/releases/latest/install.sh -o "$f" || { rm -f "$f"; echo 'loopyard: could not download install.sh (the link may have expired, or the network failed). Nothing was installed. Re-copy the command from the portal and run it again.' >&2; exit 1; }; if [ -s "$f" ] && [ -z "$(tail -c 1 "$f")" ] && head -n 1 "$f" | grep -q '^#!' && [ "$(tail -n 1 "$f")" = '# loopyard-install-end' ] && sh -n "$f" 2>/dev/null; then :; else rm -f "$f"; echo 'loopyard: the downloaded install.sh is incomplete or corrupt. Nothing was installed. Re-copy the command from the portal and run it again.' >&2; exit 1; fi; LOOPYARD_PORTAL_URL=https://portal.example LOOPYARD_INSTALL_TOKEN=lyr_EXAMPLE LOOPYARD_ENROLL_TOKEN=lyd_EXAMPLE sh "$f"; rc=$?; rm -f "$f"; exit $rc )
```

   What it does, in order:
   - fetches the gated `install.sh` (the `lyr_` token travels only in an
     `Authorization` header — never in a URL or argv — and redirects are not
     followed) into a temporary file, never piped into `sh`.
     A failed download (expired link, 401/404, network) or a cut-off file
     (it must end with the `# loopyard-install-end` line) stops right there
     with "Re-copy the command from the portal"; nothing is installed;
   - downloads the bundle for your OS/arch, verifies it against `SHA256SUMS`
     and its Ed25519 release signature, and installs it into `~/loopyard`;
   - because `LOOPYARD_ENROLL_TOKEN` is set, finishes with `yard enroll`:
     registers this machine's hub fingerprint with the portal, claims a
     device token with the single-use enrollment token, writes it into the
     hub's dashboard allowlist, and starts the dashboard behind the hub guard.
     It prints `Portal enrollment ready: <url>` when done.
4. **Open your dashboard.** Back in the portal the status moves from
   *pending* → *registered* → **online**; press **Open your dashboard**. The
   handoff lands on your own hub, which admits only this account's device
   token — a missing or forged token gets `401`.

Without `LOOPYARD_ENROLL_TOKEN` the installer never enrolls; it only installs
and prints the next step (`~/loopyard/bin/yard start`).

### Your hub's public URL

The portal hands you off to `<public URL>/_lyp/enter`, so the URL your hub
registers must be one your browser can reach. `yard enroll` reads it from
`LOOPYARD_PUBLIC_URL`; export it in the terminal **before** running the
one-liner (the installer passes it through to `yard enroll`). It must be
`https://`, with no user/password, `?query` or `#fragment` — anything else
fails enrollment with exit `3` before the enrollment token is spent.

If it is unset, the hub registers `https://127.0.0.1:<port>` — reachable only
from a browser on this same machine — where `<port>` is `LOOPYARD_GUARD_PORT`
(default `8812`). The guarded dashboard serves the hub's own self-signed
certificate there, so the browser asks you to accept it once. Put a tunnel or
reverse proxy with a real certificate in front of that port and set
`LOOPYARD_PUBLIC_URL` to its address to reach the hub from other devices.

### Re-running enrollment

Enrollment is idempotent. The first successful run saves the portal, hub id,
fingerprint, public URL and device token to `enroll.json` in the hub's state
dir (mode `0600`; the enrollment token itself is never written to disk). A
re-run — the installer again, or directly:

```sh
~/loopyard/bin/yard enroll
```

— finds that file, does **not** claim again (the enrollment token is
single-use and already spent), heartbeats the portal with the saved device
token, re-provisions it into the dashboard allowlist and restarts the guarded
dashboard only if it is not running, then exits `0`. It still needs
`LOOPYARD_PORTAL_URL` and a non-empty `LOOPYARD_ENROLL_TOKEN` in the
environment, and refuses (exit `3`) if the portal URL, the hub fingerprint or
`LOOPYARD_PUBLIC_URL` differ from what was saved — keep the same values you
enrolled with. `yard down` stops the guarded dashboard with the rest of the
stack.

### Day to day

```sh
~/loopyard/bin/yard start
~/loopyard/bin/yard status
~/loopyard/bin/yard down
~/loopyard/bin/yard --version
```

`yard start` refuses to adopt a server that belongs to a *different* install
(another install or state root answering on the port) and exits `3`; stop that
one first or pick another `--port`.

## 3. macOS desktop app

The desktop app is the same workspace, native on your Mac, and can run the
stack on the Mac itself (**Run on this Mac**). It checks the portal for a newer
release and shows an update banner; **Update stays disabled while any loop is
live** — stop or finish your loops first. A code tree the app manages carries
a `DESKTOP_MANAGED` marker, and `yard update` in it refuses and points you at
the portal release page: update the app, not the tree.

Closed-beta builds are ad-hoc signed, so the first launch needs a one-time
right-click → **Open** → **Open** to get past Gatekeeper.

## 4. Upgrade

Upgrading is re-running the installer against the same root. From the portal,
press **Set up Loopyard on this machine** again and run the new one-liner
(tokens are single-use). To see what you have and the upgrade hint for a
non-portal bundle:

```sh
~/loopyard/bin/yard update
```

An upgrade runs `yard down` → stages the new version beside the root → carries
`config/`, `data/` and `workspace/` across → swaps by rename →
`yard start` + status check → drops the old code. The root path never changes,
so every absolute path in your state stays valid.

- **Loops still running?** The installer refuses (exit `4`) rather than swap
  code under live agents. Stop them, or pass `--force` and restart them after.
- **Interrupted?** A crash leaves `<root>.new-*` / `<root>.old-*`; the next run
  completes or reverts from what it finds.
- **Pinned to a version?** Pass `--version` (e.g. `--version 0.1.0`). Arguments
  go after `sh -s --` at the end of the one-liner.

Offline / CI installs take a local bundle that sits next to its `.sha256` and
`.sig`:

```sh
sh install.sh --tarball ./loopyard-origin-<slug>.tar.gz
```

## 5. Rollback

- **Automatic.** If the new version fails its post-swap start/status check,
  the installer puts the old tree back and exits `1` — you are left on the
  version you had.
- **Manual.** Re-run the installer with an explicit older `--version`. When
  `latest` is *older* than what you run (a withdrawn release), a plain re-run
  refuses with exit `2` and tells you the exact `--version` to go back to;
  `--allow-downgrade` accepts that without naming a version. State dirs are
  moved, never deleted, in either direction.

## 6. Installer exit codes

| code | meaning |
| ---- | ------- |
| 0 | installed, upgraded, or already current |
| 1 | upgrade failed and was reverted |
| 2 | refused (not a bundle dir, unsupported OS/arch, bad args, downgrade without consent) |
| 3 | download / verify failure, or portal enrollment failed |
| 4 | loops still running (use `--force`) |

If enrollment fails (exit `3` after a successful install), the bundle is in
place and re-running over the same version skips straight to enrollment. A
failure *before* the claim (portal unreachable, bad `LOOPYARD_PUBLIC_URL`,
rejected registration) leaves the enrollment token unspent, so the same
one-liner works again until it expires; after a successful claim, re-run
`yard enroll` as above. If the token expired or was spent without a saved
`enroll.json`, press **Set up Loopyard on this machine** for a fresh one-liner.

Trusted-workspace mode runs agents with your user permissions: it disables
Loopyard's OS isolation cage as well as role restrictions. The current Linux
cage assumes Claude session storage and blocks the local socket Codex needs.
Use this mode only for your own trusted repositories. An explicit conflicting
`LOOPS_ISOLATION` override is refused at startup, not silently weakened.
Claude role-profile mode retains the existing isolation behavior.

The self-hosted dashboard uses its own browser-compatible P-256 HTTPS
certificate, separate from the Hub's Ed25519 pairing identity. The default
`https://127.0.0.1:8812` certificate is self-signed: trust this local certificate
in your browser, or use a reverse proxy with a trusted certificate. The portal's
Google login does not make the local certificate publicly trusted. Re-enrollment
keeps the dashboard certificate stable while valid.
