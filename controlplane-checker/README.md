# controlplane-checker

A FastAPI pass-through service that accepts OpenAI-style chat completion
requests, forwards them to [Groq](https://groq.com) (free-tier hosting for open-weight
models — GPT-OSS, Qwen, and others; the exact lineup rotates, see
`GET https://api.groq.com/openai/v1/models`), returns the completion
unmodified, and logs every request/response pair to a local SQLite database.

This is the baseline every later feature (rate limiting, detection logic,
policy checks, etc.) attaches to. No transformation or detection logic yet —
just requests flowing through and logged.

Groq's chat completions API is already OpenAI-compatible, so the request is
forwarded with almost no reshaping.

## Endpoint

### `POST /v1/chat`

Accepts a JSON body matching OpenAI's chat completions schema:

```json
{
  "model": "openai/gpt-oss-120b",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Say hello in one sentence."}
  ],
  "max_tokens": 256
}
```

Notes:

- `model` defaults to `openai/gpt-oss-120b` if omitted. Groq's model lineup
  rotates — `GET https://api.groq.com/openai/v1/models` (with your API key)
  is the authoritative list of what's actually available on your account;
  `detectors/cost.py`'s `PRICE_TABLE_PER_MTOK` should be kept in sync with
  whatever you pick.
- `max_tokens` defaults to `1024` if omitted.
- `temperature`, `top_p`, `stop`, `tools`, `tool_choice`, `response_format`,
  and other standard OpenAI chat-completion fields are forwarded as-is if
  present.
- `use_case` (optional, not part of the OpenAI schema — a ControlPlane
  extension): one of `"customer_facing"`, `"internal"`, `"batch"`. Selects
  which policy (`config/policies.json`) governs this request's thresholds —
  see Policy / risk tiering below. Omitted or unrecognized values fall back
  to `"internal"` silently, never a 4xx.
- `session_id` (optional, ControlPlane extension): caller-supplied string
  grouping requests into a conversation, for the session-risk detector (see
  below). Omitted → this message is its own single-message session, same as
  today's behavior.
- `context_documents` (optional, ControlPlane extension): a list of strings
  the performance judge should check the answer against, instead of judging
  plausibility alone — see Performance detector's "Retrieval-grounded mode"
  below. No document store or retrieval step lives in this service; the
  caller supplies the material per-request. Omitted → today's ungrounded
  judge behavior, unchanged.
- The response returned to the caller is the **raw Groq chat completions
  response**, except that any PII detected in it is redacted in place (see
  Responsibility detector below), plus a `controlplane` metadata block is
  attached (see Decision engine below). An extreme cost anomaly replaces the
  response entirely with a `429` error — see Decision engine.

### `GET /events`

Paginated, most-recent-first page of logged events, each carrying every
detector's flags/scores plus the raw request/response bodies — the same
enriched shape `GET /review-queue` uses, but for the events table directly.
Query params: `limit` (default `50`, max `200`), `offset` (default `0`).

```json
{
  "total": 132,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "id": 132, "timestamp": "...", "latency_ms": 812.4, "model": "...",
      "request_id": "...", "status": "success", "error": null,
      "input": { "...": "full request body" },
      "output": { "...": "full (redacted) response body" },
      "cost": {"usd": 0.0001, "flagged": false, "reason": "within baseline", "z_score": 0.4},
      "responsibility": {"flagged": false, "action": "none", "severity": "none", "reason": "clean"},
      "performance": {"judge_score": null, "judge_reason": null, "sampled": false, "grounded": false},
      "bias": {"fast_flagged": false, "fast_reason": "no bias pattern matched", "judge_score": null, "judge_reason": null, "sampled": false},
      "session": {"session_id": null, "flagged": false, "reason": "no session_id supplied"},
      "use_case": "internal",
      "policy": {"block_z_score": 4.0, "flag_z_score": 2.5, "review_threshold": 0.6},
      "controlplane": {"action": "pass", "flags": []}
    }
  ]
}
```

`use_case` and `policy` reflect what was actually resolved and applied **at
the time this event was decided** (persisted on the row, not looked up live
from the current `config/policies.json`) — so a later edit to the policy
file doesn't retroactively change how an old event's decision reads. Events
logged before the policy layer existed show `use_case: "unknown"` rather
than a tier name, since they were never actually governed by one.

### `GET /summary`

Aggregates for the dashboard's summary strip:

```json
{
  "requests_per_minute": 6,
  "avg_latency_ms": 173.33,
  "total_cost_usd_last_hour": 0.000201,
  "pct_flagged_last_hour": 60.0,
  "pending_review_count": 1
}
```

`requests_per_minute` is a live rate over the last 60 seconds.
`avg_latency_ms` / `total_cost_usd_last_hour` / `pct_flagged_last_hour` use a
consistent last-1-hour window. `pct_flagged_last_hour` is the share of
decided requests whose `controlplane_action` was anything other than a clean
`"pass"` — i.e. the combined edited+flagged+blocked ("yellow + red") rate.
`pending_review_count` is a live queue depth, not windowed.

### `GET /`

Serves a single-page dashboard (`static/dashboard.html` — plain HTML/CSS/JS,
no build step, no framework, no external dependencies) that polls `/summary`,
`/events`, and `/review-queue` every 2 seconds:

- **Summary strip** — five stat tiles for the metrics above.
- **Events table** — last 50 events, color-coded by `controlplane.action`
  (green = pass, yellow = flagged/edited, red = blocked, gray = error/no
  verdict). Click a row to expand it in place and see the full input/output
  JSON and every detector's flags/scores.
- **Review queue panel** — pending items with a note field and
  Confirm/Dismiss buttons that call `POST /review-queue/{id}/resolve` with
  `resolution: "confirmed_issue"` / `"false_positive"` respectively.
- **Feedback loop panel** — polls `GET /feedback/stats`; one stat tile per
  detector (`cost`/`responsibility`/`performance`/`bias`/`session`) showing
  its all-time confirmed-rate and sample size, plus a warning banner for any
  detector whose false-positive rate looks high on a trustworthy sample
  size. The same panel's header shows an audit-chain badge (polls
  `GET /audit/verify` — green "chain valid (N)" or red "chain broken at
  #N"), and below the stat tiles, a read-only list of policy-tuning
  suggestions (polls `GET /policy/suggestions`) when any exist.

No websockets — deliberately polling-based for the prototype. In-progress
note text is preserved across polls (tracked separately from the rendered
DOM) so typing isn't interrupted by the next refresh.

### `GET /health`

Simple liveness check.

## Logging

Every call to `/v1/chat` — success or failure — writes one row to the
`events` table in a local SQLite database:

