import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from _breeze_test_plugin import BreezeClaimPlugin, create_plugin, sender_key
from _breeze_test_plugin.breeze import BreezeResult
from _breeze_test_plugin.config import BreezeConfig
from _breeze_test_plugin.storage import ScheduleState
from maibot_sdk import MaiBotPlugin
from maibot_sdk.config_widgets import ConfigWidgetSnapshot
from maibot_sdk.context import PluginContext, PluginPaths
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def plugin(tmp_path):
    instance = create_plugin()
    config = instance.get_default_config()
    config["account"]["auto_install_browser"] = False
    instance.set_plugin_config(config)
    context = PluginContext("mi-tuan.breeze-claim", paths=PluginPaths(tmp_path / "data", tmp_path / "runtime"))
    context.send.text = AsyncMock(return_value=True)
    instance._set_context(context)
    await instance.on_load()
    yield instance
    await instance.on_unload()


def test_sdk_entry_components_and_full_schema():
    instance = create_plugin()
    assert isinstance(instance, MaiBotPlugin)
    assert len(instance.get_components()) == 1
    schema = instance.get_webui_config_schema()
    assert {"plugin", "schedule", "account", "commands"} <= schema["sections"].keys()
    assert len(schema["runtime_widgets"]) == 9
    defaults = instance.get_default_config()
    assert defaults["plugin"]["config_version"] == "1.0.2"
    assert defaults["account"]["browser_channel"] == "msedge"
    assert defaults["commands"] == {"enabled": False, "allowed_users": []}


def test_example_toml_and_manifest_match_defaults():
    import tomllib

    with (ROOT / "config.example.toml").open("rb") as handle:
        assert tomllib.load(handle) == create_plugin().get_default_config()
    manifest = json.loads((ROOT / "_manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["capabilities"] == ["send.text"]
    assert (ROOT / manifest["display"]["icon"]["value"]).is_file()
    assert (ROOT / manifest["changelog"]).is_file()
    assert "/config.toml" in (ROOT / ".gitignore").read_text("utf-8")


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("schedule", "claim_time", "25:00"),
        ("schedule", "claim_time", "8:10"),
        ("schedule", "timezone", "Mars/Olympus"),
        ("schedule", "max_attempts", 0),
        ("schedule", "retry_interval_seconds", 0),
        ("account", "request_timeout_seconds", 0),
        ("account", "browser_channel", "unknown"),
        ("commands", "allowed_users", ["123"]),
    ],
)
def test_invalid_configuration_is_rejected(section, key, value):
    with pytest.raises(ValidationError):
        BreezeConfig.model_validate({section: {key: value}})


async def test_widget_snapshot_is_complete_and_does_not_query_network(plugin):
    plugin._client.status = AsyncMock(side_effect=AssertionError("快照不应访问网络"))
    snapshot = ConfigWidgetSnapshot(widgets=plugin.config_widgets, states=await plugin.get_config_widget_state())
    assert len(snapshot.states) == len(plugin.config_widgets)
    plugin._client.status.assert_not_called()


async def test_action_starts_background_task_and_rejects_duplicate(plugin):
    gate = asyncio.Event()

    async def slow_claim():
        await gate.wait()
        return BreezeResult(True, "领取成功", claimed=True, claimed_today=True, balance=400)

    plugin._client.claim = slow_claim
    await asyncio.wait_for(plugin.on_config_widget_action("claim"), timeout=0.5)
    assert plugin._busy == "claim"
    with pytest.raises(ValueError, match="正在进行"):
        await plugin.on_config_widget_action("claim")
    gate.set()
    await plugin._job
    assert plugin._last.ok and plugin._last.balance == 400
    assert ScheduleState.load(plugin.ctx.paths.data_dir).success_until > 0


async def test_unknown_widget_action_is_rejected(plugin):
    with pytest.raises(ValueError):
        await plugin.on_config_widget_action("delete_account")


def test_sender_identity_reads_host_and_sdk_messages():
    raw = {"platform": "qq", "message_info": {"user_info": {"user_id": "123"}}}
    assert sender_key(raw) == "qq:123"
    assert sender_key({"raw_message": raw}) == "qq:123"
    assert sender_key({"plain_text": "/breeze claim qq:123"}) == ""


async def test_empty_allowlist_denies_account_operation(plugin):
    config = plugin.get_default_config()
    config["commands"]["enabled"] = True
    plugin.set_plugin_config(config)
    result = await plugin.handle_breeze(
        stream_id="s1",
        message={"platform": "qq", "message_info": {"user_info": {"user_id": "123"}}},
        matched_groups={"operation": "claim"},
    )
    assert not result[0] and plugin._job is None


async def test_authorized_command_runs_and_sends_final_result(plugin):
    config = plugin.get_default_config()
    config["commands"] = {"enabled": True, "allowed_users": ["qq:123"]}
    plugin.set_plugin_config(config)
    plugin._client.status = AsyncMock(return_value=BreezeResult(True, "状态已更新", balance=100))
    result = await plugin.handle_breeze(
        stream_id="s1",
        platform="qq",
        user_id="123",
        matched_groups={"operation": "查询"},
    )
    assert result[0]
    await plugin._job
    assert plugin.ctx.send.text.await_count == 2
    assert "余额：100" in plugin.ctx.send.text.call_args.args[0]


async def test_reload_cancels_old_tasks_and_uses_new_config(plugin):
    gate = asyncio.Event()

    async def slow_claim():
        await gate.wait()

    old_client = plugin._client
    old_client.claim = slow_claim
    await plugin.on_config_widget_action("claim")
    old_job = plugin._job
    old_scheduler = plugin._scheduler
    await asyncio.sleep(0)
    config = plugin.get_default_config()
    config["plugin"]["enabled"] = False
    config["account"]["request_timeout_seconds"] = 15
    plugin.set_plugin_config(config)
    await plugin.on_config_update("self", config, "v2")
    assert old_job.cancelled() and old_scheduler.cancelled()
    assert old_client.http.is_closed
    assert plugin._client.http.timeout.read == 15
    assert not plugin._busy
    assert (await plugin.get_config_widget_state())["claim"].disabled


async def test_failed_auto_attempt_saved_before_request(plugin):
    plugin._client.claim = AsyncMock(return_value=BreezeResult(False, "网络失败"))
    # 任意实际时刻都确保已到本周期领取时间。
    config = plugin.get_default_config()
    config["schedule"]["claim_time"] = config["schedule"]["reset_time"]
    plugin.set_plugin_config(config)
    assert not plugin._start_operation("claim", automatic=True)
    await plugin._job
    state = ScheduleState.load(plugin.ctx.paths.data_dir)
    assert state.attempts == 1 and state.last_attempt > 0
    assert asdict(state) == asdict(plugin._state)


async def test_unload_closes_client_and_scheduler(plugin):
    client, scheduler = plugin._client, plugin._scheduler
    await plugin.on_unload()
    assert client.http.is_closed and scheduler.cancelled()
    assert plugin._client is None and plugin._scheduler is None


def test_three_lifecycle_methods_overridden():
    for method in ("on_load", "on_unload", "on_config_update"):
        assert getattr(BreezeClaimPlugin, method) is not getattr(MaiBotPlugin, method)
