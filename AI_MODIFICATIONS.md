# AI Modification Notice

This repository is an AI-modified redistribution of `basketikun/chatgpt2api`.
The changes below were made with AI assistance and should be reviewed before production use.

本仓库是 `basketikun/chatgpt2api` 的 AI 修改版。以下修改由 AI 辅助完成，生产使用前请自行审查。

## Upstream

- Original project: `basketikun/chatgpt2api`
- License: MIT, preserved in `LICENSE`
- This repository is not the official upstream project.

## AI-Modified Behavior

### GPT-image-2 backend mapping

The default image model remains publicly visible as:

- `gpt-image-2`

Default behavior, configurable from the Web settings page:

- `gpt-image-2` maps to the ChatGPT web backend slug `gpt-5-5`
- No `thinking_effort` parameter is sent for plain `gpt-image-2`

Hidden suffixes are accepted for backend routing but are not exposed through `/v1/models`:

- `gpt-image-2-low` -> `thinking_effort=min`
- `gpt-image-2-medium` -> `thinking_effort=standard`
- `gpt-image-2-high` -> `thinking_effort=extended`
- `gpt-image-2-xhigh` -> `thinking_effort=max`

When one of these hidden suffixes is used, the request is routed to:

- `gpt-5-5-thinking`

The ordinary and thinking backend slugs can be changed independently. For example, both can be set to `gpt-5.6-sol`; the hidden suffix still controls whether `thinking_effort` is added.

### Image quota and task queue

- Ordinary user keys can have a bounded image quota; `0` means unlimited.
- Image quota checks cover Images, Chat Completions image mode, Responses image generation, and Web image tasks.
- Synchronous and streaming requests use persisted request-scoped reservations, reserve the requested amount first, then atomically settle against the number of image items actually returned. Chat markdown images and Responses `image_generation_call` items are included in that count. Stream settlement retries transient storage failures; abandoned API reservations are recovered on restart or expire after one hour when the next request arrives.
- Background tasks use a task-derived SHA-256 reservation ID. Reservation, commit, refund, restart recovery, and timeout continuation are idempotent; storage failures reject the request instead of silently granting quota.
- Failed background tasks refund reserved quota, while resumable timeout tasks retain the reservation until the continuation finishes. Usage resets preserve in-flight reservations, and a finite limit cannot be lowered below the in-flight amount.
- Background image work uses 1-16 fixed workers and a bounded queue of 16-128 entries instead of creating one thread for every submitted task. A full or closed queue returns HTTP 503 and rolls back the task reservation.
- The JSON-backed task/quota implementation runs as one application process. A data-directory process lock rejects extra Uvicorn/Gunicorn workers or replicas before they can race the quota and task ledgers.
- Codex plan-specific image models can fall back to the generic Codex image model only for quota or rate-limit errors. The fallback does not cross into the ChatGPT Web image transport.

### Image size and fallback rules

- Web-backed `gpt-image-2` requests reject 2K/4K-class dimensions with a structured 400 error.
- Codex image models retain high-resolution support.
- Content-policy 429 responses do not trigger model fallback.
- The fallback decision and selected backend model are snapshotted into each queued task so later settings changes cannot alter an in-flight request.

The upstream Sub2API direct-database billing implementation was intentionally not imported because it persisted raw API keys and did not provide crash-safe, idempotent debit/refund semantics.

The following aliases are intentionally not supported:

- `gpt-image-2-min`
- `gpt-image-2-standard`
- `gpt-image-2-extended`
- `gpt-image-2-max`

### Public model list

`/v1/models` is intentionally kept clean:

- Public text aliases are limited to stable names such as `gpt-5.3`, `gpt-5.4`, `gpt-5.5`, `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-5-mini`, and `gpt-5-pro`
- ChatGPT Web slugs such as `gpt-5-6-thinking`, `gpt-5.6-luna-wm`, `gpt-5.6-terra-wm`, `gpt-5.6-sol-wm`, and version-specific mini/pro slugs are not listed
- `gpt-5.4`, `gpt-5.5`, `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol` accept hidden `-low`, `-medium`, `-high`, and `-xhigh` suffixes
- Text suffixes map to the real Web values `min`, `standard`, `extended`, and `max`; the suffix models are accepted by the backend but not listed
- `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol` remain separate public models and route to their matching `-wm` Web slugs
- The generic Web slug `gpt-5-6-thinking` and compatibility alias `gpt-5.6` are intentionally not listed
- Public model list also exposes `gpt-image-2` when backed by a Web account; image suffix models remain hidden
- Error messages avoid listing hidden suffix models as public supported models

### Model account filtering

