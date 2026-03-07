import asyncio
import logging
import os
import threading
from collections import defaultdict
from typing import Optional, List

from surrealdb import AsyncSurreal
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

SURREAL_URL = os.getenv("SURREAL_URL", "ws://127.0.0.1:8000/rpc")
SURREAL_USER = os.getenv("SURREAL_USER", "root")
SURREAL_PASSWORD = os.getenv("SURREAL_PASSWORD", "root")
NS = "god_eye"
DB = "world"
DB_QUERY_TIMEOUT_SECONDS = 8.0

# all-mpnet-base-v2: 768-dim, MTEB STS 69.6 vs all-MiniLM-L6-v2's 63.3 (~10% better retrieval).
# Lazy-loaded on first vector_search call — avoids 2-5s startup delay and 420MB RAM if unused.
_embeddings: HuggingFaceEmbeddings | None = None
_embeddings_lock = threading.Lock()


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


def compute_noisy_or_confidence(obs_count: int) -> float:
    """Return diminishing evidence-weight confidence for a count of observations."""
    if obs_count <= 0:
        return 0.0
    return round(1.0 - 0.8 ** obs_count, 2)


def derive_event_severity(confidence: float, feed_type: str | None = None) -> str:
    """Translate evidence confidence into a coarse severity bucket."""
    if feed_type == "jamming" or confidence >= 0.67:
        return "high"
    if confidence >= 0.36:
        return "medium"
    return "low"


async def _query_with_timeout(db: AsyncSurreal, query: str, params: dict, timeout: float = DB_QUERY_TIMEOUT_SECONDS):
    return await asyncio.wait_for(db.query(query, params), timeout=timeout)


def _get_embeddings() -> HuggingFaceEmbeddings:
    global _embeddings
    if _embeddings is None:
        # Double-checked locking: run_in_executor spawns threads, so two concurrent cold-start
        # requests would both pass the outer None check and double-load the 420MB model.
        # The inner check inside the lock ensures only one thread initializes it.
        with _embeddings_lock:
            if _embeddings is None:
                _embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-mpnet-base-v2")
    return _embeddings


# ── Connection pool ───────────────────────────────────────────────────────────
# Eliminates per-call WebSocket handshake + auth (~50ms each).
# Pool returns connections on close() rather than terminating them.
# Benchmark: 3 tool calls/request × ~50ms saved = ~150ms off the hot path.
_pool: asyncio.Queue[AsyncSurreal] = asyncio.Queue(maxsize=5)


class _PooledConn:
    """Delegates all AsyncSurreal calls; returns connection to pool on close()."""
    __slots__ = ("_db", "_pool")

    def __init__(self, db: AsyncSurreal, pool: asyncio.Queue) -> None:
        self._db = db
        self._pool = pool

    def __getattr__(self, name: str):
        return getattr(self._db, name)

    async def close(self) -> None:
        try:
            self._pool.put_nowait(self._db)
        except asyncio.QueueFull:
            await self._db.close()


async def _create_conn() -> AsyncSurreal:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            db = AsyncSurreal(SURREAL_URL)
            await asyncio.wait_for(db.connect(), timeout=5.0)
            await asyncio.wait_for(db.signin({"username": SURREAL_USER, "password": SURREAL_PASSWORD}), timeout=5.0)
            await asyncio.wait_for(db.use(NS, DB), timeout=5.0)
            return db
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s
    raise RuntimeError(f"SurrealDB unavailable after 3 attempts ({SURREAL_URL})") from last_err


async def get_surreal_client() -> _PooledConn:
    try:
        db = _pool.get_nowait()
    except asyncio.QueueEmpty:
        db = await _create_conn()
    return _PooledConn(db, _pool)




async def log_agent_action(db: AsyncSurreal, agent: str, action: str, details: dict | None = None):
    # Non-fatal: observability writes must never abort business logic.
    try:
        await _query_with_timeout(
            db,
            """
            CREATE agent_log CONTENT {
                time: time::now(),
                agent: $agent,
                action: $action,
                details: $details
            };
            """,
            {"agent": agent, "action": action, "details": details or {}},
        )
    except Exception as e:
        logger.warning("agent_log write failed (non-fatal): %s", e)


