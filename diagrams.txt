1. High-level system context
```mermaid
flowchart LR
  U[User] --> UI[Web Frontend]
  U --> CLI[CLI / demo.py]

  UI -->|POST /api/replay\nGET /api/events\nGET /api/observations\nGET /api/entities\nGET /api/scenarios\nGET /api/jamming/tankers\nGET /health| API[FastAPI :8001]
  CLI --> LG[LangGraph Agent]

  API --> LG
  LG <--> SDB[(SurrealDB\nentity / observation / event / doc_chunk / agent_log)]
  LG -->|LLM calls| ANT[Anthropic Claude\nclaude-sonnet-4-6]
```
The four main actors: user (web or CLI), FastAPI gateway, LangGraph agent, SurrealDB world model.
Anthropic is the only external service dependency; all other state is local.
FastAPI pre-warms the SurrealDB connection pool (asyncio.Queue, maxsize=5) at startup via lifespan.


2. Container / component view
```mermaid
flowchart TB
  subgraph Client["Client Layer"]
    UI[Web Frontend\nHTML/CSS/JS]
    CLI[CLI\ndemo.py]
  end

  subgraph API["API Layer"]
    GW[FastAPI\n7 endpoints + /health\nconnection pool pre-warm]
  end

  subgraph Agent["Agent Layer — LangGraph"]
    RN[reconstruct_node\nPhase 1: fuse + query-RAG\nPhase 2: current + prev timelines\nPhase 3: entity Graph-RAG]
    NN[narrate_node]
    RN --> NN
  end

  subgraph Tools["Agent Tools"]
    FE[fuse_events\nNoisy-OR confidence]
    VS[vector_search\nBM25 + HNSW RRF k=30]
    GT[get_timeline]
  end

  subgraph DB["World Model — SurrealDB"]
    EVT[(event)]
    OBS[(observation)]
    ENT[(entity)]
    DOC[(doc_chunk\nHNSW 768d + BM25)]
    LOG[(agent_log)]
  end

  UI --> GW
  CLI --> Agent
  GW --> Agent

  RN -->|Phase 1 concurrent| FE
  RN -->|Phase 1 concurrent| VS
  RN -->|Phase 2 concurrent x2| GT
  RN -->|Phase 3 entity Graph-RAG| VS

  FE <--> EVT & OBS & ENT
  FE --> LOG
  VS <--> DOC
  GT <--> EVT & ENT
```
Both CLI and web paths converge on the same LangGraph graph and SurrealDB instance.
reconstruct_node runs three sequential phases, each with internal concurrency.
Phase 2 fires two get_timeline calls concurrently: current window and the prior equal-duration window.
Phase 3 re-queries doc_chunk using entity names extracted from the detected event graph.


3. Sequence: replay request (web path)
```mermaid
sequenceDiagram
  participant U as User
  participant UI as Web UI
  participant API as FastAPI
  participant RN as reconstruct_node
  participant NN as narrate_node
  participant DB as SurrealDB
  participant LLM as Claude

  U->>UI: Fill form (from/to, region, scenario, question)
  UI->>API: POST /api/replay
  API->>API: Validate ISO 8601 timestamps (Pydantic)
  API->>API: Acquire pooled SurrealDB connection
  API->>RN: graph.ainvoke(state)

  note over RN: Phase 1 — asyncio.gather (return_exceptions=True)
  par fuse_events
    RN->>DB: DELETE events for window (idempotency)
    RN->>DB: GROUP observations → buckets
    RN->>DB: CREATE events (Noisy-OR confidence: 1 - 0.8^n)
    RN->>DB: Correlation pass → CREATE multi events
    DB-->>RN: side-effect only (events written)
  and vector_search (query-RAG)
    RN->>RN: embed_query via run_in_executor (ThreadPool)
    RN->>DB: BM25 text search (k*3 candidates)
    RN->>DB: HNSW vector search (k*3 candidates)
    RN->>RN: RRF merge → top-5 docs (query-RAG)
  end

  note over RN: Phase 2 — asyncio.gather (return_exceptions=True)
  par get_timeline (current window)
    RN->>DB: SELECT events + graph traversal
    DB-->>RN: current events + entities
  and get_timeline (previous window)
    RN->>DB: SELECT events for prior equal-duration window
    DB-->>RN: prev events (non-fatal if empty)
  end

  note over RN: Phase 3 — entity-augmented Graph-RAG
  RN->>RN: Extract entity names from current timeline
  RN->>DB: vector_search(question + entity names, k=3)
  DB-->>RN: entity_docs (deduplicated against Phase 1 docs)

  note over NN: narrate_node — asyncio.gather (return_exceptions=True)
  par narrative LLM
    NN->>LLM: Narrative prompt (events + query-RAG + entity-RAG + prev window)
    LLM-->>NN: narrative text
  and summary LLM
    NN->>LLM: Summary prompt (events only)
    LLM-->>NN: summary text
  end

  NN-->>API: { narrative, event_summary, events }
  API-->>UI: JSON response
  UI-->>U: Rendered narrative + summary + events table
```
Three sequential phases in reconstruct_node, each with internal asyncio.gather concurrency.
All gather calls use return_exceptions=True: a failed BM25 or previous-window fetch degrades gracefully without aborting the request.
Phase 3 only fires when Phase 2 surfaces at least one entity; entity_docs are deduplicated against Phase 1 docs.


