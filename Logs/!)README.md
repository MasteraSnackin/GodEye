You are an experienced senior software engineer and technical writer. Your task is to create a clear, professional, and well-structured README.md file for my project.
Follow these rules:
Output format
Use GitHub-flavoured Markdown.
Return only the complete README.md content, no explanations.
Use concise, professional language.
Required sections (in this order)
Title (project name, with a short one-line tagline).
Badges (optional placeholder badges for build, tests, license, etc.).
Description (what the project does, who it is for, and the main problem it solves).
Table of Contents (with links to all main sections).
Features (bullet list of key features / capabilities).
Tech Stack (list the main technologies, frameworks, and tools).
Architecture Overview (with at least one architecture diagram).
Installation (step-by-step instructions to set up the project from scratch).
Usage (how to run and use the project, including example commands and code snippets).
Configuration (environment variables, config files, or important settings).
Screenshots / Demo (placeholders or markdown image tags, plus link to live demo if available).
API / CLI Reference (if relevant, show a small, clear example of usage or endpoints).
Tests (how to run tests and what test framework is used).
Roadmap (optional bullet list of planned improvements).
Contributing (clear, simple guidelines for contributions and where to open issues/PRs).
License (state license type and reference LICENSE file).
Contact / Support (how to reach the maintainer, plus links like GitHub, website, email).
Architecture diagrams (important)
Add a section called “Architecture Overview”.
Inside it, include an inlined Mermaid diagram using a fenced code block with mermaid as the language.
Use a simple but realistic diagram (e.g., system context or container diagram) showing: client, backend/service, database, and any external services.
Example structure (adapt it to this project, don’t just copy):
text
flowchart LR
  User[User] --> WebApp[Web App]
  WebApp --> API[Backend API]
  API --> DB[(Database)]
  API --> ExternalService[External Service]

Add a short 2–3 sentence explanation under the diagram describing the main components and how they interact.
If project details are missing, keep the diagram generic and use clearly marked placeholders like WebApp, API, DB, ExternalService.
Style and clarity
Use headings and subheadings to keep sections scannable.
Use bullet lists for steps, requirements, and features.
Include at least one short practical usage example (command or code snippet).
Avoid overly casual language; keep it friendly but professional.
Assume this will be public on GitHub and should make the project easy to understand in under 1 minute.
Project details
Use the following details to customize the README (fill gaps with reasonable placeholders, but keep them clearly marked so I can edit later):
Project name:
Short tagline:
One-paragraph description:
Target users:
Key features:
Main tech stack:
Install requirements (e.g., Node version, Python version, databases):
How to run locally:
How to run tests:
Does it have an API or CLI? (If yes, describe briefly):
Main components for the architecture diagram (e.g., “React SPA, Node API, PostgreSQL DB, Redis cache”):
License:
Maintainer name and contact links:
Live demo / deployment URL (if any):
If any of the above fields are missing, use generic but clearly marked placeholder text like: <ADD PROJECT DESCRIPTION HERE>.
Generate the final README.md now.
