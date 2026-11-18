# Databases

Locally (snapshot mode) the app uses two SQLite files. Deployed (`cortico_api` mode) there
is no snapshot, and the prototype tables live in Neon Postgres (`DATABASE_URL`; see
`docs/architecture/cortico.md`). Every prototype model must work on both.

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
`rl_rq_focus`, `rl_rq_merge`, `rl_code_family_star`, `rl_code_family_revision`,
`rl_session`, `rl_workspace`, `rl_cortico_conversation`, `rl_cortico_snippet`,
`rl_cortico_sync` (these five are described in `docs/architecture/cortico.md`),
`rl_reading_state`, `rl_review_draft`, `rl_listening_interest`, `rl_listening_suggestion`,
`rl_listening_suggestion_run`, `rl_recommendation_run`, `rl_set_aside`,
`rl_activity`, `rl_highlight_question`, `rl_insight_move`, `rl_insight_merge`,
`rl_merge_nudge`, `rl_passage_suggestion`, `rl_question_gap`, `rl_cortico_guide`.

`rl_rq_focus` to `rl_code_family_revision` back the Organize page (2026-10-26). They carry the `rl_` prefix because
`codebook`, `code` and `coding` already exist in `db/schema.sql`. They were added as new
tables, not columns, so `create_all` picks them up on the dev database without a migration.

| Table | Key columns | Purpose |
|---|---|---|
| `rl_rq_focus` | `research_question_id` (PK), `user_id`, `focused`, `updated_at` | The Organize page's focus flag. A missing row means not focused. |
| `rl_rq_merge` | `id`, `user_id`, `source_rq_id`, `target_rq_id`, `status` (`active` or `undone`), `created_at`, `undone_at` | One row per RQ merge. Undo sets the status; rows are never deleted. |
| `rl_code_family_star` | `code_family_id` (PK), `starred`, `updated_at` | Star flag. Starred live families go into `/codebook_prep`. |
| `rl_code_family_revision` | `id`, `code_family_id`, `name`, `definition`, `member_connection_ids_json`, `changed_at` | Snapshot written before each user edit to a family, so removed `code_family_member` rows stay auditable. |

The last five (2026-11-18) hold state that used to live only in the browser, or nowhere.
Their names were checked against `db/schema.sql`. Their timestamps are
`DateTime(timezone=True)`, like the other `rl_` tables.

| Table | Key columns | Purpose |
|---|---|---|
| `rl_reading_state` | (`user_id`, `conversation_id`) PK, `first_opened_at`, `last_opened_at`, `position_seconds`, `position_snippet_id` | That a user opened a conversation and how far they got. Written by `PUT /conversations/{id}/reading_position`, read by `GET /desk`. |
| `rl_review_draft` | `voice_memo_id` (PK), `user_id`, `payload_json`, `updated_at` | The client's unsaved review decisions, as an opaque JSON object of at most 200,000 bytes. Deleted by filing and by `DELETE .../review_draft`; this is a scratch row, not an audited one. |
| `rl_listening_interest` | `id`, `user_id`, `text`, `origin` (`reflection`, `user` or `suggestion`), `voice_memo_id` (null for `user`), `status` (`active` or `removed`), `created_at`, `updated_at` | The user's "what I want to hear next" lines. The AI review still writes `voice_memo.extracted_interests`, and also adds each new line here. Next-step generation reads the active rows. Removing sets the status. |
| `rl_listening_suggestion` | `id`, `user_id`, `run_id`, `text`, `reason`, `status` (`suggested`, `accepted` or `dismissed`), `listening_interest_id` (set on accept), `ai_metadata`, `created_at`, `updated_at` | A line the AI offered for "what you want to hear next". Dismissed rows stay, so the line is not offered again. |
| `rl_listening_suggestion_run` | `id`, `user_id`, `fingerprint`, `suggestion_count`, `created_at` | One model call for those lines. The fingerprint (the questions and highlights shown to the model) is the cache key; a run that offered nothing still gets a row. |
| `rl_set_aside` | `id`, `connection_id`, `user_id`, `kind` (`retracted` or `rejected`), `previous_status`, `set_aside_at`, `restored_at` | One row each time a connection is retracted or rejected: when it happened and the status it had, which `reflection_connection` has no column for. Restore reads `previous_status` and sets `restored_at`. Connections set aside before this table have no row. |
| `rl_activity` | `id`, `user_id`, `created_at`, `actor` (`ai` or `user`), `kind`, `summary`, `refs_json` | The activity log. Written only by `log_activity` (`services/activity.py`). |

The next seven (2026-11-18) back the Questions screen and the notebook's AI levers. Their
names were checked against `db/schema.sql`, and their timestamps are
`DateTime(timezone=True)`.

