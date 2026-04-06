from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any

from .models import (
    AccountSessionState,
    ContextTokenState,
    DeliveryReceiptState,
    PollCheckpointState,
    WeixinPluginError,
    WeixinRuntimeContext,
)


_SAFE_SCOPE_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")
_DB_TIMEOUT_SECONDS = 30
_LOGIN_QR_TTL_SECONDS = 5 * 60


def build_runtime_context(payload: dict[str, Any] | None = None) -> WeixinRuntimeContext:
    """生成并初始化插件私有运行上下文。"""

    data = payload or {}
    plugin_root = Path(__file__).resolve().parents[1]
    working_dir = _resolve_working_dir(data, plugin_root=plugin_root)
    context = WeixinRuntimeContext(
        plugin_root=plugin_root,
        working_dir=working_dir,
        account_scope=_resolve_account_scope(data),
        runtime_db_path=working_dir / "runtime.sqlite",
        media_dir=working_dir / "media",
        logs_dir=working_dir / "logs",
        qr_dir=working_dir / "qr",
    )
    ensure_runtime_layout(context)
    return context


def ensure_runtime_layout(context: WeixinRuntimeContext) -> None:
    """创建插件私有目录并初始化 SQLite 表。"""

    context.working_dir.mkdir(parents=True, exist_ok=True)
    context.media_dir.mkdir(parents=True, exist_ok=True)
    context.logs_dir.mkdir(parents=True, exist_ok=True)
    context.qr_dir.mkdir(parents=True, exist_ok=True)
    with _connect(context) as connection:
        _initialize_schema(connection)


