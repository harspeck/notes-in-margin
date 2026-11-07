# Databases

The app uses two SQLite files and no Postgres or Supabase. The PRD's Postgres assumption is
corrected in `docs/prototype-log.md`.

| | Cortico snapshot (source data) | Prototype database (the app's store) |
|---|---|---|
| File | `acp_pipeline_snapshot.db` at the repo root (146 MB, gitignored) | `db/prototype.db` (gitignored) |
| Config key (`backend/config.yaml`) | `cortico_db_path: "../acp_pipeline_snapshot.db"`, resolved relative to `backend/` | `prototype_db_path: "db/prototype.db"`, resolved relative to the repo root |
| Env override | none | `PROTOTYPE_DB_PATH` (`get_config` in `backend/app/config.py`) |
| Engine (`backend/app/db.py`) | `cortico_engine`: `mode=ro&immutable=1&uri=true`, plus `PRAGMA query_only = ON` on connect | `prototype_engine`: `PRAGMA foreign_keys = ON` on connect |
| Declarative base | `CorticoBase` (`models_cortico.py`) | `PrototypeBase` (`models_prototype.py`) |
| FastAPI dependency | `get_cortico_db` | `get_prototype_db`. The transcription thread opens `PrototypeSession()` directly. |

## Cortico models (read-only)

Organization, Community, Forum, Project, Participant, Conversation, Snippet,
ConversationGuide, GuideSection and GuideQuestion. `db/schema.sql` is this snapshot's schema,
generated with `sqlite3 acp_pipeline_snapshot.db ".schema"`. It is a reference for checking
names and is never executed, except into a new scratch file by the test fixture builder
(`backend/tests/fixtures/build_snapshot_fixture.py`).

Snapshot facts the code depends on (`docs/prototype-log.md`):
- Only 1 of the 54 projects (project 5) has a conversation guide.
- No conversation has a `conversation_guide_id` set. The guide is resolved by
  `backend/app/guide.py`: use `conversation_guide_id` if set, else the project's
  highest-`version` guide.
- None of the 201 conversations has an `audio_url`.
- Conversations 160, 162 and 201 have zero snippets.
- Snippet ids are integers; there is no uuid.

## Prototype tables

`research_question`, `rl_highlight`, `highlight_snippet`, `voice_memo`,
`reflection_connection`, `connection_evidence`, `reflection_connection_revision`,
`transcript_state`, `section_checkpoint`, `conversation_gist`, `next_step_suggestion`,
`code_family`, `code_family_member`, `code_family_merge_suggestion`, `app_setting`,
`rl_rq_focus`, `rl_rq_merge`, `rl_code_family_star`, `rl_code_family_revision`.

The last four back the Organize page (2026-10-26). They carry the `rl_` prefix because
`codebook`, `code` and `coding` already exist in `db/schema.sql`. They were added as new
tables, not columns, so `create_all` picks them up on the dev database without a migration.

| Table | Key columns | Purpose |
|---|---|---|
| `rl_rq_focus` | `research_question_id` (PK), `user_id`, `focused`, `updated_at` | The Organize page's focus flag. A missing row means not focused. |
| `rl_rq_merge` | `id`, `user_id`, `source_rq_id`, `target_rq_id`, `status` (`active` or `undone`), `created_at`, `undone_at` | One row per RQ merge. Undo sets the status; rows are never deleted. |
| `rl_code_family_star` | `code_family_id` (PK), `starred`, `updated_at` | Star flag. Starred live families go into `/codebook_prep`. |
| `rl_code_family_revision` | `id`, `code_family_id`, `name`, `definition`, `member_connection_ids_json`, `changed_at` | Snapshot written before each user edit to a family, so removed `code_family_member` rows stay auditable. |

## Schema lifecycle

- There are no migrations. `PrototypeBase.metadata.create_all` runs in three places: the
  lifespan in `backend/app/main.py`, `scripts/backfill_gists.py`, and `tests/conftest.py`.
  It creates missing tables and never alters existing ones.
- A new table is additive: check its name against `db/schema.sql`, then log it in
  `docs/prototype-log.md`.
- A new or changed column on an existing table needs a migration that doesn't exist. Stop
  and ask the user how to apply it. Never wipe `db/prototype.db`, because it holds real dev
  data.
- Startup also runs `seed_research_questions` (`backend/app/seed.py`). It loads the 3 RQs
  in `backend/seed/research_questions.seed.json`, but only when user 101 has no RQs yet.

## Invariants

- Never write through `CorticoSession` or a Cortico model, because the snapshot is source
  data. The engine refuses writes anyway.
- Rejecting, dismissing, merging or retracting a row sets a status. Rows are never deleted,
  because the trail must stay auditable. The one exception is a `code_family_member` row
  removed by `PUT /code_families/{id}/members`, which is audited by the
  `rl_code_family_revision` row written before it.
- A merged RQ's rows keep their own `research_question_id`. Reads resolve them to the
  target through `services/rq_merges.py` (`resolve_rq_id`, `rq_group_ids`).
- `section_checkpoint` has a unique constraint on (user, conversation, guide question), and
  `PUT /conversations/{id}/checkpoints` upserts against it. The question must belong to the
  conversation's resolved guide.
- Audio uploads go to `audio_dir: "db/local/audio"`, which `main.py` serves at `/audio`.
- Backups live in `db/local/` and are written only by `backend/scripts/export_md.py`, run on the user's request:
  - `exports/memo.md` and `exports/transcripts/*.md`, rendered from `get_memo` and the `voice_memo` rows.
  - `exports-<date>/`: the previous export, moved aside before each run so it is never overwritten.
  - `prototype-<YYYY-MM-DDTHHMMSS>.db`: a full copy made with SQLite's online backup, safe while the app runs. To restore one, stop the backend and copy it over `db/prototype.db`.