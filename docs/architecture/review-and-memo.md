# Review, filing, memo and evidence

## Flow

1. The user highlights passages (`rl_highlight` and `highlight_snippet`) and reflects:
   either a recorded voice memo, which is then transcribed (snapshot mode only), or a typed
   reflection (`POST /conversations/{id}/reflections`, both modes). A typed reflection is a
   `voice_memo` row with an empty `audio_path`, no duration and no word timings, already
   `ready_for_review`. Everything below treats the two alike; serializers return
   `audio_path: null` and `typed: true`, and its insights have no `reflection_timestamp`.
2. `POST /voice_memos/{id}/review` (`run_review`) calls `generate_connections`, then
   `persist_review_result`, which saves the proposals as `status="suggested"`. On resume,
   the frontend calls `GET /voice_memos/{id}/review` instead of running the review again.
3. The review step (`frontend/src/components/ReviewStep.tsx`) lets the user accept, edit or
   reject each proposal, add a connection the AI missed, and assign each starred,
   unassigned highlight to an RQ.
4. "File to memo" sends everything to `POST /voice_memos/{id}/file` (`file_to_memo`). This is
   the only route that moves a connection into the memo (to `accepted` or `edited`) for the
   first time. Rejected proposals stay, with `status="rejected"`. Filing also deletes the
   reflection's review draft (`rl_review_draft`, the client's unsaved decisions, saved with
   `PUT /voice_memos/{id}/review_draft`).
5. The memo page (`GET /memo`) shows filed connections, where filed means
   `FILED_STATUSES = ("accepted", "edited")`. The user can edit an entry (insight text and
   evidence) or retract it.
6. `GET /memo/set_aside` lists what the user retracted or rejected, and
   `POST /memo/connections/{id}/restore` brings one back (see "Set aside and restore").

## Evidence provenance (`backend/app/evidence_provenance.py`)

`connection_evidence.highlight_id` makes a passage "yours". It drives the `yours` or
`AI-suggested` badge in review, the starred flag on the memo, and whether a starred highlight
counts as assigned.

- **Every evidence write goes through `resolve_evidence`.** AI review calls it directly.
  Review adds and edits (`routers/review.py`) and memo edits (`routers/memo.py`) call
  `evidence_row`, which keeps an explicit `highlight_id` and otherwise links by overlap.
  `evidence_row` returns 400 for a `highlight_id` that belongs to another user or
  conversation.
- **Matching is by overlap.** A span links to the user highlight it overlaps on the same
  snippet. Spans are half-open `[start_index, end_index)`, and any shared word counts. Ties
  go to the starred highlight, then the larger overlap, then the older highlight
  (`match_highlight`). A linked span gets `source="user"`; an unlinked one keeps its source.
- **"Starred, unassigned"** comes only from `assigned_highlight_ids`. A starred highlight
  counts as assigned only when it is cited by a connection that has an RQ and isn't
  rejected or retracted. Review and memo both use it.
- **Backfill for older rows:** `backend/scripts/backfill_evidence_highlights.py` (supports
  `--dry-run`). It only sets `highlight_id` and `source` on rows where `highlight_id` is null
  and the span overlaps a highlight, so it is safe to re-run. The user runs it on the dev
  database.

## Editing and provenance

- Every edit (`file_to_memo` with action `edit`, and `edit_memo_entry`) first writes a
  `ReflectionConnectionRevision` snapshot (which includes `highlight_id`), then sets
  `source="user"` and `edited_from_ai=True`.
- Retracting sets a status and never deletes, so the memo's history stays intact.
- `quoted_texts` (`backend/app/evidence_text.py`) turns `(snippet_id, start, end)` spans
  into text with one batched Cortico query, and returns `""` for a missing snippet.
- Evidence links go to `/read/N?snippet=ID`. `ReaderPage` scrolls to that snippet and
  flashes it. The older `/?conversation=N&snippet=ID` form redirects there.

## Set aside and restore (`routers/memo.py`)

- Retracting an entry and rejecting a proposal each write an `rl_set_aside` row with the
  time and the status the connection had. Connections set aside before that table existed
  have no row: the list then reports the connection's `updated_at`, marked
  `set_aside_at_source: "last_changed"`.
