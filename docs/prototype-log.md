# Prototype log

Running record of every place this prototype's build diverges from the PRD
(`docs/prd-reflective-listening.md`), per its own instruction to follow `schema.sql` and
note the difference. Entries are added as they're hit, not just up front.

## File locations

- The task referenced `docs/prd-reflective-listening.md` and `db/schema.sql`, neither of
  which existed. The actual PRD was at the repo root as
  `PRD Reflective Listening Prototype (Transcript Reader + Voice Memo).md` — moved to
  `docs/prd-reflective-listening.md`.
- `db/schema.sql` didn't exist either. What existed was `acp_pipeline_snapshot.db`, a live
  SQLite snapshot (not a schema-only pg_dump as the PRD describes — it has data, and it's
  SQLite rather than Postgres). `db/schema.sql` is generated from this snapshot
  (`sqlite3 acp_pipeline_snapshot.db ".schema"`) and treated as the PRD's "source of truth
  for names." The snapshot file itself stays gitignored, as the PRD requires.
- The repo was not a git repository yet — initialized as part of scaffolding.

## Table/column naming deltas (schema.sql wins per the PRD)

- **`highlight` name collision:** Cortico already owns a real, read-only `highlight` table
  (and an unrelated prior prototype's `solo*` tables — sensemaking sessions, codebooks,
  tagging runs — not part of this PRD). This prototype's own highlight table is named
  **`rl_highlight`** instead, following the existing `solo_`-prefix convention in the same
  database for prototype-owned tables.
- **No `snippet.uuid`:** snippets have integer `id`/`cortico_id`, no UUID column. Every PRD
  field named `snippet_uuid` is **`snippet_id`** (integer) instead, in `highlight_snippet`,
  `connection_evidence`, `section_checkpoint`, and the assembled memo view.
- **Snippet field names:** `text` not `content`; `order_index` not `index_in_conversation`.
  Speaker name and facilitator-ness come from joining `participant` via
  `snippet.participant_id`, not inline snippet columns. Unused-but-available extra columns:
  `is_facilitator_prompt`, `is_mechanical`, `ai_score`.
- **`words` shape:** a JSON array of `{"word","start","end","confidence"}` objects, not
  `[word, start, end, confidence]` tuples as the PRD's mock data shows. `voice_memo`'s
  Whisper output is stored in the same dict shape for consistency with Cortico, not the
  tuple shape from the PRD's example.
- **Guide linkage is real but unused in this data:** `conversation.conversation_guide_id`
  exists, but is `NULL` for all 201 conversations in the snapshot, including project 5's
  (the only project with an actual `conversationguide` row). So the fallback the PRD already
  anticipated — project's latest guide version — is the path actually exercised:
  `conversation.conversation_guide_id` if set, else the highest-`version` `conversationguide`
  row for `conversation.project_id`. There's no `published`/status flag on `conversationguide`,
  so "latest" = max `version`.
- **Conversation guide is 3 relational tables**, not one nested `sections[].prompts[]` blob:
  `conversationguide` → `guidesection` → `guidequestion` (`text`, `shorthand_code`,
  `order_index`).
- **Conversation fields:** no `start_time` (conversation list ordering uses `id` instead);
  has `duration_seconds`, `is_open`, `facilitator_participant_id`; singular `language`, not
  `language_codes`.
- **No Postgres roles/schemas:** the snapshot is SQLite. The PRD's "SELECT-only role +
  separate schema" read-only enforcement is replaced with an app-layer equivalent: a
  dedicated read-only SQLAlchemy engine (`mode=ro&immutable=1`) for Cortico tables that never
  has `add`/`commit` called against it anywhere in the codebase, and a second, separate
  SQLite file (`db/prototype.db`) for everything prototype-owned.
- **Dropped speculative `rl_highlight` fields:** the PRD lists `type`, `tool_id`, `metadata`
  as mirroring Cortico's `CreateHighlightSchema` for future promotion, but that schema isn't
  in our ground truth and no screen or behavior in this PRD reads or writes them. Left out
  rather than carried as unused columns.
- **Added `app_setting` (key/value) table**, not in the PRD's data model: needed to persist
  the editable AI-review prompt (view/save/reset-to-default) across restarts.

## Scoping decisions (confirmed with the user, not schema deltas)

