from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .bridge import invoke_transport
from .logging_utils import get_logger
from .models import AccountSessionState, WeixinPluginError
from .runtime_state import RuntimeStateStore, build_runtime_context


def execute(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = payload or {}
    action_name = str(data.get("action_name") or "").strip()
    if not action_name:
        raise WeixinPluginError(
            error_code="invalid_action_payload",
            detail="weixin claw action_name is required",
            field="action_name",
        )

    context = build_runtime_context(data)
    logger = get_logger("action", context=context)
    store = RuntimeStateStore(context)
    channel_account_id = _resolve_channel_account_id(data)
    _assert_account_enabled(data)

    logger.info("执行微信插件动作: action=%s account=%s", action_name, channel_account_id)

    if action_name == "start_login":
        return _start_login(data=data, store=store, channel_account_id=channel_account_id)
    if action_name == "get_login_status":
        return _get_login_status(data=data, store=store, channel_account_id=channel_account_id)
    if action_name == "logout":
        return _logout(store=store, channel_account_id=channel_account_id)
    if action_name == "purge_runtime_state":
        return _purge_runtime_state(store=store, channel_account_id=channel_account_id)

    raise WeixinPluginError(
        error_code="unsupported_action",
        detail=f"Unsupported action request: {action_name}",
        field="action_name",
    )


def _start_login(
    *,
    data: dict[str, Any],
    store: RuntimeStateStore,
    channel_account_id: str,
) -> dict[str, Any]:
    existing = store.get_account_session(channel_account_id)
    force = data.get("force") is True
    if existing is not None and not force:
        if existing.status == "active":
            return _build_status_result(
                action_name="start_login",
                channel_account_id=channel_account_id,
                store=store,
                session=existing,
                message="当前账号已登录，无需重复扫码。",
            )
        if existing.status in {"waiting_scan", "scan_confirmed"} and not _is_expired(existing.expires_at):
            return _build_status_result(
                action_name="start_login",
                channel_account_id=channel_account_id,
                store=store,
                session=existing,
                message="二维码已生成，请继续扫码。",
            )

    bridge_payload = dict(data)
    bridge_payload["channel_account_id"] = channel_account_id
    result = invoke_transport(kind="action", action="start_login", payload=bridge_payload)
    if not isinstance(result, dict):
        raise WeixinPluginError(
            error_code="bridge_protocol_error",
            detail="weixin claw action bridge result must be a JSON object",
        )

    session = store.mark_waiting_scan(
        channel_account_id,
        login_session_key=_require_text(result.get("login_session_key"), field="login_session_key"),
        login_qrcode=_optional_text(result.get("login_qrcode")),
        qr_code_url=_optional_text(result.get("qr_code_url")),
        api_base_url=_optional_text(result.get("api_base_url")),
    )
    return _build_status_result(
        action_name="start_login",
        channel_account_id=channel_account_id,
        store=store,
        session=session,
        message=_optional_text(result.get("message")) or "二维码已生成，请使用微信扫码。",
    )


def _get_login_status(
    *,
    data: dict[str, Any],
    store: RuntimeStateStore,
    channel_account_id: str,
) -> dict[str, Any]:
    session = store.get_account_session(channel_account_id)
    if session is None:
        return _build_status_result(
            action_name="get_login_status",
            channel_account_id=channel_account_id,
            store=store,
            session=None,
            message="当前账号还没有可用登录态。",
        )

    if session.status == "active":
        return _build_status_result(
            action_name="get_login_status",
            channel_account_id=channel_account_id,
            store=store,
            session=session,
            message="当前账号已登录。",
        )

    if session.login_qrcode is None:
        return _build_status_result(
            action_name="get_login_status",
            channel_account_id=channel_account_id,
            store=store,
            session=session,
            message="当前没有待扫码登录会话。",
        )

    bridge_payload = dict(data)
    bridge_payload["channel_account_id"] = channel_account_id
    bridge_payload["login_session_key"] = session.login_session_key
    bridge_payload["login_qrcode"] = session.login_qrcode
    if session.api_base_url and "transport" not in bridge_payload:
        bridge_payload["transport"] = {"api_base_url": session.api_base_url}

    result = invoke_transport(kind="action", action="get_login_status", payload=bridge_payload)
    if not isinstance(result, dict):
        raise WeixinPluginError(
            error_code="bridge_protocol_error",
            detail="weixin claw action bridge result must be a JSON object",
        )

    login_status = _optional_text(result.get("login_status")) or session.status
    updated = session
    if login_status == "active":
        updated = store.save_account_session(
            channel_account_id,
            status="active",
            provider_account_id=_optional_text(result.get("provider_account_id")),
            api_base_url=_optional_text(result.get("api_base_url")) or session.api_base_url,
            token=_optional_text(result.get("token")),
            user_id=_optional_text(result.get("user_id")),
            last_error_code=None,
            last_error_message=None,
            expires_at=None,
        )
    elif login_status in {"waiting_scan", "scan_confirmed"}:
        updated = store.save_account_session(
            channel_account_id,
            status=login_status,
            api_base_url=_optional_text(result.get("api_base_url")) or session.api_base_url,
            last_error_code=None,
            last_error_message=None,
        )
    elif login_status == "expired":
        updated = store.save_account_session(
            channel_account_id,
            status="expired",
            token=None,
            provider_account_id=None,
            user_id=None,
            last_error_code="qr_code_expired",
            last_error_message=_optional_text(result.get("message")) or "二维码已过期，请重新登录。",
        )
    elif login_status == "not_logged_in":
        updated = store.save_account_session(
            channel_account_id,
            status="not_logged_in",
            token=None,
            provider_account_id=None,
            user_id=None,
            last_error_code=None,
            last_error_message=None,
        )

    return _build_status_result(
        action_name="get_login_status",
        channel_account_id=channel_account_id,
        store=store,
        session=updated,
        message=_optional_text(result.get("message")) or _default_message_for_status(updated.status),
    )


def _logout(*, store: RuntimeStateStore, channel_account_id: str) -> dict[str, Any]:
    session = store.save_account_session(
        channel_account_id,
        status="not_logged_in",
        login_session_key=None,
        login_qrcode=None,
        qr_code_url=None,
        qr_code_path=None,
        provider_account_id=None,
        token=None,
        user_id=None,
        expires_at=None,
        last_error_code=None,
        last_error_message=None,
    )
    return _build_status_result(
        action_name="logout",
        channel_account_id=channel_account_id,
        store=store,
        session=session,
        message="登录态已清理。",
        accepted=True,
    )


def _purge_runtime_state(*, store: RuntimeStateStore, channel_account_id: str) -> dict[str, Any]:
    store.purge_runtime_state()
    return {
        "action_name": "purge_runtime_state",
        "channel_account_id": channel_account_id,
        "status": "accepted",
        "message": "插件私有运行状态已清空。",
        "status_summary": {
            "status": "not_logged_in",
            "title": "运行状态已清空",
            "message": "当前账号的插件私有运行状态已清空。",
            "tone": "warning",
        },
        "runtime": {
            "working_dir": str(store.context.working_dir),
            "runtime_db_path": str(store.context.runtime_db_path),
        },
    }


def _resolve_channel_account_id(payload: dict[str, Any]) -> str:
    account = payload.get("account")
    if isinstance(account, dict):
        for key in ("id", "account_code", "account_label"):
            value = account.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    channel_account_id = payload.get("channel_account_id")
    if isinstance(channel_account_id, str) and channel_account_id.strip():
        return channel_account_id.strip()
    raise WeixinPluginError(
        error_code="invalid_action_payload",
        detail="channel_account_id is required",
        field="channel_account_id",
    )


def _assert_account_enabled(payload: dict[str, Any]) -> None:
    account = payload.get("account")
    if isinstance(account, dict) and account.get("enabled") is False:
        raise WeixinPluginError(
            error_code="account_disabled",
            detail="channel account is disabled",
            field="account.enabled",
        )


def _build_status_result(
    *,
    action_name: str,
    channel_account_id: str,
    store: RuntimeStateStore,
    session: AccountSessionState | None,
    message: str,
    accepted: bool = False,
) -> dict[str, Any]:
    login_status = session.status if session is not None else "not_logged_in"
    status_summary = {
        "status": login_status,
        "title": _build_status_title(login_status),
        "message": message,
        "tone": _build_status_tone(login_status, session=session),
        "last_error_code": None if session is None else session.last_error_code,
        "last_error_message": None if session is None else session.last_error_message,
        "updated_at": None if session is None else session.updated_at,
        "details": _build_status_details(session),
    }
    payload: dict[str, Any] = {
        "action_name": action_name,
        "channel_account_id": channel_account_id,
        "login_status": login_status,
        "message": message,
        "status_summary": status_summary,
        "artifacts": _build_artifacts(session),
        "runtime": {
            "working_dir": str(store.context.working_dir),
            "runtime_db_path": str(store.context.runtime_db_path),
        },
    }
    if accepted:
        payload["status"] = "accepted"
    if session is None:
        return payload
    payload.update(
        {
            "login_session_key": session.login_session_key,
            "qr_code_url": session.qr_code_url,
            "qr_code_path": session.qr_code_path,
            "provider_account_id": session.provider_account_id,
            "api_base_url": session.api_base_url,
            "user_id": session.user_id,
            "last_error_code": session.last_error_code,
            "last_error_message": session.last_error_message,
            "updated_at": session.updated_at,
        }
    )
    return payload


def _build_status_title(status: str) -> str:
    if status == "active":
        return "账号已登录"
    if status == "waiting_scan":
        return "等待扫码"
    if status == "scan_confirmed":
        return "等待确认"
    if status == "expired":
        return "登录已失效"
    return "尚未登录"


def _build_status_tone(status: str, *, session: AccountSessionState | None) -> str:
    if session is not None and (session.last_error_code or session.last_error_message):
        return "danger"
    if status == "active":
        return "success"
    if status in {"waiting_scan", "scan_confirmed"}:
        return "info"
    if status == "expired":
        return "warning"
    return "neutral"


def _build_status_details(session: AccountSessionState | None) -> dict[str, Any]:
    if session is None:
        return {}
    return {
        "provider_account_id": session.provider_account_id,
        "user_id": session.user_id,
        "api_base_url": session.api_base_url,
    }


def _build_artifacts(session: AccountSessionState | None) -> list[dict[str, Any]]:
    if session is None:
        return []

    artifacts: list[dict[str, Any]] = []
    if session.qr_code_url and session.status in {"waiting_scan", "scan_confirmed"}:
        artifacts.append(
            {
                "kind": "image_url",
                "label": "登录二维码",
                "url": session.qr_code_url,
            }
        )
    return artifacts


def _default_message_for_status(status: str) -> str:
    if status == "active":
        return "当前账号已登录。"
    if status == "waiting_scan":
        return "二维码已生成，请扫码。"
    if status == "scan_confirmed":
        return "已扫码，请在微信里确认。"
    if status == "expired":
        return "二维码已过期，请重新生成。"
    return "当前账号还没有可用登录态。"


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _require_text(value: Any, *, field: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise WeixinPluginError(
            error_code="bridge_protocol_error",
            detail=f"{field} is required in bridge result",
            field=field,
        )
    return normalized


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
    except ValueError:
        return False
