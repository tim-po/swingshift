#!/usr/bin/env bash
set -euo pipefail
# Both filesystems and process namespaces are independent. Only their private
# network namespace is shared, allowing install.sh's loopback-only HTTP mode.
docker run --init -d --name "$ORIGIN" --label "$LABEL" --network "container:$HUB" \
  -e LOOPYARD_ALLOW_INSECURE=1 -e LOOPS_CLAUDE_BIN=/bin/false "$IMAGE" >/dev/null
docker exec "$ORIGIN" sh -c 'test ! -e /root/loopyard && ! command -v yard'
docker exec "$HUB" cat /state/connect.sh | \
  timeout 600 docker exec -i "$ORIGIN" sh >"$ARTIFACT_DIR/s1-install.log" 2>&1 || {
    cat "$ARTIFACT_DIR/s1-install.log"; echo 'FAIL S1: connect command'; exit 1;
  }
grep -q 'signature ok' "$ARTIFACT_DIR/s1-install.log"
docker exec "$ORIGIN" python3 -c '
import json, pathlib
root = pathlib.Path("/root/loopyard")
assert json.loads((root / "BUNDLE.json").read_text())["version"] == "1.0.0"
assert not list(root.rglob("enroll.json")), "local hub enrollment unexpectedly exists"
for proc in pathlib.Path("/proc").glob("[0-9]*/cmdline"):
    try: cmd = proc.read_bytes().split(b"\0")
    except OSError: continue
    assert b"mcp_loops.hub_serve" not in cmd, "local hub started"
'
docker exec "$ORIGIN" /root/loopyard/bin/yard origin status >"$ARTIFACT_DIR/s1-origin-status.log"
for attempt in {1..60}; do
  docker exec "$HUB" cat /state/status.json >"$ARTIFACT_DIR/s1-hub-status.json"
  if "$PYTHON" -c '
import json,sys
s=json.load(open(sys.argv[1]))
assert len(s["devices"]) == 1, s
assert len(s["origins"]) == 1, s
assert s["origins"][0]["health"] == "online", s
' "$ARTIFACT_DIR/s1-hub-status.json" 2>/dev/null; then
    echo 'PASS S1: signed portal install, origin-only join, hub sees online origin'
    exit 0
  fi
  sleep 1
done
echo 'FAIL S1: origin did not become online'; exit 1
