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

Default behavior:

- `gpt-image-2` maps to the ChatGPT web backend slug `gpt-5-5`
- No `thinking_effort` parameter is sent for plain `gpt-image-2`

Hidden suffixes are accepted for backend routing but are not exposed through `/v1/models`:

- `gpt-image-2-low` -> `thinking_effort=min`
- `gpt-image-2-medium` -> `thinking_effort=standard`
- `gpt-image-2-high` -> `thinking_effort=extended`
- `gpt-image-2-xhigh` -> `thinking_effort=max`

When one of these hidden suffixes is used, the request is routed to:

- `gpt-5-5-thinking`

The following aliases are intentionally not supported:

- `gpt-image-2-min`
- `gpt-image-2-standard`
- `gpt-image-2-extended`
- `gpt-image-2-max`

### Public model list

`/v1/models` is intentionally kept clean:

- Public model list exposes `gpt-image-2`
- Hidden suffix models are accepted by the backend but not listed
- Error messages also avoid listing hidden suffix models as public supported models

### Model account filtering

The model list now only injects `gpt-image-2` when a non-Codex web account with an access token exists.
Codex-only accounts continue to expose Codex image models through their existing path.

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

## Validation Performed

The deployed AI-modified version was checked with:

- Python syntax compilation for modified files.
- Offline unit tests for account image capabilities and model listing.
- `/v1/models` check: only `gpt-image-2` is exposed for GPT-image models.
- Backend helper check:
  - `gpt-image-2` accepted with no thinking effort.
  - `gpt-image-2-low`, `gpt-image-2-medium`, `gpt-image-2-high`, `gpt-image-2-xhigh` accepted as hidden suffix models.
  - `gpt-image-2-min`, `gpt-image-2-standard`, `gpt-image-2-extended`, `gpt-image-2-max` rejected.
- Runtime health check on the deployed service.
- Real image-generation requests for default and thinking-suffix variants during deployment testing.

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
