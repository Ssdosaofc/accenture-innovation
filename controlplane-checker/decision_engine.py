"""Decision engine — consolidates fast-tier detector outputs into a single
verdict, still on the request path, BEFORE the response leaves /v1/chat.

Only fast-tier signals exist at this point: the cost-anomaly z_score, the
PII/injection outcome from the responsibility detector, the bias detector's
fast-tier match, and (informationally) the performance detector's fast
heuristic. The slow-tier judge checks (detectors/performance.py and
detectors/bias.py, tier 2 each) are async by design — they always run as
background tasks scheduled *after* the response has already gone out — so
they can only ever affect review_queue after the fact. Neither reaches this
module, and neither can ever change what the caller already received.

Thresholds are no longer hardcoded here — they come from a `Policy` (see
policy.py), resolved per-request from the caller's declared `use_case` (a
customer-facing chatbot and an internal batch job have different risk
tolerance; a single global threshold set serves neither well). The module
constants below are only the *fallback default* (`DEFAULT_POLICY`), used
when no policy is explicitly passed in — this keeps every existing caller
(including tests that don't thread a policy through) behaviorally unchanged.

Decision rules, most severe wins:

  1. BLOCK  cost z_score > policy.block_z_score. Nothing is forwarded to the
            caller; a 429-style error is returned instead, and the event is
            logged with controlplane_action="blocked".
  2. EDIT   PII was found in the outgoing response. detectors.responsibility
            _fast has already redacted it in place by the time this runs —
            this just reflects that outcome in the verdict.
  3. FLAG   cost z_score in [policy.flag_z_score, policy.block_z_score], OR
            a prompt-injection marker was found in the input, OR the bias
            detector's fast tier matched, OR the session-risk detector
            (detectors/session.py) found enough accumulated non-pass
            verdicts earlier in this message's session. Response passes
            through unchanged; the event gets high review-queue priority.
  4. PASS   nothing triggered.

BLOCK outranks EDIT: if a response is extreme enough to block, there is
nothing left to redact-and-return. Bias and session-risk never block at the
fast tier (see detectors/bias.py's and detectors/session.py's module
docstrings for why) — they only ever contribute to FLAG. The `flags` list,
unlike `action`, always reflects every fast-tier signal that fired — not
just the one that decided the action — so downstream consumers see the full
picture even on a "pass".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from detectors.cost import CostFlag
from detectors.performance import FastHeuristicResult
from detectors.responsibility_fast import ResponsibilityFlag
from policy import Policy

if TYPE_CHECKING:
    from detectors.bias import FastBiasResult
    from detectors.session import SessionRiskFlag

# Fallback defaults — used only when decide() is called without an explicit
# policy. Kept as plain constants (rather than deleted) so DEFAULT_POLICY
# below, and anything constructing a Policy by hand, has a documented
# baseline to start from.
BLOCK_Z_SCORE = 4.0
FLAG_Z_SCORE = 2.5
REVIEW_THRESHOLD = 0.6
SLOW_CHECK_SAMPLE_RATE = 0.10
BIAS_REVIEW_THRESHOLD = 0.6

DEFAULT_POLICY = Policy(
    use_case="internal",
    block_z_score=BLOCK_Z_SCORE,
    flag_z_score=FLAG_Z_SCORE,
    review_threshold=REVIEW_THRESHOLD,
    slow_check_sample_rate=SLOW_CHECK_SAMPLE_RATE,
    bias_review_threshold=BIAS_REVIEW_THRESHOLD,
)

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
    bias_flag: "FastBiasResult | None" = None,
    policy: Policy = DEFAULT_POLICY,
    session_flag: "SessionRiskFlag | None" = None,
) -> Verdict:
    """Pure function, no I/O — the caller (app/main.py) has already computed
    every input synchronously before this runs, so this itself adds no
    latency of its own. `policy` resolves which thresholds apply for this
    request's use_case (see policy.py) — defaults to DEFAULT_POLICY, which
    matches this module's original hardcoded constants, so any caller that
    doesn't pass a policy keeps its old behavior exactly. `session_flag`
    (detectors/session.py) can escalate an otherwise-clean message to FLAG
    based on accumulated risk earlier in the same session — it never
    overrides an EDIT/BLOCK that already fired on this message's own
    content, only adds a FLAG-level signal, same composition as bias."""
    pii_redacted = responsibility_flag.action == "edited"
    # Checked via the reason string, not `action == "logged"`, because when
    # BOTH PII and an injection marker are present, responsibility_fast sets
    # action="edited" (PII redaction wins there) — the reason string still
    # names the injection marker either way.
    injection_marker = "prompt-injection markers found in input" in responsibility_flag.reason
    unhedged_claim = bool(fast_heuristic_result and fast_heuristic_result.flagged)
    bias_matched = bool(bias_flag and bias_flag.flagged)
    session_risk = bool(session_flag and session_flag.flagged)

    extreme_cost_anomaly = cost_flag.z_score > policy.block_z_score
    moderate_cost_anomaly = not extreme_cost_anomaly and cost_flag.z_score >= policy.flag_z_score
    any_cost_anomaly = extreme_cost_anomaly or moderate_cost_anomaly

    flags: list[str] = []
    if any_cost_anomaly:
        flags.append("cost_anomaly")
    if pii_redacted:
        flags.append("pii_redacted")
    if injection_marker:
        flags.append("injection_marker")
    if bias_matched:
        flags.append("bias_flag")
    if session_risk:
        flags.append("session_risk")
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

    if moderate_cost_anomaly or injection_marker or bias_matched or session_risk:
        return Verdict(action=ACTION_FLAGGED, flags=flags, reason="")

    return Verdict(action=ACTION_PASS, flags=flags, reason="")
