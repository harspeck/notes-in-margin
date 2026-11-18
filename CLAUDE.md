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

"Notes in the Margin" is a reflective-listening prototype. A
researcher reads Cortico conversation transcripts, highlights passages, records voice memos
that are transcribed locally, and files AI-suggested research-question (RQ) connections, next
steps and code families into a memo. The stack is FastAPI + SQLAlchemy (`backend/`) over two
SQLite files, Claude through the Anthropic SDK (`app/llm.py`), local faster-whisper, and React 19 + Vite +
TypeScript (`frontend/`). Source data is `acp_pipeline_snapshot.db`, a read-only snapshot of
the Cortico pipeline (201 conversations, 54 projects); everything the app writes goes to
`db/prototype.db`.

It runs in two modes (`data_source`, see `docs/architecture/cortico.md`). In `snapshot` mode it
reads that snapshot as one implicit user (local dev and tests). In `cortico_api` mode, the
deployed one, users sign in with their Cortico account, pick a workspace (a named bundle of
projects), and every Cortico read goes to the v2 API with their token. That mode's sidecar is
Neon Postgres (`DATABASE_URL`), and highlights are written back to Cortico.

Retired. Never bring these back:
- **`scoped_conversation_ids`** (the config key and the `[9, 10]` filter on `GET /conversations`), because the full 201-conversation corpus is intentional (removed in 61c716a; see `docs/prototype-log.md`). Scoping now comes only from the user's workspace (its project ids), never from a hard-coded id list.
- **The PRD's original names:** the `highlight` table (it is `rl_highlight`, because the snapshot's naming would collide), `snippet_uuid` (Cortico's integer `snippet_id` is used), `guide_prompt_id` (it is `guide_question_id`, after the snapshot's `guidequestion` table), and tuple-shaped words (they are dicts), because `docs/prototype-log.md` records each deviation and the code depends on the new names.
- **Embeddings, vector search or a keyword heuristic for next-step recommendations**, because `docs/prd-phase-2.md` decided against them. Don't re-litigate.
- **`uvicorn --reload`**, because it leaves orphaned workers that don't show as "uvicorn" (`docs/prototype-log.md`). Stop the server by port.

## 3. Development commands

Every command below was run and worked on 2026-10-26.

| Purpose | Command |
|---|---|
| Setup (backend) | `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt` (`requirements.txt` is the deployed runtime set; `-local` adds faster-whisper; `-dev` adds pytest) |
| Setup (frontend) | `cd frontend && npm install` |
| Run backend | `cd backend && .venv/bin/uvicorn app.main:app --port 8000` (no `--reload`; `GET /health` → `{"ok":true}`) |
| Run frontend | `cd frontend && npm run dev`, then open http://localhost:5173 |
| Test | `cd backend && OPENAI_API_KEY=sk-fake .venv/bin/python -m pytest -q` (188 tests, about 4.5 s) |
| Create sidecar tables | `cd backend && .venv/bin/python scripts/init_db.py --env-file .env.local` (after `vercel env pull`; idempotent) |
| Live Cortico check | `cd backend && .venv/bin/python scripts/cortico_smoke.py --api <url>/api --org 353` (the user runs it; it prompts for their Cortico password) |
| Deploy (preview) | From the repo root: `vercel deploy . --project notes-in-the-margin-api --environment preview --yes --scope harrisonspeck-8050s-projects` (ships frontend and backend together) |
| Check a preview | `cd backend && vercel curl /api/health --deployment <preview url>` (previews sit behind Vercel sign-in; `backend/` holds the project link) |
| Lint | `cd frontend && npm run lint` (oxlint; exits 0 with 2 existing warnings) |
| Typecheck | `cd frontend && npx tsc -b --noEmit` |
| Build | `cd frontend && npm run build` |
| Back up | `cd backend && .venv/bin/python scripts/export_md.py` (Markdown export plus a SQLite copy into `db/local/`; `--no-db-backup`, `--out-dir DIR`) |

