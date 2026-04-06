from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from .models import BridgeRequest, WeixinBridgeError, WeixinBridgeProtocolError
from .runtime_state import build_runtime_context


DEFAULT_NODE_TIMEOUT_SECONDS = 20
CHANNEL_POLL_TIMEOUT_SECONDS = 50
CHANNEL_SEND_TIMEOUT_SECONDS = 60


def invoke_transport(*, kind: str, action: str, payload: dict[str, Any] | None = None) -> Any:
    """通过统一 JSON 协议调用 Node transport。

    这里故意把边界做窄：
    - Python 只管发标准请求和收标准结果
    - Node 只管处理 transport 细节
    - 任何微信协议脏活都不往宿主外扩
    """

    request_payload = payload or {}
    runtime_context = build_runtime_context(request_payload)
    request = BridgeRequest(
        kind=kind,
        action=action,
        payload=request_payload,
        runtime=runtime_context.to_payload(),
    )

    completed = subprocess.run(
        ["node", str(_bridge_script_path())],
        input=json.dumps(request.to_payload(), ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=_resolve_timeout_seconds(kind=kind, action=action),
        cwd=str(_vendor_root()),
        check=False,
    )
    return _parse_bridge_response(completed=completed)


def _vendor_root() -> Path:
    return Path(__file__).resolve().parents[1] / "vendor" / "weixin_transport"


def _bridge_script_path() -> Path:
    return _vendor_root() / "bridge.mjs"


def _parse_bridge_response(*, completed: subprocess.CompletedProcess[str]) -> Any:
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        payload = _try_parse_json(stdout) or _try_parse_json(stderr)
        if isinstance(payload, dict):
            _raise_structured_error(payload)
        raise WeixinBridgeProtocolError(stderr or stdout or "Node bridge process failed")

    payload = _try_parse_json(stdout)
    if not isinstance(payload, dict):
        raise WeixinBridgeProtocolError("Node bridge did not return a JSON object")

    if payload.get("ok") is True:
        return payload.get("result")

    _raise_structured_error(payload)
    raise WeixinBridgeProtocolError("Node bridge returned an unknown response")


def _raise_structured_error(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    if not isinstance(error, dict):
        raise WeixinBridgeProtocolError("Node bridge error payload is invalid")
    error_code = str(error.get("code") or "bridge_protocol_error").strip() or "bridge_protocol_error"
    detail = str(error.get("message") or "Node bridge failed").strip() or "Node bridge failed"
    field = error.get("field")
    raise WeixinBridgeError(
        error_code=error_code,
        detail=detail,
        field=field.strip() if isinstance(field, str) and field.strip() else None,
    )


def _try_parse_json(value: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_timeout_seconds(*, kind: str, action: str) -> int:
    if kind == "channel" and action == "poll":
        return CHANNEL_POLL_TIMEOUT_SECONDS
    if kind == "channel" and action == "send":
        return CHANNEL_SEND_TIMEOUT_SECONDS
    return DEFAULT_NODE_TIMEOUT_SECONDS
