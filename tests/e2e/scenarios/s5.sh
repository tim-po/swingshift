#!/usr/bin/env bash
set -euo pipefail
SECOND_HUB="$HUB-standalone"
SECOND_ORIGIN="$ORIGIN-standalone"
# Start the production hub executable, independently of the portal fixture.
docker run --init -d --name "$SECOND_HUB" --label "$LABEL" \
  -v "$WORK/bundle:/opt/loopyard:ro" "$IMAGE" \
  /opt/loopyard/runtime/bin/python3 -I -m mcp_loops.hub_serve \
  --tls --host 127.0.0.1 --port 18933 --state-dir /state/hub \
  --control-sock /state/control.sock >/dev/null
trap 'docker logs "$SECOND_HUB" >"$ARTIFACT_DIR/s5-hub.log" 2>&1 || true' EXIT
ready=0
for attempt in {1..30}; do
  if docker exec "$SECOND_HUB" test -S /state/control.sock; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo 'FAIL S5: standalone hub readiness'; exit 1; }
docker run --init -d --name "$SECOND_ORIGIN" --label "$LABEL" \
  --network "container:$SECOND_HUB" -e LOOPS_CLAUDE_BIN=/bin/false "$IMAGE" >/dev/null
# Offline signed install uses the same published artifact; no build in containers.
for file in install.sh loopyard-origin-linux-x86_64.tar.gz{,.sha256,.sig}; do
  docker cp "$WORK/releases/v1.0.0/$file" "$SECOND_ORIGIN:/tmp/$file"
done
timeout 600 docker exec "$SECOND_ORIGIN" sh /tmp/install.sh \
  --tarball /tmp/loopyard-origin-linux-x86_64.tar.gz --dir /root/loopyard \
  >"$ARTIFACT_DIR/s5-install.log" 2>&1
grep -q 'signature ok' "$ARTIFACT_DIR/s5-install.log"
docker exec "$SECOND_HUB" /opt/loopyard/runtime/bin/python3 -I -m mcp_loops.yard \
  hub pair-code --hub-url wss://127.0.0.1:18933 --state-dir /state/hub \
  --label standalone-origin --json | \
  "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["command"])' | \
  timeout 180 docker exec -i "$SECOND_ORIGIN" sh >"$ARTIFACT_DIR/s5-join.log" 2>&1
docker exec "$SECOND_ORIGIN" python3 -c '
from pathlib import Path
assert not list(Path("/root/loopyard").rglob("enroll.json"))
for p in Path("/proc").glob("[0-9]*/cmdline"):
    try: args=p.read_bytes().split(bytes([0]))
    except OSError: continue
    assert b"mcp_loops.hub_serve" not in args
'
for attempt in {1..45}; do
  if docker exec "$SECOND_HUB" /opt/loopyard/runtime/bin/python3 -I -c '
import json
from mcp_loops.origin_proto.hub_control import HubServeDispatch
c=HubServeDispatch("/state/control.sock")
r=c._call("ping")
print(json.dumps(r))
assert r["ok"] and len(r["live"]) == 1, r
' >"$ARTIFACT_DIR/s5-status.json" 2>"$ARTIFACT_DIR/s5-status-error.log"; then
    echo 'PASS S5: standalone TLS hub started, CLI pair command joined a signed origin'
    docker rm -fv "$SECOND_ORIGIN" >/dev/null
    exit 0
  fi
  sleep 1
done
echo 'FAIL S5: standalone hub has no live origin'; exit 1
