"""Performance / factual-confidence ("hallucination risk") detector.

Two tiers — only the first is allowed anywhere near the request path:

  1. FAST heuristic (inline, synchronous, pure regex): scans the outgoing
     response for specific, unhedged factual claims (numbers, dates,
     quantities) with no hedging language ("I think", "possibly", "not
     sure", ...) anywhere in the text. Cheap enough to run on every request.
     Its only job is deciding whether the response is worth the cost of the
     slow check below — it never edits the response or blocks anything.

  2. SLOW check (async, background task — NEVER inline): this is a second,
     real LLM call, so it must never sit on /v1/chat's response path. It
     runs for responses the fast heuristic flagged, plus a random 10% sample
     of everything else (so unflagged traffic still gets spot-checked). A
     cheaper/faster "judge" model is asked whether the answer contains
     claims that are unsupported, contradictory, or likely fabricated, and
     returns a 0-1 confidence score plus a one-line reason.

This module is pure logic — no groq client, no sqlite. The orchestration
(firing the judge call, writing the score back onto the event, deciding
whether to escalate to review_queue) lives in app/main.py + app/db.py, the
same split used by the other detectors.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Deliberately a small/cheap model — the judge must be cheaper and faster
# than whatever generated the response it's grading, not a bigger model.
JUDGE_MODEL = "openai/gpt-oss-20b"
JUDGE_MAX_TOKENS = 120

SLOW_CHECK_SAMPLE_RATE = 0.10  # random background-check rate for *unflagged* responses
REVIEW_THRESHOLD = 0.6  # judge_score above this -> review_queue

# --- Tier 1: fast heuristic --------------------------------------------------

_HEDGE_PATTERNS = (
    r"\bi think\b",
    r"\bi believe\b",
    r"\bi'?m not (?:sure|certain)\b",
    r"\bnot (?:sure|certain)\b",
    r"\bpossibly\b",
    r"\bperhaps\b",
    r"\bmight be\b",
    r"\bmay be\b",
    r"\bcould be\b",
    r"\bprobably\b",
    r"\bit'?s (?:possible|likely)\b",
    r"\bas far as i know\b",
    r"\bto my knowledge\b",
    r"\bi (?:don'?t|do not) (?:know|recall)\b",
    r"\bapproximately\b",
    r"\bi'?d estimate\b",
    r"\bif i recall correctly\b",
)

_FACTUAL_CLAIM_PATTERNS = (
    r"\b(?:19|20)\d{2}\b",  # years, e.g. 1994, 2026
    r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?%",  # percentages
    r"[$€£]\s?\d[\d,]*(?:\.\d+)?",  # currency amounts
    r"\b\d+(?:\.\d+)?\s?(?:million|billion|thousand|km|kg|miles?|meters?|years?|percent)\b",
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?\b",
)

_HEDGE_REGEX = re.compile("|".join(_HEDGE_PATTERNS), re.IGNORECASE)
_FACTUAL_REGEX = re.compile("|".join(_FACTUAL_CLAIM_PATTERNS), re.IGNORECASE)


@dataclass
class FastHeuristicResult:
    flagged: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def fast_heuristic(text: str) -> FastHeuristicResult:
    """Pure regex, no I/O — safe to run inline on every request.

    Flags text that states specific facts (numbers/dates/quantities)
    without any hedging language anywhere in it: a cheap proxy for
    "confidently asserted, unverified claim" that decides whether the
    (expensive) slow judge check is worth running at all.
    """
    if not text:
        return FastHeuristicResult(flagged=False, reason="empty response")

    factual_matches = [m for m in _FACTUAL_REGEX.findall(text) if m]
    has_hedge = bool(_HEDGE_REGEX.search(text))

    if factual_matches and not has_hedge:
        sample = ", ".join(sorted(set(factual_matches))[:5])
        return FastHeuristicResult(
            flagged=True,
            reason=f"unhedged factual claim(s) with no hedging language nearby: {sample}",
        )

    return FastHeuristicResult(flagged=False, reason="hedged, or no specific factual claims found")


def should_run_slow_check(
    fast_result: FastHeuristicResult, rand: float, sample_rate: float = SLOW_CHECK_SAMPLE_RATE
) -> bool:
    """`rand` is injected (rather than calling random.random() internally)
    so this decision is trivially unit-testable. `sample_rate` defaults to
    this module's constant but is normally threaded in from the resolved
    per-use-case Policy (policy.py) so customer-facing traffic can sample
    more aggressively than batch traffic."""
    return fast_result.flagged or rand < sample_rate


# --- Tier 2: slow judge check (background task only) -------------------------

_JUDGE_PROMPT_TEMPLATE = """You are a fact-checking judge. Given the question and answer below, \
decide whether the answer contains any claims that are unsupported, contradictory, or likely \
fabricated.

Question: {question}

Answer: {answer}

Respond in exactly this format and nothing else:
SCORE: <a number between 0 and 1, where 1 means the answer very likely contains unsupported or \
fabricated claims>
REASON: <one sentence explaining the score>"""

_SCORE_REGEX = re.compile(r"SCORE:\s*([01](?:\.\d+)?)", re.IGNORECASE)
_REASON_REGEX = re.compile(r"REASON:\s*(.+)", re.IGNORECASE)


def build_judge_prompt(question: str, answer: str) -> str:
    return _JUDGE_PROMPT_TEMPLATE.format(question=question or "(no question given)", answer=answer)


# --- Tier 2, grounded variant: retrieval-grounded hallucination checking -----
#
# Used instead of the plausibility-only prompt above when the caller supplies
# `context_documents` on POST /v1/chat (an optional field — no document
# store/retrieval step lives in this service; the caller supplies the source
# material per-request). Same judge model, same background-task mechanism,
# same SCORE:/REASON: response contract — parse_judge_response is reused
# unchanged. Only the question being asked of the judge differs: "is this
# supported by these specific documents" instead of "is this plausible."

_GROUNDED_JUDGE_PROMPT_TEMPLATE = """You are a fact-checking judge verifying an answer against specific \
source material. Given the question, the reference documents, and the answer below, decide whether the \
answer is well-supported by the documents — flag anything unsupported, contradicted, or fabricated \
relative to what the documents actually say.

Question: {question}

Reference documents:
{documents}

Answer: {answer}

Respond in exactly this format and nothing else:
SCORE: <a number between 0 and 1, where 1 means the answer contains claims unsupported or contradicted \
by the reference documents>
REASON: <one sentence explaining the score, citing what the documents do or don't support>"""


def build_grounded_judge_prompt(question: str, answer: str, documents: list[str]) -> str:
    joined_documents = "\n".join(f"- {doc}" for doc in documents) if documents else "(none provided)"
    return _GROUNDED_JUDGE_PROMPT_TEMPLATE.format(
        question=question or "(no question given)",
        documents=joined_documents,
        answer=answer,
    )


def parse_judge_response(text: str) -> tuple[float | None, str]:
    """Parse the judge model's structured reply.

    Never raises — falls back to (None, <diagnostic reason>) on anything
    unexpected, so a malformed judge response can't crash the background
    task it runs in.
    """
    if not text:
        return None, "judge returned an empty response"

    score_match = _SCORE_REGEX.search(text)
    if not score_match:
        return None, f"could not parse judge score from response: {text[:200]!r}"

    try:
        score = float(score_match.group(1))
    except ValueError:
        return None, f"could not parse judge score from response: {text[:200]!r}"

    score = max(0.0, min(1.0, score))  # clamp defensively

    reason_match = _REASON_REGEX.search(text)
    reason = reason_match.group(1).strip() if reason_match else text.strip()[:200]

    return score, reason