- **Ports are fixed locally:** the backend must be on 8000 and the frontend on 5173, because in development `BASE` in `frontend/src/lib/api.ts` is `http://localhost:8000` (unless `VITE_API_BASE` is set) and there is no Vite proxy. A production build uses `/api`, which the root `vercel.json` routes to the backend in the same deployment, so the session cookie is same-origin and `CORS_ORIGINS` doesn't need the deployed address. CORS comes from `cors_origins` (env `CORS_ORIGINS`, comma-separated) and allows credentials, for the session cookie.
- **Always deploy with `--project`:** a bare `vercel deploy` from the repo root isn't linked to anything, so it creates and ships a new project. That happened once (the stray `reflective-listening` project, 2026-11-06). Deploy from the repo root, never from `backend/` or `frontend/`: the root `vercel.json` is what puts both in one deployment, and the root `.vercelignore` is what keeps the snapshot, `db/`, `docs/` and `mocks/` from being uploaded.
- **Tests never use a real database:** `tests/conftest.py` drops `DATABASE_URL`, `POSTGRES_URL`, `DATA_SOURCE` and `SESSION_SECRET` before importing the app. `tests/test_cortico_api_mode.py` runs `cortico_api` mode against an `httpx.MockTransport` fake of Cortico (set `app.auth.TRANSPORT`).
- **What tests need:** no network, no API key (they pass with no `ANTHROPIC_API_KEY`, a fake `OPENAI_API_KEY` and network blocked) and no running server. They use `acp_pipeline_snapshot.db` at the repo root when it is there. Without it (and with `CORTICO_DB_PATH` unset), `tests/conftest.py` builds a synthetic stand-in with `tests/fixtures/build_snapshot_fixture.py` into a temp file and points `CORTICO_DB_PATH` at it. The fixture reproduces only the snapshot facts the tests assert on, so a new test that asserts on other real data must add them to the builder.
- **The tests' prototype database:** `tests/conftest.py` points `PROTOTYPE_DB_PATH` at a temp file before importing the app, using `setdefault`. Never export `PROTOTYPE_DB_PATH` in your shell to the real dev database, because the tests would then write test rows into it.
- **In a git worktree:** the snapshot, `backend/.venv` and `frontend/node_modules` are gitignored and missing. Symlink the main checkout's `acp_pipeline_snapshot.db` (if you have it; the tests fall back to the fixture otherwise) and `frontend/node_modules` into the worktree, and run the main checkout's `backend/.venv/bin/python`.
- **To smoke-test the server without touching dev data:** set `PROTOTYPE_DB_PATH=<scratch file>`, because startup runs `create_all` and seeds RQs into whichever database it is pointed at.
- **Gaps:** there is no backend lint or typecheck and no frontend tests.
- **First transcription** downloads the faster-whisper `small.en` model (about 500 MB). It needs network once and runs locally after that.

## 4. Production safety

The app deploys to Vercel as one project, `notes-in-the-margin-api` (team Cortico-Prototypes),
at `https://notes-in-the-margin-api.vercel.app`. The root `vercel.json` defines two services:
`web` (`frontend/`, the Vite build, served at `/`) and `api` (`backend/`, entrypoint
`backend/index.py`, served under `/api` with the prefix stripped, so `GET /health` is
`/api/health` when deployed). The backend runs in `cortico_api` mode with a Neon Postgres
sidecar from the Vercel Marketplace. Preview deployments carry the same `DATABASE_URL`
variable as production, so treat a preview as reading and writing real users' data. These rules protect the
shared state:

