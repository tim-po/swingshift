"""Loopyard Unified Sync Engine (UNIFIED-SYNC-ENGINE-SPEC.md v3).

Phased per spec §9: S-1 ships only ``ids`` (deterministic ids, D5/D14/D21).
Nothing in this package is imported by a read path while
``LOOPS_SYNC_MODE=off`` except what the S-1 hygiene wiring calls explicitly.
"""
