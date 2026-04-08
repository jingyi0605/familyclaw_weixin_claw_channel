from __future__ import annotations

import base64
import json
import secrets
from typing import Any
from urllib.parse import quote

import httpx

from .models import WeixinBridgeError


DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_API_TIMEOUT_MS = 20_000
DEFAULT_LOGIN_TIMEOUT_MS = 15_000
DEFAULT_QR_STATUS_TIMEOUT_MS = 35_000


def get_bot_qrcode(
    *,
    api_base_url: str,
    bot_type: str,
    route_tag: str | None = None,
) -> dict[str, Any]:
    url = (
        f"{_ensure_trailing_slash(api_base_url)}"
        f"ilink/bot/get_bot_qrcode?bot_type={quote(bot_type, safe='')}"
    )
    headers = {"iLink-App-ClientVersion": "1"}
    if route_tag:
        headers["SKRouteTag"] = route_tag
    return _request_json(
        method="GET",
        url=url,
        headers=headers,
        timeout_ms=DEFAULT_LOGIN_TIMEOUT_MS,
        error_code="transport_unavailable",
        error_message_prefix="微信登录二维码请求失败",
    )


def get_qrcode_status(
    *,
    api_base_url: str,
    qrcode: str,
    route_tag: str | None = None,
) -> dict[str, Any]:
    url = (
        f"{_ensure_trailing_slash(api_base_url)}"
        f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
    )
    headers = {"iLink-App-ClientVersion": "1"}
    if route_tag:
        headers["SKRouteTag"] = route_tag
    return _request_json(
        method="GET",
        url=url,
        headers=headers,
        timeout_ms=DEFAULT_QR_STATUS_TIMEOUT_MS,
        error_code="transport_unavailable",
        error_message_prefix="微信登录状态查询失败",
    )


def fetch_updates(*, api_base_url: str, token: str, cursor: str | None) -> dict[str, Any]:
    return _post_json(
        url=f"{_ensure_trailing_slash(api_base_url)}ilink/bot/getupdates",
        token=token,
        body={
            "get_updates_buf": cursor or "",
            "base_info": _build_base_info(),
        },
        timeout_ms=DEFAULT_LONG_POLL_TIMEOUT_MS,
        error_code="transport_unavailable",
        error_message_prefix="微信轮询请求失败",
    )


def fetch_upload_url(
    *,
    api_base_url: str,
    token: str,
    file_key: str,
    media_type: int,
    to_user_id: str,
    raw_size: int,
    raw_file_md5: str,
    encrypted_size: int,
    aes_key_hex: str,
) -> dict[str, Any]:
    return _post_json(
        url=f"{_ensure_trailing_slash(api_base_url)}ilink/bot/getuploadurl",
        token=token,
        body={
            "filekey": file_key,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_file_md5,
            "filesize": encrypted_size,
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
            "base_info": _build_base_info(),
        },
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
        error_code="media_upload_failed",
        error_message_prefix="微信媒体上传地址获取失败",
        field="delivery.attachments",
    )


def send_weixin_message(*, api_base_url: str, token: str, message: dict[str, Any]) -> dict[str, Any]:
    return _post_json(
        url=f"{_ensure_trailing_slash(api_base_url)}ilink/bot/sendmessage",
        token=token,
        body={
            "msg": message,
            "base_info": _build_base_info(),
        },
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
        error_code="transport_unavailable",
        error_message_prefix="微信消息发送失败",
    )


def upload_cdn_buffer(
    *,
    cdn_base_url: str,
    upload_param: str,
    file_key: str,
    ciphertext: bytes,
) -> str:
    url = (
        f"{_strip_trailing_slash(cdn_base_url)}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(file_key, safe='')}"
    )
    response = _request_bytes(
        method="POST",
        url=url,
        headers={"Content-Type": "application/octet-stream"},
        content=ciphertext,
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
        error_code="media_upload_failed",
        error_message_prefix="微信 CDN 上传失败",
        field="delivery.attachments",
    )
    download_param = response.headers.get("x-encrypted-param")
    if not isinstance(download_param, str) or not download_param.strip():
        raise WeixinBridgeError(
            error_code="media_upload_failed",
            detail="微信 CDN 上传成功，但响应缺少 x-encrypted-param。",
            field="delivery.attachments",
        )
    return download_param.strip()