| State | Rule |
|---|---|
| `acp_pipeline_snapshot.db` | Never write to it, because it is the source data. `cortico_engine` in `backend/app/db.py` opens it with `mode=ro&immutable=1` and `PRAGMA query_only = ON`. |
| `db/prototype.db` | The user's real dev data. Never wipe, recreate or bulk-edit it, because it can't be regenerated. |
| Schema changes | There are no migrations; `PrototypeBase.metadata.create_all` (in `main.py`'s lifespan) only adds missing tables. A new table only needs a name checked against `db/schema.sql`. A new or changed column on an existing table: **stop and ask the user how to migrate**, and give the column, type, default and the tables affected. |
| Backups in `db/local/` | `exports/` (Markdown memo and transcripts), `exports-<date>/` (earlier exports) and `prototype-<timestamp>.db` (database copies) are the user's backups. Refresh them only when the user asks, and only with `scripts/export_md.py`, because it moves the previous export aside and never overwrites a backup. Never delete or hand-edit them. |
| Paid model calls (Claude) | `POST /voice_memos/{id}/review`, `POST /next_steps/generate`, `POST /research_questions/{rq_id}/code_families/synthesize`, `POST /merge_nudges/generate`, `POST /research_questions/{rq_id}/find_passages` (one call per conversation searched, up to `find_passages.max_conversations_per_run`), `POST /research_questions/{rq_id}/whats_missing`, `POST /gists/generate` (one call per conversation, up to `gists.max_per_request`), `scripts/backfill_gists.py`. Two more call the model only when their cache rules allow, and the Desk and the Review page call them on their own: `POST /listening_interests/suggestions/refresh` and `POST /next_steps/recommend`. Ask before triggering any of them. Tests never call the model; check a live round trip by hand only with the user's go-ahead. |
| Backfill scripts on the dev database | The user runs these. Hand off the dry run first as one line, e.g. `cd backend && .venv/bin/python scripts/backfill_evidence_highlights.py --dry-run`, say what output to expect, then give the real run. Try it first against a copy via `PROTOTYPE_DB_PATH`. |
| `backend/.env` | Holds `ANTHROPIC_API_KEY` (and the older `OPENAI_API_KEY` and `OPENAI_BASE_URL`). Production reads `ANTHROPIC_API_KEY` from the Vercel env. Never print or open it; check by variable name only. The user edits it. |
| `backend/.env.local` and Vercel env | `vercel env pull` writes the Neon credentials here. Never print values; use `vercel env ls` for names. `SESSION_SECRET` encrypts every stored Cortico token: rotating it signs everyone out. |
| Neon Postgres (`DATABASE_URL`) | Real users' sidecar data. Never run tests or ad-hoc writes against it. Schema changes there follow the same rule as below: new tables only, and ask before any column change. |
| Cortico | Writes go only through `app/cortico/write_back.py`. Never call a Cortico write route by hand, and never delete a Cortico object this app didn't create (`rl_cortico_sync` records which ones it did). Live checks run through `scripts/cortico_smoke.py`, by the user. |
| Vercel production | Deploy previews freely; ask before `vercel deploy --prod` or promoting. |
| Git and GitHub | This checkout is the fork `github.com/harspeck/notes-in-margin` (`origin`); `upstream` is `github.com/mdetwiller/notes-in-margin`. PRs go to the fork unless the user says upstream. The repo is public: never commit transcript text, `.env*`, the snapshot, or Cortico's OpenAPI spec. Never push, open a PR or merge without the user asking, and only merge PRs this session opened. Commit on a branch, never on `main`. |

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
- Never assume an insight's evidence is in its reflection's conversation, because an insight merge copies evidence across conversations. Serializers label each evidence row through `evidence_conversations` (`app/evidence_text.py`), and code that matches evidence to highlights for a filed insight uses `user_highlight_spans` (by snippet), not `highlight_spans` (by conversation).

**AI calls** (`services/ai_review.py`, `services/next_steps.py`, `services/code_families.py`, `services/merge_nudges.py`, `services/find_passages.py`, `services/question_gaps.py`, `scripts/backfill_gists.py`)
- Accepting an AI suggestion runs the function the user's own action runs (`merge_rq`, `merge_insights`, `create_highlight_for_user`, `merge_families`), because two copies of a merge or a highlight write would drift apart.
- A merge nudge pair is suggested once: `decided_pairs` (`services/merge_nudges.py`) rules out any pair with an earlier nudge of any status or a merge by hand, because "keep separate" must stick.
- The two routes the pages call unasked must stay cheap to repeat. `listening_suggestions.refresh` calls the model only when no offer is undecided, the fingerprint of the user's questions and newest highlights differs from the last `rl_listening_suggestion_run`, and that run is older than `min_minutes_between_runs`. `next_steps.recommend_next` calls it only when no undecided suggestion names an unopened conversation, a gist exists for one, the last `rl_recommendation_run` is older than `next_steps.min_minutes_between_auto_runs`, and its fingerprint (the candidate gists and the user's lines) has changed. Both save their run row before calling the model, so a second tab does not ask too, and mark it `failed` when the call does not finish, so a failure is retried only after the wait. A run that returns nothing must still leave a row, or the page would pay for the same call on every visit.
- `conversation_gist` and `rl_cortico_conversation` are shared by every user. In `cortico_api` mode, anything that shows gists to the model or accepts a conversation id from it must be limited to the user's workspace (`scope_ids` in `services/next_steps.py`, from `_scope_ids` in `routers/next_steps.py`), because `conversations_by_ids` reads the shared cache and is not an access check (`test_recommendations_never_draw_on_conversations_outside_the_users_workspace`).
- The AI sparkle (`AiMark` in `frontend/src/lib/ui.tsx`) is the one sign of AI in the interface: on a button it means pressing it asks the AI, on content it means the AI wrote it. Never label a control "Asks Claude".
- Every id the model returns (snippet, conversation, connection, family, RQ) is checked against the real database before it is saved, because the model can invent ids.
- The AI only writes `status="suggested"` rows with `ai_metadata` (`model`, `reasoning_effort`, `prompt`, …), because only the user files things, and every AI row must be auditable.
- Every model call goes through `structured_json` in `app/llm.py`, never a provider SDK directly, because that is where errors become `AIProviderError` and the provider switch lives. Never hard-code a model id or effort in a service, because they come from `config.ai_review` in `backend/config.yaml` (`anthropic`, `claude-opus-5-5`, `low`). A feature on another model passes `model=` from its own config block: `listening_suggestions` and `gists` use `claude-haiku-5-5`, which `llm.py` calls without the fallback beta because Haiku has none. The single deliberate pin is `effort="low"` in `backfill_gists.py`.
- Never re-run the AI when a review is resumed, because it would duplicate suggestions (`ReviewPage.tsx` only calls `GET /voice_memos/{id}/review`; the one `runReview` after a recording or a typed reflection is `connectToQuestions` in `Notebook.tsx`).

