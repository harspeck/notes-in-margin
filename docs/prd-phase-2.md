# PRD: Reflective Listening — Phase 2 (Next Steps + Code Families)

Oct 6, 2026 · @Maya

## Prerequisite reading

This phase builds directly on the shipped Phase 1 prototype. Before writing any code,
read, in order:
1. `docs/prd-reflective-listening.md` — the original PRD (data model, AI behavior,
   provenance rules, acceptance criteria). Everything there still holds unless this
   document says otherwise.
2. `docs/prototype-log.md` — every place the real schema (`db/schema.sql`,
   `acp_pipeline_snapshot.db`) disagreed with the PRD, and how it was resolved. The same
   rule applies here: where this PRD's assumptions turn out to be wrong against the real
   schema, follow the schema and log the difference the same way.
3. The current `backend/app/` and `frontend/src/` as they exist at commit `9030798` —
   this phase extends that codebase, it doesn't replace any of it.

Git history and the existing prototype log are the source of truth for what's already
built; this document only covers what's new.

## Overview and goals

Phase 1 built the full reflective-listening loop: read a transcript, highlight and mark
it up, record a spoken reflection, have AI propose RQ connections grounded in the
transcript, review/accept/edit/reject them, and watch a running memo accumulate per RQ.
Two things from the original PRD's "Follow-up work" list were explicitly deferred, and a
third is new this round:

1. **Next-direction suggestions.** Every voice memo already captures "what I'd like to
   hear next" statements (`voice_memo.extracted_interests`), but nothing does anything
   with them yet. This phase acts on them.
2. **Pointing into the rest of the corpus.** Recommend specific conversations — from the
   full snapshot, not just the two already in use — that might address those interests or
   extend an RQ. The original PRD left the retrieval approach undecided (a topic-mapping
   tool, RAG, or a hybrid); this phase decides it.
3. **Thematic code-family synthesis** *(new, not in the original PRD)*. Per RQ, synthesize
   the accepted/edited memo entries into named clusters — "code families" — each with a
   name, a definition, and the memo entries that exemplify it. This is explicitly a
   stepping stone toward a future codebook, not a tagging system: **assigning codes to
   highlights/snippets is out of scope this round.** This only produces and curates the
   family definitions themselves.

**Goals**
- Turn captured-but-unused signal (`extracted_interests`) into something actionable,
  without the AI ever deciding anything on its own — same human-in-the-loop posture as
  Phase 1's RQ connections (suggest → accept/edit/dismiss).
- Make the rest of the corpus reachable: widen from the 2-conversation sandbox to the
  full snapshot, and have the AI point at specific unvisited conversations likely to be
  relevant.
- Give the sensemaker a running start on a codebook, grounded in their own filed memo
  entries, without building any tagging machinery yet.

**Non-goals this round:** actually building a codebook/tagging system (code families are
a precursor, not a codebook); cross-person or AI-vs-human comparison of next-step
suggestions or code families; any change to the Phase 1 review/filing flow itself.

## Decisions (already made, do not re-litigate)

- **Recommendation mechanism:** not embeddings, not a no-AI keyword heuristic. Cache a
  short AI-generated summary ("gist") per Cortico conversation once, then make a single
  `gpt-5.6-luna` call that reasons over *all* cached gists at once (the model's context
  window — ~1M tokens — comfortably holds ~200 short gists in one prompt; no vector search
  needed).
