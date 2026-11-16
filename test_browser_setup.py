import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from _breeze_test_plugin import create_plugin
from _breeze_test_plugin.breeze import BreezeClient
from _breeze_test_plugin.browser import BrowserRuntime, BrowserSetupError, browser_error_message
from maibot_sdk.context import PluginContext, PluginPaths

browser_module = importlib.import_module("_breeze_test_plugin.browser")


@pytest.fixture
def installer(monkeypatch):
    # Python 依赖由 Host 在加载前安装；这里隔离导入及实际浏览器下载。
    monkeypatch.setattr(browser_module.importlib, "import_module", Mock())
    monkeypatch.setattr(BrowserRuntime, "_system_browser_channel", lambda self: None)
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", None)))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return process, spawn


async def test_installer_uses_runner_python_and_reuses_prepared_browser(installer):
    _, spawn = installer
    progress = Mock()
    runtime = BrowserRuntime(channel="chromium", progress=progress)
    await asyncio.gather(runtime.ensure(), runtime.ensure())
    assert spawn.call_args.args == (sys.executable, "-m", "playwright", "install", "chromium")
    assert "shell" not in spawn.call_args.kwargs
    spawn.assert_awaited_once()
    assert "完成" in runtime.progress
    assert progress.call_count == 2


@pytest.mark.parametrize("channel", ["msedge", "chrome"])
async def test_automatically_reuses_existing_browser_without_download(installer, monkeypatch, channel):
    _, spawn = installer
    monkeypatch.setattr(BrowserRuntime, "_system_browser_channel", lambda self: channel)
    runtime = BrowserRuntime(channel="chromium")
    await runtime.ensure()
    assert runtime.launch_channel == channel
    assert "无需下载" in runtime.progress
    spawn.assert_not_called()


def test_discovers_edge_then_chrome_in_windows_install_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="nt", environ={"PROGRAMFILES": str(tmp_path)}))
    chrome = tmp_path / "Google/Chrome/Application/chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    runtime = BrowserRuntime()
    assert runtime._system_browser_channel() == "chrome"
    edge = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    assert runtime._system_browser_channel() == "msedge"


@pytest.mark.parametrize("options", [{"auto_install": False}, {"channel": "msedge"}, {"channel": "chrome"}])
async def test_does_not_download_when_disabled_or_using_system_browser(installer, options):
    _, spawn = installer
    await BrowserRuntime(**options).ensure()
    spawn.assert_not_called()


async def test_missing_python_dependency_is_left_to_host(monkeypatch):
    monkeypatch.setattr(browser_module.importlib, "import_module", Mock(side_effect=ImportError))
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(BrowserSetupError, match="MaiBot.*自动安装 Python"):
        await BrowserRuntime().ensure()
    spawn.assert_not_called()


async def test_failed_download_can_retry_without_exposing_output(installer):
    process, spawn = installer
    process.returncode = 1
    process.communicate.return_value = (b"proxy-secret https://user:password@example.invalid/download", None)
    runtime = BrowserRuntime(channel="chromium")
    with pytest.raises(BrowserSetupError, match="自动安装失败") as error:
        await runtime.ensure()
    assert "password" not in str(error.value) and "proxy-secret" not in str(error.value)
    process.returncode = 0
    await runtime.ensure()
    assert spawn.await_count == 2


@pytest.mark.parametrize("output,reason", [(b"ENOSPC", "磁盘空间"), (b"EACCES", "写入权限")])
async def test_install_failure_has_actionable_reason(installer, output, reason):
    process, _ = installer
    process.returncode = 1
    process.communicate.return_value = (output, None)
    with pytest.raises(BrowserSetupError, match=reason):
        await BrowserRuntime(channel="chromium").ensure()


async def test_timeout_stops_installer_and_allows_retry(installer, monkeypatch):
    process, _ = installer

    async def download():
        await asyncio.sleep(10)

    process.communicate.side_effect = download
    runtime = BrowserRuntime(channel="chromium", install_timeout=0.01)
    cleanup = AsyncMock()
    monkeypatch.setattr(runtime, "_stop_installer", cleanup)
    with pytest.raises(BrowserSetupError, match="超时"):
        await runtime.ensure()
    cleanup.assert_awaited_once_with(process)
    process.communicate.side_effect = None
    await runtime.ensure()


