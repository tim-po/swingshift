"""City: which kinds of files a loop DELIVERED in git (read-only, dashboard layer).

The city dresses each building by the file kinds its loop made (frontend
``views/city``, kindOf): delivered files first, the output folder only as a fallback.
The engine does not record deliveries, so this reads git the way the seed's
``loop-file-kinds.py`` does, narrowed to the dashboard's own repo:

1. the loop's branch ``loop/<name>``: files touched between its first and last
   report (+10 min), no merges;
2. else every ``commit=<sha>`` its reports named, plus ``run.json:git_commit``.

Only extension COUNTS leave this module (never paths). GET-only, nothing is
written; the answer is cached per loop until its ``status.jsonl`` changes.

Reply: ``{"name", "known", "src": "branch"|"commit"|"none", "delivered": {ext: n}, "files": n}``.
``known`` is False when the loop is unknown, git could not be read, or nothing
of the loop resolves in this repo (no ``loop/<name>`` branch, no reported
``commit=`` sha): its work lives in another repo, so "0 files" would be a guess.
The city then shows an honest "not known yet" plain building. ``known`` True
with no files means we KNOW it delivered nothing (the only case that may wear Deco).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from mcp_loops import paths as _paths

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA_RE = re.compile(r"\bcommit=([0-9a-fA-F]{7,40})\b")
_TTL_S = 300
_cache: dict[str, tuple[tuple, float, dict]] = {}
_lock = threading.Lock()


def _repo() -> Path:
    env = os.environ.get("LOOPYARD_CITY_REPO")
    return Path(env) if env else _paths.install_root()


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _ext(p: str) -> str:
    b = os.path.basename(p).lower()
    return b.rsplit(".", 1)[1] if "." in b.lstrip(".") else ""


def _reports(d: Path) -> tuple[list[float], list[str]]:
    ts: list[float] = []
    shas: list[str] = []
    try:
        with open(d / "status.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = e.get("ts", e.get("t"))  # status.jsonl writes "ts"; the seed script read "t"
                if isinstance(t, (int, float)):
                    ts.append(float(t))
                shas += _SHA_RE.findall(str(e.get("note") or ""))
    except OSError:
        pass
    return ts, shas


def delivered(name: str) -> dict:
    """Extension counts of the files ``name`` delivered in git (see module doc)."""
    unknown = {"name": name, "known": False, "src": "none", "delivered": {}, "files": 0}
    if not _NAME_RE.match(name or ""):
        return unknown
    d = Path(_paths.resolve_data_dir()) / name
    try:
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unknown
    repo = _repo()
    if _git(["rev-parse", "--git-dir"], repo) is None:
        return unknown
    ts, shas = _reports(d)
    start = int(min(ts or [run.get("started") or 0]))
    end = int(max(ts or [start])) + 600
    files: set[str] = set()
    src = "none"
    br = f"loop/{name}"
    here = False  # did anything of this loop resolve in this repo (its branch, or a sha a report named)?
    if _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{br}"], repo) is not None:
        here = True
    if start and here:
        out = _git(["log", br, "--no-merges", "--name-only", "--format=",
                    f"--since=@{start - 60}", f"--until=@{end}"], repo) or ""
        files = {f for f in out.split("\n") if f}
        if files:
            src = "branch"
    if not files:
        named = set(shas)
        if isinstance(run.get("git_commit"), str):
            shas.append(run["git_commit"])
        for sha in dict.fromkeys(reversed(shas)):
            out = _git(["show", "--name-only", "--format=", sha], repo)
            if out is not None and sha in named:
                here = True
            if out:
                files |= {f for f in out.split("\n") if f}
        if files:
            src = "commit"
    if not files and not here:
        return unknown  # another repo's loop (or talk only, with nothing to check): not known, never Talk
    counts = Counter(_ext(f) for f in files)
    return {"name": name, "known": True, "src": src, "delivered": dict(counts), "files": len(files)}


def delivered_cached(name: str) -> dict:
    d = Path(_paths.resolve_data_dir()) / name
    try:
        sig = (os.stat(d / "status.jsonl").st_mtime_ns, os.stat(d / "run.json").st_mtime_ns)
    except OSError:
        sig = (0, 0)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == sig and now - hit[1] < _TTL_S:
            return hit[2]
    res = delivered(name)
    with _lock:
        _cache[name] = (sig, now, res)
    return res


async def loop_delivered_api(request):
    """GET /api/loops/{name}/delivered — extension counts of the files the loop
    delivered in git (the city's building style). Read-only, cached."""
    name = request.path_params.get("name", "")
    if not _NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    return JSONResponse(await run_in_threadpool(delivered_cached, name))
