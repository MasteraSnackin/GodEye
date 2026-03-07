1\. Demo script (DEMO.md)

This is mainly for you, but you can also link it in the README.



text

\# Demo Script – GodEye



\## 1. Problem framing (30–45s)



\- Agents today struggle with long-term, structured context.

\- GodEye shows how to ground an agent in a persistent world model – SurrealDB – and replay an operation in 4D using OSINT-like feeds.



\## 2. Architecture overview (30–45s)



\- One world model: SurrealDB holds entities, observations, events, documents.

\- One agent graph: LangGraph orchestrates fusion, graph queries, and RAG.

\- Thin surfaces: CLI for debugging, FastAPI + simple web UI for interaction.



(Show the system context Mermaid diagram here.)



\## 3. Live replay (2–3 minutes)



Terminal:



```bash

python demo.py

Talk through:



Synthetic feeds: ADS-B, AIS, jamming, net, NOTAM/advisories.



Event fusion: anomalies and jamming bursts by feed and time bucket.



Narrative: explain what happened between 02:00–04:00Z near Hormuz.



Comparison: structured (graph+RAG) vs baseline (RAG only).



Web UI (optional):



Open frontend/index.html.



Set From/To, Region=Hormuz, Scenario=EPIC\_FURY\_DEMO, ask a question.



Press “Run Replay” and point at:



Narrative panel.



Event summary.



Events table (axis, severity, tags).



4\. Wrap-up (30–45s)

Emphasise SurrealDB as unified memory: graph + vector + time-series.



Emphasise LangGraph as explicit agent workflow, not just a single LLM call.



Note that the same pattern generalises to real data and other domains (on-call, incident response, etc.).



text



\*\*\*



\## 2. World model evolution diagram (events and graph over time)



This explains “persistent state” visually.



```mermaid

flowchart LR

&nbsp; subgraph T1\[Window 1: 02:00–02:10Z]

&nbsp;   O1\[Obs: ADS-B\\nEAGLE101]

&nbsp;   O2\[Obs: AIS\\nTANKER\_A]

&nbsp;   O3\[Obs: Jamming burst]

&nbsp; end



&nbsp; subgraph T2\[Window 2: 02:10–02:20Z]

&nbsp;   O4\[Obs: ADS-B\\nEAGLE101 near AOI]

&nbsp;   O5\[Obs: AIS\\nTANKER\_A stopped]

&nbsp; end



&nbsp; subgraph WorldModel\[SurrealDB World Model]

&nbsp;   E1\[(Entity: EAGLE101)]

&nbsp;   E2\[(Entity: TANKER\_A)]

&nbsp;   EV1\[\[Event: Anomaly cluster]]

&nbsp;   EV2\[\[Event: Jamming]]

&nbsp; end



&nbsp; O1 -->|ingest\_observations| WorldModel

&nbsp; O2 -->|ingest\_observations| WorldModel

&nbsp; O3 -->|ingest\_observations| WorldModel

&nbsp; O4 -->|ingest\_observations| WorldModel

&nbsp; O5 -->|ingest\_observations| WorldModel



&nbsp; WorldModel -->|fuse\_events| EV1

&nbsp; WorldModel -->|fuse\_events| EV2



&nbsp; EV1 -->|involves| E1

&nbsp; EV1 -->|involves| E2

&nbsp; EV2 -->|involves| E2

Narrative you can say with this:



At each ingest step, observations are added; they never disappear.



Fusion builds events and links them to entities.



When you re‑run a replay later, you are querying an evolving world model, not a transient context window.



If you have time for only one extra diagram for judges, use the “structured vs baseline” one you already have. If you have time for only one extra doc, use the DEMO.md script so you can present cleanly without thinking about the steps.