The model list now only injects `gpt-image-2` when a non-Codex web account with an access token exists.
Codex-only accounts continue to expose Codex image models through their existing path.
Authenticated model discovery merges all eligible Web accounts and keeps a compatible token set for each backend model. Explicit text models rotate only among accounts that advertised that model, while `auto` continues to use the normal account pool.

### Repository publication changes

This redistribution also includes publication-oriented cleanup:

- Runtime examples and UI links point to this AI-modified repository instead of the upstream repository.
- `docker-compose.yml` builds from the local AI-modified source by default instead of pulling the upstream image.
- The tracked runtime configuration was converted from `config.json` to `config.example.json`; real `config.json` files remain local and ignored by Git.
- Online model-list tests are opt-in through `CHATGPT2API_INTEGRATION_TESTS=1`; offline tests cover the hidden GPT-image suffix behavior.

## Modified Files

- `utils/helper.py`
  - Added hidden GPT-image-2 suffix parsing.
  - Added thinking-effort mapping helpers.
  - Kept public image model constants separate from hidden accepted suffixes.

- `services/openai_backend_api.py`
  - Updated GPT-image-2 backend slug selection.
  - Sends `thinking_effort` only when a hidden suffix requests it.
  - Reads ordinary and thinking image backend slugs from live configuration.

- `services/protocol/text_model_aliases.py`, `services/protocol/openai_v1_models.py`, and `services/protocol/conversation.py`
  - Collapse versioned Web slugs into stable public aliases.
  - Keep raw Web slugs and hidden thinking suffixes out of `/v1/models`.
  - Route text suffixes to the Web model and its accepted thinking-effort value before account selection.

- `services/auth_service.py`, `services/image_task_service.py`, and `api/ai.py`
  - Add per-user image quota enforcement across every supported image protocol.
  - Add persisted idempotent reservations, actual-output settlement, bounded task queues, and resumable timeout accounting.

- `services/log_service.py`
  - Counts actual image results in Images, Chat Completions markdown, Responses output items, and streaming events before quota settlement.

- `services/process_lock.py`
  - Enforces the single-process data-directory invariant on Windows and Unix-like hosts.

- `services/protocol/conversation.py`
  - Adds quota-only fallback within the Codex image transport family.
  - Keeps 2K/4K-class dimensions on Codex image transports and rejects them for the Web transport.

- `web/src/app/settings/` and `web/src/app/image/page.tsx`
  - Add image backend model, task worker, fallback, and user quota controls.
  - Show remaining user image quota on the image page.

- `services/protocol/openai_v1_models.py`
  - Keeps `/v1/models` from exposing hidden suffix models.
  - Filters web image model injection to non-Codex access-token accounts.

- `README.md`, `docs/deployment.md`, `docker-compose.yml`, `Dockerfile`, and web GitHub/version-check links
  - Mark this repository as an AI-modified redistribution.
  - Point user-facing install/update links to this repository.
  - Avoid pulling the upstream image when running the AI-modified source.
  - Keep real runtime configuration out of the published repository.

- `test/test_account_image_capabilities.py` and `test/test_v1_models.py`
  - Add offline regression coverage for hidden GPT-image suffix parsing and backend slug selection.
  - Verify hidden suffixes are not exposed through `/v1/models`.

- `.github/workflows/docker-publish.yml`
  - Blocks image publication until frozen dependency installation, offline backend tests, TypeScript checking, and the production Web build pass.

- `Dockerfile`
  - Uses the tracked Bun lockfile with `bun install --frozen-lockfile`, matching the frontend dependency graph validated by CI.

## Validation Performed

The current source tree was checked with:

- Python syntax compilation for `api`, `services`, `utils`, and `test`.
- Frozen dev dependency installation with `uv sync --dev --frozen`.
- Offline backend suite: `172 passed, 2 skipped` (plus 8 passing subtests).
- Frontend TypeScript check with `tsc --noEmit`.
- Next.js production build for all application routes.
- GitHub Actions workflow YAML parsing.
- `/v1/models` check: only `gpt-image-2` is exposed for GPT-image models.
- Backend helper check:
  - `gpt-image-2` accepted with no thinking effort.
  - `gpt-image-2-low`, `gpt-image-2-medium`, `gpt-image-2-high`, `gpt-image-2-xhigh` accepted as hidden suffix models.
  - `gpt-image-2-min`, `gpt-image-2-standard`, `gpt-image-2-extended`, `gpt-image-2-max` rejected.
- Desktop and mobile Web settings screenshots after the production build.

## Security Notes

This repository must not include deployment secrets.

Do not commit:

- `data/`
- OAuth account exports or tokens
- production `config.json` values
- `.env`
- logs
- generated images
- server passwords or Cloudflare tokens

The published source is intended to contain code changes and documentation only.
