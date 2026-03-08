import asyncio
import logging
import os
import time
from datetime import datetime
from collections import Counter
from statistics import mean

from langgraph.graph import StateGraph, END
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from .checkpointer import SurrealDBCheckpointSaver
from .state import State
from .tools import (
    get_event_annotations,
    flag_suspicious_event,
    fuse_events,
    get_timeline,
    vector_search,
)

logger = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 30.0
LLM_MODELS = os.getenv(
    "GODEYE_LLM_MODELS",
    "claude-3-5-sonnet-latest,claude-3-5-sonnet-20240620,claude-3-haiku-20240307",
)


def _parse_llm_models(raw_models: str | None) -> list[str]:
    models = []
    for model in (raw_models or "").split(","):
        normalized = model.strip()
        if not normalized:
            continue
        if normalized not in models:
            models.append(normalized)
    return models


def _llm_model_candidates() -> list[str]:
    configured = _parse_llm_models(os.getenv("GODEYE_LLM_MODEL"))
    fallback_chain = _parse_llm_models(LLM_MODELS)
    all_candidates: list[str] = []
    for model in configured + fallback_chain:
        if model and model not in all_candidates:
            all_candidates.append(model)
    return all_candidates or ["claude-3-5-sonnet-latest"]


async def _invoke_llm(prompt: str) -> tuple[str, str]:
    last_error: Exception | None = None
    for model in _llm_model_candidates():
        try:
            llm = ChatAnthropic(model=model)
            response = await asyncio.wait_for(llm.ainvoke([HumanMessage(content=prompt)]), timeout=LLM_TIMEOUT_SECONDS)
            if response and getattr(response, "content", None) is not None:
                logger.info("llm.call success: model=%s", model)
                return str(response.content), model
            raise RuntimeError(f"LLM returned empty content for model={model}")
        except Exception as error:
            logger.warning("llm.call failed: model=%s err=%s", model, error)
            last_error = error
    if last_error is None:
        raise RuntimeError("No LLM models configured.")
    raise last_error


def _parse_retention_from_env() -> int | None:
    raw_value = os.getenv("GODEYE_CHECKPOINT_LIMIT")
    if raw_value is None:
        return None
    try:
        limit = int(raw_value)
    except ValueError:
        raise ValueError("GODEYE_CHECKPOINT_LIMIT must be an integer")
    return limit if limit >= 0 else 0


def is_llm_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _coerce_confidence(value: object) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, n))