@tool("fuse_events", return_direct=False)
async def fuse_events(from_time: str, to_time: str, region: Optional[str] = None, scenario: str = "EPIC_FURY_DEMO") -> dict:
    """
    Fuse observations into events grouped by feed_type and 10-minute buckets.
    Also links nearby entities via 'involves' edges.
    """
    db = await get_surreal_client()
    try:
        await log_agent_action(
            db,
            agent="fuser",
            action="start_fuse",
            details={"from": from_time, "to": to_time, "region": region, "scenario": scenario},
        )

        # Delete any existing events for this window so fusion is idempotent
        await _query_with_timeout(
            db,
            """
            DELETE event
            WHERE start_time >= <datetime>$from
              AND start_time < <datetime>$to
              AND scenario = $scenario;
            """,
            {"from": from_time, "to": to_time, "scenario": scenario},
        )

        res = await _query_with_timeout(
            db,
            """
            SELECT
                feed_type,
                time::floor(time, 10m) AS bucket_start,
                array::group(id) AS obs_ids
            FROM observation
            WHERE time >= <datetime>$from
              AND time < <datetime>$to
            GROUP BY feed_type, bucket_start;
            """,
            {"from": from_time, "to": to_time},
        )

        groups = _coerce_query_rows(res)
        created_events = []

        for g in groups:
            feed_type = g["feed_type"]
            bucket_start = g["bucket_start"]
            obs_ids = g["obs_ids"]

            axis = (
                "air" if feed_type == "adsb"
                else "sea" if feed_type == "ais"
                else "cyber" if feed_type in ("net", "jamming")
                else "multi"
            )
            etype = "jamming" if feed_type == "jamming" else "anomaly"

            obs_count = len(obs_ids)
            confidence = compute_noisy_or_confidence(obs_count)
            severity = derive_event_severity(confidence=confidence, feed_type=feed_type)

            try:
                ev_res = await _query_with_timeout(
                    db,
                    """
                    CREATE event CONTENT {
                        type: $etype,
                        start_time: <datetime>$start,
                        end_time: <datetime>$end,
                        confidence: $confidence,
                        source_tags: [$feed_type, 'auto-fuse'],
                        scenario: $scenario,
                        axis: $axis,
                        severity: $severity,
                        region: $region,
                        details: $details
                    };
                    """,
                    {
                        "etype": etype,
                        "start": bucket_start,
                        "end": to_time,
                        "feed_type": feed_type,
                        "axis": axis,
                        "severity": severity,
                        "confidence": confidence,
                        "scenario": scenario,
                        "region": region,
                        "details": {"region_name": region, "region": region} if region else None,
                    },
                )
                ev_rows = _coerce_query_rows(ev_res)
                if not ev_rows:
                    continue
                ev = ev_rows[0]
            except Exception as e:
                logger.error("Failed to create event for feed_type=%s bucket=%s: %s", feed_type, bucket_start, e)
                continue

            created_events.append(ev)

            # Link observations as evidence — batch all in one query
            if obs_ids:
                try:
                    await _query_with_timeout(
                        db,
                        "FOR $o IN $obs_ids { RELATE $ev->evidence->$o SET weight = 1.0; };",
                        {"ev": ev["id"], "obs_ids": obs_ids},
                    )
                except Exception as e:
                    logger.warning("Failed to link evidence for event %s: %s", ev["id"], e)

            # Link nearby entities as 'involves'
            try:
                ent_res = await _query_with_timeout(
                    db,
                    """
                    SELECT DISTINCT <-observed_in<-entity AS ents
                    FROM $obs_ids;
                    """,
                    {"obs_ids": obs_ids},
                )
                rows = _coerce_query_rows(ent_res)
                ents = [e for row in rows for e in (row.get("ents") or [])]
            except Exception as e:
                logger.warning("Failed to fetch entities for event %s: %s", ev["id"], e)
                ents = []

            if ents:
                ent_ids = [e["id"] for e in ents if isinstance(e, dict) and e.get("id")]
                if ent_ids:
                    try:
                        await _query_with_timeout(
                            db,
                            "FOR $e IN $ents_list { RELATE $ev->involves->$e SET role = 'asset'; };",
                            {"ev": ev["id"], "ents_list": ent_ids},
                        )
                    except Exception as e:
                        logger.warning("Failed to batch-link entities to event %s: %s", ev["id"], e)

        # Cross-feed correlation: if ≥2 distinct feed_types fired in the same 10m bucket,
        # create a single 'multi' axis correlation event representing the compound signal.
        # A GPS jamming burst co-occurring with an anomalous AIS track is qualitatively
        # different from either feed alone — this surfaces that relationship explicitly.
        by_bucket: dict = defaultdict(list)
        for g in groups:
            by_bucket[g["bucket_start"]].append(g)

        for bucket_start, bucket_groups in by_bucket.items():
            if len(bucket_groups) < 2:
                continue

            feed_types    = sorted({g["feed_type"] for g in bucket_groups})
            all_obs_ids   = [oid for g in bucket_groups for oid in g["obs_ids"]]
            obs_count     = len(all_obs_ids)
            corr_conf = compute_noisy_or_confidence(obs_count)  # Noisy-OR, consistent with per-feed confidence

            try:
                corr_res = await _query_with_timeout(
                    db,
                    """
                    CREATE event CONTENT {
                        type: 'correlation',
                        start_time: <datetime>$start,
                        end_time: <datetime>$end,
                        confidence: $confidence,
                        source_tags: $source_tags,
                        scenario: $scenario,
                        axis: 'multi',
                        severity: 'high',
                        region: $region,
                        details: $details
                    };
                    """,
                    {
                        "start": bucket_start,
                        "end": to_time,
                        "confidence": corr_conf,
                        "source_tags": feed_types + ["auto-correlate"],
                        "scenario": scenario,
                        "region": region,
                        "details": {"region_name": region, "region": region} if region else None,
                    },
                )
                corr_rows = _coerce_query_rows(corr_res)
                if not corr_rows:
                    continue
                corr_ev = corr_rows[0]
            except Exception as e:
                logger.warning("Failed to create correlation event for bucket %s: %s", bucket_start, e)
                continue

            created_events.append(corr_ev)

            if all_obs_ids:
                try:
                    await _query_with_timeout(
                        db,
                        "FOR $o IN $obs_ids { RELATE $ev->evidence->$o SET weight = 1.0; };",
                        {"ev": corr_ev["id"], "obs_ids": all_obs_ids},
                    )
                except Exception as e:
                    logger.warning("Failed to link evidence for correlation event %s: %s", corr_ev["id"], e)

        await log_agent_action(
            db,
            agent="fuser",
            action="end_fuse",
            details={"created_events": len(created_events)},
        )
        return {"events": created_events}
    except Exception as e:
        logger.exception("fuse_events failed: %s", e)
        return {"events": [], "error": str(e)}
    finally:
        await db.close()


