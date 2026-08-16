import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import anyio

from detectors.cost import CostFlag, evaluate
from detectors.performance import REVIEW_THRESHOLD

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./controlplane.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    model TEXT NOT NULL,
    request_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    request_body TEXT NOT NULL,
    response_body TEXT,
    status TEXT NOT NULL,
    error TEXT,
    message_hash TEXT,
    cost_usd REAL,
    cost_flagged INTEGER,
    cost_flag_reason TEXT,
    cost_flag_z_score REAL,
    responsibility_flagged INTEGER,
    responsibility_action TEXT,
    responsibility_severity TEXT,
    responsibility_reason TEXT,
    judge_score REAL,
    judge_reason TEXT,
    sampled INTEGER NOT NULL DEFAULT 0,
    controlplane_action TEXT,
    controlplane_flags TEXT
);
"""

REVIEW_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    detector TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution TEXT,
    note TEXT,
    resolved_at TEXT
);
"""

# Feedback-loop stub: every "confirmed_issue" resolution lands here verbatim.
# This is the data you'd eventually use to tune detector thresholds (e.g.
# "responsibility flags a lot of false positives on PHONE" or "performance
# judge_score > 0.6 rarely gets confirmed, raise the threshold") — for the
# prototype it's just storage, no retraining/analysis logic yet.
CONFIRMED_ISSUES_SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmed_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_queue_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    detector TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_events_model_timestamp ON events(model, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_events_message_hash_timestamp ON events(message_hash, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_review_queue_event_id ON review_queue(event_id);",
    "CREATE INDEX IF NOT EXISTS idx_review_queue_resolved ON review_queue(resolved, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_confirmed_issues_detector ON confirmed_issues(detector);",
)

# (table, column, ddl) for columns added after the initial CREATE TABLE
# shipped. SQLite has no "ADD COLUMN IF NOT EXISTS", so init_db() applies
# these with the duplicate-column error swallowed — a lightweight migration
# path for any database file created before this endpoint pair existed.
_COLUMN_MIGRATIONS = (
    ("review_queue", "resolution", "TEXT"),
    ("review_queue", "note", "TEXT"),
    ("review_queue", "resolved_at", "TEXT"),
)


@contextmanager
def _connect():
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)
        conn.execute(REVIEW_QUEUE_SCHEMA)
        conn.execute(CONFIRMED_ISSUES_SCHEMA)
        for table, column, ddl in _COLUMN_MIGRATIONS:
            _ensure_column(conn, table, column, ddl)
        for index_sql in INDEXES:
            conn.execute(index_sql)
        conn.commit()


def _push_review_queue(cursor: sqlite3.Cursor, event_id: int, detector: str, severity: str, reason: str, created_at: str) -> None:
    cursor.execute(
        """
        INSERT INTO review_queue (event_id, detector, severity, reason, created_at, resolved)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (event_id, detector, severity, reason, created_at),
    )


def _log_event_sync(event: dict) -> tuple[CostFlag, int]:
    """Run the cost detector against prior history, then insert this event
    (including the resulting cost flag) in the same connection so the
    baseline used for detection never includes the row being inserted.

    The responsibility flag (PII/injection) is computed by the caller before
    this is invoked — it needs no database access — and is passed through
    on `event` for storage only. An injection-marker finding
    (`responsibility_action == "logged"`) is pushed to review_queue here,
    same as a high-confidence judge finding is pushed from
    `_update_judge_result_sync` below.

    Returns (cost_flag, event_id) — the caller needs event_id to later
    attach the slow performance-check result to this same row.
    """
    with _connect() as conn:
        cursor = conn.cursor()

        cost_flag = evaluate(
            cursor,
            model=event["model"],
            input_tokens=event.get("input_tokens") or 0,
            output_tokens=event.get("output_tokens") or 0,
            message_hash=event.get("message_hash", ""),
        )

        cursor.execute(
            """
            INSERT INTO events (
                timestamp, latency_ms, model, request_id,
                input_tokens, output_tokens, request_body, response_body,
                status, error, message_hash,
                cost_usd, cost_flagged, cost_flag_reason, cost_flag_z_score,
                responsibility_flagged, responsibility_action,
                responsibility_severity, responsibility_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["timestamp"],
                event["latency_ms"],
                event["model"],
                event.get("request_id"),
                event.get("input_tokens"),
                event.get("output_tokens"),
                json.dumps(event["request_body"]),
                json.dumps(event["response_body"]) if event.get("response_body") is not None else None,
                event["status"],
                event.get("error"),
                event.get("message_hash"),
                cost_flag.cost_usd,
                int(cost_flag.flagged),
                cost_flag.reason,
                cost_flag.z_score,
                int(event.get("responsibility_flagged", False)),
                event.get("responsibility_action", "none"),
                event.get("responsibility_severity", "none"),
                event.get("responsibility_reason", "clean"),
            ),
        )
        event_id = cursor.lastrowid
        assert event_id is not None  # always set right after a successful INSERT

        if event.get("responsibility_action") == "logged":
            _push_review_queue(
                cursor,
                event_id,
                detector="responsibility",
                severity=event.get("responsibility_severity", "high"),
                reason=event.get("responsibility_reason", ""),
                created_at=event["timestamp"],
            )

        conn.commit()

    return cost_flag, event_id


