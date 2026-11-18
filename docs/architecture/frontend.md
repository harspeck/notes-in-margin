# Frontend

The frontend is React 19, react-router-dom 7, Vite 8, TypeScript ~6 and oxlint (see
`frontend/package.json`). There are no frontend tests.

The screens follow `mocks/mock-e-after-sync-cortico.html` (Mock E). Its build order is Desk,
Read and Questions first, Review next, Codebook later. Desk, Read (with Reflect), Review and
Questions and Activity are built from it. Codebook, Memo and Settings are the earlier pages
under the new header, restyled to the brand but not redesigned. Mock E has no typed
reflection; the writing box in the notebook was designed in code from the recorder's parts.

## Talking to the backend (`frontend/src/lib/api.ts`)

- `BASE` is `VITE_API_BASE` when set, else `http://localhost:8000` in development and `/api`
  in a production build. `vite.config.ts` has no proxy, so local development depends on the
  backend's CORS setting for `http://localhost:5173`. Deployed, the root `vercel.json`
  serves the frontend and the backend from one Vercel project: `/api/*` goes to the backend
  with the prefix stripped, which keeps the session cookie same-origin, and every other path
  that isn't a built file falls back to `index.html`.
- Every call sends `credentials: "include"`. A 401 from anything but `/auth/login` fires
  `SIGNED_OUT_EVENT`, which `App.tsx` answers with the sign-in screen.
- Every backend route has one method on the exported `api` object. Calls use plain `fetch`;
  the JSON content-type is set unless the body is `FormData`.
- Any non-OK response throws `Error("<status> <path>: <body>")`. `errorText` turns a caught
  error into the string the pages show inline.
- `audioUrlFor` keeps only the file's basename, so audio must sit flat in `db/local/audio`.
- `setReadingPosition`, `saveReviewDraft` and `deleteReviewDraft` take a `keepalive` flag, for
  the save made while the page is being left.
- `DESK_CHANGED_EVENT` is fired by the reader once an open is recorded, and the header answers
  by re-reading `GET /desk` for its last-conversation chip.

## Types (`frontend/src/lib/types.ts`)

These types mirror the dicts each router's `_serialize*` helpers build, not
`backend/app/schemas.py`. When a backend response shape or status value changes, update the
matching type in the same PR, because nothing checks them against each other.

- `ConversationSummary` carries `start_time` and `num_participants`.
- `ResearchQuestion` carries `focused`, `merged_into` and `merged_sources` (`RQMergedSource`).
  `RQMerge.status` is `"active" | "undone"`, mirroring `rl_rq_merge.status`.
- `CodeFamily` carries `starred`. `CodeFamilyCap` is the `cap` dict shared by
  `CodeFamilyState`, `OrganizeGroup` and the create-family response.
- `OrganizeResponse` (with `OrganizeGroup` and `OrganizeEntry`) mirrors `GET /organize`;
  entry `evidence` reuses `ConnectionEvidence` and group `starred_unassigned` reuses
  `MemoStarredUnassignedGroup`, because the backend builds both with the `GET /memo` helpers.
  `CodebookPrep` mirrors `GET /codebook_prep`. `MemoSection` carries `merged_from`.
- `Highlight` carries `research_question_ids`. `QuestionHighlights` mirrors
  `GET /research_questions/{id}/highlights`, `Breadth` (`BreadthQuestion`) mirrors
  `GET /research_questions/breadth`, and `GuideIdeas` mirrors `.../guide_ideas`.
- `MemoEntry` carries `research_question_id` (as stored), `conversation_ids` and `merged_from`
  (`MemoMergedFrom`). `ConnectionEvidence` carries the passage's own `conversation_id` and
  `conversation_title`. `ReflectionConnection.status` includes `merged`.
  `MemoEntryEditResult` is the answer of `PATCH /memo/connections/{id}` (a move included) and
  `InsightMergeResult` the answer of an insight merge and of its undo.
- `MergeNudge` is a union on `kind` (`QuestionMergeNudge`, `InsightMergeNudge`), because the
  two sides in `items` differ. `MergeNudgeAcceptResult.merge.id` is what the matching undo
  route takes. `PassageSuggestion`, `FindPassagesResult`, `PassageAcceptResult`, `QuestionGap`
  and `GapListenNextResult` mirror the question lever routes.
- `VoiceMemo` carries `typed`; `audio_path` and `duration` are null for a typed reflection.
  `MemoEntry` carries `voice_memo_typed`.
- `Desk` (`DeskReadingItem`, `DeskFinishItem`, `DeskLastConversation`) mirrors `GET /desk`, and
  `ReadingState` the answer of `PUT .../reading_position`. `ReviewDraftResponse`,
  `ListeningInterest`, `SetAsideItem`, `RestoreResult`, `ActivityItem` and `ActivityPage`
  mirror the review draft, listening interest, set-aside, restore and activity routes.

