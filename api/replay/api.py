import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

from src.agents.graph import build_graph
from src.agents.state import State
from src.agents.tools import get_surreal_client, get_high_sev_jamming_on_tankers


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

try:
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
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        logger.exception("Health check failed: %s", e)
        raise HTTPException(status_code=503, detail="DB unavailable. Check server logs.")


@app.get("/api/events")
async def api_events(
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
        return {"events": res[0]["result"] if res else []}
    except Exception as e:
        logger.exception("api_events failed: %s", e)
        raise HTTPException(status_code=500, detail="Events query failed. Check server logs.")
    finally:
        await db.close()


@app.get("/api/scenarios")
async def api_scenarios():
    """Return all distinct scenario names present in the database."""
    db = await get_surreal_client()
    try:
        res = await db.query("SELECT scenario FROM event GROUP BY scenario;")
        rows = res[0]["result"] if res else []
        scenarios = sorted({r["scenario"] for r in rows if r.get("scenario")})
        return {"scenarios": scenarios}
    except Exception as e:
        logger.exception("api_scenarios failed: %s", e)
        raise HTTPException(status_code=500, detail="Scenarios query failed. Check server logs.")
    finally:
        await db.close()


@app.get("/api/jamming/tankers")
async def api_jamming_tankers(
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
        return {"observations": res[0]["result"] if res else []}
    except Exception as e:
        logger.exception("api_observations failed: %s", e)
        raise HTTPException(status_code=500, detail="Observations query failed. Check server logs.")
    finally:
        await db.close()


@app.get("/api/entities")
async def api_entities(
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
        return {"entities": res[0]["result"] if res else []}
    except Exception as e:
        logger.exception("api_entities failed: %s", e)
        raise HTTPException(status_code=500, detail="Entities query failed. Check server logs.")
    finally:
        await db.close()


@app.post("/api/replay")
async def api_replay(req: ReplayRequest):
    if graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialized. Check server logs.")
    state: State = {
        "mode": req.mode,
        "from_time": req.from_time,
        "to_time": req.to_time,
        "region": req.region,
        "scenario": req.scenario,
        "query": req.query,
    }
    thread_id = hashlib.md5(f"{req.scenario}:{req.from_time}:{req.to_time}".encode()).hexdigest()
    try:
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
    except Exception as e:
        logger.exception("Graph invocation failed: %s", e)
        raise HTTPException(status_code=500, detail="Replay failed. Check server logs.")
    return {
        "narrative": result.get("narrative"),
        "events": result.get("events", []),
        "event_summary": result.get("event_summary"),
    }
