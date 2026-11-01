# EchoGraph

**A personal memory layer for language models: retrieval, a knowledge graph, and confirmed actions over your own data.**

EchoGraph is a research prototype that explores one question: *what changes when an
assistant answers from your own history (mail, chats, notes, documents, photos, voice
notes) instead of from general knowledge, and acts on your behalf only with your say-so?*

> Status: working research prototype. It is built to test ideas, not to be a hardened product.

---

## Motivation

General-purpose assistants know the world but not you. The information that matters for
everyday questions ("what did I decide about the NOC?", "list my certifications", "what was
that receipt from March?") is scattered across tools and formats. EchoGraph treats a
person's data as a retrieval corpus and a graph, and studies how to make answers grounded,
cited and safe to act on.

## Ideas being explored

1. **One store for vectors, relations and the graph.** Postgres with `pgvector` holds
   relational data, embeddings, and graph edges as node/edge tables. The bet is that at
   single-user scale one database is simpler to operate and fast enough, compared with a
   separate vector DB and graph DB.
2. **Metadata-first aggregation.** Items are tagged with categories and entities at
   ingestion, so aggregate questions ("all my certifications") become metadata filters
   rather than purely semantic search.
3. **Grounded answers with citations.** Retrieval results, optionally filtered by
   category, date or source, are passed to the model together with source references.
4. **Confirmation-gated actions.** When a request implies a write (send an email, edit
   something), the agent prepares a draft and returns it. Nothing is executed on a
   connected account without explicit confirmation.
5. **Inline ingestion for uploads.** Files are parsed, chunked (~2,000 characters with
   overlap), embedded and stored inside the request, so the response is the final state
   with no worker hop.
6. **Local-first OCR.** Text in images is read with Tesseract on the server. EXIF capture
   dates become the memory's date, and photos without text are still indexed by filename,
   date and camera.
7. **Periodic reflections.** Scheduled jobs summarise recent memory into reflections.

## System overview

```
frontend (static HTML/CSS/JS, nginx)
        │  REST  /api/v1/*
backend (FastAPI): auth · connectors · memory · chat · graph · uploads
        │                         │
Postgres + pgvector          Redis ── Celery worker
(relational + embeddings     (connector polling, ingestion,
 + graph edges)               reflection generation)
```

Pipeline: **ingest → normalise → enrich → embed → retrieve → (confirm) act.**
See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and
[PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) for the file map.

## Stack

| Concern | Choice |
|---|---|
| API | FastAPI (async), SQLAlchemy 2, Alembic |
| Storage | PostgreSQL + `pgvector` |
| Background jobs | Celery + Redis |
| Embeddings | Voyage (`voyage-4`, 1024 dims) |
| Chat generation | Groq-hosted `openai/gpt-oss-120b` |
| Voice notes | Groq-hosted Whisper (`whisper-large-v3-turbo`) |
| Documents | pypdf, `.docx`, text/markdown, bookmark exports |
| Images | Pillow, pillow-heif, Tesseract OCR |
| Frontend | Static HTML/CSS/JS served by nginx |

## What works today

- Auth, a dashboard, a timeline with infinite scroll, a graph view, search and chat.
- File uploads: PDF, Word, text/markdown, images, voice notes and bookmark exports.
- Settings with profile, privacy and danger-zone endpoints.
- Containerised end to end (backend, worker, Postgres, Redis, frontend).

## Open questions and limitations

- No OCR yet for *scanned PDFs* (scanned images are covered).
- The enrichment step (document categories) is not built for uploads yet.
- Retrieval quality has not been formally benchmarked; there is no evaluation harness yet.
- WhatsApp auto-sync and Apple Notes automation are deliberately deferred: no official
  personal-account API exists, and the unofficial route carries ToS risk.
- Designed and tested at single-user data volumes only.

## Possible next experiments

- An evaluation set for retrieval quality (recall@k on personal-style queries).
- Comparing metadata filtering against pure semantic search for aggregate questions.
- Graph-aware retrieval (expanding hits along entity edges) versus flat top-k.
- Measuring how often confirmation-gated drafts are accepted unchanged.

## Running it

```
cp .env.example .env        # then fill in the keys you need
docker compose up --build
```

- Frontend: http://localhost:8080
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/api/v1/health

Backend without Docker:

```
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

This needs a reachable Postgres with the `pgvector` extension and Redis. Secrets are read
from environment variables and are never committed; see `.env.example`.

## Contributors

Built by Yashi Mishra ([@yashi-29](https://github.com/yashi-29)) together with Aditya
Kumarkhaniya. This repository starts from a fresh snapshot of the project, so earlier
per-commit history is not included here.

## License

MIT, see [LICENSE](LICENSE).
