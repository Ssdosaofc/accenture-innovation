#!/usr/bin/env python3
"""Concurrency/latency benchmark for controlplane-checker's POST /v1/chat.

Fires N concurrent requests at a RUNNING instance of the service and reports:

  1. p50/p95/p99 of the full endpoint round-trip — wall-clock time measured
     client-side, from just before the HTTP request is sent to just after
     the response is fully received. This is what a real caller feels.

  2. p50/p95/p99 of the underlying Groq model call alone. This is NOT
     re-measured here — it's read back from the `latency_ms` column the
     proxy already logs per-event. app/main.py captures that value
     immediately after `client.chat.completions.create()` returns, BEFORE
     the responsibility detector, the performance fast heuristic, the cost
     detector, or the decision engine run — see the `start`/`latency_ms`
     lines in the `/v1/chat` handler. So `events.latency_ms` is already the
     model call in isolation; this script's job is to fetch it back via
     GET /events (correlated by the `controlplane.event_id` each response
     carries) and compare it against what the client observed.

  3. The delta between the two — "the overhead of ControlPlane" — reported
     two ways:
       a) aggregate: subtracting each endpoint percentile from the matching
          model-call percentile (the quick, at-a-glance comparison)
       b) paired: percentiles of the PER-REQUEST difference
          (endpoint_latency_i - model_latency_i for the same request i).
          This is the statistically correct number — subtracting two
          percentiles computed from two different distributions is not the
          same as the percentile of their difference, especially in the
          tails — so (b) is the one the pass/fail verdict is based on.

It also empirically checks the async claim: requests whose slow judge check
got scheduled (`performance.sampled=true`) are compared against requests
that weren't, to confirm the client-observed latency is NOT elevated for
sampled requests. If the slow check were (incorrectly) awaited inline
instead of run as a background task, this comparison would show it clearly.

Usage:
    python scripts/latency_test.py
    python scripts/latency_test.py --url http://127.0.0.1:8000 -n 100
    python scripts/latency_test.py --model llama-3.1-8b-instant --max-tokens 100

Requires a running instance of the service (`uvicorn app.main:app`) with a
valid GROQ_API_KEY — these are real model-latency numbers against the real
API, not a simulation. A free-tier Groq key may rate-limit under 100
simultaneous requests; failed/rate-limited requests are reported separately
rather than silently corrupting the latency statistics.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

import httpx

PROMPTS = [
    "Say one interesting fact about the ocean.",
    "Give a one-sentence tip for staying focused.",
    "Name a famous bridge and one fact about it.",
    "Suggest a simple weekend activity.",
    "Describe your favorite season in one sentence.",
    "Give a quick tip for making coffee.",
    "Name a classic board game and why it's fun.",
    "Suggest a good beginner hobby.",
    "Give a one-line productivity tip.",
    "Describe a good book genre for relaxing.",
]


@dataclass
class RequestResult:
    ok: bool  # got a real HTTP response (network-level success)
    status_code: int | None
    elapsed_ms: float
    event_id: int | None
    controlplane_blocked: bool
    error: str | None


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches the conventional p50/p95/p99
    definition most latency dashboards use)."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def fire_request(
    client: httpx.AsyncClient, url: str, payload: dict, timeout: float
) -> RequestResult:
    start = time.perf_counter()
    try:
        resp = await client.post(url, json=payload, timeout=timeout)
    except httpx.RequestError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            ok=False, status_code=None, elapsed_ms=elapsed_ms,
            event_id=None, controlplane_blocked=False, error=str(exc),
        )

    elapsed_ms = (time.perf_counter() - start) * 1000

    event_id = None
    controlplane_blocked = False
    error = None
    try:
        body = resp.json()
    except Exception:
        body = {}

    cp = body.get("controlplane") if isinstance(body, dict) else None
    if cp:
        event_id = cp.get("event_id")
    if resp.status_code == 429 and isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type") == "cost_anomaly_blocked":
            controlplane_blocked = True
    if resp.status_code >= 400 and not controlplane_blocked:
        error = str(body.get("detail", body))[:200]

    return RequestResult(
        ok=True,
        status_code=resp.status_code,
        elapsed_ms=elapsed_ms,
        event_id=event_id,
        controlplane_blocked=controlplane_blocked,
        error=error,
    )


async def run_burst(
    base_url: str, n: int, model: str | None, max_tokens: int, timeout: float
) -> list[RequestResult]:
    endpoint = base_url.rstrip("/") + "/v1/chat"
    limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=n)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = []
        for i in range(n):
            payload: dict = {
                "messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}],
                "max_tokens": max_tokens,
            }
            if model:
                payload["model"] = model
            tasks.append(fire_request(client, endpoint, payload, timeout))

        # asyncio.gather schedules all N coroutines onto the event loop
        # together; each `await client.post(...)` fires as soon as the loop
        # gets to it — this is what makes the N requests concurrent, not
        # sequential-with-overlap.
        return await asyncio.gather(*tasks)


async def fetch_events_by_id(
    base_url: str, event_ids: set[int], page_limit: int
) -> dict[int, dict]:
    url = base_url.rstrip("/") + "/events"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"limit": page_limit})
        resp.raise_for_status()
        items = resp.json().get("items", [])
    return {it["id"]: it for it in items if it["id"] in event_ids}


async def check_health(base_url: str) -> bool:
    url = base_url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
            return resp.status_code == 200
    except httpx.RequestError:
        return False


def print_table(rows: list[tuple], headers: tuple) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


async def main_async(args: argparse.Namespace) -> int:
    print(f"=== controlplane-checker latency benchmark ===")
    print(f"Target:      {args.url}/v1/chat")
    print(f"Concurrency: {args.n} requests, fired simultaneously")
    print(f"Model:       {args.model or '(server default)'}")
    print(f"Max tokens:  {args.max_tokens}")
    print()

    if not await check_health(args.url):
        print(f"ERROR: {args.url}/health did not respond. Is the server running?")
        print("  Start it with: uvicorn app.main:app  (from the project root, with GROQ_API_KEY set)")
        return 1

    print("Firing burst...")
    burst_start = time.perf_counter()
    results = await run_burst(args.url, args.n, args.model, args.max_tokens, args.timeout)
    burst_wall_s = time.perf_counter() - burst_start
    print(f"Burst completed in {burst_wall_s:.2f}s wall-clock.\n")

    # --- Classify responses -------------------------------------------------
    network_failures = [r for r in results if not r.ok]
    http_responses = [r for r in results if r.ok]
    controlplane_blocked = [r for r in http_responses if r.controlplane_blocked]
    upstream_errors = [r for r in http_responses if r.error is not None]
    successful = [r for r in http_responses if not r.controlplane_blocked and r.error is None]

    print("--- Response breakdown ---")
    print(f"  Total fired:              {len(results)}")
    print(f"  Successful (200):         {len(successful)}")
    print(f"  ControlPlane-blocked(429):{len(controlplane_blocked):>4}")
    print(f"  Upstream/API errors:      {len(upstream_errors)}")
    print(f"  Network-level failures:   {len(network_failures)}")
    if upstream_errors:
        print(f"    (sample upstream error: {upstream_errors[0].error})")
        print("    Tip: a free-tier Groq key rate-limits under heavy concurrency —")
        print("    try a smaller -n if these dominate.")
    print()

    endpoint_latencies = [r.elapsed_ms for r in http_responses if r.elapsed_ms is not None]
    if not endpoint_latencies:
        print("No successful HTTP responses — nothing to report.")
        return 1

    # --- Correlate with proxy-logged model-call latency ----------------------
    event_ids = {r.event_id for r in http_responses if r.event_id is not None}
    events_by_id = await fetch_events_by_id(args.url, event_ids, page_limit=max(args.n * 2, 200))

    paired: list[tuple[float, float, bool]] = []  # (endpoint_ms, model_ms, sampled)
    for r in http_responses:
        if r.event_id is None:
            continue
        ev = events_by_id.get(r.event_id)
        if ev is None:
            continue
        paired.append((r.elapsed_ms, ev["latency_ms"], ev["performance"]["sampled"]))

    matched_n = len(paired)
    print(f"Correlated {matched_n}/{len(http_responses)} responses to their logged events "
          f"(event_id -> proxy-recorded model latency).\n")

    model_latencies = [m for _, m, _ in paired]
    deltas = [e - m for e, m, _ in paired]

    # --- Main comparison table ------------------------------------------------
    print("--- Latency comparison (ms) ---")
    rows = []
    for label, series in (
        ("Endpoint (ControlPlane, full round-trip)", endpoint_latencies),
        ("Model call alone (Groq, proxy-logged)", model_latencies),
    ):
        rows.append((
            label,
            f"{percentile(series, 50):.2f}",
            f"{percentile(series, 95):.2f}",
            f"{percentile(series, 99):.2f}",
        ))
    print_table(rows, ("Metric", "p50", "p95", "p99"))
    print()

    if model_latencies:
        agg_delta = (
            "Overhead (aggregate, Δ of percentiles)",
            f"{percentile(endpoint_latencies, 50) - percentile(model_latencies, 50):+.2f}",
            f"{percentile(endpoint_latencies, 95) - percentile(model_latencies, 95):+.2f}",
            f"{percentile(endpoint_latencies, 99) - percentile(model_latencies, 99):+.2f}",
        )
        print_table([agg_delta], ("Metric", "p50", "p95", "p99"))
        print()

    if deltas:
        print("--- Per-request paired overhead (ms) — the actual ControlPlane tax ---")
        print(
            f"  p50: {percentile(deltas, 50):+.2f}   "
            f"p95: {percentile(deltas, 95):+.2f}   "
            f"p99: {percentile(deltas, 99):+.2f}   "
            f"mean: {sum(deltas)/len(deltas):+.2f}   "
            f"max: {max(deltas):+.2f}   "
            f"(n={len(deltas)})"
        )
        print()

        p50_delta = percentile(deltas, 50)
        p95_delta = percentile(deltas, 95)
        if p50_delta < 10 and p95_delta < 10:
            print(f"VERDICT: fast-path overhead is under 10ms at both p50 ({p50_delta:.2f}ms) "
                  f"and p95 ({p95_delta:.2f}ms). ✅")
        elif p50_delta < 10:
            print(f"VERDICT: fast-path overhead is under 10ms at p50 ({p50_delta:.2f}ms), "
                  f"but p95 is {p95_delta:.2f}ms — worth a closer look under this load. ⚠️")
        else:
            print(f"VERDICT: fast-path overhead is {p50_delta:.2f}ms at p50 — above the ~10ms "
                  f"target. ⚠️")
        print()

    # --- Empirical check: sampled (slow-check-scheduled) vs not --------------
    sampled_endpoint = [e for e, _, sampled in paired if sampled]
    unsampled_endpoint = [e for e, _, sampled in paired if not sampled]
    print("--- Slow-tier (judge check) non-blocking check ---")
    print(f"  {len(sampled_endpoint)} of {matched_n} requests were sampled for the async slow "
          f"judge check (performance.sampled=true).")
    if sampled_endpoint and unsampled_endpoint:
        s_p50 = percentile(sampled_endpoint, 50)
        u_p50 = percentile(unsampled_endpoint, 50)
        print(f"  Endpoint latency p50 — sampled: {s_p50:.2f}ms vs not sampled: {u_p50:.2f}ms")
        if s_p50 <= u_p50 * 1.5:
            print("  No meaningful latency penalty for sampled requests — consistent with the "
                  "judge call running as a background task AFTER the response was already sent, "
                  "not awaited inline.")
        else:
            print("  Sampled requests are notably slower — investigate whether the slow check "
                  "is actually running inline instead of as a background task.")
    else:
        print("  Not enough of both groups in this run to compare (try a larger -n).")
    print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Base URL of the running service")
    parser.add_argument("-n", "--concurrency", dest="n", type=int, default=100, help="Number of concurrent requests")
    parser.add_argument("--model", default=None, help="Model to request (default: let the server pick its own default)")
    parser.add_argument("--max-tokens", type=int, default=200, help="max_tokens per request")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds")
    args = parser.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