## Shared code (`frontend/src/lib/`)

- `format.ts`: `useToast`, time and date formatting, `plural`, and `setAsideDate` (the
  set-aside date, worded as "on or before" when `set_aside_at_source` is `last_changed`).
  `useToast()` returns `toast(text, action?)`. With an action (`{label, run}`) the toast shows
  a button, stays 9 seconds and takes clicks. That is how a merge is undone from the toast.
- `format.ts` also words breadth: `heardLine`, `breadthWord`, `questionRowLine` (the line
  under a question's name in every list) and `breadthSentence` (the sentence on a question's
  page). They take numbers from `GET /research_questions/breadth` and compute none.
- `ui.tsx`: `ToastProvider`, `Icon`, `Dots` (the breadth gauge) and `ProvTag` (Suggested by
  AI, AI kept as written, AI + you, Yours). It exports components only, for fast refresh.
- `questions.ts`: `buildQuestions` joins `GET /organize` (questions, merges, code families)
  with `GET /memo` (who wrote each insight), for the Questions page. It holds no breadth.

Breadth is counted by the server, never in the browser. "Heard in N of M" is a question's
`conversation_count` over `opened_conversation_count` (conversations the user has opened),
and the project count is `projects_heard_count` over `projects_in_scope_count`. The Desk,
the notebook and the Questions page all read `GET /research_questions/breadth`.

What the user has open and what is waiting for them comes from `GET /desk`. Nothing about it
is kept in the browser (the earlier `recent.ts` and its `nim.recent` key are gone).

## Routes (`frontend/src/App.tsx`)

| Path | Page | Notes |
|---|---|---|
| `/` | `DeskPage` | Three lanes and three lists. Keep reading and Finish come from `GET /desk`. Keep reading: opened conversations with no reflection, with the project name, progress (audio position over duration), "You stopped at", highlight counts and "Pick up at", which links to `/read/N?resume=1`; three cards show until "Show more". Finish: every reflection waiting on the user, worded by `state` (`undecided`, `not_reviewed`, `transcribing`). Listen next: `GET /next_steps` suggestions. Then all conversations with a title search, "What you want to hear next" (`ListeningInterests`, with "Suggest next steps", paid), and the questions with their breadth (`GET /research_questions/breadth`, which also marks a conversation "Reflected") and a jot form. With no questions the list shows `GuideIdeas`. Hosted, `GET /desk` has no project names, so the page reads the active organization's projects once (`GET /organizations/{id}/projects`) and maps `project_id` to a name. `/?conversation=N&snippet=ID` (the old transcript link) redirects to `/read/N?snippet=ID`. |
| `/read/:conversationId` | `ReaderPage` | The transcript in one wide column with notes in the margin. `?snippet=ID` scrolls to and flashes a turn, `?resume=1` returns to the saved turn and audio time (`?snippet=` wins when both are there), and `?review=ID` shows "Back to review". Selecting words offers Highlight, Star and Add note. With audio, the bar holds `Player` (play and pause, back and forward 10 seconds, a position slider, speed), each turn's time is a play button, and double-clicking a word plays from that word, so a double-click no longer selects it. The turn and word being played carry `data-now`, set on the page by `markNow` and not through React state, so playing does not render the transcript again. Highlights that exist in Cortico (`GET /conversations/{id}/cortico_highlights`) are underlined in grey with an "In Cortico" margin note, read-only, and a checkbox above the transcript hides them. A margin note shows the questions the highlight is filed under as chips, with "Add to a question" (`PUT /highlights/{id}/questions`). Passages the AI found that are still undecided are drawn dashed in the transcript with "Add as a highlight" and "Not this". Guide sections are set, moved and removed from the transcript. The notebook is closed until asked for; Reflect opens it with the recorder, or with the writing box. `MAX_VOICE_MEMO_SECONDS = 1200` must match `voice_memo.max_duration_seconds` in `backend/config.yaml`. |
| `/review/:voiceMemoId` | `ReviewPage` | Insights grouped by question with transcript passages as receipts, Keep, Edit and Set aside, starred highlights not yet in an insight, "Add an insight the AI missed", the Set aside list with "Bring back", and the decision bar. A typed reflection shows "What you wrote" with no player, duration or recording links. |
| `/questions` | `QuestionsPage` | `?q=<id>` picks the question (`?q=none` is the insights with no question). The side list has the questions with their breadth, the jot form, "Look for things to merge" (paid) with the suggested question pairs under it, `GuideIdeas`, and the merged and archived questions with "Undo merge" and "Restore". A question has: reword, the breadth sentence, the three levers (`QuestionLevers`), suggested insight pairs, its insights, "Highlights filed here, no insight yet" (`GET /research_questions/{id}/highlights`, with "Remove from this question"), its Set aside list with "Bring back", merge into another question, undo a merge, and archive. An insight card groups its passages by the conversation each comes from, lists what was merged into it with "Undo merge", and offers Edit, Move (`PATCH` with `research_question_id`), "Merge into…" (any other insight, under any question) and Set aside. |
| `/activity` | `ActivityPage` | The log from `GET /activity`, newest first. The chips All, Claude suggested, You changed and You set aside are the route's `filter` (`all`, `ai`, `changed`, `set_aside`); "Show more" passes `next_before` as `before`. A row shows the summary as given and links to the review, conversation and question its `refs` name. |
| `/codebook` | `OrganizePage` | The earlier Organize page: the questions with a focus toggle, each question's insights, its code families (`CodeFamilyCard`), the cap, merges and "Prepare codebook". The selection lives in `?rq=<id>`. `/organize` redirects here. It never calls synthesis. |
| `/memo` | `MemoPage` | The earlier memo page: entries, next steps, code families and merge suggestions. |
| `/settings` | `SettingsPage` | View, edit, save and reset the review prompt. |
| `/projects` | `WorkspacePage` | Picks the projects to work in: open a saved set, or choose an organization and its projects and save a new one (`POST /workspaces`). The page says "projects"; the backend calls a saved set a workspace. |

