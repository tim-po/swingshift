"""Loop workspaces — engine-managed, ephemeral git worktrees derived from a
loop's PROJECT (Loop-Workspaces redesign, Phase 1).

The problem this replaces
-------------------------
Historically every loop minted a bespoke ``[projects.X]`` slug whose
``repo_path`` was a *permanent* git-worktree directory. Two smells followed:
(a) the project list bloated with one entry per loop, and (b) a graveyard of
near-identical checkout directories piled up (each also needing its own
``npm install``). The root cause was conflating **"where a loop builds"** with
**"a project."**

The model here
--------------
A loop's build location is an **ephemeral git worktree** off its PROJECT repo's
base branch (``master``), on a dedicated branch ``loop/<name>``, at a managed
location ``<data>/loop-workspaces/<name>``. The ENGINE provisions it on
``loop_start`` and threads its path into every session spawn; on
finish/archive/stop it **reaps** the working directory while **keeping the
branch** (so commits survive). No ``[projects.X]`` entry is ever minted.

This module is the pure primitive — provisioning, reaping, and the trigger
policy — with **no** knowledge of the runner/substrate. It shells out to real
``git worktree`` so a provision→reap cycle is deterministically testable against
a scratch repo (see ``tests/test_workspaces.py``). Callers (the substrate/runner)
own *when* to provision/reap and *whether* the loop is still live.

Design decisions (Phase-1 defaults; revise only with reason)
- **Workspace root:** ``<data>/loop-workspaces`` — one dir per loop ``name``.
- **Branch:** ``loop/<name>`` off the project repo's base branch.
- **Base branch:** ``master`` when present, else the repo's default/HEAD branch.
- **Persist-branch-on-reap:** YES — ``git worktree remove`` drops only the dir.
- **Reap triggers:** finish / archive / stop. **keep-on-error** (a failed loop
  keeps its workspace for debugging). **Never** reap while dirty (uncommitted
  work is refused, not discarded) unless the caller passes ``force=True``.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import time
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

# ── naming ──────────────────────────────────────────────────────────────────
WORKSPACES_DIRNAME = "loop-workspaces"
BRANCH_PREFIX = "loop/"
# Engine-managed identity token dropped in each provisioned worktree. It carries a
# per-PROVISION nonce so a worktree RE-CREATED at a reused path (same loop re-run,
# or — absent the _safe_name uniqueness guard — a different loop) is recognisably a
# DIFFERENT workspace from the one an old session recorded. The worker's resume
# resolver reads this same file by name (see sessions.py _LOOP_WORKSPACE_ID_FILE)
# to refuse resurrecting a session into a workspace that is no longer its own.
# It is engine metadata, NOT user work: is_dirty()/ignored-content deliberately
# ignore it so its presence never blocks a reap.
WORKSPACE_ID_FILE = ".loop-workspace-id"
# Agent-runtime scaffolding the harness writes INTO the worktree when a session
# runs there — Claude Code drops a ``.claude/`` dir (settings, hooks, transcripts).
# Like the identity token it is ENGINE/agent metadata, NOT user work: it must
# never make a workspace look dirty or block a reap, else a genuinely-untouched
# worktree never reaps on finish (Gap 2, loopyard-bug-1790346148). It is excluded
# from BOTH the dirty check and the ignored-content check — but ONLY when it is
# untracked/ignored; a repo that TRACKS ``.claude/`` keeps real edits to it as
# user work (see :func:`_is_excluded_metadata`).
AGENT_SCAFFOLD_DIR = ".claude"
# The specific entries the engine/agent runtime writes under ``.claude/`` — ONLY
# these count as disposable scaffolding (loopyard-follow-up-1790359391). Anything
# else a human drops under ``.claude/`` is user work: it keeps the workspace.
#   role / task_id          — worker spawn markers (sessions.py)
#   last_user_prompt_ts     — prompt-timestamp hook marker
#   scheduled_tasks.*       — Claude Code scheduler state
#   settings*               — settings.json / settings.local.json / swarm .bak
#   *.jsonl                 — session transcripts / state logs
_SCAFFOLD_FILES = frozenset({"role", "task_id", "last_user_prompt_ts",
                             "scheduled_tasks.json", "scheduled_tasks.lock"})
# Base branches tried in order when the caller doesn't pin one. ``master`` first
# (the Loopyard convention and the spec's stated base), then ``main`` for repos
# on the newer default, then whatever the repo's HEAD currently points at.
_BASE_CANDIDATES = ("master", "main")

# Loop names are already slugs in practice; this guards against a name that would
# escape the workspaces root or make an invalid branch/dir component.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceError(RuntimeError):
    """Provisioning failed in a way the caller must handle (e.g. the project
    repo is not a valid git checkout, or the base branch is missing)."""


@dataclass(frozen=True)
class Workspace:
    """A provisioned (or resolvable) loop workspace.

    ``path`` is the worktree working directory the loop's agents run in;
    ``branch`` is the persistent branch its commits land on; ``project_repo`` is
    the canonical project checkout the worktree was carved from; ``base`` is the
    branch it was carved off. ``synced`` records how a RE-ATTACHED branch was
    brought up to ``base`` (see :func:`_sync_to_base`); ``None`` when not re-attached.
    """

    loop_name: str
    path: str
    branch: str
    project_repo: str
    base: str
    synced: Optional[str] = None

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.path)


@dataclass(frozen=True)
class ReapResult:
    """Outcome of a reap attempt. ``reaped`` is True only when the working dir
    was actually removed; ``kept`` explains why it was left in place (dirty /
    error / keep-on-error / not-provisioned)."""

    reaped: bool
    path: str
    branch: str
    kept_reason: Optional[str] = None


# ── git plumbing ────────────────────────────────────────────────────────────
def _git(repo: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a git command in ``repo`` and return the completed process. ``check``
    raises :class:`WorkspaceError` on non-zero (with stderr) for the callers that
    treat a failure as fatal; otherwise the caller inspects ``returncode``."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{(proc.stderr or proc.stdout).strip()}")
    return proc


def is_git_checkout(repo: str) -> bool:
    """True iff ``repo`` is inside a git work tree (a valid checkout to worktree
    from). Fail-soft: a missing dir / absent git → False, never raises."""
    if not repo or not os.path.isdir(repo):
        return False
    try:
        proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    except Exception:  # noqa: BLE001 — no git binary etc.
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _branch_exists(repo: str, branch: str) -> bool:
    return _git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0


def resolve_base_branch(repo: str, base: Optional[str] = None) -> str:
    """The branch a new workspace is carved off. An explicit ``base`` wins (and
    must exist); otherwise try ``master`` then ``main``, else fall back to the
    repo's current HEAD branch. Raises :class:`WorkspaceError` if nothing usable
    is found."""
    if base:
        if not _branch_exists(repo, base):
            raise WorkspaceError(f"base branch {base!r} not found in {repo}")
        return base
    for cand in _BASE_CANDIDATES:
        if _branch_exists(repo, cand):
            return cand
    head = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    raise WorkspaceError(
        f"no base branch found in {repo} (tried {', '.join(_BASE_CANDIDATES)}, HEAD)")


# ── project → repo resolution ───────────────────────────────────────────────
def resolve_project_repo(config_dir: str, project_id: Optional[str]) -> Optional[str]:
    """The canonical project checkout to worktree from, read from
    ``<config_dir>/projects.toml`` by ``project_id`` — callers pass
    ``paths.config_dir()``, the one resolver shared with the worker daemon's
    ``spawn_session`` (R14). Returns an absolute path only when
    it is a valid git checkout, else ``None`` — the caller decides whether that is
    fatal (provision) or a graceful skip (legacy loop with no project)."""
    if not project_id:
        return None
    try:
        import tomllib
        p = Path(config_dir) / "projects.toml"
        if not p.exists():
            return None
        data = tomllib.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — bad/absent toml → no project repo
        return None
    entry = (data.get("projects") or {}).get(project_id) or {}
    rp = entry.get("repo_path")
    if rp and is_git_checkout(rp):
        return os.path.abspath(rp)
    return None


# ── layout helpers ──────────────────────────────────────────────────────────
def _safe_name(loop_name: str) -> str:
    raw = (loop_name or "").strip()
    name = _SAFE_NAME.sub("-", raw).strip("-.")
    if not name:
        raise WorkspaceError(f"loop name {loop_name!r} is not a usable workspace name")
    # Uniqueness guard: sanitisation is LOSSY — distinct loop names can collapse to
    # the same slug (``a/b`` and ``a-b`` both → ``a-b``; ``a b`` too). Two loops
    # sharing one dir+branch is a safety bug: one loop's terminal reap can remove
    # the other's (possibly live) checkout. When sanitisation changed the name, we
    # can no longer trust the slug to be injective, so we disambiguate with a short,
    # deterministic hash of the ORIGINAL name. A name that was already a clean slug
    # is returned unchanged (the common case — "loop names are already slugs"), so
    # this is byte-for-byte for every real loop and only diverges on the collision-
    # prone inputs it exists to separate.
    if name != raw:
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        name = f"{name}-{h}"
    return name


def workspaces_root(data_dir: str) -> str:
    """The dir that holds every loop's workspace — a sibling concept to the data
    root, ``<data>/loop-workspaces``."""
    return os.path.join(os.path.abspath(data_dir), WORKSPACES_DIRNAME)


def workspace_path(data_dir: str, loop_name: str) -> str:
    return os.path.join(workspaces_root(data_dir), _safe_name(loop_name))


def branch_for(loop_name: str) -> str:
    return BRANCH_PREFIX + _safe_name(loop_name)


# ── workspace identity (resume-safety token) ─────────────────────────────────
def read_identity(path: str) -> Optional[str]:
    """The identity token recorded in the worktree at ``path`` (see
    :data:`WORKSPACE_ID_FILE`), or ``None`` if absent/unreadable. Used to tell a
    still-mine workspace from one recreated at the same path for a different run."""
    try:
        tok = Path(path, WORKSPACE_ID_FILE).read_text("utf-8").strip()
    except OSError:
        return None
    return tok or None


def _ensure_token_excluded(project_repo: str) -> None:
    """Make git IGNORE the identity token, repo-wide, via the shared
    ``$GIT_COMMON_DIR/info/exclude`` (the local, never-committed exclude list).

    Why here and not a ``.gitignore``: a tracked ``.gitignore`` would be committed
    onto the loop branch, and a per-worktree ``info/exclude`` isn't honoured — git
    resolves excludes from the COMMON dir. Excluding it means (a) an agent's
    ``git add -A`` never accidentally commits the token, and (b) ``git worktree
    remove`` (non-force) drops it with the tree instead of refusing. Safe on the
    canonical checkout: the token filename never appears there, so the pattern is
    inert outside loop worktrees. Idempotent — appended once."""
    try:
        common = _git(project_repo, "rev-parse", "--git-common-dir")
        if common.returncode != 0:
            return
        gitdir = common.stdout.strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(project_repo, gitdir)
        info = os.path.join(gitdir, "info")
        os.makedirs(info, exist_ok=True)
        exclude = os.path.join(info, "exclude")
        pattern = WORKSPACE_ID_FILE
        existing = ""
        if os.path.exists(exclude):
            with open(exclude, "r", encoding="utf-8") as fh:
                existing = fh.read()
        if pattern not in existing.split():
            with open(exclude, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(pattern + "\n")
    except OSError:
        pass  # best-effort; reap still deletes the token before a non-force remove


def _write_identity(ws: "Workspace", *, only_if_missing: bool = False) -> Optional[str]:
    """Stamp a fresh per-provision identity token into the worktree and return it.
    ``only_if_missing`` preserves an existing token (an idempotent re-provision of a
    still-present worktree keeps the SAME identity, so the session that recorded it
    still matches). Best-effort: a write failure is non-fatal — identity is a
    resume-safety aid, never a provisioning gate."""
    target = Path(ws.path, WORKSPACE_ID_FILE)
    if only_if_missing:
        existing = read_identity(ws.path)
        if existing:
            return existing
    token = f"{ws.branch}@{secrets.token_hex(8)}"
    try:
        target.write_text(token + "\n", "utf-8")
    except OSError:
        return None
    return token


def plan(data_dir: str, loop_name: str, project_repo: str,
         base: Optional[str] = None) -> Workspace:
    """The Workspace a given (data_dir, loop, project) *would* provision — pure,
    touches nothing on disk beyond reading the base branch. Handy for the caller
    to know the path/branch before (or without) provisioning."""
    return Workspace(
        loop_name=loop_name,
        path=workspace_path(data_dir, loop_name),
        branch=branch_for(loop_name),
        project_repo=os.path.abspath(project_repo),
        base=resolve_base_branch(project_repo, base),
    )


def reap_target(data_dir: str, loop_name: str, project_repo: str) -> Workspace:
    """The Workspace to REAP (or capture) for an existing loop — like :func:`plan`
    but WITHOUT resolving a base branch. Removal only needs path + branch +
    project repo; the base matters solely when carving a NEW worktree. Resolving
    it here would let an unresolvable base (master/main/HEAD all gone) abort the
    archive-reap and leak the directory (issue
    loopworkspaces-remediate-follow-up-1790341346). ``base`` is left empty —
    never pass this to :func:`provision`."""
    return Workspace(
        loop_name=loop_name,
        path=workspace_path(data_dir, loop_name),
        branch=branch_for(loop_name),
        project_repo=os.path.abspath(project_repo),
        base="",
    )


# ── provision / reap ────────────────────────────────────────────────────────
def provision(data_dir: str, loop_name: str, project_repo: str,
              base: Optional[str] = None) -> Workspace:
    """Provision (or re-attach) the ephemeral worktree for ``loop_name`` off
    ``project_repo``'s base branch, at ``<data>/loop-workspaces/<name>`` on branch
    ``loop/<name>``. Idempotent:

    - if the worktree dir already exists and is a git worktree → return it as-is
      (a restart / double loop_start reuses the same workspace, never collides);
    - if the branch already exists (a prior run whose dir was reaped) → re-attach
      a fresh worktree to it, so its commits carry forward;
    - otherwise carve a new branch off the base.

    Raises :class:`WorkspaceError` if the project repo is not a git checkout or
    the base branch can't be resolved — provisioning is a hard requirement, not a
    best-effort.
    """
    if not is_git_checkout(project_repo):
        raise WorkspaceError(
            f"project repo {project_repo!r} is not a git checkout — cannot provision")
    ws = plan(data_dir, loop_name, project_repo, base)
    # Ignore the identity token repo-wide before it is written (so `git add -A`
    # never commits it and a non-force reap can remove it with the tree).
    _ensure_token_excluded(project_repo)

    if os.path.isdir(ws.path):
        # Already provisioned (restart / concurrent re-entry). Trust it if it's a
        # real worktree; otherwise a stale non-git dir is in the way — that is a
        # caller/operator problem, surfaced rather than silently clobbered.
        if is_git_checkout(ws.path):
            # Same worktree still on disk (restart / re-entry) → keep its identity
            # so an already-spawned session still recognises it as its own.
            _write_identity(ws, only_if_missing=True)
            return ws
        raise WorkspaceError(
            f"workspace path {ws.path!r} exists but is not a git worktree")

    os.makedirs(workspaces_root(data_dir), exist_ok=True)
    if _branch_exists(project_repo, ws.branch):
        # Re-attach to the surviving branch (commits from a reaped run persist),
        # then bring it up to the current base so a re-run doesn't build on a
        # stale tip (loopworkspaces-remediate-follow-up-1790341341).
        _git(project_repo, "worktree", "add", ws.path, ws.branch, check=True)
        ws = replace(ws, synced=_sync_to_base(ws))
    else:
        _git(project_repo, "worktree", "add", "-b", ws.branch, ws.path, ws.base,
             check=True)
    # Freshly (re)created worktree → a NEW identity: a session that recorded the
    # PRIOR provision's token at this same path will no longer match, and its resume
    # fails closed instead of corrupting this run's tree.
    _write_identity(ws)
    return ws


def _sync_to_base(ws: Workspace) -> str:
    """Bring a freshly RE-ATTACHED ``ws.branch`` worktree up to ``ws.base``
    without ever rewriting its history. Returns the outcome:

    - ``"up-to-date"``      — base is already contained in the branch;
    - ``"fast-forwarded"``  — the branch had no unique commits → ff to base;
    - ``"merged"``          — the branch has real commits → base MERGED in (never
      rebased; nothing dropped);
    - ``"conflict-kept"``   — the merge conflicted → aborted, branch left exactly
      at its old tip, and a warning logged (the loop still runs, on the stale tip);
    - ``"failed: …"``       — any other git failure; branch left untouched.
    """
    path, base = ws.path, ws.base

    def _ancestor(a: str, b: str) -> bool:
        return _git(path, "merge-base", "--is-ancestor", a, b).returncode == 0

    tip = _git(path, "rev-parse", "HEAD").stdout.strip()
    if _ancestor(base, "HEAD"):
        return "up-to-date"
    if _ancestor("HEAD", base):
        r = _git(path, "merge", "--ff-only", "-q", base)
        if r.returncode == 0:
            return "fast-forwarded"
        return f"failed: {(r.stderr or r.stdout).strip()[:200]}"
    env = None
    if _git(path, "config", "user.email").returncode != 0:
        env = {"GIT_AUTHOR_NAME": "loopyard", "GIT_AUTHOR_EMAIL": "loopyard@loopyard.invalid",
               "GIT_COMMITTER_NAME": "loopyard",
               "GIT_COMMITTER_EMAIL": "loopyard@loopyard.invalid"}
    r = _git_env(path, "merge", "--no-edit", "--no-ff", "-q",
                 "-m", f"Merge {base} into {ws.branch} (loop re-run sync)", base, env=env)
    if r.returncode == 0:
        return "merged"
    _git(path, "merge", "--abort")
    if _git(path, "rev-parse", "HEAD").stdout.strip() != tip:
        _git(path, "reset", "-q", "--hard", tip)
    conflicted = "CONFLICT" in (r.stdout + r.stderr)
    print(f"[workspaces] WARNING: loop {ws.loop_name!r}: could not merge {base} into "
          f"{ws.branch} ({'conflict' if conflicted else 'merge failed'}); branch left "
          f"untouched at {tip[:12]} — the re-run builds on the stale tip. Resolve by "
          f"merging {base} into {ws.branch} manually.", file=sys.stderr)
    return "conflict-kept" if conflicted else f"failed: {(r.stderr or r.stdout).strip()[:200]}"


def _is_scaffold_file(rel: str) -> bool:
    """True iff ``rel`` (a path relative to ``.claude/``) is one of the entries
    the engine/agent runtime writes (see :data:`_SCAFFOLD_FILES`). Top-level
    files only — a nested dir under ``.claude/`` is never assumed ours."""
    if "/" in rel:
        return False
    return (rel in _SCAFFOLD_FILES or rel.startswith("settings")
            or rel.endswith(".jsonl"))


def _scaffold_only(root: str, entry: str) -> bool:
    """True iff the on-disk ``.claude`` porcelain ``entry`` under worktree
    ``root`` holds nothing but runtime scaffolding. Porcelain collapses a wholly
    untracked/ignored dir to one ``.claude/`` line, so a dir entry is walked and
    EVERY file in it must be scaffolding; empty dirs are fine."""
    full = os.path.join(root, entry)
    base = os.path.join(root, AGENT_SCAFFOLD_DIR)
    if not os.path.isdir(full):
        return _is_scaffold_file(os.path.relpath(full, base))
    for dirpath, _dirs, files in os.walk(full):
        for f in files:
            if not _is_scaffold_file(os.path.relpath(os.path.join(dirpath, f), base)):
                return False
    return True


def _is_excluded_metadata(code: str, entry: str, root: str) -> bool:
    """True iff a porcelain ``(code, entry)`` pair (in worktree ``root``) is
    engine/agent metadata the reap guards deliberately ignore — never user work
    or secrets.

    Excluded:
    - the engine's own identity token (:data:`WORKSPACE_ID_FILE`) — always ours;
    - agent-runtime scaffolding under ``.claude/`` **only when untracked (``??``)
      or ignored (``!!``) AND made up solely of the runtime-written entries**
      (:func:`_is_scaffold_file`). A repo that TRACKS ``.claude/`` keeps real
      edits to it (``M``/``A``/``D`` codes) as user work, and an unexpected file a
      human put under ``.claude/`` keeps the workspace too.
    """
    entry = entry.rstrip("/")
    if entry == WORKSPACE_ID_FILE:
        return True
    if entry == AGENT_SCAFFOLD_DIR or entry.startswith(AGENT_SCAFFOLD_DIR + "/"):
        return code in ("??", "!!") and _scaffold_only(root, entry)
    return False


def _porcelain_paths(path: str, *extra: str,
                     raw: bool = False) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain`` (plus any ``extra`` flags) into
    ``(status, path)`` pairs. By default drops engine/agent metadata
    (:func:`_is_excluded_metadata`) — it is not work and must never make a
    workspace look dirty or block a reap. Pass ``raw=True`` to keep every entry
    (used by the reap purge to find the metadata it must delete from disk).
    Porcelain v1: ``XY<space>path`` (path at column 3; ``->`` for renames)."""
    out: list[tuple[str, str]] = []
    proc = _git(path, "status", "--porcelain", *extra)
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        code, entry = line[:2], line[3:]
        # rename/copy lines read "old -> new"; the live path is the destination.
        entry = entry.split(" -> ", 1)[-1].strip().strip('"')
        if not raw and _is_excluded_metadata(code, entry, path):
            continue
        out.append((code, entry))
    return out


