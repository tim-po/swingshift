# Isolated onboarding e2e

Run from a Linux x86_64 checkout with Docker, `ssh-keygen`, `uv`, and a
Python environment containing the bundle build dependencies (`cryptography`,
`httpx`). Build the web app once in this checkout, or supply an existing built
web distribution; the real publisher requires it. No npm runs inside containers.

```sh
E2E_PYTHON=/path/to/venv/bin/python \
E2E_WEB_DIST=/path/to/frontend/apps/web/dist \
E2E_ARTIFACT_DIR=/tmp/loopyard-e2e-results \
  ./tests/e2e/run.sh
```

The entrypoint builds a real, signed Linux release once with
`scripts.release.publish_local`, using a temporary Ed25519 key. The hub uses the
published runtime and serves the real control-plane onboarding and gated release
routes. The origin image has OS prerequisites only: no Loopyard source, runtime,
release artifacts, or keys are mounted into the origin. It receives and executes
the portal's one-paste command verbatim. `LOOPYARD_ALLOW_UNSIGNED` is never set.

Model execution is outside this suite: `LOOPS_CLAUDE_BIN=/bin/false` satisfies
the executable prerequisite without installing or authenticating a model CLI.
Any accidental model invocation fails. The real `yard`, worker, origin daemon,
enrollment and channel implementations run unmodified. This does not validate
Claude installation or model-driven loops.

The origin joins the hub container's **private network namespace**. Loopback HTTP
is explicitly enabled for this local fixture; hub enrollment still uses real TLS
and certificate pinning. Containers retain independent filesystems and process
namespaces. There are no published ports, host networking, privileged containers,
Docker socket mounts, host service calls, or persistent volumes.

Currently implemented scenarios (default order: S2, S1, S3, S4, S5):

- **S1**: signed portal install, expected version, no local hub process or hub
  enrollment, exactly one enrolled origin online in the hub registry.
- **S2**: fresh containers receive HTTP 401, HTTP 404, and a successful HTTP
  response containing only the installer's shebang. Each must fail with the
  portal re-copy message, leave no install root or installer tempfile, and leave
  the hub's enrollment and origin identities unchanged. The truncated body is valid shell
  syntax deliberately: syntax checking alone does not prove completeness.

- **S3**: publish a second signed release with the same key, update through
  `yard update --token -`, check version and preserved config/data/workspace,
  require the original enrolled device to return online, and repeat the update
  to assert it is already current. Also checks the installer stored the portal
  source with mode 0644 and no credential.

- **S4**: restart the installed origin with an explicit `--allow-run echo`,
  dispatch `echo docker-e2e-dispatch` through the hub's real router and TLS
  channel, and assert exact stdout, empty stderr, and exit code zero.

**S5** starts the production standalone TLS hub daemon in another container,
installs the signed bundle offline in a clean origin, executes the command from
`yard hub pair-code`, and requires one live device through the real hub control
socket. S5 can also run independently with `E2E_SCENARIOS=s5`.

For targeted diagnostics, set `E2E_SCENARIOS='s1 s3'` (S3 requires S1 in the
same run). This deliberately skips S2 and is not evidence of a full-suite pass.
For dispatch diagnostics, use `E2E_SCENARIOS='s1 s4'`. S4 needs the installed
origin from S1, but does not depend on S3. The fixture dispatches as the portal
account that owns the enrolled origin, preserving the real ownership gate.
Two bundles are published for S3, once each, and reused by all containers.

The runner collects failures and continues through the remaining scenarios, then
returns nonzero if any failed or were skipped. S3 and S4 are skipped unless S1
passed in the same run. S2 clears its injected fault even when an assertion fails.

A scenario prints `PASS` only after its assertions pass; every error produces a
nonzero exit. Logs go to `E2E_ARTIFACT_DIR` (by default a new directory in `/tmp`).
Install logs contain real short-lived test credentials: the runner uses umask 077,
and these logs must remain private CI artifacts. The fixture's connect command stays inside the disposable hub.

Every container uses Docker's `--init` so orphaned daemon children are reaped.
This matters for upgrade shutdown and PID-based service liveness checks.

An EXIT/INT/TERM trap removes only containers carrying this run's unique label,
removes its image, prunes only layers labeled with that run ID, and removes its temporary bundle/release/key directory, and preserves logs.
The shared upstream base image and bundle downloader cache are retained for reuse.
The runner records `cleanup-containers.txt` and `cleanup-images.txt`; both must
be empty, and leftover labeled resources make the run fail. No global Docker
prune is used. As with any shell trap, SIGKILL or host shutdown
cannot be cleaned synchronously; orphan containers can be found by their
`loopyard.e2e.run` label and removed by exact run label.
