"""poller.py — the dispatch-inbox poller entrypoint `yard up` auto-starts (A6).

Round-2 shipped cross-origin dispatch (the hub writes a request into a connected
origin's outbox) but never WIRED the origin-side poller, so those queued requests
were never drained — a connected origin looked connected yet did nothing. Round-3
A6 has `yard up` start THIS as a tracked, detached service.

The poll/queue mechanics already exist and are tested in the worker package
(``bot_squad_worker.dispatch_inbox``): this is a thin shim so they're reachable
from a loops-ONLY install, where ``worker/`` is a sibling of ``mcp_loops/`` and
not on the venv path. It runs them against the SAME data root the server uses
(via ``$LOOPS_DATA_DIR``), so a request the hub drops actually gets executed here.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from mcp_loops import paths


def _ensure_worker_on_path() -> bool:
    """Put ``<install>/worker`` on sys.path so ``bot_squad_worker`` imports from a
    loops-only install. Returns True if the worker package is now reachable."""
    worker = os.path.join(str(paths.install_root()), "worker")
    if os.path.isdir(worker) and worker not in sys.path:
        sys.path.insert(0, worker)
    return os.path.isdir(os.path.join(worker, "bot_squad_worker"))


def main(argv: Optional[list] = None) -> int:
    if not _ensure_worker_on_path():
        print("[poller] worker package not found next to this install — "
              "cannot start the dispatch poller", file=sys.stderr)
        return 2
    from bot_squad_worker import dispatch_inbox
    return dispatch_inbox.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
