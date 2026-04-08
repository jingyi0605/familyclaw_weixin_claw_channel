from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from plugin.models import BridgeRequest
from plugin.python_transport import dispatch_transport_request


def _build_request(
    *,
    kind: str,
    action: str,
    payload: dict,
    runtime_dir: Path,
) -> BridgeRequest:
    return BridgeRequest(
        kind=kind,
        action=action,
        payload=payload,
        runtime={
            "working_dir": str(runtime_dir),
            "runtime_db_path": str(runtime_dir / "runtime.sqlite"),
            "media_dir": str(runtime_dir / "media"),
            "logs_dir": str(runtime_dir / "logs"),
            "qr_dir": str(runtime_dir / "qr"),
        },
    )


def test_python_transport_start_login_generates_qr_preview(tmp_path: Path) -> None:
    with patch(
        "plugin.python_transport.get_bot_qrcode",
        return_value={
            "qrcode": "raw-qr-001",
            "qrcode_img_content": "https://example.com/weixin-login",
        },
    ):
        result = dispatch_transport_request(
            _build_request(
                kind="action",
                action="start_login",
                payload={"channel_account_id": "channel-account-001"},
                runtime_dir=tmp_path / "runtime",
            )
        )

    assert result["channel_account_id"] == "channel-account-001"
    assert result["login_status"] == "waiting_scan"
    assert result["login_qrcode"] == "raw-qr-001"
    assert result["qr_code_url"].startswith("data:image/svg+xml")


def test_python_transport_get_login_status_maps_confirmed_to_active(tmp_path: Path) -> None:
    with patch(
        "plugin.python_transport.get_qrcode_status",
        return_value={
            "status": "confirmed",
            "bot_token": "bot-token-001",
            "ilink_bot_id": "wx-bot-001",
            "ilink_user_id": "wx-user-001",
            "baseurl": "https://ilinkai.weixin.qq.com",
        },
    ):
        result = dispatch_transport_request(
            _build_request(
                kind="action",
                action="get_login_status",
                payload={
                    "channel_account_id": "channel-account-001",
                    "login_qrcode": "raw-qr-001",
                    "login_session_key": "session-001",
                },
                runtime_dir=tmp_path / "runtime",
            )
        )

    assert result["login_status"] == "active"
    assert result["token"] == "bot-token-001"
    assert result["provider_account_id"] == "wx-bot-001"
    assert result["user_id"] == "wx-user-001"


def test_python_transport_poll_returns_messages_and_cursor(tmp_path: Path) -> None:
    with patch(
        "plugin.python_transport.fetch_updates",
        return_value={
            "msgs": [
                {
                    "message_id": 9001,
                    "from_user_id": "wx-user-001",
                    "session_id": "session-001",
                    "create_time_ms": 1710500000000,
                    "context_token": "context-token-001",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }
            ],
            "get_updates_buf": "transport-buf-001",
            "longpolling_timeout_ms": 35000,
        },
    ):
        result = dispatch_transport_request(
            _build_request(
                kind="channel",
                action="poll",
                payload={
                    "transport": {
                        "token": "bot-token-001",
                        "api_base_url": "https://ilinkai.weixin.qq.com",
                    }
                },
                runtime_dir=tmp_path / "runtime",
            )
        )

    assert result["transport_cursor"] == "transport-buf-001"
    assert result["messages"][0]["message_id"] == 9001
    assert result["messages"][0]["item_list"][0]["text_item"]["text"] == "你好"


def test_python_transport_send_text_calls_weixin_api(tmp_path: Path) -> None:
    captured_messages: list[dict] = []

    def fake_send_weixin_message(*, api_base_url: str, token: str, message: dict) -> dict:
        captured_messages.append(
            {
                "api_base_url": api_base_url,
                "token": token,
                "message": message,
            }
        )
        return {}

    with patch("plugin.python_transport.send_weixin_message", side_effect=fake_send_weixin_message):
        result = dispatch_transport_request(
            _build_request(
                kind="channel",
                action="send",
                payload={
                    "transport": {
                        "token": "bot-token-001",
                        "api_base_url": "https://ilinkai.weixin.qq.com",
                    },
                    "delivery": {
                        "external_user_id": "wx-user-001",
                        "text": "回复微信",
                        "context_token": "context-token-001",
                    },
                },
                runtime_dir=tmp_path / "runtime",
            )
        )

    assert result["provider_message_ref"]
    assert captured_messages[0]["token"] == "bot-token-001"
    assert captured_messages[0]["message"]["item_list"][0]["text_item"]["text"] == "回复微信"


def test_python_transport_send_image_attachment_builds_media_message(tmp_path: Path) -> None:
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"fake-image-data")
    captured_messages: list[dict] = []

    def fake_send_weixin_message(*, api_base_url: str, token: str, message: dict) -> dict:
        captured_messages.append(message)
        return {}

    with patch(
        "plugin.python_transport.fetch_upload_url",
        return_value={"upload_param": "upload-param-001"},
    ):
        with patch(
            "plugin.python_transport.upload_cdn_buffer",
            return_value="download-param-001",
        ):
            with patch("plugin.python_transport.send_weixin_message", side_effect=fake_send_weixin_message):
                result = dispatch_transport_request(
                    _build_request(
                        kind="channel",
                        action="send",
                        payload={
                            "transport": {
                                "token": "bot-token-001",
                                "api_base_url": "https://ilinkai.weixin.qq.com",
                            },
                            "delivery": {
                                "external_user_id": "wx-user-001",
                                "context_token": "context-token-001",
                                "attachments": [
                                    {
                                        "kind": "image",
                                        "file_name": "photo.png",
                                        "content_type": "image/png",
                                        "source_path": str(image_path),
                                    }
                                ],
                            },
                        },
                        runtime_dir=tmp_path / "runtime",
                    )
                )

    assert result["provider_message_ref"]
    item = captured_messages[0]["item_list"][0]
    assert item["type"] == 2
    assert item["image_item"]["media"]["encrypt_query_param"] == "download-param-001"