**Review and memo** (`routers/review.py`, `routers/memo.py`)
- `POST /voice_memos/{id}/file` (`file_to_memo`) is the only route that files a connection (moves it to accepted or edited) for the first time, because nothing may reach the memo before "File to memo" (`tests/test_review_filing.py`). The one deliberate exception is `POST /memo/connections/{id}/restore` (`restore_connection`), which puts a retracted entry back to the filed status it had, after writing a revision. It sends a connection rejected at review back to `suggested`, never into the memo (`tests/test_set_aside_and_activity.py`).
- Retracting and rejecting each write an `rl_set_aside` row (when, and the status before), because `reflection_connection` has no column for either and restore needs both.
- A typed reflection is a `voice_memo` row with `audio_path=""`, because the column is NOT NULL. Never treat `audio_path` as present: serializers return null and `typed: true` for it.
- An edit writes a `ReflectionConnectionRevision` first, then sets `source="user"` and `edited_from_ai=True`, because prior versions are kept.
- A highlight's `project_id` comes from the Cortico conversation, never from `config.default_project_id`, because that was a real bug (`tests/test_highlights.py`). In `cortico_api` mode the conversation's project comes from the workspace listing (`rl_cortico_conversation`), because v2 conversations carry only `forum_id`.
- Highlights that already exist in Cortico are shown, never stored or edited: `GET /conversations/{id}/cortico_highlights` (`services/cortico_highlights.py`) reads them live, leaves out the copies of the user's own highlights (`rl_cortico_sync`), and maps each audio range to words with `spans_for`, because Cortico's listing carries offsets and no word indices.
- Every highlight is created by `create_highlight_for_user` (`services/highlights.py`), by the reader and by an accepted passage suggestion alike, because the project id and the Cortico write-back must not differ between them.
- A connection with status `merged` was folded into another by `merge_insights` (`services/insight_merges.py`, logged in `rl_insight_merge`). It is neither filed nor set aside: never add it to `FILED_STATUSES` or `SET_ASIDE_STATUSES`, and `file_to_memo` skips decisions about it, because only `undo_insight_merge` may bring it back.
- A merge and its undo each write a `ReflectionConnectionRevision` of the target first, and undo removes the target's evidence by span (`rl_insight_merge.added_evidence_json`), never by row id, because an evidence edit re-creates the rows.
- Moving a filed insight (`PATCH /memo/connections/{id}` with `research_question_id`) writes a revision and an `rl_insight_move` row, because the revision table has no column for the question.
- Highlights filed under a question live in `rl_highlight_question`. Delete a highlight's links before the highlight (`questions.delete_links_for_highlight`), because the link has a foreign key to it.