def download_cdn_buffer(
    *,
    cdn_base_url: str,
    encrypted_query_param: str,
) -> bytes:
    url = (
        f"{_strip_trailing_slash(cdn_base_url)}/download"
        f"?encrypted_query_param={quote(encrypted_query_param, safe='')}"
    )
    response = _request_bytes(
        method="GET",
        url=url,
        headers=None,
        content=None,
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
        error_code="media_download_failed",
        error_message_prefix="微信 CDN 下载失败",
    )
    return response.content


def fetch_attachment_source(*, source_url: str) -> tuple[bytes, str | None]:
    response = _request_bytes(
        method="GET",
        url=source_url,
        headers=None,
        content=None,
        timeout_ms=DEFAULT_API_TIMEOUT_MS,
        error_code="media_upload_failed",
        error_message_prefix="附件来源地址读取失败",
        field="delivery.attachments.source_url",
    )
    return response.content, response.headers.get("content-type")


def _post_json(
    *,
    url: str,
    token: str,
    body: dict[str, Any],
    timeout_ms: int,
    error_code: str,
    error_message_prefix: str,
    field: str | None = None,
) -> dict[str, Any]:
    serialized = json.dumps(body, ensure_ascii=False)
    headers = _build_authorized_headers(token=token, body=serialized)
    return _request_json(
        method="POST",
        url=url,
        headers=headers,
        content=serialized.encode("utf-8"),
        timeout_ms=timeout_ms,
        error_code=error_code,
        error_message_prefix=error_message_prefix,
        field=field,
    )


def _request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    timeout_ms: int,
    error_code: str,
    error_message_prefix: str,
    field: str | None = None,
    content: bytes | None = None,
) -> dict[str, Any]:
    response = _request_bytes(
        method=method,
        url=url,
        headers=headers,
        content=content,
        timeout_ms=timeout_ms,
        error_code=error_code,
        error_message_prefix=error_message_prefix,
        field=field,
    )
    if not response.content:
        return {}
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise WeixinBridgeError(
            error_code="bridge_protocol_error",
            detail=f"{error_message_prefix}：上游返回的不是合法 JSON（{exc}）。",
            field=field,
        ) from exc
    if not isinstance(payload, dict):
        raise WeixinBridgeError(
            error_code="bridge_protocol_error",
            detail=f"{error_message_prefix}：上游返回的不是 JSON 对象。",
            field=field,
        )
    return payload


def _request_bytes(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    content: bytes | None,
    timeout_ms: int,
    error_code: str,
    error_message_prefix: str,
    field: str | None = None,
) -> httpx.Response:
    timeout_seconds = timeout_ms / 1000
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.request(
                method=method,
                url=url,
                headers=headers,
                content=content,
            )
    except httpx.TimeoutException as exc:
        raise WeixinBridgeError(
            error_code=error_code,
            detail=f"{error_message_prefix}：请求超时（{timeout_ms}ms）。",
            field=field,
        ) from exc
    except httpx.HTTPError as exc:
        raise WeixinBridgeError(
            error_code=error_code,
            detail=f"{error_message_prefix}：{exc}",
            field=field,
        ) from exc

    if response.is_success:
        return response

    body_text = _safe_response_text(response)
    detail = f"{error_message_prefix}：{response.status_code} {response.reason_phrase}"
    if body_text:
        detail = f"{detail} {body_text}"
    raise WeixinBridgeError(
        error_code=error_code,
        detail=detail,
        field=field,
    )


def _build_authorized_headers(*, token: str, body: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("ascii"),
    }


def _build_base_info() -> dict[str, str]:
    return {
        "channel_version": "familyclaw-plugin-dev",
    }


def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"


def _strip_trailing_slash(value: str) -> str:
    return value[:-1] if value.endswith("/") else value


def _safe_response_text(response: httpx.Response) -> str:
    try:
        text = response.text.strip()
    except Exception:
        return ""
    return text[:200]
