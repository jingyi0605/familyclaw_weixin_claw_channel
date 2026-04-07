from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .models import BridgeRequest, WeixinBridgeError, WeixinBridgeProtocolError, WeixinPluginError
from .runtime_state import build_runtime_context


DEFAULT_NODE_TIMEOUT_SECONDS = 20
CHANNEL_POLL_TIMEOUT_SECONDS = 50
CHANNEL_SEND_TIMEOUT_SECONDS = 60
WEIXIN_CLAW_NODE_PATH_ENV_VAR = "WEIXIN_CLAW_NODE_PATH"


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

    node_executable = _resolve_node_executable()
    try:
        completed = subprocess.run(
            [str(node_executable), str(_bridge_script_path())],
            input=json.dumps(request.to_payload(), ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=_resolve_timeout_seconds(kind=kind, action=action),
            cwd=str(_vendor_root()),
            check=False,
        )
    except FileNotFoundError as exc:
        raise _build_missing_node_runtime_error(explicit_path=node_executable) from exc
    return _parse_bridge_response(completed=completed)


def _vendor_root() -> Path:
    return Path(__file__).resolve().parents[1] / "vendor" / "weixin_transport"


def _bridge_script_path() -> Path:
    return _vendor_root() / "bridge.mjs"


def _resolve_node_executable() -> Path:
    override_path = _resolve_node_override_path()
    if override_path is not None:
        return override_path

    seen: set[str] = set()
    for candidate in _candidate_node_paths():
        normalized = os.path.normcase(str(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if _is_existing_file(candidate):
            return candidate

    raise _build_missing_node_runtime_error()


def _resolve_node_override_path() -> Path | None:
    raw_path = os.environ.get(WEIXIN_CLAW_NODE_PATH_ENV_VAR)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    candidate = Path(raw_path.strip()).expanduser()
    if _is_existing_file(candidate):
        return candidate
    raise _build_missing_node_runtime_error(explicit_path=candidate)


def _candidate_node_paths() -> list[Path]:
    candidates: list[Path] = []
    command_names = ["node"]
    if os.name != "nt":
        command_names.append("nodejs")

    for command_name in command_names:
        resolved = shutil.which(command_name)
        if resolved:
            candidates.append(Path(resolved))

    candidates.extend(_common_node_installation_paths())
    return candidates


def _common_node_installation_paths() -> list[Path]:
    if os.name == "nt":
        candidates: list[Path] = []
        for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            base_dir = os.environ.get(env_name)
            if base_dir:
                candidates.append(Path(base_dir) / "nodejs" / "node.exe")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Programs" / "nodejs" / "node.exe")
        return candidates

    return [
        Path("/opt/homebrew/bin/node"),
        Path("/usr/local/bin/node"),
        Path("/usr/bin/node"),
        Path("/snap/bin/node"),
    ]


def _is_existing_file(path: Path) -> bool:
    return path.exists() and path.is_file()


def _build_missing_node_runtime_error(*, explicit_path: Path | None = None) -> WeixinPluginError:
    if explicit_path is not None:
        detail = (
            "未找到可用的 Node.js 运行时："
            f"{explicit_path}。"
            f"请确认该路径存在，或者移除环境变量 `{WEIXIN_CLAW_NODE_PATH_ENV_VAR}` 后重试。"
        )
    else:
        detail = (
            "未找到可用的 Node.js 运行时。"
            "微信插件需要 Node.js >= 22。"
            "请确认当前服务进程的 PATH 能找到 `node`，"
            f"或者设置环境变量 `{WEIXIN_CLAW_NODE_PATH_ENV_VAR}` 指向实际的 node 可执行文件。"
        )
    return WeixinPluginError(error_code="node_runtime_missing", detail=detail)


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
