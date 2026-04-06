from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .bridge import invoke_transport
from .logging_utils import get_logger
from .models import WeixinBridgeError, WeixinPluginError
from .runtime_state import RuntimeStateStore, build_runtime_context


def handle(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = payload or {}
    action = str(data.get("action") or "").strip()
    if action not in {"poll", "send", "probe", "webhook"}:
        raise WeixinPluginError(
            error_code="unsupported_action",
            detail="weixin claw channel action is not supported",
            field="action",
        )

    if action == "webhook":
        return {
            "message": "weixin claw channel currently runs in polling mode",
            "http_response": {
                "status_code": 202,
                "body_text": "polling-only",
                "media_type": "text/plain",
            },
        }

    context = build_runtime_context(data)
    logger = get_logger("channel", context=context)
    store = RuntimeStateStore(context)
    channel_account_id = _resolve_channel_account_id(data)
    session = store.get_account_session(channel_account_id)

    logger.info("执行微信通道动作: action=%s account=%s", action, channel_account_id)

    if action == "probe":
        return _handle_probe(
            channel_account_id=channel_account_id,
            session=session,
            store=store,
        )

    active_session = _require_active_session(session, channel_account_id=channel_account_id)
    if action == "poll":
        return _handle_poll(
            data=data,
            channel_account_id=channel_account_id,
            session=active_session,
            store=store,
        )
    if action == "send":
        return _handle_send(
            data=data,
            channel_account_id=channel_account_id,
            session=active_session,
            store=store,
        )

    raise WeixinPluginError(
        error_code="unsupported_action",
        detail=f"Unsupported channel action: {action}",
        field="action",
    )


def _handle_probe(
    *,
    channel_account_id: str,
    session,
    store: RuntimeStateStore,
) -> dict[str, Any]:
    if session is None or session.status != "active" or not session.token:
        return {
            "probe_status": "error",
            "message": "weixin channel is not logged in",
            "channel_account_id": channel_account_id,
            "login_status": "not_logged_in" if session is None else session.status,
            "runtime": {
                "working_dir": str(store.context.working_dir),
                "runtime_db_path": str(store.context.runtime_db_path),
            },
        }
    return {
        "probe_status": "ok",
        "message": "weixin channel session is active",
        "channel_account_id": channel_account_id,
        "login_status": session.status,
        "runtime": {
            "working_dir": str(store.context.working_dir),
            "runtime_db_path": str(store.context.runtime_db_path),
        },
    }


def _handle_poll(
    *,
    data: dict[str, Any],
    channel_account_id: str,
    session,
    store: RuntimeStateStore,
) -> dict[str, Any]:
    checkpoint = store.get_poll_checkpoint(channel_account_id)
    poll_state = data.get("poll_state") if isinstance(data.get("poll_state"), dict) else {}
    last_known_event_id = _optional_text(
        checkpoint.latest_external_event_id if checkpoint is not None else poll_state.get("latest_external_event_id")
    )
    bridge_payload = dict(data)
    bridge_payload["channel_account_id"] = channel_account_id
    bridge_payload["transport"] = _build_transport_payload(session)
    bridge_payload["transport_state"] = {
        "cursor": checkpoint.cursor if checkpoint is not None else None,
    }

    result = invoke_transport(kind="channel", action="poll", payload=bridge_payload)
    if not isinstance(result, dict):
        raise WeixinPluginError(
            error_code="bridge_protocol_error",
            detail="weixin claw channel bridge result must be a JSON object",
        )

    raw_messages = result.get("messages")
    if not isinstance(raw_messages, list):
        raw_messages = []

    seen_event_ids: set[str] = set()
    events: list[dict[str, Any]] = []
    latest_external_event_id = last_known_event_id
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue
        event = _build_event_from_message(raw_message)
        if event is None:
            continue
        external_event_id = event["external_event_id"]
        if external_event_id in seen_event_ids:
            continue
        seen_event_ids.add(external_event_id)
        latest_external_event_id = external_event_id
        events.append(event)
        _persist_context_token_from_event(
            store=store,
            channel_account_id=channel_account_id,
            event=event,
        )

    transport_cursor = _optional_text(result.get("transport_cursor"))
    store.set_poll_checkpoint(
        channel_account_id,
        cursor=transport_cursor,
        latest_external_event_id=latest_external_event_id,
    )
    return {
        "message": _optional_text(result.get("message")) or "weixin polling completed",
        "events": events,
        "next_cursor": latest_external_event_id,
    }


def _handle_send(
    *,
    data: dict[str, Any],
    channel_account_id: str,
    session,
    store: RuntimeStateStore,
) -> dict[str, Any]:
    delivery = data.get("delivery")
    if not isinstance(delivery, dict):
        raise WeixinPluginError(
            error_code="invalid_delivery",
            detail="weixin delivery payload is missing",
            field="delivery",
        )

    text = _optional_text(delivery.get("text"))
    attachments = delivery.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    if text is None and not attachments:
        raise WeixinPluginError(
            error_code="invalid_delivery",
            detail="delivery.text or delivery.attachments is required",
            field="delivery",
        )

    conversation_key = _require_text(
        delivery.get("external_conversation_key"),
        field="delivery.external_conversation_key",
    )
    metadata = delivery.get("metadata") if isinstance(delivery.get("metadata"), dict) else {}
    external_user_id = _resolve_external_user_id(conversation_key=conversation_key, metadata=metadata)
    context_token = _resolve_context_token(
        store=store,
        channel_account_id=channel_account_id,
        conversation_key=conversation_key,
        external_user_id=external_user_id,
        metadata=metadata,
    )

    bridge_payload = dict(data)
    bridge_payload["channel_account_id"] = channel_account_id
    bridge_payload["transport"] = _build_transport_payload(session)
    bridge_payload["delivery"] = {
        **delivery,
        "external_user_id": external_user_id,
        "context_token": context_token,
        "attachments": attachments,
    }

    try:
        result = invoke_transport(kind="channel", action="send", payload=bridge_payload)
    except WeixinBridgeError as exc:
        if exc.error_code == "context_token_invalid":
            store.save_context_token(
                channel_account_id=channel_account_id,
                conversation_key=conversation_key,
                external_user_id=external_user_id,
                token=context_token,
                status="invalid",
            )
        store.record_delivery_receipt(
            channel_account_id=channel_account_id,
            provider_message_ref=_optional_text(delivery.get("delivery_id")) or conversation_key,
            status="failed",
            error_code=exc.error_code,
            error_message=exc.detail,
        )
        raise

    if not isinstance(result, dict):
        raise WeixinPluginError(
            error_code="bridge_protocol_error",
            detail="weixin claw channel bridge result must be a JSON object",
        )

    provider_message_ref = _optional_text(result.get("provider_message_ref"))
    if provider_message_ref is None:
        provider_message_ref = _optional_text(delivery.get("delivery_id")) or conversation_key

    store.record_delivery_receipt(
        channel_account_id=channel_account_id,
        provider_message_ref=provider_message_ref,
        status="sent",
    )
    store.save_context_token(
        channel_account_id=channel_account_id,
        conversation_key=conversation_key,
        external_user_id=external_user_id,
        token=context_token,
        status="fresh",
    )
    return {
        "provider_message_ref": provider_message_ref,
    }


def _persist_context_token_from_event(
    *,
    store: RuntimeStateStore,
    channel_account_id: str,
    event: dict[str, Any],
) -> None:
    normalized_payload = event.get("normalized_payload")
    if not isinstance(normalized_payload, dict):
        return
    metadata = normalized_payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    context_token = _optional_text(metadata.get("weixin_context_token"))
    external_user_id = _optional_text(event.get("external_user_id"))
    conversation_key = _optional_text(event.get("external_conversation_key"))
    if context_token is None or external_user_id is None or conversation_key is None:
        return
    store.save_context_token(
        channel_account_id=channel_account_id,
        conversation_key=conversation_key,
        external_user_id=external_user_id,
        token=context_token,
        status="fresh",
    )


def _build_event_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    external_user_id = _optional_text(message.get("from_user_id"))
    if external_user_id is None:
        return None
    event_id = _message_event_id(message)
    if event_id is None:
        return None

    normalized_payload = _build_normalized_payload(message=message, external_user_id=external_user_id)
    if normalized_payload is None:
        return None

    return {
        "external_event_id": event_id,
        "event_type": "message",
        "external_user_id": external_user_id,
        "external_conversation_key": f"direct:{external_user_id}",
        "normalized_payload": normalized_payload,
        "status": "received",
        "received_at": _coerce_iso_datetime(message.get("create_time_ms")),
    }


def _build_normalized_payload(
    *,
    message: dict[str, Any],
    external_user_id: str,
) -> dict[str, Any] | None:
    item_list = message.get("item_list")
    if not isinstance(item_list, list):
        item_list = []

    text_parts: list[str] = []
    raw_media_types: list[str] = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        item_type = _coerce_int(item.get("type"))
        if item_type == 1:
            text_value = _extract_text_item(item)
            if text_value is not None:
                text_parts.append(text_value)
        elif item_type == 2:
            raw_media_types.append("image")
        elif item_type == 3:
            voice_text = _extract_voice_text(item)
            if voice_text is not None:
                text_parts.append(voice_text)
            raw_media_types.append("audio")
        elif item_type == 4:
            raw_media_types.append("file")
        elif item_type == 5:
            raw_media_types.append("video")

    attachments = _normalize_downloaded_attachments(message.get("downloaded_attachments"))
    download_errors = _normalize_download_errors(message.get("download_errors"))

    text = "\n".join(part for part in text_parts if part).strip()
    if not text and attachments:
        text = _fallback_text_for_media(attachments)
    if not text and raw_media_types:
        text = _fallback_text_for_media([{"kind": raw_media_types[0]}])
    if not text:
        return None

    message_id = _optional_text(message.get("message_id"))
    session_id = _optional_text(message.get("session_id"))
    metadata: dict[str, Any] = {
        "external_user_id": external_user_id,
        "message_id": message_id,
        "session_id": session_id,
        "weixin_context_token": _optional_text(message.get("context_token")),
        "attachments": attachments,
        "media_download_errors": download_errors,
        "raw_media_types": raw_media_types,
    }
    payload = {
        "text": text[:4000],
        "chat_type": "direct",
        "external_message_id": message_id,
        "thread_key": None,
        "sender_display_name": None,
        "metadata": metadata,
    }
    if attachments:
        payload["attachments"] = attachments
    return payload


def _extract_text_item(item: dict[str, Any]) -> str | None:
    text_item = item.get("text_item")
    if not isinstance(text_item, dict):
        return None
    return _optional_text(text_item.get("text"))


def _extract_voice_text(item: dict[str, Any]) -> str | None:
    voice_item = item.get("voice_item")
    if not isinstance(voice_item, dict):
        return None
    return _optional_text(voice_item.get("text"))


def _fallback_text_for_media(media_items: list[dict[str, Any]]) -> str:
    first_type = _optional_text(media_items[0].get("kind")) or _optional_text(media_items[0].get("type")) or "media"
    mapping = {
        "image": "[图片]",
        "audio": "[语音]",
        "voice": "[语音]",
        "file": "[文件]",
        "video": "[视频]",
    }
    return mapping.get(first_type, "[媒体消息]")


def _message_event_id(message: dict[str, Any]) -> str | None:
    for key in ("message_id", "seq"):
        value = _optional_text(message.get(key))
        if value is not None:
            return value
        coerced = _coerce_int(message.get(key))
        if coerced is not None:
            return str(coerced)
    item_list = message.get("item_list")
    if isinstance(item_list, list):
        for item in item_list:
            if not isinstance(item, dict):
                continue
            value = _optional_text(item.get("msg_id"))
            if value is not None:
                return value
    return None


def _build_transport_payload(session) -> dict[str, Any]:
    return {
        "api_base_url": session.api_base_url,
        "token": session.token,
        "provider_account_id": session.provider_account_id,
    }


def _resolve_context_token(
    *,
    store: RuntimeStateStore,
    channel_account_id: str,
    conversation_key: str,
    external_user_id: str,
    metadata: dict[str, Any],
) -> str:
    metadata_token = _optional_text(metadata.get("weixin_context_token"))
    if metadata_token is not None:
        return metadata_token
    token_state = store.get_context_token(
        channel_account_id=channel_account_id,
        conversation_key=conversation_key,
        external_user_id=external_user_id,
    )
    if token_state is None or token_state.status == "invalid":
        raise WeixinPluginError(
            error_code="context_token_missing",
            detail="no persisted weixin context_token is available for this conversation",
            field="delivery.metadata",
        )
    return token_state.token


def _resolve_external_user_id(*, conversation_key: str, metadata: dict[str, Any]) -> str:
    metadata_user = _optional_text(metadata.get("external_user_id"))
    if metadata_user is not None:
        return metadata_user
    if conversation_key.startswith("direct:"):
        return conversation_key.removeprefix("direct:")
    raise WeixinPluginError(
        error_code="invalid_delivery",
        detail="weixin direct conversation key is required",
        field="delivery.external_conversation_key",
    )


def _require_active_session(session, *, channel_account_id: str):
    if session is None or session.status != "active" or not session.token:
        raise WeixinPluginError(
            error_code="login_required",
            detail=f"weixin account {channel_account_id} is not logged in",
            field="channel_account_id",
        )
    return session


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


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _require_text(value: Any, *, field: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise WeixinPluginError(
            error_code="invalid_payload",
            detail=f"{field} is required",
            field=field,
        )
    return normalized


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_iso_datetime(value: Any) -> str | None:
    milliseconds = _coerce_int(value)
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def _normalize_downloaded_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    attachments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = _optional_text(item.get("kind"))
        source_path = _optional_text(item.get("source_path"))
        if kind is None or source_path is None:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        attachments.append(
            {
                "kind": kind,
                "file_name": _optional_text(item.get("file_name")),
                "content_type": _optional_text(item.get("content_type")),
                "source_path": source_path,
                "size_bytes": _coerce_int(item.get("size_bytes")),
                "metadata": metadata,
            }
        )
    return attachments


def _normalize_download_errors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    errors: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        error_code = _optional_text(item.get("error_code"))
        detail = _optional_text(item.get("detail"))
        if error_code is None or detail is None:
            continue
        normalized: dict[str, Any] = {
            "error_code": error_code,
            "detail": detail,
        }
        item_type = _coerce_int(item.get("item_type"))
        if item_type is not None:
            normalized["item_type"] = item_type
        errors.append(normalized)
    return errors