**Cortico data and identity** (`app/cortico/`, `app/auth.py`, `routers/auth.py`, `routers/workspaces.py`)
- Every Cortico read goes through the request's data source (`Depends(get_source)` in routers, `as_source(cortico_db)` in services). Never query `models_cortico` from a router, because the API mode has no snapshot.
- Every route takes `user: CurrentUser = Depends(current_user)` and scopes by `user.id`. Rows owned by another user answer 404, never 403. `main.py` also puts `current_user` on every router except `/auth`.
- Cortico user ids are the user ids in API mode. Never hard-code a user, organization or project id outside `config.yaml`.
- Never call Cortico once per conversation to build a list. Use `scope_conversations` (the cached workspace listing) and `guide_questions_for_projects` (the `rl_cortico_guide` cache, with a fetch cap), because a workspace can hold thousands of conversations. `GET /research_questions/breadth`, `/guide_ideas` and `whats_missing` depend on this (`tests/test_cortico_api_mode.py` asserts the exact calls).

**Activity log and per-user state** (`services/activity.py`, `routers/desk.py`, `services/listening_interests.py`)
- Every `rl_activity` row is written by `log_activity`, before the caller's `commit()`, with a kind listed in `KINDS` in `services/activity.py`, because `GET /activity`'s filters read that registry. A new kind also goes in the table in `docs/architecture/routers.md`.
- Next-step generation reads the user's active `rl_listening_interest` rows, not `voice_memo.extracted_interests`, because the user can remove and add lines. The review still writes both.
- `GET /desk` counts undecided insights from connection statuses, never from `rl_review_draft`, because the draft is an opaque payload the client owns.

**Code families** (`services/code_families.py`, `routers/code_families.py`)
- The per-RQ cap counts only `LIVE_FAMILY_STATUSES = ("suggested", "accepted")`, against `config.code_families` (`max_per_rq: 5`, `near_cap_at: 4`, `max_new_per_run: 2`), and is enforced server-side. `override_cap` still limits a run to `max_new_per_run`.
- Accepting a merge re-checks that every source family is still live, and returns 409 if not, because the merge suggestion may be stale. A user merge (`POST /code_families/merge`) and an accepted suggestion share `merge_families`.
- Every user edit to a family (rename, members, merge) writes a `CodeFamilyRevision` first, because `code_family_member` rows are deleted when a member is removed and the revision is their audit trail.
- Starring a family (`rl_code_family_star`) also accepts a `suggested` one, because only user-filed families may reach the codebook export. Starring a `dismissed` or `merged` family returns 409.

