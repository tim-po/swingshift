"""Mac-origin bootstrap + control channel.

A poll-based control agent for standing a personal machine up as a Loopyard
execution origin. The Mac is behind NAT, so IT dials out: `curl .../mac-origin/
<token> | bash` installs a tiny agent that polls this host over HTTPS for shell
commands, runs them, and posts the output back. The coordinator drives the
install by writing commands to a per-token queue (see mac_origin_ctl.py) and
reading results — a scoped, revocable "window" into the machine (no inbound
ports on the Mac; the owner can `pkill` the agent any time).

Security model: the token in the URL is the bearer secret (strong random, one
per setup, stored in data/_mac_origin/token). Endpoints are gate-exempt but
token-checked. Only the owner's agent (which they started) executes commands;
the queue merely serves whatever the coordinator enqueued. Tear down when done.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path

from starlette.responses import JSONResponse, Response

# R17: a sibling of the resolved loops data dir; BOT_SQUAD_HOME (deprecated)
# still pins ``<home>/data/_mac_origin`` when LOOPS_DATA_DIR is unset.
from mcp_loops import paths as _paths
_HOME = _paths.legacy_home_override("mac_origin")
ROOT = (_HOME / "data" / "_mac_origin"
        if _HOME is not None and not os.environ.get("LOOPS_DATA_DIR")
        else Path(_paths.resolve_data_dir()).parent / "_mac_origin")
# Public base URL the Mac reaches this host at (e.g. a CF tunnel → dashgate).
# No default: this is deployment-specific, so the operator MUST set it. An empty
# value makes the bootstrap script refuse loudly rather than dial a wrong host.
PUBLIC_URL = os.environ.get("MAC_ORIGIN_URL", "")


def _active_token() -> str:
    try:
        return (ROOT / "token").read_text().strip()
    except (FileNotFoundError, OSError):
        return ""


def _ok(token: str) -> bool:
    active = _active_token()
    # constant-time: the token is the bearer secret (loopyard-bug-1790562133)
    return bool(active) and hmac.compare_digest(
        (token or "").encode("utf-8", "surrogateescape"),
        active.encode("utf-8", "surrogateescape"))


def _tdir(token: str) -> Path:
    d = ROOT / token
    d.mkdir(parents=True, exist_ok=True)
    return d


_AGENT_PY = r'''import json, os, time, ssl, subprocess, urllib.request
VPS=os.environ["LOOPYARD_VPS"]; TOKEN=os.environ["LOOPYARD_TOKEN"]
BASE=f"{VPS}/mac-origin/{TOKEN}"
# Robust TLS: conda/venv pythons on macOS often can't verify certs via the system
# store. Prefer certifi's bundle; fall back to an unverified context (this is a
# token-authed channel to our OWN known host, so MITM risk is minimal for setup).
try:
    import certifi; CTX=ssl.create_default_context(cafile=certifi.where()); print("tls: certifi", flush=True)
except Exception:
    CTX=ssl.create_default_context()
# Cloudflare blocks default python-urllib UA (403); present a curl/browser-like UA.
UA="curl/8.4.0"
def _open(u):
    try:
        return urllib.request.urlopen(u, timeout=30, context=CTX)
    except ssl.SSLError as e:
        print("tls verify failed, retrying unverified:", repr(e), flush=True)
        return urllib.request.urlopen(u, timeout=30, context=ssl._create_unverified_context())
def get(p):
    req=urllib.request.Request(f"{BASE}{p}", headers={"User-Agent":UA, "Accept":"application/json"})
    with _open(req) as r: return json.load(r)
def post(p, obj):
    req=urllib.request.Request(f"{BASE}{p}", data=json.dumps(obj).encode(),
                               headers={"User-Agent":UA, "Content-Type":"application/json"})
    with _open(req) as r: return r.read()
print("loopyard control agent up; polling", BASE, flush=True)
_f=0
while True:
    try:
        c=get("/cmd"); _f=0
    except Exception as e:
        _f+=1
        if _f<=3 or _f%20==0: print("poll error:", repr(e), flush=True)
        time.sleep(3); continue
    if not c or not c.get("cmd"):
        time.sleep(2); continue
    cid=c["id"]; cmd=c["cmd"]
    if cmd=="__STOP__":
        try: post("/result", {"id":cid,"rc":0,"stdout":"agent stopped","stderr":""})
        except Exception: pass
        break
    try:
        p=subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         timeout=c.get("timeout",900), cwd=os.path.expanduser("~"))
        post("/result", {"id":cid,"rc":p.returncode,
                         "stdout":p.stdout[-60000:],"stderr":p.stderr[-20000:]})
    except Exception as e:
        post("/result", {"id":cid,"rc":-1,"stdout":"","stderr":str(e)})
'''


def _bootstrap_sh(token: str) -> str:
    if not PUBLIC_URL:
        # Fail loud instead of curling a placeholder/wrong host. The operator
        # sets MAC_ORIGIN_URL to the base URL their Mac can reach this host at.
        return (
            "#!/bin/bash\n"
            "echo 'Loopyard origin bootstrap is not configured on this host.' >&2\n"
            "echo 'Set MAC_ORIGIN_URL to this host'\\''s public base URL "
            "(the URL your Mac can reach it at) and restart the dashboard.' >&2\n"
            "exit 1\n"
        )
    return f'''#!/bin/bash
set -euo pipefail
VPS="{PUBLIC_URL}"; TOKEN="{token}"
echo "== Loopyard origin bootstrap =="
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found — install it first:  xcode-select --install   (or: brew install python)"; exit 1
fi
mkdir -p "$HOME/loopyard"
curl -fsSL "$VPS/mac-origin/$TOKEN/agent.py" -o "$HOME/loopyard/agent.py"
pkill -f "loopyard/agent.py" 2>/dev/null || true
LOOPYARD_VPS="$VPS" LOOPYARD_TOKEN="$TOKEN" nohup python3 "$HOME/loopyard/agent.py" > "$HOME/loopyard/agent.log" 2>&1 &
echo "control channel UP (pid $!). log: ~/loopyard/agent.log"
echo "The coordinator can now run setup commands on this Mac (scoped to shell it sends)."
echo "STOP anytime:  pkill -f loopyard/agent.py"
'''


# ── HTTP handlers (gate-exempt; token-checked) ──
async def serve_bootstrap(request):
    token = request.path_params.get("token", "")
    if not _ok(token):
        return Response("not found", status_code=404, media_type="text/plain")
    return Response(_bootstrap_sh(token), media_type="text/x-shellscript")


async def serve_agent(request):
    token = request.path_params.get("token", "")
    if not _ok(token):
        return Response("not found", status_code=404, media_type="text/plain")
    return Response(_AGENT_PY, media_type="text/x-python")


async def serve_repo(request):
    """Serve a pre-built harness tarball (data/_mac_origin/repo.tgz) so a fresh
    origin can pull the code over the same channel (no GitHub remote needed)."""
    token = request.path_params.get("token", "")
    if not _ok(token):
        return Response("not found", status_code=404, media_type="text/plain")
    p = ROOT / "repo.tgz"
    try:
        return Response(p.read_bytes(), media_type="application/gzip")
    except (FileNotFoundError, OSError):
        return Response("no repo tarball staged", status_code=404, media_type="text/plain")


async def serve_brand(request):
    """Serve the brand-assets tarball (data/_mac_origin/brand.tgz) so an origin
    can apply the REAL Loopyard identity, not guess from the goal's words."""
    token = request.path_params.get("token", "")
    if not _ok(token):
        return Response("not found", status_code=404, media_type="text/plain")
    p = ROOT / "brand.tgz"
    try:
        return Response(p.read_bytes(), media_type="application/gzip")
    except (FileNotFoundError, OSError):
        return Response("no brand tarball staged", status_code=404, media_type="text/plain")


async def next_cmd(request):
    """The Mac agent polls this; returns the next queued command + advances the
    cursor, or {} when the queue is drained."""
    token = request.path_params.get("token", "")
    if not _ok(token):
        return JSONResponse({}, status_code=404)
    d = _tdir(token)
    cmds = []
    try:
        with open(d / "cmds.jsonl", encoding="utf-8") as fh:
            cmds = [json.loads(ln) for ln in fh if ln.strip()]
    except (FileNotFoundError, OSError):
        return JSONResponse({})
    cur = 0
    try:
        cur = int((d / "cursor").read_text().strip() or "0")
    except (FileNotFoundError, ValueError, OSError):
        cur = 0
    if cur >= len(cmds):
        return JSONResponse({})
    (d / "cursor").write_text(str(cur + 1))
    return JSONResponse(cmds[cur])


async def post_result(request):
    token = request.path_params.get("token", "")
    if not _ok(token):
        return JSONResponse({"error": "bad token"}, status_code=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body["ts"] = time.time()
    with open(_tdir(token) / "results.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(body) + "\n")
    return JSONResponse({"ok": True})
