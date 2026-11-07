# Cortico integration

The backend reads Cortico data in one of two modes, chosen by `data_source` in
`backend/config.yaml`. The `DATA_SOURCE` environment variable overrides it.

| | `snapshot` (default, local) | `cortico_api` (deployed) |
|---|---|---|
| Source data | `acp_pipeline_snapshot.db`, read-only | Cortico v2 API at `https://api2.cortico.ai`, using the signed-in user's token |
| Who is calling | `config.default_user_id` (101), with no sign-in | The Cortico user from `/v2/me`, via a session |
| Ids in the sidecar | The snapshot's own row ids | Cortico's ids |
| Conversation list | Every conversation, or the active workspace's projects if one exists | The active workspace's projects (409 until one exists) |
| Highlight write-back | Off | On (`cortico.write_back.highlights`) |

A deployment uses exactly one mode. `rl_workspace.data_source` records which mode a
workspace belongs to. A snapshot-era database can't be pointed at the API as-is: its
conversation and snippet ids would first need remapping through the snapshot's `cortico_id`
columns.

## Pieces

- **`app/cortico/client.py`: the HTTP client.**
  - Retries 429, 5xx and network errors up to `cortico.max_retries` times. It honours
    `Retry-After`, capped at 10 s.
  - On a 401 it refreshes the token once and retries.
  - Pagination: `page_info.cursor` is passed back as `after` on organizations, projects
    and highlights, and as `cursor` everywhere else.
  - The auth routes (`/auth/login`, `/auth/refresh-tokens`, `/auth/logout`) sit at the
    root, not under `/v2`.
- **`app/cortico/errors.py`.** Every Cortico failure becomes a `CorticoError`. The handler
  in `main.py` turns it into JSON with `"source": "cortico"`:
  - 401 means sign in again;
  - 403 means a role is missing, and is never retried;
  - 404 means not found;
  - 422 means Cortico rejected the body;
  - 503 means Cortico is unavailable, with `Retry-After`.
- **`app/cortico/source.py`: the data source.** `SnapshotSource` and `ApiSource` both
  return `ConversationRef`, `SnippetRef` and `GuideQuestionRef`. Every Cortico read in the
  routers and services goes through one of them.
  - Services still take a `cortico_db` argument. `as_source()` accepts either a source or
    a snapshot `Session`, so older call sites and tests keep working.
  - Never query `models_cortico` from a router.
- **Read-through caches in the sidecar:**
  - `rl_cortico_conversation` holds the project id, because v2 conversations carry only
    `forum_id`.
  - `rl_cortico_snippet` holds words and the uuid. Evidence rows store only
    `(snippet_id, start, end)`, so quoting them needs words without a conversation in hand.
    Transcripts refresh after `cortico.snippet_cache_ttl_seconds`. Snippets that disappear
    upstream stay cached, so old evidence still quotes.
- **`app/auth.py`: sessions.**
  - `POST /auth/login` sends the user's email and password to Cortico once. Cortico has no
    OAuth flow for third-party apps. The password is never stored.
  - The session token goes in an httpOnly cookie, `nim_session`. Scripts can send it as
    `Authorization: Bearer`.
  - `rl_session` stores only the token's sha256 and the Cortico tokens, encrypted with a
    key derived from `SESSION_SECRET`.
  - Cortico rotates refresh tokens and revokes the old one. So the refresh runs under a row
    lock (`refresh_session`), and two parallel requests can't log the user out.
- **Workspaces (`routers/workspaces.py`).** A workspace is a named bundle of projects in
  one organization, stored as an integer id plus `project_ids_json`.
  - Projects come from `/v2/projects`, filtered to the user's `/v2/me` roles: a role on the
    project itself, or an organization-wide role.
  - The active workspace is per session (`rl_session.active_workspace_id`). Otherwise it is
    the most recently updated one.
- **Write-back (`app/cortico/write_back.py`).**
  - A highlight is saved locally first, then pushed with `POST /v2/highlights`. Each
    highlight gets one `rl_cortico_sync` row, with status `synced`, `failed` (retry with
    `POST /highlights/{id}/cortico_sync`), `skipped` (the shape can't be represented) or
    `deleted`.
  - Payload rules, mirrored from llm-auto-highlighter's `corticoPayload.js`:
    - one continuous passage;
    - every snippet after the first starts at word 0, every snippet before the last ends
      at its last word, and only empty snippets may sit between them;
    - `word_indices` are **inclusive**. This app's spans are half-open, so
      `end_index = end - 1`.
  - Deleting a local highlight deletes only the Cortico copy this app created. Title,
    notes and star stay local, because Cortico's highlight update takes only title and
    metadata.

## Not yet done

- **Voice memo audio on Vercel.** The filesystem is read-only apart from `/tmp`, and
  faster-whisper isn't in the deployed requirements. Hosted voice memos need audio in
  Vercel Blob and hosted transcription, for example OpenAI.
- **Gists in `cortico_api` mode.** `scripts/backfill_gists.py` still reads the snapshot.
  In API mode, next-step suggestions see only gists for conversations the user's
  workspace has listed.
- **Migrations.** `create_all` adds missing tables only. The first column change on
  Postgres should bring in Alembic.
- **Live verification.** The tests run against a fake Cortico (`tests/test_cortico_api_mode.py`).
  `scripts/cortico_smoke.py` checks the real API with a real account.