- Only project 5 (conversation ids 9, 10) has a real `conversationguide` row in this
  snapshot (1 of 54 projects). Transcript switching in this build is scoped to those two
  conversations so section checkpoints are actually exercisable; widening is a one-line
  filter change later.
- Every conversation's `audio_url` is null/empty (0 of 201). Playback and word-level
  "karaoke" highlighting are built against the real `audio_url` + `words` offset contract,
  but degrade to a plain-text view with no player when `audio_url` is empty — which is
  always, for now, until a real audio file is supplied for one conversation.
- Model ID/params verified against OpenAI's own docs (`developers.openai.com`,
  `help.openai.com`) on 2026-10-05: `gpt-5.6-luna` is real and supports
  `reasoning.effort: low`.

## Implementation notes

- **PyAV pin:** `faster-whisper` needs `av` pinned to `13.1.0` — the latest `av` (19.x)
  removed the `metadata_errors` kwarg its decoder call uses. Pinned in
  `backend/requirements.txt`.
- **Test isolation:** backend tests point `PROTOTYPE_DB_PATH` at a scratch file
  (`tests/conftest.py`), so `pytest` never leaves rows in the real `db/prototype.db`. The
  Cortico snapshot is still queried for real in tests (guide resolution, evidence
  validation) — only the prototype-owned tables are redirected.
- **`uvicorn --reload` leaves orphaned workers:** its child process doesn't show "uvicorn"
  in its command line, so `pkill -f uvicorn` can miss it and leave a zombie holding the
  port. Kill by port (`lsof -ti:8000 | xargs kill -9`) if restarts seem to serve stale data;
  the dev server in this repo is run without `--reload` for that reason.
- **AI review is unverified against a live model call** — no `OPENAI_API_KEY` was available
  in this environment. The structural-validation and filing logic is covered by tests and
  by manually exercising every other endpoint against real data; only the actual
  `gpt-5.6-luna` round trip is unverified. Add `backend/.env` with `OPENAI_API_KEY=...` to
  enable it (python-dotenv loads it automatically).
- **Full voice→transcript pipeline verified with real audio:** generated a synthetic
  reflection with macOS `say`, uploaded it through `POST /conversations/9/voice_memos`, and
  confirmed `faster-whisper` returned an accurate, correctly word-timestamped transcription
  in the exact `{word,start,end,confidence}` shape Cortico uses.

## Phase 2 (`docs/prd-phase-2.md`)

- **`OPENAI_API_KEY` now available** (with a custom base URL) in `backend/.env`, unlike
  Phase 1 — both `backfill_gists.py` and `generate_next_step_suggestions` were verified
  against real `gpt-5.6-luna` round trips, not just structurally.
- **Scope-widening done exactly as scoped:** `scoped_conversation_ids` removed from
  `config.yaml`/`Config` entirely (not just unused in `list_conversations`) since nothing
  else referenced it after the filter was dropped — leaving a dead config field around
  would've been worse than deleting it. `create_highlight` now looks up the real
  `project_id` from Cortico instead of hardcoding `config.default_project_id`.
- **Gist backfill run (real snapshot, 2026-10-06):** 201/201 conversations now have a
  `conversation_gist` row (3 from an initial smoke-test run + 198 from the full run).
  198-conversation run: **720.7s wall time (~3.64s/call avg)**, **279,579 input + 29,813
  output tokens**. No published per-token price for `gpt-5.6-luna` was available in this
  environment to convert that into a dollar figure — flagging rather than guessing.
- **Snippet sampling for gists:** "a handful of substantive snippets" (PRD wording) is
  implemented as up to 20 snippets, evenly spread across the whole conversation (not just
  its opening) after filtering out `is_mechanical`/`is_facilitator_prompt` lines, falling
  back to the full (unfiltered) snippet list for a conversation with no substantive ones.
- **3 of the 201 conversations (ids 160, 162, 201) have zero snippets at all** in the
  snapshot, not just zero substantive ones. The gist backfill's fallback path still runs,
  the model is given an empty snippet list for each, and all three correctly came back
  with some form of "no substantive conversation content was provided" rather than an
  invented summary — the guardrail doing its job on a genuine edge case, not a bug.
