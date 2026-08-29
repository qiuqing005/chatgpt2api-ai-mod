from __future__ import annotations

import json
import hashlib
from typing import Any

from sqlalchemy import Column, Index, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend

Base = declarative_base()


class AccountModel(Base):
    """账号数据模型"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Access tokens can exceed a MySQL index's byte limit when stored as utf8mb4.
    # Keep the full token in TEXT and use a fixed-size digest for identity lookups.
    access_token = Column(Text, nullable=False)
    access_token_hash = Column(String(64), nullable=False)
    data = Column(Text, nullable=False)  # JSON 格式存储完整账号数据

    __table_args__ = (
        Index("uq_accounts_access_token_hash", "access_token_hash", unique=True),
    )


class AuthKeyModel(Base):
    """鉴权密钥数据模型"""
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class DatabaseStorageBackend(StorageBackend):
    """数据库存储后端（支持 SQLite、PostgreSQL、MySQL 等）"""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,  # 自动检测连接是否有效
            pool_recycle=3600,   # 1小时回收连接
        )
        self._prepare_account_schema()
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _prepare_account_schema(self) -> None:
        """Prepare the digest column before SQLAlchemy creates indexes.

        ``create_all`` does not alter an existing table. This small migration keeps
        older SQLite/PostgreSQL installations readable while allowing new MySQL
        installations to avoid indexing the full token column.
        """
        inspector = inspect(self.engine)
        if "accounts" not in inspector.get_table_names():
            return

        columns = {column["name"] for column in inspector.get_columns("accounts")}
        if "access_token_hash" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE accounts ADD COLUMN access_token_hash VARCHAR(64)")
                )

        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, access_token FROM accounts "
                    "WHERE access_token_hash IS NULL OR access_token_hash = ''"
                )
            ).fetchall()
            for row_id, access_token in rows:
                connection.execute(
                    text(
                        "UPDATE accounts SET access_token_hash = :token_hash "
                        "WHERE id = :row_id"
                    ),
                    {
                        "token_hash": self._token_hash(str(access_token or "")),
                        "row_id": row_id,
                    },
                )

    def load_accounts(self) -> list[dict[str, Any]]:
        """从数据库加载账号数据"""
        session = self.Session()
        try:
            accounts = []
            for row in session.query(AccountModel).all():
                try:
                    account_data = json.loads(row.data)
                    if isinstance(account_data, dict):
                        accounts.append(account_data)
                except json.JSONDecodeError:
                    continue
            return accounts
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到数据库"""
        session = self.Session()
        try:
            existing_rows = session.query(AccountModel).all()
            existing_by_hash = {
                row.access_token_hash or self._token_hash(row.access_token): row
                for row in existing_rows
            }
            existing_by_token = {row.access_token: row for row in existing_rows}
            incoming_keys: set[str] = set()

            for item in accounts:
                if not isinstance(item, dict):
                    continue
                access_token = str(item.get("access_token") or "").strip()
                if not access_token:
                    continue
                token_hash = self._token_hash(access_token)
                if token_hash in incoming_keys:
                    raise ValueError("Duplicate access_token in storage snapshot")

                incoming_keys.add(token_hash)
                serialized_data = json.dumps(item, ensure_ascii=False)
                existing_row = existing_by_hash.get(token_hash) or existing_by_token.get(access_token)
                if existing_row is None:
                    session.add(
                        AccountModel(
                            access_token=access_token,
                            access_token_hash=token_hash,
                            data=serialized_data,
                        )
                    )
                else:
                    existing_row.access_token = access_token
                    existing_row.access_token_hash = token_hash
                    if existing_row.data != serialized_data:
                        existing_row.data = serialized_data

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从数据库加载鉴权密钥数据"""
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到数据库"""
        self._save_rows(AuthKeyModel, auth_keys, "id", "key_id")

    def _load_rows(self, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(model).all():
                try:
                    item_data = json.loads(row.data)
                    if isinstance(item_data, dict):
                        items.append(item_data)
                except json.JSONDecodeError:
                    continue
            return items
        finally:
            session.close()

    def _save_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        items: list[dict[str, Any]],
        source_key: str,
        target_key: str | None = None,
    ) -> None:
        session = self.Session()
        try:
            key_column = target_key or source_key
            existing_rows = {
                str(getattr(row, key_column)): row
                for row in session.query(model).all()
            }
            incoming_keys: set[str] = set()

            for item in items:
                if not isinstance(item, dict):
                    continue
                key_value = str(item.get(source_key) or "").strip()
                if not key_value:
                    continue
                if key_value in incoming_keys:
                    raise ValueError(f"Duplicate {source_key} in storage snapshot")

                incoming_keys.add(key_value)
                serialized_data = json.dumps(item, ensure_ascii=False)
                existing_row = existing_rows.get(key_value)
                if existing_row is None:
                    session.add(
                        model(
                            **{key_column: key_value},
                            data=serialized_data,
                        )
                    )
                elif existing_row.data != serialized_data:
                    existing_row.data = serialized_data

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _delete_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        values: list[str],
        key_column: str,
    ) -> int:
        normalized = {str(value or "").strip() for value in values if str(value or "").strip()}
        if not normalized:
            return 0
        session = self.Session()
        try:
            column = getattr(model, key_column)
            removed = session.query(model).filter(column.in_(normalized)).delete(synchronize_session=False)
            session.commit()
            return int(removed or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_accounts(self, access_tokens: list[str]) -> int:
        return self._delete_rows(AccountModel, access_tokens, "access_token")

    def delete_auth_keys(self, key_ids: list[str]) -> int:
        return self._delete_rows(AuthKeyModel, key_ids, "key_id")

    @staticmethod
    def _token_hash(access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            session = self.Session()
            try:
                # 尝试执行简单查询
                session.execute(text("SELECT 1"))
                count = session.query(AccountModel).count()
                auth_key_count = session.query(AuthKeyModel).count()
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": count,
                    "auth_key_count": auth_key_count,
                }
            finally:
                session.close()
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"
        
        return {
            "type": "database",
            "db_type": db_type,
            "description": f"数据库存储 ({db_type})",
            "database_url": self._mask_password(self.database_url),
        }

    @staticmethod
    def _mask_password(url: str) -> str:
        """隐藏数据库连接字符串中的密码"""
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url
