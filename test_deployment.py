"""部署识别及无浏览器的服务器登录、查询、领取流程。"""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from _breeze_test_plugin import create_plugin
from _breeze_test_plugin.breeze import BreezeClient, CookieError, CookieStore, parse_cookie_input
from _breeze_test_plugin.browser import BrowserRuntime, has_graphical_desktop, resolve_deployment_mode
from _breeze_test_plugin.storage import ScheduleState
from maibot_sdk.config import rebuild_plugin_config_data
from maibot_sdk.config_widgets import ConfigWidgetSnapshot
from maibot_sdk.context import PluginContext, PluginPaths

browser_module = importlib.import_module("_breeze_test_plugin.browser")
plugin_module = importlib.import_module("_breeze_test_plugin")
SECRET = "test-private-session=="


def cookie_data(value=SECRET):
    return [{"name": "breeze_blue_session", "value": value, "domain": "breezeblue.ai", "path": "/"}]


def summary(**changes):
    return {"balance": 123, "daily_claim_claimed_today": False, "daily_claim_available": True, **changes}


@pytest.mark.parametrize("environment,expected", [
    ({}, False), ({"DISPLAY": ":0"}, True), ({"WAYLAND_DISPLAY": "wayland-0"}, True),
    ({"DISPLAY": ":10", "SSH_CONNECTION": "server"}, False), ({"SSH_TTY": "/dev/pts/0"}, False),
])
def test_linux_headless_display_and_ssh_detection(monkeypatch, environment, expected):
    monkeypatch.setattr(browser_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(environ=environment))
    assert has_graphical_desktop() is expected


@pytest.mark.parametrize("session_id,expected", [(0, False), (1, True), (5, True)])
def test_windows_service_and_interactive_detection(monkeypatch, session_id, expected):
    import ctypes

    def process_session(pid, pointer):
        pointer._obj.value = session_id
        return 1

    monkeypatch.setattr(browser_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(environ={}, getpid=lambda: 10))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        kernel32=SimpleNamespace(ProcessIdToSessionId=process_session)
    ), raising=False)
    assert has_graphical_desktop() is expected


@pytest.mark.parametrize("available,auto", [(False, "server"), (True, "desktop")])
def test_manual_mode_overrides_auto_detection(monkeypatch, available, auto):
    monkeypatch.setattr(browser_module, "has_graphical_desktop", lambda: available)
    assert resolve_deployment_mode("auto") == auto
    assert resolve_deployment_mode("server") == "server"
    assert resolve_deployment_mode("desktop") == "desktop"


@pytest.mark.parametrize("text", [
    SECRET, "  " + SECRET + "  ", "breeze_blue_session=" + SECRET,
    "Cookie: other=ignore; breeze_blue_session=" + SECRET + "; another=ignore",
    json.dumps(cookie_data()), json.dumps({"cookies": cookie_data()}),
])
def test_cookie_input_formats(text):
    cookies = parse_cookie_input(text)
    assert len(cookies) == 1 and cookies[0]["value"] == SECRET


@pytest.mark.parametrize("text", [
    "", "{private-secret", '"private-secret"', "private-secret\r\nx", "private secret", "私密数据",
    "breeze_blue_session=", "breeze_blue_session=a; breeze_blue_session=b", "x" * (1024 * 1024 + 1),
    json.dumps(cookie_data()).replace("breezeblue.ai", "other.example"),
])
def test_cookie_input_errors_are_safe(text):
    with pytest.raises(CookieError) as error:
        parse_cookie_input(text)
    assert "private-secret" not in str(error.value)


async def test_server_import_status_claim_and_refresh_never_prepare_browser(tmp_path, monkeypatch):
    ensure = AsyncMock(side_effect=AssertionError("服务器不应准备浏览器"))
    monkeypatch.setattr(BrowserRuntime, "ensure", ensure)
    requests = []

    def handler(request):
        requests.append(request.method)
        assert request.url.host == "breezeblue.ai"
        assert "breeze_blue_session=" + SECRET in request.headers["cookie"]
        return httpx.Response(200, json=summary(**(
            {"claimed": True, "daily_claim_claimed_today": True} if request.method == "POST" else {}
        )))

    # 即使留有旧 profile，云服务器也不能重新打开浏览器。
    (tmp_path / "profile").mkdir()
    client = BreezeClient(tmp_path, deployment_mode="server", transport=httpx.MockTransport(handler))
    try:
        assert (await client.prepare()).ok
        assert not (await client.login()).ok
        assert (await client.import_session(SECRET)).ok
        assert (await client.status()).ok
        assert (await client.claim()).claimed
        assert (await client.refresh()).ok
        assert requests == ["GET", "GET", "GET", "POST", "GET"]
        assert CookieStore(tmp_path).load()[0]["value"] == SECRET
        ensure.assert_not_called()
    finally:
        await client.close()


