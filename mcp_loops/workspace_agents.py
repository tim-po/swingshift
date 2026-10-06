"""Hub-as-Workspace — the two ADDITIVE-ONLY origin agents (SWEEP + RECONCILE).

HUB-WORKSPACE-SPEC (owner, 2026-09-24, LOCKED). Both agents run as ``origin.run``
jobs on the Hub Fabric: the job execs one small, **read-only** command on the
TARGET origin (this module's own ``sweep`` / ``reconcile`` CLI — a git-only scan
that prints JSON), ships the JSON back to the hub, and the hub folds it into
SUGGESTIONS via :meth:`Workspace.add_suggestion` — **never** ``write_doc``.

That split is the safety spine. The agent (collection) half is read-only and runs
on the origin; the hub (emit) half only ever *appends to the suggestion log*. No
agent code path can reach a human doc under ``docs/`` — proven in
``test_workspace_agents.py`` (both statically, the emit functions never call a
writer, and dynamically, a full Sweep/Reconcile leaves every human doc byte-for-
byte unchanged).

* **SWEEP** mines an origin's tracked files for *candidate objectives*, **dedupes**
  against existing Hub objectives (and against already-pending inbox candidates),
  and emits new-objective drafts (into the Sweep inbox) + enrichment notes (onto
  the matched existing objective). Never edits an existing file.
* **RECONCILE** reads an objective's docs + the origin's **git history**, maps each
  doc to commits by keyword/path overlap, and proposes a **done/todo split with
  commit links**. Never edits an existing file.

Open questions resolved (assumptions noted):

* **Sweep candidate signals** — a file whose name/path marks *intent*: it carries
  one of :data:`CANDIDATE_MARKERS` (spec/design/rfc/proposal/plan/roadmap/todo/adr)
  in its name, OR it is a ``*.md`` under a ``docs/`` tree. Plus an ``# H1`` title
  when present, else a humanised filename. The lowest-false-positive floor; richer
  signals (rationale comment blocks, README gaps) can layer on later.
* **Reconcile claim→commit mapping** — deterministic **token overlap** between a
  doc (its title tokens + slug words) and each commit (subject tokens + touched
  path tokens). A doc with >=1 matching commit ⇒ proposed ``done`` with those
  commit links; else ⇒ ``todo``. An LLM refinement can layer on; the overlap floor
  is what a test proves against a real ``git log``.
* **Commit links** — ``<repo_url>/commit/<hash>`` when the origin's repo url is
  known, else the short hash stands alone as the evidence handle.
* **Dedupe method** — normalized-title match (:func:`~mcp_loops.workspace.normalize_title`),
  the deterministic floor; embeddings are a later depth.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

from .workspace import Workspace, normalize_title

# ── Sweep candidate signals ─────────────────────────────────────────────────────
CANDIDATE_MARKERS = ("spec", "design", "rfc", "proposal", "plan", "roadmap",
                     "todo", "adr")
# a control char the git format below never collides with real content
_REC = "\x01"
_FLD = "\x1e"


def _title_from(path: str, text: str) -> str:
    """A candidate's title: the file's first markdown ``# H1``, else a humanised
    filename (``check-out-rewrite.md`` → ``Check Out Rewrite``)."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            t = s[2:].strip()
            if t:
                return t
        if s and not s.startswith("#"):
            break  # first non-heading content line — no leading H1
    base = os.path.basename(path).rsplit(".", 1)[0]
    words = [w for w in re.split(r"[-_ .]+", base) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or base


def _path_token_set(path: str) -> set:
    """The path's alnum tokens (``docs/CHECKOUT-SPEC.md`` → ``{docs, checkout,
    spec, md}``). Whole-token matching so ``designer/`` never trips the ``design``
    marker."""
    return {t for t in re.split(r"[^a-z0-9]+", path.lower()) if t}


def _signal_of(path: str) -> str:
    toks = _path_token_set(path)
    for m in CANDIDATE_MARKERS:
        if m in toks:
            return m
    return "docs-md"


def is_candidate_file(path: str) -> bool:
    """A file marks a candidate objective when any of its path tokens is a
    :data:`CANDIDATE_MARKERS` token (``docs/x-spec.md``, ``design/payments.md``,
    ``TODO``), or it is a ``*.md`` under a ``docs/`` tree. A stray ``*.md`` (a
    plain root README, say) is intentionally NOT a candidate — it keeps Sweep's
    false-positive floor low, and whole-token matching avoids ``designer/``."""
    if _path_token_set(path) & set(CANDIDATE_MARKERS):
        return True
    low = path.lower()
    if low.endswith(".md") and ("/docs/" in "/" + low or low.startswith("docs/")):
        return True
    return False


def scan_candidates(files: List[dict]) -> List[dict]:
    """Pure: origin files ``[{path, text}]`` → candidate objectives, de-duplicated
    *within the batch* by normalized title (two files that name the same objective
    collapse to one, recording the extra path)."""
    seen: Dict[str, dict] = {}
    out: List[dict] = []
    for f in files:
        path = f.get("path", "")
        if not path or not is_candidate_file(path):
            continue
        text = f.get("text", "") or ""
        title = _title_from(path, text)
        key = normalize_title(title)
        if not key:
            continue
        if key in seen:
            seen[key].setdefault("also", []).append(path)
            continue
        cand = {"title": title, "path": path, "body": text,
                "signal": _signal_of(path), "also": []}
        seen[key] = cand
        out.append(cand)
    return out


def _provenance(cand: dict, origin: str) -> str:
    """A short, honest provenance header prepended to a suggested draft body, so an
    accepted draft carries where it came from (the human still owns the final text
    once they Accept + Edit)."""
    where = f"`{cand['path']}`" + (f" on origin `{origin}`" if origin else "")
    also = cand.get("also") or []
    extra = ("\n> Also seen in: " + ", ".join(f"`{p}`" for p in also)) if also else ""
    return (f"> _Sweep candidate ({cand.get('signal', 'docs')}) from {where}._"
            f"{extra}\n\n")


def run_sweep(ws: Workspace, files: List[dict], *, origin: str = "",
              scope: Optional[str] = None, agent: str = "sweep") -> dict:
    """Emit Sweep suggestions from mined files. ADDITIVE-ONLY: the ONLY writer this
    touches is :meth:`Workspace.add_suggestion`. Dedupe is three-layered — within
    the batch (scan_candidates), against existing Hub objectives (a matching title
    ⇒ an *enrichment* note on that objective, not a new objective), and against
    already-pending inbox candidates (never re-emit the same new objective twice).
    Returns a deterministic summary."""
    if scope:
        files = [f for f in files if (f.get("path", "")).startswith(scope)]
    cands = scan_candidates(files)
    inbox = ws.ensure_inbox()
    # already-pending new-objective candidate titles → don't re-emit
    pending = {normalize_title(s.get("title", ""))
               for s in ws.list_suggestions(inbox, state="pending")
               if s.get("kind") == "new_objective"}
    new_objs: List[dict] = []
    enrich: List[dict] = []
    skipped: List[str] = []
    for c in cands:
        body = _provenance(c, origin) + c["body"]
        dup = ws.find_duplicate(c["title"])
        if dup and dup != inbox:
            sug = ws.add_suggestion(dup, kind="enrichment", title=c["title"],
                                    body=body, origin=origin, agent=agent)
            enrich.append({"objective": dup, "sug": sug["id"], "title": c["title"]})
        elif normalize_title(c["title"]) in pending:
            skipped.append(c["title"])
        else:
            sug = ws.add_suggestion(inbox, kind="new_objective", title=c["title"],
                                    body=body, origin=origin, agent=agent)
            pending.add(normalize_title(c["title"]))
            new_objs.append({"sug": sug["id"], "title": c["title"], "path": c["path"]})
    return {"origin": origin, "scope": scope, "scanned": len(files),
            "candidates": len(cands), "inbox": inbox,
            "new_objective": new_objs, "enrichment": enrich,
            "deduped_pending": skipped}


# ── Reconcile: claim → commit evidence mapping ──────────────────────────────────
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "to", "and", "for", "with", "doc", "docs", "md",
         "objective", "spec", "design", "todo", "in", "on", "at", "is", "it", "this",
         "that", "add", "adds", "fix", "wip", "draft", "new", "update", "notes"}


