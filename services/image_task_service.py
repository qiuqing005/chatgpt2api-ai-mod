from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from services.account_service import account_service
from services.auth_service import auth_service
from services.config import DATA_DIR, config
from services.content_filter import request_text
from services.log_service import LOG_TYPE_CALL, log_service
from services.openai_backend_api import resolve_image_backend_route
from services.protocol import openai_v1_image_edit, openai_v1_image_generations
from utils.log import logger

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}
MAX_CLIENT_TASK_ID_LENGTH = 128


class ImageTaskQueueUnavailable(RuntimeError):
    pass


class ImageTaskStorageError(RuntimeError):
    pass


class DaemonWorkerPool:
    def __init__(self, max_workers: int, *, name_prefix: str) -> None:
        max_queue_size = max(16, min(128, max_workers * 8))
        self._queue: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.Queue(
            maxsize=max_queue_size
        )
        self._threads: list[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._closed = False
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._run,
                name=f"{name_prefix}-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._threads.append(worker)

    def submit(self, handler: Callable[..., Any], *args: Any) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("image task worker pool is closed")
            self._queue.put_nowait((handler, args))

    def close(self, *, wait: bool = False) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if wait:
            self._queue.join()
        for _ in self._threads:
            try:
                self._queue.put_nowait((None, ()))  # type: ignore[arg-type]
            except queue.Full:
                break
        if wait:
            for worker in self._threads:
                worker.join(timeout=1)

    def _run(self) -> None:
        while True:
            handler, args = self._queue.get()
            try:
                if handler is None:
                    return
                handler(*args)
            except Exception as exc:
                logger.error({"event": "image_task_worker_crash", "error": str(exc)[:300]})
            finally:
                self._queue.task_done()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _quota_reservation_id(task_key: str) -> str:
    return hashlib.sha256(task_key.encode("utf-8")).hexdigest()


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "size": task.get("size"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("conversation_id"):
        item["conversation_id"] = task.get("conversation_id")
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("usage") is not None:
        item["usage"] = task.get("usage")
    if task.get("error"):
        item["error"] = task.get("error")
    if task.get("progress"):
        item["progress"] = task.get("progress")
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    if task.get("status") in (TASK_STATUS_RUNNING, TASK_STATUS_QUEUED):
        if task.get("status") == TASK_STATUS_RUNNING:
            # RUNNING 状态仅在 started_ts 被设置后（image_stream_resolve_start）才计时
            base_ts = task.get("started_ts")
        else:
            # QUEUED 状态从 created_ts 开始计时（排队等待中）
            base_ts = task.get("created_ts") or task.get("updated_ts")
        if base_ts:
            item["elapsed_secs"] = round(time.time() - base_ts, 1)
    return item


class ImageTaskService:
    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_generations.handle,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        retention_days_getter: Callable[[], int] | None = None,
        quota_service: Any = auth_service,
        task_workers_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self.quota_service = quota_service
        workers_getter = task_workers_getter or (lambda: config.image_task_workers)
        try:
            worker_count = max(1, min(16, int(workers_getter())))
        except Exception:
            worker_count = 2
        self._executor = DaemonWorkerPool(worker_count, name_prefix="image-task")
        self._lock = threading.RLock()
        self._quota_refund_lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._tasks = self._load_locked()
            changed = self._recover_unfinished_locked()
            changed = self._cleanup_locked() or changed
            if changed:
                self._save_locked()

    def close(self, *, wait: bool = False) -> None:
        self._executor.close(wait=wait)

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
    ) -> dict[str, Any]:
        backend_model, thinking_effort = resolve_image_backend_route(model)
        fallback_enabled = config.image_model_fallback_enabled
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
            "_image_backend_model": backend_model,
            "_image_thinking_effort": thinking_effort,
            "_image_fallback_enabled": fallback_enabled,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
    ) -> dict[str, Any]:
        backend_model, thinking_effort = resolve_image_backend_route(model)
        fallback_enabled = config.image_model_fallback_enabled
        payload = {
            "prompt": prompt,
            "images": images or [],
            "mask": masks or [],
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
            "_image_backend_model": backend_model,
            "_image_thinking_effort": thinking_effort,
            "_image_fallback_enabled": fallback_enabled,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [
                    _public_task(task)
                    for task in self._tasks.values()
                    if task.get("owner_id") == owner
                ]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        mode: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        if len(task_id) > MAX_CLIENT_TASK_ID_LENGTH:
            raise ValueError(f"client_task_id must not exceed {MAX_CLIENT_TASK_ID_LENGTH} characters")
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        reservation_id = _quota_reservation_id(key)
        now = _now_iso()
        should_start = False
        with self._lock:
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), "gpt-image-2"),
                "size": _clean(payload.get("size")),
                "quality": _clean(payload.get("quality"), "auto"),
                "resolved_backend_model": _clean(payload.get("_image_backend_model")),
                "resolved_thinking_effort": _clean(payload.get("_image_thinking_effort")),
                "resolved_fallback_enabled": bool(payload.get("_image_fallback_enabled")),
                "created_at": now,
                "updated_at": now,
                "created_ts": time.time(),
                "quota_reserved": False,
                "quota_reservation_tracked": True,
                "quota_reservation_id": reservation_id,
            }
            quota_reserved = False
            try:
                self._tasks[key] = task
                self._save_locked()

                quota_reserved = self.quota_service.reserve_image_quota(
                    identity,
                    1,
                    reservation_id=reservation_id,
                )
                if quota_reserved:
                    task["quota_reserved"] = True
                    self._save_locked()
            except Exception:
                self._tasks.pop(key, None)
                if quota_reserved:
                    self.quota_service.refund_image_quota(owner, 1, reservation_id=reservation_id)
                self._save_locked()
                raise
            should_start = True

        if should_start:
            try:
                self._executor.submit(
                    self._run_task,
                    key,
                    mode,
                    payload,
                    dict(identity),
                    _clean(payload.get("model"), "gpt-image-2"),
                )
            except Exception as exc:
                self._refund_task_quota(key)
                with self._lock:
                    self._tasks.pop(key, None)
                    self._save_locked()
                raise ImageTaskQueueUnavailable("image task queue is unavailable") from exc
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
    ) -> None:
        started = time.time()
        self._update_task(key, status=TASK_STATUS_RUNNING, error="")
        # 创建进度回调，每个步骤完成后更新任务状态
        def progress_callback(step: str) -> None:
            if step == "image_stream_resolve_start":
                self._update_task(key, started_ts=time.time())
            self._update_task(key, progress=step)
        # 将进度回调添加到 payload 中（handler 会提取并传递给 ConversationRequest）
        payload_with_progress = {**payload, "progress_callback": progress_callback}
        try:
            handler = self.edit_handler if mode == "edit" else self.generation_handler
            result = handler(payload_with_progress)
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            account_email = _clean(result.get("_account_email") or result.get("account_email"))
            if not isinstance(data, list) or not data:
                upstream = _clean(result.get("message"))
                if upstream:
                    message = upstream
                else:
                    message = "号池中没有可用账号或所有账号均被限流，请检查号池状态（账号额度、是否被封禁、是否到达生图上限）"
                error = RuntimeError(message)
                if account_email:
                    setattr(error, "account_email", account_email)
                raise error
            usage = result.get("usage")
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(
                key,
                status=TASK_STATUS_SUCCESS,
                data=data,
                usage=usage,
                error="",
                duration_ms=duration_ms,
                **({"account_email": account_email} if account_email else {}),
            )
            try:
                self._commit_task_quota(key)
            except Exception as exc:
                logger.error({"event": "image_task_quota_commit_failed", "error": str(exc)[:300]})
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成",
                request_preview=request_text(payload.get("prompt")),
                urls=_collect_image_urls(data),
                account_email=account_email,
            )
        except Exception as exc:
            error_message = str(exc) or "image task failed"
            account_email = _clean(getattr(exc, "account_email", ""))
            conversation_id = _clean(getattr(exc, "conversation_id", ""))
            duration_ms = int((time.time() - started) * 1000)
            if not self._is_resumable_timeout(error_message, conversation_id):
                self._refund_task_quota(key)
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_message, data=[],
                              duration_ms=duration_ms,
                              **({"account_email": account_email} if account_email else {}),
                              **({"conversation_id": conversation_id} if conversation_id else {}))
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败",
                request_preview=request_text(payload.get("prompt")),
                status="failed",
                error=error_message,
                account_email=account_email,
            )

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        request_preview: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
        account_email: str = "",
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if error:
            detail["error"] = error
        if account_email:
            detail["account_email"] = account_email
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        try:
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            task.update(updates)
            task["updated_at"] = _now_iso()
            task["updated_ts"] = time.time()
            self._save_locked()

    @staticmethod
    def _is_resumable_timeout(error: str, conversation_id: str) -> bool:
        text = str(error or "").lower()
        return bool(
            conversation_id
            and any(marker in text for marker in ("超时", "timeout", "timed out", "仍未找到图片结果"))
        )

    def _refund_task_quota(self, key: str) -> bool:
        with self._quota_refund_lock:
            with self._lock:
                task = self._tasks.get(key)
                if not task or not bool(task.get("quota_reserved")):
                    return False
                owner_id = _clean(task.get("owner_id"))
                tracked = bool(task.get("quota_reservation_tracked"))
                reservation_id = _clean(task.get("quota_reservation_id")) or _quota_reservation_id(key)
            if tracked:
                self.quota_service.refund_image_quota(owner_id, 1, reservation_id=reservation_id)
            else:
                self.quota_service.refund_image_quota(owner_id, 1)
            with self._lock:
                task = self._tasks.get(key)
                if not task or not bool(task.get("quota_reserved")):
                    return False
                task["quota_reserved"] = False
                task["updated_at"] = _now_iso()
                task["updated_ts"] = time.time()
                self._save_locked()
            return True

    def _commit_task_quota(self, key: str) -> bool:
        with self._quota_refund_lock:
            with self._lock:
                task = self._tasks.get(key)
                if not task or not bool(task.get("quota_reserved")):
                    return False
                owner_id = _clean(task.get("owner_id"))
                tracked = bool(task.get("quota_reservation_tracked"))
                reservation_id = _clean(task.get("quota_reservation_id")) or _quota_reservation_id(key)
            if tracked:
                self.quota_service.commit_image_quota(owner_id, reservation_id=reservation_id)
            with self._lock:
                task = self._tasks.get(key)
                if not task or not bool(task.get("quota_reserved")):
                    return False
                task["quota_reserved"] = False
                task["updated_at"] = _now_iso()
                task["updated_ts"] = time.time()
                self._save_locked()
            return True

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ImageTaskStorageError(f"图片任务账本无法读取：{self.path}") from exc
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            raise ImageTaskStorageError(f"图片任务账本格式无效：{self.path}")
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                raise ImageTaskStorageError(f"图片任务账本包含无效记录：{self.path}")
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                raise ImageTaskStorageError(f"图片任务账本包含缺少标识的记录：{self.path}")
            status = _clean(item.get("status"))
            if status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}:
                status = TASK_STATUS_ERROR
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": status,
                "mode": "edit" if item.get("mode") == "edit" else "generate",
                "model": _clean(item.get("model"), "gpt-image-2"),
                "size": _clean(item.get("size")),
                "quality": _clean(item.get("quality"), "auto"),
                "created_at": _clean(item.get("created_at"), _now_iso()),
                "updated_at": _clean(item.get("updated_at"), _clean(item.get("created_at"), _now_iso())),
                "created_ts": item.get("created_ts"),
                "updated_ts": item.get("updated_ts"),
                "started_ts": item.get("started_ts"),
                "duration_ms": item.get("duration_ms"),
                "quota_reserved": bool(item.get("quota_reserved", False)),
                "quota_reservation_tracked": bool(item.get("quota_reservation_tracked", False)),
                "quota_reservation_id": _clean(item.get("quota_reservation_id")) or _quota_reservation_id(
                    _task_key(owner, task_id)
                ),
                "resolved_backend_model": _clean(item.get("resolved_backend_model")),
                "resolved_thinking_effort": _clean(item.get("resolved_thinking_effort")),
                "resolved_fallback_enabled": bool(item.get("resolved_fallback_enabled", False)),
                "account_email": _clean(item.get("account_email")),
            }
            conversation_id = _clean(item.get("conversation_id"))
            if conversation_id:
                task["conversation_id"] = conversation_id
            data = item.get("data")
            if isinstance(data, list):
                task["data"] = data
            usage = item.get("usage")
            if isinstance(usage, dict):
                task["usage"] = usage
            error = _clean(item.get("error"))
            if error:
                task["error"] = error
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps({"tasks": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for key, task in self._tasks.items():
            if task.get("status") in UNFINISHED_STATUSES:
                if task.get("quota_reserved") or task.get("quota_reservation_tracked"):
                    try:
                        if task.get("quota_reservation_tracked"):
                            self.quota_service.refund_image_quota(
                                _clean(task.get("owner_id")),
                                1,
                                reservation_id=_clean(task.get("quota_reservation_id")) or _quota_reservation_id(key),
                            )
                        else:
                            self.quota_service.refund_image_quota(_clean(task.get("owner_id")), 1)
                        task["quota_reserved"] = False
                    except Exception as exc:
                        logger.error({"event": "image_task_recovery_refund_failed", "error": str(exc)[:300]})
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "服务已重启，未完成的图片任务已中断"
                task["updated_at"] = _now_iso()
                changed = True
            elif task.get("status") == TASK_STATUS_SUCCESS and task.get("quota_reserved"):
                try:
                    if task.get("quota_reservation_tracked"):
                        self.quota_service.commit_image_quota(
                            _clean(task.get("owner_id")),
                            reservation_id=_clean(task.get("quota_reservation_id")) or _quota_reservation_id(key),
                        )
                    task["quota_reserved"] = False
                    changed = True
                except Exception as exc:
                    logger.error({"event": "image_task_recovery_commit_failed", "error": str(exc)[:300]})
            elif (
                task.get("status") == TASK_STATUS_ERROR
                and task.get("quota_reserved")
                and not self._is_resumable_timeout(_clean(task.get("error")), _clean(task.get("conversation_id")))
            ):
                try:
                    if task.get("quota_reservation_tracked"):
                        self.quota_service.refund_image_quota(
                            _clean(task.get("owner_id")),
                            1,
                            reservation_id=_clean(task.get("quota_reservation_id")) or _quota_reservation_id(key),
                        )
                    else:
                        self.quota_service.refund_image_quota(_clean(task.get("owner_id")), 1)
                    task["quota_reserved"] = False
                    changed = True
                except Exception as exc:
                    logger.error({"event": "image_task_recovery_refund_failed", "error": str(exc)[:300]})
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        removed_keys = [
            key
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES and _timestamp(task.get("updated_at")) < cutoff
        ]
        for key in removed_keys:
            task = self._tasks.get(key)
            if task and task.get("quota_reserved"):
                try:
                    if task.get("status") == TASK_STATUS_SUCCESS:
                        if task.get("quota_reservation_tracked"):
                            self.quota_service.commit_image_quota(
                                _clean(task.get("owner_id")),
                                reservation_id=_clean(task.get("quota_reservation_id")) or _quota_reservation_id(key),
                            )
                    elif task.get("quota_reservation_tracked"):
                        self.quota_service.refund_image_quota(
                            _clean(task.get("owner_id")),
                            1,
                            reservation_id=_clean(task.get("quota_reservation_id")) or _quota_reservation_id(key),
                        )
                    else:
                        self.quota_service.refund_image_quota(_clean(task.get("owner_id")), 1)
                except Exception as exc:
                    logger.error({"event": "image_task_cleanup_refund_failed", "error": str(exc)[:300]})
                    continue
            self._tasks.pop(key, None)
        return bool(removed_keys)

    def resume_poll(
        self,
        identity: dict[str, object],
        task_id: str,
        extra_timeout_secs: float = 30.0,
    ) -> dict[str, Any]:
        """恢复对已超时任务的轮询，额外等待 extra_timeout_secs 秒。"""
        owner = _owner_id(identity)
        key = _task_key(owner, _clean(task_id))
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                raise ValueError("task not found")
            if task.get("status") != TASK_STATUS_ERROR:
                raise ValueError("task is not in error state")
            error_msg = _clean(task.get("error"))
            if not self._is_resumable_timeout(error_msg, _clean(task.get("conversation_id"))):
                raise ValueError("task error is not a timeout error")
            conversation_id = _clean(task.get("conversation_id"))
            if not conversation_id:
                raise ValueError("task has no conversation_id")
            mode = task.get("mode", "generate")
            model = task.get("model", "gpt-image-2")
            # 将任务状态重置为 running
            self._update_task(key, status=TASK_STATUS_RUNNING, error="")

        try:
            self._executor.submit(
                self._run_resume_poll,
                key,
                conversation_id,
                extra_timeout_secs,
                dict(identity),
                mode,
                model,
            )
        except Exception as exc:
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_msg)
            raise ImageTaskQueueUnavailable("image task queue is unavailable") from exc
        return _public_task(task)

    def _run_resume_poll(
        self,
        key: str,
        conversation_id: str,
        extra_timeout_secs: float,
        identity: dict[str, object],
        mode: str,
        model: str,
    ) -> None:
        """后台线程：继续轮询已有 conversation_id 的图片结果。"""
        started = time.time()
        backend = None
        try:
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol.conversation import format_image_result

            with self._lock:
                task = self._tasks.get(key)
                account_email = _clean(task.get("account_email")) if task else ""
            account = next(
                (
                    item
                    for item in account_service.list_accounts()
                    if _clean(item.get("email")).lower() == account_email.lower() and _clean(item.get("access_token"))
                ),
                None,
            )
            if not account:
                raise RuntimeError("无法找到原图片任务使用的账号，不能继续等待")
            backend = OpenAIBackendAPI(access_token=_clean(account.get("access_token")))
            file_ids, sediment_ids = backend._poll_image_results(
                conversation_id,
                extra_timeout_secs,
            )
            if not file_ids and not sediment_ids:
                raise RuntimeError(
                    f"继续等待 {extra_timeout_secs} 秒后仍未找到图片结果。"
                )

            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if not image_urls:
                raise RuntimeError("图片 URL 解析失败")

            image_items = [
                {"b64_json": __import__("base64").b64encode(image_data).decode("ascii")}
                for image_data in backend.download_image_bytes(image_urls)
            ]
            # 获取 task 的原始 prompt（从 _public_task 的 mode 判断）
            with self._lock:
                task = self._tasks.get(key)
                quality = _clean(task.get("quality"), "auto") if task else "auto"
                size = _clean(task.get("size")) if task else None
            data = format_image_result(
                image_items,
                "",  # prompt 已不重要，结果已经拿到了
                "b64_json",
                "",
                int(time.time()),
            )["data"]
            self._update_task(
                key,
                status=TASK_STATUS_SUCCESS,
                data=data,
                error="",
                duration_ms=int((time.time() - started) * 1000),
            )
            try:
                self._commit_task_quota(key)
            except Exception as exc:
                logger.error({"event": "image_task_quota_commit_failed", "error": str(exc)[:300]})
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成（续轮询）",
                status="success",
                urls=_collect_image_urls(data),
            )
        except Exception as exc:
            error_message = str(exc) or "resume poll failed"
            duration_ms = int((time.time() - started) * 1000)
            if not self._is_resumable_timeout(error_message, conversation_id):
                self._refund_task_quota(key)
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_message, data=[], duration_ms=duration_ms)
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败（续轮询）",
                status="failed",
                error=error_message,
            )
        finally:
            if backend is not None:
                backend.close()


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