**RQ merges and focus** (`services/rq_merges.py`, `routers/research_questions.py`, `routers/organize.py`)
- Every RQ-scoped read (memo sections, code family lists, the cap, synthesis input, `/organize`, `/codebook_prep`, breadth, a question's highlights, passage suggestions, gaps, merge nudges) goes through `resolve_rq_id` and `rq_group_ids`, because a merged RQ's entries and families keep their own `research_question_id` and must read under the target. `services/code_families.group_for` wraps both.
- A merge archives the source; undo sets `rl_rq_merge.status = "undone"` and un-archives it. Merge rows are never deleted. Merging an archived source, into an archived or merged target, or into itself returns 409, and so does un-archiving a merged RQ through `PATCH /research_questions/{id}`.

**Values defined twice. Change both together.**
- `FILED_STATUSES = ("accepted", "edited")` is in both `routers/memo.py` and `services/code_families.py`.
- The voice memo cap: `voice_memo.max_duration_seconds: 1200` in `config.yaml`, and `MAX_VOICE_MEMO_SECONDS = 1200` in `frontend/src/pages/ReaderPage.tsx`.
- Status values: each backend status (for example the code family's `merged`) also appears in a union in `frontend/src/lib/types.ts`. This includes `rl_rq_merge.status` (`"active" | "undone"`), in the `RQMerge` type in `types.ts`, and the connection status `merged`, which `GET /voice_memos/{id}/review` can now return.
- The focus range `(5, 7)`: `FOCUS_RANGE` in `routers/organize.py`, which `GET /organize` also returns as `focus_range`.
- `conversation_gist` rows with an empty `summary_text` mark a conversation that had no transcript to summarise (`services/gists.py`); every reader of gists filters them out (`_gist_context` and `gists_by_conversation` in `services/next_steps.py`).
- The activity filter buckets: the `CHANGED` and `SET_ASIDE` kinds of `KINDS` in `services/activity.py`, and `CHANGED_KINDS` and `SET_ASIDE_KINDS` in `frontend/src/pages/ActivityPage.tsx`, which tag each row.
- The number of conversations one "Find more passages" run reads: `find_passages.max_conversations_per_run: 3` in `config.yaml`, and `MAX_FIND_CONVERSATIONS = 3` in `frontend/src/components/QuestionLevers.tsx`, which calls the route once per conversation.
- The typed reflection and listening interest length caps: `REFLECTION_MAX_CHARS` and `LISTENING_INTEREST_MAX_CHARS` in `backend/app/schemas.py`, and `MAX_TYPED_CHARS` in `Notebook.tsx` and `MAX_CHARS` in `ListeningInterests.tsx`.

### Where the detail lives

| Before touching | Read |
|---|---|
| Models, tables, engines, schema changes | `docs/architecture/databases.md` |
| Any model call, prompt, output schema or gist backfill | `docs/architecture/llm.md` |
| Any endpoint (method, path, owning router) | `docs/architecture/routers.md` |
| Review, filing, memo, evidence and highlights | `docs/architecture/review-and-memo.md` |
| Code families, the cap, merges | `docs/architecture/code-families.md` |
| Frontend pages, components, `api.ts`, `types.ts` | `docs/architecture/frontend.md` |
| Cortico API, sign-in, workspaces, caches, write-back | `docs/architecture/cortico.md` |
| Writing or changing tests | `docs/architecture/tests.md` |

## 6. Key conventions

- **Python modules** start with `from __future__ import annotations`. Request bodies are Pydantic models in `backend/app/schemas.py`. Responses are hand-built dicts from each router's `_serialize*` helpers; there are no response models.
- **`frontend/src/lib/types.ts`** mirrors those serialized dicts, not `schemas.py`. Every backend route has one `api.*` method in `frontend/src/lib/api.ts`.
- **Error shapes:**
  - Expected errors are `HTTPException` with a `{"detail": ...}` body.
  - `api.ts` throws `Error("<status> <path>: <body>")` for any non-OK response.
  - Model failures raise `AIProviderError` (`app/llm.py`), answered as `{"detail": ..., "source": "ai"}` with 422, 502 or 503.
- **Frozen formats:**
  - Words JSON is `[{word, start, end, confidence}]` dicts, never tuples, because it must match Cortico's `snippet.words` and `quoted_texts` indexes `w["word"]`.
  - `conversation_gist` is keyed by Cortico `conversation_id` and is never refreshed.
  - The editable review prompt lives in `app_setting` under `REVIEW_PROMPT_SETTING_KEY = "review_prompt"`.
  - Audio files sit flat in `db/local/audio`, because `audioUrlFor` in `api.ts` keeps only the basename.
- **Users:** in `snapshot` mode every request is `config.default_user_id` (101), with no sign-in. In `cortico_api` mode the user is the signed-in Cortico account. `GET /memo` filters connections through `VoiceMemo.user_id`.
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
