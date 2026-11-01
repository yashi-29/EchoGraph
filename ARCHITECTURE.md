# Architecture

## Concept

A personal digital twin: an assistant that answers questions from the user's
*own* data (email, chat, notes, docs, calendar) instead of general knowledge,
and can take actions on their behalf (e.g. sending an email) with explicit
confirmation first.

## System layers

```
                        ┌─────────────────────┐
                        │   frontend (static)  │  HTML/CSS/JS, served by nginx
                        └──────────┬───────────┘
                                   │ REST (fetch), /api/v1/*
                        ┌──────────▼───────────┐
                        │   backend (FastAPI)   │  auth, connectors, memory,
                        │                       │  chat, graph endpoints
                        └───┬───────────────┬───┘
                            │               │
                 ┌──────────▼───┐   ┌───────▼────────┐
                 │  Postgres +   │   │     Redis       │  celery broker/backend
                 │   pgvector    │   └───────┬────────┘
                 │ (relational + │           │
                 │  embeddings + │   ┌───────▼────────┐
                 │  graph edges) │   │  celery worker  │  connector polling,
                 └───────────────┘   │                 │  ingestion, embedding,
                                     └─────────────────┘  reflection generation
```

One database (Postgres + the `pgvector` extension) holds relational data,
vector embeddings, and the knowledge graph (as node/edge tables) — avoids
running a separate vector DB or graph DB at personal-project scale.

Claude (Anthropic API) is the LLM for both RAG-style synthesis and the
agentic tool-use loop that drives actions.

## Data flow

1. **Ingestion** — a connector (OAuth-based background poller, or a manual
   file upload) produces raw items.
2. **Normalization** — each item becomes one or more `MemoryItem` rows:
   text content + source + timestamp + participants + metadata.
3. **Enrichment** — an LLM tags each item with categories/entities during
   ingestion (e.g. "certification", "NOC", person names) so aggregation
   queries like "list all my certifications" are metadata filters, not
   pure semantic search.
4. **Embedding** — content is embedded and stored in the `embedding` vector
   column for semantic retrieval.
5. **Retrieval** — a chat query is embedded, matched against stored vectors
   (optionally filtered by category/date/source), and the top results are
   fed to Claude with citations.
6. **Action** — if the query implies a write action (e.g. "email my NOC to
   Aditya"), the agentic loop calls a tool that prepares a draft and returns
   it to the user for confirmation. Nothing that sends/deletes/edits on a
   real connected account executes without that confirmation step.

## Connector rollout order

**Phase 2 — official APIs only:** Gmail, Google Calendar, Google Drive,
Outlook Calendar, Notion, Slack.

**Phase 3 — manual upload** (same ingestion pipeline, upload instead of
OAuth as the acquisition step): PDFs, voice notes (transcribed via Whisper),
browser bookmarks exports. WhatsApp chat exports and Apple Notes exports can
also land here as a manual stopgap.

*Status:* PDFs, Word `.docx`, `.txt`/`.md` notes, images (jpg, png, heic,
webp, gif, tiff, bmp, avif, svg — text read locally by Tesseract OCR, EXIF
capture date used as the memory's date; photos with no text are still indexed
by filename/date/camera), voice notes (Groq-hosted Whisper) and
Netscape-format bookmark exports are live via `POST /api/v1/uploads/` and
the Uploads page. Each file is parsed, chunked (~2,000 chars with overlap),
embedded and stored **inside the request** — no worker hop, so the response
is the final state. Originals are kept in the `uploads` table so later
actions can attach the real file; deleting an upload cascades to its
`memory_items`. Not yet: OCR for scanned PDFs (scanned *images* are covered), and the enrichment
step above (document categories).

**Deferred (explicitly, by user decision):**
- **WhatsApp automatic sync** — no personal-account API exists; automatic
  access requires an unofficial library (e.g. Baileys) with real ToS/ban
  risk. Building this only after the official-API connectors work.
- **Apple Notes automation** — no cloud API exists; automatic sync would
  require a local agent running on a Mac, which isn't currently available.

## Deployment

The stack is containerized end to end (backend, worker, Postgres, Redis,
frontend all have images) specifically so the deployment target stays an
open choice rather than baked into the code:

- **Single-host**: `docker compose up` on one VPS (e.g. a small cloud
  instance), optionally behind a reverse proxy (Caddy/nginx) for TLS. Cheapest,
  simplest, fine for a single-user personal project.
- **Split hosting**: frontend on a static host (Vercel/Netlify/Cloudflare
  Pages), backend + worker on a container platform (Fly.io/Railway/Render),
  Postgres on a managed provider (with the pgvector extension available).
  The only coupling point is `BACKEND_CORS_ORIGINS` and the frontend's API
  base URL — both are environment-driven, not hardcoded, so this split can
  happen later without restructuring code.

Either way: secrets (DB password, `SECRET_KEY`, `ANTHROPIC_API_KEY`, OAuth
client secrets) are injected via environment variables, never committed —
see `env.example` for the full list. The backend Dockerfile is a multi-stage
build (build tools only in the builder stage, slim non-root runtime image)
so it's already reasonably production-shaped, not just a dev convenience.

## Why these specific choices

- **FastAPI over Django/Flask**: async-native, and ingestion/RAG code is
  mostly I/O-bound (API calls, embedding calls) where async pays off.
- **Postgres+pgvector over a dedicated vector DB**: one less service to run,
  operate, and pay for; sufficient performance at single-user data volumes.
- **Celery+Redis over cron/in-process jobs**: connector polling and
  ingestion (embedding, transcription) are exactly the "background task
  queue" shape Celery is built for, and the same broker doubles as the
  Phase 6 scheduling mechanism for weekly reflections.
- **psycopg3 for both the async app engine and Alembic's sync migrations**:
  one driver instead of two (asyncpg + psycopg2), less to keep in sync.
