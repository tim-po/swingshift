#!/usr/bin/env bash
set -euo pipefail
# Explicit owner opt-in: the installed origin initially denies command execution.
timeout 60 docker exec "$ORIGIN" /root/loopyard/bin/yard origin down >"$ARTIFACT_DIR/s4-origin.log" 2>&1
timeout 90 docker exec "$ORIGIN" /root/loopyard/bin/yard origin up \
  --hub wss://127.0.0.1:18932 --allow-run echo >>"$ARTIFACT_DIR/s4-origin.log" 2>&1
docker exec "$HUB" touch /state/dispatch-request
for attempt in {1..45}; do
  if docker exec "$HUB" test -f /state/dispatch-result.json; then
    docker exec "$HUB" cat /state/dispatch-result.json >"$ARTIFACT_DIR/s4-result.json"
    "$PYTHON" -c '
import base64,json,sys
r=json.load(open(sys.argv[1]))
assert r.get("exitCode") == 0, r
assert base64.b64decode(r["stdout"]) == b"docker-e2e-dispatch\n", r
assert base64.b64decode(r.get("stderr", "")) == b"", r
' "$ARTIFACT_DIR/s4-result.json"
    echo 'PASS S4: hub dispatch returned exact origin stdout and successful exit'
    exit 0
  fi
  sleep 1
done
echo 'FAIL S4: no dispatch result'; exit 1