- **Next-step suggestions reuse `config.ai_review`'s model/effort** (not pinned to `low`
  the way the backfill is) per the PRD; manually verified end-to-end in the browser against
  the one real filed voice memo in the dev db (conversation 10, an extracted interest about
  "what kids internalize from leaders' behavior") — produced suggestions correctly grounded
  in that interest, each citing a real, valid, previously-unvisited conversation id with a
  rationale traceable to that conversation's actual gist content.
- **Code-family synthesis manually verified live** against RQ 1's one real filed entry in
  the dev db ("How do people describe trust in local institutions?") — produced a family
  grounded in that entry's actual text, correctly member-linked to the real connection id.
  Re-ran synthesis a second time: no duplicate family was created (confirms the
  attach-to-existing-family path, not just the create-new path).
- **Clustering across multiple entries is now exercised (updated 2026-10-14).** The dev db
  has 3 filed reflections (conversations 10, 44, 91) and 7 code families across RQs 1, 3, 4
  and 5. Two of those families (ids 4 and 7) each cluster 2 entries. The note above,
  written with one filed entry, was out of date.

## Phase 2 follow-ups (2026-10-14)

- **Previously unlogged Phase 1 data-model deviations**, found in the 2026-10-14
  acceptance-criteria audit:
  - `section_checkpoint.guide_question_id` (the PRD says `guide_prompt_id`). It follows the
    snapshot's `guidequestion` table.
  - `voice_memo` has two extra columns, `transcription_progress` and
    `transcription_error`, plus `transcribing`/`failed`/`filed` status values for the
    background transcription job.
  - `reflection_connection.memo_order` (memo ordering).
  - `edited_from_ai` is a boolean column rather than a source value.
- **`connection_evidence.highlight_id` is never populated** by AI review, manual adds, or
  memo-page evidence edits. As a result, review badges only say `yours` for exact matches
  with a *starred* span, and "Starred, unassigned" lists starred highlights the AI did cite.
  All 85 evidence rows in the dev db are `source=ai`. Fixed by the evidence provenance fix
  below.
- **Code-family cap and merges.** These aren't in `docs/prd-phase-2.md`; they come from
  resolving its granularity open question.
  - New config block `code_families` (`max_per_rq: 5`, `near_cap_at: 4`,
    `max_new_per_run: 2`).
  - New additive table `code_family_merge_suggestion`. Its name was checked against
    `db/schema.sql`, which has no `merge` tables.
  - New `merged` status on `code_family`.
  - New endpoints: `GET /research_questions/{id}/code_families/state`,
    `POST .../synthesize?override_cap=true`, and `POST /code_family_merges/{id}/accept|dismiss`.
  - The synthesize response changed from a bare list to the state object.
- **Live run against a copy of the dev db.** Synthesis on RQ 3 (3 families, 4 filed
  entries) attached every entry to an existing family and proposed no new ones. With
  `near_cap_at` lowered to 3 for one run, the model proposed no merges, judging RQ 3's
  three families distinct. The merge-accept path is covered only by unit tests so far, not
  a live merge.

## Evidence provenance fix (2026-10-14)

Closes the four Phase 1 acceptance criteria the 2026-10-14 audit left open, all caused by
`connection_evidence.highlight_id` never being set.

