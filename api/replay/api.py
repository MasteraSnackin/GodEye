import hashlib
import logging
import os
import json
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

from src.agents.checkpointer import SurrealDBCheckpointSaver
from src.agents.graph import build_graph, is_llm_configured
from src.agents.state import State
from src.agents.tools import get_surreal_client, get_high_sev_jamming_on_tankers


def _parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


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


def _safe_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _build_fallback_timeline_metrics(events: list[dict], prev_events: list[dict], from_time: str, to_time: str) -> dict:
    axis_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    confidences = []
    bucket_counts: dict[str, int] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        axis = str(event.get("axis", "unknown")).lower()
        severity = str(event.get("severity", "unknown")).lower()
        axis_counts[axis] = axis_counts.get(axis, 0) + 1
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

        conf = _safe_confidence(event.get("confidence"))
        if conf is not None:
            confidences.append(conf)

        raw = str(event.get("start_time") or "").replace("Z", "+00:00")
        try:
            bucket_dt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        bucket = bucket_dt.replace(minute=(bucket_dt.minute // 10) * 10, second=0, microsecond=0)
        bucket_key = bucket.isoformat()
        bucket_counts[bucket_key] = bucket_counts.get(bucket_key, 0) + 1

    conf_avg = sum(confidences) / len(confidences) if confidences else 0.0
    conf_min = min(confidences) if confidences else 0.0
    conf_max = max(confidences) if confidences else 0.0

    timeline_buckets = [
        {"bucket": bucket, "count": count}
        for bucket, count in sorted(bucket_counts.items(), key=lambda item: str(item[0]))
    ]

    return {
        "window": {"from": from_time, "to": to_time},
        "event_count": len(events),
        "prev_event_count": len(prev_events),
        "axis_distribution": axis_counts,
        "severity_distribution": severity_counts,
        "confidence": {
            "count": len(confidences),
            "avg": round(conf_avg, 3),
            "min": round(conf_min, 3),
            "max": round(conf_max, 3),
        },
        "timeline_buckets": timeline_buckets,
        "retrieval_counts": {
            "query_rag": 0,
            "entity_graph_rag": 0,
            "baseline_docs": 0,
        },
    }


ALLOW_ORIGINS = _parse_csv_env(
    "CORS_ORIGINS",
    "http://localhost:8001, http://127.0.0.1:8001, http://localhost:8080, http://127.0.0.1:8080, http://localhost:8086, http://127.0.0.1:8086, http://localhost:3000, http://127.0.0.1:3000",
)
API_KEYS = set(_parse_csv_env("GODEYE_API_KEYS", os.getenv("API_KEYS", "")))
_FALLBACK_OBSERVATION_MAP = None


def _build_observation_position_fallback() -> dict[str, dict]:
    base_path = Path(__file__).resolve().parents[2] / "synthetic_world.json"
    if not base_path.exists():
        return {}
    try:
        payload = json.loads(base_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    mapping: dict[str, dict] = {}
    for obs in payload.get("observations", []) or []:
        obs_id = obs.get("id")
        if not obs_id:
            continue
        mapping[obs_id] = {k: obs.get(k) for k in ("position", "entity_id", "entity_name", "entity_type", "raw")}
    return mapping


def validate_runtime_config():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required for replay endpoints.")


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if not API_KEYS:
        return
    if not x_api_key or x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm 3 pool connections at startup so early requests don't pay the
    # ~50ms WebSocket handshake + auth cost on first use.
    conns = []
    for _ in range(3):
        try:
            conns.append(await get_surreal_client())
        except Exception as e:
            logger.warning("Pool pre-warm failed: %s", e)
            break
    for c in conns:
        await c.close()  # returns connections to pool rather than closing them
    logger.info("Connection pool pre-warmed with %d connections.", len(conns))
    yield


app = FastAPI(title="GodEye API", version="2.0", lifespan=lifespan)
resolved_origins = sorted({*ALLOW_ORIGINS, "null"})
app.add_middleware(
    CORSMiddleware,
    allow_origins=resolved_origins,
    # Local dev and evaluation frequently run API at multiple localhost ports.
    allow_origin_regex=r"^https?://(localhost|127\\.0\\.0\\.1)(:[0-9]{1,5})?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    validate_runtime_config()
    graph = build_graph()
except Exception as _e:
    logger.exception("Graph failed to initialize at startup: %s", _e)
    graph = None


class ReplayRequest(BaseModel):
    mode: str = "replay"
    from_time: str
    to_time: str
    region: str = ""
    scenario: str = "EPIC_FURY_DEMO"
    query: str = "What anomalies occurred in this window?"

    @field_validator("from_time", "to_time")
    @classmethod
    def must_be_iso(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Must be ISO 8601 datetime, got: {v!r}")
        return v


@app.get("/health")
async def health():
    try:
        db = await get_surreal_client()
        await db.query("RETURN 1;")
        await db.close()
        if not is_llm_configured():
            return {"status": "degraded", "db": "connected", "llm": "missing_api_key"}
        return {"status": "ok", "db": "connected", "llm": "configured"}
    except Exception as e:
        logger.exception("Health check failed: %s", e)
        raise HTTPException(status_code=503, detail="DB unavailable. Check server logs.")


@app.get("/api/events")
async def api_events(
    _auth: Optional[str] = Depends(require_api_key),
    scenario: str = Query(default="EPIC_FURY_DEMO"),
    from_time: Optional[str] = Query(default=None),
    to_time: Optional[str] = Query(default=None),
):
    for field, val in (("from_time", from_time), ("to_time", to_time)):
        if val is not None:
            try:
                datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{field} must be ISO 8601, got: {val!r}")
    db = await get_surreal_client()
    try:
        if from_time and to_time:
            res = await db.query(
                """
                SELECT * FROM event
                WHERE scenario = $scenario
                  AND start_time >= <datetime>$from
                  AND start_time < <datetime>$to
                ORDER BY start_time;
                """,
                {"scenario": scenario, "from": from_time, "to": to_time},
            )
        else:
            res = await db.query(
                "SELECT * FROM event WHERE scenario = $scenario ORDER BY start_time;",
                {"scenario": scenario},
            )
        return {"events": _coerce_query_rows(res)}
    except Exception as e:
        logger.exception("api_events failed: %s", e)
        raise HTTPException(status_code=500, detail="Events query failed. Check server logs.")
    finally:
        await db.close()


@app.get("/api/checkpoints")
async def api_checkpoints(
    _auth: Optional[str] = Depends(require_api_key),
    thread_id: str = Query(..., description="Replay thread ID"),
    checkpoint_ns: str = Query(default=""),
    limit: int = Query(default=20, le=200),
):
    """Inspect persisted checkpoint history for a replay thread."""
    saver = SurrealDBCheckpointSaver()
    try:
        checkpoints = []
        async for cp in saver.alist(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}},
            limit=limit,
        ):
            checkpoints.append(
                {
                    "checkpoint_id": cp.config["configurable"]["checkpoint_id"],
                    "parent_checkpoint_id": cp.parent_config["configurable"]["checkpoint_id"]
                    if cp.parent_config
                    else None,
                    "metadata": cp.metadata,
                }
            )
        return {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoints": checkpoints}
    except Exception as e:
        logger.exception("api_checkpoints failed: %s", e)
        raise HTTPException(status_code=500, detail="Checkpoint lookup failed. Check server logs.")


@app.get("/api/scenarios")
async def api_scenarios(_auth: Optional[str] = Depends(require_api_key)):
    """Return all distinct scenario names present in the database."""
    db = await get_surreal_client()
    try:
        res = await db.query("SELECT scenario FROM event GROUP BY scenario;")
        rows = _coerce_query_rows(res)
        scenarios = sorted({r["scenario"] for r in rows if r.get("scenario")})
        return {"scenarios": scenarios}
    except Exception as e:
        logger.exception("api_scenarios failed: %s", e)
        raise HTTPException(status_code=500, detail="Scenarios query failed. Check server logs.")
    finally:
        await db.close()


@app.get("/api/jamming/tankers")
async def api_jamming_tankers(
    _auth: Optional[str] = Depends(require_api_key),
    from_time: str = Query(..., description="ISO 8601 start (inclusive)"),
    to_time: str = Query(..., description="ISO 8601 end (exclusive)"),
    scenario: str = Query(default="EPIC_FURY_DEMO"),
):
    """High-severity jamming events where at least one involved entity is a ship."""
    for field, val in (("from_time", from_time), ("to_time", to_time)):
        try:
            datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{field} must be ISO 8601, got: {val!r}")
    try:
        results = await get_high_sev_jamming_on_tankers.ainvoke(
            {"from_time": from_time, "to_time": to_time, "scenario": scenario}
        )
        return {"events": results}
    except Exception as e:
        logger.exception("api_jamming_tankers failed: %s", e)
        raise HTTPException(status_code=500, detail="Jamming query failed. Check server logs.")


@app.get("/api/observations")
async def api_observations(
    _auth: Optional[str] = Depends(require_api_key),
    from_time: Optional[str] = Query(default=None, description="ISO 8601 start (inclusive)"),
    to_time: Optional[str] = Query(default=None, description="ISO 8601 end (exclusive)"),
    feed_type: Optional[str] = Query(default=None, description="Filter by feed type: adsb | ais | jamming | net | sat_pass"),
    limit: int = Query(default=100, le=1000),
):
    """Raw observations from SurrealDB, optionally filtered by time window and feed type."""
    for field, val in (("from_time", from_time), ("to_time", to_time)):
        if val is not None:
            try:
                datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{field} must be ISO 8601, got: {val!r}")
    db = await get_surreal_client()
    try:
        conditions = []
        params: dict = {"limit": limit}
        if from_time:
            conditions.append("time >= <datetime>$from")
            params["from"] = from_time
        if to_time:
            conditions.append("time < <datetime>$to")
            params["to"] = to_time
        if feed_type:
            conditions.append("feed_type = $feed_type")
            params["feed_type"] = feed_type
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        res = await db.query(
            f"SELECT * FROM observation {where} ORDER BY time LIMIT $limit;",
            params,
        )
        observations = _coerce_query_rows(res)
        global _FALLBACK_OBSERVATION_MAP
        if _FALLBACK_OBSERVATION_MAP is None:
            _FALLBACK_OBSERVATION_MAP = _build_observation_position_fallback()
        if _FALLBACK_OBSERVATION_MAP:
            for obs in observations:
                if not isinstance(obs, dict):
                    continue
                pos = obs.get("position")
                if pos not in (None, "", {}):
                    continue

                raw_id = obs.get("id")
                obs_id = None
                if isinstance(raw_id, dict):
                    obs_id = raw_id.get("id") or raw_id.get("record_id")
                elif isinstance(raw_id, str):
                    obs_id = raw_id.split(":")[-1]
                else:
                    raw_id_text = str(raw_id)
                    if ":" in raw_id_text:
                        obs_id = raw_id_text.rsplit(":", 1)[-1]
                    elif raw_id_text.strip():
                        obs_id = raw_id_text.strip()

                if obs_id and obs_id in _FALLBACK_OBSERVATION_MAP:
                    source = _FALLBACK_OBSERVATION_MAP[obs_id]
                    if source.get("position"):
                        obs["position"] = source.get("position")
                    if source.get("entity_name"):
                        obs.setdefault("entity_name", source.get("entity_name"))
                    if source.get("entity_id"):
                        obs.setdefault("entity_id", source.get("entity_id"))
                    if not obs.get("entity_type") and source.get("entity_type"):
                        obs.setdefault("entity_type", source.get("entity_type"))
                    if not obs.get("raw") and source.get("raw"):
                        obs.setdefault("raw", source.get("raw"))

        return {"observations": observations}
    except Exception as e:
        logger.exception("api_observations failed: %s", e)
        raise HTTPException(status_code=500, detail="Observations query failed. Check server logs.")
    finally:
        await db.close()


@app.get("/api/entities")
async def api_entities(
    _auth: Optional[str] = Depends(require_api_key),
    entity_type: Optional[str] = Query(default=None, description="Filter by entity type: ship | aircraft | station"),
):
    """All entities in the knowledge graph, optionally filtered by type."""
    db = await get_surreal_client()
    try:
        if entity_type:
            res = await db.query(
                "SELECT * FROM entity WHERE type = $entity_type ORDER BY name;",
                {"entity_type": entity_type},
            )
        else:
            res = await db.query("SELECT * FROM entity ORDER BY name;")
        return {"entities": _coerce_query_rows(res)}
    except Exception as e:
        logger.exception("api_entities failed: %s", e)
        raise HTTPException(status_code=500, detail="Entities query failed. Check server logs.")
    finally:
        await db.close()


@app.post("/api/replay")
async def api_replay(req: ReplayRequest, _auth: Optional[str] = Depends(require_api_key)):
    if graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialized. Check server logs.")
    state: State = {
        "mode": req.mode,
        "thread_id": hashlib.md5(f"{req.scenario}:{req.from_time}:{req.to_time}".encode()).hexdigest(),
        "from_time": req.from_time,
        "to_time": req.to_time,
        "region": req.region,
        "scenario": req.scenario,
        "query": req.query,
    }
    thread_id = state["thread_id"]
    try:
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id, "checkpoint_ns": "replay"}})
    except Exception as e:
        logger.exception("Graph invocation failed: %s", e)
        raise HTTPException(status_code=500, detail="Replay failed. Check server logs.")

    events = result.get("events") or []
    if not isinstance(events, list):
        events = []
    prev_events = result.get("prev_events") or []
    if not isinstance(prev_events, list):
        prev_events = []
    narrative = result.get("narrative")
    summary = result.get("event_summary")
    timeline_metrics = result.get("timeline_metrics")
    if not isinstance(timeline_metrics, dict):
        timeline_metrics = _build_fallback_timeline_metrics(events, prev_events, req.from_time, req.to_time)
    if not isinstance(narrative, str) or not narrative.strip():
        narrative = (
            "Narrative unavailable. Fallback data path generated no narrative. "
            "Check LLM configuration, DB connectivity, and logs."
        )
        if "narrative_status" not in result:
            result["narrative_status"] = "missing_data"
    if not isinstance(summary, str) or not summary.strip():
        summary = "Summary unavailable. Check source data and logs."

    return {
        "narrative": narrative,
        "events": events,
        "event_summary": summary,
        "narrative_status": result.get("narrative_status", "ok"),
        "summary_status": result.get("summary_status", "ok"),
        "narrative_status_reason": result.get("narrative_status_reason"),
        "summary_status_reason": result.get("summary_status_reason"),
        "timeline_metrics": timeline_metrics,
        "baseline_events": prev_events,
        "runtime_metrics": result.get("runtime_metrics", {}),
        "llm_model_used": result.get("llm_model_used"),
        "trace_url": os.getenv("LANGSMITH_RUN_BASE_URL", ""),
        "thread_id": thread_id,
    }