def is_dirty(path: str) -> bool:
    """True iff the worktree at ``path`` has uncommitted changes or untracked
    files — the signal that reaping would discard work. A non-worktree/absent path
    is not 'dirty' (nothing to lose). Engine/agent metadata is excluded: the
    identity token and untracked/ignored ``.claude/`` scaffolding
    (:func:`_is_excluded_metadata`)."""
    if not is_git_checkout(path):
        return False
    return bool(_porcelain_paths(path))


def has_ignored_content(path: str) -> bool:
    """True iff the worktree at ``path`` holds git-IGNORED files (``.env``/secrets,
    local data, build output). ``git status --porcelain`` OMITS these, so
    :func:`is_dirty` alone can't see them — yet ``git worktree remove`` would
    delete them with the dir. Detecting them lets :func:`reap` KEEP the workspace
    rather than silently destroy un-recreatable local state. A non-worktree/absent
    path holds nothing to lose. Engine/agent metadata is excluded — the identity
    token and untracked/ignored ``.claude/`` scaffolding — so agent runtime files
    (which git may ignore) never masquerade as user secrets and block a reap."""
    if not is_git_checkout(path):
        return False
    # ``!!`` marks ignored entries in --ignored porcelain output; _porcelain_paths
    # has already dropped excluded engine/agent metadata.
    return any(code == "!!" for code, _ in _porcelain_paths(path, "--ignored"))