4. Sequence: event fusion in detail
```mermaid
sequenceDiagram
  participant LG as fuse_events
  participant DB as SurrealDB

  LG->>DB: LOG agent_log { action: start_fuse }
  LG->>DB: DELETE events WHERE window + scenario (idempotency)

  LG->>DB: SELECT + GROUP observations by (feed_type, floor(time,10m))
  DB-->>LG: grouped buckets [ {feed_type, bucket_start, obs_ids} ]

  loop per (feed_type, bucket)
    LG->>LG: confidence = round(1 - 0.8^obs_count, 2)  [Noisy-OR]
    LG->>LG: severity: >= 0.67 → high, >= 0.36 → medium, else low (jamming always high)
    LG->>DB: CREATE event { type, axis, severity, confidence, scenario, details:{region} }
    DB-->>LG: event record

    LG->>DB: FOR obs IN obs_ids RELATE event->evidence->obs (batch)
    LG->>DB: SELECT <-observed_in<-entity (graph traversal)
    DB-->>LG: entity list

    loop per entity
      LG->>DB: RELATE event->involves->entity SET role='asset'
    end
  end

  note over LG,DB: Cross-feed correlation pass
  loop per bucket with >= 2 distinct feed_types
    LG->>LG: corr_conf = round(1 - 0.8^total_obs_count, 2)  [Noisy-OR]
    LG->>DB: CREATE event { type:'correlation', axis:'multi', severity:'high', confidence:corr_conf }
    LG->>DB: FOR obs IN all_obs_ids RELATE corr_event->evidence->obs (batch)
  end

  LG->>DB: LOG agent_log { action: end_fuse, created_events: N }
```
DELETE-before-write makes fusion idempotent. Evidence linking is batched (one query per event, not per observation).
Noisy-OR confidence (Pearl 1988): each additional observation independently raises confidence, approaching 1.0 asymptotically.
n=1 → 0.20 (tentative), n=2 → 0.36, n=5 → 0.67 (strong). Severity thresholds align to these natural breakpoints.
The correlation pass runs after the main loop. Its confidence also uses Noisy-OR over the combined observation pool.


