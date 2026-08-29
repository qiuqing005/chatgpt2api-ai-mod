from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """增量保存账号数据；缺失记录不得被解释为删除。"""
        pass

    def delete_accounts(self, access_tokens: list[str]) -> int:
        """显式删除账号。具体后端可覆盖此方法以执行原子删除。"""
        tokens = {str(token or "").strip() for token in access_tokens if str(token or "").strip()}
        if not tokens:
            return 0
        current = self.load_accounts()
        remaining = [
            item for item in current
            if not isinstance(item, dict) or str(item.get("access_token") or "").strip() not in tokens
        ]
        removed = len(current) - len(remaining)
        if removed:
            self.save_accounts(remaining)
        return removed

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """增量保存鉴权密钥；缺失记录不得被解释为删除。"""
        pass

    def delete_auth_keys(self, key_ids: list[str]) -> int:
        """显式删除鉴权密钥。"""
        ids = {str(key_id or "").strip() for key_id in key_ids if str(key_id or "").strip()}
        if not ids:
            return 0
        current = self.load_auth_keys()
        remaining = [
            item for item in current
            if not isinstance(item, dict) or str(item.get("id") or "").strip() not in ids
        ]
        removed = len(current) - len(remaining)
        if removed:
            self.save_auth_keys(remaining)
        return removed

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