def _purge_reapable_metadata(path: str) -> None:
    """Delete the engine/agent metadata a non-force reap must clear from disk so
    ``git worktree remove`` (no ``--force``) doesn't refuse over lingering
    untracked files. That is exactly the set :func:`_is_excluded_metadata`
    hides from the dirty/ignored guards: the identity token and untracked/ignored
    ``.claude/`` scaffolding. Tracked or modified paths are never touched — the
    guards already blocked the reap before we got here, so nothing user-owned is
    among these entries. Best-effort: failures are swallowed (the subsequent
    remove reports any real problem)."""
    # Identity token: always ours and git-ignored; drop unconditionally in case
    # its exclude write ever failed and it lingers as untracked.
    try:
        os.remove(os.path.join(path, WORKSPACE_ID_FILE))
    except OSError:
        pass
    # Untracked/ignored agent scaffolding under `.claude/` — only the runtime-
    # written entries; the guards already kept the workspace if anything else
    # (a user file) was there, so this never deletes user work.
    for code, entry in _porcelain_paths(path, "--ignored", raw=True):
        if code not in ("??", "!!"):
            continue
        stripped = entry.rstrip("/")
        if not (stripped == AGENT_SCAFFOLD_DIR
                or stripped.startswith(AGENT_SCAFFOLD_DIR + "/")):
            continue
        if not _scaffold_only(path, stripped):
            continue
        target = os.path.join(path, stripped)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            try:
                os.remove(target)
            except OSError:
                pass