@tool("get_timeline", return_direct=False)
async def get_timeline(from_time: str, to_time: str, scenario: Optional[str] = None) -> List[dict]:
    """
    Get events and their involved entities for a time window.
    Omits ->evidence->observation: raw observations are not used in prompts or the
    frontend, and the multi-hop traversal adds latency + bloats the LLM payload 5-10x.
    """
    db = await get_surreal_client()
    try:
        res = await _query_with_timeout(
            db,
            """
            SELECT
                *,
                ->involves->entity AS entities
            FROM event
            WHERE start_time >= <datetime>$from
              AND start_time < <datetime>$to
              AND scenario = $scenario;
            """,
            {"from": from_time, "to": to_time, "scenario": scenario or "EPIC_FURY_DEMO"},
        )
        return _coerce_query_rows(res)
    except Exception as e:
        logger.exception("get_timeline failed: %s", e)
        return []
    finally:
        await db.close()


@tool("get_high_sev_jamming_on_tankers", return_direct=False)
async def get_high_sev_jamming_on_tankers(from_time: str, to_time: str, scenario: str = "EPIC_FURY_DEMO") -> list:
    """
    Return high-severity jamming events where at least one involved entity is a ship (tanker-like).
    Walks the graph via involves->entity edges — a multi-hop SurrealDB knowledge-graph query.
    """
    db = await get_surreal_client()
    try:
        res = await _query_with_timeout(
            db,
            """
            SELECT
              e.id,
              e.type,
              e.start_time,
              e.end_time,
              e.axis,
              e.severity,
              e.source_tags,
              e->involves->entity AS ents,
              e->evidence->observation AS obs
            FROM event e
            WHERE e.start_time >= <datetime>$from
              AND e.start_time <  <datetime>$to
              AND e.scenario = $scenario
              AND e.type = "jamming"
              AND e.severity = "high"
              AND array::any(e->involves->entity, |$ent| { $ent.type = "ship" });
            """,
            {"from": from_time, "to": to_time, "scenario": scenario},
        )
        return _coerce_query_rows(res)
    except Exception as e:
        logger.exception("get_high_sev_jamming_on_tankers failed: %s", e)
        return []
    finally:
        await db.close()


