from __future__ import annotations

from unittest.mock import patch

from plugin.config_preview import preview


def test_preview_returns_qr_artifact_and_status_text() -> None:
    with patch(
        "plugin.config_preview.execute",
        return_value={
            "message": "二维码已生成，请扫码。",
            "status_summary": {
                "status": "waiting_scan",
                "title": "等待扫码",
                "message": "二维码已生成，请扫码。",
                "updated_at": "2026-03-23T10:00:00+00:00",
            },
            "artifacts": [
                {
                    "kind": "image_url",
                    "label": "登录二维码",
                    "url": "https://example.com/qr.png",
                }
            ],
        },
    ) as execute_mock:
        result = preview(
            {
                "operation": "preview",
                "scope_key": "channel-account-001",
                "action_key": "start_login",
            }
        )

    execute_mock.assert_called_once_with(
        {
            "action_name": "start_login",
            "channel_account_id": "channel-account-001",
        }
    )
    assert result["runtime_state"]["status"] == "waiting_scan"
    assert result["preview_artifacts"][0]["kind"] == "image_url"
    assert result["preview_artifacts"][0]["url"] == "https://example.com/qr.png"
    assert result["preview_artifacts"][1]["kind"] == "text"
    assert result["preview_artifacts"][1]["text"] == "二维码已生成，请扫码。"


def test_preview_accepts_legacy_preview_action_field() -> None:
    with patch(
        "plugin.config_preview.execute",
        return_value={"status_summary": {}, "artifacts": []},
    ) as execute_mock:
        preview(
            {
                "operation": "preview",
                "scope_key": "channel-account-001",
                "preview_action": "get_login_status",
            }
        )

    execute_mock.assert_called_once_with(
        {
            "action_name": "get_login_status",
            "channel_account_id": "channel-account-001",
        }
    )


def test_validate_operation_does_not_trigger_preview_side_effect() -> None:
    with patch("plugin.config_preview.execute") as execute_mock:
        result = preview(
            {
                "operation": "validate",
                "scope_key": "channel-account-001",
            }
        )

    execute_mock.assert_not_called()
    assert result == {}