async def log_event(event: dict) -> tuple[CostFlag, int]:
    """Detect cost anomalies against the rolling baseline and log the event.
    Runs in a worker thread since sqlite3 is blocking; the detector itself is
    pure in-process SQLite aggregation, so this adds negligible latency."""
    return await anyio.to_thread.run_sync(_log_event_sync, event)


def _update_judge_result_sync(event_id: int, judge_score: float | None, judge_reason: str, sampled: bool) -> None:
    """Write the slow performance-check (judge) result back onto an
    already-logged event row, and escalate to review_queue if the score
    indicates high hallucination risk. Called from a background task —
    never from the request path."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET judge_score = ?, judge_reason = ?, sampled = ? WHERE id = ?",
            (judge_score, judge_reason, int(sampled), event_id),
        )

        if judge_score is not None and judge_score > REVIEW_THRESHOLD:
            _push_review_queue(
                cursor,
                event_id,
                detector="performance",
                severity="high",
                reason=judge_reason,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        conn.commit()


async def update_judge_result(event_id: int, judge_score: float | None, judge_reason: str, sampled: bool) -> None:
    await anyio.to_thread.run_sync(_update_judge_result_sync, event_id, judge_score, judge_reason, sampled)


def _update_controlplane_verdict_sync(event_id: int, action: str, flags: list[str]) -> None:
    """Persist decision_engine's final verdict for this request. Called
    synchronously, still on the request path, right after decision_engine.decide()
    runs — this is just a cheap single-row UPDATE by primary key."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET controlplane_action = ?, controlplane_flags = ? WHERE id = ?",
            (action, json.dumps(flags), event_id),
        )
        conn.commit()


async def update_controlplane_verdict(event_id: int, action: str, flags: list[str]) -> None:
    await anyio.to_thread.run_sync(_update_controlplane_verdict_sync, event_id, action, flags)


_SEVERITY_ORDER_SQL = "CASE rq.severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"


def _row_to_review_item(row: tuple) -> dict:
    (
        review_id, event_id, detector, severity, reason, created_at, resolved,
        resolution, note, resolved_at,
        timestamp, model, request_id, status,
        request_body, response_body,
        cost_usd, cost_flagged, cost_flag_reason, cost_flag_z_score,
        responsibility_flagged, responsibility_action, responsibility_severity, responsibility_reason,
        judge_score, judge_reason, sampled,
        controlplane_action, controlplane_flags,
    ) = row

    return {
        "review_id": review_id,
        "event_id": event_id,
        "detector": detector,
        "severity": severity,
        "reason": reason,
        "created_at": created_at,
        "resolved": bool(resolved),
        "resolution": resolution,
        "note": note,
        "resolved_at": resolved_at,
        "event": {
            "timestamp": timestamp,
            "model": model,
            "request_id": request_id,
            "status": status,
            "input": json.loads(request_body) if request_body else None,
            "output": json.loads(response_body) if response_body else None,
            "cost": {
                "usd": cost_usd,
                "flagged": bool(cost_flagged),
                "reason": cost_flag_reason,
                "z_score": cost_flag_z_score,
            },
            "responsibility": {
                "flagged": bool(responsibility_flagged),
                "action": responsibility_action,
                "severity": responsibility_severity,
                "reason": responsibility_reason,
            },
            "performance": {
                "judge_score": judge_score,
                "judge_reason": judge_reason,
                "sampled": bool(sampled),
            },
            "controlplane": {
                "action": controlplane_action,
                "flags": json.loads(controlplane_flags) if controlplane_flags else [],
            },
        },
    }


