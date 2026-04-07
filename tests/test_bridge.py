from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from plugin.bridge import (
    CHANNEL_POLL_TIMEOUT_SECONDS,
    CHANNEL_SEND_TIMEOUT_SECONDS,
    WEIXIN_CLAW_NODE_PATH_ENV_VAR,
    _resolve_node_executable,
    invoke_transport,
)
from plugin.models import WeixinBridgeError, WeixinBridgeProtocolError, WeixinPluginError


def test_bridge_supports_poll_send_and_action_requests() -> None:
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


def test_bridge_prefers_explicit_node_override(tmp_path: Path) -> None:
    node_executable = tmp_path / "node.exe"
    node_executable.write_text("", encoding="utf-8")

    with patch.dict("plugin.bridge.os.environ", {WEIXIN_CLAW_NODE_PATH_ENV_VAR: str(node_executable)}, clear=False):
        with patch("plugin.bridge.shutil.which", return_value=None):
            assert _resolve_node_executable() == node_executable


def test_bridge_falls_back_to_common_node_installation_path(tmp_path: Path) -> None:
    node_executable = tmp_path / "node.exe"
    node_executable.write_text("", encoding="utf-8")

    with patch.dict("plugin.bridge.os.environ", {WEIXIN_CLAW_NODE_PATH_ENV_VAR: ""}, clear=False):
        with patch("plugin.bridge.shutil.which", return_value=None):
            with patch("plugin.bridge._common_node_installation_paths", return_value=[node_executable]):
                assert _resolve_node_executable() == node_executable


def test_bridge_reports_missing_node_runtime_with_helpful_message() -> None:
    with patch.dict("plugin.bridge.os.environ", {WEIXIN_CLAW_NODE_PATH_ENV_VAR: ""}, clear=False):
        with patch("plugin.bridge.shutil.which", return_value=None):
            with patch("plugin.bridge._common_node_installation_paths", return_value=[]):
                with pytest.raises(WeixinPluginError) as exc_info:
                    invoke_transport(kind="channel", action="poll", payload={})

    assert exc_info.value.error_code == "node_runtime_missing"
    assert WEIXIN_CLAW_NODE_PATH_ENV_VAR in exc_info.value.detail


def test_bridge_rejects_invalid_json_output() -> None:
    completed = subprocess.CompletedProcess(
        args=["node", "bridge.mjs"],
        returncode=0,
        stdout="not-json",
        stderr="",
    )
    with patch("plugin.bridge.subprocess.run", return_value=completed):
        with pytest.raises(WeixinBridgeProtocolError):
            invoke_transport(kind="channel", action="poll", payload={})


def test_bridge_uses_action_specific_timeouts() -> None:
    completed = subprocess.CompletedProcess(
        args=["node", "bridge.mjs"],
        returncode=0,
        stdout='{"ok": true, "result": {"provider_message_ref": "provider-msg-001"}}',
        stderr="",
    )
    with patch("plugin.bridge.subprocess.run", return_value=completed) as run_mock:
        invoke_transport(kind="channel", action="poll", payload={})
        assert run_mock.call_args.kwargs["timeout"] == CHANNEL_POLL_TIMEOUT_SECONDS

        invoke_transport(kind="channel", action="send", payload={})
        assert run_mock.call_args.kwargs["timeout"] == CHANNEL_SEND_TIMEOUT_SECONDS
