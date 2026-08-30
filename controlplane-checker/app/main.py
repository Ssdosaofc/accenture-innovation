import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import groq
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

import decision_engine
from app.db import (
    get_dashboard_summary,
    get_feedback_stats,
    get_policy_suggestions,
    init_db,
    list_events,
    list_review_queue,
    log_event,
    resolve_review_queue_item,
    update_bias_judge_result,
    update_controlplane_verdict,
    update_judge_result,
    verify_audit_chain,
)
from detectors import bias
from detectors.cost import hash_messages
from detectors.performance import (
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL,
    build_grounded_judge_prompt,
    build_judge_prompt,
    fast_heuristic,
    parse_judge_response,
    should_run_slow_check,
)
from detectors.responsibility_fast import ResponsibilityFlag, evaluate_response
from policy import Policy, load_policies, resolve_policy

load_dotenv()

client = groq.AsyncGroq()  # reads GROQ_API_KEY from env

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_MAX_TOKENS = 1024

# Fields from the OpenAI chat-completions schema that Groq's API accepts
# and that we forward as-is if present on the incoming request.
PASSTHROUGH_FIELDS = (
    "temperature",
    "top_p",
    "n",
    "stop",
    "stream",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "user",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
)


POLICIES: dict[str, Policy] = {}
DEFAULT_USE_CASE: str = "internal"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global POLICIES, DEFAULT_USE_CASE
    init_db()
    # Loaded once at process start (not per-request) — see policy.py's
    # module docstring for why this is a file-based, restart-to-apply
    # config rather than a hot-reloadable/admin-API one.
    POLICIES, DEFAULT_USE_CASE = load_policies()
    yield


app = FastAPI(title="controlplane-checker", lifespan=lifespan)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[ChatMessage]
    max_tokens: int | None = None
    # Risk-tiering: which threshold profile applies (see policy.py /
    # config/policies.json). Unknown or omitted falls back to the
    # configured default use case rather than rejecting the request.
    use_case: str | None = None
    # Multi-turn/session risk (detectors/session.py): caller-supplied id
    # grouping requests into a conversation. Omitted = this message is its
    # own single-message session, same as today's behavior.
    session_id: str | None = None
    # Retrieval-grounded hallucination checking (detectors/performance.py):
    # caller-supplied source material the performance judge should check the
    # answer against, instead of judging plausibility alone. No document
    # store/retrieval lives in this service — the caller supplies the
    # material per-request. Omitted = today's ungrounded judge behavior.
    context_documents: list[str] | None = None


class ResolveReviewRequest(BaseModel):
    resolution: Literal["confirmed_issue", "false_positive"]
    note: str = ""


def _print_if_flagged(prefix: str, flagged: bool, detail: str) -> None:
    if flagged:
        print(f"[{prefix}] {detail}")


def _print_cost_flag(cost_flag, model: str, request_id: str | None) -> None:
    _print_if_flagged(
        "cost-anomaly",
        cost_flag.flagged,
        f"model={model} request_id={request_id} z_score={cost_flag.z_score} "
        f"cost_usd=${cost_flag.cost_usd:.6f} reason={cost_flag.reason}",
    )


def _print_responsibility_flag(
    flag: ResponsibilityFlag, model: str, request_id: str | None
) -> None:
    _print_if_flagged(
        "responsibility",
        flag.flagged,
        f"model={model} request_id={request_id} action={flag.action} "
        f"severity={flag.severity} reason={flag.reason}",
    )


def _print_bias_fast_flag(
    flag: "bias.FastBiasResult", model: str, request_id: str | None
) -> None:
    _print_if_flagged(
        "bias",
        flag.flagged,
        f"model={model} request_id={request_id} matched={flag.matched_patterns} reason={flag.reason}",
    )


def _print_session_flag(flag, model: str, request_id: str | None) -> None:
    _print_if_flagged(
        "session-risk",
        flag.flagged,
        f"model={model} request_id={request_id} flagged_count_in_window={flag.flagged_count_in_window} "
        f"reason={flag.reason}",
    )


def _print_verdict(verdict: decision_engine.Verdict, event_id: int) -> None:
    if verdict.action != decision_engine.ACTION_PASS:
        print(
            f"[decision-engine] event_id={event_id} action={verdict.action} "
            f"flags={verdict.flags} reason={verdict.reason or 'n/a'}"
        )


def _extract_user_input_text(messages: list[dict]) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )


