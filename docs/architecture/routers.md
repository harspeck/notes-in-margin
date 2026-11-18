# Routers

All routers live in `backend/app/routers/` and are registered in `backend/app/main.py`.
`GET /health` lives in `main.py` and needs no sign-in. CORS origins come from
`config.cors_origins` (default `http://localhost:5173`; the `CORS_ORIGINS` environment
variable, comma-separated, overrides it) and credentials are allowed, for the session cookie.
Routes marked **paid** call the model (through `app/llm.py`).

Every router except `auth` is registered with `Depends(current_user)`, so in `cortico_api`
mode every route below the `auth` rows answers 401 without a session.

| Router | Method and path | Handler |
|---|---|---|
| auth | `POST /auth/login?include_token=` | `login` (body `{email, password}`; `cortico_api` mode only, 400 otherwise; sets the `nim_session` cookie; `include_token=true` also returns `session_token` for scripts) |
| | `POST /auth/logout` | `logout` (ends the local session and, best effort, the Cortico one) |
| | `GET /auth/me` | `me` (`{data_source, user, active_workspace}`; in snapshot mode the user is `config.default_user_id`) |
| workspaces | `GET /organizations` | `list_organizations` (only organizations with a project open to the user; each has `project_count`) |
| | `GET /organizations/{organization_id}/projects` | `list_projects` (from `/v2/projects`, filtered to the user's roles; from the snapshot in snapshot mode) |
| | `GET /workspaces` | `list_workspaces` (the user's workspaces for the current `data_source`) |
| | `POST /workspaces` | `create_workspace` (body `{name, organization_id, project_ids}`; 403 for projects not open to the user; becomes the active workspace) |
| | `PATCH /workspaces/{workspace_id}` | `update_workspace` (`name`, `project_ids`, `archived`) |
| | `POST /workspaces/{workspace_id}/activate` | `activate_workspace` (409 if archived) |
| conversations | `GET /conversations` | `list_conversations` (the active workspace's projects; in snapshot mode with no workspace, the full 201-conversation corpus; in `cortico_api` mode with no workspace, 409) |
| | `GET /conversations/{conversation_id}` | `get_conversation` |
| | `PUT /conversations/{conversation_id}/state` | `update_transcript_state` |
| highlights | `GET /conversations/{conversation_id}/highlights` | `list_highlights` |
| | `GET /conversations/{conversation_id}/cortico_highlights` | `list_cortico_highlights` (highlights other people and tools made in Cortico, read live with one `GET /v2/highlights`; each is `{cortico_id, title, description, author_name, mine, tool_id, created_at, start_offset, end_offset, snippets}` with half-open word spans worked out from the audio range; the user's own copies are left out; `[]` in snapshot mode; nothing is stored) |
| | `POST /conversations/{conversation_id}/highlights` | `create_highlight` (`project_id` comes from the Cortico conversation; in `cortico_api` mode the highlight is then pushed to Cortico) |
| | `POST /highlights/{highlight_id}/cortico_sync` | `retry_highlight_sync` (retries the push to Cortico; 400 in snapshot mode; never creates a second copy) |
| | `PATCH /highlights/{highlight_id}` | `update_highlight` |
| | `PUT /highlights/{highlight_id}/questions` | `set_highlight_questions` (body `{research_question_ids: [int]}`, replaces the set; 404 for a question that isn't the user's, 409 for an archived one; a merged question is stored as its target) |
| | `DELETE /highlights/{highlight_id}` | `delete_highlight` (also deletes the Cortico copy this app created, and the highlight's question links) |
| checkpoints | `PUT /conversations/{conversation_id}/checkpoints` | `set_checkpoint` (an upsert) |
| | `DELETE /conversations/{conversation_id}/checkpoints/{guide_question_id}` | `delete_checkpoint` |
| research_questions | `GET /research_questions?include_archived=` | `list_research_questions` |
| | `POST /research_questions` | `create_research_question` (`project_id` is the active workspace's first project, else `default_project_id`) |
| | `PATCH /research_questions/{rq_id}` | `update_research_question` |
| | `PUT /research_questions/reorder` | `reorder_research_questions` |
| | `POST /research_questions/{rq_id}/focus` | `set_focus` (409 on a merged RQ) |
| | `POST /research_questions/{rq_id}/merge_into` | `merge_into` (409 on self, archived or merged RQs, or a cycle) |
| | `POST /rq_merges/{merge_id}/undo` | `undo` (on `rq_merges_router`; 409 if already undone) |
| | `GET /research_questions/breadth` | `get_breadth` (see below) |
| | `GET /research_questions/guide_ideas` | `get_guide_ideas` (see below) |
| | `GET /research_questions/{rq_id}/highlights` | `list_question_highlights` (highlights filed under the question's merged group that no filed insight cites; see below) |
| voice_memos | `POST /conversations/{conversation_id}/voice_memos` | `create_voice_memo` (multipart `audio` and `duration`; 400 over `voice_memo.max_duration_seconds`; starts transcription; 501 in `cortico_api` mode) |
| | `POST /conversations/{conversation_id}/reflections` | `create_typed_reflection` (body `{text}`, 1 to 20,000 characters; a voice memo with no audio, already `ready_for_review`; both modes) |
| | `GET /voice_memos/{voice_memo_id}` | `get_voice_memo` |
| | `PATCH /voice_memos/{voice_memo_id}` | `edit_voice_memo` |
| | `GET /conversations/{conversation_id}/voice_memos` | `list_voice_memos_for_conversation` |
| review | `POST /voice_memos/{voice_memo_id}/review` | `run_review` (**paid**) |
| | `GET /voice_memos/{voice_memo_id}/review` | `get_review` |
| | `POST /voice_memos/{voice_memo_id}/file` | `file_to_memo` (the only way into the memo; also deletes the review draft) |
| | `GET /voice_memos/{voice_memo_id}/review_draft` | `get_review_draft` (`payload` is null when there is none) |
| | `PUT /voice_memos/{voice_memo_id}/review_draft` | `save_review_draft` (body `{payload: object}`, replaced whole; 413 over 200,000 bytes) |
| | `DELETE /voice_memos/{voice_memo_id}/review_draft` | `delete_review_draft` |
| memo | `GET /memo` | `get_memo` (the signed-in user's filed connections, through `VoiceMemo.user_id`) |
| | `GET /memo/set_aside` | `list_set_aside` (the user's retracted and rejected connections, newest first) |
| | `PATCH /memo/connections/{connection_id}` | `edit_memo_entry` (body `insight_text`, `evidence`, `research_question_id`; naming `research_question_id`, null included, moves the entry; see "Moving and merging insights") |
| | `POST /memo/connections/{connection_id}/merge_into` | `merge_memo_entry_into` (body `{target_connection_id}`; 409 unless both are filed and distinct) |
| | `POST /insight_merges/{merge_id}/undo` | `undo_merge_of_insights` (on `insight_merges_router`; 409 if already undone or stale) |
| | `POST /memo/connections/{connection_id}/retract` | `retract_memo_entry` (writes an `rl_set_aside` row) |
| | `POST /memo/connections/{connection_id}/restore` | `restore_connection` (retracted: back to its earlier filed status, revision first; rejected: back to `suggested`, not into the memo; 409 if not set aside) |
| | `PUT /memo/reorder` | `reorder_memo_entries` (accepts entries from the RQ's merged group) |
| next_steps | `POST /next_steps/generate` | `generate` (**paid**) |
| | `POST /next_steps/recommend` | `recommend` (the conversation to read next, for the end of a session: `{recommendation, generated, reason}`. Answers from an undecided suggestion that names an unopened conversation; otherwise **paid**, unless there is no gist to choose from (`reason: "no_summaries"`) the model was asked within `next_steps.min_minutes_between_auto_runs` (`"recent"`), or what it would be shown has not changed since it last named nothing (`"unchanged"`). Hosted, the gists shown and the conversation recommended are limited to the user's workspace) |
| | `GET /next_steps?include_dismissed=` | `list_suggestions` |
| | `POST /next_steps/{suggestion_id}/accept` | `accept` |
| | `POST /next_steps/{suggestion_id}/dismiss` | `dismiss` |
| code_families | `GET /research_questions/{rq_id}/code_families?include_dismissed=` | `list_code_families` |
| | `GET /research_questions/{rq_id}/code_families/state` | `code_family_state` |
| | `POST /research_questions/{rq_id}/code_families/synthesize?override_cap=` | `synthesize` (**paid**; returns the state object, not a list) |
| | `POST /research_questions/{rq_id}/code_families` | `create_family` (user-made, accepted, not capped; 422 on entries outside the group) |
| | `PATCH /code_families/{family_id}` | `update_family` (writes a revision first) |
| | `PUT /code_families/{family_id}/members` | `replace_members` (writes a revision first; 409 unless live; 422 on bad entries) |
| | `POST /code_families/{family_id}/star` | `star_family` (starring accepts a suggested family; 409 on dismissed or merged) |
| | `POST /code_families/merge` | `merge_user_families` (shares `merge_families` with suggestion accept) |
| | `POST /code_families/{family_id}/accept` | `accept_family` |
| | `POST /code_families/{family_id}/dismiss` | `dismiss_family` |
| | `POST /code_family_merges/{merge_id}/accept` | `accept_merge_suggestion` (409 if not pending or stale) |
| | `POST /code_family_merges/{merge_id}/dismiss` | `dismiss_merge_suggestion` |
| organize | `GET /organize` | `get_organize` (focused and other RQ groups, merged sources folded into their target; see below) |
| | `GET /codebook_prep` | `get_codebook_prep` (read-only JSON of focused RQs' starred families; nothing written server-side) |
| settings | `GET /settings/review-prompt` | `get_review_prompt` |
| | `PUT /settings/review-prompt` | `save_review_prompt` |
| | `POST /settings/review-prompt/reset` | `reset_review_prompt` |
| desk | `PUT /conversations/{conversation_id}/reading_position` | `set_reading_position` (upserts `rl_reading_state` and bumps last opened; body optional: `position_seconds`, `snippet_id`; 404 on the first open of a conversation Cortico doesn't show the user) |
| | `GET /desk` | `get_desk` (see below) |
| listening_interests | `GET /listening_interests` | `list_listening_interests` (active lines; the first call for a user with no rows imports their voice memos' `extracted_interests`) |
| | `POST /listening_interests` | `add_listening_interest` (body `{text}`, up to 500 characters; origin `user`) |
| | `PATCH /listening_interests/{interest_id}` | `edit_listening_interest` (409 on a removed line) |
| | `DELETE /listening_interests/{interest_id}` | `remove_listening_interest` (sets status `removed`; the row stays) |
| | `GET /listening_interests/suggestions` | `list_listening_suggestions` (undecided lines the AI offered; never calls the model) |
| | `POST /listening_interests/suggestions/refresh` | `refresh_listening_suggestions` (`{suggestions, generated, reason}`; **paid** only when nothing is undecided, the user's questions or newest highlights changed since the last run, and that run is older than `listening_suggestions.min_minutes_between_runs`. `reason` is `undecided`, `nothing_to_go_on`, `unchanged` or `recent` when no call was made) |
| | `POST /listening_interests/suggestions/{id}/accept` | `accept_listening_suggestion` (adds the line with origin `suggestion`, through `add_line`; 409 once decided) |
| | `POST /listening_interests/suggestions/{id}/dismiss` | `dismiss_listening_suggestion` (kept, so the line is not offered again) |
| gists | `GET /gists/status` | `gist_status` (`{total, with_gist, remaining}` over the user's conversations) |
| | `POST /gists/generate` | `generate_gists` (**paid**: summarises up to `gists.max_per_request` conversations with no gist, one call each, and returns the status plus `generated`, `without_transcript` and `failed`; a model failure stops the batch and keeps what was made) |
| activity | `GET /activity?filter=&limit=&before=` | `list_activity` (see below) |
| merge_nudges | `POST /merge_nudges/generate` | `generate` (**paid**; returns the nudges this run created) |
| | `GET /merge_nudges` | `list_nudges` (undecided nudges whose two items are both still live) |
| | `POST /merge_nudges/{nudge_id}/accept` | `accept` (body optional: `target_id`, `apply_suggested_wording`; merges through `merge_rq` or `merge_insights`; 409 if decided or stale, 422 for a `target_id` that isn't one of the two) |
| | `POST /merge_nudges/{nudge_id}/dismiss` | `dismiss` ("keep separate"; 409 on an accepted nudge) |
| question_levers | `POST /research_questions/{rq_id}/find_passages` | `run_find_passages` (**paid**, one call per conversation; body optional: `conversation_id`; 409 on an archived question) |
| | `GET /research_questions/{rq_id}/passage_suggestions?conversation_id=` | `list_passage_suggestions` (undecided ones, newest first) |
| | `POST /passage_suggestions/{suggestion_id}/accept` | `accept_passage` (creates a highlight through `create_highlight_for_user` and files it under the question; 409 once decided) |
| | `POST /passage_suggestions/{suggestion_id}/dismiss` | `dismiss_passage` (409 on an accepted one) |
| | `POST /research_questions/{rq_id}/whats_missing` | `run_whats_missing` (**paid**; returns the observations this run created) |
| | `GET /research_questions/{rq_id}/gaps` | `list_gaps` (observations not dismissed, newest first) |
| | `POST /question_gaps/{gap_id}/dismiss` | `dismiss_gap` |
| | `POST /question_gaps/{gap_id}/listen_next` | `gap_listen_next` (body optional: `conversation_ids`, a subset of the gap's; adds `suggested` next-step rows; 409 if dismissed or no conversation is named, 422 for an id the gap doesn't name) |

## Highlights and questions

Every highlight response (`list_highlights`, `create_highlight`, `update_highlight`,
`retry_highlight_sync`, `set_highlight_questions`) carries `research_question_ids: [int]`:
the questions the highlight is filed under, each resolved through question merges. The links
are in `rl_highlight_question` and stay local (Cortico highlights have no questions).

`GET /research_questions/{rq_id}/highlights` returns `{research_question_id, highlights: [...]}`.
`research_question_id` is the question the group reads under (the target, for a merged
question). Each highlight is `{highlight_id, conversation_id, conversation_title, title,
notes, starred, created_at, research_question_ids, snippets: [{snippet_id, start_index,
end_index, text}], text}`, oldest first. `text` is the snippets' quoted text joined with
spaces. A highlight is left out once any filed insight of the user's cites it as evidence,
under whichever question.

## `GET /research_questions/breadth` shape

`{opened_conversation_ids, opened_conversation_count, projects_in_scope,
projects_in_scope_count, questions: [...], unfiled: {...}}`.

- A conversation counts as **opened** when the user has an `rl_reading_state` row, a
  highlight or a voice memo on it. Only conversations in scope are listed: the active
  workspace's projects, or in snapshot mode with no workspace, the whole corpus. In
  `cortico_api` mode with no workspace every list is empty.
- `projects_in_scope` is the active workspace's project ids. In snapshot mode with no
  workspace it is every project that has a conversation.
- `questions[]` has one item per question that is neither archived nor merged, in display
  order: `{research_question_id, name, conversation_ids, conversation_count,
  insight_conversation_ids, highlight_only_conversation_ids, project_ids_heard,
  projects_heard_count, insight_count, highlight_count}`. Everything is read over the
  question's merged group.
  - `insight_conversation_ids`: conversations a filed insight under the question comes
    from. That is its reflection's conversation, plus the conversation of each evidence
    passage (they differ after an insight merge).
  - `highlight_only_conversation_ids`: conversations with a highlight filed under the
    question and no insight.
  - `conversation_ids` is the union of the two. `project_ids_heard` are those
    conversations' projects.
  - `insight_count` is every filed insight in the group, as on `GET /memo`, whatever the
    workspace. `highlight_count` is every highlight filed under the group, cited or not.
- `unfiled`: `{insight_count, conversation_ids, conversation_count, project_ids_heard,
  projects_heard_count}` for filed insights under no active question ("doesn't fit a
  question yet", or a question that was archived without being merged).

It runs a fixed number of queries whatever the number of questions: questions, active
merges, filed insights, their evidence rows, highlight links, three for "opened", and two
batched Cortico reads (snippets by id, conversations by id), which hit the sidecar caches in
`cortico_api` mode.

## `GET /research_questions/guide_ideas` shape

`{ideas: [{text, shorthand_code, conversation_count, project_ids}], projects_not_loaded: [int]}`.
An idea is a distinct conversation-guide question (compared ignoring case and spacing) from
the projects in scope that is not already the name of one of the user's research questions,
archived and merged ones included. `conversation_count` is the number of in-scope
conversations in the projects whose guide has it. At most 12, most conversations first.
Nothing is filtered by content, so facilitation prompts in a guide (consent, guidelines) are
returned too.

- **Snapshot mode:** each project's highest-version guide, read from the snapshot.
- **`cortico_api` mode:** the conversations and their projects come from the sidecar's
  cached listing (`rl_cortico_conversation`), with no Cortico call. Guides come from
  `rl_cortico_guide`, which opening a conversation also fills. A workspace project with no
  cached guide (or one older than `cortico.snippet_cache_ttl_seconds`) costs one
  `/v2/conversation-guides?project_id=` call, for at most 5 projects per request; the rest
  are returned in `projects_not_loaded` and load on a later request. It never calls Cortico
  per conversation. With no workspace it returns no ideas.

## Moving and merging insights

- **Move:** `PATCH /memo/connections/{id}` with `research_question_id` in the body. Null
  means "doesn't fit a question yet" (the entry then reads in `GET /memo`'s `unmapped`). A
  merged question resolves to its target; an archived one answers 409, another user's 404.
  The route writes a `ReflectionConnectionRevision` first and an `rl_insight_move` row.
  A move alone changes neither `status` nor `source`. It clears `memo_order`, and takes the
  entry out of code families that belong to another question's group (a family revision is
  written first). Response: `{id, status, research_question_id, moved, removed_from_family_ids}`.
  A body without `research_question_id` behaves as before (the entry becomes `edited`).
- **Merge:** `POST /memo/connections/{source}/merge_into` `{target_connection_id}`. Both
  must be filed entries of the user's (404 otherwise, 409 if either is not filed or they are
  the same). A revision of the target is written first. The target gains the source's
  evidence through `resolve_evidence`, identical spans once. The source's status becomes
  `merged`, a connection status that is neither filed nor set aside. The two need not be
  under the same question. Response: `{merge: {id, source_connection_id,
  target_connection_id, status, added_evidence_count, source_previous_status, created_at,
  undone_at}, source: {id, status}, target: {id, status, evidence_count}}`.
- **Undo:** `POST /insight_merges/{id}/undo` writes a revision of the target, removes the
  target's evidence whose span the merge added, restores the source's earlier status and
  sets the merge `undone`. Same response shape. 409 if the merge is already undone, the
  source is no longer `merged`, or the target is no longer filed (retracted, or itself
  merged into another insight: undo that merge first).
- **`GET /memo` entries** gained `research_question_id` (as stored), `conversation_ids` (the
  reflection's conversation, then any other its evidence comes from) and `merged_from:
  [{merge_id, connection_id, insight_text, merged_at}]` (the active merges into the entry).
- **Evidence items** in `GET /memo`, `GET /organize` and the review routes carry
  `conversation_id` and `conversation_title` of the passage itself, read from its snippet.
  `GET /codebook_prep` evidence carries `conversation_id`. An entry's own
  `conversation_id`/`conversation_title` remain its reflection's.

## Merge nudges

`GET /merge_nudges` and `POST /merge_nudges/generate` return a list of `{id, kind, status,
reason, suggested_wording, default_target_id, research_question_id, research_question_name,
items, merged_preview, result_merge_id, ai_metadata, created_at}`.

- `kind` is `questions` or `insights`. `items` holds the two sides, in the order the model
  named them.
  - questions: `{id, name, description, insight_count, highlight_count}`.
    `suggested_wording` is a wording for the merged question.
  - insights: `{id, insight_text, conversation_id, conversation_title, evidence_count,
    conversation_count}`, where `id` is the connection id. `research_question_id` and
    `research_question_name` say which question both are under. `merged_preview` is
    `{evidence_count, conversation_count}` for the merged insight (distinct spans, distinct
    conversations).
- `default_target_id` is the side that would be kept: the question with more filed
  insights, or the insight with more evidence.
- `POST .../accept` returns `{id, status, kind, source_id, target_id, merge}`. `merge` is
  the question-merge object of `POST /research_questions/{id}/merge_into` or the
  insight-merge object above, plus `kind`. Undo it with `POST /rq_merges/{id}/undo` or
  `POST /insight_merges/{id}/undo`. `apply_suggested_wording: true` also renames the target
  question.
- `POST .../dismiss` returns `{id, status}`.
- A pair is suggested once. Any earlier nudge for it (pending, accepted or dismissed) and
  any merge of it by hand (undone or not) rules it out.

## Find more passages

`POST /research_questions/{rq_id}/find_passages` returns `{research_question_id,
searched_conversation_ids, failed: [{conversation_id, detail}], suggestions: [...]}`.
Without `conversation_id` it searches the conversations the user opened
(`rl_reading_state`), most recently opened first, that are in scope and have a transcript,
up to `find_passages.max_conversations_per_run` (3). Each conversation is one model call and
its suggestions are saved before the next. If the first call fails the route answers with
the AI error; if a later one fails the run stops and reports it in `failed`.

A suggestion is `{id, research_question_id, conversation_id, conversation_title, snippet_id,
start_index, end_index, speaker_name, is_facilitator, time_seconds, snippet_start_offset,
text, reason, status, highlight_id, ai_metadata, created_at}`. `time_seconds` is the start
time Cortico gives the passage's first word, else the snippet's start offset.
`GET .../passage_suggestions` returns `{research_question_id, suggestions}`.
`POST /passage_suggestions/{id}/accept` returns `{suggestion, highlight}`, where `highlight`
is the ordinary highlight object (with `cortico_sync` and `research_question_ids`).
`.../dismiss` returns `{id, status}`.

## What am I missing

`POST /research_questions/{rq_id}/whats_missing` and `GET /research_questions/{rq_id}/gaps`
return `{research_question_id, gaps: [...]}`. A gap is `{id, research_question_id, kind,
observation, conversations: [{conversation_id, title, in_listen_next, next_step_id}],
status, ai_metadata, created_at}`. `kind` is `perspective`, `counter_view` or `thin_spot`.
`conversations` are unopened conversations the observation names; `in_listen_next` says
whether the user already has a next step for that conversation that they have not dismissed.

`POST /question_gaps/{id}/listen_next` returns `{gap, next_steps: [...]}`. Each next step is
the `GET /next_steps` object plus `created` (false when the conversation was already on the
list). New rows are `suggested`, which is the status the Listen next list shows, with the
gap's `ai_metadata` plus `origin: "question_gap"` and `question_gap_id`.
`POST /question_gaps/{id}/dismiss` returns `{id, status}`.

## `GET /desk` shape

`{keep_reading: [...], finish: [...], last_conversation: {...} | null}`, for the signed-in
user, limited to conversations in the active workspace's projects (in snapshot mode with no
workspace, the whole corpus; in `cortico_api` mode with no workspace, everything is empty).

- `keep_reading[]`: conversations the user opened (`rl_reading_state`) that have no voice
  memo of theirs other than a failed one, newest opened first, at most 8. Each is
  `{conversation_id, title, project_id, project_name, start_time, duration_seconds,
  first_opened_at, last_opened_at, position_seconds, position_snippet_id, highlight_count,
  starred_count}`. `project_name` is null in `cortico_api` mode, because project names are
  not cached in the sidecar; `start_time` is null in snapshot mode.
- `finish[]`: every voice memo of the user's that is waiting on them, newest first, across
  all conversations: `{voice_memo_id, conversation_id, conversation_title, state,
  undecided_count, typed, voice_memo_status, created_at}`. `state` is `undecided` (it has
  `suggested` connections; `undecided_count` says how many), `not_reviewed`
  (`ready_for_review` with no connections at all) or `transcribing`. The count comes from
  connection statuses, never from the review draft.
- `last_conversation`: the most recently opened visible conversation, `{conversation_id,
  title, last_opened_at, position_seconds, position_snippet_id}`.

## `GET /memo/set_aside` shape

`{items: [{connection_id, kind, insight_text, source, research_question_id,
research_question_name, conversation_id, conversation_title, voice_memo_id, set_aside_at,
set_aside_at_source, restores_to}]}`. `kind` is `retracted` or `rejected`.
`research_question_id` is resolved through RQ merges. `set_aside_at_source` is `recorded`
when an `rl_set_aside` row holds the time, and `last_changed` for a connection set aside
before that table existed, where the time is the connection's `updated_at`. `restores_to`
is the status `POST .../restore` would set.

## Activity log

`GET /activity` returns `{items: [{id, created_at, actor, kind, summary, refs}],
next_before}`, newest first. `filter` is `all` (default), `ai` (actor `ai`), `changed` or
`set_aside`. `limit` is 1 to 200 (default 50). Pass a page's `next_before` as `before` for
the next page; it is null on the last page. `refs` is an object of ids, with only the keys
that apply.

Rows are written only by `log_activity` in `backend/app/services/activity.py`. That module
lists every kind in `KINDS`, with its filter bucket, and its docstring says how to add one.

| Kind | Actor | Filter | Written by | Refs |
|---|---|---|---|---|
| `insights_suggested` | ai | ai | `persist_review_result` | `conversation_id`, `voice_memo_id`, `count` |
| `next_steps_suggested` | ai | ai | `generate_next_step_suggestions` | `count` |
| `code_families_suggested` | ai | ai | `synthesize_code_families` | `research_question_id`, `count`, `merge_count` |
| `reflection_typed` | user | | `create_typed_reflection` | `conversation_id`, `voice_memo_id` |
| `reflection_recorded` | user | | `create_voice_memo` | `conversation_id`, `voice_memo_id` |
| `insight_kept` | user | | `file_to_memo` (accept) | `connection_id`, `research_question_id`, `conversation_id`, `voice_memo_id` |
| `insight_added` | user | | `file_to_memo` (a connection the user wrote) | same |
| `insight_edited` | user | changed | `file_to_memo` (edit) | same |
| `insight_set_aside` | user | set_aside | `file_to_memo` (reject) | same |
| `insight_returned_to_review` | user | | `restore_connection` (rejected) | same |
| `memo_entry_edited` | user | changed | `edit_memo_entry` | same |
| `memo_entry_retracted` | user | set_aside | `retract_memo_entry` | same |
| `memo_entry_restored` | user | | `restore_connection` (retracted) | same |
| `rq_created` | user | | `create_research_question` | `research_question_id` |
| `rq_reworded` | user | changed | `update_research_question` (name or description changed) | `research_question_id`, `previous_name` |
| `rq_archived`, `rq_restored` | user | changed | `update_research_question` (`archived` changed) | `research_question_id` |
| `rq_merged`, `rq_merge_undone` | user | changed | `merge_rq`, `undo_merge` | `research_question_id` (the target), `source_rq_id`, `rq_merge_id` |
| `next_step_accepted` | user | | `accept` | `next_step_id`, `research_question_id`, `conversation_id` |
| `next_step_dismissed` | user | set_aside | `dismiss` | same |
| `code_family_accepted` | user | | `accept_family` | `code_family_id`, `research_question_id` |
| `code_family_dismissed` | user | set_aside | `dismiss_family` | same |
| `code_family_starred` | user | | `star_family` (only when it becomes starred) | same |
| `merge_nudges_suggested` | ai | ai | `generate_merge_nudges` | `count`, `question_pair_count`, `insight_pair_count` |
| `passages_suggested` | ai | ai | `find_passages` (one row per run that searched a conversation) | `research_question_id`, `count`, `conversation_ids` |
| `question_gaps_suggested` | ai | ai | `generate_gaps` | `research_question_id`, `count` |
| `highlight_filed` | user | | `set_highlight_questions` (only when the set changes) | `highlight_id`, `conversation_id`, `research_question_ids` |
| `memo_entry_moved` | user | changed | `edit_memo_entry` (a move) | `connection_id`, `research_question_id` (the new one), `previous_research_question_id`, `conversation_id`, `voice_memo_id` |
| `insights_merged`, `insight_merge_undone` | user | changed | `merge_insights`, `undo_insight_merge` | `connection_id` (the target), `source_connection_id`, `insight_merge_id`, `research_question_id`, `conversation_id`, `voice_memo_id` |
| `merge_nudge_dismissed` | user | set_aside | `dismiss` (merge nudges) | `merge_nudge_id`, `nudge_kind`, `item_ids`, `research_question_id` |
| `passage_accepted` | user | | `accept_passage` | `passage_suggestion_id`, `highlight_id`, `research_question_id`, `conversation_id` |
| `passage_dismissed` | user | set_aside | `dismiss_passage` | `passage_suggestion_id`, `research_question_id`, `conversation_id` |
| `question_gap_dismissed` | user | set_aside | `dismiss_gap` | `question_gap_id`, `research_question_id` |
| `question_gap_listen_next` | user | | `gap_listen_next` (only when a row is added) | `question_gap_id`, `research_question_id`, `conversation_ids`, `next_step_ids` |
| `listening_lines_suggested` | ai | | `listening_suggestions.refresh` | `count` |
| `listening_line_accepted` | user | | `accept_listening_suggestion` | `listening_suggestion_id` |
| `listening_line_dismissed` | user | set_aside | `dismiss_listening_suggestion` | `listening_suggestion_id` |
| `gists_generated` | ai | | `gists.generate_missing` | `count` |

Accepting a merge nudge writes the merge's own row (`rq_merged` or `insights_merged`), and
`rq_reworded` when the suggested wording is applied.

Not logged: unstarring, family renames, member changes and family merges, merge-suggestion
decisions, creating, editing or deleting a highlight, reorders, and workspace changes. A repeat of a decision that changes
nothing (accepting an accepted next step) writes no row.

## `GET /organize` shape

`{focused: [group], other: [group], focus_count, focus_range: [5, 7]}`. Each group is
`{rq, merged_sources, entry_count, entries, families, cap, starred_unassigned}`:

- `entries[]`: `{connection_id, insight_text, research_question_id, conversation_id,
  conversation_title, voice_memo_id, family_ids, evidence}`. `evidence[]` is
  `{highlight_id, snippet_id, start_index, end_index, source, starred, text,
  conversation_id, conversation_title}`, built by `evidence_serializer` in
  `routers/memo.py`, the same code path as `GET /memo` entries. The last two are the
  passage's own conversation, which differs from the entry's after an insight merge.
- `starred_unassigned[]`: `{conversation_id, conversation_title, highlights: [{highlight_id,
  notes, snippets: [{snippet_id, start_index, end_index, text}]}]}`. It is the set `GET /memo`
  returns (`starred_unassigned_groups`, over every filed connection via
  `assigned_highlight_ids`), restricted to the conversations the group's entries and their
  evidence come from. (Highlights filed under a question are a separate list:
  `GET /research_questions/{rq_id}/highlights`.)

## Cross-cutting rules

- **Users:** every route takes `user: CurrentUser = Depends(current_user)` and scopes by
  `user.id`. In snapshot mode that is `config.default_user_id` (101) with no sign-in. In
  `cortico_api` mode it is the Cortico user behind the `nim_session` cookie (or
  `Authorization: Bearer <session token>`), and a request without one answers 401. A row
  that belongs to another user answers 404 (`backend/app/ownership.py`).
- **Cortico reads** go through `Depends(get_source)`, never `models_cortico` (the two
  snapshot-mode listings in `routers/workspaces.py` are the exception).
- **Timestamps** on the routes added 2026-11-18 (desk, reading position, review draft,
  listening interests, set aside, activity, question highlights, insight merges, merge
  nudges, passage suggestions, gaps) are ISO 8601 with a `+00:00` offset
  (`app/timefmt.py`). Older routes return `isoformat()` of whatever the database gave,
  which has no offset on SQLite.
- **Request bodies** are Pydantic models in `backend/app/schemas.py`. Responses are dicts
  built by each router's `_serialize*` helpers.
- **Expected errors** are `HTTPException`, with a `{"detail": ...}` body.
- **RQ-scoped routes** read the RQ's merged group (`services/rq_merges.py`). A merged
  source has no memo section or Organize group of its own.
- **Adding a route** means also adding its `api.*` method in `frontend/src/lib/api.ts` and
  its response type in `frontend/src/lib/types.ts`.
