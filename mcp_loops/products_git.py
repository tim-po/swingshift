"""Impure git READS for products — the ONLY module here that shells out to git.

Kept strictly OUT of the pure ``products.py`` / ``products_gather.py`` so those
stay unit-testable path arithmetic. Every function here is **read-only** (no
fetch, no write, no network — ``remote get-url`` reads the local config) and
**fails soft to ``None``** on any error: a missing dir, a non-git dir, ``git``
absent from PATH, a non-zero exit, or a timeout. The server calls these during
*gather* to self-populate a product's real ``gitRemote``/``gitBranch`` from a
resolved checkout, and to group merge-suggestions by a shared git checkout.

Never raises — a broken/absent checkout simply yields ``None`` so the caller
degrades to the portable-config defaults it already had.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

# git config reads are local + fast; a small timeout guards a wedged git/FS.
_TIMEOUT_S = 5


def _git(checkout: str, *args: str) -> Optional[str]:
    """Run ``git -C <checkout> <args>`` read-only; return stripped stdout, or
    ``None`` on any failure (missing dir, not a repo, git absent, timeout,
    non-zero exit, empty output). No shell, argv list only."""
    if not isinstance(checkout, str) or not checkout or not os.path.isdir(checkout):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", checkout, *args],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def remote_url(checkout: str, remote: str = "origin") -> Optional[str]:
    """The configured URL of ``remote`` (default ``origin``) — a LOCAL config
    read, no network. ``None`` if the dir isn't a git repo or has no such
    remote."""
    return _git(checkout, "remote", "get-url", remote)


def current_branch(checkout: str) -> Optional[str]:
    """The current branch name, or ``None`` when detached (``HEAD``) / not a
    repo — so a caller never persists the literal string ``"HEAD"`` as a
    branch."""
    branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")
    return None if branch in (None, "HEAD") else branch


def common_dir(checkout: str) -> Optional[str]:
    """Absolute, symlink-resolved git COMMON dir — the shared ``.git`` that all
    linked worktrees of one repo point at (so two checkouts of the same repo
    share this value, which is how merge-suggestions detect a shared checkout).
    git returns this RELATIVE to the checkout when it sits inside it, so we
    absolutize + realpath it. ``None`` if not a repo."""
    cd = _git(checkout, "rev-parse", "--git-common-dir")
    if cd is None:
        return None
    abs_cd = cd if os.path.isabs(cd) else os.path.join(checkout, cd)
    return os.path.realpath(abs_cd)
