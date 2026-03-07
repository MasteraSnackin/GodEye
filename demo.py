import asyncio
import hashlib

from src.agents.graph import build_graph
from src.agents.state import State

FROM_TIME = "2026-02-28T02:00:00Z"
TO_TIME = "2026-02-28T04:00:00Z"
REGION = "Hormuz"
SCENARIO = "EPIC_FURY_DEMO"


async def main():
    graph = build_graph()
    thread_id = hashlib.md5(f"{SCENARIO}:{FROM_TIME}:{TO_TIME}".encode()).hexdigest()

    initial_state: State = {
        "mode": "replay",
        "query": "What anomalies occurred between 02:00 and 04:00 UTC near Hormuz?",
        "from_time": FROM_TIME,
        "to_time": TO_TIME,
        "region": REGION,
        "scenario": SCENARIO,
        "thread_id": thread_id,
    }

    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": "replay"}},
    )

    print("\n=== GOD EYE DEMO ===\n")
    print("Narrative (structured vs baseline):\n")
    print(result["narrative"])

    print("\nEvent summary:\n")
    print(result.get("event_summary", ""))

    print("\nEvents (structured graph):\n")
    for ev in result.get("events", []):
        print(
            f"- {ev.get('id')} | {ev.get('type')} | axis={ev.get('axis')} | "
            f"severity={ev.get('severity')} | tags={ev.get('source_tags')}"
        )

    print("\nStructured RAG docs:\n")
    for d in result.get("context_docs", []):
        meta = d["metadata"]
        print(f"- {meta.get('source')} @ {meta.get('time')}: {d['text'][:80]}...")

    print("\nBaseline RAG docs (no events):\n")
    for d in result.get("baseline_docs", []):
        meta = d["metadata"]
        print(f"- {meta.get('source')} @ {meta.get('time')}: {d['text'][:80]}...")


if __name__ == "__main__":
    asyncio.run(main())
