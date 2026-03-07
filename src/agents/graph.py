import asyncio
import logging
from datetime import datetime

from langgraph.graph import StateGraph, END
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from .state import State
from .tools import fuse_events, get_timeline, vector_search

logger = logging.getLogger(__name__)

llm = ChatAnthropic(model="claude-sonnet-4-6")


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
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return resp.content
    except Exception as e:
        logger.error("summarise_events LLM call failed: %s", e)
        return f"Summary unavailable ({len(events)} events found)."


async def reconstruct_node(state: State) -> State:
    from_time = state["from_time"]
    to_time = state["to_time"]
    scenario = state.get("scenario") or "EPIC_FURY_DEMO"
    user_q = state.get("query") or "What happened?"

    # Compute previous window bounds synchronously — inputs are Pydantic-validated ISO strings.
    from_dt   = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
    to_dt     = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
    prev_from = (from_dt - (to_dt - from_dt)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Phase 1: fusion (write) + vector search (read) concurrently.
    # vector_search is independent of fusion and safe to run alongside it.
    docs: list = []
    try:
        _, docs = await asyncio.gather(
            fuse_events.ainvoke({"from_time": from_time, "to_time": to_time, "region": state.get("region"), "scenario": scenario}),
            vector_search.ainvoke({"query": user_q, "k": 5}),
        )  # fuse_events is called for its DB side-effect; get_timeline below is the authoritative read
    except Exception as e:
        logger.error("reconstruct_node fusion/search failed: %s", e)

    docs_structured = docs[:3]
    docs_baseline   = docs

    # Phase 2: read current + previous timelines concurrently — both read-only after fusion.
    try:
        timeline, prev_timeline = await asyncio.gather(
            get_timeline.ainvoke({"from_time": from_time, "to_time": to_time, "scenario": scenario}),
            get_timeline.ainvoke({"from_time": prev_from, "to_time": from_time, "scenario": scenario}),
        )
    except Exception as e:
        logger.error("reconstruct_node timeline fetch failed: %s", e)
        timeline, prev_timeline = [], []

    return {
        **state,
        "events": timeline,
        "prev_events": prev_timeline,
        "context_docs": docs_structured,
        "baseline_docs": docs_baseline,
    }


async def narrate_node(state: State) -> State:
    events = state.get("events", [])
    prev_events = state.get("prev_events", [])
    docs_structured = state.get("context_docs", [])
    docs_baseline = state.get("baseline_docs", [])
    user_q = state.get("query") or "Describe what happened."

    prev_section = f"""
Structured path (previous window — for comparison):
- Events (JSON): {prev_events}
""" if prev_events else ""

    prompt = f"""
You are an OSINT analyst reviewing a time-windowed replay.

Field glossary:
- confidence: 0.0–1.0 evidence weight (0.2 = tentative, 1.0 = well-supported).
- severity: low | medium | high.
- axis: air | sea | cyber | multi.
- type=correlation: multi-feed co-occurrence in the same 10-minute bucket — highest priority.

User question:
{user_q}

Structured path (current window):
- Events (JSON): {events}
- Context docs (RAG, hybrid BM25+vector): {docs_structured}
{prev_section}
Baseline path (RAG only, no event graph):
- Context docs: {docs_baseline}

1. Explain what happened in the current window. Lead with correlation events if present.
   Weight your confidence in each claim by the event's confidence score — flag anything below 0.4 as tentative.
{"2. Compare to the previous window: what escalated, de-escalated, or is new." + chr(10) if prev_events else ""}{"3" if prev_events else "2"}. Note what you could not have concluded using only the baseline RAG path.
"""

    try:
        # Both LLM calls are independent — run concurrently
        narrative_resp, event_summary = await asyncio.gather(
            llm.ainvoke([HumanMessage(content=prompt)]),
            summarise_events(events, user_q),
        )
        narrative = narrative_resp.content
    except Exception as e:
        logger.error("narrate_node LLM call failed: %s", e)
        narrative = "Narrative unavailable. Check server logs."
        event_summary = f"Summary unavailable ({len(events)} events found)."
    return {**state, "narrative": narrative, "event_summary": event_summary}


def build_graph():
    builder = StateGraph(State)
    builder.add_node("reconstruct_node", reconstruct_node)
    builder.add_node("narrate_node", narrate_node)

    builder.set_entry_point("reconstruct_node")
    builder.add_edge("reconstruct_node", "narrate_node")
    builder.add_edge("narrate_node", END)

    return builder.compile()