@pytest.mark.parametrize("response", [
    httpx.Response(401), httpx.Response(403), httpx.Response(500),
    httpx.Response(200, json={"unexpected": "private-secret"}), httpx.Response(200, text="private-secret"),
])
async def test_failed_import_preserves_previous_login(tmp_path, response):
    store = CookieStore(tmp_path)
    store.save(cookie_data("old-session"))
    previous = store.path.read_bytes()
    client = BreezeClient(tmp_path, deployment_mode="server", transport=httpx.MockTransport(lambda _: response))
    try:
        result = await client.import_session(SECRET)
        assert not result.ok and "导入失败" in result.message
        assert SECRET not in result.message and "private-secret" not in result.message
        assert store.path.read_bytes() == previous
    finally:
        await client.close()


async def test_expired_server_login_does_not_use_old_profile(tmp_path, monkeypatch):
    (tmp_path / "profile").mkdir()
    CookieStore(tmp_path).save(cookie_data())
    client = BreezeClient(
        tmp_path, deployment_mode="server", transport=httpx.MockTransport(lambda _: httpx.Response(401))
    )
    refresh = AsyncMock(side_effect=AssertionError("不能刷新旧 profile"))
    monkeypatch.setattr(client, "refresh", refresh)
    try:
        result = await client.claim()
        assert not result.ok and result.need_login
        refresh.assert_not_called()
    finally:
        await client.close()


async def test_server_widget_import_reload_and_retry_state(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(browser_module, "has_graphical_desktop", lambda: False)
    ensure = AsyncMock(side_effect=AssertionError("服务器不应准备浏览器"))
    monkeypatch.setattr(BrowserRuntime, "ensure", ensure)
    plugin = create_plugin()
    config = plugin.get_default_config()
    config["account"]["session_cookie"] = SECRET
    plugin.set_plugin_config(config)
    plugin._set_context(PluginContext("test", paths=PluginPaths(tmp_path / "data", tmp_path / "runtime")))
    log_info = Mock()
    monkeypatch.setattr(plugin.ctx.logger, "info", log_info)
    await plugin.on_load()
    try:
        assert plugin._job is None and not plugin._busy
        await plugin._client.http.aclose()
        plugin._client.http = httpx.AsyncClient(
            base_url="https://breezeblue.ai",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=summary())),
        )
        states = await plugin.get_config_widget_state()
        ConfigWidgetSnapshot(widgets=plugin.config_widgets, states=states)
        assert not states["login"].visible and not states["import_cookie"].disabled
        assert "服务器模式" in states["environment"].text
        schema = plugin.get_webui_config_schema()
        assert schema["sections"]["account"]["fields"]["session_cookie"]["input_type"] == "password"
        assert schema["sections"]["account"]["fields"]["session_cookie"]["ui_type"] == "password"
        assert SECRET not in repr(plugin.settings)
        ScheduleState(attempts=3, last_attempt=1, success_until=9999999999).save(plugin.ctx.paths.data_dir)
        await plugin.on_config_widget_action("import_cookie")
        await plugin._job
        assert plugin._last.ok and plugin._last.balance == 123
        state = ScheduleState.load(plugin.ctx.paths.data_dir)
        assert state.attempts == state.last_attempt == state.success_until == 0
        # 成功后清空输入并热更新，cookies.json 仍可使用。
        config["account"]["session_cookie"] = ""
        plugin.set_plugin_config(config)
        await plugin.on_config_update("self", config, "cleared")
        assert CookieStore(plugin.ctx.paths.data_dir).load()[0]["value"] == SECRET
        states = await plugin.get_config_widget_state()
        assert SECRET not in str(states) and SECRET not in caplog.text
        assert SECRET not in str(log_info.mock_calls)
        ensure.assert_not_called()
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("version,channel", [("1.0.0", "chromium"), ("1.0.1", "msedge")])
def test_host_rebuild_adds_auto_mode_and_preserves_schedule(tmp_path, monkeypatch, version, channel):
    path = tmp_path / "config.toml"
    path.write_text(f'[plugin]\nconfig_version = "{version}"\n', encoding="utf-8")
    monkeypatch.setattr(plugin_module, "CONFIG_PATH", path)
    plugin = create_plugin()
    old = {"plugin": {"config_version": version}, "account": {"browser_channel": channel},
           "schedule": {"claim_time": "09:30"}}
    rebuilt = rebuild_plugin_config_data(plugin.get_default_config(), old)
    normalized, _ = plugin.normalize_plugin_config(rebuilt)
    assert normalized["plugin"]["config_version"] == "1.0.2"
    assert normalized["account"]["deployment_mode"] == "auto"
    assert normalized["account"]["session_cookie"] == ""
    assert normalized["schedule"]["claim_time"] == "09:30"
    assert normalized["account"]["browser_channel"] == "msedge"


async def test_switch_to_server_cancels_pending_browser_setup(tmp_path, monkeypatch):
    import asyncio

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_setup(runtime):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(BrowserRuntime, "ensure", pending_setup)
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
        config["account"]["deployment_mode"] = "server"
        plugin.set_plugin_config(config)
        await plugin.on_config_update("self", config, "server")
        assert previous.cancelled() and cancelled.is_set()
        assert plugin._job is None and not plugin._busy and plugin._client.is_server
        states = await plugin.get_config_widget_state()
        assert not states["claim"].disabled and "服务器模式" in states["environment"].text
    finally:
        await plugin.on_unload()
