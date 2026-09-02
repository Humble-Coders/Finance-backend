# FinAI — Backend

[![CI](https://github.com/Humble-Coders/Finance-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/Humble-Coders/Finance-backend/actions/workflows/ci.yml)

FastAPI service behind the FinAI mobile and web clients. Owns all business logic,
all authoritative financial math, and all AI orchestration.

- Product spec: [docs/PRD.md in FinAI-Mobile-2026](https://github.com/Humble-Coders/FinAI-Mobile-2026/blob/main/docs/PRD.md) — canonical, not duplicated here
- Conventions: `CLAUDE.md`
- Data platform: Supabase (Postgres, Auth, Storage, Queues, pgvector) — `us-east-1`
- Hosting: Render (`finai-api` web service, `finai-worker` background worker) — Virginia

## Local development

Python **3.12** (pinned in `.python-version`; Render uses `PYTHON_VERSION` in
`render.yaml` — keep the two in step).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # fill in from your own Supabase project
uvicorn app.main:app --reload
```

API docs at `/docs` (disabled in production).

Tests:

```bash
pytest
```

## Layout

```
app/
├── main.py            FastAPI entrypoint
├── config.py          settings from environment
├── db.py              async SQLAlchemy engine + session
├── auth.py            Supabase JWT verification (JWKS)
├── api/               HTTP routes
├── core/money.py      the decimal-string <-> minor-units boundary
└── workers/main.py    queue consumer (extraction pipeline)
alembic/               database migrations
tests/
render.yaml            Render Blueprint (both services)
```

## Deployment

Render reads `render.yaml`. Both services must be in the **virginia** region to sit
beside the Supabase project. Environment variables come from the Render environment
group `finai-shared` — never from this repo.

## Notes

- The `DATABASE_URL` must be Supabase's **transaction pooler** string (port 6543),
  not the direct `:5432` connection.
- This repo is public. No secrets, ever. If a key is committed by accident, rotate it
  in Supabase immediately — deleting the commit is not sufficient.
