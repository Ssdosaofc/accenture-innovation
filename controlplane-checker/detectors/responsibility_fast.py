"""Fast, synchronous responsibility/safety detector.

Runs inline in the /v1/chat pipeline: after the model response comes back,
before it's returned to the caller. Two independent, pure-regex checks — no
model calls, no I/O — designed to complete in single-digit milliseconds even
on multi-KB responses.

1. PII scan on the OUTGOING response text. Matches are redacted in place
   (`[REDACTED:TYPE]`) before the response is returned to the caller.
   action = "edited".
2. Prompt-injection marker scan on the ORIGINAL user input. Never blocks or
   edits anything — just flags the event for later human review.
   action = "logged", severity = "high".

Patterns live in detectors/responsibility_patterns.py, not here, so new
signatures can be added without touching this file.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from detectors.responsibility_patterns import INJECTION_PATTERNS, PII_PATTERNS

_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

_PII_REGEXES: dict[str, re.Pattern] = {
    name: re.compile(cfg["pattern"], re.IGNORECASE) for name, cfg in PII_PATTERNS.items()
}
_PII_SEVERITY: dict[str, str] = {name: cfg["severity"] for name, cfg in PII_PATTERNS.items()}
_INJECTION_REGEXES: dict[str, re.Pattern] = {
    name: re.compile(pattern, re.IGNORECASE) for name, pattern in INJECTION_PATTERNS.items()
}


@dataclass
class ResponsibilityFlag:
    flagged: bool
    action: str  # "none" | "edited" | "logged"
    severity: str  # "none" | "low" | "medium" | "high"
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum — cuts false positives on the loose CREDIT_CARD shape
    regex, which alone would match many non-card digit sequences."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total > 0 and total % 10 == 0


def scan_and_redact_pii(text: str) -> tuple[str, list[str]]:
    """Redact every PII match in `text` in place.

    Returns (redacted_text, sorted list of PII type names found).
    """
    if not text:
        return text, []

    found: set[str] = set()

    for name, regex in _PII_REGEXES.items():

        def _replace(match: re.Match, name: str = name) -> str:
            if name == "CREDIT_CARD":
                digits = re.sub(r"[ -]", "", match.group(0))
                if not (13 <= len(digits) <= 19) or not _luhn_valid(digits):
                    return match.group(0)  # shape matched but failed Luhn — not a real card
            found.add(name)
            return f"[REDACTED:{name}]"

        text = regex.sub(_replace, text)

    return text, sorted(found)


def scan_injection(text: str) -> list[str]:
    """Return the sorted list of injection marker names found in `text`."""
    if not text:
        return []
    return sorted(name for name, regex in _INJECTION_REGEXES.items() if regex.search(text))


def evaluate_response(
    response_texts: list[str], user_input_text: str
) -> tuple[list[str], ResponsibilityFlag]:
    """Run both checks for one request.

    `response_texts` is one string per response choice (usually length 1;
    pass an empty list when there's no model output, e.g. on an upstream
    error, to skip the PII scan and run only the injection scan).
    `user_input_text` is the original user-role message content(s),
    concatenated.

    Returns (redacted_texts, flag). `redacted_texts` is the same
    length/order as `response_texts`, with PII replaced in place wherever
    found; caller is responsible for writing it back into the response body.
    """
    redacted_texts: list[str] = []
    all_pii_types: set[str] = set()

    for text in response_texts:
        redacted, pii_types = scan_and_redact_pii(text)
        redacted_texts.append(redacted)
        all_pii_types.update(pii_types)

    pii_types = sorted(all_pii_types)
    injection_types = scan_injection(user_input_text)

    reasons: list[str] = []
    severity = "none"

    if pii_types:
        reasons.append(f"PII redacted from response: {', '.join(pii_types)}")
        severity = max((_PII_SEVERITY[t] for t in pii_types), key=_SEVERITY_RANK.get)

    if injection_types:
        reasons.append(f"prompt-injection markers found in input: {', '.join(injection_types)}")
        severity = "high"  # injection markers always require human review, regardless of PII severity

    if pii_types:
        action = "edited"
    elif injection_types:
        action = "logged"
    else:
        action = "none"

    flagged = bool(pii_types or injection_types)
    reason = "; ".join(reasons) if reasons else "clean"

    return redacted_texts, ResponsibilityFlag(
        flagged=flagged,
        action=action,
        severity=severity,
        reason=reason,
    )
