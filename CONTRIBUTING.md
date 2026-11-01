# Contributing / Coding Standards

## Branch strategy

Trunk-based, not git-flow — one long-lived branch, short-lived feature
branches:

- `main` — always deployable. Nothing broken gets merged here.
- `feature/<short-description>` — new functionality (e.g. `feature/gmail-connector`).
- `fix/<short-description>` — bug fixes.
- `chore/<short-description>` — tooling, deps, docs, CI.

Branch off `main`, open a PR back into `main`, merge once it's green.
Prefer small, frequent PRs over long-lived branches — the ingestion/connector
work naturally splits by source (one PR per connector), which keeps reviews
scoped.

## Commit messages

Conventional-commit style prefixes: `feat:`, `fix:`, `chore:`, `refactor:`,
`docs:`, `test:`. Keep the subject line under ~70 chars; explain *why* in
the body when it's not obvious from the diff.

## Python (backend)

- **Formatting/linting**: `ruff` (configured in `backend/pyproject.toml`) —
  run `ruff check .` and `ruff format .` before committing.
- **Typing**: type hints required on function signatures (`mypy` enforces
  this via `disallow_untyped_defs`). Run `mypy app`.
- **Tests**: `pytest`, async tests via `pytest-asyncio` (`asyncio_mode = auto`,
  no per-test decorator needed). New endpoints need at least one test.
  **Run pytest against a separate `digitaltwin_test` database, never the
  `digitaltwin` one `docker compose up` uses** — tests commit real rows and
  don't clean up after themselves, and it's easy to confuse the two since
  both live in the same Postgres container on the same port. Locally:
  `createdb`-equivalent is `psql -c "CREATE DATABASE digitaltwin_test"` then
  `psql -d digitaltwin_test -c "CREATE EXTENSION vector"`, then point
  `DATABASE_URL` at it before running `alembic upgrade head` / `pytest`. CI
  is unaffected — its Postgres service is a fresh container every run.
- **Structure**: keep the layering in `backend/app/` — `api/` (routing only),
  `schemas/` (Pydantic I/O models), `models/` (SQLAlchemy ORM), `db/`
  (engine/session), `core/` (config/security), `workers/` (Celery tasks).
  Business logic that isn't trivial routing belongs in a `services/` module
  (add it when the first non-trivial service lands — no empty scaffolding
  for logic that doesn't exist yet).
- **Migrations**: every model change ships an Alembic migration in the same
  PR (`alembic revision --autogenerate -m "..."`) — never hand-edit the DB
  schema out of band. Known gotcha: autogenerate on a column using
  `pgvector`'s `Vector` type emits `pgvector.sqlalchemy.vector.VECTOR(...)`
  in the generated file but doesn't add the import — add
  `import pgvector.sqlalchemy` to the migration by hand or `alembic upgrade`
  will fail with a `NameError`.

## Safety-critical rule

Any tool/endpoint that causes a real external side effect (sending an
email, deleting a file, modifying a calendar event, etc.) must produce a
preview/draft and require explicit user confirmation before executing.
This is a hard requirement from the project's design decisions, not a
style preference — see `ARCHITECTURE.md`.

## Frontend

Vanilla HTML/CSS/JS, no build step currently. Keep it that way until there's
a concrete reason to add one (e.g. real componentization need) — don't
introduce a framework preemptively.
