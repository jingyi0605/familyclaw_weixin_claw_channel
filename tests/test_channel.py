from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from plugin.channel import handle
from plugin.runtime_state import RuntimeStateStore, build_runtime_context


def test_channel_probe_reads_persisted_login_state(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    store.save_account_session(
        "channel-account-001",
        status="active",
        token="bot-token-001",
        api_base_url="https://ilinkai.weixin.qq.com",
    )

    result = handle(
        {
            "action": "probe",
            "account": {"id": "channel-account-001"},
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )

    assert result["probe_status"] == "ok"
    assert result["message"] == "weixin channel session is active"


def test_channel_poll_persists_checkpoint_and_context_token(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    store.save_account_session(
        "channel-account-001",
        status="active",
        token="bot-token-001",
        api_base_url="https://ilinkai.weixin.qq.com",
        provider_account_id="wx-bot-001",
    )

    with patch(
        "plugin.channel.invoke_transport",
        return_value={
            "message": "weixin polling completed",
            "transport_cursor": "transport-buf-001",
            "messages": [
                {
                    "message_id": 9001,
                    "from_user_id": "wx-user-001",
                    "session_id": "session-001",
                    "create_time_ms": 1710500000000,
                    "context_token": "context-token-001",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "你好，微信"}},
                    ],
                }
            ],
        },
    ):
        result = handle(
            {
                "action": "poll",
                "account": {"id": "channel-account-001"},
                "runtime": {"working_dir": str(runtime_dir)},
            }
        )

    restored_context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    restored_store = RuntimeStateStore(restored_context)
    checkpoint = restored_store.get_poll_checkpoint("channel-account-001")
    token_state = restored_store.get_context_token(
        channel_account_id="channel-account-001",
        conversation_key="direct:wx-user-001",
        external_user_id="wx-user-001",
    )

    assert result["next_cursor"] == "9001"
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["external_event_id"] == "9001"
    assert event["external_conversation_key"] == "direct:wx-user-001"
    assert event["normalized_payload"]["text"] == "你好，微信"
    assert event["normalized_payload"]["metadata"]["weixin_context_token"] == "context-token-001"
    assert checkpoint is not None
    assert checkpoint.cursor == "transport-buf-001"
    assert checkpoint.latest_external_event_id == "9001"
    assert token_state is not None
    assert token_state.token == "context-token-001"


def test_channel_send_restores_context_token_and_records_receipt(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    store.save_account_session(
        "channel-account-001",
        status="active",
        token="bot-token-001",
        api_base_url="https://ilinkai.weixin.qq.com",
        provider_account_id="wx-bot-001",
    )
    store.save_context_token(
        channel_account_id="channel-account-001",
        conversation_key="direct:wx-user-001",
        external_user_id="wx-user-001",
        token="context-token-001",
    )

    with patch(
        "plugin.channel.invoke_transport",
        return_value={"provider_message_ref": "provider-msg-001"},
    ) as bridge_mock:
        result = handle(
            {
                "action": "send",
                "account": {"id": "channel-account-001"},
                "delivery": {
                    "delivery_id": "delivery-001",
                    "external_conversation_key": "direct:wx-user-001",
                    "text": "回复微信",
                    "metadata": {"external_user_id": "wx-user-001"},
                },
                "runtime": {"working_dir": str(runtime_dir)},
            }
        )

    receipt = store.get_delivery_receipt(
        channel_account_id="channel-account-001",
        provider_message_ref="provider-msg-001",
    )
    payload = bridge_mock.call_args.kwargs["payload"]

    assert payload["delivery"]["context_token"] == "context-token-001"
    assert result["provider_message_ref"] == "provider-msg-001"
    assert receipt is not None
    assert receipt.status == "sent"


def test_channel_send_supports_attachments_without_text(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    attachment_path = tmp_path / "photo.png"
    attachment_path.write_bytes(b"fake-image")
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    store.save_account_session(
        "channel-account-001",
        status="active",
        token="bot-token-001",
        api_base_url="https://ilinkai.weixin.qq.com",
        provider_account_id="wx-bot-001",
    )
    store.save_context_token(
        channel_account_id="channel-account-001",
        conversation_key="direct:wx-user-001",
        external_user_id="wx-user-001",
        token="context-token-001",
    )

    with patch(
        "plugin.channel.invoke_transport",
        return_value={"provider_message_ref": "provider-media-001"},
    ) as bridge_mock:
        result = handle(
            {
                "action": "send",
                "account": {"id": "channel-account-001"},
                "delivery": {
                    "delivery_id": "delivery-002",
                    "external_conversation_key": "direct:wx-user-001",
                    "attachments": [
                        {
                            "kind": "image",
                            "file_name": "photo.png",
                            "content_type": "image/png",
                            "source_path": str(attachment_path),
                        }
                    ],
                    "metadata": {"external_user_id": "wx-user-001"},
                },
                "runtime": {"working_dir": str(runtime_dir)},
            }
        )

    payload = bridge_mock.call_args.kwargs["payload"]
    assert result["provider_message_ref"] == "provider-media-001"
    assert "text" not in payload["delivery"] or payload["delivery"]["text"] is None
    assert payload["delivery"]["attachments"][0]["kind"] == "image"
    assert payload["delivery"]["context_token"] == "context-token-001"


def test_channel_poll_emits_downloaded_media_references(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    downloaded_file = runtime_dir / "media" / "inbound" / "voice.sil"
    downloaded_file.parent.mkdir(parents=True, exist_ok=True)
    downloaded_file.write_bytes(b"voice-data")
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    store.save_account_session(
        "channel-account-001",
        status="active",
        token="bot-token-001",
        api_base_url="https://ilinkai.weixin.qq.com",
        provider_account_id="wx-bot-001",
    )

    with patch(
        "plugin.channel.invoke_transport",
        return_value={
            "message": "weixin polling completed",
            "transport_cursor": "transport-buf-voice-001",
            "messages": [
                {
                    "message_id": 9002,
                    "from_user_id": "wx-user-voice-001",
                    "session_id": "session-voice-001",
                    "create_time_ms": 1710500001000,
                    "context_token": "context-token-voice-001",
                    "item_list": [
                        {"type": 3, "voice_item": {"text": "", "playtime": 2500}},
                    ],
                    "downloaded_attachments": [
                        {
                            "kind": "audio",
                            "file_name": "voice.sil",
                            "content_type": "audio/silk",
                            "source_path": str(downloaded_file),
                            "size_bytes": 10,
                            "metadata": {"duration_ms": 2500},
                        }
                    ],
                    "download_errors": [
                        {
                            "error_code": "media_download_failed",
                            "detail": "first attempt failed",
                            "item_type": 3,
                        }
                    ],
                }
            ],
        },
    ):
        result = handle(
            {
                "action": "poll",
                "account": {"id": "channel-account-001"},
                "runtime": {"working_dir": str(runtime_dir)},
            }
        )

    event = result["events"][0]
    assert event["normalized_payload"]["text"] == "[语音]"
    assert event["normalized_payload"]["attachments"][0]["kind"] == "audio"
    assert event["normalized_payload"]["metadata"]["attachments"][0]["source_path"] == str(downloaded_file)
    assert event["normalized_payload"]["metadata"]["media_download_errors"][0]["error_code"] == "media_download_failed"


def test_channel_webhook_keeps_polling_boundary() -> None:
    result = handle({"action": "webhook"})

    assert result["http_response"]["status_code"] == 202
    assert result["message"] == "weixin claw channel currently runs in polling mode"
