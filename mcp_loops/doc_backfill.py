"""CLI for the one-shot Hub doc-result backfill (loopyard-clear-all-bug-1790578879).

    python -m mcp_loops.doc_backfill            # dry-run: print the plan, write nothing
    python -m mcp_loops.doc_backfill --apply    # write the ribbons + re-index stale rows
    python -m mcp_loops.doc_backfill --exclude DOC_ID [--exclude DOC_ID ...]
    python -m mcp_loops.doc_backfill --only DOC_ID [--only DOC_ID ...]

``--exclude`` holds back an objective its loop only partly delivered (listed in the
plan as ``skipped: excluded``); ``--only`` touches just the named docs. An id that
matches no doc is reported and blocks ``--apply`` (exit 1, nothing written).

Reads ``LOOPS_DATA_DIR`` like the server. Idempotent: a second ``--apply`` finds
nothing to write. Same body as the ``loop_doc_backfill_results`` MCP tool.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mcp_loops.doc_backfill",
                                 description="Backfill Hub doc results from finished loops.")
    ap.add_argument("--apply", action="store_true",
                    help="write the results (default: dry-run, print the plan only)")
    ap.add_argument("--exclude", action="append", default=[], metavar="DOC_ID",
                    help="never write/re-index this doc (repeatable)")
    ap.add_argument("--only", action="append", default=[], metavar="DOC_ID",
                    help="touch only this doc (repeatable)")
    args = ap.parse_args(argv)
    from mcp_loops import server
    out = server.doc_results_backfill(apply=args.apply, exclude=args.exclude,
                                      only=args.only)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if out.get("unmatched"):
        print(f"no doc with id: {', '.join(out['unmatched'])}", file=sys.stderr)
    if not out.get("ok"):
        print(out.get("error") or "backfill failed", file=sys.stderr)
        return 1
    mode = "applied" if args.apply else "dry-run (pass --apply to write)"
    print(f"{len(out['write'])} result(s), {len(out['reindex'])} re-index, "
          f"{len(out['skipped'])} skipped — {mode}", file=sys.stderr)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