@tool("vector_search", return_direct=False)
async def vector_search(query: str, k: int = 3) -> list:
    """
    Retrieve top-k doc_chunks using hybrid BM25 + vector search merged via
    Reciprocal Rank Fusion (Cormack et al. 2009). Outperforms either alone on
    domain-specific corpora, especially for keyword-heavy OSINT queries.
    """
    # embed_query is CPU-bound HuggingFace inference (~400-1000ms on CPU).
    # Calling it synchronously blocks the event loop, preventing fuse_events DB
    # queries from progressing during asyncio.gather in reconstruct_node.
    # run_in_executor offloads to the default ThreadPoolExecutor, keeping the
    # loop free — turning the serial gather into a genuinely concurrent one.
    try:
        loop = asyncio.get_running_loop()
        query_vec = await loop.run_in_executor(None, _get_embeddings().embed_query, query)
    except Exception as e:
        logger.error("Embedding failed for query=%r: %s", query, e)
        return []

    # Fetch more candidates per source so RRF has good coverage to merge from.
    fetch_k = k * 3

    db = await get_surreal_client()
    try:
        # BM25 and vector queries are independent — run concurrently.
        # return_exceptions=True: a failed BM25 index still allows vector results through (and vice versa).
        bm25_res, vec_res = await asyncio.gather(
            _query_with_timeout(
                db,
                "SELECT id, text, source, time FROM doc_chunk WHERE text @@ $query LIMIT $fetch_k;",
                {"query": query, "fetch_k": fetch_k},
            ),
            _query_with_timeout(
                db,
                f"SELECT id, text, source, time FROM doc_chunk WHERE embedding <|{fetch_k},COSINE|> $vec;",
                {"vec": query_vec},
            ),
            return_exceptions=True,
        )
        if isinstance(bm25_res, BaseException):
            logger.warning("BM25 search failed (falling back to vector only): %s", bm25_res)
            bm25_rows = []
        else:
            bm25_rows = _coerce_query_rows(bm25_res)
        if isinstance(vec_res, BaseException):
            logger.warning("Vector search failed (falling back to BM25 only): %s", vec_res)
            vec_rows = []
        else:
            vec_rows = _coerce_query_rows(vec_res)

        # Reciprocal Rank Fusion: score = sum(1 / (rrf_k + rank)) across both lists.
        # k=60 was tuned on TREC web corpora; lower values (20-40) increase rank
        # spread on short domain-specific corpora like this OSINT fixture.
        rrf_k = 30
        scores: dict[str, float] = {}
        docs:   dict[str, dict]  = {}

        for rank, row in enumerate(bm25_rows):
            doc_id = str(row["id"])
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
            docs[doc_id] = row

        for rank, row in enumerate(vec_rows):
            doc_id = str(row["id"])
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
            docs[doc_id] = row

        top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:k]
        return [
            {"text": docs[i]["text"], "metadata": {"source": docs[i].get("source"), "time": str(docs[i].get("time", ""))}}
            for i in top_ids
        ]
    except Exception as e:
        logger.exception("vector_search failed: %s", e)
        return []
    finally:
        await db.close()


@tool("flag_suspicious_event", return_direct=False)
async def flag_suspicious_event(event_id: str, reason: str, flagged_by: str = "agent") -> dict:
    """
    Persist a review annotation for a replay event. This allows operator input or
    automated passes to be preserved as structured graph state.
    """
    db = await get_surreal_client()
    try:
        existing = await _query_with_timeout(
            db,
            """
            SELECT id FROM event_annotation
            WHERE event_id = $event_id
              AND reason = $reason
            LIMIT 1;
            """,
            {"event_id": event_id, "reason": reason},
        )
        if _coerce_query_rows(existing):
            return {"status": "already_flagged", "event_id": event_id, "reason": reason}

        await _query_with_timeout(
            db,
            """
            CREATE event_annotation CONTENT {
                event_id: $event_id,
                reason: $reason,
                flagged_by: $flagged_by,
                flagged_at: time::now()
            };
            """,
            {"event_id": event_id, "reason": reason, "flagged_by": flagged_by},
        )
        return {"status": "flagged", "event_id": event_id, "reason": reason}
    except Exception as e:
        logger.warning("flag_suspicious_event failed for %s: %s", event_id, e)
        return {"status": "error", "event_id": event_id, "error": str(e)}
    finally:
        await db.close()


@tool("get_event_annotations", return_direct=False)
async def get_event_annotations(event_ids: List[str]) -> list:
    """
    Return structured annotations for a set of event IDs (manual flags, analyst notes).
    """
    if not event_ids:
        return []

    db = await get_surreal_client()
    try:
        res = await _query_with_timeout(
            db,
            """
            SELECT event_id, reason, flagged_by, flagged_at
            FROM event_annotation
            WHERE event_id IN $event_ids
            ORDER BY flagged_at DESC;
            """,
            {"event_ids": event_ids},
        )
        return _coerce_query_rows(res)
    except Exception as e:
        logger.warning("get_event_annotations failed: %s", e)
        return []
    finally:
        await db.close()