def reap(ws: Workspace, *, force: bool = False) -> ReapResult:
    """Remove the workspace working dir, KEEPING the branch (commits survive).

    Guards (Phase-1 safety):
    - **not provisioned** → no-op success (nothing to remove).
    - **dirty** (uncommitted/untracked) and not ``force`` → REFUSE, keep the dir
      so uncommitted work is never silently discarded.
    - **ignored content** (git-ignored ``.env``/secrets/local data) and not
      ``force`` → REFUSE, keep the dir: ``git worktree remove`` would delete these
      with the tree, but ``git status --porcelain`` never surfaces them, so the
      dirty guard alone can't protect them.
    - **removal error** → keep-on-error: the dir is left for debugging and the
      failure is reported (``reaped=False``), never raised.

    The branch is never deleted here — that's the whole point of persist-on-reap.
    Deciding *whether* to reap (finish/archive/stop vs. keep-on-error for a failed
    loop, and never-while-live) is the caller's job; see :func:`should_reap`.
    """
    path, branch = ws.path, ws.branch
    if not os.path.isdir(path):
        return ReapResult(False, path, branch, kept_reason="not-provisioned")
    if not force and is_dirty(path):
        return ReapResult(False, path, branch, kept_reason="dirty")
    if not force and has_ignored_content(path):
        # Ignored files (.env/secrets/local data) would be destroyed with the dir
        # and are invisible to the dirty guard — keep the workspace instead.
        return ReapResult(False, path, branch, kept_reason="ignored-content")

    # Drop the engine/agent metadata the guards above deliberately ignore BEFORE
    # the remove. A non-force `git worktree remove` REFUSES while untracked files
    # linger, so the token (if its git-exclude write ever failed) and untracked/
    # ignored `.claude/` scaffolding would otherwise wedge the reap even though the
    # tree is "clean" by our guards. We only reach here when is_dirty() is False
    # (or force), so nothing user-owned is at these paths — deleting them is safe.
    if not force:
        _purge_reapable_metadata(path)
    # Prune stale worktree admin first, then remove. `git worktree remove` keeps
    # the branch by design; --force is needed to drop an (already-checked) dirty
    # tree when the caller explicitly asked for it.
    remove_args = ["worktree", "remove", path]
    if force:
        remove_args.append("--force")
    proc = _git(ws.project_repo, *remove_args)
    if proc.returncode != 0:
        # keep-on-error: leave the dir in place, report the failure, don't raise.
        return ReapResult(
            False, path, branch,
            kept_reason=f"remove-failed: {(proc.stderr or proc.stdout).strip()[:200]}")
    # Tidy the admin dir so a later re-provision to the same path is clean.
    _git(ws.project_repo, "worktree", "prune")
    return ReapResult(True, path, branch)


