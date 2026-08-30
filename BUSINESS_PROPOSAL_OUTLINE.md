# ControlPlane.ai — Business Proposal Outline

This is an outline for the Round 2 Detailed Business Proposal document, built
from the working prototype in `controlplane-checker/`. Each section below
states what it should argue and points at the concrete evidence (code,
README sections, test output) already in this repo to back it.

## 1. Problem framing

- Enterprises deploying LLMs in production face three named risk categories
  simultaneously — **bias, hallucination, privacy leaks** — with no single
  existing tool covering all three at the request/response boundary.
- **Risk tolerance is not uniform.** A customer-facing chatbot answer, an
  internal analyst tool, and an overnight batch summarization job warrant
  wildly different sensitivity to the same underlying signal (e.g. a cost/
  token-count spike, a borderline hallucination score). A single global
  threshold is either too loose for customer-facing traffic or too noisy for
  batch traffic — there is no one number that's right for both.
- **No ground truth, and alert fatigue is real.** Detectors are heuristic
  (regex nets, a cheaper LLM-as-judge) by necessity — there is no labeled
  dataset of "this response was actually biased" to train against day one.
  Left unmanaged, this produces either false-positive fatigue (reviewers stop
  trusting the queue) or false-negative blind spots (real issues ship
  unflagged). The proposal needs a credible story for *earning* trust in the
  system over time, not just deploying it once.
- **Governance needs to be an configurable input, not a hardcoded assumption.**
  Different teams, geographies, and product lines will have different
  compliance postures. A checker that can't be tuned per use case doesn't fit
  how real organizations are actually structured.

## 2. Solution design summary

- **Fast-path / slow-path architecture.** Every check that could plausibly
  change or block the live response runs inline, synchronously, in
  single-digit milliseconds (regex, arithmetic, SQL only) — see
  `README.md` §1 for the full flow diagram. Every check that needs a second
  model call (hallucination judge, bias judge) is forced onto a background
  task that runs strictly *after* the response has already been returned —
  by construction, it can never add latency or become a bottleneck to the
  live response path. This is the architectural answer to "governance
  shouldn't cost you your response-time SLA."
- **Four detector categories, one consolidated verdict.** Cost/anomaly, PII +
  prompt-injection ("responsibility"), hallucination-risk ("performance"),
  and bias/fairness all funnel into a single `decision_engine.py` that
  produces one of `pass` / `edited` / `flagged` / `blocked` — the customer
  sees one clear signal, not four disconnected dashboards.
- **Configurable policy / risk-tiering layer.** `config/policies.json` +
  `policy.py` let every threshold above resolve per-request from a named
  tier (`customer_facing` / `internal` / `batch`) selected by a `use_case`
  field on the request. This is the direct, working answer to "risk
  tolerance isn't uniform" — the same cost deviation passes quietly under
  `batch`'s lenient tolerance and gets flagged under `customer_facing`'s
  strict one (proven live in `test_decision_engine.py`'s policy-tiering
  scenarios).
- **Feedback loop visibility.** `GET /feedback/stats` aggregates every
  reviewer's confirm/dismiss decision from the review queue into a
  per-detector confirmed-rate — the first step toward "which detectors are
  actually trustworthy," surfaced to a stakeholder without manual SQL.
- **Human-in-the-loop by design, not full automation.** Nothing in the
  system silently deletes or rewrites content beyond well-defined PII
  redaction; ambiguous findings go to a review queue for a person to confirm
  or dismiss. This is a deliberate trust-building choice for a first
  deployment, not a technical limitation.

## 3. Target users (the three risk-tier personas)

1. **Customer-facing tier** — support chatbots, public-facing assistants.
   Lowest risk tolerance (`flag_z_score=2.0`, `bias_review_threshold=0.3`,
   25% slow-check sampling) — false negatives here are reputational risk in
   front of real customers, so the system samples aggressively and flags
   early.
2. **Internal tier** (default) — employee-facing tools, internal copilots.
   Moderate tolerance (`flag_z_score=2.5`) — real productivity tooling that
   shouldn't be swamped with noise, but still needs a safety net.
3. **Batch tier** — offline summarization, bulk classification, non-interactive
   jobs. Highest tolerance (`flag_z_score=4.0`, 2% sampling) — cost and
   judge-call overhead matter more than per-item scrutiny at this volume,
   and a human is not waiting on any single item's result.

## 4. Business case

- **Judge-model cost economics.** The hallucination and bias judges are real,
  billed LLM calls — this is not free governance. The system controls that
  cost directly via each tier's `slow_check_sample_rate` (2%–25%) rather
  than paying full judge-model cost on every single request; batch workloads
  in particular get governance at a fraction of the cost a naive
  "judge everything" design would incur.
- **Alert-fatigue tradeoff.** The tiering system is the mechanism that keeps
  the review queue actionable: a `batch` job generating thousands of
  responses overnight would flood a single global threshold's review queue
  with mostly-benign findings; per-tier thresholds keep the signal-to-noise
  ratio usable for the humans actually staffing the queue.
- **Latency cost is small and measured, not asserted.** `scripts/latency_test.py`
  empirically shows the fast-path's real per-request overhead (see the repo's
  documented, honest finding: overhead is close to target at low concurrency,
  and grows under heavy concurrent write load to SQLite — a known, named
  limitation, not a hidden one).