def _to_groq_request(req: ChatCompletionRequest, raw_body: dict) -> dict:
    """Groq's chat completions API is OpenAI-compatible, so this is close to
    an identity mapping — just select the fields Groq accepts and apply
    baseline defaults."""
    groq_request: dict = {
        "model": req.model or DEFAULT_MODEL,
        "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
        "messages": [
            {"role": msg.role, "content": msg.content} for msg in req.messages
        ],
    }
    for field in PASSTHROUGH_FIELDS:
        if field in raw_body and raw_body[field] is not None:
            groq_request[field] = raw_body[field]

    return groq_request


async def _run_slow_performance_check(
    event_id: int,
    question: str,
    answer: str,
    review_threshold: float,
    context_documents: list[str] | None = None,
) -> None:
    """Tier 2 of the performance detector: a real, second LLM call to a
    cheap/fast judge model. This ONLY ever runs as a FastAPI background
    task — scheduled after the response has already been sent to the
    caller — so it can never add latency to /v1/chat, no matter how slow
    the judge call turns out to be. `review_threshold` is the resolved
    Policy's value for this request's use_case (policy.py).

    When `context_documents` is supplied, uses the retrieval-grounded prompt
    (checks the answer against those specific documents) instead of the
    default plausibility-only prompt — same model, same background-task
    guarantee, same SCORE:/REASON: parser either way.
    """
    grounded = bool(context_documents)
    prompt = (
        build_grounded_judge_prompt(question, answer, context_documents)
        if grounded
        else build_judge_prompt(question, answer)
    )
    judge_score: float | None
    try:
        judge_response = await client.chat.completions.create(
            model=JUDGE_MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        judge_text = (judge_response.choices[0].message.content or "") if judge_response.choices else ""
        judge_score, judge_reason = parse_judge_response(judge_text)
    except Exception as exc:  # noqa: BLE001 - background task must never raise
        judge_score, judge_reason = None, f"judge call failed: {exc}"

    await update_judge_result(
        event_id, judge_score, judge_reason, sampled=True, review_threshold=review_threshold, grounded=grounded
    )

    if judge_score is not None and judge_score > review_threshold:
        print(
            f"[performance] event_id={event_id} judge_score={judge_score:.2f} grounded={grounded} "
            f"reason={judge_reason}"
        )


async def _run_slow_bias_check(
    event_id: int, question: str, answer: str, review_threshold: float
) -> None:
    """Tier 2 of the bias detector — structurally identical to
    `_run_slow_performance_check` above (same background-task guarantee: it
    only ever runs after the response has already been sent). Reuses the
    same judge model and response-parsing logic as the performance
    detector; only the prompt and the destination columns/detector name
    differ. `review_threshold` is the resolved Policy's bias_review_threshold.
    """
    bias_judge_score: float | None
    try:
        judge_response = await client.chat.completions.create(
            model=JUDGE_MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            messages=[{"role": "user", "content": bias.build_bias_judge_prompt(question, answer)}],
        )
        judge_text = (judge_response.choices[0].message.content or "") if judge_response.choices else ""
        bias_judge_score, bias_judge_reason = parse_judge_response(judge_text)
    except Exception as exc:  # noqa: BLE001 - background task must never raise
        bias_judge_score, bias_judge_reason = None, f"judge call failed: {exc}"

    await update_bias_judge_result(
        event_id, bias_judge_score, bias_judge_reason, sampled=True, review_threshold=review_threshold
    )

    if bias_judge_score is not None and bias_judge_score > review_threshold:
        print(
            f"[bias] event_id={event_id} judge_score={bias_judge_score:.2f} "
            f"reason={bias_judge_reason}"
        )


@app.post("/v1/chat")
async def chat(request: Request, background_tasks: BackgroundTasks):
    try:
        raw_body = await request.json()
    except ValueError as exc:
        # Malformed JSON (e.g. a stray backslash from a shell that ate a
        # line-continuation inside a quoted string) is a client mistake,
        # not a server fault — surface it as a 400 with the parser's own
        # message instead of an opaque 500.
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc
    try:
        req = ChatCompletionRequest.model_validate(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Risk-tiering: resolve which threshold profile applies to this request.
    # Everything downstream (decision_engine, both slow-check sample rates,
    # both review-queue escalation thresholds) reads from this Policy
    # instead of a hardcoded module constant — see policy.py.
    policy = resolve_policy(req.use_case, POLICIES, DEFAULT_USE_CASE)

    groq_request = _to_groq_request(req, raw_body)
    message_hash = hash_messages(raw_body.get("messages", []))
    user_input_text = _extract_user_input_text(raw_body.get("messages", []))

    start = time.perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        response = await client.chat.completions.create(**groq_request)
    except groq.APIStatusError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        error_request_id = getattr(exc, "request_id", None)

        # No model output to scan on an upstream error, but the original
        # input can still carry a prompt-injection marker worth flagging.
        # There's also nothing to run the performance/bias judge against.
        _, responsibility_flag = evaluate_response([], user_input_text)

        cost_flag, session_flag, _event_id = await log_event(
            {
                "timestamp": timestamp,
                "latency_ms": latency_ms,
                "model": groq_request["model"],
                "request_id": error_request_id,
                "message_hash": message_hash,
                "request_body": raw_body,
                "response_body": None,
                "status": "error",
                "error": str(exc),
                "responsibility_flagged": responsibility_flag.flagged,
                "responsibility_action": responsibility_flag.action,
                "responsibility_severity": responsibility_flag.severity,
                "responsibility_reason": responsibility_flag.reason,
                "use_case": policy.use_case,
                "policy_block_z_score": policy.block_z_score,
                "policy_flag_z_score": policy.flag_z_score,
                "policy_review_threshold": policy.review_threshold,
                "session_id": req.session_id,
            }
        )
        _print_cost_flag(cost_flag, groq_request["model"], error_request_id)
        _print_responsibility_flag(responsibility_flag, groq_request["model"], error_request_id)
        _print_session_flag(session_flag, groq_request["model"], error_request_id)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    latency_ms = (time.perf_counter() - start) * 1000
    response_dict = response.model_dump()

    usage = response.usage
    input_tokens = usage.prompt_tokens if usage else None
    output_tokens = usage.completion_tokens if usage else None

    # Responsibility detector runs synchronously, inline, before the response
    # is returned: it scans the OUTGOING response for PII and redacts it in
    # place, and scans the ORIGINAL input for prompt-injection markers
    # (flag-only, never blocks). Pure regex — single-digit milliseconds.
    choices = response_dict.get("choices") or []
    response_texts = [
        (choice.get("message") or {}).get("content") or "" for choice in choices
    ]
    redacted_texts, responsibility_flag = evaluate_response(response_texts, user_input_text)
    for choice, redacted_text in zip(choices, redacted_texts):
        message = choice.get("message")
        if message is not None:
            message["content"] = redacted_text

    # Performance detector, tier 1 (FAST): pure regex over the final,
    # redacted response text — safe to run inline, same as the other fast
    # checks above. It only decides whether tier 2 (a real LLM call) is
    # worth running; it never touches the response itself.
    combined_answer_text = "\n".join(redacted_texts)
    fast_result = fast_heuristic(combined_answer_text)
    trigger_slow_check = should_run_slow_check(fast_result, random.random(), policy.slow_check_sample_rate)
    # Retrieval-grounded checking: a caller who supplied context_documents
    # has explicitly opted into stronger verification, so the slow judge
    # check runs unconditionally in that case — skipping it because the
    # fast heuristic didn't flag or the random sample missed would defeat
    # the point of asking for grounding at all.
    trigger_slow_check = trigger_slow_check or bool(req.context_documents)

    # Bias detector, tier 1 (FAST): same shape as performance's fast tier,
    # but — unlike performance — a match here IS action-determining (see
    # decision_engine.decide()), not just a sampling signal.
    bias_fast_result = bias.fast_heuristic(combined_answer_text)
    trigger_slow_bias_check = bias.should_run_slow_check(
        bias_fast_result, random.random(), policy.slow_check_sample_rate
    )

    # Cost detector runs synchronously here too, before the response is
    # returned — it flags the logged event but does not alter the response.
    cost_flag, session_flag, event_id = await log_event(
        {
            "timestamp": timestamp,
            "latency_ms": latency_ms,
            "model": response.model,
            "request_id": response.id,
            "message_hash": message_hash,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "request_body": raw_body,
            "response_body": response_dict,
            "status": "success",
            "responsibility_flagged": responsibility_flag.flagged,
            "responsibility_action": responsibility_flag.action,
            "responsibility_severity": responsibility_flag.severity,
            "responsibility_reason": responsibility_flag.reason,
            "use_case": policy.use_case,
            "policy_block_z_score": policy.block_z_score,
            "policy_flag_z_score": policy.flag_z_score,
            "policy_review_threshold": policy.review_threshold,
            "bias_flagged": bias_fast_result.flagged,
            "bias_fast_reason": bias_fast_result.reason,
            "session_id": req.session_id,
        }
    )

    _print_cost_flag(cost_flag, response.model, response.id)
    _print_responsibility_flag(responsibility_flag, response.model, response.id)
    _print_bias_fast_flag(bias_fast_result, response.model, response.id)
    _print_session_flag(session_flag, response.model, response.id)

    # Decision engine: consolidates the fast-tier signals gathered above
    # (cost z_score, PII/injection outcome, hedge heuristic, bias fast-tier
    # match, session-risk escalation) into a single verdict, still on the
    # request path, using this request's resolved Policy for thresholds.
    # This is the ONLY thing allowed to change what the caller receives —
    # the slow judge checks below never can.
    verdict = decision_engine.decide(
        cost_flag, responsibility_flag, fast_result, bias_fast_result, policy, session_flag
    )
    await update_controlplane_verdict(event_id, verdict.action, verdict.flags)
    _print_verdict(verdict, event_id)

    if verdict.action == decision_engine.ACTION_BLOCKED:
        # Extreme cost anomaly: don't forward the response at all. Already
        # logged above (status="success", controlplane_action="blocked") so
        # the full context is there for investigation, but the caller gets
        # a 429-style rejection instead of the model's output.
        return JSONResponse(
            status_code=429,
            content={
                "error": {"type": "cost_anomaly_blocked", "message": verdict.reason},
                "controlplane": verdict.to_metadata(event_id),
            },
        )

    response_dict["controlplane"] = verdict.to_metadata(event_id)

    # Performance + bias detectors, tier 2 (SLOW): only ever scheduled as
    # background tasks, after this point — the response below is returned
    # to the caller immediately, and the (cheap-model) judge calls happen
    # afterward, off the request path entirely. Skipped for blocked
    # responses above, since nobody will ever see that output.
    if trigger_slow_check:
        background_tasks.add_task(
            _run_slow_performance_check,
            event_id,
            user_input_text,
            combined_answer_text,
            policy.review_threshold,
            req.context_documents,
        )
    if trigger_slow_bias_check:
        background_tasks.add_task(
            _run_slow_bias_check,
            event_id,
            user_input_text,
            combined_answer_text,
            policy.bias_review_threshold,
        )

    return response_dict


@app.get("/review-queue")
async def get_review_queue():
    """Pending review_queue items, each joined with its full event (input,
    output, and every detector's flags/scores), sorted by severity (high
    first) then recency within a severity tier."""
    items = await list_review_queue()
    return {"items": items, "count": len(items)}


@app.post("/review-queue/{review_id}/resolve")
async def resolve_review_item(review_id: int, body: ResolveReviewRequest):
    """Mark a review_queue item resolved. A "confirmed_issue" resolution is
    also logged to `confirmed_issues` — the feedback-loop stub: data you'd
    eventually mine to tune detector thresholds. No retraining logic yet,
    just storage."""
    result = await resolve_review_queue_item(review_id, body.resolution, body.note)
    if result is None:
        raise HTTPException(status_code=404, detail=f"review_queue item {review_id} not found")
    return result


@app.get("/events")
async def get_events(limit: int = 50, offset: int = 0):
    """Most-recent-first page of events, each carrying every detector's
    flags/scores plus the raw input/output — powers the dashboard's live
    events table."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    items, total = await list_events(limit, offset)
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@app.get("/summary")
async def get_summary():
    """Aggregates for the dashboard's summary strip."""
    return await get_dashboard_summary()


@app.get("/feedback/stats")
async def get_feedback_stats_endpoint():
    """All-time, per-detector confirmation/false-positive stats computed
    from review_queue resolutions — the feedback loop closing over the
    review-queue data. Distinct from /summary (a rolling-window operational
    snapshot): this is cumulative accuracy data, a different axis. Purely
    observability — see app/db.py's module comment for why this never
    auto-adjusts a threshold."""
    return await get_feedback_stats()


@app.get("/audit/verify")
async def get_audit_verify():
    """Recomputes the full audit_log hash chain and reports whether it's
    intact. A row edited or deleted after being written — in `events`,
    `review_queue`, or `audit_log` itself — breaks the chain at the id it
    happened at. See audit.py's module docstring for the design."""
    return await verify_audit_chain()


@app.get("/policy/suggestions")
async def get_policy_suggestions_endpoint():
    """Suggestion-only, human-sign-off threshold tuning: reads the same
    review_queue resolutions as /feedback/stats and, for detectors with a
    tunable float threshold (performance, bias — see app/db.py's module
    comment for why cost/responsibility are excluded), suggests raising the
    threshold when the false-positive rate is high on a trustworthy sample.
    Never lowers a threshold and never applies anything automatically."""
    return await get_policy_suggestions(POLICIES)


DASHBOARD_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML_PATH.read_text()


@app.get("/health")
async def health():
    return {"status": "ok"}
