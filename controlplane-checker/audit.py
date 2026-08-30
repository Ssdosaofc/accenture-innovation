"""Tamper-evident audit trail.

`events`/`review_queue` rows are legitimately updated multiple times over an
event's lifecycle (verdict, judge score, resolution) — freezing them isn't
an option. Instead, every lifecycle milestone (event logged, verdict
decided, judge result attached, review resolved) gets one append-only entry
in `audit_log`, each cryptographically chained to the previous entry's hash.
Editing or deleting any row afterward — in `events`, `review_queue`, or
`audit_log` itself — breaks the chain at a specific, identifiable point,
detectable by recomputing it end to end (see app/db.py's
`_verify_audit_chain_sync`).

This module is pure logic — no sqlite, no I/O. The orchestration (appending
inside the same transaction as the underlying write, verifying the full
chain) lives in app/db.py, the same split used by the detectors.
"""

from __future__ import annotations

import hashlib
import json

GENESIS_HASH = "0" * 64


def compute_row_hash(
    prev_hash: str, action: str, event_id: int | None, timestamp: str, payload: dict
) -> str:
    """Deterministic SHA-256 over this entry plus the previous entry's hash.
    Canonical JSON (sorted keys, no extra whitespace) so the same logical
    entry always hashes identically regardless of dict insertion order."""
    canonical = json.dumps(
        {
            "prev_hash": prev_hash,
            "action": action,
            "event_id": event_id,
            "timestamp": timestamp,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