def _safe_bucket_key(ts: object) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        minute_bucket = (dt.minute // 10) * 10
        return dt.replace(minute=minute_bucket, second=0, microsecond=0).isoformat()
    except (TypeError, ValueError):
        return "unknown"


def _build_timeline_metrics(
    events: list[dict],
    prev_events: list[dict],
    context_docs: list[dict],
    entity_docs: list[dict],
    baseline_docs: list[dict],
    from_time: str,
    to_time: str,
) -> dict:
    axis_counts = Counter(str(ev.get("axis") or "unknown").lower() for ev in events if isinstance(ev, dict))
    severity_counts = Counter(str(ev.get("severity") or "unknown").lower() for ev in events if isinstance(ev, dict))
    confidences = [_coerce_confidence(ev.get("confidence")) for ev in events if isinstance(ev, dict)]
    bucket_counts = Counter(_safe_bucket_key(ev.get("start_time")) for ev in events if isinstance(ev, dict))

    return {
        "window": {"from": from_time, "to": to_time},
        "event_count": len(events),
        "prev_event_count": len(prev_events),
        "axis_distribution": dict(axis_counts),
        "severity_distribution": dict(severity_counts),
        "confidence": {
            "count": len(confidences),
            "avg": round(mean(confidences), 3) if confidences else 0.0,
            "min": round(min(confidences), 3) if confidences else 0.0,
            "max": round(max(confidences), 3) if confidences else 0.0,
        },
        "timeline_buckets": [
            {"bucket": bucket, "count": count}
            for bucket, count in sorted(bucket_counts.items(), key=lambda i: str(i[0]))
        ],
        "retrieval_counts": {
            "query_rag": len(context_docs),
            "entity_graph_rag": len(entity_docs),
            "baseline_docs": len(baseline_docs),
        },
    }


def _fallback_narrative(
    events: list[dict],
    prev_events: list[dict],
    query: str,
) -> str:
    event_count = len(events)
    if event_count == 0:
        return (
            f"No events were emitted for query: \"{query}\".\n\n"
            "Either the evidence window is empty, or retrieval confidence was below threshold.\n\n"
            "Recommendation: widen the replay window or request fewer filters to increase context."
        )

    by_conf = sorted(
        [ev for ev in events if isinstance(ev, dict)],
        key=lambda ev: _coerce_confidence(ev.get("confidence")),
        reverse=True,
    )

    lines = [f"LLM generation was unavailable; generated fallback narrative for: {query}.", ""]
    top = by_conf[: min(5, event_count)]
    for idx, ev in enumerate(top, 1):
        axis = str(ev.get("axis") or "unknown").upper()
        sev = str(ev.get("severity") or "unknown").lower()
        conf = round(_coerce_confidence(ev.get("confidence")), 2)
        start = ev.get("start_time", "unknown")
        etype = str(ev.get("type") or "event")
        entities = [e.get("name") for e in (ev.get("entities") or []) if isinstance(e, dict) and e.get("name")]
        entity_text = f" entities={', '.join(entities)}" if entities else " entities unavailable"
        lines.append(f"{idx}. [{etype.upper()} | {axis} | {sev} | conf {conf}] @ {start} — {entity_text}.")

    lines.append("")
    if prev_events:
        lines.append(f"Previous-window baseline contained {len(prev_events)} event(s); compare for escalation/de-escalation.")
    else:
        lines.append("No previous-window baseline was available.")
    return "\n".join(lines)


def compute_previous_window_bounds(from_time: str, to_time: str) -> tuple[str, str]:
    from_dt = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
    if to_dt <= from_dt:
        raise ValueError("to_time must be later than from_time")
    window_delta = to_dt - from_dt
    prev_from = (from_dt - window_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev_to = from_time
    return prev_from, prev_to


async def summarise_events(events: list, question: str) -> tuple[str, str | None]:
    if not events:
        return "No events found in this window.", None

    prompt = f"""
You are an OSINT analyst reviewing fused sensor events.

Field glossary:
- confidence: 0.0–1.0 — proportion of evidence (0.2 = 1 observation, 1.0 = 5+). Low-confidence events are tentative.
- severity: low | medium | high — tiered by observation count; jamming is always high.
- axis: air | sea | cyber | multi — domain of the signal.
- type=correlation: a multi-feed event where ≥2 distinct feed_types fired in the same 10-minute window. Treat these as the highest-priority signals.

User question:
{question}

Events (JSON):
{events}

Write a concise, plain-language summary. One line per event cluster. Lead with correlation events if present.
Highlight axis, severity, confidence, and any notable entities. Flag low-confidence events (< 0.4) as tentative.
"""
    try:
        summary_text, model = await _invoke_llm(prompt)
        return summary_text, model
    except Exception as e:
        logger.error("summarise_events LLM call failed: %s", e)
        return f"Summary unavailable ({len(events)} events found). {type(e).__name__}: {e}", None


async def reconstruct_node(state: State) -> State:
    t_total = time.perf_counter()
    from_time = state["from_time"]
    to_time = state["to_time"]
    scenario = state.get("scenario") or "EPIC_FURY_DEMO"
    user_q = state.get("query") or "What happened?"
    thread_id = state.get("thread_id", "unknown")

    # Compute previous window bounds synchronously — inputs are Pydantic-validated ISO strings.
    prev_from, prev_to = compute_previous_window_bounds(from_time, to_time)
    logger.info("replay.phase1 start: thread_id=%s window=%s-%s", thread_id, from_time, to_time)
    t_phase_1 = time.perf_counter()

    # Phase 1: fusion (write) + vector search (read) concurrently.
    # return_exceptions=True preserves both results independently — a vector_search
    # failure must not discard a successful fuse_events side-effect, and vice versa.
    docs: list = []
    p1_fuse, p1_docs = await asyncio.gather(
        fuse_events.ainvoke({"from_time": from_time, "to_time": to_time, "region": state.get("region"), "scenario": scenario}),
        vector_search.ainvoke({"query": user_q, "k": 5}),
        return_exceptions=True,
    )
    if isinstance(p1_fuse, BaseException):
        logger.error("fuse_events failed: %s", p1_fuse)
    if isinstance(p1_docs, BaseException):
        logger.error("vector_search failed: %s", p1_docs)
    else:
        docs = p1_docs  # fuse_events result is intentionally unused (DB side-effect only)
    logger.info("replay.phase1 complete: thread_id=%s duration_ms=%.1f", thread_id, (time.perf_counter() - t_phase_1) * 1000)

    t_phase_2 = time.perf_counter()
    logger.info("replay.phase2 start: thread_id=%s", thread_id)

    docs_structured = docs[:3]
    docs_baseline   = docs

    # Phase 2: read current + previous timelines concurrently — both read-only after fusion.
    # return_exceptions=True ensures a stale/empty previous window doesn't lose the current one.
    p2_cur, p2_prev = await asyncio.gather(
        get_timeline.ainvoke({"from_time": from_time, "to_time": to_time, "scenario": scenario}),
        get_timeline.ainvoke({"from_time": prev_from, "to_time": prev_to, "scenario": scenario}),
        return_exceptions=True,
    )
    timeline = p2_cur if not isinstance(p2_cur, BaseException) else []
    prev_timeline = p2_prev if not isinstance(p2_prev, BaseException) else []
    if isinstance(p2_cur, BaseException):
        logger.error("current timeline fetch failed: %s", p2_cur)
    if isinstance(p2_prev, BaseException):
        logger.warning("previous timeline fetch failed (non-fatal): %s", p2_prev)
    logger.info("replay.phase2 complete: thread_id=%s duration_ms=%.1f", thread_id, (time.perf_counter() - t_phase_2) * 1000)

    t_phase_3 = time.perf_counter()
    logger.info("replay.phase3 start: thread_id=%s", thread_id)

    # Phase 3: entity-augmented Graph-RAG.
    # Extract names of entities detected by the event graph and re-query the doc_chunk
    # store using those names alongside the user question. A query like "what anomalies
    # occurred?" yields poor recall for NOTAMs that name a specific vessel — the graph
    # fills this gap by surfacing entity names the user didn't know to ask for.
    entity_docs: list = []
    entity_names = list({
        ent.get("name", "")
        for ev in timeline
        for ent in (ev.get("entities") or [])
        if isinstance(ent, dict) and ent.get("name")
    })
    if entity_names:
        entity_query = f"{user_q} {' '.join(entity_names)}"
        try:
            raw_entity_docs = await vector_search.ainvoke({"query": entity_query, "k": 3})
            seen_texts = {d["text"] for d in docs}
            entity_docs = [d for d in raw_entity_docs if d["text"] not in seen_texts]
        except Exception as e:
            logger.warning("Entity-augmented search failed (non-fatal): %s", e)
    logger.info("replay.phase3 complete: thread_id=%s duration_ms=%.1f", thread_id, (time.perf_counter() - t_phase_3) * 1000)

    # Optional graph annotations (agent + operator review context) are kept in
    # SurrealDB and fed into the narrative prompt as additional memory.
    event_annotations: list[dict] = []
    event_ids = [str(ev.get("id")) for ev in timeline if isinstance(ev, dict) and ev.get("id")]
    if event_ids:
        high_events = [
            str(ev["id"])
            for ev in timeline
            if isinstance(ev, dict) and ev.get("severity") == "high" and ev.get("id")
        ]
        if high_events:
            await asyncio.gather(
                *[
                    flag_suspicious_event.ainvoke(
                        {"event_id": ev_id, "reason": "Auto-flagged high-severity replay event"},
                    )
                    for ev_id in high_events
                ],
                return_exceptions=True,
            )

        event_annotations = await get_event_annotations.ainvoke({"event_ids": event_ids})

    timeline_metrics = _build_timeline_metrics(
        timeline,
        prev_timeline,
        docs_structured,
        entity_docs,
        docs_baseline,
        from_time,
        to_time,
    )
    return {
        **state,
        "events": timeline,
        "prev_events": prev_timeline,
        "context_docs": docs_structured,
        "baseline_docs": docs_baseline,
        "entity_docs": entity_docs,
        "event_annotations": event_annotations,
        "timeline_metrics": timeline_metrics,
        "runtime_metrics": {
            "phase1_ms": round((t_phase_2 - t_phase_1) * 1000, 2),
            "phase2_ms": round((t_phase_3 - t_phase_2) * 1000, 2),
            "phase3_ms": round((time.perf_counter() - t_phase_3) * 1000, 2),
            "total_reconstruct_ms": round((time.perf_counter() - t_total) * 1000, 2),
        },
    }


async def narrate_node(state: State) -> State:
    events = state.get("events", [])
    prev_events = state.get("prev_events", [])
    docs_structured = state.get("context_docs", [])
    docs_baseline = state.get("baseline_docs", [])
    entity_docs = state.get("entity_docs", [])
    user_q = state.get("query") or "Describe what happened."
    thread_id = state.get("thread_id", "unknown")
    annotations = state.get("event_annotations", [])

    prev_section = f"""
Structured path (previous window — for comparison):
- Events (JSON): {prev_events}
""" if prev_events else ""

    entity_section = f"""
Graph-RAG path (entity-augmented retrieval — docs retrieved using names of detected entities):
- Context docs: {entity_docs}
""" if entity_docs else ""

    # Build numbered instructions dynamically — sections only appear when they have data.
    instructions = [
        "1. Explain what happened in the current window. Lead with correlation events if present.\n"
        "   Weight your confidence in each claim by the event confidence score — flag anything below 0.4 as tentative."
    ]
    n = 2
    if prev_events:
        instructions.append(f"{n}. Compare to the previous window: what escalated, de-escalated, or is new.")
        n += 1
    if entity_docs:
        instructions.append(f"{n}. Note what information came from Graph-RAG (entity-linked docs) that query-RAG alone could not have surfaced.")
        n += 1
    if annotations:
        instructions.append(f"{n}. Integrate analyst/agent annotations where they alter confidence or escalation narrative.")
        n += 1
    instructions.append(f"{n}. Note what you could not have concluded using only the baseline RAG path.")
    instruction_block = "\n".join(instructions)

    prompt = f"""
You are an OSINT analyst reviewing a time-windowed replay.

Field glossary:
- confidence: 0.0–1.0 Noisy-OR evidence weight (0.2 = 1 obs tentative, 0.67 = 5 obs strong, approaches 1.0 asymptotically).
- severity: low | medium | high.
- axis: air | sea | cyber | multi.
- type=correlation: multi-feed co-occurrence in the same 10-minute bucket — highest priority.

User question:
{user_q}

Structured path (current window):
- Events (JSON): {events}
- Query-RAG docs (BM25+vector on user question): {docs_structured}
{entity_section}{prev_section}
Baseline path (RAG only, no event graph):
- Context docs: {docs_baseline}
Analyst/agent annotations:
{annotations}

{instruction_block}
"""

    # Both LLM calls are independent — run concurrently.
    # return_exceptions=True means a summary failure doesn't lose the narrative, and vice versa.
    t_llm = time.perf_counter()
    logger.info("replay.narrate start: thread_id=%s", thread_id)
    n_narrative, n_summary = await asyncio.gather(
        _invoke_llm(prompt),
        summarise_events(events, user_q),
        return_exceptions=True,
    )
    narrative_status = "ok"
    summary_status = "ok"
    narrative_status_reason = None
    summary_status_reason = None
    llm_model_used = None
    if isinstance(n_narrative, BaseException):
        logger.error("narrative LLM call failed: %s", n_narrative)
        narrative_status = "llm_unavailable"
        narrative = _fallback_narrative(events, prev_events, user_q)
        narrative_status_reason = f"{type(n_narrative).__name__}: {n_narrative}"
    else:
        narrative = str(n_narrative[0])
        llm_model_used = str(n_narrative[1])
    if isinstance(n_summary, BaseException):
        logger.error("summarise_events failed: %s", n_summary)
        summary_status = "llm_unavailable"
        event_summary = f"Summary unavailable ({len(events)} events found)."
        summary_status_reason = f"{type(n_summary).__name__}: {n_summary}"
    else:
        event_summary = str(n_summary[0])
        if llm_model_used is None and n_summary[1]:
            llm_model_used = str(n_summary[1])
        if isinstance(event_summary, str) and event_summary.startswith("Summary unavailable"):
            summary_status = "llm_unavailable"
            summary_status_reason = "LLM unavailable or timed out."
    runtime_metrics = dict(state.get("runtime_metrics") or {})
    runtime_metrics["narrate_ms"] = round((time.perf_counter() - t_llm) * 1000, 2)
    runtime_metrics["total_ms"] = round(runtime_metrics.get("total_reconstruct_ms", 0.0) + runtime_metrics["narrate_ms"], 2)
    logger.info("replay.narrate complete: thread_id=%s duration_ms=%.1f", thread_id, (time.perf_counter() - t_llm) * 1000)
    return {
        **state,
        "narrative": narrative,
        "event_summary": event_summary,
        "narrative_status": narrative_status,
        "summary_status": summary_status,
        "narrative_status_reason": narrative_status_reason,
        "summary_status_reason": summary_status_reason,
        "runtime_metrics": runtime_metrics,
        "llm_model_used": llm_model_used,
    }


def build_graph():
    if not is_llm_configured():
        raise RuntimeError("ANTHROPIC_API_KEY is required to run replay pipeline.")

    builder = StateGraph(State)
    builder.add_node("reconstruct_node", reconstruct_node)
    builder.add_node("narrate_node", narrate_node)

    builder.set_entry_point("reconstruct_node")
    builder.add_edge("reconstruct_node", "narrate_node")
    builder.add_edge("narrate_node", END)

    return builder.compile(
        checkpointer=SurrealDBCheckpointSaver(max_checkpoints_per_thread=_parse_retention_from_env())
    )
