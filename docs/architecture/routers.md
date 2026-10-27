# Routers

All routers live in `backend/app/routers/` and are registered in `backend/app/main.py`.
`GET /health` lives in `main.py`. CORS allows only `http://localhost:5173`. Routes marked
**paid** call OpenAI.

| Router | Method and path | Handler |
|---|---|---|
| conversations | `GET /conversations` | `list_conversations` (the full 201-conversation corpus, never scoped) |
| | `GET /conversations/{conversation_id}` | `get_conversation` |
| | `PUT /conversations/{conversation_id}/state` | `update_transcript_state` |
| highlights | `GET /conversations/{conversation_id}/highlights` | `list_highlights` |
| | `POST /conversations/{conversation_id}/highlights` | `create_highlight` (`project_id` comes from the Cortico conversation) |
| | `PATCH /highlights/{highlight_id}` | `update_highlight` |
| | `DELETE /highlights/{highlight_id}` | `delete_highlight` |
| checkpoints | `PUT /conversations/{conversation_id}/checkpoints` | `set_checkpoint` (an upsert) |
| | `DELETE /conversations/{conversation_id}/checkpoints/{guide_question_id}` | `delete_checkpoint` |
| research_questions | `GET /research_questions?include_archived=` | `list_research_questions` |
| | `POST /research_questions` | `create_research_question` (uses `default_project_id`) |
| | `PATCH /research_questions/{rq_id}` | `update_research_question` |
| | `PUT /research_questions/reorder` | `reorder_research_questions` |
| | `POST /research_questions/{rq_id}/focus` | `set_focus` (409 on a merged RQ) |
| | `POST /research_questions/{rq_id}/merge_into` | `merge_into` (409 on self, archived or merged RQs, or a cycle) |
| | `POST /rq_merges/{merge_id}/undo` | `undo` (on `rq_merges_router`; 409 if already undone) |
| voice_memos | `POST /conversations/{conversation_id}/voice_memos` | `create_voice_memo` (multipart `audio` and `duration`; 400 over `voice_memo.max_duration_seconds`; starts transcription) |
| | `GET /voice_memos/{voice_memo_id}` | `get_voice_memo` |
| | `PATCH /voice_memos/{voice_memo_id}` | `edit_voice_memo` |
| | `GET /conversations/{conversation_id}/voice_memos` | `list_voice_memos_for_conversation` |
| review | `POST /voice_memos/{voice_memo_id}/review` | `run_review` (**paid**) |
| | `GET /voice_memos/{voice_memo_id}/review` | `get_review` |
| | `POST /voice_memos/{voice_memo_id}/file` | `file_to_memo` (the only way into the memo) |
| memo | `GET /memo` | `get_memo` (not filtered by user, which is fine with a single user) |
| | `PATCH /memo/connections/{connection_id}` | `edit_memo_entry` |
| | `POST /memo/connections/{connection_id}/retract` | `retract_memo_entry` |
| | `PUT /memo/reorder` | `reorder_memo_entries` (accepts entries from the RQ's merged group) |
| next_steps | `POST /next_steps/generate` | `generate` (**paid**) |
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
| organize | `GET /organize` | `get_organize` (focused and other RQ groups, merged sources folded into their target) |
| | `GET /codebook_prep` | `get_codebook_prep` (read-only JSON of focused RQs' starred families; nothing written server-side) |
| settings | `GET /settings/review-prompt` | `get_review_prompt` |
| | `PUT /settings/review-prompt` | `save_review_prompt` |
| | `POST /settings/review-prompt/reset` | `reset_review_prompt` |

## Cross-cutting rules

- **Single user:** every user-scoped read and write uses `config.default_user_id` (101).
  There is no auth.
- **Request bodies** are Pydantic models in `backend/app/schemas.py`. Responses are dicts
  built by each router's `_serialize*` helpers.
- **Expected errors** are `HTTPException`, with a `{"detail": ...}` body.
- **RQ-scoped routes** read the RQ's merged group (`services/rq_merges.py`). A merged
  source has no memo section or Organize group of its own.
- **Adding a route** means also adding its `api.*` method in `frontend/src/lib/api.ts` and
  its response type in `frontend/src/lib/types.ts`.
