"""Multi-turn / session-level risk detector.

Mirrors detectors/cost.py's retry-loop check exactly, just grouped by
`session_id` (an optional, caller-supplied field on POST /v1/chat) instead
of `message_hash`: if a session has accumulated enough non-"pass"
decision-engine verdicts within a recent window, the CURRENT message gets
escalated to at least "flagged" — even if its own signals in isolation
would have passed — because risk that only emerges from a conversation's
arc (e.g. an injection attempt built up gradually over several turns, none
individually block-worthy) is exactly the gap a single-request-only view
can't see.

Pure SQLite aggregation over the existing `events` table — no external
calls, no network I/O, negligible latency, safe to run inline on every
request (same fast-path guarantee every other detector here makes).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

SESSION_RISK_WINDOW_MINUTES = 10
SESSION_RISK_THRESHOLD = 3  # this occurrence + N prior non-pass verdicts in the session


@dataclass
class SessionRiskFlag:
    flagged: bool
    reason: str
    flagged_count_in_window: int

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(
    cursor: sqlite3.Cursor,
    session_id: str | None,
    window_minutes: int = SESSION_RISK_WINDOW_MINUTES,
    threshold: int = SESSION_RISK_THRESHOLD,
) -> SessionRiskFlag:
    """Count prior non-"pass" decision-engine verdicts for this session
    within the window, and flag if that count already meets the threshold.

    Must be called BEFORE the current event row is inserted, so the count
    reflects only prior turns — mirrors detectors.cost.evaluate's own
    "never includes the row being inserted" contract. `session_id` is
    optional on the request; a falsy value (no session supplied) always
    returns unflagged, since there's nothing to group by.
    """
    if not session_id:
        return SessionRiskFlag(flagged=False, reason="no session_id supplied", flagged_count_in_window=0)

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    row = cursor.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE session_id = ? AND timestamp >= ?
          AND controlplane_action IS NOT NULL AND controlplane_action != 'pass'
        """,
        (session_id, cutoff),
    ).fetchone()
    count = row[0] if row else 0

    if count >= threshold:
        return SessionRiskFlag(
            flagged=True,
            reason=(
                f"{count} prior non-pass verdict(s) for session_id={session_id!r} in the last "
                f"{window_minutes} minutes — this message escalated regardless of its own content"
            ),
            flagged_count_in_window=count,
        )

    return SessionRiskFlag(flagged=False, reason="within session risk threshold", flagged_count_in_window=count)
