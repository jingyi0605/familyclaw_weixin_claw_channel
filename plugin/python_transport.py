from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
from pathlib import Path
import re
import uuid
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import qrcode
from qrcode.image.svg import SvgPathImage

from .models import BridgeRequest, WeixinBridgeError
from .weixin_api_client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_TIMEOUT_MS,
    DEFAULT_CDN_BASE_URL,
    fetch_attachment_source,
    fetch_updates,
    fetch_upload_url,
    get_bot_qrcode,
    get_qrcode_status,
    send_weixin_message,
    upload_cdn_buffer,
    download_cdn_buffer,
)


DEFAULT_BOT_TYPE = "3"
LOGIN_QR_TTL_MS = 5 * 60_000
_SAFE_FILE_NAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

_EXTENSION_TO_MIME = {
    ".bin": "application/octet-stream",
    ".bmp": "image/bmp",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".gif": "image/gif",
    ".gz": "application/gzip",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".sil": "audio/silk",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}

_MIME_TO_EXTENSION = {
    "application/gzip": ".gz",
    "application/msword": ".doc",
    "application/octet-stream": ".bin",
    "application/pdf": ".pdf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/zip": ".zip",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/silk": ".sil",
    "audio/wav": ".wav",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}

_UPLOAD_MEDIA_TYPE = {
    "image": 1,
    "video": 2,
    "audio": 3,
    "file": 3,
}


def dispatch_transport_request(request: BridgeRequest) -> Any:
    payload = request.payload if isinstance(request.payload, dict) else {}
    runtime = request.runtime if isinstance(request.runtime, dict) else {}
    testing = payload.get("testing") if isinstance(payload.get("testing"), dict) else {}

    _maybe_raise_forced_error(testing)

    if request.kind == "action":
        stubbed = _resolve_stubbed_response(testing, request.action)
        if stubbed is not None:
            return {
                "action_name": request.action,
                "channel_account_id": _resolve_channel_account_id(payload),
                **stubbed,
            }
        return _handle_action_request(action=request.action, payload=payload)

    if request.kind == "channel":
        stubbed = _resolve_stubbed_response(testing, request.action)
        if stubbed is not None:
            return stubbed
        if _should_use_mock_channel_transport(action=request.action, payload=payload):
            return _run_mock_transport(action=request.action, payload=payload)
        return _handle_channel_request(action=request.action, payload=payload, runtime=runtime)

    raise WeixinBridgeError(
        error_code="bridge_request_invalid",
        detail=f"Unsupported request kind: {request.kind}",
        field="kind",
    )