| Column | Description |
| --- | --- |
| `timestamp` | UTC ISO-8601 timestamp of the request |
| `latency_ms` | Round-trip latency to Groq, in milliseconds |
| `model` | Model used |
| `request_id` | Groq completion ID |
| `input_tokens` / `output_tokens` | Token usage from the response |
| `request_body` | Full original request JSON |
| `response_body` | Full Groq response JSON (`NULL` on error) |
| `status` | `success` or `error` |
| `error` | Error message, if any |
| `message_hash` | SHA-256 of the request's role+content pairs (retry-loop detection key) |
| `cost_usd` | Computed cost of the request, from `detectors/cost.py`'s price table |
| `cost_flagged` | `1` if the cost detector flagged this request, else `0` |
| `cost_flag_reason` | Human-readable reason(s) for the flag, or `"within baseline"` |
| `cost_flag_z_score` | Max of the token-count and cost z-scores vs. the rolling baseline |
| `responsibility_flagged` | `1` if the responsibility detector flagged this request, else `0` |
| `responsibility_action` | `"none"`, `"edited"` (PII redacted), or `"logged"` (injection marker, not blocked) |
| `responsibility_severity` | `"none"`, `"medium"`, or `"high"` |
| `responsibility_reason` | Human-readable reason(s) for the flag, or `"clean"` |
| `judge_score` | `NULL` until the performance detector's slow (async) check runs; then 0-1 hallucination-risk score |
| `judge_reason` | One-line reason from the judge model, or a diagnostic if the judge call/parse failed |
| `sampled` | `1` if the slow performance check ran for this event (flagged or random-sampled), else `0` |
| `bias_flagged` | `1` if the bias detector's fast tier matched a pattern, else `0` |
| `bias_fast_reason` | Human-readable reason from the fast tier, e.g. matched pattern name(s) |
| `bias_judge_score` | `NULL` until the bias detector's slow (async) check runs; then 0-1 stereotyping-risk score |
| `bias_judge_reason` | One-line reason from the judge model, or a diagnostic if the judge call/parse failed |
| `bias_sampled` | `1` if the slow bias check ran for this event (fast-tier match, or random-sampled), else `0` |
| `session_id` | Caller-supplied string grouping this event into a conversation (`POST /v1/chat`'s optional `session_id` field), or `NULL` if not supplied |
| `session_flagged` | `1` if the session-risk detector escalated THIS message due to accumulated non-pass verdicts earlier in its session, else `0` |
| `session_flag_reason` | Human-readable reason, e.g. how many prior non-pass verdicts triggered the escalation |
| `performance_grounded` | `1` if the slow performance check ran against caller-supplied `context_documents` (retrieval-grounded prompt) rather than the default plausibility-only prompt, else `0` |
| `use_case` | The resolved policy tier applied to this request (`"customer_facing"` / `"internal"` / `"batch"`), or `NULL` for events logged before the policy layer existed |
| `policy_block_z_score` / `policy_flag_z_score` / `policy_review_threshold` | The exact threshold values in force when this event was decided — persisted on the row, not looked up live from the current policy file, so old events stay auditable even if `config/policies.json` changes later |
| `controlplane_action` | The decision engine's final verdict: `"pass"`, `"edited"`, `"flagged"`, or `"blocked"` |
| `controlplane_flags` | JSON array of every fast-tier signal that fired, e.g. `["cost_anomaly","injection_marker","bias_flag","session_risk"]` |

High-severity findings — an injection marker from the responsibility check, a
`judge_score` above the resolved policy's `review_threshold` from the
performance check, a `bias_judge_score` above the resolved policy's
`bias_review_threshold` from the bias check, or a session-risk escalation —
are also inserted into a `review_queue` table for a human to look at later.
(A fast-tier bias match alone does **not** escalate — only the slow judge
score crossing the threshold does; see the bias detector section below for
why. Session risk DOES escalate immediately on its own fast-tier signal,
same as an injection marker — see the session risk detector section below.)

| Column | Description |
| --- | --- |
| `event_id` | FK into `events.id` |
| `detector` | `"responsibility"`, `"performance"`, `"bias"`, or `"session"` (`"cost"` never appears here — an extreme cost anomaly blocks immediately instead of queuing for review) |
| `severity` | Currently always `"high"` |
| `reason` | The detector's reason string at the time of the finding |
| `created_at` | UTC ISO-8601 timestamp |
| `resolved` | `0`/`1` — set via `POST /review-queue/{id}/resolve` (see below) |
| `resolution` | `NULL` until resolved; then `"confirmed_issue"` or `"false_positive"` |
| `note` | Free-text note supplied at resolution time |
| `resolved_at` | UTC ISO-8601 timestamp of resolution, `NULL` until resolved |

Every `"confirmed_issue"` resolution is also copied into a `confirmed_issues`
table — the feedback-loop stub described below.

## Audit trail

`events`/`review_queue` rows are legitimately updated multiple times over an
event's lifecycle (verdict, judge score, resolution), so "tamper-evident"
can't mean "the row never changes." Instead, every one of those 5 write
sites (`_log_event_sync`, `_update_controlplane_verdict_sync`,
`_update_judge_result_sync`, `_update_bias_judge_result_sync`,
`_resolve_review_queue_item_sync` in `app/db.py`) also appends one entry to
an append-only `audit_log` table, hash-chained to the previous entry —
computed by `audit.compute_row_hash` and inserted **in the same transaction**
as the underlying write, so the audit trail and the row it describes can
never drift apart.

| Column | Description |
| --- | --- |
| `id` | Primary key, also the chain's implicit sequence number |
| `timestamp` | UTC ISO-8601 timestamp of this entry |
| `action` | `"event_logged"`, `"verdict_decided"`, `"judge_result"`, `"bias_judge_result"`, or `"review_resolved"` |
| `event_id` | FK into `events.id` (or the resolved review's originating event, for `"review_resolved"`) |
| `payload` | Small JSON snapshot of what changed — not the full row, just the fields relevant to that action (e.g. `{"action": "flagged", "flags": [...]}` for `"verdict_decided"`) |
| `prev_hash` | The previous entry's `row_hash`, or 64 zeros (`GENESIS_HASH`) for the first entry ever |
| `row_hash` | `SHA-256(prev_hash + action + event_id + timestamp + payload)`, canonical JSON, computed by `audit.compute_row_hash` |

### `GET /audit/verify`

Recomputes every `audit_log` row's hash from scratch (not just spot-checking
the tip) and confirms each row's stored `prev_hash` matches the previous
row's stored `row_hash`:

```json
{"valid": true, "checked": 214, "first_break_id": null, "reason": null}
```

If any row was edited or deleted after being written — in `events`,
`review_queue`, or `audit_log` itself — this reports exactly where:

```json
{
  "valid": false,
  "checked": 214,
  "first_break_id": 57,
  "reason": "row 57's stored row_hash does not match its recomputed hash — the row was edited after being written"
}
```

**What this does and doesn't prove:** it proves *that* something in the
chain changed after the fact, and *where*. It does not prevent someone with
direct file access to `controlplane.db` from rewriting the chain
consistently forward from a point (recomputing every hash after their edit)
— there's no external anchor, signing key, or write-once storage backing
this. That's an explicit, stated limitation (see the top-level README), not
an oversight: tamper-*evident*, not tamper-*proof*.

## Review queue endpoints

### `GET /review-queue`

Returns pending (`resolved = 0`) items, each joined with its **full**
originating event — the raw request/response bodies plus every detector's
flags and scores — sorted by severity (`high` first) then recency (most
recent first within a severity tier):

```json
{
  "count": 1,
  "items": [
    {
      "review_id": 2,
      "event_id": 2,
      "detector": "responsibility",
      "severity": "high",
      "reason": "prompt-injection markers found in input: IGNORE_INSTRUCTIONS",
      "created_at": "2026-08-16T14:48:24.11Z",
      "resolved": false,
      "resolution": null,
      "note": null,
      "resolved_at": null,
      "event": {
        "timestamp": "...", "model": "...", "request_id": "...", "status": "success",
        "input": { "...": "the full original request body" },
        "output": { "...": "the full (redacted) response body" },
        "cost": {"usd": 0.0001, "flagged": false, "reason": "within baseline", "z_score": 0.4},
        "responsibility": {"flagged": true, "action": "logged", "severity": "high", "reason": "..."},
        "performance": {"judge_score": null, "judge_reason": null, "sampled": false, "grounded": false},
        "bias": {"fast_flagged": false, "fast_reason": "no bias pattern matched", "judge_score": null, "judge_reason": null, "sampled": false},
        "session": {"session_id": null, "flagged": false, "reason": "no session_id supplied"},
        "use_case": "internal",
        "policy": {"block_z_score": 4.0, "flag_z_score": 2.5, "review_threshold": 0.6},
        "controlplane": {"action": "flagged", "flags": ["injection_marker"]}
      }
    }
  ]
}
```

### `POST /review-queue/{id}/resolve`

```json
{"resolution": "confirmed_issue", "note": "genuine jailbreak attempt, escalate"}
```

`resolution` is `"confirmed_issue"` or `"false_positive"`; `note` is
free-text. Returns `404` for an unknown `id`. Marks the item resolved
(`resolved=1`) and stores `resolution`/`note`/`resolved_at`. Re-resolving an
already-`"confirmed_issue"` item with the same resolution is a no-op on
`confirmed_issues` (won't insert a duplicate row); flipping a prior
`"false_positive"` to `"confirmed_issue"` does insert one.

### Feedback-loop stub

A `"confirmed_issue"` resolution is copied into a `confirmed_issues` table:

| Column | Description |
| --- | --- |
| `review_queue_id` | FK into `review_queue.id` |
| `event_id` | FK into `events.id` |
| `detector` | Which detector raised the original finding |
| `severity` | Severity at the time of the finding |
| `reason` | The detector's reason string |
| `note` | The human reviewer's note |
| `created_at` | UTC ISO-8601 timestamp of confirmation |

This is the data you'd eventually mine to tune detector thresholds — e.g. "PHONE
redactions get marked false_positive 80% of the time, loosen that pattern" or
"performance judge_score > 0.6 is confirmed 90% of the time, the threshold is
well-calibrated." For the prototype it's pure storage — no retraining or
threshold-adjustment logic yet.

### `GET /feedback/stats`

Reads back `review_queue` resolutions (not `confirmed_issues` — that table
only ever receives `"confirmed_issue"` rows, so `"false_positive"`
resolutions are only visible via `review_queue.resolution` directly),
grouped by detector, into a confirmed/false-positive rate per detector —
closing the visibility half of the feedback loop (the write side has existed
since the `confirmed_issues` table was added; nothing read it back until
now):

```json
{
  "detectors": {
    "cost":           {"total_resolved": 0,  "confirmed_count": 0,  "false_positive_count": 0,  "confirmed_rate": null},
    "responsibility": {"total_resolved": 12, "confirmed_count": 9,  "false_positive_count": 3,  "confirmed_rate": 0.75},
    "performance":    {"total_resolved": 8,  "confirmed_count": 2,  "false_positive_count": 6,  "confirmed_rate": 0.25},
    "bias":           {"total_resolved": 3,  "confirmed_count": 3,  "false_positive_count": 0,  "confirmed_rate": 1.0},
    "session":        {"total_resolved": 1,  "confirmed_count": 1,  "false_positive_count": 0,  "confirmed_rate": 1.0}
  },
  "warnings": []
}
```

All five detector categories are always present, even `cost` (which never
escalates to `review_queue` today — an extreme cost anomaly blocks
immediately instead of queuing — included with zeros for shape-consistency
rather than omitted). `confirmed_rate` is `null` until `total_resolved` is
nonzero (same divide-by-zero guard style as `pct_flagged_last_hour` in
`GET /summary`). `warnings` lists detectors where `total_resolved >= 5` (a
sample size worth trusting) **and** more than half of resolutions were
`"false_positive"` — a plain visibility signal that a detector may be
miscalibrated, computed on read, never acted on automatically. The dashboard
renders this as a stat-tile row plus a warning banner.

**Migration note:** the `resolution`/`note`/`resolved_at` columns and the
`confirmed_issues` table are added by `init_db()` on startup — including to
a `controlplane.db` created by an earlier version of this service, via
`ALTER TABLE ... ADD COLUMN` with the duplicate-column error swallowed
(SQLite has no `ADD COLUMN IF NOT EXISTS`). Safe to run repeatedly.

### `GET /policy/suggestions`

Goes one step further than `/feedback/stats`: for the two detectors whose
review-queue escalation is gated by a single tunable float threshold
(`performance.review_threshold`, `bias.bias_review_threshold`), suggests
*raising* a use case's threshold when its false-positive rate is high on a
trustworthy sample:

```json
{
  "suggestions": [
    {
      "use_case": "internal",
      "detector": "performance",
      "field": "review_threshold",
      "current_value": 0.6,
      "suggested_value": 0.7,
      "false_positive_rate": 0.75,
      "n": 8,
      "reason": "6 of 8 resolved performance review-queue items under use_case='internal' were false_positive (75%) — raising review_threshold should reduce false escalations. No suggestions to lower a threshold are made: review_queue only contains items that were already flagged, so this data has no visibility into missed (false-negative) cases."
    }
  ],
  "note": "Suggestions only — edit config/policies.json and restart the server to apply any of these. Nothing is ever changed automatically."
}
```

**Scoping, stated explicitly:** `cost` never escalates to `review_queue` (an
extreme anomaly blocks immediately — no human resolution ever exists to
learn from); `responsibility` is binary regex-triggered (PII redaction,
injection flag) with no single numeric threshold to move; and `session` is
gated by a plain count threshold (`SESSION_RISK_THRESHOLD`), not a
0-1 float like `review_threshold`/`bias_review_threshold` — none of the
three ever appears here, even when they have real review-queue findings (see
`test_audit_and_tuning.py`'s Test 4, which proves this by generating real
`responsibility` false-positive resolutions and asserting they still don't
produce a suggestion). The trigger condition mirrors `/feedback/stats`'s
warning bar exactly (`total_resolved >= 5`, `false_positive_rate > 0.5`), and
the suggested value is always `min(current + 0.1, 0.95)` — a fixed,
conservative step, never a jump to an extreme value.

**This never suggests lowering a threshold.** `review_queue` only contains
items that were already flagged, so this data has zero visibility into false
negatives (things that should have been flagged but weren't). A high
false-positive rate is unambiguous evidence a threshold is too loose in the
"escalates too much" direction; a low false-positive rate is not evidence a
threshold could safely be tightened, since nothing here can see what got
missed.

Applying a suggestion is manual: edit the value in `config/policies.json`
and restart the server. There is no apply-from-the-API path, by design —
this is observability that informs a human decision, not an automated
tuning loop.

## Cost anomaly detector

`detectors/cost.py` runs synchronously in the `/v1/chat` pipeline, after the
Groq response comes back but before the event is logged and the response
returned. It's pure SQLite aggregation — no external calls — so it adds
negligible latency. It does **not** alter the response; anomalies are logged
and printed to the console only.

Three checks, per model:

1. **Cost** — `cost_usd` is computed from a hardcoded USD-per-1M-token price
   table (`PRICE_TABLE_PER_MTOK`). Unknown models fall back to a conservative
   default price rather than silently skipping cost tracking.
2. **Rolling baseline** — mean + population stddev of tokens-per-request and
   cost-per-request over the last 100 successful events for that model
   (`events(model, timestamp)` is indexed). A request is flagged if its token
   count or cost is more than 2.5 standard deviations above the mean. Needs
   at least 5 prior events for a given model before it will flag on z-score,
   to avoid false positives from a thin baseline.
3. **Retry-loop detection** — the request's messages are hashed
   (`events(message_hash, timestamp)` is indexed); if the same hash appears
   more than 3 times in the last 5 minutes, it's flagged regardless of cost.
   Note: this is a signal on `cost_flag` itself, visible in
   `cost_flagged`/`cost_flag_reason` — it does **not**, by itself, move
   `cost_flag_z_score`, so it does not drive the decision engine's cost-based
   FLAG/BLOCK rules below (those are strictly z-score-gated). A burst of
   identical *but ordinarily-priced* requests is therefore visible in the
   logs as a retry loop while the live response keeps flowing normally.
4. **Numerical-stability guard (`MIN_MEANINGFUL_STD`)** — when a model's
   baseline happens to have near-zero variance, floating-point rounding
   noise in the stddev computation can otherwise leave it as a tiny nonzero
   epsilon instead of exact `0.0`, and dividing by that produces an
   astronomically large, meaningless z-score. Both `token_std` and
   `cost_std` are floored at `1e-6` — well above any float-noise scale, well
   below any real-world stddev — before being used as a divisor.

When a request is flagged, you'll see a line like this on stdout:

```
[cost-anomaly] model=openai/gpt-oss-120b request_id=chatcmpl-abc123 z_score=3.11 cost_usd=$0.004920 reason=token_count=8200 is 3.11 stddev above baseline mean 512 (n=87)
```

## Responsibility detector

`detectors/responsibility_fast.py` runs synchronously, inline, in the
`/v1/chat` pipeline — after the Groq response comes back, before it's
returned to the caller. It's pure regex/string work (no model calls, no I/O)
and completes in low single-digit milliseconds even on large responses.
Patterns live in `detectors/responsibility_patterns.py`, kept separate from
the detection logic so new signatures can be added without touching code.

Two independent checks:

1. **PII scan on the OUTGOING response.** Emails, phone numbers, SSN-shaped
   numbers, credit-card-shaped numbers (validated against the Luhn checksum
   to cut false positives before redacting), and known API-key prefixes
   (OpenAI/Stripe-style `sk-`/`pk-`, AWS `AKIA...`, GitHub `ghp_...`, Google
   `AIza...`). Every match is **redacted in place** as `[REDACTED:TYPE]`
   before the response is returned to the caller — `responsibility_flag.action
   = "edited"`.
2. **Prompt-injection marker scan on the ORIGINAL user input.** Phrases like
   "ignore previous instructions", "you are now ...", "reveal your system
   prompt", "developer mode" / "jailbreak", etc. This check never blocks or
   edits anything — it only flags the event for later human review:
   `responsibility_flag.action = "logged"`, `severity = "high"`.

If both fire on the same request, `action` is `"edited"` (redaction still
happens) and `severity` is forced to `"high"` (an injection marker always
warrants review, regardless of PII severity).

When a request is flagged, you'll see a line like this on stdout:

```
[responsibility] model=openai/gpt-oss-120b request_id=chatcmpl-abc123 action=edited severity=high reason=PII redacted from response: EMAIL, SSN; prompt-injection markers found in input: IGNORE_INSTRUCTIONS
```

These are cheap heuristics for an inline check, not a full PII/DLP engine —
expect some false positives/negatives on edge cases, and extend the patterns
in `detectors/responsibility_patterns.py` as real traffic surfaces gaps.

## Performance / hallucination-risk detector

`detectors/performance.py` is the most expensive detector — its slow tier
makes a real second LLM call — so unlike the other two, **only its fast tier
is allowed anywhere near the request path.** The slow tier always runs as a
FastAPI background task, scheduled *after* `/v1/chat`'s response has already
been returned to the caller. No matter how slow or unreliable the judge call
is, it can never add latency to the response the caller sees.

**Tier 1 — FAST heuristic (inline, synchronous, pure regex).** Scans the
final (already-redacted) response text for specific, unhedged factual
claims — years, percentages, currency amounts, quantities with units, dates —
with no hedging language ("I think", "possibly", "not sure", "probably", ...)
anywhere in the text. This never touches the response; its only job is
deciding whether the expensive slow check is worth running.

**Tier 2 — SLOW check (async, background task, never inline).** Runs when
tier 1 flags the response, **or** for a random 10% sample of everything else
(`SLOW_CHECK_SAMPLE_RATE`) — so unflagged traffic still gets spot-checked. A
cheaper/faster **judge model** (`openai/gpt-oss-20b`, vs. the default
generation model `openai/gpt-oss-120b`) is asked:

> Given this question and this answer, does the answer contain any claims
> that are unsupported, contradictory, or likely fabricated? Respond with a
> confidence score 0-1 and a one-line reason.

The parsed `(score, reason)` is written back onto the already-logged event
row (`judge_score`, `judge_reason`, `sampled=1`). A score above
`REVIEW_THRESHOLD` (`0.6`) pushes the event into `review_queue`, same as an
injection-marker finding from the responsibility detector, and prints:

```
[performance] event_id=42 judge_score=0.92 reason=The cost and date are both fabricated and inconsistent with known facts.
```

If the judge call itself fails (rate limit, network error, malformed
response), the background task never raises — it records
`judge_score=NULL` with a diagnostic `judge_reason` and moves on.

### Retrieval-grounded mode

When a request supplies `context_documents` (a list of strings, see
`POST /v1/chat` above), the slow check uses a different judge prompt — one
that asks whether the answer is well-supported by those specific documents,
not just "is this plausible" — and, unlike the default sampling behavior
above, **runs unconditionally**: neither the fast heuristic's outcome nor
the random sample roll can skip it, since a caller who explicitly supplied
grounding material has opted into stronger verification. Built by
`detectors.performance.build_grounded_judge_prompt(question, answer,
documents)`; the response is parsed with the exact same
`parse_judge_response` used by the ungrounded prompt (identical
`SCORE:`/`REASON:` contract) and escalates to `review_queue` under the same
`review_threshold` rule. The event's `performance.grounded` field records
which prompt variant actually ran.

**What this is and isn't:** there is no document store or retrieval step in
this service — the caller supplies the source material per-request. This is
"grounding against what you give me," not a RAG pipeline that fetches its
own sources; a real production deployment adding retrieval would sit
upstream of this field, feeding it the retrieved passages.

## Bias / fairness detector

`detectors/bias.py` deliberately mirrors the performance detector's
fast/slow split — proof the two-tier architecture generalizes to a new risk
category, not a one-off bolt-on. Patterns live in
`detectors/bias_patterns.py`, kept separate from the detection logic, same
file-split convention as the responsibility detector.

**Tier 1 — FAST heuristic (inline, synchronous, pure regex).** Scans the
final response text against a small, deliberately narrow set of patterns for
blatant, unhedged generalizations tying a protected characteristic to a
negative or deterministic trait ("all X are Y", "X people can't Y", "X don't
belong in Y"). Unlike the performance detector's fast tier, **a match here is
itself action-determining** — it contributes a `bias_flag` to the
decision-engine verdict directly (FLAG), not merely a signal for whether the
slow check is worth running. Reasoning: waiting for the slow judge on an
obvious blatant match would let one more visibly biased response ship before
anyone finds out. It never blocks and never edits — there's no clean
substring to surgically redact out of a stereotyping sentence the way there
is for PII, and blocking on a 4-pattern regex net alone (no LLM judgment in
the loop) risks blocking legitimate content, e.g. a response that quotes
biased language academically.

**Tier 2 — SLOW check (async, background task, never inline).** Runs when
tier 1 matched, **or** for a random sample of everything else (rate set by
the resolved policy's `slow_check_sample_rate` — shared with the performance
detector's sampling for now, no independent tuning yet). Reuses the same
judge model (`openai/gpt-oss-20b`) and the same `SCORE:`/`REASON:` response
parser as the performance detector (`detectors.performance.parse_judge_response`
— identical format, no need to duplicate it), asked whether the answer
stereotypes, demeans, or treats people differently based on a protected
characteristic. The parsed score is written back onto the event
(`bias_judge_score`, `bias_judge_reason`, `bias_sampled=1`). A score above
the resolved policy's `bias_review_threshold` pushes the event into
`review_queue` — exactly like the performance judge, this can never
retroactively change a response the caller already received.

```
[bias] model=openai/gpt-oss-120b request_id=chatcmpl-abc123 matched=['BLANKET_GENERALIZATION'] reason=matched bias pattern(s): BLANKET_GENERALIZATION
```

The exact pattern list is a deliberately narrow starting point (see the
docstring in `detectors/bias_patterns.py`), not a validated bias taxonomy —
expect false negatives on subtler bias (coded language, implication,
disparate framing that never uses a blanket "all X" construction).

## Session risk detector

`detectors/session.py` mirrors `detectors/cost.py`'s retry-loop check
exactly, just grouped by the caller-supplied `session_id` (see
`POST /v1/chat` above) instead of a message-content hash: every risk check
in this codebase so far looks at one request in isolation, which misses
risk that only emerges from a conversation's *arc* — e.g. an injection
attempt escalating gradually over several turns, none individually
block-worthy on their own.

**How it works:** before each event is logged, count how many prior events
in the same `session_id` got a non-`"pass"` decision-engine verdict within
the last `SESSION_RISK_WINDOW_MINUTES` (10). If that count already meets
`SESSION_RISK_THRESHOLD` (3 — a plain module constant, same "no independent
tuning yet" scoping as `detectors/cost.py`'s `RETRY_THRESHOLD`), **this
message is escalated to `flagged` regardless of its own content**, with
`"session_risk"` in the verdict's `flags`, and pushed straight to
`review_queue` with `detector="session"` — the same immediate-escalation
treatment an injection marker gets, since accumulated session risk is a
strong enough signal to warrant a human's attention right away rather than
waiting on the slow judge sample.

```
[session-risk] model=openai/gpt-oss-120b request_id=chatcmpl-abc123 flagged_count_in_window=3 reason=3 prior non-pass verdict(s) for session_id='conv-42' in the last 10 minutes — this message escalated regardless of its own content
```

A request with no `session_id` is always treated as its own isolated
session and can never trigger this — `session.evaluate()` returns
unflagged immediately in that case, matching today's pre-existing behavior
for every caller that doesn't opt in.

**FLAG-only, like bias — never BLOCK.** The window/threshold are a coarse
heuristic on verdict *counts*, not a judgment about the actual content of
those prior turns; blocking outright on a count alone, with no model
judgment in the loop, risks blocking a legitimate conversation that simply
had a few borderline moments early on.

## Policy / risk tiering

Every threshold above — the cost z-score cutoffs, the judge review
thresholds, the slow-check sample rate — is resolved **per request** from a
named policy rather than being a single hardcoded constant. `policy.py`
loads `config/policies.json` once at process startup into three named
tiers:

| `use_case` | `block_z_score` | `flag_z_score` | `review_threshold` | `slow_check_sample_rate` | `bias_review_threshold` |
| --- | --- | --- | --- | --- | --- |
| `customer_facing` | 3.0 | 2.0 | 0.4 | 0.25 | 0.3 |
| `internal` (default) | 4.0 | 2.5 | 0.6 | 0.10 | 0.6 |
| `batch` | 6.0 | 4.0 | 0.8 | 0.02 | 0.8 |

`internal`'s values are set to exactly match the thresholds this project
used before the policy layer existed, so a request that omits `use_case`
behaves identically to before. The request-level `use_case` field
(`POST /v1/chat`, see above) selects which tier applies; an unrecognized or
missing value falls back to `internal` silently rather than raising. This is
the concrete answer to "different use cases have very different risk
tolerance" — the same cost deviation that passes quietly under `batch`'s
generous tolerance can get flagged under `customer_facing`'s much stricter
one (see `test_decision_engine.py`'s policy-tiering scenarios for a worked
example). Editing `config/policies.json` and restarting picks up new values;
there's no hot-reload and no per-request override beyond picking one of the
three named tiers.

## Decision engine

`decision_engine.py` (project root, not under `detectors/` — it sits a level
above them) consolidates the fast-tier detector outputs computed above into a
**single verdict, still on the request path**, before the response leaves
`/v1/chat`. It only ever sees fast-tier signals: the cost-anomaly z-score,
the responsibility detector's PII/injection outcome, the bias detector's
fast-tier match, the session-risk detector's escalation, and
(informationally) the performance detector's fast heuristic. The slow judge
checks (performance and bias) are async by design and can only ever affect
`review_queue` after the fact — they never reach this module, and can never
change what the caller already received.

The z-score cutoffs below are resolved from the request's `Policy` (see
Policy / risk tiering above) — the numbers shown are `internal`'s (the
default), not fixed constants; `customer_facing`/`batch` requests apply
their own tier's stricter/looser cutoffs to the same rule shape.

Rules, most severe wins:

| Priority | Condition | Verdict | Effect on the response |
| --- | --- | --- | --- |
| 1 | Cost z-score `> block_z_score` (4.0 for `internal`) | `blocked` | **Not forwarded.** Caller gets a `429` with a reason; the event is still logged (`controlplane_action="blocked"`) for investigation. |
| 2 | PII found in the outgoing response | `edited` | Already redacted in place by the responsibility detector — this reflects that in the verdict. |
| 3 | Cost z-score between `flag_z_score` and `block_z_score` (2.5-4.0 for `internal`), OR an injection marker in the input, OR a bias fast-tier pattern match, OR a session-risk escalation | `flagged` | Passes through unchanged. An injection marker and a session-risk escalation both always get a `review_queue` entry (high severity); a bias fast-tier match does **not** by itself — only its slow judge score crossing `bias_review_threshold` does. |
| 4 | Nothing triggered | `pass` | Passes through unchanged. |

BLOCK outranks EDIT — if a response is extreme enough to block, there's
nothing left to redact-and-return. The `flags` list always reflects **every**
fast-tier signal that fired, not just the one that decided the action, so a
`"pass"` can still carry an informational `unhedged_claim` flag.

**Every response the caller receives — including the `429` block — carries a
`controlplane` metadata block:**

```json
{"action": "edited", "flags": ["pii_redacted"], "event_id": 42}
```

A blocked request instead gets:

```json
{
  "error": {"type": "cost_anomaly_blocked", "message": "cost anomaly blocked (z_score=6.21): ..."},
  "controlplane": {"action": "blocked", "flags": ["cost_anomaly"], "event_id": 17}
}
```

### Test script

```bash
python test_decision_engine.py
```

Fires 22 synthetic requests through the full pipeline (mocked Groq client, no
API key needed) covering every rule — clean requests, PII in the response,
prompt-injection phrases, blatant bias patterns in the response, a burst of
4 identical requests (retry-loop detector signal, but ordinary cost —
demonstrates that detector-level flagging and decision-engine-level action
are different layers), and engineered token spikes landing in the moderate
(2.5-4) and extreme (>4) z-score bands. Prints a table of category / HTTP
status / verdict / expected verdict / match, plus the raw `cost_z` and
`flags` for each request.

It then fires 2 more requests, separately, to prove the policy layer
actually changes behavior: the same ~z=2.2 cost deviation sent under
`use_case: "internal"` (expected `pass`, since 2.2 < internal's
`flag_z_score` of 2.5) and under `use_case: "customer_facing"` (expected
`flagged`, since 2.2 > customer_facing's stricter `flag_z_score` of 2.0) —
each target token count is computed fresh immediately before its own
request, since firing the first one shifts the shared per-model rolling
baseline the second one is evaluated against.

```bash
python test_audit_and_tuning.py
```

Covers the audit trail and policy-suggestions endpoints: the hash chain
stays valid across ordinary traffic (clean requests, an injection flag, a
performance judge finding, a review-queue resolution — exercising all 5
audit call sites); directly tampering one `audit_log` row's payload is
detected, with `GET /audit/verify` reporting the exact row id it broke at;
enough `false_positive` resolutions on `performance`/`bias` review-queue
items produce a suggestion with the expected current→suggested threshold
math; and `cost`/`responsibility` never appear in `/policy/suggestions`,
proven by generating real `responsibility` false-positive resolutions and
asserting they still don't produce a suggestion (not just that none happened
to appear).

```bash
python test_session_and_grounding.py
```

Covers multi-turn/session risk and retrieval-grounded checking: 3 injection
flags in the same `session_id` escalate a 4th, otherwise-clean message to
`flagged` with `session_risk` and a `review_queue` entry; the same sequence
spread across 4 different `session_id`s (and one request with no
`session_id` at all) never escalates, proving the grouping is genuinely
per-session; a request with `context_documents` forces the slow performance
check to run even when nothing else would have triggered it, and records
`performance.grounded == True` with the judge's actual grounded score; and
an ordinary request without `context_documents` records
`performance.grounded == False`, confirming the new field changes nothing
by default.

## Setup

```bash
cd controlplane-checker
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set GROQ_API_KEY (free key: https://console.groq.com/keys)
```

## Run

```bash
uvicorn app.main:app --reload
```

The service listens on `http://127.0.0.1:8000` by default.

## Try it

```bash
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-120b",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
    "max_tokens": 100
  }'
```

## Inspect logged events

```bash
sqlite3 controlplane.db "SELECT id, timestamp, model, latency_ms, input_tokens, output_tokens, status FROM events ORDER BY id DESC LIMIT 10;"
```

## Latency benchmark: measuring ControlPlane's overhead

```bash
python scripts/latency_test.py                    # 100 concurrent requests, default model
python scripts/latency_test.py -n 50 --model openai/gpt-oss-20b
```

Fires N concurrent requests at a **running instance** (`uvicorn app.main:app`
with a real `GROQ_API_KEY` — these are real model-latency numbers, not
simulated) and prints a before/after comparison table:

1. **p50/p95/p99 of the full endpoint round-trip** — wall-clock time measured
   client-side around each HTTP call.
2. **p50/p95/p99 of the underlying Groq call alone** — not re-measured; read
   back from `events.latency_ms`, which `/v1/chat` already logs immediately
   after `client.chat.completions.create()` returns and *before* any
   detector runs (responsibility, performance fast heuristic, cost, decision
   engine). Correlated per-request via the `controlplane.event_id` each
   response carries.
3. **The delta** — "the overhead of ControlPlane" — shown two ways: the
   quick aggregate view (subtracting matching percentiles) and the
   statistically correct **per-request paired difference**
   (`endpoint_latency_i - model_latency_i` for the same request, then
   percentiled) — the second one is what the pass/fail verdict is based on,
   since subtracting two percentiles from two different distributions isn't
   the same as the percentile of their difference.

It also empirically checks that the slow-tier judge checks never block the
response: it compares client-observed latency between requests that got
sampled for the async check (`performance.sampled=true`) and those that
didn't, and repeats the same comparison for the bias detector's slow tier
(`bias.sampled=true`), since it uses the identical `BackgroundTasks`
mechanism. If either slow check were mistakenly awaited inline instead of
run as a background task, sampled requests would show up markedly slower —
the script calls this out explicitly rather than just asserting the design
is correct.

Responses are classified into successes, `ControlPlane`-blocked (429 from
`decision_engine`, distinguished from an ordinary error by the
`error.type: "cost_anomaly_blocked"` body shape), upstream/API errors (e.g.
Groq rate-limiting under heavy concurrency — common on a free-tier key at
`-n 100`; reported separately rather than silently corrupting the latency
stats), and network-level failures.

**What this actually measures.** The "fast-path checks" as implemented
include two synchronous SQLite round-trips per request (`log_event`'s
INSERT + baseline query, `update_controlplane_verdict`'s UPDATE), each
dispatched via `anyio.to_thread.run_sync`. The pure regex/CPU cost of the
detectors themselves is sub-2ms (see the responsibility-detector and
decision-engine test coverage), but SQLite's single-writer model plus the
thread-pool dispatch mean the *measured* overhead grows with concurrency —
close to the ~10ms target at low concurrency (n=1–10), well above it at
n=100 due to write contention. That's a real, useful finding this script is
designed to surface honestly, not a bug in the measurement.
