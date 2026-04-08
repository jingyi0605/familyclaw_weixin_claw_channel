from __future__ import annotations

from typing import Any

from .models import BridgeRequest, WeixinBridgeError, WeixinBridgeProtocolError
from .python_transport import dispatch_transport_request
from .runtime_state import build_runtime_context


DEFAULT_NODE_TIMEOUT_SECONDS = 20
CHANNEL_POLL_TIMEOUT_SECONDS = 50
CHANNEL_SEND_TIMEOUT_SECONDS = 60


def invoke_transport(*, kind: str, action: str, payload: dict[str, Any] | None = None) -> Any:
    """通过统一请求结构调用插件内部 transport。

    这层现在不再负责启动 Node 进程，只保留一件事：
    把 action/channel 的标准请求包装好，再交给 Python transport 实现。
    """

    request_payload = payload or {}
    runtime_context = build_runtime_context(request_payload)
    request = BridgeRequest(
        kind=kind,
        action=action,
        payload=request_payload,
        runtime=runtime_context.to_payload(),
    )

    result = dispatch_transport_request(request)
    if kind == "channel" and action == "poll" and isinstance(result, dict):
        return result
    if kind == "channel" and action == "send" and isinstance(result, dict):
        return result
    if kind == "action" and isinstance(result, dict):
        return result
    if kind == "channel" and action in {"probe", "webhook"} and isinstance(result, dict):
        return result
    raise WeixinBridgeProtocolError("Python transport did not return a JSON object")


def _parse_bridge_response(*, completed: object) -> Any:
    raise WeixinBridgeProtocolError("Node bridge has been removed; use Python transport instead")


def _raise_structured_error(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    if not isinstance(error, dict):
        raise WeixinBridgeProtocolError("transport error payload is invalid")
    error_code = str(error.get("code") or "bridge_protocol_error").strip() or "bridge_protocol_error"
    detail = str(error.get("message") or "transport failed").strip() or "transport failed"
    field = error.get("field")
    raise WeixinBridgeError(
        error_code=error_code,
        detail=detail,
        field=field.strip() if isinstance(field, str) and field.strip() else None,
    )


def _resolve_timeout_seconds(*, kind: str, action: str) -> int:
    if kind == "channel" and action == "poll":
        return CHANNEL_POLL_TIMEOUT_SECONDS
    if kind == "channel" and action == "send":
        return CHANNEL_SEND_TIMEOUT_SECONDS
    return DEFAULT_NODE_TIMEOUT_SECONDS
