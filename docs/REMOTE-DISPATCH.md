# Remote dispatch — security notes

What the Hub can do to another computer (an **origin**) when it probes its AI
tools or starts a loop on it, what stops it from doing more, and what the
coordinator must check before the first real hop to a Mac.

Two remote operations are covered:

- **`capabilities.probe`**: the Hub asks an origin which AI CLIs it has and
  whether they are signed in (`loop_origin_capabilities`, the Computers view).
- **Remote `loop_start(name, origin=…)`**: the Hub ships a saved loop config to
  the origin, which saves it (if absent) and runs it on **its own** engine.

Every link below points at the code that enforces the rule and the test that
proves it. Each rule is enforced at a named function; where the rule is checked
on both sides, both are listed.

## 1. Manifest gating

Each origin advertises a small versioned manifest on every connect (what RPCs
and verbs it permits). The Hub may only send a unit the origin advertised **on
its current session**, and the origin checks again on receipt.

| Rule | Enforced in | Proven by |
|---|---|---|
| `capabilities.probe` is a **read-only** RPC on the fixed wire allowlist: no argv, no exec, no fs. The origin answers by running its own `probe_local_clis` on its own PATH/HOME. | [`origin_proto/wire.py`](../mcp_loops/origin_proto/wire.py) `RPC_METHODS`; [`origin_proto/agent_core.py`](../mcp_loops/origin_proto/agent_core.py) `OriginAgentExecutor._probe_clis` | [`test_remote_capabilities_probe.py`](../mcp_loops/tests/test_remote_capabilities_probe.py) `test_hub_probes_far_origin_clis_over_the_channel` |
| The default manifest includes `capabilities.probe` in `READ_RPCS` (agent + reads, **no exec, no fs**). A manifest with an explicit `rpcs` list that leaves it out gets refused. | [`origin_policy.py`](../mcp_loops/origin_policy.py) `READ_RPCS`, `OriginManifest.default`, `OriginManifest.check` | `test_capabilities_probe_is_a_default_read_rpc`, `test_far_origin_manifest_without_the_rpc_is_refused`; [`test_origin_policy.py`](../mcp_loops/tests/test_origin_policy.py) `test_default_manifest_is_no_exec_no_fs` |
| **Hub side:** a unit the target did not advertise is refused before it is sent. | [`origin_proto/channel.py`](../mcp_loops/origin_proto/channel.py) `ChannelServer._call_rpc` (`live.manifest.check`) | `test_origin_policy.py` `test_unadvertised_agent_and_rpc_rejected_at_hub` |
| **Origin side:** the origin re-checks the unit against its own manifest and the session owner, then the executor refuses anything off the wire allowlist. | `channel.py` `ChannelClient._refusal`; `agent_core.py` `OriginAgentExecutor.__call__` | [`test_origin_channel.py`](../mcp_loops/tests/test_origin_channel.py) `test_call_rpc_refuses_off_allowlist_method` |
| The manifest is accepted only **after** the peer proves its device key; an unauthenticated peer cannot plant one. | `channel.py` `ChannelServer._handshake` (`_policy.receive`) | `test_origin_policy.py` `test_unauthenticated_peer_cannot_plant_a_manifest` |
| `loop.start` needs the manifest's **`agent`** verb. The default manifest grants it. | `origin_policy.py` `OriginManifest.check` (`AGENT_METHODS`) | `test_unadvertised_agent_and_rpc_rejected_at_hub` |
| A probe answer can never claim more than the local record shape: `authed` is never upgraded (anything but `True`/`False`/`"unknown"` becomes `"unknown"`), `applied` is forced `False` when `authed` is `False`, and every record is tagged `remote: true`. | [`origins_probe.py`](../mcp_loops/origins_probe.py) `normalize_remote` | `test_normalize_remote_never_upgrades_authed`, `test_normalize_remote_bad_answers_are_honest_refusals` |

### What a remote `loop.start` can do (read this before enabling `agent`)

With `agent` advertised, the Hub can **define and start a new loop** on the
origin: `loop.start` carries the Hub's saved config, and the origin saves it if
it has no loop by that name. Only two things limit this:

1. **The manifest's `agent` verb**, from the table above.
2. **The origin's dispatchable-Project allowlist (§6.4)**, which is
   **default-deny**. The shipped config's `projectId` must be opted in by id, or
   the origin must opt into the `"*"` wildcard, which also admits unbound
   loops. The check runs on the **shipped** config *before* it is saved. It runs
   again (`_guard_start`) on the loop actually started, so preparing cannot
   smuggle a loop past the gate. The origin's own copy of a loop always wins
   and is never overwritten.
   Enforced in `agent_core.py` `ProjectAllowlist.is_allowed`,
   `OriginAgentExecutor._prepare` and `OriginAgentExecutor._guard_start`.
   Proven by [`test_remote_dispatch.py`](../mcp_loops/tests/test_remote_dispatch.py)
   `test_prepare_gates_the_shipped_project_before_saving`,
   `test_prepare_never_overwrites_the_origins_own_loop` and
   `test_prepare_rejects_a_malformed_or_misnamed_config`.

