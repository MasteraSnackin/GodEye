from typing import Optional, TypedDict, List


class State(TypedDict, total=False):
    mode: str
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
    event_summary: Optional[str]
    narrative: Optional[str]
