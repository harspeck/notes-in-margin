# CLAUDE.md

Rules and pointers for working in this repo. Update CLAUDE.md in the same PR as any change
to the behaviour it describes, because a stale rule here misleads every later session.

## 1. Subagents

| Tier | Use for |
|---|---|
| Model unset (inherits the session model) | Anything trusted without re-checking: implementation, debugging, review |
| `opus` | Well-scoped edits, and tests written against a clear spec |
| `sonnet` | Search sweeps, collecting call sites, mechanical edits |

When unsure, go one tier higher.

## 2. What this project is

"Notes in the Margin" is a single-user, local-only reflective-listening prototype. A
researcher reads Cortico conversation transcripts, highlights passages, records voice memos
that are transcribed locally, and files AI-suggested research-question (RQ) connections, next
steps and code families into a memo. The stack is FastAPI + SQLAlchemy (`backend/`) over two
SQLite files, OpenAI's Responses API, local faster-whisper, and React 19 + Vite +
TypeScript (`frontend/`). Source data is `acp_pipeline_snapshot.db`, a read-only snapshot of
the Cortico pipeline (201 conversations, 54 projects); everything the app writes goes to
`db/prototype.db`.

Retired. Never bring these back:
- **`scoped_conversation_ids`** (the config key and the `[9, 10]` filter on `GET /conversations`), because the full 201-conversation corpus is intentional (removed in 61c716a; see `docs/prototype-log.md`).
- **The PRD's original names:** the `highlight` table (it is `rl_highlight`, because the snapshot's naming would collide), `snippet_uuid` (Cortico's integer `snippet_id` is used), `guide_prompt_id` (it is `guide_question_id`, after the snapshot's `guidequestion` table), and tuple-shaped words (they are dicts), because `docs/prototype-log.md` records each deviation and the code depends on the new names.
- **Embeddings, vector search or a keyword heuristic for next-step recommendations**, because `docs/prd-phase-2.md` decided against them. Don't re-litigate.
- **`uvicorn --reload`**, because it leaves orphaned workers that don't show as "uvicorn" (`docs/prototype-log.md`). Stop the server by port.

## 3. Development commands

Every command below was run and worked on 2026-10-26.

| Purpose | Command |
|---|---|
| Setup (backend) | `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` |
| Setup (frontend) | `cd frontend && npm install` |
| Run backend | `cd backend && .venv/bin/uvicorn app.main:app --port 8000` (no `--reload`; `GET /health` → `{"ok":true}`) |
| Run frontend | `cd frontend && npm run dev`, then open http://localhost:5173 |
| Test | `cd backend && .venv/bin/python -m pytest -q` (80 tests, about 1.3 s) |
| Lint | `cd frontend && npm run lint` (oxlint; exits 0 with 10 existing warnings) |
| Typecheck | `cd frontend && npx tsc -b --noEmit` |
| Build | `cd frontend && npm run build` |
| Back up | `cd backend && .venv/bin/python scripts/export_md.py` (Markdown export plus a SQLite copy into `db/local/`; `--no-db-backup`, `--out-dir DIR`) |

- **Ports are fixed:** the backend must be on 8000 and the frontend on 5173, because `frontend/src/lib/api.ts` hard-codes `BASE = "http://localhost:8000"`, there is no Vite proxy, and CORS in `backend/app/main.py` allows only `http://localhost:5173`.
- **What tests need:** no network, no API key (they pass with a fake `OPENAI_API_KEY` and network blocked) and no running server. They use `acp_pipeline_snapshot.db` at the repo root when it is there. Without it (and with `CORTICO_DB_PATH` unset), `tests/conftest.py` builds a synthetic stand-in with `tests/fixtures/build_snapshot_fixture.py` into a temp file and points `CORTICO_DB_PATH` at it. The fixture reproduces only the snapshot facts the tests assert on, so a new test that asserts on other real data must add them to the builder.
- **The tests' prototype database:** `tests/conftest.py` points `PROTOTYPE_DB_PATH` at a temp file before importing the app, using `setdefault`. Never export `PROTOTYPE_DB_PATH` in your shell to the real dev database, because the tests would then write test rows into it.
- **In a git worktree:** the snapshot, `backend/.venv` and `frontend/node_modules` are gitignored and missing. Symlink the main checkout's `acp_pipeline_snapshot.db` (if you have it; the tests fall back to the fixture otherwise) and `frontend/node_modules` into the worktree, and run the main checkout's `backend/.venv/bin/python`.
- **To smoke-test the server without touching dev data:** set `PROTOTYPE_DB_PATH=<scratch file>`, because startup runs `create_all` and seeds RQs into whichever database it is pointed at.
- **Gaps:** there is no backend lint or typecheck and no frontend tests.
- **First transcription** downloads the faster-whisper `small.en` model (about 500 MB). It needs network once and runs locally after that.

## 4. Production safety

There is no deployment: no Docker, Vercel, CI or Procfile, and `localhost` is hard-coded on
both sides. The shared state is local, and these rules protect it:

| State | Rule |
|---|---|
| `acp_pipeline_snapshot.db` | Never write to it, because it is the source data. `cortico_engine` in `backend/app/db.py` opens it with `mode=ro&immutable=1` and `PRAGMA query_only = ON`. |
| `db/prototype.db` | The user's real dev data. Never wipe, recreate or bulk-edit it, because it can't be regenerated. |
| Schema changes | There are no migrations; `PrototypeBase.metadata.create_all` (in `main.py`'s lifespan) only adds missing tables. A new table only needs a name checked against `db/schema.sql`. A new or changed column on an existing table: **stop and ask the user how to migrate**, and give the column, type, default and the tables affected. |
| Backups in `db/local/` | `exports/` (Markdown memo and transcripts), `exports-<date>/` (earlier exports) and `prototype-<timestamp>.db` (database copies) are the user's backups. Refresh them only when the user asks, and only with `scripts/export_md.py`, because it moves the previous export aside and never overwrites a backup. Never delete or hand-edit them. |
| Paid OpenAI calls | `POST /voice_memos/{id}/review`, `POST /next_steps/generate`, `POST /research_questions/{rq_id}/code_families/synthesize`, `scripts/backfill_gists.py`. Ask before triggering any of them. Tests never call the model; check a live round trip by hand only with the user's go-ahead. |
| Backfill scripts on the dev database | The user runs these. Hand off the dry run first as one line, e.g. `cd backend && .venv/bin/python scripts/backfill_evidence_highlights.py --dry-run`, say what output to expect, then give the real run. Try it first against a copy via `PROTOTYPE_DB_PATH`. |
| `backend/.env` | Holds `OPENAI_API_KEY` and `OPENAI_BASE_URL`. Never print or open it; check by variable name only. The user edits it. |
| Git and GitHub | PRs go to `github.com/mdetwiller/notes-in-margin`. Never push, open a PR or merge without the user asking, and only merge PRs this session opened. Commit on a branch, never on `main`. |

## 5. Architecture map

Read the linked doc before changing an area. The rules here are the ones a change elsewhere
can break.

**Databases** (`backend/app/db.py`, `models_cortico.py`, `models_prototype.py`)
- Never `add()` or `commit()` through a Cortico session or model, because the snapshot is read-only by design and the engine refuses writes.
- Every new prototype table must have a name that doesn't collide with `db/schema.sql`, because both databases share one naming world (`highlight` had to become `rl_highlight`). `db/schema.sql` is the snapshot's schema, for reference only; never execute it, except into a new scratch file as the test fixture builder does.
- Rejected, dismissed, merged and retracted rows are status changes, never deletes, because the review trail must stay auditable.

**Evidence provenance** (`backend/app/evidence_provenance.py`)
- Every evidence write goes through `resolve_evidence`, either directly (`persist_review_result` in `services/ai_review.py`) or via `evidence_row` (`routers/review.py`, `routers/memo.py`). That is the only place `connection_evidence.highlight_id` and `source` get set, and it drives the `yours`/`AI-suggested` badge, the memo's starred flag, and "Starred, unassigned".
- Spans are half-open word ranges `[start_index, end_index)`, the same as `highlight_snippet`, because overlap matching depends on it.
- "Starred, unassigned" is computed only by `assigned_highlight_ids`, because review and memo must agree.

**AI calls** (`services/ai_review.py`, `services/next_steps.py`, `services/code_families.py`, `scripts/backfill_gists.py`)
- Every id the model returns (snippet, conversation, connection, family, RQ) is checked against the real database before it is saved, because the model can invent ids.
- The AI only writes `status="suggested"` rows with `ai_metadata` (`model`, `reasoning_effort`, `prompt`, …), because only the user files things, and every AI row must be auditable.
- Never hard-code a model id or effort in a service, because they come from `config.ai_review` in `backend/config.yaml` (`gpt-5.6-luna`, `low`). The single deliberate pin is `reasoning_effort="low"` in `backfill_gists.py`.
- Never re-run the AI when a review is resumed, because it would duplicate suggestions (`ReaderPage.tsx` falls back to `GET /voice_memos/{id}/review`).

**Review and memo** (`routers/review.py`, `routers/memo.py`)
- `POST /voice_memos/{id}/file` (`file_to_memo`) is the only route that moves a connection to accepted or edited, because nothing may reach the memo before "File to memo" (`tests/test_review_filing.py`).
- An edit writes a `ReflectionConnectionRevision` first, then sets `source="user"` and `edited_from_ai=True`, because prior versions are kept.
- A highlight's `project_id` comes from the Cortico conversation, never from `config.default_project_id`, because that was a real bug (`tests/test_highlights.py`).

**Code families** (`services/code_families.py`, `routers/code_families.py`)
- The per-RQ cap counts only `LIVE_FAMILY_STATUSES = ("suggested", "accepted")`, against `config.code_families` (`max_per_rq: 5`, `near_cap_at: 4`, `max_new_per_run: 2`), and is enforced server-side. `override_cap` still limits a run to `max_new_per_run`.
- Accepting a merge re-checks that every source family is still live, and returns 409 if not, because the merge suggestion may be stale. A user merge (`POST /code_families/merge`) and an accepted suggestion share `merge_families`.
- Every user edit to a family (rename, members, merge) writes a `CodeFamilyRevision` first, because `code_family_member` rows are deleted when a member is removed and the revision is their audit trail.
- Starring a family (`rl_code_family_star`) also accepts a `suggested` one, because only user-filed families may reach the codebook export. Starring a `dismissed` or `merged` family returns 409.

**RQ merges and focus** (`services/rq_merges.py`, `routers/research_questions.py`, `routers/organize.py`)
- Every RQ-scoped read (memo sections, code family lists, the cap, synthesis input, `/organize`, `/codebook_prep`) goes through `resolve_rq_id` and `rq_group_ids`, because a merged RQ's entries and families keep their own `research_question_id` and must read under the target. `services/code_families.group_for` wraps both.
- A merge archives the source; undo sets `rl_rq_merge.status = "undone"` and un-archives it. Merge rows are never deleted. Merging an archived source, into an archived or merged target, or into itself returns 409, and so does un-archiving a merged RQ through `PATCH /research_questions/{id}`.

**Values defined twice. Change both together.**
- `FILED_STATUSES = ("accepted", "edited")` is in both `routers/memo.py` and `services/code_families.py`.
- The voice memo cap: `voice_memo.max_duration_seconds: 1200` in `config.yaml`, and `MAX_VOICE_MEMO_SECONDS = 1200` in `frontend/src/pages/ReaderPage.tsx`.
- Status values: each backend status (for example the code family's `merged`) also appears in a union in `frontend/src/lib/types.ts`. This includes `rl_rq_merge.status` (`"active" | "undone"`), in the `RQMerge` type in `types.ts`.
- The focus range `(5, 7)`: `FOCUS_RANGE` in `routers/organize.py`, which `GET /organize` also returns as `focus_range`.

### Where the detail lives

| Before touching | Read |
|---|---|
| Models, tables, engines, schema changes | `docs/architecture/databases.md` |
| Any OpenAI call, prompt, output schema or gist backfill | `docs/architecture/llm.md` |
| Any endpoint (method, path, owning router) | `docs/architecture/routers.md` |
| Review, filing, memo, evidence and highlights | `docs/architecture/review-and-memo.md` |
| Code families, the cap, merges | `docs/architecture/code-families.md` |
| Frontend pages, components, `api.ts`, `types.ts` | `docs/architecture/frontend.md` |
| Writing or changing tests | `docs/architecture/tests.md` |

## 6. Key conventions

- **Python modules** start with `from __future__ import annotations`. Request bodies are Pydantic models in `backend/app/schemas.py`. Responses are hand-built dicts from each router's `_serialize*` helpers; there are no response models.
- **`frontend/src/lib/types.ts`** mirrors those serialized dicts, not `schemas.py`. Every backend route has one `api.*` method in `frontend/src/lib/api.ts`.
- **Error shapes:**
  - Expected errors are `HTTPException` with a `{"detail": ...}` body.
  - `api.ts` throws `Error("<status> <path>: <body>")` for any non-OK response.
  - **Known gap:** OpenAI failures are not caught, so they surface as a bare 500, and no timeout or retry is set (SDK defaults apply).
- **Frozen formats:**
  - Words JSON is `[{word, start, end, confidence}]` dicts, never tuples, because it must match Cortico's `snippet.words` and `quoted_texts` indexes `w["word"]`.
  - `conversation_gist` is keyed by Cortico `conversation_id` and is never refreshed.
  - The editable review prompt lives in `app_setting` under `REVIEW_PROMPT_SETTING_KEY = "review_prompt"`.
  - Audio files sit flat in `db/local/audio`, because `audioUrlFor` in `api.ts` keeps only the basename.
- **Single user:** every user-scoped read and write uses `config.default_user_id` (101), and `GET /memo` doesn't filter by user. This is intentional; there is no auth.
- **Pinned dependency:** keep `av==13.1.0`, because 19.x breaks faster-whisper (`docs/prototype-log.md`).
- **Tests to copy:**
  - A router test: copy `backend/tests/test_review_filing.py` (seed rows with `PrototypeSession`, call through `TestClient(app)`).
  - Logic that runs after an LLM call: copy `backend/tests/test_code_families.py`, which feeds hand-built model output (including invalid ids) to the `_apply_*` helpers.
  - Never call the model in a test.

## 7. Pointers

| Doc | Read it when |
|---|---|
| `docs/prototype-log.md` | Before any schema, naming or scoping change. It records every deviation from the PRDs and the measured facts: snapshot data quirks, the model check of 2026-10-05, gist backfill timing and tokens (720.7 s for 198 calls), and live-run results. |
| `docs/prd-reflective-listening.md` | Before touching review, memo or evidence. This is the Phase 1 spec and acceptance criteria, and lists open bugs. |
| `docs/prd-phase-2.md` | Before touching next steps, gists or code families. This is the Phase 2 spec, decisions not to revisit, and open items. |
| `docs/architecture/*.md` | Before changing an area (see the table in section 5). |
| `README.md` | Human-facing setup and run steps. |
| `frontend/README.md` | Vite template boilerplate; ignore it. |
