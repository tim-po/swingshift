#!/usr/bin/env bash
# loopyard-quickstart.sh — clone → a running loops box you can DRIVE and that
# RUNS AGENTS hands-free, HEADLESS.
#
# Dev-clone entry point. It builds the clone's venv and then hands over to the
# ONE bring-up implementation, `python -I -m mcp_loops.yard start` (the same verb
# a bundle's bin/yard runs; PHASE-B-SPEC §1.8). That verb brings up:
#
#   1. the MCP loops server (port-open readiness; fails loud with the log tail
#      if the process dies — "server failed to listen", mcp-server.log)
#   2. the WORKER DAEMON (BOT_SQUAD_MODE=user-worker: tmux ops only, no
#      scheduler), health-probed on its socket (/health) — or a LOUD
#      "worker daemon not started", never a bare ConnectError
#   3. a runner bound to that daemon's socket (WORKER_SOCK) so `start_loop` works
#   4. the origin dispatch poller + the dashboard SPA
#   and prints MCP_LOOPS_URL / LOOPS_DATA_DIR / status lines copy-paste-ready.
#
# Hub is OFF by default (Phase-A contract): LOOPYARD_ORIGIN_AGENT defaults to 0.
# NO prompts, NO file edits outside <install>/{config,data,workspace}, safe to
# re-run. Overridable by env:
#   LOOPYARD_PORT        server port      (default 8771)
#   LOOPYARD_DASH_PORT   dashboard port   (default 8811)
#   LOOPS_DATA_DIR       data root        (default <install>/data/_loops)
#   LOOPYARD_HOME        state root       (default <install>)
#   LOOPYARD_VENV        venv dir         (default <install>/.loopyard-venv)
#
# See docs/SETUP.md for the full onboarding path (connect a CLI → run the
# creator → point it at tasks).
set -euo pipefail

# Install root = the repo dir that contains mcp_loops/ (this script is in scripts/).
_self="$0"
while [ -L "$_self" ]; do   # portable symlink walk (old macOS readlink has no -f)
    _link="$(readlink "$_self")"
    case "$_link" in /*) _self="$_link" ;; *) _self="$(dirname "$_self")/$_link" ;; esac
done
INSTALL="$(cd "$(dirname "$_self")/.." && pwd -P)"
cd "$INSTALL"

VENV="${LOOPYARD_VENV:-$INSTALL/.loopyard-venv}"
PY="$VENV/bin/python"

log() { printf '  %s\n' "$*"; }
die() { printf 'loopyard-quickstart: %s\n' "$*" >&2; exit 1; }

# ── 1. python ────────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || die "python3 (3.12+) is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    || die "python 3.12+ required (found $(python3 -V 2>&1))"

# ── 2. venv + pinned deps (idempotent) ───────────────────────────────────────
# Prefer `uv` (fast, and works where python3 ships without ensurepip — e.g. a
# distro python-venv split); fall back to the stdlib venv + pip otherwise.
# We install BOTH the loops server/CLI deps AND the minimal worker-daemon deps
# (user-worker mode needs only fastapi/uvicorn/apscheduler/httpx — a strict
# subset of worker/pyproject.toml; the heavy telethon/autogen coordinator deps
# are never imported because no scheduler runs).
REQ="$INSTALL/mcp_loops/requirements.txt"
WORKER_REQ="$INSTALL/worker/requirements-loopyard.txt"
if command -v uv >/dev/null 2>&1; then
    [ -x "$PY" ] || { log "creating venv at $VENV (uv)"; uv venv "$VENV" >/dev/null; }
    uv pip install --python "$PY" -r "$REQ" --quiet
    if [ -f "$WORKER_REQ" ]; then uv pip install --python "$PY" -r "$WORKER_REQ" --quiet; fi
else
    if [ ! -x "$PY" ]; then
        log "creating venv at $VENV"
        python3 -m venv "$VENV" \
            || die "could not create a venv — install 'uv' or your distro's python3-venv"
    fi
    "$PY" -m pip install --upgrade pip --quiet
    "$PY" -m pip install -r "$REQ" --quiet
    if [ -f "$WORKER_REQ" ]; then "$PY" -m pip install -r "$WORKER_REQ" --quiet; fi
fi
# Put the install root AND its worker/ dir on the venv's path so `python -m
# mcp_loops.*` AND `python -m bot_squad_worker` import from ANY cwd (neither is
# pip-installed — both are resolved by path). Without this a user who cd's away
# from the install hits ModuleNotFoundError. A .pth is the standard, surgical way
# — no packaging metadata, no build backend needed.
"$PY" - "$INSTALL" <<'PY'
import os, sys, sysconfig
install = sys.argv[1]
site = sysconfig.get_path("purelib")
with open(os.path.join(site, "loopyard-install.pth"), "w", encoding="utf-8") as fh:
    fh.write(install + "\n")
    fh.write(os.path.join(install, "worker") + "\n")
PY
log "deps ready (loops + worker-daemon) + mcp_loops/bot_squad_worker on path in $VENV"

# ── 3. hand over to the one bring-up ─────────────────────────────────────────
# `-I`: the clone's cwd, a user site and PYTHONPATH can never shadow the venv
# (the loopyard-install.pth above keeps mcp_loops importable without cwd).
export LOOPYARD_ORIGIN_AGENT="${LOOPYARD_ORIGIN_AGENT:-0}"
START_ARGS=()
if [ -n "${LOOPYARD_PORT:-}" ]; then START_ARGS+=(--port "$LOOPYARD_PORT"); fi
exec "$PY" -I -m mcp_loops.yard start ${START_ARGS[@]+"${START_ARGS[@]}"}