async def test_cancellation_stops_installer(installer, monkeypatch):
    process, _ = installer
    entered = asyncio.Event()

    async def download():
        entered.set()
        await asyncio.Event().wait()

    process.communicate.side_effect = download
    runtime = BrowserRuntime(channel="chromium")
    cleanup = AsyncMock()
    monkeypatch.setattr(runtime, "_stop_installer", cleanup)
    task = asyncio.create_task(runtime.ensure())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cleanup.assert_awaited_once_with(process)


async def test_windows_cleanup_includes_child_processes(monkeypatch):
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(browser_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    killer = SimpleNamespace(communicate=AsyncMock(return_value=(None, None)))
    spawn = AsyncMock(return_value=killer)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    process = SimpleNamespace(pid=1234, returncode=None, kill=Mock(), communicate=AsyncMock())
    await BrowserRuntime()._stop_installer(process)
    assert spawn.call_args.args == ("taskkill", "/PID", "1234", "/T", "/F")
    process.communicate.assert_awaited_once()


async def test_posix_cleanup_includes_child_processes(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="posix", killpg=killpg))
    monkeypatch.setattr(browser_module, "signal", SimpleNamespace(SIGKILL=9))
    process = SimpleNamespace(pid=1234, returncode=None, kill=Mock(), communicate=AsyncMock())
    await BrowserRuntime()._stop_installer(process)
    killpg.assert_called_once_with(1234, browser_module.signal.SIGKILL)
    process.communicate.assert_awaited_once()


async def test_plugin_load_is_nonblocking_and_unload_cancels_preparation(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def prepare(runtime):
        runtime.report("测试下载进度")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(BrowserRuntime, "ensure", prepare)
    plugin = create_plugin()
    config = plugin.get_default_config()
    config["account"]["deployment_mode"] = "desktop"
    plugin.set_plugin_config(config)
    plugin._set_context(PluginContext("test", paths=PluginPaths(tmp_path / "data", tmp_path / "runtime")))
    try:
        await asyncio.wait_for(plugin.on_load(), 0.5)
        await asyncio.wait_for(entered.wait(), 1)
        states = await plugin.get_config_widget_state()
        assert "测试下载进度" in states["environment"].text
        assert states["login"].disabled
    finally:
        await plugin.on_unload()
    assert cancelled.is_set() and not plugin._jobs


async def test_login_reports_setup_failure_and_can_retry(tmp_path, monkeypatch):
    ensure = AsyncMock(side_effect=BrowserSetupError("测试下载失败"))
    monkeypatch.setattr(BrowserRuntime, "ensure", ensure)
    client = BreezeClient(tmp_path, deployment_mode="desktop")
    try:
        for _ in range(2):
            result = await client.login()
            assert not result.ok and result.message == "测试下载失败"
            assert client.browser.error == result.message
            assert client.browser.progress == result.message
        assert ensure.await_count == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "detail,channel,expected",
    [
        ("Missing X server or $DISPLAY private-secret", "chromium", "图形桌面"),
        ("ProcessSingleton private-secret", "chromium", "占用"),
        ("Host system is missing dependencies private-secret", "chromium", "系统库"),
        ("Executable doesn't exist private-secret", "msedge", "Microsoft Edge"),
        ("Executable doesn't exist private-secret", "chromium", "自动补齐"),
        ("net::ERR_CONNECTION_RESET private-secret", "chromium", "网络"),
        ("Timeout 60000ms private-secret", "chromium", "超时"),
        ("unknown private-secret", "chromium", "重试"),
    ],
)
def test_browser_error_classification_does_not_expose_details(detail, channel, expected):
    message = browser_error_message(Exception(detail), channel)
    assert expected in message and "private-secret" not in message


def test_host_manifest_declares_all_runtime_dependencies():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "_manifest.json").read_text("utf-8"))
    names = {dependency["name"] for dependency in manifest["dependencies"]}
    assert names == {"httpx", "tzdata", "playwright"}
    requirements = (root / "requirements.txt").read_text("utf-8")
    assert all(name in requirements for name in names)
