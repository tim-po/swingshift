#!/usr/bin/env bash
set -euo pipefail
docker exec "$ORIGIN" python3 -c '
import json, pathlib, stat
r=pathlib.Path("/root/loopyard")
assert json.loads((r/"BUNDLE.json").read_text())["version"] == "1.0.0"
p=r/"config/release-source.json"
assert json.loads(p.read_text()) == {"portalUrl":"http://127.0.0.1:18931"}
assert stat.S_IMODE(p.stat().st_mode) == 0o644
for d in ("config", "data", "workspace"):
    (r/d).mkdir(exist_ok=True)
    (r/d/"e2e-preserved").write_text("preserved:"+d)
'
docker exec "$HUB" cat /state/status.json >"$ARTIFACT_DIR/s3-before.json"
"$PYTHON" -m scripts.release.publish_local --store "$WORK/releases" --version 1.0.1 \
  --target linux-x86_64 --signing-key "$WORK/signing-key" --web-dist "$WEB_DIST" \
  --portal-url http://127.0.0.1:18931 >"$ARTIFACT_DIR/s3-publish.log" 2>&1
docker exec "$HUB" cat /state/update-token | \
  timeout 600 docker exec -i "$ORIGIN" /root/loopyard/bin/yard update --token - \
  >"$ARTIFACT_DIR/s3-update.log" 2>&1 || {
    cat "$ARTIFACT_DIR/s3-update.log"; echo 'FAIL S3: yard update'; exit 1;
  }
grep -q 'signature ok' "$ARTIFACT_DIR/s3-update.log"
docker exec "$ORIGIN" python3 -c '
import json, pathlib
r=pathlib.Path("/root/loopyard")
assert json.loads((r/"BUNDLE.json").read_text())["version"] == "1.0.1"
for d in ("config", "data", "workspace"):
    assert (r/d/"e2e-preserved").read_text() == "preserved:"+d
assert list(r.parent.glob("loopyard*")) == [r], "unexpected sibling root"
'
docker exec "$HUB" cat /state/update-token | \
  timeout 60 docker exec -i "$ORIGIN" /root/loopyard/bin/yard update --token - \
  >"$ARTIFACT_DIR/s3-current.log" 2>&1
grep -q 'already current' "$ARTIFACT_DIR/s3-current.log"
for attempt in {1..60}; do
  docker exec "$HUB" cat /state/status.json >"$ARTIFACT_DIR/s3-after.json"
  if "$PYTHON" -c '
import json, sys
before, after = [json.load(open(p)) for p in sys.argv[1:]]
assert before["devices"] == after["devices"], "device identity changed"
assert len(after["origins"]) == 1 and after["origins"][0]["health"] == "online"
' "$ARTIFACT_DIR/s3-before.json" "$ARTIFACT_DIR/s3-after.json" 2>/dev/null; then
    echo 'PASS S3: signed update in place, state and identity preserved, repeat is current'
    exit 0
  fi
  sleep 1
done
echo 'FAIL S3: original device did not reconnect'; exit 1