| Table | Key columns | Purpose |
|---|---|---|
| `rl_highlight_question` | `id`, `highlight_id`, `research_question_id`, `user_id`, `created_at`; unique (`highlight_id`, `research_question_id`) | A highlight filed under a question. Written by `PUT /highlights/{id}/questions` and by accepting a passage suggestion. Not audit data: replacing the set or deleting the highlight deletes rows. The stored id is the question the link was made under; reads resolve it through merges. |
| `rl_insight_move` | `id`, `connection_id`, `user_id`, `from_research_question_id`, `to_research_question_id` (either may be null), `moved_at` | One row per move of a filed insight between questions. It exists because `reflection_connection_revision` has no column for the question. |
| `rl_insight_merge` | `id`, `user_id`, `source_connection_id`, `target_connection_id`, `source_previous_status`, `added_evidence_json` (`[{id, snippet_id, start_index, end_index}]`), `status` (`active` or `undone`), `created_at`, `undone_at` | One row per merge of one filed insight into another. Undo sets the status; rows are never deleted. |
| `rl_merge_nudge` | `id`, `user_id`, `kind` (`questions` or `insights`), `item_a_id`, `item_b_id`, `default_target_id`, `research_question_id` (insights), `reason`, `suggested_wording` (questions), `status` (`suggested`, `accepted` or `dismissed`), `result_merge_id`, `ai_metadata`, `created_at`, `updated_at` | An AI suggestion to merge two questions or two insights. `dismissed` means kept separate. `item_*_id` are question ids or connection ids by `kind`, so they are not foreign keys. |
| `rl_passage_suggestion` | `id`, `user_id`, `research_question_id`, `conversation_id`, `snippet_id`, `start_index`, `end_index`, `reason`, `status` (`suggested`, `accepted` or `dismissed`), `highlight_id`, `ai_metadata`, `created_at`, `updated_at` | A passage the AI found for a question: one half-open word range in one snippet. `highlight_id` is the highlight made on accept. It is not a foreign key, so deleting that highlight later is not blocked. |
| `rl_question_gap` | `id`, `user_id`, `research_question_id`, `gap_kind` (`perspective`, `counter_view` or `thin_spot`), `observation`, `conversation_ids_json`, `status` (`suggested` or `dismissed`), `ai_metadata`, `created_at`, `updated_at` | A "what am I missing" observation, with the unopened conversations it names. |
| `rl_cortico_guide` | `project_id` (PK), `questions_json` (`[{id, text, shorthand_code, order_index}]`), `fetched_at` | `cortico_api` mode's cache of a project's latest conversation guide, refreshed after `cortico.snippet_cache_ttl_seconds`. See `docs/architecture/cortico.md`. |

`reflection_connection.status` gained the value `merged` (an insight folded into another by
`rl_insight_merge`). No column changed: the status column is a plain string.

A typed reflection needs no table: it is a `voice_memo` row whose `audio_path` is the empty
string (the column is NOT NULL), with `duration` null, `transcription_raw` null and
`transcription_engine` `"typed"`. Serializers return null for the path and `typed: true`.

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
  because the trail must stay auditable. The exceptions:
  - a `code_family_member` row removed by `PUT /code_families/{id}/members`, or by moving
    its insight to another question, which is audited by the `rl_code_family_revision` row
    written before it;
  - `connection_evidence` rows replaced by an evidence edit or removed by undoing an
    insight merge, which are audited by the `reflection_connection_revision` written first;
  - `rl_highlight_question` rows, which are not audit data.
- An insight merge never deletes the source's evidence. The target gets copies, and
  `rl_insight_merge.added_evidence_json` lists them. Undo removes the target's evidence
  with those spans (matched by span, not row id, because an evidence edit re-creates rows).
- A merged RQ's rows keep their own `research_question_id`. Reads resolve them to the
  target through `services/rq_merges.py` (`resolve_rq_id`, `rq_group_ids`).
- Listening interests are imported once per user: `ensure_imported`
  (`services/listening_interests.py`) copies the voice memos' `extracted_interests` only
  while the user has no `rl_listening_interest` row at all, removed rows included.
- `section_checkpoint` has a unique constraint on (user, conversation, guide question), and
  `PUT /conversations/{id}/checkpoints` upserts against it. The question must belong to the
  conversation's resolved guide.
- Audio uploads go to `audio_dir: "db/local/audio"`, which `main.py` serves at `/audio`.
- Backups live in `db/local/` and are written only by `backend/scripts/export_md.py`, run on the user's request:
  - `exports/memo.md` and `exports/transcripts/*.md`, rendered from `get_memo` and the `voice_memo` rows.
  - `exports-<date>/`: the previous export, moved aside before each run so it is never overwritten.
  - `prototype-<YYYY-MM-DDTHHMMSS>.db`: a full copy made with SQLite's online backup, safe while the app runs. To restore one, stop the backend and copy it over `db/prototype.db`.