- **One linking rule for every evidence write** (`backend/app/evidence_provenance.py`):
  an evidence span is linked to the user's highlight it *overlaps* on the same snippet
  (half-open word ranges, any shared word counts), starred or not; a starred highlight wins
  ties, then the larger overlap, then the older highlight. A linked span gets
  `highlight_id` and `source="user"`; an unlinked one keeps its source (`ai` for the
  model's). The old rule was an exact span match against *starred* highlights only.
- **AI review** (`services/ai_review.py`): persistence is split into
  `persist_review_result` (testable without a model call). Evidence is linked as above and
  ordered starred-highlight → other highlight → AI-only text within each connection, the
  deterministic half of "prefer the user's highlights". Duplicate spans from the model are
  dropped.
- **Review-step adds/edits and memo-page evidence edits** (`routers/review.py`,
  `routers/memo.py`) go through `evidence_row`, which keeps an explicit `highlight_id`
  (refusing one from another user/conversation with a 400) and otherwise links by
  overlap. So the memo page's "remove evidence" no longer drops the link, even though it
  sends evidence back without `highlight_id`. Revision snapshots now include `highlight_id`.
- **"Starred, unassigned" is now deterministic:** a starred highlight counts as assigned
  only when it's cited by a connection that has an RQ and isn't rejected/retracted
  (`assigned_highlight_ids`). Review and memo use the same rule. Change from before:
  citation by an *unmapped* connection no longer counts as assigned, since the criterion
  is about RQ assignment.
- **Assigning a starred highlight in review** (`ReviewStep.tsx`): each starred-unassigned
  highlight has an "Assign to: RQ" select. On "File to memo" each assigned one becomes its
  own accepted connection, with the highlight's spans as `user` evidence carrying its
  `highlight_id`. Insight text defaults to the margin memo, else the quoted passage. Like
  everything else in review, nothing is written before "File to memo".
- **Memo evidence links** now go to `/?conversation=N&snippet=ID`. `ReaderPage` scrolls
  the cited snippet into view once per page load and outlines it briefly. Memo entries'
  evidence now includes `highlight_id`.
- **Backfill** (`backend/scripts/backfill_evidence_highlights.py`, additive): sets
  `highlight_id` (and `source="user"`) only on rows where `highlight_id` is null and the
  span overlaps a highlight. It never deletes rows or changes linked ones, so it's safe to
  re-run. On a copy of `db/prototype.db`, 83 of the 85 evidence rows linked (none to a
  starred highlight, since the dev db has none), and a second run found nothing more to
  do. Run on the real dev db 2026-10-26 (after a copy to `db/prototype.pre-backfill.db`):
  135 of 139 unlinked evidence rows linked, the other 4 overlap no highlight, and a second
  dry run found nothing more to do.
- Tests: `backend/tests/test_evidence_provenance.py` (overlap matching, AI linking and
  ordering, starred coverage incl. rejected/unmapped citers, review assignment, foreign
  highlight refused, memo edit keeps link). Frontend changes were clicked through in the
  browser 2026-10-26: `yours` badges on backfilled evidence, memo evidence links scrolling
  to the cited snippet, "Assign to: RQ" in review, and "Starred, unassigned".

## Organize page backend (2026-10-26)

Adds the step before codebook generation (`docs/prd-phase-2.md`, "Follow-up work"): focus a
working set of RQs, merge RQs, edit and star code families, and export a draft codebook.

- **New tables only**, so `db/prototype.db` needs no migration: `rl_rq_focus`,
  `rl_rq_merge`, `rl_code_family_star` and `rl_code_family_revision`. The `rl_` prefix is
  needed because `codebook`, `code` and `coding` exist in `db/schema.sql`; the four names
  were grepped against it and don't collide.
- **RQ merges** (`backend/app/services/rq_merges.py`): a merge archives the source and is
  undone by status. Rows keep their own `research_question_id`; reads resolve them through
  `resolve_rq_id` and `rq_group_ids`. `GET /memo` now omits a merged RQ's section and lists
  its entries under the target (each section gains `merged_from`), so
  `scripts/export_md.py`, which calls `get_memo`, agrees. The code family cap, synthesis
  input and merge suggestions count the whole group, and new AI families go to the target.
- **Refused merges** (409): into itself, into an archived or merged RQ, a source that is
  already merged, an archived source (because undo un-archives it, which would lose the
  earlier archived state; a deviation from the plan, which didn't mention it), and anything
  closing a cycle. `PATCH /research_questions/{id}` also refuses to un-archive a merged RQ.
- **Undo** reports families that stay with the target although they hold entries from the
  restored RQ (`families_kept_with_target`); these are families the user merged or built
  across the two while they were merged.
- **Family edits**: user-created families are `accepted` with no `ai_metadata` and are not
  capped. Members, rename and merge write `rl_code_family_revision` first. Starring a
  `suggested` family accepts it. `accept_merge` now goes through a shared
  `merge_families`.
- **Organize payload** additions beyond the plan: each group has an `entries` list
  (connection id, insight text, its own RQ, conversation id and title, and the family ids
  it is in), which the "Add entries" picker needs; and `merged_sources` items carry
  `into_id`, the RQ they were merged directly into, because sources are listed
  transitively.
- **Codebook prep** (`GET /codebook_prep`) is read-only and writes no file, so the backups
  in `db/local/` are untouched. Retracted member entries are left out.
- Tests: `test_rq_merges.py`, `test_code_family_edits.py`, `test_organize.py` (78 tests in
  all). None calls the model.
