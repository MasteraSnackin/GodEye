Here are two diagrams that make the “structured vs baseline” story explicit.



1\. Side‑by‑side structured vs baseline paths

text

flowchart LR

&nbsp; subgraph Inputs

&nbsp;   Q\[User question<br/>+ time window<br/>+ region + scenario]

&nbsp; end



&nbsp; subgraph StructuredPath\[Structured Path<br/>(Graph + RAG)]

&nbsp;   FUSE\[fuse\_events<br/>create/refresh events]

&nbsp;   TL\[get\_timeline<br/>events + entities + observations]

&nbsp;   RAG\_S\[vector\_search<br/>(structured docs)]

&nbsp; end



&nbsp; subgraph BaselinePath\[Baseline Path<br/>(RAG only)]

&nbsp;   RAG\_B\[vector\_search<br/>(baseline docs)]

&nbsp; end



&nbsp; subgraph LLM\[LLM Analyst]

&nbsp;   NARR\[Narrative + comparison]

&nbsp; end



&nbsp; Q --> FUSE --> TL --> RAG\_S

&nbsp; Q --> RAG\_B



&nbsp; RAG\_S --> NARR

&nbsp; TL --> NARR

&nbsp; RAG\_B --> NARR

The structured path uses event fusion and the knowledge graph (fuse\_events + get\_timeline) plus RAG.



The baseline path ignores structure and only uses vector search.



The LLM sees both and explicitly explains what the graph adds over plain RAG.



2\. Sequence: LLM comparison of structured vs baseline

text

sequenceDiagram

&nbsp; participant U as User

&nbsp; participant LG as LangGraph Node

&nbsp; participant SDB as SurrealDB

&nbsp; participant LLM as LLM



&nbsp; U->>LG: State { from,to,region,scenario,query }



&nbsp; rect rgb(220,240,255)

&nbsp;   note over LG,SDB: Structured path

&nbsp;   LG->>SDB: fuse\_events()

&nbsp;   SDB-->>LG: events created/refreshed



&nbsp;   LG->>SDB: get\_timeline()

&nbsp;   SDB-->>LG: events + entities + observations



&nbsp;   LG->>SDB: vector\_search(query) (structured docs)

&nbsp;   SDB-->>LG: structured\_docs

&nbsp; end



&nbsp; rect rgb(240,220,255)

&nbsp;   note over LG,SDB: Baseline path

&nbsp;   LG->>SDB: vector\_search(query) (baseline docs)

&nbsp;   SDB-->>LG: baseline\_docs

&nbsp; end



&nbsp; LG->>LLM: prompt(events, structured\_docs, baseline\_docs, query)

&nbsp; LLM-->>LG: narrative (with comparison)

&nbsp; LG-->>U: narrative + events JSON

This shows clearly that the LLM is given both sets of context and asked to compare them, which is the core “why graph + temporal structure beats pure RAG” message.

