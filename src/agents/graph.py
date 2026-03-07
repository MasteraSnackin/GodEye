import asyncio
import logging
import os
import time
from datetime import datetime

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
llm = ChatAnthropic(model="claude-sonnet-4-6")


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


def compute_previous_window_bounds(from_time: str, to_time: str) -> tuple[str, str]:
    from_dt = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
    if to_dt <= from_dt:
        raise ValueError("to_time must be later than from_time")
    window_delta = to_dt - from_dt
    prev_from = (from_dt - window_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev_to = from_time
    return prev_from, prev_to


async def summarise_events(events: list, question: str) -> str:
    if not events:
        return "No events found in this window."

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
        resp = await asyncio.wait_for(llm.ainvoke([HumanMessage(content=prompt)]), timeout=LLM_TIMEOUT_SECONDS)
        return resp.content
    except Exception as e:
        logger.error("summarise_events LLM call failed: %s", e)
        return f"Summary unavailable ({len(events)} events found)."


async def reconstruct_node(state: State) -> State:
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
    event_ids = [ev.get("id") for ev in timeline if isinstance(ev, dict) and ev.get("id")]
    if event_ids:
        high_events = [
            ev["id"]
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
    return {
        **state,
        "events": timeline,
        "prev_events": prev_timeline,
        "context_docs": docs_structured,
        "baseline_docs": docs_baseline,
        "entity_docs": entity_docs,
        "event_annotations": event_annotations,
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
        asyncio.wait_for(llm.ainvoke([HumanMessage(content=prompt)]), timeout=LLM_TIMEOUT_SECONDS),
        summarise_events(events, user_q),
        return_exceptions=True,
    )
    if isinstance(n_narrative, BaseException):
        logger.error("narrative LLM call failed: %s", n_narrative)
        narrative = "Narrative unavailable. Check server logs."
    else:
        narrative = n_narrative.content
    if isinstance(n_summary, BaseException):
        logger.error("summarise_events failed: %s", n_summary)
        event_summary = f"Summary unavailable ({len(events)} events found)."
    else:
        event_summary = n_summary
    logger.info("replay.narrate complete: thread_id=%s duration_ms=%.1f", thread_id, (time.perf_counter() - t_llm) * 1000)
    return {**state, "narrative": narrative, "event_summary": event_summary}


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