- **Feedback loop reduces the ongoing tuning cost.** Instead of a governance
  team guessing at threshold values indefinitely, `GET /feedback/stats` gives
  a concrete, cheap-to-compute confirmed-rate per detector to guide where
  tuning effort is actually worth spending, and `GET /policy/suggestions`
  turns that into a concrete recommendation (never an automatic change) —
  cutting the time between "this detector is noisy" and "here's the exact
  threshold edit to make."
- **Audit trail supports the compliance conversation directly.** A
  stakeholder asking "how do we know this log wasn't edited after an
  incident" gets a concrete answer — `GET /audit/verify` — rather than "we
  trust the database." Framed honestly: tamper-*evident*, not tamper-*proof*
  (see Risks below); still a meaningfully stronger starting position than an
  unverified SQLite log.

## 5. Phased roadmap

**Shipped this round:**
- Cost-anomaly + retry-loop detection
- PII redaction + prompt-injection flagging
- Two-tier hallucination-risk detector (fast heuristic + async LLM judge)
- Two-tier bias/fairness detector (same fast/slow pattern)
- Consolidated decision engine (BLOCK > EDIT > FLAG > PASS)
- Configurable policy / risk-tiering layer (3 named use-case tiers)
- Review queue + resolution workflow
- Feedback-loop visibility (`GET /feedback/stats`, per-detector confirmed-rate)
- Tamper-evident audit trail (`audit_log`, hash-chained; `GET /audit/verify`)
- Automated threshold tuning, suggestion-only with human sign-off
  (`GET /policy/suggestions` — never auto-applies a change)
- Multi-turn/session risk detector — escalates a message based on
  accumulated non-pass verdicts earlier in its caller-supplied `session_id`,
  the first check in the system that looks across a conversation's arc
  rather than one request in isolation
- Retrieval-grounded hallucination checking — an optional
  `context_documents` field on `POST /v1/chat` forces the performance judge
  to check the answer against caller-supplied source material instead of
  judging plausibility alone
- Live dashboard (events, summary, review queue, feedback stats, audit-chain
  badge, policy suggestions, per-event session-risk flag line)
- Empirical latency benchmark proving the fast-path/slow-path separation

**Explicitly deferred (named, not silently dropped):**
- **A real retrieval pipeline for grounding.** What shipped this round makes
  grounding *possible* — the judge will check an answer against whatever
  documents the caller hands it — but there's no document store, search, or
  ranking behind `context_documents` itself. A caller integrating this today
  still has to do their own retrieval and pass the results in; a managed
  retrieval layer (index a knowledge base, fetch relevant passages
  automatically per-request) remains a real, separate build, not something
  this round's `context_documents` field quietly already does.
- **Session-risk severity weighting.** The shipped detector treats every
  prior non-pass verdict in a session identically — 3 unrelated false
  positives escalate a session exactly the same as 3 genuine, worsening
  jailbreak attempts. Weighting by severity or by the review queue's actual
  confirmed/false-positive outcome for those prior findings (once enough
  session-level resolution data exists) is the natural next refinement, not
  a from-scratch feature.

## 6. Risks + mitigations

Starting point: the "Known limitations" section of the top-level
`README.md`, which already documents these honestly rather than glossing
over them. Summarized for a business audience:

| Risk | Mitigation status |
| --- | --- |
| Single-node SQLite doesn't horizontally scale; write contention shows up under heavy concurrent load | Documented, measured via `scripts/latency_test.py`; a production deployment would move to a proper OLTP store, which doesn't change the detector logic |
| No authentication on any endpoint today | Explicitly scoped out of this prototype; standard auth middleware is additive, not an architecture change |
| Judge-model calls are billed on every sampled request, with no budget cap or circuit breaker | Sample-rate is already policy-tiered to bound cost; a hard budget cap/circuit breaker is a near-term addition, not a redesign |
| Regex-based detectors (PII, injection, bias) will miss novel phrasing and can false-positive | Deliberately narrow, documented as a starting point in the pattern files themselves; `GET /feedback/stats` gives the concrete data needed to know where to expand coverage |
| Policy tiers and their threshold values are hand-picked, not learned from data | `GET /policy/suggestions` (shipped) turns confirm/dismiss data into a concrete, human-reviewed recommendation — still requires a person to apply it, by design |
| The audit trail is tamper-evident, not tamper-proof — someone with direct file access could rewrite the chain consistently from a point forward | Explicitly scoped and stated as a limitation, not oversold; a production deployment would add an external anchor (e.g. periodic hash publication) or move to write-once storage |
| Session-risk escalation treats every prior non-pass verdict identically — no severity weighting | Stated explicitly as a known gap, not hidden; it's still the first check that sees across a conversation's arc at all, and confirmed/false-positive resolution data (once it exists at the session level) is the natural input for weighting later |
| Retrieval grounding is only as accurate as the documents the caller supplies — there's no retrieval/search behind `context_documents` | Scoped deliberately: this round proves the grounding *mechanism* works end-to-end (judge checks the answer against given material, forces the check to run, records that it did); a managed retrieval layer is a distinct, larger build named explicitly in the roadmap, not conflated with what shipped |
