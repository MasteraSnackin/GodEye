import json
import asyncio
import logging
from pathlib import Path

from surrealdb import AsyncSurreal
from langchain_community.embeddings import HuggingFaceEmbeddings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SURREAL_URL = "ws://127.0.0.1:8000/rpc"
NS = "god_eye"
DB = "world"
FIXTURE_PATH = Path("synthetic_world.json")


async def load_fixture():
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(f"Fixture not found: {FIXTURE_PATH}")
    data = json.loads(FIXTURE_PATH.read_text())

    db = AsyncSurreal(SURREAL_URL)
    await db.connect()
    await db.signin({"username": "root", "password": "root"})
    await db.use(NS, DB)

    entities = data.get("entities", [])
    observations = data.get("observations", [])
    doc_chunks = data.get("doc_chunks", [])
    ok = err = 0

    try:
        # Entities
        for ent in entities:
            try:
                await db.query(
                    """
                    CREATE type::record("entity", $id) CONTENT {
                        type: $type,
                        name: $name,
                        coords: $coords,
                        metadata: $metadata
                    };
                    """,
                    {
                        "id": ent["id"],
                        "type": ent["type"],
                        "name": ent["name"],
                        "coords": ent.get("coords"),
                        "metadata": ent.get("metadata"),
                    },
                )
                ok += 1
            except Exception as e:
                logger.warning("Skipped entity %s: %s", ent.get("id"), e)
                err += 1

        # Observations + observed_in edges
        for obs in observations:
            try:
                await db.query(
                    """
                    CREATE type::record("observation", $id) CONTENT {
                        feed_type: $feed_type,
                        time: <datetime>$time,
                        position: $position,
                        value: $value,
                        raw: $raw
                    };
                    """,
                    {
                        "id": obs["id"],
                        "feed_type": obs["feed_type"],
                        "time": obs["time"],
                        "position": obs.get("position"),
                        "value": obs.get("value"),
                        "raw": obs.get("raw"),
                    },
                )
                ok += 1
            except Exception as e:
                logger.warning("Skipped observation %s: %s", obs.get("id"), e)
                err += 1
                continue

            ent_id = obs.get("entity_id")
            if ent_id:
                try:
                    await db.query(
                        """
                        LET $e = type::record("entity", $ent);
                        LET $o = type::record("observation", $obs);
                        RELATE $e->observed_in->$o SET role = 'track';
                        """,
                        {"ent": ent_id, "obs": obs["id"]},
                    )
                except Exception as e:
                    logger.warning("Skipped edge entity->obs %s->%s: %s", ent_id, obs["id"], e)

        # doc_chunk with embeddings stored directly
        if doc_chunks:
            try:
                embed_model = HuggingFaceEmbeddings(model_name="sentence-transformers/all-mpnet-base-v2")
                texts = [d["text"] for d in doc_chunks]
                vectors = embed_model.embed_documents(texts)
            except Exception as e:
                logger.error("Embedding failed, skipping all doc_chunks: %s", e)
                vectors = []

            for d, vec in zip(doc_chunks, vectors):
                try:
                    await db.query(
                        """
                        CREATE type::record("doc_chunk", $id) CONTENT {
                            text: $text,
                            source: $source,
                            time: <option<datetime>>$time,
                            embedding: $embedding
                        };
                        """,
                        {
                            "id": d["id"],
                            "text": d["text"],
                            "source": d["source"],
                            "time": d.get("time"),
                            "embedding": vec,
                        },
                    )
                    ok += 1
                except Exception as e:
                    logger.warning("Skipped doc_chunk %s: %s", d.get("id"), e)
                    err += 1

        logger.info("Fixture loaded: %d ok, %d skipped.", ok, err)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(load_fixture())
