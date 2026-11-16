import asyncio
import importlib
import json
import sys
from argparse import Namespace
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from _breeze_test_plugin.breeze import BreezeClient, CookieStore
from _breeze_test_plugin.browser import BrowserRuntime
from _breeze_test_plugin.cli import run
from _breeze_test_plugin.storage import ScheduleState


def cookies(value="test-session-secret"):
    return [{"name": "breeze_blue_session", "value": value, "domain": ".breezeblue.ai", "path": "/"}]


def fake_browser(monkeypatch, cookie_data):
    monkeypatch.setattr("_breeze_test_plugin.browser.has_graphical_desktop", lambda: True)
    monkeypatch.setattr(BrowserRuntime, "ensure", AsyncMock())
    page = SimpleNamespace(goto=AsyncMock(), is_closed=Mock(side_effect=[False, True]))
    context = SimpleNamespace(pages=[page], cookies=AsyncMock(return_value=cookie_data), close=AsyncMock())
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    class Manager:
        async def __aenter__(self):
            return playwright

        async def __aexit__(self, *_):
            return None

    module = ModuleType("playwright.async_api")
    module.Error = type("FakePlaywrightError", (Exception,), {})
    module.async_playwright = Manager
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return context, launch


async def test_browser_login_saves_only_verified_session(tmp_path, monkeypatch):
    context, launch = fake_browser(monkeypatch, cookies("new-session"))

    def handler(request):
        assert "new-session" in request.headers["cookie"]
        return httpx.Response(
            200, json={"balance": 10, "daily_claim_claimed_today": False, "daily_claim_available": True}
        )

    client = BreezeClient(tmp_path, transport=httpx.MockTransport(handler), browser_channel="msedge")
    try:
        result = await client.login()
        assert result.ok
        assert CookieStore(tmp_path).load()[0]["value"] == "new-session"
        assert launch.call_args.kwargs["channel"] == "msedge"
        assert launch.call_args.kwargs["headless"] is False
        context.close.assert_awaited_once()
    finally:
        await client.close()


async def test_login_uses_automatically_selected_system_browser(tmp_path, monkeypatch):
    _, launch = fake_browser(monkeypatch, cookies())

    async def reuse_edge(runtime):
        runtime.launch_channel = "msedge"

    monkeypatch.setattr(BrowserRuntime, "ensure", reuse_edge)
    client = BreezeClient(
        tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"balance": 10, "daily_claim_claimed_today": False, "daily_claim_available": True}
        )),
    )
    try:
        assert (await client.login()).ok
        assert launch.call_args.kwargs["channel"] == "msedge"
    finally:
        await client.close()


async def test_invalid_browser_session_does_not_overwrite_previous_cookie(tmp_path, monkeypatch):
    context, _ = fake_browser(monkeypatch, cookies("unverified-session"))
    CookieStore(tmp_path).save(cookies("previous-session"))
    client = BreezeClient(tmp_path, transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    try:
        result = await client.login()
        assert not result.ok and result.need_login
        assert CookieStore(tmp_path).load()[0]["value"] == "previous-session"
        context.close.assert_awaited_once()
    finally:
        await client.close()


async def test_cli_import_accepts_dsh_cookies_and_resets_retry_state(tmp_path, capsys):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(cookies()), encoding="utf-8")
    target = tmp_path / "account"
    ScheduleState(attempts=3, success_until=9999999999).save(target)
    code = await run(Namespace(operation="import", data_dir=target, source=source))
    assert code == 0
    output = capsys.readouterr().out
    assert json.loads(output)["ok"]
    assert "test-session-secret" not in output
    assert ScheduleState.load(target).attempts == 0
    assert ScheduleState.load(target).success_until == 0
    assert CookieStore(target).load()[0]["name"] == "breeze_blue_session"


async def test_cli_bad_json_is_safe(tmp_path, capsys):
    source = tmp_path / "source.json"
    source.write_text("{test-session-secret", encoding="utf-8")
    code = await run(Namespace(operation="import", data_dir=tmp_path / "account", source=source))
    assert code == 1
    output = capsys.readouterr().out
    assert "test-session-secret" not in output
    assert not json.loads(output)["ok"]


async def test_browser_cancellation_closes_context(tmp_path, monkeypatch):
    context, _ = fake_browser(monkeypatch, cookies())
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def blocked_goto(*args, **kwargs):
        entered.set()
        await gate.wait()

    context.pages[0].goto = blocked_goto
    client = BreezeClient(tmp_path, transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    task = asyncio.create_task(client.login())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        context.close.assert_awaited_once()
    finally:
        await client.close()


def test_entry_does_not_import_host_internals():
    plugin = importlib.import_module("_breeze_test_plugin")
    assert plugin.create_plugin().get_components()[0]["type"] == "COMMAND"
