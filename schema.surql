-- Namespace: god_eye
-- Database: world

-- === Core node tables ===

DEFINE TABLE entity SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD type       ON TABLE entity TYPE string;
DEFINE FIELD name       ON TABLE entity TYPE string;
DEFINE FIELD coords     ON TABLE entity TYPE option<object>;
DEFINE FIELD metadata   ON TABLE entity TYPE option<object>;


DEFINE TABLE event SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD type         ON TABLE event TYPE string;
DEFINE FIELD start_time   ON TABLE event TYPE datetime;
DEFINE FIELD end_time     ON TABLE event TYPE option<datetime>;
DEFINE FIELD region       ON TABLE event TYPE option<object>;
DEFINE FIELD confidence   ON TABLE event TYPE float;
DEFINE FIELD source_tags  ON TABLE event TYPE array<string>;
DEFINE FIELD scenario     ON TABLE event TYPE option<string>;
DEFINE FIELD severity     ON TABLE event TYPE option<string>;  -- 'low' | 'medium' | 'high'
DEFINE FIELD axis         ON TABLE event TYPE option<string>;  -- 'air' | 'sea' | 'cyber' | 'multi'
DEFINE FIELD summary      ON TABLE event TYPE option<string>;
DEFINE FIELD details      ON TABLE event TYPE option<object>;


DEFINE TABLE observation SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD feed_type  ON TABLE observation TYPE string;      -- 'adsb' | 'ais' | 'sat_pass' | 'jamming' | 'net'
DEFINE FIELD time       ON TABLE observation TYPE datetime;
DEFINE FIELD position   ON TABLE observation TYPE option<object>;
DEFINE FIELD value      ON TABLE observation TYPE option<string>;
DEFINE FIELD raw        ON TABLE observation TYPE option<object>;


DEFINE TABLE doc_chunk SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD text        ON TABLE doc_chunk TYPE string;
DEFINE FIELD source      ON TABLE doc_chunk TYPE string;       -- 'NOTAM' | 'advisory' | 'news' | ...
DEFINE FIELD time        ON TABLE doc_chunk TYPE option<datetime>;
DEFINE FIELD embedding   ON TABLE doc_chunk TYPE option<array<float>>;

-- Vector index: dimension matches all-mpnet-base-v2 (768d). Update + re-run loader if switching models.
DEFINE INDEX doc_chunk_embedding ON doc_chunk FIELDS embedding HNSW DIMENSION 768 DIST COSINE;

-- Full-text index for BM25 hybrid retrieval (used alongside vector search via RRF).
DEFINE ANALYZER text_en TOKENIZERS blank,class FILTERS snowball(english);
DEFINE INDEX doc_chunk_fts ON doc_chunk FIELDS text SEARCH ANALYZER text_en BM25;


-- === Graph edge tables ===

DEFINE TABLE observed_in SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD role        ON TABLE observed_in TYPE option<string>;  -- e.g. 'track';


DEFINE TABLE involves SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD role        ON TABLE involves TYPE option<string>;     -- e.g. 'asset' | 'target';


DEFINE TABLE evidence SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD weight      ON TABLE evidence TYPE option<float>;

-- Analyst or agent-generated event annotations for persistent review context.
DEFINE TABLE event_annotation SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD event_id    ON TABLE event_annotation TYPE string;
DEFINE FIELD reason      ON TABLE event_annotation TYPE string;
DEFINE FIELD flagged_by  ON TABLE event_annotation TYPE string;
DEFINE FIELD flagged_at  ON TABLE event_annotation TYPE datetime;


-- === Agent log ===

DEFINE TABLE agent_log SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD time         ON TABLE agent_log TYPE datetime;
DEFINE FIELD agent        ON TABLE agent_log TYPE string;
DEFINE FIELD action       ON TABLE agent_log TYPE string;
DEFINE FIELD details      ON TABLE agent_log TYPE option<object>;

-- === Checkpoints for LangGraph stateful execution ===

DEFINE TABLE agent_checkpoint SCHEMAFULL PERMISSIONS FULL;

DEFINE FIELD thread_id            ON TABLE agent_checkpoint TYPE string;
DEFINE FIELD checkpoint_ns        ON TABLE agent_checkpoint TYPE option<string>;
DEFINE FIELD checkpoint_id        ON TABLE agent_checkpoint TYPE string;
DEFINE FIELD parent_checkpoint_id ON TABLE agent_checkpoint TYPE option<string>;
DEFINE FIELD created_at           ON TABLE agent_checkpoint TYPE datetime;
DEFINE FIELD checkpoint           ON TABLE agent_checkpoint TYPE object;
DEFINE FIELD metadata             ON TABLE agent_checkpoint TYPE option<object>;
DEFINE FIELD versions             ON TABLE agent_checkpoint TYPE option<object>;
DEFINE FIELD writes               ON TABLE agent_checkpoint TYPE option<array>;
