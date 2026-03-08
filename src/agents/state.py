from typing import Optional, TypedDict, List


class State(TypedDict, total=False):
    mode: str
    thread_id: Optional[str]
    query: Optional[str]
    from_time: Optional[str]
    to_time: Optional[str]
    region: Optional[str]
    scenario: Optional[str]

    events: Optional[List[dict]]
    prev_events: Optional[List[dict]]
    context_docs: Optional[List[dict]]
    baseline_docs: Optional[List[dict]]
    entity_docs: Optional[List[dict]]
    event_annotations: Optional[List[dict]]
    event_summary: Optional[str]
    narrative: Optional[str]
    narrative_status: Optional[str]
    summary_status: Optional[str]
    narrative_status_reason: Optional[str]
    summary_status_reason: Optional[str]
    timeline_metrics: Optional[dict]
    runtime_metrics: Optional[dict]
    llm_model_used: Optional[str]
