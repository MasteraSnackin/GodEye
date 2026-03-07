import asyncio
import logging
import os
from collections import defaultdict
from typing import Optional, List

from surrealdb import AsyncSurreal
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

SURREAL_URL = os.getenv("SURREAL_URL", "ws://127.0.0.1:8000/rpc")
NS = "god_eye"
DB = "world"

# all-mpnet-base-v2: 768-dim, MTEB STS 69.6 vs all-MiniLM-L6-v2's 63.3 (~10% better retrieval).
# Lazy-loaded on first vector_search call — avoids 2-5s startup delay and 420MB RAM if unused.
_embeddings: HuggingFaceEmbeddings | None = None


def _get_embeddings() -> HuggingFaceEmbeddings:
    global _embeddings
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
            await db.connect()
            await db.signin({"username": "root", "password": "root"})
            await db.use(NS, DB)
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
        await db.query(
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
        await db.query(
            """
            DELETE event
            WHERE start_time >= <datetime>$from
              AND start_time < <datetime>$to
              AND scenario = $scenario;
            """,
            {"from": from_time, "to": to_time, "scenario": scenario},
        )

        res = await db.query(
            """
            LET $rows = SELECT
                feed_type,
                time::floor(time, 10m) AS bucket_start,
                array::group(id) AS obs_ids
            FROM observation
            WHERE time >= <datetime>$from
              AND time < <datetime>$to
            GROUP BY feed_type, bucket_start;

            RETURN $rows;
            """,
            {"from": from_time, "to": to_time},
        )

        groups = res[1]["result"] if len(res) > 1 and res[1]["result"] else []
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

            # Confidence scales with evidence count: more observations = higher confidence.
            # Linear scale: 1 obs = 0.2, 5+ obs = 1.0.
            # (True D-S conjunctive rule would give 1-(1-b)^n, saturating more slowly.)
            obs_count = len(obs_ids)
            confidence = round(min(1.0, obs_count / 5.0), 2)

            # Severity tiered by count; jamming always high regardless of count.
            if feed_type == "jamming" or obs_count >= 5:
                severity = "high"
            elif obs_count >= 2:
                severity = "medium"
            else:
                severity = "low"

            try:
                ev_res = await db.query(
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
                        "details": {"region_name": region} if region else None,
                    },
                )
                ev = ev_res[0]["result"][0]
            except Exception as e:
                logger.error("Failed to create event for feed_type=%s bucket=%s: %s", feed_type, bucket_start, e)
                continue

            created_events.append(ev)

            # Link observations as evidence — batch all in one query
            if obs_ids:
                try:
                    await db.query(
                        "FOR $o IN $obs_ids { RELATE $ev->evidence->$o SET weight = 1.0; };",
                        {"ev": ev["id"], "obs_ids": obs_ids},
                    )
                except Exception as e:
                    logger.warning("Failed to link evidence for event %s: %s", ev["id"], e)

            # Link nearby entities as 'involves'
            try:
                ent_res = await db.query(
                    """
                    SELECT DISTINCT <-observed_in<-entity AS ents
                    FROM $obs_ids;
                    """,
                    {"obs_ids": obs_ids},
                )
                rows = ent_res[0]["result"] if ent_res and ent_res[0]["result"] else []
                ents = [e for row in rows for e in (row.get("ents") or [])]
            except Exception as e:
                logger.warning("Failed to fetch entities for event %s: %s", ev["id"], e)
                ents = []

            if ents:
                ent_ids = [e["id"] for e in ents if isinstance(e, dict) and e.get("id")]
                if ent_ids:
                    try:
                        await db.query(
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
            corr_conf     = round(min(1.0, obs_count / 5.0), 2)

            try:
                corr_res = await db.query(
                    """
                    CREATE event CONTENT {
                        type: 'correlation',
                        start_time: <datetime>$start,
                        end_time: <datetime>$end,
                        confidence: $confidence,
                        source_tags: $source_tags,
                        scenario: $scenario,
                        axis: 'multi',
                        severity: 'high'
                    };
                    """,
                    {
                        "start": bucket_start,
                        "end": to_time,
                        "confidence": corr_conf,
                        "source_tags": feed_types + ["auto-correlate"],
                        "scenario": scenario,
                    },
                )
                corr_ev = corr_res[0]["result"][0]
            except Exception as e:
                logger.warning("Failed to create correlation event for bucket %s: %s", bucket_start, e)
                continue

            created_events.append(corr_ev)

            if all_obs_ids:
                try:
                    await db.query(
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
    Get events and their linked observations and entities for a window.
    """
    db = await get_surreal_client()
    try:
        res = await db.query(
            """
            SELECT
                *,
                ->evidence->observation AS observations,
                ->involves->entity AS entities
            FROM event
            WHERE start_time >= <datetime>$from
              AND start_time < <datetime>$to
              AND scenario = $scenario;
            """,
            {"from": from_time, "to": to_time, "scenario": scenario or "EPIC_FURY_DEMO"},
        )
        return res[0]["result"] if res else []
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
        res = await db.query(
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
        return res[0]["result"] if res else []
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
        bm25_res, vec_res = await asyncio.gather(
            db.query(
                "SELECT id, text, source, time FROM doc_chunk WHERE text @@ $query LIMIT $fetch_k;",
                {"query": query, "fetch_k": fetch_k},
            ),
            db.query(
                f"SELECT id, text, source, time FROM doc_chunk WHERE embedding <|{fetch_k},COSINE|> $vec;",
                {"vec": query_vec},
            ),
        )
        bm25_rows = bm25_res[0]["result"] if bm25_res else []
        vec_rows  = vec_res[0]["result"]  if vec_res  else []

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
