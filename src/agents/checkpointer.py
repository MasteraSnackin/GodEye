import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
)

from .tools import get_surreal_client


def _coerce_query_rows(result: object) -> list[dict]:
    if result is None:
        return []
    if isinstance(result, list):
        if len(result) == 1 and isinstance(result[0], dict) and "result" in result[0]:
            payload = result[0].get("result", [])
            return payload if isinstance(payload, list) else ([payload] if payload else [])
        return [row for row in result if row is not None and isinstance(row, dict)]
    if isinstance(result, dict) and "result" in result:
        payload = result.get("result", [])
        return payload if isinstance(payload, list) else ([payload] if payload else [])
    return []


class SurrealDBCheckpointSaver(BaseCheckpointSaver):
    """Persist LangGraph checkpoints in SurrealDB for resumable multi-step workflows."""

    def __init__(self, timeout_seconds: float = 20.0, max_checkpoints_per_thread: int | None = None):
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.max_checkpoints_per_thread = max_checkpoints_per_thread

    @staticmethod
    def _run_sync(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        if not loop.is_running():
            return loop.run_until_complete(coro)

        raise RuntimeError("Synchronous checkpoint API called from a running event loop. Use async methods.")

    @staticmethod
    def _checkpoint_config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    @staticmethod
    def _extract_id(config: RunnableConfig) -> tuple[str, str, str | None]:
        cfg = config.get("configurable", {})
        thread_id = cfg["thread_id"]
        checkpoint_ns = cfg.get("checkpoint_ns") or "replay"
        checkpoint_id = cfg.get("checkpoint_id")
        return thread_id, checkpoint_ns, checkpoint_id

    async def _query(self, query: str, params: dict[str, Any]) -> list[dict]:
        db = await get_surreal_client()
        try:
            result = await asyncio.wait_for(db.query(query, params), timeout=self.timeout_seconds)
            return _coerce_query_rows(result)
        finally:
            await db.close()

    async def _store_checkpoint(self, payload: dict[str, Any]) -> None:
        thread_id = payload["thread_id"]
        checkpoint_ns = payload["checkpoint_ns"]
        checkpoint_id = payload["checkpoint_id"]
        created_at = payload["created_at"]
        parent_checkpoint_id = payload["parent_checkpoint_id"]
        checkpoint = payload["checkpoint"]
        metadata = payload["metadata"]
        versions = payload["versions"]
        writes = payload.get("writes", [])

        await self._query(
            """
            INSERT INTO agent_checkpoint (
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                parent_checkpoint_id,
                created_at,
                checkpoint,
                metadata,
                versions,
                writes
            ) VALUES (
                $thread_id,
                $checkpoint_ns,
                $checkpoint_id,
                $parent_checkpoint_id,
                $created_at,
                $checkpoint,
                $metadata,
                $versions,
                $writes
            );
            """,
            {
                "payload": payload,
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": parent_checkpoint_id,
                "created_at": created_at,
                "checkpoint": checkpoint,
                "metadata": metadata,
                "versions": versions,
                "writes": writes,
            },
        )

    async def _enforce_retention(self, thread_id: str, checkpoint_ns: str) -> None:
        if self.max_checkpoints_per_thread is None:
            return
        if self.max_checkpoints_per_thread <= 0:
            await self._query("DELETE agent_checkpoint WHERE thread_id = $thread_id AND checkpoint_ns = $checkpoint_ns;", {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            })
            return

        rows = await self._query(
            """
            SELECT * FROM agent_checkpoint
            WHERE thread_id = $thread_id
              AND checkpoint_ns = $checkpoint_ns
            ORDER BY created_at DESC;
            """,
            {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns},
        )
        if len(rows) <= self.max_checkpoints_per_thread:
            return

        for row in rows[self.max_checkpoints_per_thread :]:
            cid = row.get("checkpoint_id")
            if not cid:
                continue
            await self._query(
                """
                DELETE agent_checkpoint
                WHERE thread_id = $thread_id
                  AND checkpoint_id = $checkpoint_id;
                """,
                {"thread_id": thread_id, "checkpoint_id": cid},
            )

    @staticmethod
    def get_next_version(current, channel: None):
        del channel
        if current is None:
            return 1
        if isinstance(current, int):
            return current + 1
        if isinstance(current, float):
            return current + 1
        if isinstance(current, str) and current.isdigit():
            return str(int(current) + 1)
        raise ValueError(f"Unsupported checkpoint version type: {type(current)!r}")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        _ = new_versions
        self._run_sync(self.aput(config, checkpoint, metadata, new_versions))
        thread_id, checkpoint_ns, _ = self._extract_id(config)
        return self._checkpoint_config(thread_id, checkpoint_ns, checkpoint["id"])

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns, parent_checkpoint_id = self._extract_id(config)
        checkpoint_id = checkpoint["id"]
        await self._store_checkpoint(
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "parent_checkpoint_id": parent_checkpoint_id,
                "created_at": datetime.now(UTC).isoformat(),
                "checkpoint": checkpoint,
                "metadata": dict(metadata),
                "versions": dict(new_versions),
                "writes": [],
            }
        )
        await self._enforce_retention(thread_id, checkpoint_ns)
        return self._checkpoint_config(thread_id, checkpoint_ns, checkpoint_id)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._run_sync(self.aget_tuple(config))

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns, checkpoint_id = self._extract_id(config)
        if checkpoint_id:
            rows = await self._query(
                """
                SELECT * FROM agent_checkpoint
                WHERE thread_id = $thread_id
                  AND checkpoint_ns = $checkpoint_ns
                  AND checkpoint_id = $checkpoint_id
                LIMIT 1;
                """,
                {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id},
            )
        else:
            rows = await self._query(
                """
                SELECT * FROM agent_checkpoint
                WHERE thread_id = $thread_id
                  AND checkpoint_ns = $checkpoint_ns
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns},
            )

        if not rows:
            return None
        row = rows[0]
        parent_id = row.get("parent_checkpoint_id")
        return CheckpointTuple(
            config=self._checkpoint_config(thread_id, checkpoint_ns, row.get("checkpoint_id", "")),
            checkpoint=row.get("checkpoint", {}),
            metadata=row.get("metadata", {}),
            pending_writes=[],
            parent_config=(
                self._checkpoint_config(thread_id, checkpoint_ns, parent_id)
                if parent_id
                else None
            ),
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._run_sync(self.aput_writes(config, writes, task_id, task_path))

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        return None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ):
        del filter, before
        if config is None:
            return iter(())

        async def _consume() -> list[CheckpointTuple]:
            return [item async for item in self.alist(config, limit=limit)]

        return iter(self._run_sync(_consume()))

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        del filter, before
        if config is None:
            return
            yield  # pragma: no cover

        thread_id, checkpoint_ns, _ = self._extract_id(config)
        rows = await self._query(
            """
            SELECT * FROM agent_checkpoint
            WHERE thread_id = $thread_id
              AND checkpoint_ns = $checkpoint_ns
            ORDER BY created_at DESC;
            """,
            {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns},
        )

        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            checkpoint_id = row.get("checkpoint_id")
            if not checkpoint_id:
                continue
            parent_id = row.get("parent_checkpoint_id")
            yield CheckpointTuple(
                config=self._checkpoint_config(thread_id, checkpoint_ns, str(checkpoint_id)),
                checkpoint=row.get("checkpoint", {}),
                metadata=row.get("metadata", {}),
                pending_writes=[],
                parent_config=self._checkpoint_config(thread_id, checkpoint_ns, parent_id) if parent_id else None,
            )
            count += 1

    def delete_thread(self, thread_id: str) -> None:
        self._run_sync(self.adelete_thread(thread_id))

    async def adelete_thread(self, thread_id: str) -> None:
        await self._query("DELETE agent_checkpoint WHERE thread_id = $thread_id;", {"thread_id": thread_id})

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self._run_sync(self.acopy_thread(source_thread_id, target_thread_id))

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        rows = await self._query(
            """
            SELECT * FROM agent_checkpoint
            WHERE thread_id = $thread_id
            ORDER BY created_at ASC;
            """,
            {"thread_id": source_thread_id},
        )
        if not rows:
            return
        for row in rows:
            source_id = row.get("checkpoint_id")
            if not source_id:
                continue
            copied = dict(row)
            copied["thread_id"] = target_thread_id
            copied["checkpoint_id"] = f"{source_id}-copy-{target_thread_id}"
            copied["created_at"] = datetime.now(UTC).isoformat()
            copied.pop("id", None)
            await self._store_checkpoint(copied)

    def prune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        self._run_sync(self.aprune(thread_ids, strategy=strategy))

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        if not thread_ids:
            return
        for thread_id in thread_ids:
            if strategy == "delete":
                await self._query("DELETE agent_checkpoint WHERE thread_id = $thread_id;", {"thread_id": thread_id})
                continue

            rows = await self._query(
                """
                SELECT * FROM agent_checkpoint
                WHERE thread_id = $thread_id
                ORDER BY created_at DESC;
                """,
                {"thread_id": thread_id},
            )
            if len(rows) <= 1:
                continue
            for row in rows[1:]:
                cid = row.get("checkpoint_id")
                if not cid:
                    continue
                await self._query(
                    """
                    DELETE agent_checkpoint
                    WHERE thread_id = $thread_id
                      AND checkpoint_id = $checkpoint_id;
                    """,
                    {"thread_id": thread_id, "checkpoint_id": cid},
                )
