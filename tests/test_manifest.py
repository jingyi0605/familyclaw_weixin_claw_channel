from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.modules.plugin.service import discover_plugin_manifests, load_plugin_manifest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_can_be_loaded() -> None:
    manifest = load_plugin_manifest(PLUGIN_ROOT / "manifest.json")

    assert manifest.id == "weixin-claw-channel"
    assert manifest.types == ["channel", "action"]
    assert manifest.entrypoints.channel == "plugin.channel.handle"
    assert manifest.entrypoints.action == "plugin.action.execute"
    assert manifest.entrypoints.config_preview == "plugin.config_preview.preview"
    assert [locale.id for locale in manifest.locales] == ["zh-CN", "en-US"]

    assert manifest.capabilities.channel is not None
    assert manifest.capabilities.channel.platform_code == "weixin-claw"
    assert manifest.capabilities.channel.ui.status_action_key == "refresh-login-status"
    assert [item.key for item in manifest.capabilities.channel.ui.account_actions] == [
        "start-login",
        "refresh-login-status",
        "logout",
        "purge-runtime-state",
    ]

    channel_account_spec = next(
        item for item in manifest.config_specs if item.scope_type == "channel_account"
    )
    assert channel_account_spec.title_key == "weixin_claw_channel.channel_account.title"
    assert channel_account_spec.ui_schema.submit_text_key == "weixin_claw_channel.channel_account.submit"
    assert [section.id for section in channel_account_spec.ui_schema.sections] == ["basic", "login"]
    assert [action.key for action in channel_account_spec.ui_schema.actions] == [
        "start_login",
        "refresh_login_status",
    ]
    assert [section.key for section in channel_account_spec.ui_schema.runtime_sections] == [
        "login_runtime",
    ]


def test_plugins_dev_manifest_discovery_includes_weixin_plugin() -> None:
    manifests = discover_plugin_manifests(settings.plugin_dev_root)

    manifest = next(item for item in manifests if item.id == "weixin-claw-channel")
    assert manifest.entrypoints.channel == "plugin.channel.handle"
    assert manifest.entrypoints.action == "plugin.action.execute"
    assert manifest.entrypoints.config_preview == "plugin.config_preview.preview"