def _list_review_queue_sync() -> list[dict]:
    """Pending (unresolved) review_queue items, each joined with its full
    event — every flag/score every detector produced, plus the raw request
    (`input`) and response (`output`) bodies. Sorted by severity (high first)
    then recency (most recent first) within a severity tier."""
    with _connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            f"""
            SELECT
                rq.id, rq.event_id, rq.detector, rq.severity, rq.reason,
                rq.created_at, rq.resolved, rq.resolution, rq.note, rq.resolved_at,
                e.timestamp, e.model, e.request_id, e.status,
                e.request_body, e.response_body,
                e.cost_usd, e.cost_flagged, e.cost_flag_reason, e.cost_flag_z_score,
                e.responsibility_flagged, e.responsibility_action,
                e.responsibility_severity, e.responsibility_reason,
                e.judge_score, e.judge_reason, e.sampled,
                e.controlplane_action, e.controlplane_flags
            FROM review_queue rq
            JOIN events e ON e.id = rq.event_id
            WHERE rq.resolved = 0
            ORDER BY {_SEVERITY_ORDER_SQL} DESC, rq.created_at DESC
            """
        ).fetchall()

    return [_row_to_review_item(row) for row in rows]


async def list_review_queue() -> list[dict]:
    return await anyio.to_thread.run_sync(_list_review_queue_sync)


def _resolve_review_queue_item_sync(review_id: int, resolution: str, note: str) -> dict | None:
    """Mark a review_queue item resolved and record the resolution. Returns
    None if no such item exists.

    Feedback-loop stub: a "confirmed_issue" resolution is also copied into
    confirmed_issues — the data you'd eventually mine to tune detector
    thresholds. Guarded so re-resolving an item that's already
    confirmed_issue doesn't insert a duplicate row; flipping a prior
    false_positive to confirmed_issue does insert one.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT event_id, detector, severity, reason, resolution FROM review_queue WHERE id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None

        event_id, detector, severity, reason, previous_resolution = row
        resolved_at = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            "UPDATE review_queue SET resolved = 1, resolution = ?, note = ?, resolved_at = ? WHERE id = ?",
            (resolution, note, resolved_at, review_id),
        )

        if resolution == "confirmed_issue" and previous_resolution != "confirmed_issue":
            cursor.execute(
                """
                INSERT INTO confirmed_issues (
                    review_queue_id, event_id, detector, severity, reason, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (review_id, event_id, detector, severity, reason, note, resolved_at),
            )

        conn.commit()

    return {
        "review_id": review_id,
        "event_id": event_id,
        "detector": detector,
        "severity": severity,
        "resolved": True,
        "resolution": resolution,
        "note": note,
        "resolved_at": resolved_at,
    }


async def resolve_review_queue_item(review_id: int, resolution: str, note: str) -> dict | None:
    return await anyio.to_thread.run_sync(_resolve_review_queue_item_sync, review_id, resolution, note)