`App.tsx` reads `GET /auth/me` before showing any route (`lib/session.ts` holds the result).
A 401 shows `SignInPage` (`POST /auth/login`). In `cortico_api` mode with no active workspace
it shows `WorkspacePage` alone. In `snapshot` mode `/auth/me` answers as the default user, so
neither appears. `Routes` is keyed by the active workspace id, so screens reload when it
changes. In `cortico_api` mode the Reflect panel shows the writing box in place of the
recorder, because the hosted backend can't store or transcribe audio
(`docs/architecture/cortico.md`). The header's last-conversation chip is
`last_conversation` from `GET /desk` and links with `?resume=1`.

## Components (`frontend/src/components/`)

- `TranscriptView`: turns, word-level highlights, margin notes (star, note, questions,
  remove), AI-found passages (`mark.hl.found` and `aside.mnote.found`), guide section markers,
  and the selection menu.
- `GuideIdeas`: "From your conversation guide" (`GET /research_questions/guide_ideas`), each
  idea with "Add as a question" and, when `projects_not_loaded` is not empty, "Load more".
  With `emptyState` it is the empty state of a questions list. Used by `QuestionsPage`,
  `DeskPage` and `Notebook`.
- `PassageItem`: one passage the AI found, as a row of a `ul.receipts`, with "Add as a
  highlight" and "Not this". Used by `QuestionLevers` and `Notebook`.
- `QuestionLevers`: "Find more passages", "What am I missing?" and "Suggest themes" for one
  question, with their results. It reads the saved undecided passages and gaps when the
  question opens (`GET .../passage_suggestions`, `GET .../gaps`).
- `MergeNudges`: `QuestionNudge` (which question stays, "Use the suggested wording") and
  `InsightNudge` (which wording stays), each with Merge and Keep separate.
- `Notebook`: the docked panel with the conversation's notes and the questions (reword, jot,
  breadth, and "Find more passages" for this conversation only, with the results under the row).
  In reflect mode it also holds `Recorder`: record, pause, finish, then three visible steps
  (saved, transcribing with a poll every 700 ms, connecting to the questions). `Writer` is the
  typed way in: the same three prompts, a textarea, and two visible steps (saved, connecting).
  Hosted it is the only one; locally "Type it instead" and "Record it instead" switch between
  the two while the recorder is idle. Unsent text is kept per conversation in memory and in
  `localStorage` (`nim.reflection.<conversation id>`), and is cleared once it is saved.
- `AiMark` (`lib/ui.tsx`): the sparkle that marks AI. It sits inside every button that asks the AI and beside content the AI wrote; there is no "Asks Claude" text.
- `ListenNext`: the "Read next" card under a review once everything is decided and saved. It calls `POST /next_steps/recommend` when shown. The Desk's Listen next lane calls the same route when nothing is waiting, and offers "Summarise N conversations" (`POST /gists/generate`, repeated until none remain or Stop) while conversations have no gist.
- `ListeningInterests`: the "what you want to hear next" lines. Lines the AI offered show dashed with Keep and Not this; saved offers load first, and with none waiting it calls `POST /listening_interests/suggestions/refresh`, which the server answers from its cache unless the questions or highlights changed. Each saves when it loses focus
  (`PATCH`), has a remove button (`DELETE`), and "Add a line" posts a new one. Its children sit
  beside "Add a line".
