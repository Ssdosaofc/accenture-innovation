"""Cost anomaly detector.

Runs synchronously in the /v1/chat pipeline, right before the event is
logged and the response returned. Pure SQLite aggregation over the existing
`events` table — no external calls, no network I/O, negligible latency.

Three checks:
  1. cost_usd is computed from a hardcoded per-model price table.
  2. Token count and cost are compared against a rolling per-model baseline
     (mean + population stddev) built from the last ROLLING_WINDOW events.
  3. Identical message content (hashed) repeated more than RETRY_THRESHOLD
     times within RETRY_WINDOW_MINUTES is flagged as a retry loop.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

# USD per 1,000,000 tokens. Unknown models fall back to DEFAULT_PRICE so cost
# tracking never silently breaks when a new model shows up in requests.
# Pulled from GET https://api.groq.com/openai/v1/models pricing (per-token
# figures there, multiplied by 1e6 here) — Groq's model lineup rotates, so
# re-check that endpoint if requests start 404ing with "model not found".
PRICE_TABLE_PER_MTOK: dict[str, dict[str, float]] = {
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    "openai/gpt-oss-20b": {"input": 0.075, "output": 0.30},
    "openai/gpt-oss-safeguard-20b": {"input": 0.075, "output": 0.30},
    "qwen/qwen3.8-27b": {"input": 0.80, "output": 4.00},
    "qwen/qwen3.6-27b": {"input": 0.60, "output": 3.00},
}
DEFAULT_PRICE = {"input": 0.60, "output": 3.00}  # conservative fallback for unlisted models

ROLLING_WINDOW = 100  # most recent successful events considered for the baseline
Z_SCORE_THRESHOLD = 2.5
RETRY_WINDOW_MINUTES = 5
RETRY_THRESHOLD = 3  # this occurrence + RETRY_THRESHOLD prior identical ones = "more than 3 times"
MIN_BASELINE_SAMPLES = 5  # don't trust a stddev computed from fewer prior events than this
MIN_MEANINGFUL_STD = 1e-6  # floor below which stddev is treated as float noise, not real variance


@dataclass
class CostFlag:
    flagged: bool
    reason: str
    z_score: float
    cost_usd: float

    def to_dict(self) -> dict:
        return asdict(self)


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICE_TABLE_PER_MTOK.get(model, DEFAULT_PRICE)
    return (input_tokens / 1_000_000) * price["input"] + (output_tokens / 1_000_000) * price["output"]


def hash_messages(messages: list) -> str:
    """Stable hash of role+content pairs, used for retry-loop detection."""
    canonical = json.dumps(
        [{"role": m.get("role"), "content": m.get("content")} for m in messages],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _baseline_stats(cursor: sqlite3.Cursor, model: str) -> tuple[float, float, float, float, int]:
    """Mean / population-stddev of total_tokens and cost_usd over the last
    ROLLING_WINDOW successful events for this model. Single indexed query;
    stddev computed in Python from the fetched rows (no external stats lib)."""
    rows = cursor.execute(
        """
        SELECT input_tokens, output_tokens, cost_usd
        FROM events
        WHERE model = ? AND status = 'success'
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (model, ROLLING_WINDOW),
    ).fetchall()

    n = len(rows)
    if n < MIN_BASELINE_SAMPLES:
        return 0.0, 0.0, 0.0, 0.0, n

    tokens = [(r[0] or 0) + (r[1] or 0) for r in rows]
    costs = [r[2] or 0.0 for r in rows]

    token_mean = sum(tokens) / n
    cost_mean = sum(costs) / n
    token_std = math.sqrt(sum((t - token_mean) ** 2 for t in tokens) / n)
    cost_std = math.sqrt(sum((c - cost_mean) ** 2 for c in costs) / n)

    return token_mean, token_std, cost_mean, cost_std, n


def _retry_loop_count(cursor: sqlite3.Cursor, message_hash: str) -> int:
    """Count of prior events with identical message content in the retry window."""
    if not message_hash:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RETRY_WINDOW_MINUTES)).isoformat()
    row = cursor.execute(
        "SELECT COUNT(*) FROM events WHERE message_hash = ? AND timestamp >= ?",
        (message_hash, cutoff),
    ).fetchone()
    return row[0] if row else 0


def evaluate(
    cursor: sqlite3.Cursor,
    model: str,
    input_tokens: int,
    output_tokens: int,
    message_hash: str,
) -> CostFlag:
    """Run all anomaly checks for one request.

    Must be called BEFORE the current event row is inserted, so the rolling
    baseline and retry count reflect only prior history.
    """
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cost_usd = compute_cost(model, input_tokens, output_tokens)
    total_tokens = input_tokens + output_tokens

    token_mean, token_std, cost_mean, cost_std, n = _baseline_stats(cursor, model)

    # Guard against a near-zero (not just exactly-zero) stddev: when a
    # baseline happens to be uniform or near-uniform, float rounding noise
    # in the variance sum can leave token_std/cost_std as a tiny nonzero
    # epsilon (e.g. ~1e-19) instead of exact 0.0. Dividing by that produces
    # an astronomically large, meaningless z_score rather than a real
    # anomaly signal — MIN_MEANINGFUL_STD floors both at a value far above
    # any float-noise scale but far below any real-world stddev.
    token_z = (total_tokens - token_mean) / token_std if token_std > MIN_MEANINGFUL_STD else 0.0
    cost_z = (cost_usd - cost_mean) / cost_std if cost_std > MIN_MEANINGFUL_STD else 0.0
    z_score = max(token_z, cost_z)

    reasons: list[str] = []
    if n >= MIN_BASELINE_SAMPLES:
        if token_z > Z_SCORE_THRESHOLD:
            reasons.append(
                f"token_count={total_tokens} is {token_z:.2f} stddev above baseline mean "
                f"{token_mean:.0f} (n={n})"
            )
        if cost_z > Z_SCORE_THRESHOLD:
            reasons.append(
                f"cost_usd={cost_usd:.6f} is {cost_z:.2f} stddev above baseline mean "
                f"{cost_mean:.6f} (n={n})"
            )

    retry_count = _retry_loop_count(cursor, message_hash)
    if retry_count >= RETRY_THRESHOLD:
        reasons.append(
            f"identical message content seen {retry_count + 1} times in the last "
            f"{RETRY_WINDOW_MINUTES} minutes (retry-loop suspected)"
        )

    flagged = len(reasons) > 0
    reason = "; ".join(reasons) if reasons else "within baseline"

    return CostFlag(
        flagged=flagged,
        reason=reason,
        z_score=round(z_score, 3),
        cost_usd=round(cost_usd, 6),
    )