def _row_to_event(row: tuple) -> dict:
    (
        event_id, timestamp, latency_ms, model, request_id, status, error,
        request_body, response_body,
        cost_usd, cost_flagged, cost_flag_reason, cost_flag_z_score,
        responsibility_flagged, responsibility_action, responsibility_severity, responsibility_reason,
        judge_score, judge_reason, sampled,
        controlplane_action, controlplane_flags,
    ) = row

    return {
        "id": event_id,
        "timestamp": timestamp,
        "latency_ms": latency_ms,
        "model": model,
        "request_id": request_id,
        "status": status,
        "error": error,
        "input": json.loads(request_body) if request_body else None,
        "output": json.loads(response_body) if response_body else None,
        "cost": {
            "usd": cost_usd,
            "flagged": bool(cost_flagged),
            "reason": cost_flag_reason,
            "z_score": cost_flag_z_score,
        },
        "responsibility": {
            "flagged": bool(responsibility_flagged),
            "action": responsibility_action,
            "severity": responsibility_severity,
            "reason": responsibility_reason,
        },
        "performance": {
            "judge_score": judge_score,
            "judge_reason": judge_reason,
            "sampled": bool(sampled),
        },
        "controlplane": {
            "action": controlplane_action,
            "flags": json.loads(controlplane_flags) if controlplane_flags else [],
        },
    }


def _list_events_sync(limit: int, offset: int) -> tuple[list[dict], int]:
    """Most-recent-first page of events, each carrying every detector's
    flags/scores plus the raw input/output bodies — the same enriched shape
    used by the review-queue endpoint, but for the events table directly."""
    with _connect() as conn:
        cursor = conn.cursor()
        total = cursor.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        rows = cursor.execute(
            """
            SELECT
                id, timestamp, latency_ms, model, request_id, status, error,
                request_body, response_body,
                cost_usd, cost_flagged, cost_flag_reason, cost_flag_z_score,
                responsibility_flagged, responsibility_action,
                responsibility_severity, responsibility_reason,
                judge_score, judge_reason, sampled,
                controlplane_action, controlplane_flags
            FROM events
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    return [_row_to_event(row) for row in rows], total


async def list_events(limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    return await anyio.to_thread.run_sync(_list_events_sync, limit, offset)


def _get_dashboard_summary_sync() -> dict:
    """Aggregates for the dashboard's summary strip. `requests_per_minute`
    is a live rate over the last 60 seconds; the other windowed metrics use
    the last hour, for one consistent "recent activity" picture. Cheap
    aggregate queries — no detector logic, just SUM/AVG/COUNT.
    """
    now = datetime.now(timezone.utc)
    one_minute_ago = (now - timedelta(minutes=1)).isoformat()
    one_hour_ago = (now - timedelta(hours=1)).isoformat()

    with _connect() as conn:
        cursor = conn.cursor()

        requests_per_minute = cursor.execute(
            "SELECT COUNT(*) FROM events WHERE timestamp >= ?", (one_minute_ago,)
        ).fetchone()[0]

        # "% flagged" = share of decided requests where the decision engine's
        # verdict was anything other than a clean pass-through (edited,
        # flagged, or blocked) — i.e. the yellow+red share of traffic.
        avg_latency_ms, total_cost_usd, flagged_n, decided_n = cursor.execute(
            """
            SELECT
                AVG(latency_ms),
                SUM(COALESCE(cost_usd, 0)),
                SUM(CASE WHEN controlplane_action IS NOT NULL AND controlplane_action != 'pass' THEN 1 ELSE 0 END),
                SUM(CASE WHEN controlplane_action IS NOT NULL THEN 1 ELSE 0 END)
            FROM events
            WHERE timestamp >= ?
            """,
            (one_hour_ago,),
        ).fetchone()

        pending_review_count = cursor.execute(
            "SELECT COUNT(*) FROM review_queue WHERE resolved = 0"
        ).fetchone()[0]

    pct_flagged = (flagged_n / decided_n * 100.0) if decided_n else 0.0

    return {
        "requests_per_minute": requests_per_minute,
        "avg_latency_ms": round(avg_latency_ms, 2) if avg_latency_ms is not None else 0.0,
        "total_cost_usd_last_hour": round(total_cost_usd or 0.0, 6),
        "pct_flagged_last_hour": round(pct_flagged, 2),
        "pending_review_count": pending_review_count,
    }


async def get_dashboard_summary() -> dict:
    return await anyio.to_thread.run_sync(_get_dashboard_summary_sync)