A started loop runs its agents' CLIs (claude/codex) on the origin with the
origin user's credentials, in the project's workspace. So opting a Project in
lets the Hub owner run agent turns on that computer. Opt in only Projects you
would run there yourself.

Arbitrary exec (`origin.run`) is a **separate** opt-in and is off by default: the
manifest must advertise `exec` and the program must be on the origin's own
`run-allowlist.json`. The Hub can never widen either one. Remote dispatch does
not use or need `origin.run`.

## 2. mTLS device identity

| Rule | Enforced in | Proven by |
|---|---|---|
| Each origin has an Ed25519 device key, enrolled on the Hub through a one-time pairing code. The Hub stores only the public key. An unenrolled key or a bad signature is refused at handshake. | `channel.py` `ChannelServer._handshake`; [`origin_proto/enrollment.py`](../mcp_loops/origin_proto/enrollment.py) | `test_origin_channel.py` `test_unenrolled_key_is_refused`, `test_tampered_signature_is_refused` |
| The TLS client cert is minted from the device key, and with `require_tls_binding` the cert's key must **equal** the key proven in the app-layer auth frame. So a hijacked TLS session cannot be driven as another device. This fails closed. | `channel.py` `ChannelServer._handshake` (step 3b, `tls.peer_pubkey_b64`); [`origin_proto/tls.py`](../mcp_loops/origin_proto/tls.py) `mint_device_cert` | [`test_origin_tls.py`](../mcp_loops/tests/test_origin_tls.py) `test_wrong_key_client_cert_is_refused_by_binding`, `test_forged_cert_signed_by_a_different_key_is_rejected` |
| The origin pins the Hub's cert, so a different Hub cert is refused. | `tls.py` `client_ssl_context`, `verify_pairing_hub_cert` | `test_origin_tls.py` `test_pinned_server_cert_rejects_a_different_hub_cert` |
| Engine → prod `hub_serve` bridge: a Unix socket, mode **0600**, and the peer uid must equal the Hub's own uid (`SO_PEERCRED`). It serves only a fixed, versioned op set (`PROTOCOL_VERSION` 2): v1 `ping resolve start stop status probe events snapshot`; v2 adds `hello`, the identity reads `device.get policy.view audit.read` (answered from the Hub's own EnrollmentStore + audit) and exec `run fs.pull fs.push`, which go **only** through `router.run` — the same manifest gate, binding gate, origin RunAllowlist and Hub audit as the in-process hub. A request for a newer protocol is refused (`HubProtocolError`), and it never replaces a regular file at its path. Opt-in (`--control-sock` + `LOOPYARD_HUB_CONTROL_SOCK`). | [`origin_proto/hub_control.py`](../mcp_loops/origin_proto/hub_control.py) `OPS`, `serve_control`, `_peer_uid` | [`test_hub_bridge.py`](../mcp_loops/tests/test_hub_bridge.py) `test_control_socket_ops_perms_and_gated_exec`, `test_hub_identity_v2.py` `test_v2_exec_ops_route_through_router_run`, `test_control_socket_refuses_to_replace_a_regular_file`, `test_install_hub_bridge_is_opt_in` |

## 3. No local fallback

A start, stop or status aimed at another origin **never** quietly runs on this
computer. With no origin hub, an unknown or disconnected origin, a refusal, or
a transport error, the call returns an `error` with `routed` set and writes no
local `run.json`.

