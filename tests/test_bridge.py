from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.bridge import (
    CHANNEL_POLL_TIMEOUT_SECONDS,
    CHANNEL_SEND_TIMEOUT_SECONDS,
    _resolve_timeout_seconds,
    invoke_transport,
)
from plugin.models import WeixinBridgeError, WeixinBridgeProtocolError


def test_bridge_supports_mock_poll_send_and_action_requests() -> None:
    poll_result = invoke_transport(
        kind="channel",
        action="poll",
        payload={"poll_state": {"cursor": "42"}},
    )
    send_result = invoke_transport(
        kind="channel",
        action="send",
        payload={"delivery": {"text": "hello"}},
    )
    action_result = invoke_transport(
        kind="action",
        action="get_login_status",
        payload={"account": {"id": "channel-account-001"}},
    )

    assert poll_result["next_cursor"] == "42"
    assert send_result["provider_message_ref"] == "mock-provider-message-ref"
    assert action_result["channel_account_id"] == "channel-account-001"
    assert action_result["login_status"] == "not_logged_in"


def test_bridge_returns_structured_error() -> None:
    with pytest.raises(WeixinBridgeError) as exc_info:
        invoke_transport(
            kind="channel",
            action="poll",
            payload={
                "testing": {
                    "force_error": True,
                    "error_code": "transport_mock_failure",
                    "message": "forced from test",
                    "field": "testing.force_error",
                }
            },
        )

    assert exc_info.value.error_code == "transport_mock_failure"
    assert exc_info.value.field == "testing.force_error"


def test_bridge_dispatches_request_to_python_transport() -> None:
    captured_requests = []

    def fake_dispatch(request):
        captured_requests.append(request)
        return {"provider_message_ref": "provider-msg-001"}

    with patch("plugin.bridge.dispatch_transport_request", side_effect=fake_dispatch):
        result = invoke_transport(
            kind="channel",
            action="send",
            payload={"delivery": {"text": "hello"}},
        )

    request = captured_requests[0]
    assert request.kind == "channel"
    assert request.action == "send"
    assert request.payload["delivery"]["text"] == "hello"
    assert request.runtime["working_dir"]
    assert result["provider_message_ref"] == "provider-msg-001"


def test_bridge_rejects_invalid_python_transport_output() -> None:
    with patch("plugin.bridge.dispatch_transport_request", return_value="not-a-dict"):
        with pytest.raises(WeixinBridgeProtocolError):
            invoke_transport(
                kind="channel",
                action="poll",
                payload={"transport": {"token": "bot-token-001"}},
            )


def test_bridge_uses_action_specific_timeouts() -> None:
    assert _resolve_timeout_seconds(kind="channel", action="poll") == CHANNEL_POLL_TIMEOUT_SECONDS
    assert _resolve_timeout_seconds(kind="channel", action="send") == CHANNEL_SEND_TIMEOUT_SECONDS
