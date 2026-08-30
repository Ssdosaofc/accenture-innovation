import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import anyio

from audit import GENESIS_HASH, compute_row_hash
from detectors import session as session_detector
from detectors.cost import CostFlag, evaluate
from detectors.performance import REVIEW_THRESHOLD
from detectors.session import SessionRiskFlag

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

# Tamper-evident audit trail (audit.py): one append-only entry per lifecycle
# milestone (event logged, verdict decided, judge result attached, review
# resolved), each hash-chained to the previous entry via prev_hash/row_hash.
# Deliberately separate from `events`/`review_queue` rather than freezing
# their columns — those are legitimately updated multiple times over an
# event's lifecycle, so "tamper-evident" has to mean "the sequence of writes
# is provable," not "the row never changes."
AUDIT_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    event_id INTEGER,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
"""

# (table, column, ddl) for columns added after the initial CREATE TABLE
# shipped. SQLite has no "ADD COLUMN IF NOT EXISTS", so init_db() applies
# these with the duplicate-column error swallowed — a lightweight migration
# path for any database file created before this endpoint pair existed.
_COLUMN_MIGRATIONS = (
    ("review_queue", "resolution", "TEXT"),
    ("review_queue", "note", "TEXT"),
    ("review_queue", "resolved_at", "TEXT"),
    # Policy / risk-tiering layer (policy.py): which use_case was requested,
    # and a snapshot of the thresholds actually applied — stored on the row
    # itself, not just the use_case name, so a later policies.json edit
    # can't retroactively re-interpret a past decision during audit.
    ("events", "use_case", "TEXT"),
    ("events", "policy_block_z_score", "REAL"),
    ("events", "policy_flag_z_score", "REAL"),
    ("events", "policy_review_threshold", "REAL"),
    # Bias detector (detectors/bias.py) — mirrors the judge_score/judge_reason
    # /sampled columns used for the performance detector, plus fast-tier
    # columns since (unlike performance's fast tier) a bias fast-tier match
    # is itself action-determining (see decision_engine.decide()).
    ("events", "bias_flagged", "INTEGER"),
    ("events", "bias_fast_reason", "TEXT"),
    ("events", "bias_judge_score", "REAL"),
    ("events", "bias_judge_reason", "TEXT"),
    ("events", "bias_sampled", "INTEGER NOT NULL DEFAULT 0"),
    # Session risk detector (detectors/session.py) — session_id is an
    # optional caller-supplied field grouping requests into a conversation;
    # session_flagged/session_flag_reason record whether THIS message was
    # escalated due to accumulated non-pass verdicts earlier in its session.
    ("events", "session_id", "TEXT"),
    ("events", "session_flagged", "INTEGER"),
    ("events", "session_flag_reason", "TEXT"),
    # Retrieval-grounded hallucination checking — whether the slow
    # performance judge check for this event was run against caller-supplied
    # context_documents (detectors/performance.py's grounded prompt) rather
    # than the default plausibility-only prompt. The documents themselves
    # aren't duplicated into their own column — they're already preserved
    # in request_body via the existing raw-request logging.
    ("events", "performance_grounded", "INTEGER NOT NULL DEFAULT 0"),
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
        conn.execute(AUDIT_LOG_SCHEMA)
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


def _append_audit_log_sync(
    cursor: sqlite3.Cursor, action: str, event_id: int | None, payload: dict
) -> None:
    """Append one entry to the hash chain, in the SAME transaction as the
    caller's own write (called with that write's own cursor, never a new
    connection) — so the audit trail and the underlying row can never drift
    apart. Reads the current chain tip (or GENESIS_HASH if empty) and
    chains onto it."""
    tip = cursor.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = tip[0] if tip else GENESIS_HASH
    timestamp = datetime.now(timezone.utc).isoformat()
    row_hash = compute_row_hash(prev_hash, action, event_id, timestamp, payload)
    cursor.execute(
        """
        INSERT INTO audit_log (timestamp, action, event_id, payload, prev_hash, row_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (timestamp, action, event_id, json.dumps(payload, sort_keys=True), prev_hash, row_hash),
    )


