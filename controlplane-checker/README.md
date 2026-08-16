# controlplane-checker

A FastAPI pass-through service that accepts OpenAI-style chat completion
requests, forwards them to [Groq](https://groq.com) (free-tier hosting for
open-source models like Llama 3.3/4), returns the completion unmodified, and
logs every request/response pair to a local SQLite database.

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
  "model": "llama-3.3-70b-versatile",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Say hello in one sentence."}
  ],
  "max_tokens": 256
}
```

Notes:

- `model` defaults to `llama-3.3-70b-versatile` if omitted. See
  [Groq's model list](https://console.groq.com/docs/models) for other
  free open-source options (e.g. `llama-3.1-8b-instant`,
  `meta-llama/llama-4-scout-17b-16e-instruct`, `mixtral-8x7b-32768`).
- `max_tokens` defaults to `1024` if omitted.
- `temperature`, `top_p`, `stop`, `tools`, `tool_choice`, `response_format`,
  and other standard OpenAI chat-completion fields are forwarded as-is if
  present.
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
      "performance": {"judge_score": null, "judge_reason": null, "sampled": false},
      "controlplane": {"action": "pass", "flags": []}
    }
  ]
}
```

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
| `controlplane_action` | The decision engine's final verdict: `"pass"`, `"edited"`, `"flagged"`, or `"blocked"` |
| `controlplane_flags` | JSON array of every fast-tier signal that fired, e.g. `["cost_anomaly","injection_marker"]` |

High-severity findings — an injection marker from the responsibility check, or
a `judge_score` above `0.6` from the performance check — are also inserted
into a `review_queue` table for a human to look at later:

| Column | Description |
| --- | --- |
| `event_id` | FK into `events.id` |
| `detector` | `"responsibility"` or `"performance"` |
| `severity` | Currently always `"high"` |
| `reason` | The detector's reason string at the time of the finding |
| `created_at` | UTC ISO-8601 timestamp |
| `resolved` | `0`/`1` — set via `POST /review-queue/{id}/resolve` (see below) |
| `resolution` | `NULL` until resolved; then `"confirmed_issue"` or `"false_positive"` |
| `note` | Free-text note supplied at resolution time |
| `resolved_at` | UTC ISO-8601 timestamp of resolution, `NULL` until resolved |

Every `"confirmed_issue"` resolution is also copied into a `confirmed_issues`
table — the feedback-loop stub described below.

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
        "performance": {"judge_score": null, "judge_reason": null, "sampled": false},
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

**Migration note:** the `resolution`/`note`/`resolved_at` columns and the
`confirmed_issues` table are added by `init_db()` on startup — including to
a `controlplane.db` created by an earlier version of this service, via
`ALTER TABLE ... ADD COLUMN` with the duplicate-column error swallowed
(SQLite has no `ADD COLUMN IF NOT EXISTS`). Safe to run repeatedly.

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
[cost-anomaly] model=llama-3.3-70b-versatile request_id=chatcmpl-abc123 z_score=3.11 cost_usd=$0.004920 reason=token_count=8200 is 3.11 stddev above baseline mean 512 (n=87)
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
[responsibility] model=llama-3.3-70b-versatile request_id=chatcmpl-abc123 action=edited severity=high reason=PII redacted from response: EMAIL, SSN; prompt-injection markers found in input: IGNORE_INSTRUCTIONS
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
cheaper/faster **judge model** (`llama-3.1-8b-instant`, vs. the default
generation model `llama-3.3-70b-versatile`) is asked:

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

## Decision engine

`decision_engine.py` (project root, not under `detectors/` — it sits a level
above them) consolidates the fast-tier detector outputs computed above into a
**single verdict, still on the request path**, before the response leaves
`/v1/chat`. It only ever sees fast-tier signals: the cost-anomaly z-score,
the responsibility detector's PII/injection outcome, and (informationally)
the performance detector's fast heuristic. The slow judge check is async by
design and can only ever affect `review_queue` after the fact — it never
reaches this module, and can never change what the caller already received.

Rules, most severe wins:

| Priority | Condition | Verdict | Effect on the response |
| --- | --- | --- | --- |
| 1 | Cost z-score `> 4.0` (extreme) | `blocked` | **Not forwarded.** Caller gets a `429` with a reason; the event is still logged (`controlplane_action="blocked"`) for investigation. |
| 2 | PII found in the outgoing response | `edited` | Already redacted in place by the responsibility detector — this reflects that in the verdict. |
| 3 | Cost z-score `2.5`-`4.0` (moderate), OR an injection marker in the input | `flagged` | Passes through unchanged. An injection marker also gets a `review_queue` entry (from the responsibility detector, high severity). |
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

Fires 20 synthetic requests through the full pipeline (mocked Groq client, no
API key needed) covering every rule — clean requests, PII in the response,
prompt-injection phrases, a burst of 4 identical requests (retry-loop
detector signal, but ordinary cost — demonstrates that detector-level
flagging and decision-engine-level action are different layers), and
engineered token spikes landing in the moderate (2.5-4) and extreme (>4)
z-score bands. Prints a table of category / HTTP status / verdict / expected
verdict / match, plus the raw `cost_z` and `flags` for each request.

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
    "model": "llama-3.3-70b-versatile",
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
python scripts/latency_test.py -n 50 --model llama-3.1-8b-instant
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

It also empirically checks that the slow-tier judge check never blocks the
response: it compares client-observed latency between requests that got
sampled for the async check (`performance.sampled=true`) and those that
didn't. If the slow check were mistakenly awaited inline instead of run as a
`BackgroundTasks` job, sampled requests would show up markedly slower — the
script calls this out explicitly rather than just asserting the design is
correct.

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
