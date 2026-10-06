"""Review surface — a read-only snapshot of the whole core system.

Lets a human (or the coordinator) review the live entity graph at a glance:
totals by type + trust, every tag as a project/goal collection, the pending
confirm-queue, the dependency graph, and (when the librarian has run) per-tag
meta-artifacts. Pure reads over ``core.store`` — safe to run anytime.

CLI:  python -m bot_squad_worker.core.inspect <core.db>
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Structured snapshot of the whole system (JSON-friendly)."""
    totals_type = {t: len(store.by_type(conn, t)) for t in store.TYPES}
    totals_trust = {tr: len(store.by_trust(conn, tr)) for tr in store.TRUST}

    pending = [{"id": e.id, "type": e.type, "label": e.label or ""}
               for e in store.by_trust(conn, "proposed")]

    tags: dict[str, Any] = {}
    # ctx:* = context-cloud tags (CONTEXT_CLOUD.md §4): high-cardinality, owned
    # by the coordinators' cloud — excluded from every enumerating surface so
    # they cannot flood the review; board keying is exact-name and unaffected.
    for row in conn.execute(
            "SELECT name FROM tag WHERE name NOT LIKE 'ctx:%' ORDER BY name"):
        name = row["name"]
        members = store.entities_by_tag(conn, name)
        by_type: dict[str, int] = {}
        for e in members:
            by_type[e.type] = by_type.get(e.type, 0) + 1
        meta = store.tag_meta_artifact(conn, name)
        tags[name] = {"total": len(members), "by_type": by_type,
                      "meta_artifact": (meta.id if meta else None)}

    # dependency graph: every canonical `blocks` edge as blocker -> blocked
    deps = []
    for r in conn.execute("SELECT from_id, to_id FROM edge WHERE type='blocks'"):
        b, t = store.get(conn, r["from_id"]), store.get(conn, r["to_id"])
        if b and t:
            deps.append({"blocker": b.label or b.id, "blocks": t.label or t.id})

    return {"totals": {"by_type": totals_type, "by_trust": totals_trust},
            "pending_confirm": pending, "tags": tags, "dependencies": deps}


def render(conn: sqlite3.Connection) -> str:
    """Human-readable report of the snapshot."""
    s = snapshot(conn)
    out: list[str] = ["=== PRODUCT-CORE SYSTEM SNAPSHOT ==="]
    bt = s["totals"]["by_type"]
    out.append("entities: " + ", ".join(f"{k}={v}" for k, v in bt.items()))
    out.append("trust:    " + ", ".join(
        f"{k}={v}" for k, v in s["totals"]["by_trust"].items()))

    out.append(f"\n-- pending confirm ({len(s['pending_confirm'])}) --")
    for p in s["pending_confirm"][:20]:
        out.append(f"  [{p['type']}] {p['label'][:70]}")
    if len(s["pending_confirm"]) > 20:
        out.append(f"  … +{len(s['pending_confirm']) - 20} more")

    out.append(f"\n-- tags / project+goal collections ({len(s['tags'])}) --")
    for name, info in s["tags"].items():
        bits = ", ".join(f"{k}:{v}" for k, v in info["by_type"].items())
        meta = " [meta✓]" if info["meta_artifact"] else ""
        out.append(f"  #{name} → {info['total']} ({bits}){meta}")

    out.append(f"\n-- dependency graph ({len(s['dependencies'])} blocks edges) --")
    for d in s["dependencies"][:20]:
        out.append(f"  {d['blocker'][:40]}  ⟶ blocks ⟶  {d['blocks'][:40]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Snapshot the product-core system.")
    ap.add_argument("db", help="path to the core SQLite db")
    args = ap.parse_args(argv)
    conn = store.connect(args.db)
    try:
        print(render(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
