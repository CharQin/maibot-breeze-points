import asyncio
import sys
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from _breeze_test_plugin import create_plugin
from _breeze_test_plugin.browser import BrowserRuntime
from maibot_sdk.config import rebuild_plugin_config_data
from maibot_sdk.context import PluginContext, PluginPaths

plugin_module = sys.modules["_breeze_test_plugin"]


@pytest.fixture(autouse=True)
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(plugin_module, "CONFIG_PATH", path)
    return path


def test_legacy_chromium_changes_to_edge_without_mutating_other_settings():
    plugin = create_plugin()
    legacy = plugin.get_default_config()
    legacy["plugin"]["config_version"] = "1.0.0"
    legacy["account"]["browser_channel"] = "chromium"
    legacy["schedule"]["claim_time"] = "09:30"
    legacy["commands"]["allowed_users"] = ["qq:123"]
    original = deepcopy(legacy)
    normalized, changed = plugin.normalize_plugin_config(legacy)
    assert changed and legacy == original
    assert normalized["account"]["browser_channel"] == "msedge"
    assert normalized["plugin"]["config_version"] == "1.0.2"
    assert normalized["schedule"] == original["schedule"]
    assert normalized["commands"] == original["commands"]


def test_host_1319_rebuilt_config_uses_original_file_for_migration(config_path):
    plugin = create_plugin()
    legacy = plugin.get_default_config()
    legacy["plugin"]["config_version"] = "1.0.0"
    legacy["account"]["browser_channel"] = "chromium"
    stored = '[plugin]\nconfig_version = "1.0.0"\n[account]\nbrowser_channel = "chromium"\n'
    config_path.write_text(stored, encoding="utf-8")
    rebuilt = rebuild_plugin_config_data(plugin.get_default_config(), legacy)
    assert rebuilt["plugin"]["config_version"] == "1.0.2"
    assert rebuilt["account"]["browser_channel"] == "chromium"
    normalized, changed = plugin.normalize_plugin_config(rebuilt)
    assert changed and normalized["account"]["browser_channel"] == "msedge"
    # 配置落盘由 Runner 完成，插件只负责返回迁移后的配置。
    assert config_path.read_text("utf-8") == stored


def test_legacy_explicit_chrome_is_kept():
    plugin = create_plugin()
    legacy = plugin.get_default_config()
    legacy["plugin"]["config_version"] = "1.0.0"
    legacy["account"]["browser_channel"] = "chrome"
    normalized, _ = plugin.normalize_plugin_config(legacy)
    assert normalized["account"]["browser_channel"] == "chrome"


def test_new_configuration_can_explicitly_select_chromium(config_path):
    config_path.write_text('[plugin]\nconfig_version = "1.0.1"\n', encoding="utf-8")
    plugin = create_plugin()
    config = plugin.get_default_config()
    config["account"]["browser_channel"] = "chromium"
    config["account"]["deployment_mode"] = "desktop"
    plugin.set_plugin_config(config)
    assert plugin.settings.account.browser_channel == "chromium"


async def test_default_edge_never_runs_chromium_installer(monkeypatch):
    runtime = BrowserRuntime()
    installer = AsyncMock(side_effect=AssertionError("默认 Edge 不应下载 Chromium"))
    monkeypatch.setattr(runtime, "_install", installer)
    await runtime.ensure()
    assert runtime.launch_channel == "msedge"
    assert "Microsoft Edge" in runtime.progress and "无需下载" in runtime.progress
    installer.assert_not_called()


async def test_changing_to_edge_cancels_in_progress_chromium_preparation(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def prepare(runtime):
        if runtime.channel == "chromium":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            runtime.report("已选择本机 Microsoft Edge，无需下载 Chromium。")

    monkeypatch.setattr(BrowserRuntime, "ensure", prepare)
    plugin = create_plugin()
    config = plugin.get_default_config()
    config["account"]["deployment_mode"] = "desktop"
    config["account"]["browser_channel"] = "chromium"
    plugin.set_plugin_config(config)
    plugin._set_context(PluginContext("test", paths=PluginPaths(tmp_path / "data", tmp_path / "runtime")))
    try:
        await plugin.on_load()
        await asyncio.wait_for(entered.wait(), 1)
        previous = plugin._job
        config["account"]["browser_channel"] = "msedge"
        plugin.set_plugin_config(config)
        await plugin.on_config_update("self", config, "edge-test")
        await plugin._job
        assert previous.cancelled() and cancelled.is_set()
        assert plugin._client.browser_channel == "msedge"
        assert "Edge" in (await plugin.get_config_widget_state())["environment"].text
    finally:
        await plugin.on_unload()
