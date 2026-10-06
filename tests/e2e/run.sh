#!/usr/bin/env bash
set -euo pipefail
# Install logs include disposable credentials; keep artifacts private by default.
umask 077
ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"
PYTHON=${E2E_PYTHON:-python3}
WEB_DIST=${E2E_WEB_DIST:-$ROOT/frontend/apps/web/dist}
ARTIFACT_DIR=${E2E_ARTIFACT_DIR:-$(mktemp -d /tmp/loopyard-e2e-results.XXXXXX)}
mkdir -p "$ARTIFACT_DIR"
ARTIFACT_DIR=$(cd "$ARTIFACT_DIR" && pwd)
export ARTIFACT_DIR PYTHON
[[ -f "$WEB_DIST/index.html" ]] || { echo 'Build frontend/apps/web first or set E2E_WEB_DIST'; exit 2; }
docker info >/dev/null
command -v uv >/dev/null
"$PYTHON" -c 'import cryptography, httpx'
RUN_ID="ly-e2e-$(date +%s)-$$"
LABEL="loopyard.e2e.run=$RUN_ID"
IMAGE="$RUN_ID:latest"
HUB="$RUN_ID-hub"
ORIGIN="$RUN_ID-origin"
WORK=$(mktemp -d /tmp/loopyard-e2e.XXXXXX)
export LABEL IMAGE HUB ORIGIN WORK WEB_DIST
cleanup() {
  status=$?
  trap - EXIT
  docker logs "$HUB" >"$ARTIFACT_DIR/hub.log" 2>&1 || true
  docker exec "$ORIGIN" /root/loopyard/bin/yard origin status >"$ARTIFACT_DIR/final-origin-status.log" 2>&1 || true
  docker exec "$ORIGIN" sh -c 'cat /root/loopyard/data/_loops/_origin/origin.log' >"$ARTIFACT_DIR/final-origin.log" 2>&1 || true
  mapfile -t containers < <(docker ps -aq --filter "label=$LABEL")
  if ((${#containers[@]})); then docker rm -fv "${containers[@]}" >/dev/null; fi
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  docker image prune -f --filter "label=$LABEL" >/dev/null
  rm -rf "$WORK"
  # Persist evidence that this run left no containers or tagged image behind.
  docker ps -aq --filter "label=$LABEL" >"$ARTIFACT_DIR/cleanup-containers.txt"
  docker image ls -q --filter "label=$LABEL" >"$ARTIFACT_DIR/cleanup-images.txt"
  if [[ -s "$ARTIFACT_DIR/cleanup-containers.txt" || -s "$ARTIFACT_DIR/cleanup-images.txt" ]]; then
    echo 'FAIL cleanup: labeled Docker resources remain' >&2
    status=1
  fi
  echo "e2e exit=$status artifacts=$ARTIFACT_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ssh-keygen -q -t ed25519 -N '' -f "$WORK/signing-key"
"$PYTHON" -m scripts.release.publish_local --store "$WORK/releases" --version 1.0.0 \
  --target linux-x86_64 --signing-key "$WORK/signing-key" --web-dist "$WEB_DIST" \
  --portal-url http://127.0.0.1:18931 >"$ARTIFACT_DIR/publish.log" 2>&1
mkdir "$WORK/bundle"
tar -xzf "$WORK/releases/v1.0.0/loopyard-origin-linux-x86_64.tar.gz" -C "$WORK/bundle" --strip-components=1
# Legacy builder avoids retaining a separate BuildKit cache after image removal.
DOCKER_BUILDKIT=0 docker build --force-rm --build-arg "E2E_RUN_ID=$RUN_ID" --label "$LABEL" -t "$IMAGE" tests/e2e >"$ARTIFACT_DIR/image-build.log" 2>&1
docker run --init -d --name "$HUB" --label "$LABEL" \
  -v "$WORK/bundle:/opt/loopyard:ro" -v "$WORK/releases:/releases:ro" \
  -v "$ROOT/tests/e2e:/e2e:ro" "$IMAGE" \
  /opt/loopyard/runtime/bin/python3 -I /e2e/portal.py >/dev/null
ready=0
for attempt in {1..60}; do
  if docker exec "$HUB" curl -sS --max-time 2 http://127.0.0.1:18931/api/me >/dev/null 2>&1; then ready=1; break; fi
  [[ $(docker inspect -f '{{.State.Running}}' "$HUB") == true ]] || break
  sleep 1
done
if [[ "$ready" != 1 ]]; then docker logs "$HUB"; echo 'FAIL: portal readiness'; exit 1; fi
# S3 and S4 depend on the installed origin from S1. Default includes every implemented
# scenario; a selected run is diagnostic evidence, not a full-suite pass.
read -ra scenarios <<< "${E2E_SCENARIOS:-s2 s1 s3 s4 s5}"
: >"$ARTIFACT_DIR/results.log"
failed=0
s1_passed=0
for scenario in "${scenarios[@]}"; do
  case "$scenario" in s1|s2|s3|s4|s5) ;; *) echo "Unknown scenario: $scenario"; exit 2;; esac
  if [[ "$scenario" == s3 || "$scenario" == s4 ]] && [[ "$s1_passed" != 1 ]]; then
    echo "SKIP $scenario: requires successful S1 in this run" | tee -a "$ARTIFACT_DIR/results.log"
    failed=1
    continue
  fi
  if bash "tests/e2e/scenarios/$scenario.sh" 2>&1 | tee -a "$ARTIFACT_DIR/results.log"; then
    if [[ "$scenario" == s1 ]]; then s1_passed=1; fi
  else
    echo "FAIL $scenario: see scenario artifacts" | tee -a "$ARTIFACT_DIR/results.log"
    failed=1
  fi
done
exit "$failed"
