"""Pattern config for detectors/responsibility_fast.py.

Kept separate from the detector logic so new signatures can be added by
editing data here, without touching the scanning/redaction code.

PII_PATTERNS: name -> {"pattern": <regex string>, "severity": "low"|"medium"|"high"}
    Scanned against the OUTGOING model response. A match is redacted in place
    as `[REDACTED:<name>]`.

INJECTION_PATTERNS: name -> <regex string>
    Scanned against the ORIGINAL user input. Never redacted — only flagged
    for human review, always at severity "high" regardless of which pattern
    matched (see responsibility_fast.evaluate_response).

All patterns are matched case-insensitively. These are cheap heuristics for
a single-digit-millisecond inline check, not a full PII/DLP engine — expect
some false positives/negatives, and tune the patterns here as real traffic
surfaces gaps.
"""

# Order matters: redaction is applied pattern-by-pattern in dict-insertion
# order, and each pass mutates the text the next pass sees. More specific,
# longer-run patterns (API_KEY, SSN, CREDIT_CARD) must run BEFORE the loose
# generic PHONE pattern, or PHONE will chew a 10-digit chunk out of a credit
# card number or a key's trailing digits before the specific pattern gets to
# see the full, contiguous run.
PII_PATTERNS: dict[str, dict[str, str]] = {
    "EMAIL": {
        "pattern": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "severity": "medium",
    },
    "API_KEY": {
        # Known provider prefixes rather than a generic "long token" match,
        # to keep false positives low: OpenAI/Stripe-style sk-/pk-/rk-,
        # AWS access key IDs, GitHub tokens, Google API keys.
        "pattern": (
            r"\b(?:sk|pk|rk)-[A-Za-z0-9]{20,}\b"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bgh[opusr]_[A-Za-z0-9]{36,}\b"
            r"|\bAIza[0-9A-Za-z\-_]{35}\b"
        ),
        "severity": "high",
    },
    "SSN": {
        "pattern": r"\b\d{3}-\d{2}-\d{4}\b",
        "severity": "high",
    },
    "CREDIT_CARD": {
        # Loose digit-count shape; responsibility_fast validates the actual
        # match against the Luhn checksum before treating it as a real card
        # number, so this pattern is intentionally permissive.
        "pattern": r"\b(?:\d[ -]?){13,19}\b",
        "severity": "high",
    },
    "PHONE": {
        "pattern": r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
        "severity": "medium",
    },
}

INJECTION_PATTERNS: dict[str, str] = {
    "IGNORE_INSTRUCTIONS": r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions?\b",
    "ROLE_OVERRIDE": r"\byou\s+are\s+now\b|\bact\s+as\s+if\s+you\s+are\b|\bpretend\s+(?:to\s+be|you\s+are)\b",
    "SYSTEM_PROMPT_LEAK": r"\b(?:repeat|print|show|reveal|output)\s+(?:your\s+|the\s+)?(?:system\s+prompt|initial\s+instructions?)\b",
    "DEVELOPER_MODE": r"\bdeveloper\s+mode\b|\bDAN\s+mode\b|\bjailbreak\b",
    "DISREGARD_RULES": r"\bdisregard\s+(?:the\s+)?(?:rules|guidelines|policy|policies|instructions)\b",
    "NEW_SYSTEM_PROMPT": r"\bnew\s+system\s+prompt\b|\byour\s+new\s+instructions\s+are\b",
}
