import json
from unittest.mock import AsyncMock

import httpx
import pytest
from _breeze_test_plugin.breeze import (
    BreezeClient,
    BreezeResult,
    CookieError,
    CookieStore,
    normalize_cookies,
    parse_result,
)


def cookies(**patch):
    return [
        {
            "name": "breeze_blue_session",
            "value": "test-session-secret",
            "domain": ".breezeblue.ai",
            "path": "/",
            "expires": -1,
            **patch,
        }
    ]


def summary(**patch):
    return {
        "balance": 100,
        "daily_claim_claimed_today": False,
        "daily_claim_available": True,
        "daily_claim_amount": 400,
        "daily_claim_next_reset_at": "2026-11-17T00:00:00Z",
        **patch,
    }


@pytest.fixture
async def client_factory(tmp_path):
    clients = []
    CookieStore(tmp_path).save(cookies())

    def factory(handler, **kwargs):
        client = BreezeClient(tmp_path, transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.close()


def test_cookie_formats_domain_filter_and_expiry():
    data = cookies() + cookies(domain="breezeblue.ai.evil.example") + cookies(domain="evilbreezeblue.ai")
    assert len(normalize_cookies({"cookies": data})) == 1
    with pytest.raises(CookieError):
        normalize_cookies(cookies(expires=1), now=2)
    with pytest.raises(CookieError):
        normalize_cookies("breeze_blue_session=secret")
    with pytest.raises(CookieError):
        normalize_cookies(cookies(value="secret\r\nInjected: yes"))
    with pytest.raises(CookieError):
        normalize_cookies(cookies(path="/app"))


def test_invalid_cookie_file_does_not_expose_contents(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text("{bad-secret", encoding="utf-8")
    with pytest.raises(CookieError) as error:
        CookieStore(tmp_path).load()
    assert "bad-secret" not in str(error.value)


async def test_missing_cookie_never_calls_network(tmp_path):
    def unexpected(request):
        pytest.fail("未登录不能发起网络请求")

    client = BreezeClient(tmp_path, transport=httpx.MockTransport(unexpected))
    try:
        result = await client.status()
        assert not result.ok and result.need_login
    finally:
        await client.close()


async def test_claim_headers_and_success(client_factory):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "breezeblue.ai"
        assert request.headers["origin"] == "https://breezeblue.ai"
        assert request.headers["referer"] == "https://breezeblue.ai/app/home"
        assert "breeze_blue_session=test-session-secret" in request.headers["cookie"]
        if request.method == "GET":
            return httpx.Response(200, json=summary())
        assert json.loads(request.content) == {}
        return httpx.Response(
            200, json=summary(claimed=True, credits_issued=400, balance=500, daily_claim_claimed_today=True)
        )

    result = await client_factory(handler).claim()
    assert result.ok and result.claimed and result.balance == 500 and result.credits == 400
    assert [request.method for request in requests] == ["GET", "POST"]


async def test_already_claimed_never_posts(client_factory):
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json=summary(daily_claim_claimed_today=True))

    result = await client_factory(handler).claim()
    assert result.ok and not result.claimed and result.claimed_today
    assert requests == ["GET"]


async def test_unavailable_does_not_report_already_claimed(client_factory):
    result = await client_factory(
        lambda request: httpx.Response(
            200,
            json=summary(daily_claim_available=False),
        )
    ).claim()
    assert not result.ok and "暂不可" in result.message


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_http_errors_are_safe(client_factory, status):
    result = await client_factory(lambda request: httpx.Response(status, text="test-session-secret")).status()
    assert not result.ok
    assert "test-session-secret" not in result.message
    assert result.need_login == (status in (401, 403))


@pytest.mark.parametrize("body", ["<html>login</html>", "[]", "null", '{"balance": 100}'])
async def test_unexpected_json_and_schema(client_factory, body):
    result = await client_factory(lambda request: httpx.Response(200, text=body)).status()
    assert not result.ok


async def test_post_timeout_is_not_blindly_retried(client_factory):
    requests = []

    def handler(request):
        requests.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("secret detail", request=request)
        return httpx.Response(200, json=summary())

    result = await client_factory(handler).claim()
    assert not result.ok and "超时" in result.message
    assert "secret detail" not in result.message
    assert requests == ["GET", "POST"]


async def test_expired_session_refreshes_once(client_factory, tmp_path):
    (tmp_path / "profile").mkdir()
    client = client_factory(lambda request: httpx.Response(401), deployment_mode="desktop")
    client.refresh = AsyncMock(return_value=BreezeResult(True, "已刷新", claimed_today=True))
    result = await client.claim()
    assert result.ok and result.claimed_today
    client.refresh.assert_awaited_once()


def test_claim_must_be_confirmed():
    result = parse_result(summary(claimed=False), claim=True)
    assert not result.ok
    result = parse_result(summary(claimed=True, credits_issued="400", balance="500"), claim=True)
    assert result.ok and result.credits == 400 and result.balance == 500
    assert parse_result(summary(balance="nan")).balance is None
