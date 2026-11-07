# Tests

Run them with `cd backend && .venv/bin/python -m pytest -q` (80 tests, about 1.3 s). They
need no network, API key or running server.

## Environment (`backend/tests/conftest.py`)

- **Prototype database:** conftest sets `PROTOTYPE_DB_PATH` to a temp file before importing
  `app` (with `os.environ.setdefault`), and runs `create_all` once per session. Never export
  `PROTOTYPE_DB_PATH` to the real `db/prototype.db`, because the tests would then write into
  it.
- **Snapshot:** tests use the real Cortico snapshot when `acp_pipeline_snapshot.db` is at
  the repo root (symlink it into a worktree), or whatever `CORTICO_DB_PATH` names (an
  absolute path). Otherwise conftest builds a synthetic stand-in with
  `fixtures/build_snapshot_fixture.py` into a temp file and sets `CORTICO_DB_PATH` before
  importing `app`. The builder executes `db/schema.sql` into that new file only, refuses
  to overwrite an existing file, and is deterministic. Its text is invented, every
  `cortico_id` is the local id plus 100000, and it mirrors the quirks the code depends on
  (no `conversation_guide_id` or `audio_url` set, conversations 160, 162 and 201 with no
  snippets, guide `order_index` restarting per section). A new test that asserts on other
  real data must add that data to the builder. Tests assert on:
  - 201 conversations
  - conversations 1, 9 and 10 (1 in project 1, which has no guide; 9 and 10 in project 5)
  - conversations 12 to 16, each with at least three snippets of 20 or more words
  - snippets 1936 and 1938 (in conversation 9)
  - guide question 4
  - the guide codes "Consent", "Guidelines" and "Q1 Intros"
- **No lifespan:** routers are exercised with `TestClient(app)` outside a `with` block, so
  the lifespan (`create_all` and the RQ seed) does not run.
- **No model:** no test calls OpenAI or loads Whisper. Logic that runs after an LLM call is
  tested by feeding hand-built model output to the helpers. A live round trip is checked by
  hand, and only with the user's go-ahead, because it costs money.

## Files

| File | Covers |
|---|---|
| `test_ai_review_validation.py` | `_validate_evidence_in_range` against real snippets |
| `test_evidence_text.py` | `quoted_texts` |
| `test_evidence_provenance.py` | Overlap matching, AI linking and ordering, starred coverage, assignment in review, a foreign highlight refused, a memo edit keeping its link |
| `test_guide.py` | Guide fallback and question order |
| `test_highlights.py` | The full corpus is listed; a highlight's `project_id` comes from Cortico; 404s |
| `test_reflection_timestamp.py` | `reflection_timestamp_from_words` (a pure function) |
| `test_review_filing.py` | `file_to_memo` is the single write point; rejected rows are kept; checkpoint upsert |
| `test_next_steps.py` | Id validation, interest accumulation, engaged ids, accept and dismiss |
| `test_export_md.py` | The backup script: export contents, previous export moved aside, database copy complete, backups never overwrite each other |
| `test_code_families.py` | Validation, `_apply_family_results`, the cap, merges, `accept_merge`, 409 on a stale merge, the state endpoint |
| `test_rq_merges.py` | RQ merge and undo, refused merges (self, archived, merged, cycles), a cyclic merge row not hanging the resolver, memo bucketing and reorder across a merged group, the cap and valid entries spanning the group, families kept with the target on undo |
| `test_code_family_edits.py` | Create (accepted, uncapped, 422 on entries outside the group), replacing members and renaming write a revision first, star accepts a suggested family and 409s on dismissed or merged, user merge and its validation, `accept_merge` going through `merge_families` |
| `test_organize.py` | Focus toggle and 409 on a merged RQ, `/organize` folding merged sources into their target, the `/codebook_prep` shape, filters and warnings |

## Which test to copy

- **A router test:** copy `test_review_filing.py`. It seeds rows with `PrototypeSession`,
  then calls through `TestClient(app)`. `test_highlights.py` is the smallest example.
- **Logic after an LLM call:** copy `test_code_families.py`. It builds the raw model output
  by hand, including invalid ids, and passes it to the `_apply_*` helpers.
- **Checks against Cortico data:** copy `test_ai_review_validation.py`.
- `/memo`, `/organize` and `/codebook_prep` filter by `config.default_user_id` (101), and every test file shares one database, so tests of those routes create their RQs for user 101 and look rows up by id rather than counting everything.
- `test_code_families.py` asserts `max_per_rq: 5` from the real `config.yaml`. If you change
  that config value, update the test.
