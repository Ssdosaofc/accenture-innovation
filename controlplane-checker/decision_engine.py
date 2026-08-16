"""Decision engine — consolidates fast-tier detector outputs into a single
verdict, still on the request path, BEFORE the response leaves /v1/chat.

Only fast-tier signals exist at this point: the cost-anomaly z_score, the
PII/injection outcome from the responsibility detector, and (informationally)
the performance detector's fast heuristic. The slow-tier judge check
(detectors/performance.py tier 2) is async by design — it always runs as a
background task scheduled *after* the response has already gone out — so it
can only ever affect review_queue after the fact. It never reaches this
module, and can never change what the caller already received.

Decision rules, most severe wins:

  1. BLOCK  cost z_score > BLOCK_Z_SCORE (4.0). Nothing is forwarded to the
            caller; a 429-style error is returned instead, and the event is
            logged with controlplane_action="blocked".
  2. EDIT   PII was found in the outgoing response. detectors.responsibility
            _fast has already redacted it in place by the time this runs —
            this just reflects that outcome in the verdict.
  3. FLAG   cost z_score in [FLAG_Z_SCORE, BLOCK_Z_SCORE] (2.5-4), OR a
            prompt-injection marker was found in the input. Response passes
            through unchanged; the event gets high review-queue priority.
  4. PASS   nothing triggered.

BLOCK outranks EDIT: if a response is extreme enough to block, there is
nothing left to redact-and-return. The `flags` list, unlike `action`,
always reflects every fast-tier signal that fired — not just the one that
decided the action — so downstream consumers see the full picture even on
a "pass".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from detectors.cost import CostFlag
from detectors.performance import FastHeuristicResult
from detectors.responsibility_fast import ResponsibilityFlag

BLOCK_Z_SCORE = 4.0
FLAG_Z_SCORE = 2.5

ACTION_PASS = "pass"
ACTION_EDITED = "edited"
ACTION_FLAGGED = "flagged"
ACTION_BLOCKED = "blocked"


@dataclass
class Verdict:
    action: str  # "pass" | "edited" | "flagged" | "blocked"
    flags: list[str]
    reason: str  # populated for "blocked"; empty string for everything else

    def to_metadata(self, event_id: int | None) -> dict:
        """The `controlplane` block attached to every response the caller
        receives (success or blocked)."""
        return {"action": self.action, "flags": self.flags, "event_id": event_id}

    def to_dict(self) -> dict:
        return asdict(self)


def decide(
    cost_flag: CostFlag,
    responsibility_flag: ResponsibilityFlag,
    fast_heuristic_result: FastHeuristicResult | None = None,
) -> Verdict:
    """Pure function, no I/O — the caller (app/main.py) has already computed
    all three inputs synchronously before this runs, so this itself adds no
    latency of its own."""
    pii_redacted = responsibility_flag.action == "edited"
    # Checked via the reason string, not `action == "logged"`, because when
    # BOTH PII and an injection marker are present, responsibility_fast sets
    # action="edited" (PII redaction wins there) — the reason string still
    # names the injection marker either way.
    injection_marker = "prompt-injection markers found in input" in responsibility_flag.reason
    unhedged_claim = bool(fast_heuristic_result and fast_heuristic_result.flagged)

    extreme_cost_anomaly = cost_flag.z_score > BLOCK_Z_SCORE
    moderate_cost_anomaly = not extreme_cost_anomaly and cost_flag.z_score >= FLAG_Z_SCORE
    any_cost_anomaly = extreme_cost_anomaly or moderate_cost_anomaly

    flags: list[str] = []
    if any_cost_anomaly:
        flags.append("cost_anomaly")
    if pii_redacted:
        flags.append("pii_redacted")
    if injection_marker:
        flags.append("injection_marker")
    if unhedged_claim:
        flags.append("unhedged_claim")

    if extreme_cost_anomaly:
        return Verdict(
            action=ACTION_BLOCKED,
            flags=flags,
            reason=f"cost anomaly blocked (z_score={cost_flag.z_score:.2f}): {cost_flag.reason}",
        )

    if pii_redacted:
        return Verdict(action=ACTION_EDITED, flags=flags, reason="")

    if moderate_cost_anomaly or injection_marker:
        return Verdict(action=ACTION_FLAGGED, flags=flags, reason="")

    return Verdict(action=ACTION_PASS, flags=flags, reason="")