def _tokens(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOP}


def _path_tokens(paths: List[str]) -> set:
    toks: set = set()
    for p in paths or []:
        toks |= _tokens(p.replace("/", " ").replace(".", " ").replace("-", " ")
                         .replace("_", " "))
    return toks


def _commit_url(commit_hash: str, repo_url: str) -> str:
    # Only http(s) origins yield a link. An agent-mined repo_url (from an origin's
    # `git remote`) could be javascript:/data:/file: — never let it become an href
    # (defense-in-depth alongside the client-side evidenceHref scheme guard).
    if not repo_url or not repo_url.lower().startswith(("http://", "https://")):
        return ""
    base = repo_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    return f"{base}/commit/{commit_hash}"


def map_commits(doc: dict, commits: List[dict], *, repo_url: str = "") -> List[dict]:
    """The commits whose subject/paths token-overlap a doc's title + slug words.
    Returns evidence rows (short hash + subject + link + the matched tokens),
    newest-first (git-log order preserved)."""
    keys = _tokens(doc.get("title", "")) | _tokens(
        (doc.get("slug", "") or "").replace("-", " "))
    evidence: List[dict] = []
    for c in commits:
        hay = _tokens(c.get("subject", "")) | _path_tokens(c.get("files", []))
        matched = sorted(keys & hay)
        if matched:
            h = c.get("hash", "")
            evidence.append({"commit": h[:7], "full": h,
                             "subject": c.get("subject", ""),
                             "url": _commit_url(h, repo_url), "matched": matched})
    return evidence


