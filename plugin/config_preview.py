from __future__ import annotations

from typing import Any

from .action import execute


def preview(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = payload or {}
    operation = str(data.get("operation") or "").strip()
    if operation == "validate":
        return {}

    scope_key = str(data.get("scope_key") or "").strip()
    if not scope_key:
        return {
            "field_errors": {
                "account_label": "当前预览缺少账号作用域，无法生成登录二维码。",
            }
        }

    result = execute(
        {
            "action_name": _resolve_preview_action(data),
            "channel_account_id": scope_key,
        }
    )
    return {
        "runtime_state": _build_runtime_state(result),
        "preview_artifacts": _build_preview_artifacts(result),
    }


def _resolve_preview_action(payload: dict[str, Any]) -> str:
    requested_action = str(payload.get("action_key") or payload.get("preview_action") or "").strip()
    if requested_action in {"start_login", "get_login_status"}:
        return requested_action
    return "start_login"


def _build_runtime_state(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("status_summary")
    if not isinstance(summary, dict):
        return {}
    return {
        "status": summary.get("status"),
        "title": summary.get("title"),
        "message": summary.get("message"),
        "updated_at": summary.get("updated_at"),
    }


def _build_preview_artifacts(result: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    raw_artifacts = result.get("artifacts")
    if isinstance(raw_artifacts, list):
        for index, item in enumerate(raw_artifacts):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if kind not in {"image_url", "external_url", "text"}:
                continue
            artifact: dict[str, Any] = {
                "key": str(item.get("key") or f"artifact-{index}"),
                "kind": kind,
                "label": item.get("label"),
            }
            if kind in {"image_url", "external_url"}:
                artifact["url"] = item.get("url")
            if kind == "text":
                artifact["text"] = item.get("text")
            artifacts.append(artifact)

    message = str(result.get("message") or "").strip()
    if message:
        artifacts.append(
            {
                "key": "login-status-message",
                "kind": "text",
                "label": "当前状态",
                "text": message,
            }
        )
    return artifacts
