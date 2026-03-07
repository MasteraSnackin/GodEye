import asyncio
from src.agents import tools as tools_module


class _FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.queries = []

    async def query(self, query_text: str, params: dict | None = None):
        self.queries.append((query_text, params or {}))
        result = self.results.pop(0)
        return [result]

    async def close(self):
        return None


def _make_client(results):
    async def _get_client():
        return _FakeClient(results)

    return _get_client


def test_flag_suspicious_event_creates_annotation(monkeypatch):
    results = [
        {"result": []},
        {"result": {}},
    ]
    monkeypatch.setattr(tools_module, "get_surreal_client", _make_client(results))

    outcome = asyncio.run(
        tools_module.flag_suspicious_event.ainvoke({"event_id": "event:1", "reason": "follow-up review"})
    )

    assert outcome["status"] == "flagged"
    assert outcome["event_id"] == "event:1"
    assert outcome["reason"] == "follow-up review"


def test_get_event_annotations_queries_table(monkeypatch):
    payload = {
        "result": [
            {"event_id": "event:1", "reason": "critical", "flagged_by": "agent", "flagged_at": "2026-03-07T00:00:00Z"},
            {"event_id": "event:2", "reason": "follow-up", "flagged_by": "agent", "flagged_at": "2026-03-07T00:00:00Z"},
        ]
    }
    monkeypatch.setattr(tools_module, "get_surreal_client", _make_client([payload]))

    rows = asyncio.run(
        tools_module.get_event_annotations.ainvoke({"event_ids": ["event:1", "event:2"]})
    )

    assert rows == payload["result"]
