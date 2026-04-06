from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from plugin.action import execute
from plugin.runtime_state import RuntimeStateStore, build_runtime_context


def test_action_start_login_persists_waiting_scan_state(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    with patch(
        "plugin.action.invoke_transport",
        return_value={
            "login_status": "waiting_scan",
            "login_session_key": "session-001",
            "login_qrcode": "raw-qr-001",
            "qr_code_url": "https://example.com/qr.png",
            "api_base_url": "https://ilinkai.weixin.qq.com",
            "message": "二维码已生成",
        },
    ):
        result = execute(
            {
                "action_name": "start_login",
                "account": {"id": "channel-account-001"},
                "runtime": {"working_dir": str(runtime_dir)},
            }
        )

    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    session = store.get_account_session("channel-account-001")

    assert session is not None
    assert session.status == "waiting_scan"
    assert Path(session.qr_code_path or "").exists()
    assert result["action_name"] == "start_login"
    assert result["login_status"] == "waiting_scan"
    assert result["qr_code_url"] == "https://example.com/qr.png"
    assert result["status_summary"]["status"] == "waiting_scan"
    assert result["artifacts"][0]["kind"] == "image_url"
    assert result["artifacts"][0]["url"] == "https://example.com/qr.png"


def test_action_get_login_status_promotes_active_session(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    store = RuntimeStateStore(context)
    store.mark_waiting_scan(
        "channel-account-001",
        login_session_key="session-001",
        login_qrcode="raw-qr-001",
        qr_code_url="https://example.com/qr.png",
        api_base_url="https://ilinkai.weixin.qq.com",
    )

    with patch(
        "plugin.action.invoke_transport",
        return_value={
            "login_status": "active",
            "provider_account_id": "wx-bot-001",
            "token": "bot-token-001",
            "api_base_url": "https://ilinkai.weixin.qq.com",
            "user_id": "wx-user-001",
            "message": "登录成功",
        },
    ):
        result = execute(
            {
                "action_name": "get_login_status",
                "account": {"id": "channel-account-001"},
                "runtime": {"working_dir": str(runtime_dir)},
            }
        )

    session = store.get_account_session("channel-account-001")

    assert session is not None
    assert session.status == "active"
    assert session.provider_account_id == "wx-bot-001"
    assert session.token == "bot-token-001"
    assert result["login_status"] == "active"
    assert result["provider_account_id"] == "wx-bot-001"
    assert result["status_summary"]["status"] == "active"
    assert result["artifacts"] == []


def test_action_logout_clears_persisted_login_state(tmp_path: Path) -> None:
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
        provider_account_id="wx-bot-001",
    )

    result = execute(
        {
            "action_name": "logout",
            "account": {"id": "channel-account-001"},
            "runtime": {"working_dir": str(runtime_dir)},
        }
    )
    session = store.get_account_session("channel-account-001")

    assert session is not None
    assert session.status == "not_logged_in"
    assert session.token is None
    assert result["status"] == "accepted"
    assert result["channel_account_id"] == "channel-account-001"
    assert result["status_summary"]["status"] == "not_logged_in"
