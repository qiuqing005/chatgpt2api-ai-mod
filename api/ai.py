from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.auth_service import (
    ImageQuotaExceeded,
    ImageQuotaStorageError,
    auth_service,
    create_api_image_reservation_id,
)
from services.content_filter import check_request, request_shape, request_text
from services.config import config
from services.editable_file_task_service import editable_file_task_service
from services.log_service import LoggedCall, count_response_image_items
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
    openai_search,
)
from services.openai_backend_api import resolve_image_backend_route
from utils.helper import has_response_image_generation_tool, is_image_chat_request, parse_image_count
from utils.log import logger


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


class SearchRequest(BaseModel):
    prompt: str = Field(..., min_length=1)


class EditableFileTaskRequest(BaseModel):
    prompt: str = ""
    base64_images: list[str] = Field(default_factory=list)
    client_task_id: str | None = None


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    async def run_image_call_with_quota(
        identity: dict[str, object],
        amount: int,
        call: LoggedCall,
        handler,
        payload: dict[str, object],
    ):
        quota_reserved = False
        quota_settled = False
        reservation_id = create_api_image_reservation_id()

        def settle_image_quota(success_count: int, *, suppress_errors: bool = False) -> None:
            nonlocal quota_settled
            if not quota_reserved or quota_settled:
                return
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    settled = auth_service.settle_image_quota(
                        str(identity.get("id") or ""),
                        max(0, int(success_count)),
                        reservation_id=reservation_id,
                    )
                    if not settled:
                        raise ImageQuotaStorageError("图片额度预留记录不存在，无法完成结算")
                    quota_settled = True
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
            if suppress_errors:
                logger.error({
                    "event": "image_quota_stream_settlement_failed",
                    "reservation_id": reservation_id,
                    "error": str(last_error or "")[:300],
                })
                return
            raise last_error or ImageQuotaStorageError("图片额度结算失败")

        try:
            backend_model, thinking_effort = resolve_image_backend_route(str(payload.get("model") or "gpt-image-2"))
            payload["_image_backend_model"] = backend_model
            payload["_image_thinking_effort"] = thinking_effort
            payload["_image_fallback_enabled"] = config.image_model_fallback_enabled
            quota_reserved = auth_service.reserve_image_quota(
                identity,
                amount,
                reservation_id=reservation_id,
            )
            result = await call.run(
                handler,
                payload,
                stream_finalizer=lambda success_count, _completed: settle_image_quota(
                    success_count,
                    suppress_errors=True,
                ),
            )
            if getattr(result, "status_code", 200) >= 400:
                settle_image_quota(0)
            elif isinstance(result, dict):
                settle_image_quota(count_response_image_items(result))
            return result
        except ImageQuotaExceeded as exc:
            raise HTTPException(status_code=429, detail={"error": str(exc)}) from exc
        except Exception:
            if quota_reserved and not quota_settled:
                auth_service.refund_image_quota(
                    str(identity.get("id") or ""),
                    amount,
                    reservation_id=reservation_id,
                )
            raise

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await run_image_call_with_quota(
            identity,
            body.n,
            call,
            openai_v1_image_generations.handle,
            payload,
        )

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图", request_text=prompt)
        await filter_or_log(call, prompt)
        payload["images"] = await read_image_sources(image_sources)
        if mask_sources:
            payload["mask"] = await read_image_sources(mask_sources)
        payload["base_url"] = resolve_image_base_url(request)
        return await run_image_call_with_quota(
            identity,
            int(payload.get("n") or 1),
            call,
            openai_v1_image_edit.handle,
            payload,
        )

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        call = LoggedCall(
            identity,
            "/v1/chat/completions",
            model,
            "文本生成",
            request_text=request_preview,
            request_shape=request_shape(payload.get("messages")),
        )
        await filter_or_log(call, request_preview)
        if is_image_chat_request(payload):
            return await run_image_call_with_quota(
                identity,
                parse_image_count(payload.get("n")),
                call,
                openai_v1_chat_complete.handle,
                payload,
            )
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        call = LoggedCall(
            identity,
            "/v1/responses",
            model,
            "Responses",
            request_text=request_preview,
            request_shape=request_shape(payload.get("input")),
        )
        await filter_or_log(call, request_preview)
        if has_response_image_generation_tool(payload):
            return await run_image_call_with_quota(
                identity,
                parse_image_count(payload.get("n")),
                call,
                openai_v1_response.handle,
                payload,
            )
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("system"), payload.get("messages"), payload.get("tools"))
        call = LoggedCall(identity, "/v1/messages", model, "Messages", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    @router.post("/v1/search")
    async def search(body: SearchRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        call = LoggedCall(identity, "/v1/search", openai_search.MODEL, "搜索", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_search.handle, body.model_dump(mode="python"))

    @router.get("/v1/editable-file-tasks")
    async def list_editable_file_tasks(ids: str = "", authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        task_ids = [item.strip() for item in ids.split(",") if item.strip()]
        return await run_in_threadpool(editable_file_task_service.list_tasks, identity, task_ids)

    @router.get("/files/{file_path:path}")
    async def download_editable_file(file_path: str):
        try:
            path = await run_in_threadpool(editable_file_task_service.public_file_path, file_path)
        except Exception as exc:
            raise HTTPException(status_code=404, detail={"error": "file not found"}) from exc
        return FileResponse(path, filename=path.name)

    @router.post("/v1/ppt/generations")
    async def create_ppt_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/ppt/generations", "gpt-5-5-thinking", "PPT生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_ppt,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.post("/v1/psd/generations")
    async def create_psd_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/psd/generations", "gpt-5-5-thinking", "PSD生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_psd,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    return router