5. Sequence: hybrid vector retrieval (RRF)
```mermaid
sequenceDiagram
  participant VS as vector_search
  participant EM as all-mpnet-base-v2\n(local CPU, ThreadPoolExecutor)
  participant DB as SurrealDB

  VS->>EM: run_in_executor(embed_query, user_query)
  note over EM: CPU-bound — offloaded to thread;\nevent loop stays free
  EM-->>VS: 768-dim vector

  par asyncio.gather (return_exceptions=True)
    VS->>DB: SELECT doc_chunk WHERE text @@ query LIMIT k*3  (BM25)
    DB-->>VS: bm25_rows [ ranked by BM25 score ]
  and
    VS->>DB: SELECT doc_chunk WHERE embedding <|k*3,COSINE|> vec  (HNSW)
    DB-->>VS: vec_rows [ ranked by cosine similarity ]
  end

  note over VS: Graceful degradation: if BM25 fails → vector only;\nif HNSW fails → BM25 only
  VS->>VS: RRF merge: score[id] += 1/(30 + rank) for each list
  VS->>VS: sort by RRF score, return top-k
```
Both DB queries are independent and run concurrently. return_exceptions=True allows either index to fail gracefully.
run_in_executor prevents the synchronous sentence-transformers encode from blocking the asyncio event loop.
RRF k=30 (tuned for short OSINT corpora) rewards documents that rank highly in either list and doubly rewards those that rank well in both.
The embeddings singleton is initialised once under a threading.Lock (double-checked locking) to prevent duplicate model loads under concurrent requests.


6. Data flow: ingestion to replay
```mermaid
flowchart LR
  subgraph Ingestion
    FIX[synthetic_world.json] --> LOADER[load_synthetic_world.py]
    LOADER -->|embed_documents\nall-mpnet-base-v2| EMB[768-dim vectors]
    LOADER --> SDB[(SurrealDB)]
    EMB --> SDB
  end

  subgraph Fusion
    FUSER[fuse_events\nNoisy-OR confidence] -->|write events\ncorrelation events| SDB
    SDB -->|read observations| FUSER
  end

  subgraph Retrieval["Retrieval (Phase 1 + 3)"]
    VS1[vector_search\nquery-RAG\nBM25 + HNSW RRF] <--> SDB
    VS2[vector_search\nentity Graph-RAG\naugmented query] <--> SDB
  end

  subgraph Timeline["Timeline (Phase 2)"]
    GT1[get_timeline\ncurrent window] <--> SDB
    GT2[get_timeline\nprev window] <--> SDB
  end

  subgraph Narration
    LG[narrate_node] -->|2x concurrent LLM| ANT[Anthropic Claude]
    ANT --> OUT[narrative + event_summary]
  end

  SDB -->|get_timeline| LG
  VS1 --> LG
  VS2 --> LG
  GT1 --> LG
  GT2 --> LG
  OUT --> CLI[CLI / demo.py]
  OUT --> UI[Web UI]
```
Pipeline: fixtures → DB (with 768-dim embeddings) → fusion + correlation → hybrid retrieval (query-RAG) → timeline (current + prev) → entity Graph-RAG → LLM narration → outputs.
Phase 3 entity Graph-RAG re-queries doc_chunk using entity names discovered in Phase 2, surfacing NOTAM/advisory docs the user did not know to ask for.


7. Data model (entities and relations)
```mermaid
erDiagram
  ENTITY {
    string type
    string name
    object coords
    object metadata
  }

  OBSERVATION {
    string feed_type
    datetime time
    object position
    string value
    object raw
  }

  EVENT {
    string type
    datetime start_time
    datetime end_time
    float confidence
    string severity
    string axis
    string scenario
    array source_tags
    object details
  }

  DOC_CHUNK {
    string text
    string source
    datetime time
    array embedding_768d
  }

  AGENT_LOG {
    datetime time
    string agent
    string action
    object details
  }

  ENTITY ||--o{ OBSERVATION : "observed_in"
  EVENT  ||--o{ OBSERVATION : "evidence"
  EVENT  ||--o{ ENTITY      : "involves"
```
EVENT.type may be 'anomaly', 'jamming', or 'correlation' (multi-feed co-occurrence).
EVENT.confidence uses Noisy-OR (Pearl 1988): 1 - 0.8^n. n=1 → 0.20 tentative, n=2 → 0.36, n=5 → 0.67 strong.
EVENT.details stores {region_name} when a region is provided; null otherwise.
DOC_CHUNK has two indices: HNSW (768d COSINE) for vector search and BM25 (text_en analyser) for keyword search.
AGENT_LOG is standalone — populated by fuse_events with start_fuse / end_fuse records; log failures never abort business logic.
