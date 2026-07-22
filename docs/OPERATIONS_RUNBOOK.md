# Operations Runbook (V2)

> Stub created in Phase B; completed in Phase K per the master build prompt.

## Local database (Phase B)

One-line setup: copy `.env.example` to `.env`, set `POSTGRES_PASSWORD`, then
`docker compose up -d postgres` and `.\.venv\Scripts\python.exe -m alembic upgrade head`.

- PostgreSQL is the only engine (prototype and live); it binds to `127.0.0.1` only.
- Schema changes happen exclusively through Alembic migrations
  (`src/persistence/migrations/versions/`), one per Section 14 table, all reversible.
- The test suite provisions its own ephemeral Postgres via testcontainers (Docker must
  be running); set `TEST_DATABASE_URL` to use a standing local test DB instead.
- V1's SQLite `data/trading.db` belongs to the archived V1 system and is never touched.

## Startup / shutdown / reconciliation / emergency halt / recovery

To be written in Phase K.