def _log_event_sync(event: dict) -> tuple[CostFlag, SessionRiskFlag, int]:
    """Run the cost and session-risk detectors against prior history, then
    insert this event (including both resulting flags) in the same
    connection so neither detector's view of history ever includes the row
    being inserted.

    The responsibility flag (PII/injection) is computed by the caller before
    this is invoked — it needs no database access — and is passed through
    on `event` for storage only. An injection-marker finding
    (`responsibility_action == "logged"`) or a session-risk escalation is
    pushed to review_queue here, same as a high-confidence judge finding is
    pushed from `_update_judge_result_sync` below.

    Returns (cost_flag, session_flag, event_id) — the caller needs event_id
    to later attach the slow performance-check result to this same row.
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
        session_flag = session_detector.evaluate(cursor, event.get("session_id"))

        cursor.execute(
            """
            INSERT INTO events (
                timestamp, latency_ms, model, request_id,
                input_tokens, output_tokens, request_body, response_body,
                status, error, message_hash,
                cost_usd, cost_flagged, cost_flag_reason, cost_flag_z_score,
                responsibility_flagged, responsibility_action,
                responsibility_severity, responsibility_reason,
                use_case, policy_block_z_score, policy_flag_z_score, policy_review_threshold,
                bias_flagged, bias_fast_reason,
                session_id, session_flagged, session_flag_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.get("use_case"),
                event.get("policy_block_z_score"),
                event.get("policy_flag_z_score"),
                event.get("policy_review_threshold"),
                int(event.get("bias_flagged", False)),
                event.get("bias_fast_reason"),
                event.get("session_id"),
                int(session_flag.flagged),
                session_flag.reason,
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

        if session_flag.flagged:
            _push_review_queue(
                cursor,
                event_id,
                detector="session",
                severity="high",
                reason=session_flag.reason,
                created_at=event["timestamp"],
            )

        _append_audit_log_sync(
            cursor,
            action="event_logged",
            event_id=event_id,
            payload={
                "model": event["model"],
                "request_id": event.get("request_id"),
                "status": event["status"],
                "cost_usd": cost_flag.cost_usd,
                "use_case": event.get("use_case"),
                "session_id": event.get("session_id"),
                "session_flagged": session_flag.flagged,
            },
        )

        conn.commit()

    return cost_flag, session_flag, event_id


async def log_event(event: dict) -> tuple[CostFlag, SessionRiskFlag, int]:
    """Detect cost/session-risk anomalies against the rolling baseline and
    log the event. Runs in a worker thread since sqlite3 is blocking; both
    detectors are pure in-process SQLite aggregation, so this adds
    negligible latency."""
    return await anyio.to_thread.run_sync(_log_event_sync, event)


def _update_judge_result_sync(
    event_id: int,
    judge_score: float | None,
    judge_reason: str,
    sampled: bool,
    review_threshold: float = REVIEW_THRESHOLD,
    grounded: bool = False,
) -> None:
    """Write the slow performance-check (judge) result back onto an
    already-logged event row, and escalate to review_queue if the score
    indicates high hallucination risk. Called from a background task —
    never from the request path. `review_threshold` defaults to the module
    constant but is normally the value from the Policy that was resolved
    for this request (see policy.py), so it can vary by use_case.
    `grounded` records whether this check ran against caller-supplied
    context_documents (detectors.performance.build_grounded_judge_prompt)
    rather than the default plausibility-only prompt."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET judge_score = ?, judge_reason = ?, sampled = ?, performance_grounded = ? WHERE id = ?",
            (judge_score, judge_reason, int(sampled), int(grounded), event_id),
        )

        if judge_score is not None and judge_score > review_threshold:
            _push_review_queue(
                cursor,
                event_id,
                detector="performance",
                severity="high",
                reason=judge_reason,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        _append_audit_log_sync(
            cursor,
            action="judge_result",
            event_id=event_id,
            payload={"judge_score": judge_score, "sampled": bool(sampled), "grounded": bool(grounded)},
        )

        conn.commit()


async def update_judge_result(
    event_id: int,
    judge_score: float | None,
    judge_reason: str,
    sampled: bool,
    review_threshold: float = REVIEW_THRESHOLD,
    grounded: bool = False,
) -> None:
    await anyio.to_thread.run_sync(
        _update_judge_result_sync, event_id, judge_score, judge_reason, sampled, review_threshold, grounded
    )


def _update_bias_judge_result_sync(
    event_id: int,
    bias_judge_score: float | None,
    bias_judge_reason: str,
    sampled: bool,
    review_threshold: float,
) -> None:
    """Bias-detector counterpart to `_update_judge_result_sync` — same
    shape, same escalation rule, different columns/detector name. Called
    from a background task — never from the request path."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET bias_judge_score = ?, bias_judge_reason = ?, bias_sampled = ? WHERE id = ?",
            (bias_judge_score, bias_judge_reason, int(sampled), event_id),
        )

        if bias_judge_score is not None and bias_judge_score > review_threshold:
            _push_review_queue(
                cursor,
                event_id,
                detector="bias",
                severity="high",
                reason=bias_judge_reason,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        _append_audit_log_sync(
            cursor,
            action="bias_judge_result",
            event_id=event_id,
            payload={"bias_judge_score": bias_judge_score, "sampled": bool(sampled)},
        )

        conn.commit()


async def update_bias_judge_result(
    event_id: int,
    bias_judge_score: float | None,
    bias_judge_reason: str,
    sampled: bool,
    review_threshold: float,
) -> None:
    await anyio.to_thread.run_sync(
        _update_bias_judge_result_sync, event_id, bias_judge_score, bias_judge_reason, sampled, review_threshold
    )


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
        _append_audit_log_sync(
            cursor,
            action="verdict_decided",
            event_id=event_id,
            payload={"action": action, "flags": flags},
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
        use_case, policy_block_z_score, policy_flag_z_score, policy_review_threshold,
        bias_flagged, bias_fast_reason, bias_judge_score, bias_judge_reason, bias_sampled,
        session_id, session_flagged, session_flag_reason, performance_grounded,
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
            "use_case": use_case or "unknown",  # NULL = row predates the policy layer
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
                "grounded": bool(performance_grounded),
            },
            "bias": {
                "fast_flagged": bool(bias_flagged),
                "fast_reason": bias_fast_reason,
                "judge_score": bias_judge_score,
                "judge_reason": bias_judge_reason,
                "sampled": bool(bias_sampled),
            },
            "session": {
                "session_id": session_id,
                "flagged": bool(session_flagged),
                "reason": session_flag_reason,
            },
            "controlplane": {
                "action": controlplane_action,
                "flags": json.loads(controlplane_flags) if controlplane_flags else [],
            },
            "policy": {
                "block_z_score": policy_block_z_score,
                "flag_z_score": policy_flag_z_score,
                "review_threshold": policy_review_threshold,
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
                e.controlplane_action, e.controlplane_flags,
                e.use_case, e.policy_block_z_score, e.policy_flag_z_score, e.policy_review_threshold,
                e.bias_flagged, e.bias_fast_reason, e.bias_judge_score, e.bias_judge_reason, e.bias_sampled,
                e.session_id, e.session_flagged, e.session_flag_reason, e.performance_grounded
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

        _append_audit_log_sync(
            cursor,
            action="review_resolved",
            event_id=event_id,
            payload={"review_id": review_id, "resolution": resolution},
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
        use_case, policy_block_z_score, policy_flag_z_score, policy_review_threshold,
        bias_flagged, bias_fast_reason, bias_judge_score, bias_judge_reason, bias_sampled,
        session_id, session_flagged, session_flag_reason, performance_grounded,
    ) = row

    return {
        "id": event_id,
        "timestamp": timestamp,
        "latency_ms": latency_ms,
        "model": model,
        "request_id": request_id,
        "status": status,
        "error": error,
        "use_case": use_case or "unknown",  # NULL = row predates the policy layer
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
            "grounded": bool(performance_grounded),
        },
        "bias": {
            "fast_flagged": bool(bias_flagged),
            "fast_reason": bias_fast_reason,
            "judge_score": bias_judge_score,
            "judge_reason": bias_judge_reason,
            "sampled": bool(bias_sampled),
        },
        "session": {
            "session_id": session_id,
            "flagged": bool(session_flagged),
            "reason": session_flag_reason,
        },
        "controlplane": {
            "action": controlplane_action,
            "flags": json.loads(controlplane_flags) if controlplane_flags else [],
        },
        "policy": {
            "block_z_score": policy_block_z_score,
            "flag_z_score": policy_flag_z_score,
            "review_threshold": policy_review_threshold,
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
                controlplane_action, controlplane_flags,
                use_case, policy_block_z_score, policy_flag_z_score, policy_review_threshold,
                bias_flagged, bias_fast_reason, bias_judge_score, bias_judge_reason, bias_sampled,
                session_id, session_flagged, session_flag_reason, performance_grounded
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


# --- Feedback loop / FP-FN metrics ------------------------------------------
#
# Closes the loop on `confirmed_issues`/`review_queue.resolution`, which
# until now were pure write-only stubs (populated on every resolve, never
# read back). This is all-time, per-detector accuracy data — a different
# axis from `/summary`'s rolling-window operational snapshot above, which is
# why it's a separate endpoint rather than folded into that one.
#
# Deliberately observability-only: it computes and surfaces a false-positive
# warning, it does not adjust any threshold automatically. That's a scoping
# decision, not an oversight — see the module docstring pattern already
# established elsewhere in this codebase (e.g. detectors/performance.py's
# "no retraining logic needed yet" framing).

FEEDBACK_FP_WARNING_MIN_N = 5  # mirrors detectors.cost.MIN_BASELINE_SAMPLES's
# "don't trust a stat computed from too few samples" convention
FEEDBACK_FP_WARNING_THRESHOLD = 0.5  # majority-false-positive is an
# unambiguous "this detector is currently more wrong than right" signal

# Every detector that CAN escalate to review_queue today, so the response
# always has a stable 5-key shape even before any bias/performance/session
# finding has ever been resolved. "cost" never escalates to review_queue (an
# extreme cost anomaly is an immediate BLOCK, not a review item) but is
# included with zeros rather than omitted, for shape-consistency with the
# "detector categories" story.
_ALL_DETECTORS = ("cost", "responsibility", "performance", "bias", "session")


def _get_feedback_stats_sync() -> dict:
    with _connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT
                detector,
                SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) AS total_resolved,
                SUM(CASE WHEN resolution = 'confirmed_issue' THEN 1 ELSE 0 END) AS confirmed_count,
                SUM(CASE WHEN resolution = 'false_positive' THEN 1 ELSE 0 END) AS false_positive_count
            FROM review_queue
            GROUP BY detector
            """
        ).fetchall()

    by_detector = {detector: (0, 0, 0) for detector in _ALL_DETECTORS}
    for detector, total_resolved, confirmed_count, false_positive_count in rows:
        by_detector[detector] = (total_resolved or 0, confirmed_count or 0, false_positive_count or 0)

    detectors: dict[str, dict] = {}
    warnings: list[dict] = []

    for detector, (total_resolved, confirmed_count, false_positive_count) in by_detector.items():
        confirmed_rate = (confirmed_count / total_resolved) if total_resolved else None
        detectors[detector] = {
            "total_resolved": total_resolved,
            "confirmed_count": confirmed_count,
            "false_positive_count": false_positive_count,
            "confirmed_rate": round(confirmed_rate, 3) if confirmed_rate is not None else None,
        }

        if total_resolved >= FEEDBACK_FP_WARNING_MIN_N:
            fp_rate = false_positive_count / total_resolved
            if fp_rate > FEEDBACK_FP_WARNING_THRESHOLD:
                warnings.append(
                    {
                        "detector": detector,
                        "false_positive_rate": round(fp_rate, 3),
                        "n": total_resolved,
                        "message": (
                            f"{detector}: {false_positive_count}/{total_resolved} resolved review-queue "
                            f"items were false positives ({fp_rate:.0%}) — this detector is currently "
                            f"more wrong than right on what it escalates."
                        ),
                    }
                )

    return {"detectors": detectors, "warnings": warnings}


async def get_feedback_stats() -> dict:
    return await anyio.to_thread.run_sync(_get_feedback_stats_sync)


# --- Tamper-evident audit trail (verification) ------------------------------

def _verify_audit_chain_sync() -> dict:
    """Recomputes every audit_log row's hash from scratch and compares it
    against what's stored, plus checks each row's stored prev_hash matches
    the previous row's stored row_hash. Never raises — a corrupted/
    unparseable payload is treated as a break at that id, not an exception,
    since this needs to be safe to call against a genuinely tampered
    database."""
    with _connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT id, timestamp, action, event_id, payload, prev_hash, row_hash "
            "FROM audit_log ORDER BY id ASC"
        ).fetchall()

    expected_prev = GENESIS_HASH
    for row_id, timestamp, action, event_id, payload_text, prev_hash, row_hash in rows:
        if prev_hash != expected_prev:
            return {
                "valid": False,
                "checked": len(rows),
                "first_break_id": row_id,
                "reason": f"row {row_id}'s prev_hash does not match the previous row's row_hash",
            }
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError):
            return {
                "valid": False,
                "checked": len(rows),
                "first_break_id": row_id,
                "reason": f"row {row_id}'s payload is not valid JSON — cannot verify",
            }
        recomputed = compute_row_hash(prev_hash, action, event_id, timestamp, payload)
        if recomputed != row_hash:
            return {
                "valid": False,
                "checked": len(rows),
                "first_break_id": row_id,
                "reason": f"row {row_id}'s stored row_hash does not match its recomputed hash — "
                "the row was edited after being written",
            }
        expected_prev = row_hash

    return {"valid": True, "checked": len(rows), "first_break_id": None, "reason": None}


async def verify_audit_chain() -> dict:
    return await anyio.to_thread.run_sync(_verify_audit_chain_sync)


# --- Automated threshold tuning (suggestions only) ---------------------------
#
# Only `performance.review_threshold` and `bias.bias_review_threshold` are
# tunable from review_queue data: `cost` never escalates to review_queue (an
# extreme anomaly blocks immediately — no human resolution ever exists to
# learn from), and `responsibility` is binary regex-triggered (PII redaction,
# injection flag) with no single numeric threshold to move. So this
# deliberately does NOT reuse `_ALL_DETECTORS` from the feedback-stats
# section above — only the two detectors with a tunable float threshold
# gating a judge-scored, human-resolved queue item are eligible here.
#
# Only ever suggests RAISING a threshold, never lowering one: review_queue
# only contains items that were already flagged, so this data has zero
# visibility into false negatives (things that should have been flagged but
# weren't). A high false-positive rate is unambiguous evidence a threshold
# is too loose in the "escalates too much" direction; a low false-positive
# rate is not evidence it could be tightened, since nothing here can see
# what got missed.

_TUNABLE_DETECTOR_FIELDS = {
    "performance": "review_threshold",
    "bias": "bias_review_threshold",
}
POLICY_SUGGESTION_STEP = 0.1
POLICY_SUGGESTION_CAP = 0.95


def _get_policy_suggestions_sync(policies: dict) -> dict:
    with _connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT rq.detector, e.use_case,
                   SUM(CASE WHEN rq.resolved = 1 THEN 1 ELSE 0 END) AS total_resolved,
                   SUM(CASE WHEN rq.resolution = 'false_positive' THEN 1 ELSE 0 END) AS false_positive_count
            FROM review_queue rq
            JOIN events e ON e.id = rq.event_id
            WHERE rq.detector IN ('performance', 'bias')
            GROUP BY rq.detector, e.use_case
            """
        ).fetchall()

    suggestions: list[dict] = []
    for detector, use_case, total_resolved, false_positive_count in rows:
        total_resolved = total_resolved or 0
        false_positive_count = false_positive_count or 0
        if total_resolved < FEEDBACK_FP_WARNING_MIN_N:
            continue
        fp_rate = false_positive_count / total_resolved
        if fp_rate <= FEEDBACK_FP_WARNING_THRESHOLD:
            continue

        policy = policies.get(use_case) if use_case else None
        if policy is None:
            continue  # NULL/unrecognized use_case (pre-policy-layer rows) — nothing to suggest against

        field = _TUNABLE_DETECTOR_FIELDS[detector]
        current_value = getattr(policy, field)
        suggested_value = round(min(current_value + POLICY_SUGGESTION_STEP, POLICY_SUGGESTION_CAP), 2)

        suggestions.append(
            {
                "use_case": use_case,
                "detector": detector,
                "field": field,
                "current_value": current_value,
                "suggested_value": suggested_value,
                "false_positive_rate": round(fp_rate, 3),
                "n": total_resolved,
                "reason": (
                    f"{false_positive_count} of {total_resolved} resolved {detector} review-queue items "
                    f"under use_case={use_case!r} were false_positive ({fp_rate:.0%}) — raising {field} "
                    "should reduce false escalations. No suggestions to lower a threshold are made: "
                    "review_queue only contains items that were already flagged, so this data has no "
                    "visibility into missed (false-negative) cases."
                ),
            }
        )

    return {
        "suggestions": suggestions,
        "note": (
            "Suggestions only — edit config/policies.json and restart the server to apply any of "
            "these. Nothing is ever changed automatically."
        ),
    }


async def get_policy_suggestions(policies: dict) -> dict:
    return await anyio.to_thread.run_sync(_get_policy_suggestions_sync, policies)