# ── Step 5: teardown capture (isolation) ────────────────────────────────────
# After the loop cage is torn down (every agent process dead), whatever the
# agents left UNCOMMITTED in the worktree is snapshotted into a side ref
#   refs/loop-captures/<branch>/<UTC stamp>
# as a commit whose parent is the branch head. The branch, the real index and
# the working tree are never touched, so a later resume/reap sees exactly what
# the agents left, and the branch's own commits are fsync'd to disk. The plan's
# order was "collect, then destroy the scope" because a podman overlay dies with
# its scope; a native-cage worktree survives the slice stop, so we stop FIRST and
# collect a quiescent tree (no agent can race the snapshot).
CAPTURE_REF_PREFIX = "refs/loop-captures/"
_FSYNC_CFG = ("-c", "core.fsync=committed,reference", "-c", "core.fsyncMethod=fsync")
# Locks a hard-killed `git commit`/`git checkout` leaves behind. Removed only when
# the caller proves the cage is empty (``quiescent``) — then no live git owns them.
_STALE_LOCKS = ("index.lock", "HEAD.lock", "ORIG_HEAD.lock", "MERGE_HEAD.lock")
_CAPTURE_IDENT = {"GIT_AUTHOR_NAME": "loopyard-teardown",
                  "GIT_AUTHOR_EMAIL": "teardown@loopyard.invalid",
                  "GIT_COMMITTER_NAME": "loopyard-teardown",
                  "GIT_COMMITTER_EMAIL": "teardown@loopyard.invalid"}


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of :func:`capture`. ``head`` is the branch tip at teardown (the
    committed work); ``captured`` is True only when uncommitted work was saved to
    ``ref`` (commit ``commit``, ``files`` paths differing from ``head``)."""

    path: str
    branch: str
    head: Optional[str]
    captured: bool
    ref: Optional[str] = None
    commit: Optional[str] = None
    files: int = 0
    locks_cleared: tuple = ()
    error: Optional[str] = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["locks_cleared"] = list(self.locks_cleared)
        return d


def _git_env(path: str, *args: str, env: Optional[dict] = None,
             inp: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, input=inp, timeout=120)


def _syncfs(path: str) -> None:
    """Flush the filesystem holding ``path`` (the loop's git objects + refs).
    syncfs(2) scopes it to one fs; fall back to a global sync."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syncfs(fd) != 0:
            os.sync()
    except (OSError, AttributeError):
        os.sync()
    finally:
        os.close(fd)


def _clear_stale_locks(path: str) -> tuple:
    """Remove the per-worktree git locks a killed agent left. Caller guarantees
    no process of the loop is alive."""
    gd = _git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
    if not gd:
        return ()
    cleared = []
    for name in _STALE_LOCKS:
        f = os.path.join(gd, name)
        if os.path.isfile(f):
            try:
                os.remove(f)
                cleared.append(name)
            except OSError:
                pass
    return tuple(cleared)


def capture(ws: Workspace, *, quiescent: bool, stamp: Optional[str] = None) -> CaptureResult:
    """Step 5 artifact capture for a torn-down loop. Never raises; never mutates
    the branch, the index or the working tree.

    - ``quiescent`` (the cage slice stopped with 0 processes left) also clears
      stale git locks so the tree is resumable; otherwise locks are left alone.
    - The branch head is fsync'd (syncfs) — committed work is durable.
    - Uncommitted tracked/untracked changes (git-IGNORED files excluded, so
      ``.env`` secrets never enter git objects; engine metadata excluded) are
      committed onto a private index and stored at
      ``refs/loop-captures/<branch>/<stamp>``."""
    path, branch = ws.path, ws.branch
    if not is_git_checkout(path):
        return CaptureResult(path, branch, None, False, error="not-provisioned")
    try:
        locks = _clear_stale_locks(path) if quiescent else ()
        rh = _git(path, "rev-parse", "--verify", "-q", "HEAD")
        head = rh.stdout.strip() if rh.returncode == 0 else None
        common = _git(path, "rev-parse", "--path-format=absolute",
                      "--git-common-dir").stdout.strip() or path
        if not _porcelain_paths(path):
            _syncfs(common)
            return CaptureResult(path, branch, head, False, locks_cleared=locks)
        gd = _git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
        idx = os.path.join(gd, f"loop-capture-{secrets.token_hex(4)}.index")
        env = {"GIT_INDEX_FILE": idx, **_CAPTURE_IDENT}
        try:
            steps = [(["read-tree", head] if head else ["read-tree", "--empty"]),
                     ["add", "-A", "--", "."],
                     ["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                      WORKSPACE_ID_FILE]]
            if not head or not _git(path, "ls-tree", "-d", head,
                                    AGENT_SCAFFOLD_DIR).stdout.strip():
                steps.append(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                              AGENT_SCAFFOLD_DIR])     # untracked scaffolding ≠ work
            for a in steps:
                r = _git_env(path, *_FSYNC_CFG, *a, env=env)
                if r.returncode != 0:
                    raise WorkspaceError(f"git {a[0]}: {(r.stderr or r.stdout).strip()[:200]}")
            tree = _git_env(path, "write-tree", env=env).stdout.strip()
        finally:
            try:
                os.remove(idx)
            except OSError:
                pass
        if not tree:
            raise WorkspaceError("write-tree produced no tree")
        if head and tree == _git(path, "rev-parse", f"{head}^{{tree}}").stdout.strip():
            _syncfs(common)
            return CaptureResult(path, branch, head, False, locks_cleared=locks)
        stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        msg = (f"loop-capture: uncommitted work in {branch} at teardown {stamp}\n\n"
               f"Snapshot taken after the loop cage stopped; branch head "
               f"{head or '(unborn)'} is unchanged.\n")
        ct = _git_env(path, *_FSYNC_CFG, "commit-tree", tree,
                      *(["-p", head] if head else []), env=_CAPTURE_IDENT, inp=msg)
        commit = ct.stdout.strip()
        if ct.returncode != 0 or not commit:
            raise WorkspaceError(f"commit-tree: {ct.stderr.strip()[:200]}")
        ref = f"{CAPTURE_REF_PREFIX}{branch}/{stamp}"
        # "" old-value ⇒ create-only: never overwrite an earlier capture
        ur = _git(path, *_FSYNC_CFG, "update-ref", "-m", "loop teardown capture",
                  ref, commit, "")
        if ur.returncode != 0:
            ref = f"{ref}-{secrets.token_hex(2)}"
            ur = _git(path, *_FSYNC_CFG, "update-ref", ref, commit, "")
            if ur.returncode != 0:
                raise WorkspaceError(f"update-ref: {ur.stderr.strip()[:200]}")
        diff = _git(path, "diff-tree", "-r", "--name-only", "--no-commit-id",
                    *([head, commit] if head else ["--root", commit]))
        _syncfs(common)
        return CaptureResult(path, branch, head, True, ref=ref, commit=commit,
                             files=len([ln for ln in diff.stdout.splitlines() if ln]),
                             locks_cleared=locks)
    except Exception as e:  # noqa: BLE001 — capture is best-effort, never blocks stop
        return CaptureResult(path, branch, None, False, error=str(e)[:300])


# ── trigger policy ──────────────────────────────────────────────────────────
# Loop end-states that REAP the workspace (dir removed, branch kept) vs. states
# that KEEP it. Anything error-shaped keeps the workspace for debugging
# (keep-on-error); a still-live loop is never reaped.
_REAP_ENDSTATES = frozenset({"finished", "finish", "complete", "completed",
                             "archived", "archive", "stopped", "stop", "done"})
_KEEP_ENDSTATES = frozenset({"error", "errored", "failed", "failure", "crash",
                             "crashed", "timeout", "abandoned", "live", "running"})


def should_reap(end_state: str) -> bool:
    """Whether a loop that ended in ``end_state`` should have its workspace
    reaped. Reap on finish/archive/stop; KEEP on any error/failure/timeout
    (keep-on-error) and never for a still-live loop. Unknown states default to
    KEEP — the conservative choice, since an un-reaped dir is recoverable but a
    wrongly-reaped one is not."""
    s = (end_state or "").strip().lower()
    if s in _REAP_ENDSTATES:
        return True
    return False