def reconcile_objective(docs: List[dict], commits: List[dict], *,
                        repo_url: str = "") -> dict:
    """Pure: an objective's docs + the origin's commits → a done/todo split. A doc
    with >=1 matching commit is proposed ``done`` (carrying those commit links);
    a doc with none is proposed ``todo``. No status is invented for a doc already
    at the proposed value — the caller decides whether to emit (reduces noise)."""
    done: List[dict] = []
    todo: List[dict] = []
    for d in docs:
        ev = map_commits(d, commits, repo_url=repo_url)
        row = {"slug": d.get("slug"), "title": d.get("title"),
               "current": d.get("status"), "evidence": ev}
        if ev:
            row["propose"] = "done"
            done.append(row)
        else:
            row["propose"] = "todo"
            todo.append(row)
    return {"done": done, "todo": todo}


def run_reconcile(ws: Workspace, oid: str, commits: List[dict], *,
                  origin: str = "", repo_url: str = "",
                  agent: str = "reconcile") -> dict:
    """Emit Reconcile status proposals for an objective. ADDITIVE-ONLY: touches
    only :meth:`Workspace.add_suggestion`. A ``status`` suggestion is emitted only
    where the proposed status DIFFERS from the doc's current status (an already-
    correct status is not re-proposed). Reconcile's commit links ride in
    ``evidence``. Returns the split + the emitted suggestion ids."""
    obj = ws.get_objective(oid)
    if obj is None:
        raise ValueError(f"unknown objective {oid!r}")
    split = reconcile_objective(obj.get("docs", []), commits, repo_url=repo_url)
    emitted: List[dict] = []
    for row in split["done"] + split["todo"]:
        if row.get("current") == row["propose"]:
            continue
        sug = ws.add_suggestion(oid, kind="status",
                                title=f"{row['title']} → {row['propose']}",
                                target_slug=row["slug"], status=row["propose"],
                                origin=origin, agent=agent,
                                evidence=row["evidence"])
        emitted.append({"sug": sug["id"], "slug": row["slug"],
                        "propose": row["propose"], "commits": len(row["evidence"])})
    return {"objective": oid, "origin": origin,
            "done": len(split["done"]), "todo": len(split["todo"]),
            "split": split, "emitted": emitted}


# ── collection half — runs ON the origin (git-only, read-only) ──────────────────
def _git(root: str, args: List[str]) -> str:
    out = subprocess.run(["git", "-C", root, *args], capture_output=True,
                         text=True, check=False)
    return out.stdout