- `CodeFamilyCard`: `CodeFamilyCard` (star, rename and redefine, accept, dismiss; with
  `addableEntries` it also shows removable member chips and an "Add entries" picker, with
  `onSelect` a merge checkbox, and with `onToggleActive` the name becomes a toggle that sets
  `active`), `MergeSuggestionCard`, and `EntryPicker`. Every prop past `family` and `onChanged`
  is optional, so `MemoPage` uses the plain card. Used by `MemoPage` and `OrganizePage`.

## Behaviour to keep

- Every paid action runs only from its own click: "Look for things to merge"
  (`POST /merge_nudges/generate`), "Find more passages", "What am I missing?", "Suggest
  themes" and "Suggest next steps". Nothing calls them on load. Opening a question or the
  notebook only reads what was saved (`GET /merge_nudges`, `.../passage_suggestions`,
  `.../gaps`).
- "Find more passages" on the Questions page calls `POST .../find_passages` once per
  conversation, never without `conversation_id`, so each request stays inside the server's
  time limit. It reads up to `MAX_FIND_CONVERSATIONS` (3) conversations: the last one opened,
  then the Desk's Keep reading, then any other opened conversation. It stops at the first
  failure and keeps what it already found, and leaving the question stops it before the next
  call. In the notebook it is one call, for the open conversation.
- Passage suggestions for an open conversation are read the first time the notebook opens
  (one `GET .../passage_suggestions?conversation_id=` per question), because no route lists
  them by conversation. `ReaderPage` holds them, so the transcript and the notebook show the
  same list, and an accepted one becomes a highlight in both at once.
- `PUT /highlights/{id}/questions` replaces the whole set and answers 409 for an archived
  question. So the margin note and "Remove from this question" send back only the questions
  that are still active.
- A merged insight (`status: "merged"`) is neither in the memo nor set aside. The Questions
  page never lists it, and Review only counts it in one line under "Already in your memo".

- The AI review runs once per reflection, through `connectToQuestions` in `Notebook.tsx`:
  `Recorder` calls it right after transcription and `Writer` right after the typed reflection
  is saved. It is the only `runReview` call in the notebook; don't add another. It is
  best-effort: if `runReview` fails, the Review page still opens with the error and the
  reflection's text. `ReviewPage` only ever calls `GET /voice_memos/{id}/review`, so resuming a
  review never re-runs the AI, which would duplicate suggestions. The one exception is a
  reflection with no suggestions at all, where the page offers a button that asks Claude.
- Review decisions reach the memo only at "Add to memo" (`POST /voice_memos/{id}/file`). Until
  then they are a draft on the server (`/voice_memos/{id}/review_draft`), saved 800 ms after a
  change, on "Finish later", and when the page is left. Keep is `accept`, an edit is `edit`,
  and Set aside is `reject`. Three rules keep the draft from duplicating insights:
  - The page waits for the draft before it shows the review, so a new decision can't overwrite
    saved ones.
  - Saves run one after another and filing waits for the last one, because filing deletes the
    server's draft and a late save would put it back, with its added insights.
  - A draft left in `localStorage` by an earlier version (`nim.review.<voice memo id>`) is
    uploaded once, when the server has none, and the local copy is removed. It is also removed
    when the server has a draft.
- A typed reflection (`VoiceMemo.typed`) has no audio. Review and Memo show no player,
  duration or recording link for it, and say "what you wrote".
- "Set aside" on the Questions page is `POST /memo/connections/{id}/retract`, and "Bring back"
  on both Set aside lists is `POST /memo/connections/{id}/restore`. A retracted insight goes
  back into the memo. A rejected one goes back to `suggested` in its reflection's review, never
  straight into the memo, so the Questions page says so and links to `/review/<voice memo id>`.
- The reader records the open with an empty `PUT .../reading_position`, then sends the
  position only when it has changed, at most every 10 seconds, and once more on `pagehide` and
  on leaving the page. `position_seconds` is sent only after the audio has been played there,
  and `snippet_id` only after the page has scrolled, so opening a conversation never moves a
  saved position. The turn saved is the one on the line 200 px from the top, which must match
  `.turn { scroll-margin-top }` in `styles.css` (`READING_LINE_PX` in `ReaderPage.tsx`), or
  every resume would drift by a turn.
- `ActivityPage` tags a row "You changed" or "You set aside" from two sets of kinds that mirror
  the `CHANGED` and `SET_ASIDE` buckets of `KINDS` in `backend/app/services/activity.py`.
  A new kind in either bucket must be added to both.
- `npm run lint` exits 0 with 2 warnings, both in `MemoPage.tsx` (set-state-in-effect,
  exhaustive-deps). Don't add new ones.

## In Mock E, not built

Each needs a backend route first, except the last.

- AI suggestions for where a guide section starts.
- The Codebook as parent themes with codes under them, and the code storming handoff.
- A leave reminder when a conversation has highlights but no reflection.
- History on an insight card (its earlier wordings).
- Keep and Set aside on a suggested code right under "Suggest themes". The result links to the
  codebook draft, where they are.
