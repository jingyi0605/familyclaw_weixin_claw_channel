from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _as_optional_text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


@dataclass(slots=True)
class WeixinRuntimeContext:
    """插件私有运行上下文。"""

    plugin_root: Path
    working_dir: Path
    account_scope: str
    runtime_db_path: Path
    media_dir: Path
    logs_dir: Path
    qr_dir: Path

    def to_payload(self) -> dict[str, str]:
        return {
            "plugin_root": str(self.plugin_root),
            "working_dir": str(self.working_dir),
            "account_scope": self.account_scope,
            "runtime_db_path": str(self.runtime_db_path),
            "media_dir": str(self.media_dir),
            "logs_dir": str(self.logs_dir),
            "qr_dir": str(self.qr_dir),
        }


@dataclass(slots=True)
class BridgeRequest:
    """Python 发给 Node 的统一请求结构。"""

    kind: str
    action: str
    payload: dict[str, Any]
    runtime: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "payload": self.payload,
            "runtime": self.runtime,
        }


class WeixinPluginError(RuntimeError):
    """插件本地逻辑返回的结构化错误。"""

    def __init__(self, *, error_code: str, detail: str, field: str | None = None) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.field = field


class WeixinBridgeError(WeixinPluginError):
    """Node 桥接层已经返回结构化错误。"""


class WeixinBridgeProtocolError(WeixinPluginError):
    """Node 进程返回了不符合协议的内容。"""

    def __init__(self, detail: str) -> None:
        super().__init__(error_code="bridge_protocol_error", detail=detail)


@dataclass(slots=True)
class AccountSessionState:
    """账号级登录态。"""

    channel_account_id: str
    status: str
    login_session_key: str | None = None
    login_qrcode: str | None = None
    qr_code_url: str | None = None
    qr_code_path: str | None = None
    session_blob_path: str | None = None
    provider_account_id: str | None = None
    api_base_url: str | None = None
    token: str | None = None
    user_id: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    login_started_at: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "AccountSessionState":
        return cls(
            channel_account_id=str(payload.get("channel_account_id") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            login_session_key=_as_optional_text(payload.get("login_session_key")),
            login_qrcode=_as_optional_text(payload.get("login_qrcode")),
            qr_code_url=_as_optional_text(payload.get("qr_code_url")),
            qr_code_path=_as_optional_text(payload.get("qr_code_path")),
            session_blob_path=_as_optional_text(payload.get("session_blob_path")),
            provider_account_id=_as_optional_text(payload.get("provider_account_id")),
            api_base_url=_as_optional_text(payload.get("api_base_url")),
            token=_as_optional_text(payload.get("token")),
            user_id=_as_optional_text(payload.get("user_id")),
            last_error_code=_as_optional_text(payload.get("last_error_code")),
            last_error_message=_as_optional_text(payload.get("last_error_message")),
            login_started_at=_as_optional_text(payload.get("login_started_at")),
            expires_at=_as_optional_text(payload.get("expires_at")),
            created_at=_as_optional_text(payload.get("created_at")),
            updated_at=_as_optional_text(payload.get("updated_at")),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "channel_account_id": self.channel_account_id,
            "status": self.status,
            "login_session_key": self.login_session_key,
            "login_qrcode": self.login_qrcode,
            "qr_code_url": self.qr_code_url,
            "qr_code_path": self.qr_code_path,
            "session_blob_path": self.session_blob_path,
            "provider_account_id": self.provider_account_id,
            "api_base_url": self.api_base_url,
            "token": self.token,
            "user_id": self.user_id,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "login_started_at": self.login_started_at,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class PollCheckpointState:
    """轮询游标。"""

    channel_account_id: str
    cursor: str | None = None
    latest_external_event_id: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "PollCheckpointState":
        return cls(
            channel_account_id=str(payload.get("channel_account_id") or "").strip(),
            cursor=_as_optional_text(payload.get("cursor")),
            latest_external_event_id=_as_optional_text(payload.get("latest_external_event_id")),
            updated_at=_as_optional_text(payload.get("updated_at")),
        )


@dataclass(slots=True)
class ContextTokenState:
    """可恢复的 context_token。"""

    channel_account_id: str
    conversation_key: str
    external_user_id: str
    token: str
    status: str
    expires_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "ContextTokenState":
        return cls(
            channel_account_id=str(payload.get("channel_account_id") or "").strip(),
            conversation_key=str(payload.get("conversation_key") or "").strip(),
            external_user_id=str(payload.get("external_user_id") or "").strip(),
            token=str(payload.get("token") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            expires_at=_as_optional_text(payload.get("expires_at")),
            updated_at=_as_optional_text(payload.get("updated_at")),
        )


@dataclass(slots=True)
class DeliveryReceiptState:
    """发送回执。"""

    channel_account_id: str
    provider_message_ref: str
    status: str
    error_code: str | None = None
    error_message: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "DeliveryReceiptState":
        return cls(
            channel_account_id=str(payload.get("channel_account_id") or "").strip(),
            provider_message_ref=str(payload.get("provider_message_ref") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            error_code=_as_optional_text(payload.get("error_code")),
            error_message=_as_optional_text(payload.get("error_message")),
            updated_at=_as_optional_text(payload.get("updated_at")),
        )