def collect_sweep(root: str, *, scope: Optional[str] = None) -> List[dict]:
    """Read-only scan of a git-tracked tree on the origin: the tracked files that
    :func:`is_candidate_file` accepts, with their text. Reads only candidate files
    (not the whole tree), so it stays cheap."""
    listing = _git(root, ["ls-files"] + ([scope] if scope else []))
    files: List[dict] = []
    for path in listing.splitlines():
        path = path.strip()
        if not path or not is_candidate_file(path):
            continue
        try:
            with open(os.path.join(root, path), encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        files.append({"path": path, "text": text})
    return files


def collect_commits(root: str, *, limit: int = 200,
                    paths: Optional[List[str]] = None) -> List[dict]:
    """Read-only ``git log`` on the origin → ``[{hash, subject, author, date,
    files:[...]}]``, newest-first. Uses control-char field/record separators so a
    commit subject can contain anything."""
    fmt = f"{_REC}%H{_FLD}%s{_FLD}%an{_FLD}%ad"
    args = ["log", f"-n{int(limit)}", "--no-merges", "--name-only",
            "--date=short", f"--pretty=format:{fmt}"]
    if paths:
        args += ["--", *paths]
    raw = _git(root, args)
    commits: List[dict] = []
    for block in raw.split(_REC):
        block = block.strip("\n")
        if not block:
            continue
        head, _, rest = block.partition("\n")
        parts = head.split(_FLD)
        if len(parts) < 2:
            continue
        h, subject = parts[0], parts[1]
        author = parts[2] if len(parts) > 2 else ""
        date = parts[3] if len(parts) > 3 else ""
        changed = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        commits.append({"hash": h, "subject": subject, "author": author,
                        "date": date, "files": changed})
    return commits


def _detect_repo_url(root: str) -> str:
    url = _git(root, ["config", "--get", "remote.origin.url"]).strip()
    if url.startswith("git@") and ":" in url:  # git@github.com:owner/repo.git
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    return url


# ── CLI — the exact command an ``origin.run`` job execs on the target origin ─────
def sweep_run_argv(*, python: Optional[str] = None,
                   scope: Optional[str] = None) -> List[str]:
    """The argv to hand ``origin.run`` for a Sweep collection on the origin."""
    argv = [python or sys.executable, "-m", "mcp_loops.workspace_agents", "sweep"]
    if scope:
        argv += ["--scope", scope]
    return argv


def reconcile_run_argv(*, python: Optional[str] = None,
                       limit: int = 200) -> List[str]:
    """The argv to hand ``origin.run`` for a Reconcile collection on the origin."""
    return [python or sys.executable, "-m", "mcp_loops.workspace_agents",
            "reconcile", "--limit", str(limit)]


# ── the round-trip bridge — dispatch through origin.run, fold JSON into the store ─
class OriginRunError(RuntimeError):
    """An ``origin.run`` dispatch (or its JSON payload) came back unusable. Raised
    LOUD rather than swallowed — a Sweep/Reconcile that could not actually reach the
    origin must not look like an empty-but-successful scan."""


def _decode_stream(raw: str) -> str:
    """A real ``origin.run`` envelope carries ``stdout``/``stderr`` **base64-encoded**
    (the executor's :func:`~mcp_loops.origin_proto.agent_core._b64`, so a binary or
    newline-laden payload survives the JSON tool boundary). Decode it; if the string
    is not valid base64 — JSON text starts with ``{``, outside the base64 alphabet,
    so ``validate=True`` rejects it — it is already-plain text (a simpler injected
    envelope) and is returned as-is. Robust to BOTH shapes, so the fold never
    silently mistakes an encoded scan for an empty one."""
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        return base64.b64decode(s, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return raw


def _collection_json(env: object, *, agent: str) -> dict:
    """Extract the collection payload from an ``origin.run`` result envelope
    (``{routed, origin, exitCode, stdout, stderr, ...}`` — the shape
    :func:`mcp_loops.server.origin_run` returns), or raise :class:`OriginRunError`
    with the real reason. ``stdout``/``stderr`` come base64-encoded from the real
    executor; :func:`_decode_stream` handles that (and a plain-text fallback).
    Read-only: it only parses what the origin printed."""
    if not isinstance(env, dict):
        raise OriginRunError(f"{agent}: origin.run returned {type(env).__name__}, "
                             "expected a result envelope")
    if env.get("error"):
        raise OriginRunError(f"{agent}: origin.run refused — {env['error']}")
    code = env.get("exitCode", env.get("exit_code"))
    if code not in (0, None):
        tail = _decode_stream(env.get("stderr") or "")[-300:]
        raise OriginRunError(f"{agent}: collection exited {code} on origin "
                             f"{env.get('origin', '?')!r} — {tail}")
    stdout = _decode_stream(env.get("stdout") or "")
    if not stdout.strip():
        raise OriginRunError(f"{agent}: collection produced no stdout to fold")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise OriginRunError(f"{agent}: collection stdout was not JSON — {e}") from e
    if not isinstance(payload, dict):
        raise OriginRunError(f"{agent}: collection JSON was not an object")
    return payload


def sweep_via_origin(ws: Workspace, origin: str, run_fn, *,
                     scope: Optional[str] = None, python: Optional[str] = None,
                     timeout: float = 0.0, agent: str = "sweep") -> dict:
    """The REAL Sweep round-trip: hand :func:`sweep_run_argv` to ``run_fn`` (the
    Hub's ``origin.run``), which execs the read-only collection on ``origin`` and
    returns its envelope; fold the mined files back through :func:`run_sweep` into
    SUGGESTIONS. ``run_fn`` matches ``origin_run(argv, origin=…, timeout=…)`` and
    is injected so this is provable without a live fabric. ADDITIVE-ONLY:
    everything downstream still routes through :meth:`Workspace.add_suggestion`."""
    argv = sweep_run_argv(python=python, scope=scope)
    env = run_fn(argv, origin=origin, timeout=timeout)
    payload = _collection_json(env, agent=agent)
    files = payload.get("files") or []
    out = run_sweep(ws, files, origin=origin, scope=scope, agent=agent)
    out["routed"] = bool(isinstance(env, dict) and env.get("routed"))
    out["repo_url"] = payload.get("repo_url", "")
    return out


def reconcile_via_origin(ws: Workspace, oid: str, origin: str, run_fn, *,
                         limit: int = 200, python: Optional[str] = None,
                         timeout: float = 0.0, agent: str = "reconcile") -> dict:
    """The REAL Reconcile round-trip: hand :func:`reconcile_run_argv` to ``run_fn``
    (the Hub's ``origin.run``), which execs the read-only ``git log`` collection on
    ``origin``; fold the commits back through :func:`run_reconcile` — carrying the
    origin's own detected ``repo_url`` so the emitted commit links are real. Never
    reaches a human doc; only :meth:`Workspace.add_suggestion`."""
    argv = reconcile_run_argv(python=python, limit=limit)
    env = run_fn(argv, origin=origin, timeout=timeout)
    payload = _collection_json(env, agent=agent)
    commits = payload.get("commits") or []
    repo_url = payload.get("repo_url", "")
    out = run_reconcile(ws, oid, commits, origin=origin, repo_url=repo_url,
                        agent=agent)
    out["routed"] = bool(isinstance(env, dict) and env.get("routed"))
    out["repo_url"] = repo_url
    return out


def main(argv: Optional[List[str]] = None) -> int:
    """Origin-side entrypoint. Prints a JSON payload the hub folds into
    suggestions. Read-only: it scans git and files, and writes NOTHING."""
    ap = argparse.ArgumentParser(prog="workspace_agents",
                                 description="Hub-as-Workspace origin agents (collection half)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sweep", help="mine candidate objectives from this origin")
    sp.add_argument("--root", default=".")
    sp.add_argument("--scope", default=None)
    rp = sub.add_parser("reconcile", help="collect git evidence from this origin")
    rp.add_argument("--root", default=".")
    rp.add_argument("--limit", type=int, default=200)
    rp.add_argument("--path", action="append", default=None)
    ns = ap.parse_args(argv)
    if ns.cmd == "sweep":
        payload = {"agent": "sweep", "root": os.path.abspath(ns.root),
                   "repo_url": _detect_repo_url(ns.root),
                   "files": collect_sweep(ns.root, scope=ns.scope)}
    else:
        payload = {"agent": "reconcile", "root": os.path.abspath(ns.root),
                   "repo_url": _detect_repo_url(ns.root),
                   "commits": collect_commits(ns.root, limit=ns.limit,
                                              paths=ns.path)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
