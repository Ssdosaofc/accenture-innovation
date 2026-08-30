"""Pattern config for detectors/bias.py.

Kept separate from the detector logic so new signatures can be added by
editing data here, without touching the scanning code — same rationale as
detectors/responsibility_patterns.py.

BIAS_PATTERNS: name -> regex string. Scanned against the OUTGOING model
    response. A match never gets redacted (unlike PII) — there's no clean
    substring to surgically edit out of a stereotyping sentence — it only
    flags the response and decides whether the slow LLM-judge call
    (detectors/bias.py tier 2) is worth making. The fast tier's job is
    catching the *obvious* cases cheaply; the judge does the real semantic
    work, exactly the same division of labor detectors/performance.py
    already establishes for hallucination risk.

Deliberately narrow: patterns look for blatant, unhedged generalizations
that tie a protected characteristic to a negative or deterministic trait
("all X are Y", "X people can't Y", "X don't belong in Y") — not any mere
mention of a demographic term. This is a keyword/pattern net for the most
obvious cases, not an ML classifier or a validated bias taxonomy — expect
false negatives on subtle bias (coded language, implication, disparate
framing that never uses a blanket "all X" construction), and tune the
patterns here as real traffic surfaces gaps. Treat the exact wordlist below
as a starting point, not a finished list.
"""

BIAS_PATTERNS: dict[str, str] = {
    "BLANKET_GENERALIZATION": (
        r"\ball\s+(?:women|men|muslims?|christians?|jews|blacks?|whites?|asians?|latinos?|"
        r"immigrants?|gay people|disabled people|old people|young people)\s+"
        r"(?:are|can'?t|cannot|don'?t|do not|never|always)\b"
    ),
    "INTRINSIC_INFERIORITY": (
        r"\b(?:women|men|blacks?|whites?|asians?|latinos?|muslims?|immigrants?|"
        r"disabled people|elderly people)\s+"
        r"(?:are\s+(?:naturally|inherently|biologically)\s+(?:less|worse|inferior|unfit)"
        r"|aren'?t\s+(?:as\s+)?(?:smart|capable|qualified|fit))\b"
    ),
    "EXCLUSIONARY_BELONGING": (
        r"\b(?:women|men|immigrants?|muslims?|gay people|disabled people)\s+"
        r"(?:don'?t|do not|shouldn'?t|should not)\s+belong\s+in\b"
    ),
    "SLUR_ADJACENT_STEREOTYPE": (
        r"\btypical\s+(?:woman|man|muslim|jew|black person|immigrant)\s+"
        r"(?:behavior|move|thing)\b"
    ),
}