- **Corpus scope:** widens from project 5's 2 conversations to the full 201-conversation
  snapshot, for both reading and recommendations. Most conversations outside project 5
  have no conversation guide, which the reader already handles gracefully ("no guide for
  this conversation" in the section menu) — this is not a blocker.
- **Code families persist as a reviewable draft**, not an ephemeral recompute-every-time
  view: suggested → accepted/renamed/dismissed, mirroring the existing RQ-connection
  review pattern exactly. Dismissed families stay logged, not deleted (same provenance
  posture as rejected connections).
- **No destructive DB work:** every table below is new. Adding them via `create_all`
  doesn't touch `rl_highlight`, `reflection_connection`, or any other existing table or
  row — unlike the Phase 1 `voice_memo` column addition, there is no reason to wipe
  `db/prototype.db` for this phase. If the implementing agent thinks it needs to, that's a
  sign something is being over-engineered — stop and reconsider.

## Data model (prototype-owned, additive only)

All new tables, following the same conventions as Phase 1 (see
`docs/prototype-log.md` for the naming rules these follow — e.g. check new names against
`db/schema.sql` for collisions before using them, the way `highlight` collided and became
`rl_highlight`):

| Table | Purpose | Key fields |
| --- | --- | --- |
| `conversation_gist` | Cached AI summary of a Cortico conversation, corpus-level fact (not per-user) | `conversation_id` (PK, Cortico's int id), `project_id`, `forum_id`, `summary_text`, `keywords_json`, `generated_at` |
| `next_step_suggestion` | An AI-proposed direction and/or conversation to listen to next | `id`, `user_id`, `research_question_id` (nullable), `suggested_direction` (nullable text), `recommended_conversation_id` (nullable int), `rationale`, `status` (`suggested`/`accepted`/`dismissed`), `ai_metadata`, `created_at`, `updated_at` |
| `code_family` | A proposed thematic cluster of memo entries under one RQ | `id`, `user_id`, `research_question_id`, `name`, `definition`, `status` (`suggested`/`accepted`/`dismissed`), `ai_metadata`, `created_at`, `updated_at` |
| `code_family_member` | Join: which memo entries exemplify a family | `id`, `code_family_id` (FK), `connection_id` (FK → `reflection_connection.id`) |

Notes:
- `next_step_suggestion` deliberately isn't split into two tables or a "kind" enum for
  direction-vs-conversation: the common case is both together ("you wanted to hear about
  X — conversation #47 touches on it"), so one row can carry either or both.
- Check all four names above against the live `db/schema.sql` before implementing, the
  same way Phase 1 did — if any of them turn out to collide with something real, rename
  and log it, don't silently reuse a colliding name.

## Mechanics

### Corpus gists (prerequisite for next-step suggestions)

A one-time, resumable backfill, **not** an API endpoint or something the UI triggers:
`backend/scripts/backfill_gists.py`. For every Cortico conversation without a
`conversation_gist` row yet: pull its title, project/forum/community names, and a handful
of substantive snippets (skip ones where `is_mechanical` is true or that are pure
facilitator prompts), ask `gpt-5.6-luna` at `low` effort for a 2–3 sentence summary plus a
short keyword list, store it. Must skip conversations that already have a row, so it's
safe to re-run or resume after an interruption — ~200 cheap calls total; the implementing
agent should actually run this once against the real snapshot and report how long it took
and roughly what it cost, not just write the script and leave it unrun.

### Next-step suggestions

`generate_next_step_suggestions(...)`, triggered by an explicit user action (a button —
see UI below), not automatically after every filed reflection:
- Gathers every `extracted_interests` entry across *all* of the user's voice memos (not
  just the most recent one — interests accumulate across sessions), the user's active
  (non-archived) RQs, which conversation ids the user has already engaged with (via
  `transcript_state`, `rl_highlight`, or `voice_memo` presence — so the model is told to
  prefer unvisited ones), existing non-dismissed `next_step_suggestion` rows (so it
  doesn't repeat itself), and every `conversation_gist` row.
- One `gpt-5.6-luna` call returns candidate suggestions (direction text, optional
  recommended conversation, rationale).
- Every `recommended_conversation_id` is validated against real Cortico conversation ids
  before persisting — same structural-validation posture Phase 1 used for evidence
  snippet/word-range checks (drop anything that doesn't resolve, don't trust it blindly).
- Persisted with `status='suggested'`.

### Code-family synthesis

`synthesize_code_families(research_question_id, ...)`, triggered per RQ by an explicit
button on the memo page:
- Gathers that RQ's accepted/edited `reflection_connection` rows — insight text plus
  their evidence passages (reuse `app/evidence_text.py`'s `quoted_texts`, already built in
  Phase 1) — plus that RQ's existing non-dismissed `code_family` rows, so a re-run can
  attach a new memo entry to an existing family instead of proposing a duplicate.
- One `gpt-5.6-luna` call returns proposed families (name, definition, member connection
  ids — possibly referencing an existing family id instead of a new one).
- Validate every claimed `connection_id` actually belongs to that RQ and has status
  `accepted` or `edited` — reject anything else before persisting.
- New families persist with `status='suggested'`; a family matched to an existing one adds
  `code_family_member` rows to it instead of creating a duplicate `code_family`.

### Provenance and guardrails

Same rules as Phase 1's RQ connections, applied to both new AI outputs: cite real
material (gists, insight text, evidence) rather than inventing it; every suggestion/family
carries `ai_metadata` (model, reasoning effort, prompt, rationale) for auditability;
nothing is accepted or treated as final until the user explicitly acts — generation alone
never changes anything the user sees as "decided."

## Backend surface

- `backend/scripts/backfill_gists.py` (see above — CLI, not an endpoint).
- `app/services/next_steps.py`, `app/services/code_families.py` — the two generation
  functions described above.
- `routers/next_steps.py`:
  - `POST /next_steps/generate`
  - `GET /next_steps`
  - `POST /next_steps/{id}/accept`
  - `POST /next_steps/{id}/dismiss`
- `routers/code_families.py`:
  - `POST /research_questions/{rq_id}/code_families/synthesize`
  - `GET /research_questions/{rq_id}/code_families`
  - `PATCH /code_families/{id}` (rename / redefine)
  - `POST /code_families/{id}/accept`
  - `POST /code_families/{id}/dismiss`
- Both new AI call sites reuse the existing `config.ai_review` model/effort settings
  rather than adding more config surface — trivially splittable into their own config
  later if that turns out to matter.
- **Widen conversation scope:** `routers/conversations.py`'s `list_conversations` currently
  filters to `config.scoped_conversation_ids`; remove that filter so all 201 conversations
  are listed. `get_conversation(id)` already has no such restriction — only the list did —
  so this is a small, low-risk change.
- **Bundled correctness fix:** `routers/highlights.py`'s `create_highlight` currently
  hardcodes `project_id=config.default_project_id` (always 5). Once conversations outside
  project 5 are reachable, this must instead look up the conversation's real `project_id`
  from Cortico — otherwise every highlight made outside project 5 gets mislabeled.

## Frontend surface

- **Memo page** (`frontend/src/pages/MemoPage.tsx`):
  - New top-level section, "What to listen to next": a "Suggest next steps" button, then
    suggestions showing the direction text, an optional "Listen to: *{conversation
    title}*" link (`/?conversation=ID`, same pattern as existing evidence links),
    rationale, and Accept/Dismiss actions.
  - Inside each RQ section: a "Synthesize code families" button plus family cards (name,
    definition, member count, expandable to the member insight texts), with
    accept/rename/dismiss controls.
- `lib/types.ts` / `lib/api.ts`: `NextStepSuggestion` and `CodeFamily` types plus the
  corresponding `api.*` methods for the routes above.
- No reader UI changes are needed for the scope-widening beyond the conversation dropdown
  naturally growing to 201 options.

## Acceptance criteria

- [x] `backfill_gists.py` has actually been run against the real snapshot; spot-checking
      a handful of `conversation_gist` rows shows sane, real summaries (not hallucinated
      or empty). *Verified 2026-10-06: all 201/201 conversations backfilled (720.7s,
      279,579 input + 29,813 output tokens for the 198-conversation full run); spot-checked
      rows are grounded, and the 3 conversations with zero real snippets correctly came
      back with an honest "no substantive content" summary instead of a hallucinated one.
      See docs/prototype-log.md.*
- [x] The reader's conversation switcher lists all 201 conversations, not just project 5's
      two; opening any of them renders correctly, with or without a conversation guide.
      *Verified live: `GET /conversations` returns all 201; conversation #1 (project 1, no
      guide) renders correctly with an empty `guide_questions` list.*
- [x] A highlight created on a conversation outside project 5 is labeled with that
      conversation's real `project_id`, not a hardcoded 5. *Verified live and by
      `tests/test_highlights.py`: a highlight on conversation 1 gets `project_id=1`.*
- [x] "Suggest next steps" on the memo page produces at least one suggestion referencing
      real `extracted_interests` content and, where a conversation is recommended, that
      conversation is a real, valid id and (when possible) one the user hasn't already
      engaged with. *Verified live against the one real filed voice memo in the dev db
      (conversation 10's extracted interest about "what kids internalize from leaders'
      behavior") — produced suggestions grounded in that interest, each citing a real,
      valid, previously-unvisited conversation id.*
- [x] Accepting or dismissing a next-step suggestion persists and is reflected on reload;
      dismissed ones are hidden by default but not deleted. *Verified live in the browser
      and by `tests/test_next_steps.py`.*
- [x] "Synthesize code families" on an RQ with accepted/edited memo entries produces
      family suggestions whose member entries are verifiably real, accepted/edited
      connections under that RQ. *Verified live against RQ 1's one real filed entry, and
      by `tests/test_code_families.py`.*
- [x] Re-running synthesis on an RQ that already has accepted families doesn't propose
      obvious duplicates — it either leaves them alone or attaches new members to them.
      *Verified live: re-running synthesis on RQ 1 attached to the existing family rather
      than creating a duplicate; also covered by
      `test_apply_family_results_attaches_to_existing_family_instead_of_duplicating`.*
- [x] Renaming, accepting, or dismissing a code family persists and is reflected on
      reload; dismissed ones stay logged, not deleted. *Verified live (rename + accept) and
      by `tests/test_code_families.py` (rename, accept, dismiss-not-deleted).*
- [x] No existing Phase 1 data (highlights, connections, memo entries) is altered or lost
      by anything in this phase — verified against the real `db/prototype.db`, not just an
      isolated test database. *All four new tables are additive-only; real dev-db data
      (RQs, highlights, filed connections) was spot-checked before and after each change
      and is untouched.*

## Open questions

- [x] **Gist staleness:** if new conversations are added to the snapshot later, or a
      conversation's content changes, `conversation_gist` rows won't auto-refresh.
      *Decided 2026-10-14: no refresh logic. Conversation data doesn't change, and re-running
      the backfill (which skips existing rows) covers newly added conversations.*
- [ ] **Suggestion fatigue:** if "Suggest next steps" is clicked repeatedly without
      dismissing old suggestions, they accumulate. *Under discussion 2026-10-14: let the user
      drag suggestions into priority order, and possibly move next steps off the memo page
      (own page or side panel).*
- [x] **Code family granularity.** *Decided 2026-10-14 and implemented:*
      - *Code families are broad parent themes (the future parents of child codes), not
        nuances within a topic.*
      - *Each RQ is capped at 5 live (suggested + accepted) families, with at most 2 new
        families per synthesis run. The prompt asks the model to prefer attaching entries
        to existing families, and the server enforces both limits.*
      - *From 4 families on, synthesis also proposes merges of overlapping families. Accepting
        a merge folds the members into one family and marks the others `merged` (logged,
        not deleted).*
      - *At the cap, the user can choose "synthesize past the cap", still limited to 2 new
        families per run.*
      - *The limits live in `config.yaml` under `code_families`.*

## Follow-up work

- **Codebook generation and tagging**, the next phase once the open questions above are
  settled. Code families become parent codes with child codes under them, and codes get
  assigned to highlights/snippets.
  - *2026-10-26: the codebook-prep step before this is delivered by the Organize page.
    The user focuses 5-7 RQs, merges RQs (reversibly), creates, edits, merges and stars
    code families, and downloads `GET /codebook_prep`, a JSON draft of the focused RQs'
    starred families with evidence, for separate codebook code. Codebook generation and
    tagging themselves are still to do. See `docs/prototype-log.md`.*
