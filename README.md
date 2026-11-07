# Reflective Listening Prototype

See `docs/prd-reflective-listening.md` for the spec and `docs/prototype-log.md` for every
place the build diverges from it (naming, schema, scoping).

## One-time setup

```sh
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

To enable the AI steps (review, next steps, code families), create `backend/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Without it, everything works except those AI steps, which answer 503. To use OpenAI instead,
set `OPENAI_API_KEY` there and run with `AI_PROVIDER=openai AI_MODEL=gpt-5.6-luna`.
faster-whisper downloads its model (`small.en`, ~500MB) on first
transcription — needs network once, runs local after that.

```sh
cd frontend
npm install
```

## Running it

```sh
# backend (from backend/, with .venv active)
uvicorn app.main:app --port 8000

# frontend (from frontend/)
npm run dev
```

Open http://localhost:5173. Locally the app reads the snapshot (`acp_pipeline_snapshot.db`) as
one user and lists every conversation in it.

## Cortico mode and the hosted backend

With `DATA_SOURCE=cortico_api`, users sign in with their Cortico account, choose a workspace
(a named bundle of projects in an organization), and the backend reads the Cortico v2 API with
their token. Highlights are written back to Cortico. The hosted backend is the Vercel project
`notes-in-the-margin-api`, with a Neon Postgres sidecar. See `docs/architecture/cortico.md`.

To check the live pipes with your own Cortico account:

```sh
cd backend && .venv/bin/python scripts/cortico_smoke.py --api <backend url> --org 353
```

## Tests

```sh
cd backend && source .venv/bin/activate && OPENAI_API_KEY=sk-fake python -m pytest
```

Tests run against a scratch database (see `tests/conftest.py`) and the read-only Cortico
snapshot, or a synthetic stand-in when the snapshot isn't present. They never touch
`db/prototype.db`, Postgres, or Cortico.