class RuntimeStateStore:
    """插件私有状态读写封装。"""

    def __init__(self, context: WeixinRuntimeContext) -> None:
        self.context = context
        ensure_runtime_layout(context)

    def get_account_session(self, channel_account_id: str) -> AccountSessionState | None:
        with _connect(self.context) as connection:
            row = connection.execute(
                """
                SELECT
                    channel_account_id,
                    status,
                    login_session_key,
                    login_qrcode,
                    qr_code_url,
                    qr_code_path,
                    session_blob_path,
                    provider_account_id,
                    api_base_url,
                    token,
                    user_id,
                    last_error_code,
                    last_error_message,
                    login_started_at,
                    expires_at,
                    created_at,
                    updated_at
                FROM account_sessions
                WHERE channel_account_id = ?
                """,
                (_require_text(channel_account_id, field="channel_account_id"),),
            ).fetchone()
        if row is None:
            return None
        return AccountSessionState.from_mapping(dict(row))

    def save_account_session(self, channel_account_id: str, **updates: Any) -> AccountSessionState:
        normalized = _require_text(channel_account_id, field="channel_account_id")
        current = self.get_account_session(normalized)
        now = _utcnow_iso()
        record = current.to_record() if current is not None else {
            "channel_account_id": normalized,
            "status": "not_logged_in",
            "login_session_key": None,
            "login_qrcode": None,
            "qr_code_url": None,
            "qr_code_path": None,
            "session_blob_path": None,
            "provider_account_id": None,
            "api_base_url": None,
            "token": None,
            "user_id": None,
            "last_error_code": None,
            "last_error_message": None,
            "login_started_at": None,
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }
        for key, value in updates.items():
            if key in record:
                record[key] = _normalize_optional_text(value)
        record["channel_account_id"] = normalized
        record["status"] = _require_text(record.get("status"), field="status")
        record["updated_at"] = now
        if record.get("created_at") is None:
            record["created_at"] = now

        with _connect(self.context) as connection:
            connection.execute(
                """
                INSERT INTO account_sessions (
                    channel_account_id,
                    status,
                    login_session_key,
                    login_qrcode,
                    qr_code_url,
                    qr_code_path,
                    session_blob_path,
                    provider_account_id,
                    api_base_url,
                    token,
                    user_id,
                    last_error_code,
                    last_error_message,
                    login_started_at,
                    expires_at,
                    created_at,
                    updated_at
                ) VALUES (
                    :channel_account_id,
                    :status,
                    :login_session_key,
                    :login_qrcode,
                    :qr_code_url,
                    :qr_code_path,
                    :session_blob_path,
                    :provider_account_id,
                    :api_base_url,
                    :token,
                    :user_id,
                    :last_error_code,
                    :last_error_message,
                    :login_started_at,
                    :expires_at,
                    :created_at,
                    :updated_at
                )
                ON CONFLICT(channel_account_id) DO UPDATE SET
                    status = excluded.status,
                    login_session_key = excluded.login_session_key,
                    login_qrcode = excluded.login_qrcode,
                    qr_code_url = excluded.qr_code_url,
                    qr_code_path = excluded.qr_code_path,
                    session_blob_path = excluded.session_blob_path,
                    provider_account_id = excluded.provider_account_id,
                    api_base_url = excluded.api_base_url,
                    token = excluded.token,
                    user_id = excluded.user_id,
                    last_error_code = excluded.last_error_code,
                    last_error_message = excluded.last_error_message,
                    login_started_at = excluded.login_started_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                record,
            )
        return AccountSessionState.from_mapping(record)

    def mark_waiting_scan(
        self,
        channel_account_id: str,
        *,
        login_session_key: str,
        login_qrcode: str | None,
        qr_code_url: str | None,
        api_base_url: str | None,
    ) -> AccountSessionState:
        qr_path = self.write_qr_artifact(
            channel_account_id=channel_account_id,
            login_session_key=login_session_key,
            qr_code_url=qr_code_url,
        )
        return self.save_account_session(
            channel_account_id,
            status="waiting_scan",
            login_session_key=login_session_key,
            login_qrcode=login_qrcode,
            qr_code_url=qr_code_url,
            qr_code_path=str(qr_path) if qr_path is not None else None,
            api_base_url=api_base_url,
            token=None,
            provider_account_id=None,
            user_id=None,
            last_error_code=None,
            last_error_message=None,
            login_started_at=_utcnow_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=_LOGIN_QR_TTL_SECONDS)).isoformat(),
        )

    def set_poll_checkpoint(
        self,
        channel_account_id: str,
        *,
        cursor: str | None,
        latest_external_event_id: str | None = None,
    ) -> PollCheckpointState:
        record = {
            "channel_account_id": _require_text(channel_account_id, field="channel_account_id"),
            "cursor": _normalize_optional_text(cursor),
            "latest_external_event_id": _normalize_optional_text(latest_external_event_id),
            "updated_at": _utcnow_iso(),
        }
        with _connect(self.context) as connection:
            connection.execute(
                """
                INSERT INTO poll_checkpoints (
                    channel_account_id,
                    cursor,
                    latest_external_event_id,
                    updated_at
                ) VALUES (
                    :channel_account_id,
                    :cursor,
                    :latest_external_event_id,
                    :updated_at
                )
                ON CONFLICT(channel_account_id) DO UPDATE SET
                    cursor = excluded.cursor,
                    latest_external_event_id = excluded.latest_external_event_id,
                    updated_at = excluded.updated_at
                """,
                record,
            )
        return PollCheckpointState.from_mapping(record)

    def get_poll_checkpoint(self, channel_account_id: str) -> PollCheckpointState | None:
        with _connect(self.context) as connection:
            row = connection.execute(
                """
                SELECT
                    channel_account_id,
                    cursor,
                    latest_external_event_id,
                    updated_at
                FROM poll_checkpoints
                WHERE channel_account_id = ?
                """,
                (_require_text(channel_account_id, field="channel_account_id"),),
            ).fetchone()
        if row is None:
            return None
        return PollCheckpointState.from_mapping(dict(row))

    def save_context_token(
        self,
        *,
        channel_account_id: str,
        conversation_key: str,
        external_user_id: str,
        token: str,
        status: str = "fresh",
        expires_at: str | None = None,
    ) -> ContextTokenState:
        record = {
            "channel_account_id": _require_text(channel_account_id, field="channel_account_id"),
            "conversation_key": _require_text(conversation_key, field="conversation_key"),
            "external_user_id": _require_text(external_user_id, field="external_user_id"),
            "token": _require_text(token, field="token"),
            "status": _require_text(status, field="status"),
            "expires_at": _normalize_optional_text(expires_at),
            "updated_at": _utcnow_iso(),
        }
        with _connect(self.context) as connection:
            connection.execute(
                """
                INSERT INTO context_tokens (
                    channel_account_id,
                    conversation_key,
                    external_user_id,
                    token,
                    status,
                    expires_at,
                    updated_at
                ) VALUES (
                    :channel_account_id,
                    :conversation_key,
                    :external_user_id,
                    :token,
                    :status,
                    :expires_at,
                    :updated_at
                )
                ON CONFLICT(channel_account_id, conversation_key, external_user_id) DO UPDATE SET
                    token = excluded.token,
                    status = excluded.status,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                record,
            )
        return ContextTokenState.from_mapping(record)

    def get_context_token(
        self,
        *,
        channel_account_id: str,
        conversation_key: str,
        external_user_id: str,
    ) -> ContextTokenState | None:
        with _connect(self.context) as connection:
            row = connection.execute(
                """
                SELECT
                    channel_account_id,
                    conversation_key,
                    external_user_id,
                    token,
                    status,
                    expires_at,
                    updated_at
                FROM context_tokens
                WHERE channel_account_id = ?
                  AND conversation_key = ?
                  AND external_user_id = ?
                """,
                (
                    _require_text(channel_account_id, field="channel_account_id"),
                    _require_text(conversation_key, field="conversation_key"),
                    _require_text(external_user_id, field="external_user_id"),
                ),
            ).fetchone()
        if row is None:
            return None
        return ContextTokenState.from_mapping(dict(row))

    def record_delivery_receipt(
        self,
        *,
        channel_account_id: str,
        provider_message_ref: str,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DeliveryReceiptState:
        record = {
            "channel_account_id": _require_text(channel_account_id, field="channel_account_id"),
            "provider_message_ref": _require_text(provider_message_ref, field="provider_message_ref"),
            "status": _require_text(status, field="status"),
            "error_code": _normalize_optional_text(error_code),
            "error_message": _normalize_optional_text(error_message),
            "updated_at": _utcnow_iso(),
        }
        with _connect(self.context) as connection:
            connection.execute(
                """
                INSERT INTO delivery_receipts (
                    channel_account_id,
                    provider_message_ref,
                    status,
                    error_code,
                    error_message,
                    updated_at
                ) VALUES (
                    :channel_account_id,
                    :provider_message_ref,
                    :status,
                    :error_code,
                    :error_message,
                    :updated_at
                )
                ON CONFLICT(channel_account_id, provider_message_ref) DO UPDATE SET
                    status = excluded.status,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                record,
            )
        return DeliveryReceiptState.from_mapping(record)

    def get_delivery_receipt(
        self,
        *,
        channel_account_id: str,
        provider_message_ref: str,
    ) -> DeliveryReceiptState | None:
        with _connect(self.context) as connection:
            row = connection.execute(
                """
                SELECT
                    channel_account_id,
                    provider_message_ref,
                    status,
                    error_code,
                    error_message,
                    updated_at
                FROM delivery_receipts
                WHERE channel_account_id = ?
                  AND provider_message_ref = ?
                """,
                (
                    _require_text(channel_account_id, field="channel_account_id"),
                    _require_text(provider_message_ref, field="provider_message_ref"),
                ),
            ).fetchone()
        if row is None:
            return None
        return DeliveryReceiptState.from_mapping(dict(row))

    def write_qr_artifact(
        self,
        *,
        channel_account_id: str,
        login_session_key: str,
        qr_code_url: str | None,
    ) -> Path | None:
        normalized_url = _normalize_optional_text(qr_code_url)
        if normalized_url is None:
            return None
        filename = f"{_normalize_scope(channel_account_id)}-{_normalize_scope(login_session_key)}.json"
        target = self.context.qr_dir / filename
        target.write_text(
            json.dumps(
                {
                    "channel_account_id": channel_account_id,
                    "login_session_key": login_session_key,
                    "qr_code_url": normalized_url,
                    "written_at": _utcnow_iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return target

    def purge_runtime_state(self) -> None:
        with _connect(self.context) as connection:
            connection.executescript(
                """
                DELETE FROM account_sessions;
                DELETE FROM poll_checkpoints;
                DELETE FROM context_tokens;
                DELETE FROM delivery_receipts;
                """
            )
        _clear_directory(self.context.media_dir)
        _clear_directory(self.context.qr_dir)
        _clear_directory(self.context.logs_dir)
        ensure_runtime_layout(self.context)


def _resolve_working_dir(payload: dict[str, Any], *, plugin_root: Path) -> Path:
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        value = runtime.get("working_dir")
        if isinstance(value, str) and value.strip():
            return Path(value.strip()).resolve()
    return (plugin_root / ".runtime" / _resolve_account_scope(payload)).resolve()


def _resolve_account_scope(payload: dict[str, Any]) -> str:
    account = payload.get("account")
    if isinstance(account, dict):
        for key in ("id", "account_code", "account_label"):
            value = account.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_scope(value)
    channel_account_id = payload.get("channel_account_id")
    if isinstance(channel_account_id, str) and channel_account_id.strip():
        return _normalize_scope(channel_account_id)
    return "global"


def _normalize_scope(value: str) -> str:
    normalized = _SAFE_SCOPE_PATTERN.sub("-", value.strip())
    normalized = normalized.strip("-")
    return normalized or "global"


def _connect(context: WeixinRuntimeContext) -> sqlite3.Connection:
    connection = sqlite3.connect(
        context.runtime_db_path,
        timeout=_DB_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_sessions (
            channel_account_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            login_session_key TEXT,
            login_qrcode TEXT,
            qr_code_url TEXT,
            qr_code_path TEXT,
            session_blob_path TEXT,
            provider_account_id TEXT,
            api_base_url TEXT,
            token TEXT,
            user_id TEXT,
            last_error_code TEXT,
            last_error_message TEXT,
            login_started_at TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poll_checkpoints (
            channel_account_id TEXT PRIMARY KEY,
            cursor TEXT,
            latest_external_event_id TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_tokens (
            channel_account_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            external_user_id TEXT NOT NULL,
            token TEXT NOT NULL,
            status TEXT NOT NULL,
            expires_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel_account_id, conversation_key, external_user_id)
        );

        CREATE TABLE IF NOT EXISTS delivery_receipts (
            channel_account_id TEXT NOT NULL,
            provider_message_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel_account_id, provider_message_ref)
        );
        """
    )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, *, field: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise WeixinPluginError(
            error_code="invalid_action_payload",
            detail=f"{field} is required",
            field=field,
        )
    return normalized


def _normalize_optional_text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
