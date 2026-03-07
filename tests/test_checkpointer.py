from __future__ import annotations

from src.agents.checkpointer import SurrealDBCheckpointSaver


def test_surrealdb_checkpoint_saver_persists_and_restores_checkpoint():
    stored: list[dict] = []

    async def fake_query(query: str, params: dict) -> list[dict]:
        if query.strip().startswith("INSERT INTO agent_checkpoint"):
            stored.append(params["payload"])
            return []

        if "checkpoint_id = $checkpoint_id" in query:
            thread_id = params["thread_id"]
            namespace = params["checkpoint_ns"]
            cp_id = params["checkpoint_id"]
            found = [
                row
                for row in stored
                if row.get("thread_id") == thread_id and row.get("checkpoint_ns") == namespace and row.get("checkpoint_id") == cp_id
            ]
            return found[:1]

        if "ORDER BY created_at DESC" in query:
            thread_id = params["thread_id"]
            namespace = params["checkpoint_ns"]
            rows = [
                row
                for row in stored
                if row.get("thread_id") == thread_id and row.get("checkpoint_ns") == namespace
            ]
            rows.sort(key=lambda row: row.get("created_at", ""))
            return list(reversed(rows))

        return []

    saver = SurrealDBCheckpointSaver(timeout_seconds=1.0)
    saver._query = fake_query  # type: ignore[assignment]

    base_config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "replay"}}
    cp1 = {"id": "cp-1", "channel_values": {"step": 1}}
    saver.put(base_config, cp1, {"step": "start"}, {"vals": 1})

    tuple_latest = saver.get_tuple({"configurable": {"thread_id": "thread-1", "checkpoint_ns": "replay"}})
    assert tuple_latest is not None
    assert tuple_latest.checkpoint["id"] == "cp-1"
    assert tuple_latest.config["configurable"]["checkpoint_id"] == "cp-1"

    cp2 = {"id": "cp-2", "channel_values": {"step": 2}}
    saver.put(
        {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "replay", "checkpoint_id": "cp-1"}},
        cp2,
        {"step": "continue"},
        {"vals": 2},
    )

    rows = list(saver.list({"configurable": {"thread_id": "thread-1", "checkpoint_ns": "replay"}}))
    assert len(rows) == 2
    assert rows[0].checkpoint["id"] == "cp-2"
    assert rows[1].checkpoint["id"] == "cp-1"
    assert rows[0].parent_config is not None
    assert rows[0].parent_config["configurable"]["checkpoint_id"] == "cp-1"


def test_surrealdb_checkpoint_saver_enforces_retention():
    stored: list[dict] = []

    async def fake_query(query: str, params: dict) -> list[dict]:
        if query.strip().startswith("INSERT INTO agent_checkpoint"):
            stored.append(params["payload"])
            return []

        if query.strip().startswith("DELETE agent_checkpoint") and "checkpoint_ns" in query and "checkpoint_id" not in query:
            thread_id = params["thread_id"]
            namespace = params["checkpoint_ns"]
            stored[:] = [
                row
                for row in stored
                if not (row.get("thread_id") == thread_id and row.get("checkpoint_ns") == namespace)
            ]
            return []

        if query.strip().startswith("DELETE agent_checkpoint") and "checkpoint_id" in query:
            thread_id = params["thread_id"]
            checkpoint_id = params["checkpoint_id"]
            stored[:] = [
                row
                for row in stored
                if not (row.get("thread_id") == thread_id and row.get("checkpoint_id") == checkpoint_id)
            ]
            return []

        if "ORDER BY created_at DESC" in query:
            thread_id = params["thread_id"]
            namespace = params["checkpoint_ns"]
            rows = [
                row
                for row in stored
                if row.get("thread_id") == thread_id and row.get("checkpoint_ns") == namespace
            ]
            rows.sort(key=lambda row: row.get("created_at", ""))
            return list(reversed(rows))

        if "ORDER BY created_at ASC" in query:
            thread_id = params["thread_id"]
            rows = [row for row in stored if row.get("thread_id") == thread_id]
            rows.sort(key=lambda row: row.get("created_at", ""))
            return rows

        if "checkpoint_id = $checkpoint_id" in query:
            thread_id = params["thread_id"]
            namespace = params["checkpoint_ns"]
            cp_id = params["checkpoint_id"]
            found = [
                row
                for row in stored
                if row.get("thread_id") == thread_id and row.get("checkpoint_ns") == namespace and row.get("checkpoint_id") == cp_id
            ]
            return found[:1]

        return []

    saver = SurrealDBCheckpointSaver(timeout_seconds=1.0, max_checkpoints_per_thread=2)
    saver._query = fake_query  # type: ignore[assignment]

    config = {"configurable": {"thread_id": "thread-retain", "checkpoint_ns": "replay"}}
    saver.put(config, {"id": "cp-1", "channel_values": {"step": 1}}, {"step": "start"}, {"vals": 1})
    saver.put(
        {"configurable": {"thread_id": "thread-retain", "checkpoint_ns": "replay", "checkpoint_id": "cp-1"}},
        {"id": "cp-2", "channel_values": {"step": 2}},
        {"step": "mid"},
        {"vals": 1},
    )
    saver.put(
        {"configurable": {"thread_id": "thread-retain", "checkpoint_ns": "replay", "checkpoint_id": "cp-2"}},
        {"id": "cp-3", "channel_values": {"step": 3}},
        {"step": "end"},
        {"vals": 1},
    )

    rows = list(saver.list(config))
    assert len(rows) == 2
    assert rows[0].checkpoint["id"] == "cp-3"
    assert rows[1].checkpoint["id"] == "cp-2"

    saver2 = SurrealDBCheckpointSaver(timeout_seconds=1.0, max_checkpoints_per_thread=0)
    saver2._query = fake_query  # type: ignore[assignment]
    saver2.put(
        {"configurable": {"thread_id": "thread-zero", "checkpoint_ns": "replay"}},
        {"id": "cp-zero-1", "channel_values": {"step": 1}},
        {"step": "start"},
        {"vals": 1},
    )
    rows_zero = list(saver2.list({"configurable": {"thread_id": "thread-zero", "checkpoint_ns": "replay"}}))
    assert len(rows_zero) == 0