| Rule | Enforced in | Proven by |
|---|---|---|
| Remote start refuses when no hub is reachable, when the origin *proved* a model's CLI is not signed in, or on any classified error. It never calls the local runner. | [`server.py`](../mcp_loops/server.py) `_is_remote_origin`, `_loop_start_remote` | `test_remote_dispatch.py` `test_remote_start_without_an_origin_hub_errors_and_runs_nothing_locally`, `test_remote_transport_failure_never_falls_back_to_local`, `test_two_engines_unknown_origin_is_refused_not_run_locally`; `test_hub_bridge.py` `test_no_hub_at_all_still_refuses_remote_start`, `test_three_procs_unknown_origin_refused_not_run_locally` |
| Remote stop goes to that origin's engine and never stops a local run of the same name. | `server.py` `loop_stop` → `_remote_loop_stop` | `test_hub_bridge.py` `test_remote_stop_routes_through_the_bridge_never_local`, `test_remote_stop_without_any_hub_refuses`, `test_three_procs_remote_stop_halts_the_run_on_far` |
| The bridge never takes an ordinary local start. | `origin_service.py` `maybe_install_hub_bridge` | `test_hub_bridge.py` `test_bridge_local_route_never_hijacks_a_plain_start` |
| The run lives on the origin: `run.json` is under the origin's `LOOPYARD_HOME`, never the Hub's. | `agent_core.py` `OriginAgentExecutor._call_sync` (`loop.start` runs on the origin's engine) | `test_two_engines_loop_started_on_hub_runs_on_the_far_origin`, `test_three_procs_engine_runs_loop_on_far_via_hub_serve` |

## 4. Before the real Mac hop — coordinator checklist

Everything above is proven only against a second engine under a `/tmp`
`LOOPYARD_HOME` on loopback (two- and three-process harnesses). No real device
has been hopped to. Before doing that (deploy steps are in the gap #3 hand-off,
`_output/clearall-remote-origin-build/deliverables/builder-gap3-remote-dispatch.md`),
verify each of these:

1. **Transport posture.** Run `hub_serve` with `--tls --require-tls-binding`.
   On the Mac, `yard origin up --dry-run` must print `tls: mtls-pinned` with a
   pinned Hub cert (`origin_client.py` `OriginClient.plan`). Never enable
   remote start over a plaintext or unbound channel. The per-session
   `tls_bound` flag (`channel.py` `_LiveOrigin`) is internal and not shown by
   any tool yet, so the flags are the check.
2. **Device identity.** The Mac's device id on the Hub matches the key the Mac
   enrolled with a pairing code you issued. There should be no stale or
   unknown devices; review recent units in `origin_audit`.
3. **Manifest.** `origin_policy(<mac-id>)` shows the manifest the Hub
   enforces. Expect `manifestStatus: ok`, `agent: true`, reads including
   `capabilities.probe`, and **`exec: false`, `fsRead/fsWrite: false`** unless
   the owner deliberately opted in. The Mac changes it only on its own disk
   (`origin-manifest.json`).
4. **Project allowlist — opt in ONE Project, never `"*"`.** The origin's
   dispatchable-Project allowlist is the file
   `<data>/_loops/_origin/project-allowlist.json` on the origin's own disk
   (mode 0600, in the 0700 state dir). With no file, or an empty one, every
   remote `loop.start` is refused with `ProjectNotAllowed`. That is the default.
   On the Mac, the owner opts in by running one of these commands:

   ```sh
   yard origin allow-project <project-id>     # or: yard origin up … --allow-project <project-id>
   yard origin projects                       # show what a Hub may start here
   yard origin deny-project <project-id>      # take it back out
   ```

   Opt in **one throwaway Project by id**. `"*"` admits every Project plus
   unbound loops. It is only accepted with the explicit `--all-projects` flag,
   which prints a loud warning. Don't use it for a real hop. A running origin
   re-reads the file on its next start request, so no restart is needed.
   If the file can't be written (for example, a read-only state dir), the
   command exits nonzero, prints `did NOT take effect` and what is still
   allowed, and changes nothing. A failed `deny-project` never reports
   success. `build_executor` loads the file, and the Hub cannot write it: the state dir
   is P4a-protected against `origin.run` and `fs.push`. The one residual is the
   same as for `run-allowlist.json`: an interpreter or shell on the origin's
   run allowlist can compute any path. Keep interpreters off it.
   Enforced in [`origin_client.py`](../mcp_loops/origin_client.py)
   `build_executor` / `_live_project_allowlist`, `agent_core.py`
   `ProjectAllowlist._persist` (0600, atomic) and
   [`yard.py`](../mcp_loops/yard.py) `_cmd_origin_projects`.
   Proven by [`test_origin_project_allowlist.py`](../mcp_loops/tests/test_origin_project_allowlist.py)
   `test_cli_allow_projects_deny_round_trip`,
   `test_cli_wildcard_needs_explicit_flag_and_warns`,
   `test_cli_deny_on_unwritable_state_fails_loudly`,
   `test_build_executor_loads_file_and_follows_owner_edits`,
   `test_hub_cannot_write_project_allowlist_over_origin_run` and
   `test_hub_cannot_fs_push_the_project_allowlist_over_the_channel`. The
   two-engine proof is `test_two_engines_owner_opted_in_project_starts_without_wildcard`:
   there is no harness wildcard, the owner opts in `alpha` with
   `yard origin allow-project alpha`, an `alpha` loop runs on the far origin,
   and a `beta` loop or an unbound loop is refused.
5. **Capabilities.** `loop_origin_capabilities(<mac-id>)` returns
   `via: "origin-channel"` with `remote: true` records whose notes show the
   Mac's own CLI paths. A CLI with `authed: false` blocks starts that need it.
6. **Bridge scope.** The control socket is mode 0600 and owned by the same uid
   as the engine. `hub_serve` logs `HUB_LISTENING … control=<path>`.
7. **No local fallback, live.** After `loop_start(<loop>, origin=<mac-id>)`,
   `run.json` exists on the Mac and **not** under the Hub's data dir. Also
   confirm that `loop_start` to a bogus origin id returns `routed` with an error
   and starts nothing.
8. **Rollback ready.** Removing `--control-sock` and
   `LOOPYARD_HUB_CONTROL_SOCK`, then restarting, restores the previous
   behaviour exactly.

Note on the embedded local origin: `LocalOriginService`
(`origin_proto/dispatch.py`) gives its own executor the `"*"` wildcard by
default. That executor only dials the in-process hub on the same computer
(same trust domain), so this is not a remote-authority grant. Don't reuse
that default for an origin that dials a different Hub.
