# LLM and transcription calls

## Provider and configuration

Every model call goes through `structured_json` in `backend/app/llm.py`. It takes a system
prompt, a JSON user message and a JSON schema, and returns the parsed object plus the model
that answered.

- **Provider:** `ai_review.provider` in `backend/config.yaml`, `"anthropic"` (the default)
  or `"openai"`. The env vars `AI_PROVIDER`, `AI_MODEL` and `AI_REASONING_EFFORT` override
  the config, so a local run can go back to OpenAI with
  `AI_PROVIDER=openai AI_MODEL=gpt-5.6-luna`.
- **Claude (default):** `anthropic==2.6.0`, model `claude-opus-5-5`, effort `low`.
  - The client reads `ANTHROPIC_API_KEY` from the environment (`backend/.env` locally, the
    Vercel env in production).
  - Requests stream through `client.beta.messages.stream(...)` and use
    `output_config={"effort": ..., "format": {"type": "json_schema", "schema": ...}}`.
  - Thinking is left at the model's default (adaptive; Opus 5.5 can't turn it off), and
    `effort` is the only depth control.
  - `anthropic_schema` rewrites `{"type": [X, "null"]}` as `anyOf` before sending.
  - Server-side fallback is on (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`):
    if Claude declines on policy grounds, the API reruns the request on a fallback model.
    `ai_metadata.model` records the model that actually answered.
  - `max_output_tokens: 32000`. Timeout 280 s (under Vercel's 300 s limit), 2 SDK retries on
    429, 5xx and network errors.
- **OpenAI (selectable):** `openai==3.24.0`, the Responses API with
  `text={"format": {"type": "json_schema", "strict": True, ...}}`. It reads `OPENAI_API_KEY`
  and `OPENAI_BASE_URL`.
- **Errors:** every provider failure becomes `AIProviderError`, which `main.py` answers as
  `{"detail": ..., "source": "ai"}`:
  - 503 for a bad or missing key, rate limits, overload and network errors;
  - 422 when Claude declines (`stop_reason: "refusal"`) and the fallback declines too;
  - 502 for a rejected request, a cut-off answer (`max_tokens`) or JSON that doesn't parse.

## Call sites

| Function | Output schema name and shape | Checks after the model returns |
|---|---|---|
| `generate_connections` and `persist_review_result` (`services/ai_review.py`) | `reflection_review`: `{connections: [{research_question_id or null, insight_text, confidence, rationale, reflection_quote_start_word, reflection_quote_end_word, evidence: [{snippet_id, start_index, end_index}]}], extracted_interests: [str]}` | `_validate_evidence_in_range` keeps only evidence whose snippet exists, is in this conversation, and has `0 <= start < end <= word_count`; invalid evidence is dropped. Evidence is linked with `resolve_evidence`, ordered starred highlight first, then other highlights, then AI-only text. Duplicate spans are dropped. |
| `generate_next_step_suggestions` (`services/next_steps.py`) | `next_step_suggestions`: `{suggestions: [{research_question_id, suggested_direction, recommended_conversation_id, rationale}]}` | A conversation id not found in Cortico becomes null. An RQ id that isn't an active RQ becomes null. A row with neither a direction nor a conversation is skipped. |
| `synthesize_code_families` (`services/code_families.py`) | `code_families`: `{families: [{existing_family_id, name, definition, member_connection_ids}], merge_suggestions: [{family_ids, name, definition, rationale}]}` | Members must be filed connections in this RQ (`_valid_connection_ids`). The cap is applied server-side. The model call is skipped when nothing is filed. See `code-families.md`. |
| `generate_merge_nudges` and `_apply_nudges` (`services/merge_nudges.py`) | `merge_nudges`: `{question_pairs: [{question_a_id, question_b_id, reason, suggested_wording}], insight_pairs: [{connection_a_id, connection_b_id, reason}]}` | Question ids must be two different active questions of the user's. Connection ids must be two different filed insights under the same question. A pair with any earlier nudge, or merged by hand before, is dropped. The caps in `config.merge_nudges` are applied server-side. No call is made when nothing could pair. |
| `find_passages` and `_apply_passages` (`services/find_passages.py`) | `find_passages`: `{passages: [{snippet_id, start_index, end_index, reason}]}` | The snippet must be in the conversation searched, and `0 <= start < end <= word_count` (half-open, as in `highlight_snippet`). A passage overlapping one of the user's highlights, an earlier suggestion for the question, or an earlier passage of the same answer is dropped. `find_passages.max_passages_per_conversation` is applied server-side. A conversation with no transcript costs no call. |
| `generate_gaps` and `_apply_gaps` (`services/question_gaps.py`) | `question_gaps`: `{observations: [{kind, observation, conversation_ids}]}`, `kind` one of `perspective`, `counter_view`, `thin_spot` | Conversation ids outside the unopened conversations the model was shown are removed. An empty observation is dropped, an unknown kind becomes `thin_spot`, and `whats_missing.max_observations` is applied server-side. |
| `backfill` (`scripts/backfill_gists.py`) | `conversation_gist`: `{summary, keywords}` | Effort is pinned to `"low"`. Rows that already exist are skipped, and it commits per conversation, so it can resume. |
| `generate_missing` (`services/gists.py`) | `conversation_gist`: `{summary, keywords}` | The same prompt and schema as the backfill, for whatever the request's data source can read (hosted conversations included). Runs on `gists.model` (`claude-haiku-5-5`). A conversation with no transcript gets an empty gist row so it is not fetched again. |
| `refresh` and `apply_suggestions` (`services/listening_suggestions.py`) | `listening_suggestions`: `{suggestions: [{text, reason}]}` | Sent the user's active questions, their newest highlights with notes, the lines already on the list and the lines turned down. Runs on `listening_suggestions.model` (`claude-haiku-5-5`). Anything matching an existing line or an earlier offer is dropped. Cached by fingerprint; see CLAUDE.md. |

## What each of the three question-screen calls is sent

All three follow `services/code_families.py`: the function that calls the model builds the
payload, and a pure `_apply_*` helper validates and saves the answer, so tests feed it
hand-built output. The prompts are constants in the service modules (`MERGE_NUDGE_PROMPT`,
`FIND_PASSAGES_PROMPT`, `WHATS_MISSING_PROMPT`) and are stored in each row's `ai_metadata`.
None can be edited by the user.

- **Merge nudges** (`POST /merge_nudges/generate`, one call): `research_questions` (each
  active question's `id`, `name`, `description` and its filed `insights` as
  `{connection_id, insight_text}`, read over its merged group),
  `already_decided_question_pairs`, `already_decided_insight_pairs`, `max_question_pairs`
  and `max_insight_pairs` (`config.merge_nudges`, 2 and 3).
- **Find more passages** (`POST /research_questions/{id}/find_passages`, one call per
  conversation, at most `find_passages.max_conversations_per_run`, 3): `research_question`
  (`name`, `description`, up to `max_insights_in_prompt` recent filed `insights`),
  `conversation` (`id`, `title`), `transcript_snippets` (`snippet_id`, `speaker`,
  `is_facilitator`, `text`, `word_count`, the same shape the review sends),
  `already_highlighted` (the user's spans in that conversation) and `max_passages`.
  Three calls in one request can come close to Vercel's 300 s limit; the client can send one
  `conversation_id` per request to avoid that.
- **What am I missing** (`POST /research_questions/{id}/whats_missing`, one call):
  `research_question`, `insights` (filed insight texts), `highlight_quotes` (the highlights
  filed under the question: `conversation_id`, `text`, `note`), `conversations_heard` (the
  conversations the user opened that are in scope), `unopened_conversations` (in-scope
  conversations they have not opened: `conversation_id`, `title`, `project_id`, plus
  `summary` and `keywords` when a `conversation_gist` exists, built by `gist_dict` in
  `services/next_steps.py`), `earlier_observations` and `max_observations`. Conversations
  with a gist are listed first, and the list is cut at
  `whats_missing.max_unopened_conversations` (150). It works with no gists at all, which is
  the hosted case today.

## Listening interests

`persist_review_result` still stores the model's `extracted_interests` on the voice memo, and
also adds each line to `rl_listening_interest` (`services/listening_interests.py`), skipping
exact duplicates of the user's active lines. Next-step generation sends the user's active
rows from that table as `extracted_interests`: lines from reflections, less the ones the
user removed, plus the ones they added. It still runs when there are none.

## Rules

- Every AI-written row has `status="suggested"` and `ai_metadata` (`model`, the one that answered,
  `reasoning_effort`, `prompt`, plus `confidence`, `rationale` and `reflection_timestamp`
  for connections). Only user actions move a row past suggested. A next step added from a
  gap by `POST /question_gaps/{id}/listen_next` is also `suggested`, with the gap's
  `ai_metadata`: the user put it on the list, and still accepts or dismisses it there.
- Accepting a merge nudge or a passage suggestion runs the code the user's own action runs
  (`merge_rq`, `merge_insights`, `create_highlight_for_user`). Never write a second copy of
  those for the AI path.
- Model ids and effort come from `config.ai_review`. The single deliberate exception is the
  gist backfill's `"low"` pin.
- The review prompt can be edited. It is stored in `app_setting` under `"review_prompt"`
  (`get_review_prompt`, `set_review_prompt`, `reset_review_prompt`), with
  `DEFAULT_REVIEW_PROMPT` as the fallback.
- The gist backfill is not an endpoint and is not triggered from the UI, by decision
  (`docs/prd-phase-2.md`). It makes about 200 paid calls; the measured OpenAI run took
  720.7 s for 198 calls, with 279,579 input and 29,813 output tokens. Claude counts tokens
  differently, so re-measure before quoting a cost.
- Tests replace the Claude client with a fake (`tests/test_llm.py` patches
  `llm._anthropic_client`), so no test calls a model.

## Transcription

- `services/transcription.py:transcribe` uses local faster-whisper (`small.en`, `int8`) and
  makes no API call. `_get_model` is cached with `lru_cache`, and the import is lazy, so
  tests never load the model.
- It runs in a daemon thread started by `POST /conversations/{id}/voice_memos`. Status goes
  `recorded` → `transcribing` → `ready_for_review` or `failed` (with `transcription_error`
  set) → `filed`. The frontend polls every 700 ms (`ReflectionPanel.tsx`).
- Words are stored as `[{word, start, end, confidence}]` dicts to match Cortico's
  `snippet.words`.
- Keep `av==13.1.0`, because 19.x breaks faster-whisper.