- **Restoring a retracted entry** is the one deliberate exception to "only `file_to_memo`
  sets `accepted` or `edited`". The entry had already been filed, so restore writes a
  `ReflectionConnectionRevision` first and then puts back the filed status it had
  (`rl_set_aside.previous_status`). With no row, the status is `edited` if the entry was
  edited from an AI proposal or has a revision, else `accepted`. Restore never files
  anything that had not been filed (`tests/test_set_aside_and_activity.py`).
- **Restoring a rejected proposal** does not put it in the memo. It goes back to
  `suggested`, so it shows as undecided in its reflection's review and in `GET /desk`'s
  `finish` list, and reaches the memo only through `file_to_memo`. This also works when the
  reflection was already filed once: `file_to_memo` accepts decisions for any connection of
  the voice memo whatever the memo's own status, and the voice memo's `status` stays
  `filed`.
- Restoring a connection that is not retracted or rejected answers 409.

## Moving and merging insights (`routers/memo.py`, `services/insight_merges.py`)

Connection statuses are now `suggested`, `accepted`, `edited`, `rejected`, `retracted` and
`merged`. `merged` is neither filed (`FILED_STATUSES`) nor set aside (`SET_ASIDE_STATUSES`):
the insight left the memo because another insight took over its evidence.

- **Move.** `edit_memo_entry` moves an entry when the body names `research_question_id`
  (null for "doesn't fit a question yet"). It writes the revision every edit writes, plus an
  `rl_insight_move` row holding the old and new question, because the revision has no
  column for the question. A move alone leaves `status`, `source` and `edited_from_ai`
  alone. It takes the entry out of code families of another question's group, after writing
  an `rl_code_family_revision`. Moving back does not restore that membership.
- **Merge.** `merge_insights` is the one function behind
  `POST /memo/connections/{id}/merge_into` and an accepted insight merge nudge. It writes a
  revision of the target, copies the source's evidence onto the target through
  `resolve_evidence` (skipping a span the target already has), sets the source to `merged`
  and writes an `rl_insight_merge` row. The target's text, status and source do not change;
  the user rewords it afterwards if they want to. The source keeps its own evidence rows and
  its code family memberships, like a retracted entry.
- **Undo.** `undo_insight_merge` writes a revision of the target, deletes the target's
  evidence whose span the merge added, restores the source's earlier status and sets the
  merge `undone`. It refuses (409) when the source is no longer `merged` or the target is
  no longer filed. If the user re-saved the target's evidence after the merge, undo still
  removes the spans that came from the source and keeps the rest.
- **Evidence from several conversations.** After a merge, one insight's evidence comes from
  more than one conversation, so nothing may assume the reflection's conversation:
  - serializers label each evidence row with its own conversation through
    `evidence_conversations` (`app/evidence_text.py`), which reads it from the snippet;
  - a memo evidence edit matches highlights with `user_highlight_spans` (by snippet, across
    conversations), and `evidence_row` accepts an explicit `highlight_id` on another
    conversation when that highlight covers the evidence's snippet.
- **Guards on `merged`.** `file_to_memo` skips a decision about a `merged` connection, so
  re-filing a reflection can't put it back in the memo next to its target. Edit, retract and
  restore refuse it (404, 404, 409). `assigned_highlight_ids` still counts a `merged`
  connection's highlights as assigned in its reflection's review, because its target cites
  them.

## Highlights

- `create_highlight` takes `project_id` from the Cortico conversation, never from
  `config.default_project_id`. That was a regression, and `tests/test_highlights.py`
  guards it. The route body is `create_highlight_for_user` (`services/highlights.py`), which
  accepting a passage suggestion also calls, so both get the same project id and the same
  Cortico write-back.
- A highlight can be filed under research questions (`rl_highlight_question`,
  `PUT /highlights/{id}/questions`). `GET /research_questions/{id}/highlights` lists the
  ones no filed insight cites yet. This is separate from "starred, unassigned", which is
  still computed only by `assigned_highlight_ids`.
- RQs still use `config.default_project_id` (`research_questions.py` and `seed.py`).
