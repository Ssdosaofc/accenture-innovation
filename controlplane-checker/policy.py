"""Configurable policy / risk-tiering layer.

Before this module existed, every detection threshold in this codebase was
a hardcoded Python module constant (decision_engine.BLOCK_Z_SCORE/
FLAG_Z_SCORE, detectors.performance.REVIEW_THRESHOLD/SLOW_CHECK_SAMPLE_RATE),
identical for every request regardless of who's calling or what it's for.
That's the "one-size-fits-all" problem: a customer-facing chatbot and an
internal batch job genuinely have different risk tolerance and latency
budgets, and a single global threshold set serves neither well.

This module loads named threshold profiles ("use cases") from
config/policies.json — plain JSON, editable without touching Python or
redeploying — and resolves a request's declared `use_case` to a `Policy`
object that decision_engine.decide() and the detectors read from, instead
of module constants. Unknown/absent use_case falls back to the configured
default rather than raising, matching this project's existing style of
never hard-failing on malformed optional input (see
detectors.performance.parse_judge_response).

Not a general policy engine — three tiers, one file, loaded once at process
startup (app/main.py's `lifespan()`). Retuning a tier requires editing the
JSON and restarting the process; there's no hot-reload or admin API. That's
an intentional prototype-scope tradeoff, documented as a known limitation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_POLICIES_PATH = Path(__file__).resolve().parent / "config" / "policies.json"

# Fields pulled from each policy's JSON object into the dataclass. Anything
# else in the JSON (e.g. "_comment") is ignored, so config authors can leave
# notes without breaking the loader.
_POLICY_FIELDS = (
    "block_z_score",
    "flag_z_score",
    "review_threshold",
    "slow_check_sample_rate",
    "bias_review_threshold",
)


@dataclass
class Policy:
    use_case: str
    block_z_score: float
    flag_z_score: float
    review_threshold: float
    slow_check_sample_rate: float
    bias_review_threshold: float


def load_policies(path: str | Path = DEFAULT_POLICIES_PATH) -> tuple[dict[str, Policy], str]:
    """Returns (policies_by_use_case, default_use_case).

    Never raises past a missing/malformed file in a way that would take the
    whole app down at import time — the caller (app/main.py's lifespan) is
    expected to call this once at startup, where a loud failure is actually
    fine (better to fail fast on a broken config file than serve requests
    against no thresholds at all). This function itself just does the
    parsing; it's synchronous file I/O run once, not per-request.
    """
    raw = json.loads(Path(path).read_text())
    default_use_case = raw.get("default_use_case", "internal")

    policies: dict[str, Policy] = {}
    for use_case, values in raw.get("policies", {}).items():
        policies[use_case] = Policy(
            use_case=use_case,
            **{field: float(values[field]) for field in _POLICY_FIELDS},
        )

    if default_use_case not in policies:
        raise ValueError(
            f"policies.json default_use_case={default_use_case!r} has no matching entry "
            f"in policies (found: {sorted(policies)})"
        )

    return policies, default_use_case


def resolve_policy(use_case: str | None, policies: dict[str, Policy], default_use_case: str) -> Policy:
    """Resolve a request's (optional, possibly unknown) use_case to a Policy.
    Falls back to the default tier silently rather than rejecting the
    request — an unrecognized use_case is a caller mistake, not grounds to
    refuse service."""
    if use_case and use_case in policies:
        return policies[use_case]
    return policies[default_use_case]
