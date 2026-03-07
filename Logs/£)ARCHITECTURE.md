You are an experienced software architect and technical writer. Your task is to create a clear, detailed, and well-structured ARCHITECTURE.md document for my project.
Follow these rules:
Output format
Use GitHub-flavoured Markdown.
Return only the complete ARCHITECTURE.md content, no explanations.
Use precise, professional language, but keep it easy to read.
Document structure (sections in this order)
Title: “System Architecture” with the project name.
Overview: 1–2 paragraphs summarising the system, main goals, and high-level context.
Key Requirements: Short list of main functional and non-functional requirements that drive the design (performance, scalability, security, etc.).
High-Level Architecture:
Text description of the main components and how they interact.
At least one inlined Mermaid diagram (system context or container-level) in a fenced mermaid block.
Component Details:
Subsections for each major component/service (e.g., Web Client, API Gateway, User Service, Database, Message Broker, External Integrations).
For each component: responsibilities, main technologies, important data it owns, and how it communicates with others.
Data Flow:
Describe typical request/response flows or event flows (e.g., “user signup”, “order placement”).
Include a sequence diagram or flowchart in Mermaid if helpful.
Data Model (high-level):
Brief description of key entities and relationships (no need for full schema, just high-level).
Infrastructure & Deployment:
How the system is deployed (e.g., containers, Kubernetes, serverless, on-prem).
Environments (dev, staging, prod) and any differences.
Scalability & Reliability:
How the architecture handles load, failures, and redundancy (e.g., load balancers, queues, auto-scaling).
Security & Compliance:
Important security measures (auth, authz, encryption, secrets management).
Mention any relevant compliance or data protection considerations if applicable.
Observability:
How logging, metrics, and tracing are handled.
Trade-offs & Decisions:
A short “Design Decisions” or “Trade-offs” section summarising key choices and why they were made.
Future Improvements:
List of planned or possible architecture changes or optimisations.
Style and diagrams
Use headings, subheadings, and bullet lists to keep it scannable.
Use clear, neutral British English.
For Mermaid diagrams, ensure they are valid and easy to understand (simple labels, not too many nodes).
Add a brief explanation under each diagram describing what it shows and why it matters.
Project-specific details
Use the following details to tailor the document (if any are missing, use clearly marked placeholders like <ADD DETAIL HERE> and keep things generic but realistic):
Project name and short description.
Type of system (e.g., REST API, microservices platform, monolith web app, event-driven system, mobile backend, data pipeline).
Main technologies (frontend, backend, database, messaging, cloud provider).
Core features or domains (e.g., authentication, billing, reporting).
Deployment approach (e.g., Docker + Kubernetes, serverless, simple VM).
Any special constraints (e.g., must handle X users, low latency, strict data privacy).
Use these details to build a coherent, realistic ARCHITECTURE.md. If information is missing, make sensible assumptions but keep them clearly marked so they can be edited later.
Generate the complete ARCHITECTURE.md now.
