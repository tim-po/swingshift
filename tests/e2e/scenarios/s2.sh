#!/usr/bin/env bash
set -euo pipefail
# Clear fault injection even on assertion failure so later scenarios remain valid.
cleanup_fault() {
  docker exec "$HUB" rm -f /state/installer-fault >/dev/null 2>&1 || true
  if [[ -n "${name:-}" ]]; then docker rm -fv "$name" >/dev/null 2>&1 || true; fi
}
trap cleanup_fault EXIT
# Compare enrollment identities so S2 also works after S1 in a selected run.
docker exec "$HUB" cat /state/status.json >"$ARTIFACT_DIR/s2-before.json"
for fault in 401 404 truncated; do
  name="$ORIGIN-fault-$fault"
  docker exec "$HUB" sh -c 'printf "%s" "$1" > /state/installer-fault' sh "$fault"
  docker run --init -d --name "$name" --label "$LABEL" --network "container:$HUB" \
    -e LOOPYARD_ALLOW_INSECURE=1 -e LOOPS_CLAUDE_BIN=/bin/false "$IMAGE" >/dev/null
  if docker exec "$HUB" cat /state/connect.sh | \
      timeout 60 docker exec -i "$name" sh >"$ARTIFACT_DIR/s2-$fault.log" 2>&1; then
    echo "FAIL S2 $fault: connect unexpectedly succeeded"; exit 1
  fi
  if ! grep -qi 'Re-copy the command from the portal' "$ARTIFACT_DIR/s2-$fault.log"; then
    cat "$ARTIFACT_DIR/s2-$fault.log"
    echo "FAIL S2 $fault: missing actionable download failure"; exit 1
  fi
  docker exec "$name" sh -c 'test ! -e /root/loopyard && test -z "$(find /tmp -name "loopyard-install.*" -print)"'
  docker exec "$HUB" cat /state/status.json >"$ARTIFACT_DIR/s2-$fault-status.json"
  "$PYTHON" -c '
import json, sys
before, after = [json.load(open(p)) for p in sys.argv[1:]]
assert before["devices"] == after["devices"], "failed installer enrolled a device"
assert {o["deviceId"] for o in before["origins"]} == {o["deviceId"] for o in after["origins"]}, "failed installer joined"
' "$ARTIFACT_DIR/s2-before.json" "$ARTIFACT_DIR/s2-$fault-status.json"
  docker rm -fv "$name" >/dev/null
  echo "PASS S2 $fault: aborts loud, no install, no staging, no enrollment"
done
docker exec "$HUB" rm /state/installer-fault
