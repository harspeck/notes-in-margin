# Frontend

The frontend is React 19, react-router-dom 7, Vite 8, TypeScript ~6 and oxlint (see
`frontend/package.json`). There are no frontend tests.

## Talking to the backend (`frontend/src/lib/api.ts`)

- `BASE = "http://localhost:8000"` is hard-coded, and `vite.config.ts` has no proxy, so the
  app depends on the backend's CORS setting for `http://localhost:5173`.
- Every backend route has one method on the exported `api` object. Calls use plain `fetch`;
  the JSON content-type is set unless the body is `FormData`.
- Any non-OK response throws `Error("<status> <path>: <body>")`. `errorText` turns a caught
  error into the string the Organize page and `CodeFamilyCard` show inline.
- `audioUrlFor` keeps only the file's basename, so audio must sit flat in `db/local/audio`.

## Types (`frontend/src/lib/types.ts`)

These types mirror the dicts each router's `_serialize*` helpers build, not
`backend/app/schemas.py`. When a backend response shape or status value changes, update the
matching type in the same PR, because nothing checks them against each other.

- `ResearchQuestion` carries `focused`, `merged_into` and `merged_sources` (`RQMergedSource`).
  `RQMerge.status` is `"active" | "undone"`, mirroring `rl_rq_merge.status`.
- `CodeFamily` carries `starred`. `CodeFamilyCap` is the `cap` dict shared by
  `CodeFamilyState`, `OrganizeGroup` and the create-family response.
- `OrganizeResponse` (with `OrganizeGroup` and `OrganizeEntry`) mirrors `GET /organize`;
  entry `evidence` reuses `ConnectionEvidence` and group `starred_unassigned` reuses
  `MemoStarredUnassignedGroup`, because the backend builds both with the `GET /memo` helpers.
  `CodebookPrep` mirrors `GET /codebook_prep`. `MemoSection` carries `merged_from`.

## Routes (`frontend/src/App.tsx`)

| Path | Page | Notes |
|---|---|---|
| `/` | `ReaderPage` | `?conversation=N` picks the transcript, and `&snippet=ID` scrolls to and outlines a snippet. `MAX_VOICE_MEMO_SECONDS = 1200` must match `voice_memo.max_duration_seconds` in `backend/config.yaml`. |
| `/memo` | `MemoPage` | Memo entries, next steps, code families and merge suggestions |
| `/organize` | `OrganizePage` | Three columns that scroll independently (stacked below 1000px). The left sidebar lists RQs with a focus toggle (●/○) and entry count, focused ones first, then "Other", with the "N of 5-7 focused" counter (`focus_range`; amber outside it) and "Prepare codebook" at the bottom, which downloads `codebook-prep-<YYYY-MM-DD>.json` in the browser. Clicking an RQ selects it; the selection lives in `?rq=<id>` and falls back to the first focused RQ (else the first other) when missing or merged away. The middle column shows the selected RQ (merge into, undo of merged sources), each filed insight with its quoted evidence, family tags and "+ add to family", then "Starred, not yet cited". The right column holds the RQ's code families (`CodeFamilyCard`: star, rename, members, add entries, accept, dismiss, merge checkbox), the cap, "New family" and "Merge selected"; clicking a family's name highlights its insights in the middle column. It never calls synthesis. |
| `/settings` | `SettingsPage` | View, edit, save and reset the review prompt |

## Components (`frontend/src/components/`)

- `TranscriptView`: words and highlights.
- `HoverSectionMenu`: section checkpoints.
- `SidePanel`: the RQ tab and a compact memo.
- `EndOfTranscriptPrompt`: the prompt shown once at the end of a transcript.
- `LeaveConfirmationDialog`: guards only the transcript switcher; the top-nav links aren't
  guarded.
- `ReflectionPanel`: recording, upload, and polling transcription every 700 ms.
- `ReviewStep`: accept, edit or reject proposals, assign starred highlights, and file.
- `CodeFamilyCard`: `CodeFamilyCard` (star, rename and redefine, accept, dismiss; with
  `addableEntries` it also shows removable member chips and an "Add entries" picker, with
  `onSelect` a merge checkbox, and with `onToggleActive` the name becomes a toggle that sets
  `active`), `MergeSuggestionCard`, and `EntryPicker`. Every prop past `family` and `onChanged`
  is optional, so `MemoPage` uses the plain card. Used by `MemoPage`
  and `OrganizePage`.

## Behaviour to keep

- Review is best-effort: if `runReview` fails, `ReaderPage` falls back to
  `GET /voice_memos/{id}/review`. Never re-run the review on resume, because that would
  duplicate suggestions.
- `npm run lint` currently exits 0 with 10 warnings, in `ReflectionPanel.tsx`
  (no-unused-expressions), `ReaderPage.tsx` and `MemoPage.tsx` (set-state-in-effect,
  exhaustive-deps). Don't add new ones.
