from contextlib import contextmanager
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from api.replay import api as api_module


class _FakeSurrealClient:
    def __init__(self, result: list | None = None):
        self._result = result if result is not None else []
        self.queries = []

    async def query(self, _query: str, _params: dict | None = None):
        self.queries.append((_query, _params or {}))
        return [{"result": self._result}]

    async def close(self):
        return None


def _new_client(result=None):
    query_store = []

    async def _get_client():
        client = _FakeSurrealClient(result=result)
        client.queries = query_store
        return client

    return _get_client


def _new_client_with_capture(result=None, capture=None):
    if capture is None:
        capture = []

    async def _get_client():
        client = _FakeSurrealClient(result=result)
        capture.append(client)
        return client

    return _get_client


@contextmanager
def _set_api_keys(keys):
    prev = api_module.API_KEYS
    api_module.API_KEYS = set(keys)
    try:
        yield
    finally:
        api_module.API_KEYS = prev


def test_api_events_requires_key_when_configured(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        resp = client.get("/api/events")
        assert resp.status_code == 401

        orig = api_module.get_surreal_client
        monkeypatch.setattr(api_module, "get_surreal_client", _new_client([]))
        resp_ok = client.get("/api/events", headers={"X-API-Key": "abc"})
        assert resp_ok.status_code == 200
        assert resp_ok.json() == {"events": []}
        monkeypatch.setattr(api_module, "get_surreal_client", orig)

def test_api_replay_requires_key_and_invokes_graph(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)

        # Without key -> unauthorized.
        no_auth = client.post(
            "/api/replay",
            json={
                "from_time": "2026-03-07T10:00:00Z",
                "to_time": "2026-03-07T11:00:00Z",
                "scenario": "EPIC_FURY_DEMO",
                "query": "What happened?",
            },
        )
        assert no_auth.status_code == 401

        # Restore auth and execute graph path.
        graph_mock = AsyncMock()
        graph_mock.ainvoke = AsyncMock(return_value={
            "narrative": "narrative",
            "events": [{"id": "event:test"}],
            "event_summary": "summary",
        })
        monkeypatch.setattr(api_module, "graph", graph_mock)
        with_auth = client.post(
            "/api/replay",
            headers={"X-API-Key": "abc"},
            json={
                "from_time": "2026-03-07T10:00:00Z",
                "to_time": "2026-03-07T11:00:00Z",
                "scenario": "EPIC_FURY_DEMO",
                "query": "What happened?",
            },
        )
        assert with_auth.status_code == 200
        body = with_auth.json()
        assert body["events"][0]["id"] == "event:test"
        assert body["narrative"] == "narrative"
        called_state = graph_mock.ainvoke.await_args.args[0]
        assert isinstance(called_state, dict)
        assert "thread_id" in called_state


def test_api_events_rejects_invalid_iso(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        resp = client.get(
            "/api/events",
            headers={"X-API-Key": "abc"},
            params={"from_time": "not-a-time", "to_time": "2026-03-07T11:00:00Z"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "must be ISO 8601" in body["detail"]


def test_api_observations_rejects_invalid_iso(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        resp = client.get(
            "/api/observations",
            headers={"X-API-Key": "abc"},
            params={"from_time": "2026-13-99", "to_time": "2026-03-07T11:00:00Z"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "must be ISO 8601" in body["detail"]


def test_api_observations_filtering_and_shape(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        captures = []
        mocked_result = [
            {"id": "observation:1", "time": "2026-03-07T10:15:00Z", "feed_type": "jamming"},
            {"id": "observation:2", "time": "2026-03-07T10:25:00Z", "feed_type": "jamming"},
        ]
        monkeypatch.setattr(
            api_module,
            "get_surreal_client",
            _new_client_with_capture(mocked_result, captures),
        )

        resp = client.get(
            "/api/observations",
            headers={"X-API-Key": "abc"},
            params={
                "from_time": "2026-03-07T10:00:00Z",
                "to_time": "2026-03-07T11:00:00Z",
                "feed_type": "jamming",
                "limit": 10,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "observations" in body
        assert body["observations"] == mocked_result

        assert captures
        assert len(captures[0].queries) == 1
        query_text, params = captures[0].queries[0]
        assert "time >= <datetime>$from" in query_text
        assert "time < <datetime>$to" in query_text
        assert "feed_type = $feed_type" in query_text
        assert params["from"] == "2026-03-07T10:00:00Z"
        assert params["to"] == "2026-03-07T11:00:00Z"
        assert params["feed_type"] == "jamming"
        assert params["limit"] == 10


def test_api_observations_no_filters_allows_default_query(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        captures = []
        mocked_result = []
        monkeypatch.setattr(api_module, "get_surreal_client", _new_client_with_capture(mocked_result, captures))
        resp = client.get("/api/observations", headers={"X-API-Key": "abc"})
        assert resp.status_code == 200
        assert resp.json() == {"observations": []}
        query_text, params = captures[0].queries[0]
        assert query_text.strip().startswith("SELECT * FROM observation")
        assert "WHERE" not in query_text
        assert params == {"limit": 100}


def test_api_entities_filtering_and_shape(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        captures = []
        mocked_result = [
            {"id": "entity:ship_1", "name": "SIRIUS STAR", "type": "ship"},
            {"id": "entity:ship_2", "name": "ORION", "type": "ship"},
        ]
        monkeypatch.setattr(
            api_module,
            "get_surreal_client",
            _new_client_with_capture(mocked_result, captures),
        )
        resp = client.get(
            "/api/entities",
            headers={"X-API-Key": "abc"},
            params={"entity_type": "ship"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entities"] == mocked_result
        query_text, params = captures[0].queries[0]
        assert "WHERE type = $entity_type" in query_text
        assert params["entity_type"] == "ship"


def test_api_entities_all_shape_no_filter(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        captures = []
        mocked_result = [
            {"id": "entity:air_1", "name": "ALPHA", "type": "aircraft"},
        ]
        monkeypatch.setattr(api_module, "get_surreal_client", _new_client_with_capture(mocked_result, captures))
        resp = client.get("/api/entities", headers={"X-API-Key": "abc"})
        assert resp.status_code == 200
        assert resp.json()["entities"] == mocked_result
        query_text, _ = captures[0].queries[0]
        assert query_text.strip().startswith("SELECT * FROM entity ORDER BY name;")
        assert "WHERE" not in query_text


def test_api_scenarios_returns_sorted_unique(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)
        captures = []
        db_rows = [{"scenario": "EPIC_FURY_DEMO"}, {"scenario": "TEST"}, {"scenario": "EPIC_FURY_DEMO"}]
        monkeypatch.setattr(
            api_module,
            "get_surreal_client",
            _new_client_with_capture(db_rows, captures),
        )
        resp = client.get("/api/scenarios", headers={"X-API-Key": "abc"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenarios"] == ["EPIC_FURY_DEMO", "TEST"]


def test_api_checkpoints_requires_key_and_returns_history(monkeypatch):
    with _set_api_keys({"abc"}):
        client = TestClient(api_module.app)

        # Without key -> unauthorized.
        no_auth = client.get("/api/checkpoints", params={"thread_id": "thread-1"})
        assert no_auth.status_code == 401

        class _Checkpoint:
            def __init__(self, checkpoint_id: str, parent_id: str | None, metadata: dict):
                self.config = {"configurable": {"checkpoint_id": checkpoint_id}}
                self.parent_config = {"configurable": {"checkpoint_id": parent_id}} if parent_id else None
                self.metadata = metadata

        class _Saver:
            async def alist(self, *_args, **_kwargs):
                yield _Checkpoint("cp-2", "cp-1", {"source": "graph"})
                yield _Checkpoint("cp-1", None, {"source": "graph"})

        monkeypatch.setattr(api_module, "SurrealDBCheckpointSaver", lambda *_a, **_k: _Saver())

        resp = client.get(
            "/api/checkpoints",
            headers={"X-API-Key": "abc"},
            params={"thread_id": "thread-1", "checkpoint_ns": "replay", "limit": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["thread_id"] == "thread-1"
        assert body["checkpoint_ns"] == "replay"
        assert body["checkpoints"] == [
            {"checkpoint_id": "cp-2", "parent_checkpoint_id": "cp-1", "metadata": {"source": "graph"}},
            {"checkpoint_id": "cp-1", "parent_checkpoint_id": None, "metadata": {"source": "graph"}},
        ]


def test_health_reports_db_status(monkeypatch):
    # Degraded DB state.
    async def fail_client():
        raise RuntimeError("db down")

    # using TestClient captures async dependency call path exactly as runtime
    client = TestClient(api_module.app)
    monkeypatch.setattr(api_module, "get_surreal_client", fail_client)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "DB unavailable. Check server logs."


def test_health_schema_when_llm_missing(monkeypatch):
    async def ok_client():
        class _C:
            async def query(self, _q):
                return [{"result": [1]}]

            async def close(self):
                return None

        return _C()

    monkeypatch.setattr(api_module, "get_surreal_client", ok_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with _set_api_keys(set()):
        client = TestClient(api_module.app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"status", "db", "llm"}
        assert body["status"] == "degraded"
        assert body["db"] == "connected"
        assert body["llm"] == "missing_api_key"