def _handle_action_request(*, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    channel_account_id = _resolve_channel_account_id(payload)
    if action == "start_login":
        return _start_login(payload=payload, channel_account_id=channel_account_id)
    if action == "get_login_status":
        return _get_login_status(payload=payload, channel_account_id=channel_account_id)
    if action in {"logout", "purge_runtime_state"}:
        return {
            "action_name": action,
            "channel_account_id": channel_account_id,
            "status": "accepted",
            "message": "登录态清理由 Python 插件处理。",
        }
    raise WeixinBridgeError(
        error_code="unsupported_action",
        detail=f"Unsupported action request: {action}",
        field="action",
    )


def _start_login(*, payload: dict[str, Any], channel_account_id: str) -> dict[str, Any]:
    transport = _resolve_transport_config(payload)
    response = get_bot_qrcode(
        api_base_url=transport["api_base_url"],
        bot_type=transport["bot_type"],
        route_tag=transport["route_tag"],
    )
    qr_code = _as_text(response.get("qrcode"))
    qr_target_url = _as_text(response.get("qrcode_img_content"))
    if qr_code is None or qr_target_url is None:
        raise WeixinBridgeError(
            error_code="bridge_protocol_error",
            detail="微信登录二维码响应缺少必要字段。",
        )

    return {
        "action_name": "start_login",
        "channel_account_id": channel_account_id,
        "login_status": "waiting_scan",
        "login_session_key": _as_text(payload.get("login_session_key")) or channel_account_id or str(uuid.uuid4()),
        "login_qrcode": qr_code,
        "qr_code_url": _build_qr_preview_data_url(qr_target_url),
        "api_base_url": transport["api_base_url"],
        "expires_at": _future_iso(milliseconds=LOGIN_QR_TTL_MS),
        "message": "二维码已生成，请使用微信扫码继续登录。",
    }


def _get_login_status(*, payload: dict[str, Any], channel_account_id: str) -> dict[str, Any]:
    login_qrcode = _as_text(payload.get("login_qrcode"))
    login_session_key = _as_text(payload.get("login_session_key")) or channel_account_id
    if login_qrcode is None:
        return {
            "action_name": "get_login_status",
            "channel_account_id": channel_account_id,
            "login_status": "not_logged_in",
            "login_session_key": login_session_key,
            "message": "当前没有待扫码登录会话。",
        }

    transport = _resolve_transport_config(payload)
    response = get_qrcode_status(
        api_base_url=transport["api_base_url"],
        qrcode=login_qrcode,
        route_tag=transport["route_tag"],
    )
    login_status = _map_login_status(_as_text(response.get("status")) or "wait")

    return {
        "action_name": "get_login_status",
        "channel_account_id": channel_account_id,
        "login_session_key": login_session_key,
        "login_status": login_status,
        "token": _as_nullable_text(response.get("bot_token")),
        "provider_account_id": _as_nullable_text(response.get("ilink_bot_id")),
        "api_base_url": _as_text(response.get("baseurl")) or transport["api_base_url"],
        "user_id": _as_nullable_text(response.get("ilink_user_id")),
        "message": _build_login_status_message(login_status),
    }


def _handle_channel_request(*, action: str, payload: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    if action == "poll":
        return _poll_messages(payload=payload, runtime=runtime)
    if action == "send":
        return _send_delivery(payload=payload, runtime=runtime)
    if action == "probe":
        return _probe_channel(payload=payload)
    if action == "webhook":
        return {
            "message": "weixin claw channel currently runs in polling mode",
            "http_response": {
                "status_code": 202,
                "body_text": "polling-only",
                "media_type": "text/plain",
            },
        }
    raise WeixinBridgeError(
        error_code="unsupported_action",
        detail=f"Unsupported channel action: {action}",
        field="action",
    )


def _poll_messages(*, payload: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    transport = _resolve_transport_config(payload)
    _ensure_token_present(transport["token"])
    transport_state = payload.get("transport_state") if isinstance(payload.get("transport_state"), dict) else {}
    response = fetch_updates(
        api_base_url=transport["api_base_url"],
        token=transport["token"],
        cursor=_as_nullable_text(transport_state.get("cursor")),
    )
    if _coerce_int(response.get("errcode")) == -14:
        raise WeixinBridgeError(
            error_code="login_expired",
            detail=_as_text(response.get("errmsg")) or "weixin login expired",
        )

    raw_messages = response.get("msgs")
    if not isinstance(raw_messages, list):
        raw_messages = []

    messages: list[dict[str, Any]] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue
        messages.append(
            _enrich_inbound_message(
                message=raw_message,
                runtime=runtime,
                cdn_base_url=transport["cdn_base_url"],
            )
        )

    return {
        "message": "weixin polling completed",
        "messages": messages,
        "transport_cursor": _as_nullable_text(response.get("get_updates_buf")),
        "longpolling_timeout_ms": _coerce_int(response.get("longpolling_timeout_ms")),
    }


def _send_delivery(*, payload: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    transport = _resolve_transport_config(payload)
    _ensure_token_present(transport["token"])

    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
    to_user_id = _as_text(delivery.get("external_user_id"))
    text = _as_nullable_text(delivery.get("text"))
    context_token = _as_text(delivery.get("context_token"))
    attachments = delivery.get("attachments") if isinstance(delivery.get("attachments"), list) else []
    if to_user_id is None:
        raise WeixinBridgeError(
            error_code="invalid_delivery",
            detail="delivery.external_user_id is required",
            field="delivery.external_user_id",
        )
    if text is None and not attachments:
        raise WeixinBridgeError(
            error_code="invalid_delivery",
            detail="delivery.text or delivery.attachments is required",
            field="delivery",
        )
    if context_token is None:
        raise WeixinBridgeError(
            error_code="context_token_missing",
            detail="delivery.context_token is required",
            field="delivery.context_token",
        )

    provider_message_ref: str | None = None
    if text is not None:
        provider_message_ref = _send_text_only_message(
            transport=transport,
            to_user_id=to_user_id,
            context_token=context_token,
            text=text,
        )

    for attachment in attachments:
        provider_message_ref = _send_attachment_message(
            transport=transport,
            runtime=runtime,
            to_user_id=to_user_id,
            context_token=context_token,
            attachment=attachment,
        )

    return {
        "provider_message_ref": provider_message_ref or _as_text(delivery.get("delivery_id")) or str(uuid.uuid4()),
        "message": "weixin send completed",
    }


def _probe_channel(*, payload: dict[str, Any]) -> dict[str, Any]:
    transport = _resolve_transport_config(payload)
    _ensure_token_present(transport["token"])
    return {
        "probe_status": "ok",
        "message": "weixin channel session is active",
    }


def _enrich_inbound_message(*, message: dict[str, Any], runtime: dict[str, Any], cdn_base_url: str) -> dict[str, Any]:
    downloaded: list[dict[str, Any]] = []
    download_errors: list[dict[str, Any]] = []
    item_list = message.get("item_list")
    if not isinstance(item_list, list):
        item_list = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        try:
            attachment = _download_attachment_from_item(item=item, runtime=runtime, cdn_base_url=cdn_base_url)
        except WeixinBridgeError as exc:
            download_errors.append(
                {
                    "error_code": "media_download_failed",
                    "detail": exc.detail,
                    "item_type": _coerce_int(item.get("type")),
                }
            )
            continue
        if attachment is not None:
            downloaded.append(attachment)
    return {
        **message,
        "downloaded_attachments": downloaded,
        "download_errors": download_errors,
    }


def _download_attachment_from_item(
    *,
    item: dict[str, Any],
    runtime: dict[str, Any],
    cdn_base_url: str,
) -> dict[str, Any] | None:
    item_type = _coerce_int(item.get("type"))
    if item_type == 2:
        return _download_image_attachment(item=item, runtime=runtime, cdn_base_url=cdn_base_url)
    if item_type == 3:
        return _download_voice_attachment(item=item, runtime=runtime, cdn_base_url=cdn_base_url)
    if item_type == 4:
        return _download_file_attachment(item=item, runtime=runtime, cdn_base_url=cdn_base_url)
    if item_type == 5:
        return _download_video_attachment(item=item, runtime=runtime, cdn_base_url=cdn_base_url)
    return None


def _download_image_attachment(*, item: dict[str, Any], runtime: dict[str, Any], cdn_base_url: str) -> dict[str, Any] | None:
    image_item = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
    media = image_item.get("media") if isinstance(image_item.get("media"), dict) else {}
    encrypt_query_param = _as_text(media.get("encrypt_query_param"))
    if encrypt_query_param is None:
        return None
    aes_key = _resolve_inbound_aes_key(
        raw_hex_key=_as_nullable_text(image_item.get("aeskey")),
        encoded_key=_as_nullable_text(media.get("aes_key")),
    )
    if aes_key is None:
        buffer = download_cdn_buffer(cdn_base_url=cdn_base_url, encrypted_query_param=encrypt_query_param)
    else:
        buffer = _download_and_decrypt_buffer(
            cdn_base_url=cdn_base_url,
            encrypted_query_param=encrypt_query_param,
            aes_key=aes_key,
        )
    saved = _save_buffer_to_media_dir(
        buffer=buffer,
        media_dir=_resolve_runtime_media_dir(runtime),
        bucket="inbound",
        preferred_name="image.jpg",
        content_type="image/jpeg",
    )
    return {
        "kind": "image",
        "file_name": Path(saved["path"]).name,
        "content_type": "image/jpeg",
        "source_path": saved["path"],
        "size_bytes": saved["size_bytes"],
        "metadata": {
            "weixin_item_type": "image",
            "mid_size": _coerce_int(image_item.get("mid_size")),
        },
    }


def _download_voice_attachment(*, item: dict[str, Any], runtime: dict[str, Any], cdn_base_url: str) -> dict[str, Any] | None:
    voice_item = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
    media = voice_item.get("media") if isinstance(voice_item.get("media"), dict) else {}
    encrypt_query_param = _as_text(media.get("encrypt_query_param"))
    aes_key = _decode_aes_key(_as_nullable_text(media.get("aes_key")))
    if encrypt_query_param is None or aes_key is None:
        return None
    buffer = _download_and_decrypt_buffer(
        cdn_base_url=cdn_base_url,
        encrypted_query_param=encrypt_query_param,
        aes_key=aes_key,
    )
    saved = _save_buffer_to_media_dir(
        buffer=buffer,
        media_dir=_resolve_runtime_media_dir(runtime),
        bucket="inbound",
        preferred_name="voice.sil",
        content_type="audio/silk",
    )
    return {
        "kind": "audio",
        "file_name": Path(saved["path"]).name,
        "content_type": "audio/silk",
        "source_path": saved["path"],
        "size_bytes": saved["size_bytes"],
        "metadata": {
            "weixin_item_type": "voice",
            "duration_ms": _coerce_int(voice_item.get("playtime")),
            "transcript": _as_nullable_text(voice_item.get("text")),
        },
    }


def _download_file_attachment(*, item: dict[str, Any], runtime: dict[str, Any], cdn_base_url: str) -> dict[str, Any] | None:
    file_item = item.get("file_item") if isinstance(item.get("file_item"), dict) else {}
    media = file_item.get("media") if isinstance(file_item.get("media"), dict) else {}
    encrypt_query_param = _as_text(media.get("encrypt_query_param"))
    aes_key = _decode_aes_key(_as_nullable_text(media.get("aes_key")))
    if encrypt_query_param is None or aes_key is None:
        return None
    preferred_name = _as_text(file_item.get("file_name")) or "file.bin"
    content_type = _infer_mime_from_file_name(preferred_name)
    buffer = _download_and_decrypt_buffer(
        cdn_base_url=cdn_base_url,
        encrypted_query_param=encrypt_query_param,
        aes_key=aes_key,
    )
    saved = _save_buffer_to_media_dir(
        buffer=buffer,
        media_dir=_resolve_runtime_media_dir(runtime),
        bucket="inbound",
        preferred_name=preferred_name,
        content_type=content_type,
    )
    return {
        "kind": "file",
        "file_name": Path(saved["path"]).name,
        "content_type": content_type,
        "source_path": saved["path"],
        "size_bytes": saved["size_bytes"],
        "metadata": {
            "weixin_item_type": "file",
            "original_file_name": preferred_name,
        },
    }


def _download_video_attachment(*, item: dict[str, Any], runtime: dict[str, Any], cdn_base_url: str) -> dict[str, Any] | None:
    video_item = item.get("video_item") if isinstance(item.get("video_item"), dict) else {}
    media = video_item.get("media") if isinstance(video_item.get("media"), dict) else {}
    encrypt_query_param = _as_text(media.get("encrypt_query_param"))
    aes_key = _decode_aes_key(_as_nullable_text(media.get("aes_key")))
    if encrypt_query_param is None or aes_key is None:
        return None
    buffer = _download_and_decrypt_buffer(
        cdn_base_url=cdn_base_url,
        encrypted_query_param=encrypt_query_param,
        aes_key=aes_key,
    )
    saved = _save_buffer_to_media_dir(
        buffer=buffer,
        media_dir=_resolve_runtime_media_dir(runtime),
        bucket="inbound",
        preferred_name="video.mp4",
        content_type="video/mp4",
    )
    return {
        "kind": "video",
        "file_name": Path(saved["path"]).name,
        "content_type": "video/mp4",
        "source_path": saved["path"],
        "size_bytes": saved["size_bytes"],
        "metadata": {
            "weixin_item_type": "video",
            "duration_ms": _coerce_int(video_item.get("play_length")),
        },
    }


def _send_text_only_message(*, transport: dict[str, Any], to_user_id: str, context_token: str, text: str) -> str:
    provider_message_ref = str(uuid.uuid4())
    send_weixin_message(
        api_base_url=transport["api_base_url"],
        token=transport["token"],
        message={
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": provider_message_ref,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [
                {
                    "type": 1,
                    "text_item": {
                        "text": text,
                    },
                }
            ],
        },
    )
    return provider_message_ref


def _send_attachment_message(
    *,
    transport: dict[str, Any],
    runtime: dict[str, Any],
    to_user_id: str,
    context_token: str,
    attachment: Any,
) -> str:
    prepared_attachment = _load_attachment_source(attachment=attachment, runtime=runtime)
    upload_media_type = _UPLOAD_MEDIA_TYPE[prepared_attachment["kind"]]
    raw_file_md5 = hashlib.md5(prepared_attachment["buffer"]).hexdigest()
    encrypted_size = _aes_ecb_padded_size(len(prepared_attachment["buffer"]))
    file_key = uuid.uuid4().hex
    aes_key = uuid.uuid4().bytes[:16]

    upload_response = fetch_upload_url(
        api_base_url=transport["api_base_url"],
        token=transport["token"],
        file_key=file_key,
        media_type=upload_media_type,
        to_user_id=to_user_id,
        raw_size=len(prepared_attachment["buffer"]),
        raw_file_md5=raw_file_md5,
        encrypted_size=encrypted_size,
        aes_key_hex=aes_key.hex(),
    )
    upload_param = _as_text(upload_response.get("upload_param"))
    if upload_param is None:
        raise WeixinBridgeError(
            error_code="media_upload_failed",
            detail="微信上传地址响应缺少 upload_param。",
            field="delivery.attachments",
        )

    ciphertext = _encrypt_aes_ecb(prepared_attachment["buffer"], aes_key)
    download_param = upload_cdn_buffer(
        cdn_base_url=transport["cdn_base_url"],
        upload_param=upload_param,
        file_key=file_key,
        ciphertext=ciphertext,
    )

    provider_message_ref = str(uuid.uuid4())
    send_weixin_message(
        api_base_url=transport["api_base_url"],
        token=transport["token"],
        message={
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": provider_message_ref,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [
                _build_outbound_message_item(
                    prepared_attachment=prepared_attachment,
                    download_param=download_param,
                    aes_key=aes_key,
                )
            ],
        },
    )
    return provider_message_ref


def _load_attachment_source(*, attachment: Any, runtime: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(attachment, dict):
        raise WeixinBridgeError(
            error_code="invalid_delivery",
            detail="delivery attachment item is invalid",
            field="delivery.attachments",
        )
    kind = _as_text(attachment.get("kind"))
    if kind is None or kind not in _UPLOAD_MEDIA_TYPE:
        raise WeixinBridgeError(
            error_code="invalid_delivery",
            detail="unsupported delivery attachment kind",
            field="delivery.attachments.kind",
        )

    source_path = _as_nullable_text(attachment.get("source_path"))
    source_url = _as_nullable_text(attachment.get("source_url"))
    if source_path is None and source_url is None:
        raise WeixinBridgeError(
            error_code="invalid_delivery",
            detail="delivery attachment requires source_path or source_url",
            field="delivery.attachments",
        )

    if source_path is not None:
        try:
            buffer = Path(source_path).read_bytes()
        except OSError as exc:
            raise WeixinBridgeError(
                error_code="media_upload_failed",
                detail=f"failed to read attachment source_path: {exc}",
                field="delivery.attachments.source_path",
            ) from exc
        preferred_name = _sanitize_file_name(
            _as_nullable_text(attachment.get("file_name")) or Path(source_path).name,
            _infer_extension(file_name=source_path, content_type=_as_nullable_text(attachment.get("content_type"))),
        )
        return {
            "kind": kind,
            "buffer": buffer,
            "file_name": preferred_name,
            "content_type": _as_nullable_text(attachment.get("content_type")) or _infer_mime_from_file_name(preferred_name),
        }

    assert source_url is not None
    buffer, response_content_type = fetch_attachment_source(source_url=source_url)
    preferred_name = _sanitize_file_name(
        _as_nullable_text(attachment.get("file_name")),
        _infer_extension(
            file_name=source_url,
            content_type=response_content_type or _as_nullable_text(attachment.get("content_type")),
        ),
    )
    _save_buffer_to_media_dir(
        buffer=buffer,
        media_dir=_resolve_runtime_media_dir(runtime),
        bucket="outbound-cache",
        preferred_name=preferred_name,
        content_type=response_content_type or _as_nullable_text(attachment.get("content_type")),
    )
    return {
        "kind": kind,
        "buffer": buffer,
        "file_name": preferred_name,
        "content_type": _as_nullable_text(attachment.get("content_type"))
        or response_content_type
        or _infer_mime_from_file_name(preferred_name),
    }


def _build_outbound_message_item(
    *,
    prepared_attachment: dict[str, Any],
    download_param: str,
    aes_key: bytes,
) -> dict[str, Any]:
    common_media = {
        "encrypt_query_param": download_param,
        "aes_key": _encode_aes_key_for_message(aes_key),
        "encrypt_type": 1,
    }
    if prepared_attachment["kind"] == "image":
        return {
            "type": 2,
            "image_item": {
                "media": common_media,
                "mid_size": _aes_ecb_padded_size(len(prepared_attachment["buffer"])),
            },
        }
    if prepared_attachment["kind"] == "video":
        return {
            "type": 5,
            "video_item": {
                "media": common_media,
                "video_size": _aes_ecb_padded_size(len(prepared_attachment["buffer"])),
            },
        }
    return {
        "type": 4,
        "file_item": {
            "media": common_media,
            "file_name": prepared_attachment["file_name"],
            "len": str(len(prepared_attachment["buffer"])),
        },
    }


def _build_qr_preview_data_url(qr_target_url: str) -> str:
    normalized = qr_target_url.strip()
    if not normalized:
        raise WeixinBridgeError(
            error_code="bridge_protocol_error",
            detail="二维码预览目标地址不能为空。",
            field="qrcode_img_content",
        )
    qr_builder = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr_builder.add_data(normalized)
    qr_builder.make(fit=True)
    image = qr_builder.make_image(image_factory=SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return f"data:image/svg+xml;charset=utf-8,{quote(svg)}"


def _download_and_decrypt_buffer(*, cdn_base_url: str, encrypted_query_param: str, aes_key: bytes) -> bytes:
    encrypted = download_cdn_buffer(
        cdn_base_url=cdn_base_url,
        encrypted_query_param=encrypted_query_param,
    )
    return _decrypt_aes_ecb(encrypted, aes_key)


def _save_buffer_to_media_dir(
    *,
    buffer: bytes,
    media_dir: Path,
    bucket: str,
    preferred_name: str,
    content_type: str | None,
) -> dict[str, Any]:
    extension = _infer_extension(file_name=preferred_name, content_type=content_type)
    safe_name = _sanitize_file_name(preferred_name, extension)
    target_dir = media_dir / bucket
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid.uuid4()}-{safe_name}"
    target_path.write_bytes(buffer)
    return {
        "path": str(target_path),
        "size_bytes": len(buffer),
    }


def _resolve_transport_config(payload: dict[str, Any]) -> dict[str, Any]:
    transport = payload.get("transport") if isinstance(payload.get("transport"), dict) else {}
    return {
        "api_base_url": _as_text(transport.get("api_base_url"))
        or _as_text(transport.get("base_url"))
        or DEFAULT_API_BASE_URL,
        "cdn_base_url": _as_text(transport.get("cdn_base_url"))
        or _as_text(transport.get("cdnBaseUrl"))
        or DEFAULT_CDN_BASE_URL,
        "token": _as_nullable_text(transport.get("token")),
        "provider_account_id": _as_nullable_text(transport.get("provider_account_id")),
        "bot_type": _as_text(transport.get("bot_type"))
        or _as_text(transport.get("botType"))
        or DEFAULT_BOT_TYPE,
        "route_tag": _as_nullable_text(transport.get("route_tag")),
    }


def _should_use_mock_channel_transport(*, action: str, payload: dict[str, Any]) -> bool:
    if action not in {"poll", "send", "probe", "webhook"}:
        return False
    transport = payload.get("transport")
    if isinstance(transport, dict):
        return False
    testing = payload.get("testing") if isinstance(payload.get("testing"), dict) else {}
    responses = testing.get("transport_responses") if isinstance(testing.get("transport_responses"), dict) else {}
    return not (action in responses and isinstance(responses[action], dict))


def _run_mock_transport(*, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "poll":
        poll_state = payload.get("poll_state") if isinstance(payload.get("poll_state"), dict) else {}
        return {
            "events": [],
            "next_cursor": _as_nullable_text(poll_state.get("cursor")),
            "message": "weixin mock bridge poll is wired",
        }
    if action == "send":
        delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
        text = _as_text(delivery.get("text"))
        if text is None:
            raise WeixinBridgeError(
                error_code="invalid_delivery",
                detail="delivery.text is required",
                field="delivery.text",
            )
        return {
            "provider_message_ref": "mock-provider-message-ref",
            "message": "weixin mock bridge send is wired",
        }
    if action == "probe":
        return {
            "probe_status": "ok",
            "message": "weixin mock bridge probe is ready",
        }
    if action == "webhook":
        return {
            "message": "weixin claw channel currently runs in polling mode",
            "http_response": {
                "status_code": 202,
                "body_text": "polling-only",
                "media_type": "text/plain",
            },
        }
    raise WeixinBridgeError(
        error_code="unsupported_action",
        detail=f"Unsupported channel action: {action}",
        field="action",
    )


def _resolve_stubbed_response(testing: dict[str, Any], action: str) -> dict[str, Any] | None:
    responses = testing.get("transport_responses") if isinstance(testing.get("transport_responses"), dict) else {}
    response = responses.get(action)
    return response if isinstance(response, dict) else None


def _maybe_raise_forced_error(testing: dict[str, Any]) -> None:
    if testing.get("force_error") is True:
        raise WeixinBridgeError(
            error_code=_as_text(testing.get("error_code")) or "transport_mock_failure",
            detail=_as_text(testing.get("message")) or "forced mock failure",
            field=_as_nullable_text(testing.get("field")),
        )


def _resolve_channel_account_id(payload: dict[str, Any]) -> str:
    account = payload.get("account")
    if isinstance(account, dict):
        for key in ("id", "account_code", "account_label"):
            value = _as_text(account.get(key))
            if value is not None:
                return value
    channel_account_id = _as_text(payload.get("channel_account_id"))
    return channel_account_id or "unknown-account"


def _ensure_token_present(token: str | None) -> None:
    if token is None:
        raise WeixinBridgeError(
            error_code="login_required",
            detail="weixin transport token is missing",
            field="transport.token",
        )


def _map_login_status(raw_status: str) -> str:
    if raw_status == "confirmed":
        return "active"
    if raw_status == "scaned":
        return "scan_confirmed"
    if raw_status == "expired":
        return "expired"
    return "waiting_scan"


def _build_login_status_message(login_status: str) -> str:
    if login_status == "active":
        return "微信登录成功。"
    if login_status == "scan_confirmed":
        return "二维码已扫描，请在微信里确认登录。"
    if login_status == "expired":
        return "二维码已过期，请重新生成。"
    return "二维码已生成，等待扫码。"


def _resolve_inbound_aes_key(*, raw_hex_key: str | None, encoded_key: str | None) -> bytes | None:
    if raw_hex_key:
        try:
            return bytes.fromhex(raw_hex_key)
        except ValueError as exc:
            raise WeixinBridgeError(
                error_code="bridge_protocol_error",
                detail=f"invalid inbound aes hex key: {exc}",
            ) from exc
    return _decode_aes_key(encoded_key)


def _decode_aes_key(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        decoded = base64.b64decode(value)
    except Exception as exc:
        raise WeixinBridgeError(
            error_code="bridge_protocol_error",
            detail=f"invalid aes_key payload: {exc}",
        ) from exc
    if len(decoded) == 16:
        return decoded
    ascii_text = decoded.decode("ascii", errors="ignore")
    if len(decoded) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", ascii_text):
        return bytes.fromhex(ascii_text)
    raise WeixinBridgeError(
        error_code="bridge_protocol_error",
        detail=f"invalid aes_key payload length: {len(decoded)}",
    )


def _encode_aes_key_for_message(aes_key: bytes) -> str:
    return base64.b64encode(aes_key.hex().encode("utf-8")).decode("ascii")


def _aes_ecb_padded_size(plain_size: int) -> int:
    return ((plain_size + 1 + 15) // 16) * 16


def _encrypt_aes_ecb(plain_buffer: bytes, aes_key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain_buffer) + padder.finalize()
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _decrypt_aes_ecb(encrypted_buffer: bytes, aes_key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted_buffer) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _resolve_runtime_media_dir(runtime: dict[str, Any]) -> Path:
    media_dir = _as_text(runtime.get("media_dir"))
    return Path(media_dir) if media_dir is not None else Path.cwd() / "media"


def _infer_mime_from_file_name(file_name: str) -> str:
    extension = Path(file_name).suffix.lower()
    if extension in _EXTENSION_TO_MIME:
        return _EXTENSION_TO_MIME[extension]
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or "application/octet-stream"


def _infer_extension(*, file_name: str | None, content_type: str | None) -> str:
    extension = Path(file_name or "").suffix.lower()
    if extension in _EXTENSION_TO_MIME:
        return extension
    normalized_content_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_content_type in _MIME_TO_EXTENSION:
        return _MIME_TO_EXTENSION[normalized_content_type]
    guessed = mimetypes.guess_extension(normalized_content_type)
    if isinstance(guessed, str) and guessed:
        return guessed
    return ".bin"


def _sanitize_file_name(file_name: str | None, fallback_extension: str = ".bin") -> str:
    raw_name = file_name.strip() if isinstance(file_name, str) and file_name.strip() else f"media{fallback_extension}"
    base_name = Path(raw_name).name
    safe_name = _SAFE_FILE_NAME_PATTERN.sub("-", base_name)
    if Path(safe_name).suffix:
        return safe_name
    return f"{safe_name}{fallback_extension}"


def _future_iso(*, milliseconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(milliseconds=milliseconds)).isoformat()


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


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, int):
        return str(value)
    return None


def _as_nullable_text(value: Any) -> str | None:
    return _as_text(value)
