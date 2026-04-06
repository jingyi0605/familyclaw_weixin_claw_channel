from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from plugin.runtime_state import RuntimeStateStore, build_runtime_context


def test_runtime_state_initializes_layout_and_sqlite_schema(tmp_path: Path) -> None:
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(tmp_path / "runtime")},
        }
    )

    assert context.working_dir.exists()
    assert context.media_dir.exists()
    assert context.logs_dir.exists()
    assert context.qr_dir.exists()
    assert context.runtime_db_path.exists()

    connection = sqlite3.connect(context.runtime_db_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()

    table_names = {row[0] for row in rows}
    assert "account_sessions" in table_names
    assert "poll_checkpoints" in table_names
    assert "context_tokens" in table_names
    assert "delivery_receipts" in table_names


def test_runtime_state_persists_sessions_tokens_and_receipts(tmp_path: Path) -> None:
    context = build_runtime_context(
        {
            "channel_account_id": "channel-account-001",
            "runtime": {"working_dir": str(tmp_path / "runtime")},
        }
    )
    store = RuntimeStateStore(context)

    session = store.mark_waiting_scan(
        "channel-account-001",
        login_session_key="session-001",
        login_qrcode="qr-raw",
        qr_code_url="https://example.com/qr.png",
        api_base_url="https://ilinkai.weixin.qq.com",
    )
    checkpoint = store.set_poll_checkpoint(
        "channel-account-001",
        cursor="cursor-001",
        latest_external_event_id="event-001",
    )
    token = store.save_context_token(
        channel_account_id="channel-account-001",
        conversation_key="direct:wx_user_001",
        external_user_id="wx_user_001",
        token="context-token-001",
    )
    receipt = store.record_delivery_receipt(
        channel_account_id="channel-account-001",
        provider_message_ref="provider-001",
        status="sent",
    )

    loaded_session = store.get_account_session("channel-account-001")
    loaded_checkpoint = store.get_poll_checkpoint("channel-account-001")
    loaded_token = store.get_context_token(
        channel_account_id="channel-account-001",
        conversation_key="direct:wx_user_001",
        external_user_id="wx_user_001",
    )
    loaded_receipt = store.get_delivery_receipt(
        channel_account_id="channel-account-001",
        provider_message_ref="provider-001",
    )

    assert loaded_session is not None
    assert loaded_session.status == "waiting_scan"
    assert Path(loaded_session.qr_code_path or "").exists()
    qr_payload = json.loads(Path(loaded_session.qr_code_path or "").read_text(encoding="utf-8"))
    assert qr_payload["qr_code_url"] == "https://example.com/qr.png"

    assert loaded_checkpoint is not None
    assert loaded_checkpoint.cursor == checkpoint.cursor

    assert loaded_token is not None
    assert loaded_token.token == token.token
    assert loaded_token.status == "fresh"

    assert loaded_receipt is not None
    assert loaded_receipt.status == receipt.status

    store.purge_runtime_state()

    assert context.runtime_db_path.exists()
    assert store.get_account_session(session.channel_account_id) is None
