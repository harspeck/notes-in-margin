# Code families

Code families are broad parent themes per RQ, synthesized from that RQ's filed memo entries.
They are owned by `backend/app/services/code_families.py` and served by
`backend/app/routers/code_families.py`. The cap and merges aren't in `docs/prd-phase-2.md`;
they come from resolving its granularity question (`docs/prototype-log.md`).

## Statuses

- **Families:** `suggested`, `accepted`, `dismissed`, `merged`. Only the first two are live:
  `LIVE_FAMILY_STATUSES = ("suggested", "accepted")`.
- **Members** must be filed connections in the same RQ group (the RQ plus every RQ merged
  into it), where filed means
  `FILED_STATUSES = ("accepted", "edited")`. This tuple is duplicated in `routers/memo.py`;
  change both together.
- The frontend's status unions are in `frontend/src/lib/types.ts`.

## Cap (`config.code_families` in `backend/config.yaml`)

| Key | Value | Meaning |
|---|---|---|
| `max_per_rq` | 5 | Maximum live families per RQ, unless the user overrides it |
| `near_cap_at` | 4 | At or above this many live families, synthesis also proposes merges |
| `max_new_per_run` | 2 | New families one run may create, even with `override_cap=true` |

- `cap_status(live_count, limits, override_cap)` computes what a run may do.
  `_apply_family_results` enforces it server-side, whatever the model returned.
- `GET /research_questions/{rq_id}/code_families/state` reports the cap.
  `test_code_families.py` asserts `max_per_rq: 5` from the real config, so changing the
  config value means updating that test.

## Synthesis and merges

- `synthesize_code_families` skips the model call when the RQ has nothing filed.
- `_apply_family_results` attaches members to existing families or creates new ones. Only
  valid connection ids are kept (`_valid_connection_ids`).
- `_apply_merge_suggestions` runs only near the cap. It needs 2 or more live family ids, and
  skips a set that already has a pending suggestion.
- `accept_merge` re-checks that every source family is still live (the router returns 409
  if not). The target is the accepted family if there is one, else the oldest
  (`_merge_target`). The source families move to `merged`; nothing is deleted.
- A user merge (`POST /code_families/merge`) and an accepted suggestion both go through
  `merge_families`. The surviving family keeps a star if any merged family had one. If the
  families came from different RQs of one merged group, the survivor moves to the group's
  target RQ, and undoing the RQ merge leaves it there (the undo response lists it).
- Assigning codes to individual highlights or snippets is out of scope
  (`docs/prd-phase-2.md`).

## Merged RQ groups

When an RQ is merged into another (`services/rq_merges.py`), its families keep their own
`research_question_id` but every read goes through `group_for(rq_id)`, which is
`rq_group_ids(resolve_rq_id(rq_id))`. So a merged group has one cap, synthesis sees all of
its entries and families, and new AI families are written to the target. For an unmerged
RQ the group is just that RQ, so behaviour is unchanged.

## User edits (Organize page)

- **Create** (`POST /research_questions/{rq_id}/code_families`): `status="accepted"`,
  `ai_metadata=None`, written to the group's target RQ. It is not blocked by the cap; the
  response carries the cap so the UI can warn. Every connection id must be a filed entry
  in the group, else 422.
- **Members** (`PUT /code_families/{id}/members`) and **rename** (`PATCH`) write an
  `rl_code_family_revision` snapshot first. Removed `code_family_member` rows are deleted,
  and the revision is their audit trail.
- **Star** (`POST /code_families/{id}/star`): starring a `suggested` family accepts it, so
  only user-filed families reach the codebook. `dismissed` or `merged` families return 409.
- **Codebook prep** (`GET /codebook_prep`): the focused RQs' starred live families, each
  with its filed member entries and their quoted evidence (`quoted_texts`), plus warnings
  (focus count outside 5-7, a focused RQ with no starred family, unreviewed suggested
  families). It is shaped after Cortico's `code` table (`name`, `definition`, a parent
  link) so the families can later become parent codes.
