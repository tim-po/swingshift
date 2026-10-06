"""Runner registry — the ATTACHED runner a fresh install dispatches loops onto.

P1 (fresh-install runnable): before this, ``loop_start``/``start_loop`` defaulted
their worker-daemon slug to the hardcoded ``"coord"``. On a brand-new box no such
project exists, so the very first dispatch died with the opaque worker-daemon
trace ``spawn_session HTTP 400: unknown project slug coord`` — the entire INBOUND
path (an app dispatching a loop) was dead out-of-the-box.

A **runner** is the binding the loop engine needs to reach a worker daemon and
spawn agent sessions: a first-class *default slug* plus how to reach the daemon
(``sock``) and what repo/python the spawned sessions run under. ``yard up``
writes it; the server reads it. With a runner attached, ``start_loop`` with NO
slug just works; with none attached, the server fails CLEARLY ("no runner
attached — run `yard up`") instead of leaking the daemon trace.

The marker is a single ``runner.json`` at the DATA ROOT (the ``LOOPS_DATA_DIR``
dir that holds the per-loop subdirs), so it travels with the install's data and
honours the same env redirection the rest of the stack does. It carries NO
secrets — the box authenticates its own CLIs (Principle P3), exactly like the
origin markers ``yard connect`` writes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from mcp_loops import paths

# The default runner slug a fresh `yard up` registers. Deliberately NOT "coord"
# (which only exists on the original swarm box) — a first-class, install-neutral
# default so `start_loop` with no slug works on any fresh box.
DEFAULT_RUNNER_SLUG = "loopyard"

MARKER = "runner.json"

# A slug becomes a worker-daemon project key + a tmux session name, so keep it to
# a safe, path/name-fragment-free charset (same shape as loop/host names).
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def data_root(data_dir: Optional[str] = None) -> str:
    """The data root that holds the per-loop dirs + this marker. Resolution
    matches ``report.status_dir`` so the marker sits beside the loops the server
    reads: explicit arg > ``LOOPS_DATA_DIR`` > ``<install>/data/_loops``."""
    return paths.resolve_data_dir(data_dir)


def marker_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_root(data_dir), MARKER)


def valid_slug(slug: str) -> Optional[str]:
    """Return an error string if ``slug`` is not usable as a runner slug, else
    None."""
    if not slug:
        return "runner needs a slug."
    if not _SLUG_RE.match(slug):
        return (f"{slug!r} isn't a valid slug (use letters, digits, and . _ - "
                "only).")
    return None


def isolation_flags(python: str, root: str) -> list[str]:
    """R31/D13: ``["-I"]`` iff ``python -I`` still imports ``mcp_loops`` from
    ``root`` (so isolated mode cannot break the report command), else ``[]``.
    Self-verifying: a dev venv relying on PYTHONPATH/.pth gets no flag."""
    import subprocess
    probe = ("import mcp_loops,os;print(os.path.realpath("
             "os.path.dirname(os.path.dirname(mcp_loops.__file__))))")
    try:
        out = subprocess.run([python, "-I", "-c", probe], capture_output=True,
                             text=True, timeout=10, cwd=os.path.expanduser("~"))
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode == 0 and out.stdout.strip() == os.path.realpath(root):
        return ["-I"]
    return []


def attach(slug: str = DEFAULT_RUNNER_SLUG, *, sock: Optional[str] = None,
           repo: Optional[str] = None, python: Optional[str] = None,
           host: Optional[str] = None, data_dir: Optional[str] = None,
           python_flags: Optional[list] = None,
           now: float) -> dict:
    """Register the attached runner: write ``runner.json`` at the data root.
    Idempotent — re-attaching refreshes the binding but keeps the original
    ``attachedAt``. Raises ``ValueError`` on a bad slug. Carries no secrets."""
    err = valid_slug(slug)
    if err:
        raise ValueError(err)
    root = data_root(data_dir)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, MARKER)
    attached_at = now
    prior = read(data_dir)
    if prior and isinstance(prior.get("attachedAt"), (int, float)):
        attached_at = prior["attachedAt"]
    marker = {
        "slug": slug,
        "sock": sock,
        "repo": repo,
        "python": python,
        "host": host,
        "attachedAt": attached_at,
        "source": "yard",
    }
    if python_flags:  # omitted when empty ⇒ existing runner.json goldens hold
        marker["pythonFlags"] = list(python_flags)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(marker, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return marker


def read(data_dir: Optional[str] = None) -> Optional[dict]:
    """The attached runner marker, or None if no runner is attached / the marker
    is unreadable. Never raises — a corrupt marker reads as 'no runner'."""
    try:
        with open(marker_path(data_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("slug"):
        return None
    return data


def is_attached(data_dir: Optional[str] = None) -> bool:
    return read(data_dir) is not None


def default_slug(data_dir: Optional[str] = None) -> Optional[str]:
    """The attached runner's slug, or None when no runner is attached."""
    m = read(data_dir)
    return m.get("slug") if m else None


def detach(data_dir: Optional[str] = None) -> bool:
    """Forget the attached runner. Returns True if a marker was removed."""
    path = marker_path(data_dir)
    try:
        os.remove(path)
        return True
    except OSError:
        return False
