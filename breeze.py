"""Breeze 网页接口及可选浏览器登录；不依赖 MaiBot 主程序。"""

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .browser import (
    SERVER_LOGIN_HELP,
    BrowserRuntime,
    BrowserSetupError,
    browser_error_message,
    resolve_deployment_mode,
)
from .storage import write_json

BASE_URL = "https://breezeblue.ai"
SESSION_COOKIE = "breeze_blue_session"


class CookieError(ValueError):
    pass


def parse_cookie_input(text: str) -> list[dict[str, Any]]:
    """配置页支持单个会话值、Cookie 请求头及旧版 JSON，错误不包含输入。"""
    text = text.strip()
    if not text:
        raise CookieError("请先填写「Breeze 会话 Cookie」并保存配置，再点击「导入登录态」。")
    if len(text) > 1024 * 1024:
        raise CookieError("Cookie 内容超过 1 MiB，请检查输入。")
    if text.startswith(("[", "{")):
        try:
            return normalize_cookies(json.loads(text))
        except (ValueError, RecursionError) as exc:
            raise CookieError("Cookie JSON 格式无效或缺少有效的 Breeze 会话，请重新复制。") from exc
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if "breeze_blue_session=" in text:
        values = [
            part.strip().partition("=")[2] for part in text.split(";")
            if part.strip().partition("=")[0] == SESSION_COOKIE
        ]
        if len(values) != 1:
            raise CookieError("Cookie 请求头中应包含一个 breeze_blue_session，请重新复制。")
        text = values[0]
    if not text or len(text) > 16384 or any(
        ord(char) <= 32 or ord(char) >= 127 or char in ';,"\\' for char in text
    ):
        raise CookieError("会话 Cookie 格式无效，请复制 breeze_blue_session 的完整值。")
    return normalize_cookies([{"name": SESSION_COOKIE, "value": text, "domain": "breezeblue.ai", "path": "/"}])


def normalize_cookies(data: Any, now: float | None = None) -> list[dict[str, Any]]:
    """接受原版 Cookie 数组或 Playwright storage_state，严格筛选目标站点。"""
    if isinstance(data, dict):
        data = data.get("cookies")
    if not isinstance(data, list):
        raise CookieError("Cookie 文件应为 Cookie 数组，或包含 cookies 数组的 Playwright storage_state。")
    result = []
    now = time.time() if now is None else now
    for cookie in data:
        if not isinstance(cookie, dict) or str(cookie.get("domain", "")).lstrip(".").lower() != "breezeblue.ai":
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
            continue
        if any(ord(char) < 32 or ord(char) >= 127 or char in ";\r\n" for char in name + value):
            continue
        expires = cookie.get("expires", -1)
        if not isinstance(expires, (float, int)) or not math.isfinite(expires):
            continue
        if expires > 0 and expires <= now:
            continue
        path = cookie.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        result.append(
            {
                "name": name,
                "value": value,
                "domain": cookie["domain"],
                "path": path,
                "expires": expires,
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", True)),
                "sameSite": cookie.get("sameSite", "Lax"),
            }
        )
    if not any(
        cookie["name"] == SESSION_COOKIE
        and (cookie["path"] == "/" or cookie["path"] == "/api" or cookie["path"] == "/api/")
        for cookie in result
    ):
        raise CookieError("未找到有效的 Breeze 会话 Cookie，请重新登录或导入未过期的 cookies.json。")
    return result


class CookieStore:
    def __init__(self, root: Path):
        self.path = root / "cookies.json"

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise CookieError("尚未登录 Breeze，请在配置页导入登录态；本地桌面也可点击「登录 Breeze」。")
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise CookieError("Cookie 文件超过 1 MiB，请检查文件格式。")
            return normalize_cookies(json.loads(self.path.read_text("utf-8-sig")))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise CookieError("cookies.json 不是有效的 UTF-8 JSON，请重新导入。") from exc

    def save(self, cookies: Any) -> None:
        write_json(self.path, normalize_cookies(cookies))


@dataclass(frozen=True)
class BreezeResult:
    ok: bool
    message: str
    claimed: bool = False
    balance: float | None = None
    credits: float | None = None
    claimed_today: bool | None = None
    available: bool | None = None
    amount: float | None = None
    next_reset_at: str | None = None
    need_login: bool = False


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_result(data: dict[str, Any], *, claim: bool = False) -> BreezeResult:
    claimed_today = data.get("daily_claim_claimed_today")
    available = data.get("daily_claim_available")
    if not isinstance(claimed_today, bool) and not isinstance(available, bool):
        return BreezeResult(False, "Breeze 响应缺少领取状态，接口可能已变更，请更新插件。")
    got = data.get("claimed") is True if claim else False
    if claim and not got and claimed_today is not True:
        return BreezeResult(False, "Breeze 未确认领取成功，请刷新状态后再试。")
    credits = number(data.get("credits_issued"))
    message = "状态已更新"
    if claim:
        message = (
            f"领取成功，获得 {credits:g} 积分"
            if got and credits is not None
            else ("领取成功" if got else "当前周期已经领取过了")
        )
    return BreezeResult(
        True,
        message,
        claimed=got,
        balance=number(data.get("balance")),
        credits=credits,
        claimed_today=claimed_today if isinstance(claimed_today, bool) else None,
        available=available if isinstance(available, bool) else None,
        amount=number(data.get("daily_claim_amount")),
        next_reset_at=data.get("daily_claim_next_reset_at")
        if isinstance(data.get("daily_claim_next_reset_at"), str)
        else None,
    )


class BreezeClient:
    def __init__(
        self,
        root: Path,
        *,
        timeout: int = 30,
        browser_channel: str = "msedge",
        login_timeout: int = 300,
        auto_refresh: bool = True,
        auto_install_browser: bool = True,
        deployment_mode: str = "auto",
        progress: Callable[[str], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.root = root
        self.cookies = CookieStore(root)
        self.browser_channel = browser_channel
        self.login_timeout = login_timeout
        self.auto_refresh = auto_refresh
        self.deployment_mode = resolve_deployment_mode(deployment_mode)
        self.browser = BrowserRuntime(channel=browser_channel, auto_install=auto_install_browser, progress=progress)
        if self.is_server:
            self.browser.report(SERVER_LOGIN_HELP)
        else:
            self.browser.report("桌面模式：点击「登录 Breeze」后，窗口将在运行 MaiBot 的电脑上打开。")
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/app/home",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            },
        )

    async def close(self) -> None:
        await self.http.aclose()

    @property
    def is_server(self) -> bool:
        return self.deployment_mode == "server"

    async def import_session(self, text: str) -> BreezeResult:
        try:
            cookies = parse_cookie_input(text)
        except CookieError as exc:
            return BreezeResult(False, str(exc), need_login=True)
        response = await self._request("/api/credits/summary", cookies=cookies)
        result = response if isinstance(response, BreezeResult) else parse_result(response)
        if result.ok:
            self.cookies.save(cookies)
            return replace(result, message="登录态已导入并通过验证，可以查询和领取积分。")
        return replace(result, message="登录态导入失败：" + result.message)

    async def _request(self, path: str, method: str = "GET", *, cookies: Any = None) -> dict[str, Any] | BreezeResult:
        try:
            source = self.cookies.load() if cookies is None else normalize_cookies(cookies)
        except CookieError as exc:
            return BreezeResult(False, str(exc), need_login=True)
        jar = httpx.Cookies()
        for cookie in source:
            jar.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        # 每次请求使用最新文件，避免 Cookie 轮换或重新导入后复用过期的客户端 Cookie。
        request = self.http.build_request(method, path, json={} if method == "POST" else None)
        request.headers.pop("cookie", None)
        jar.set_cookie_header(request)
        try:
            response = await self.http.send(request)
        except httpx.TimeoutException:
            return BreezeResult(False, "Breeze 请求超时，请检查网络或稍后重试。")
        except httpx.RequestError:
            return BreezeResult(False, "无法连接 Breeze，请检查网络、代理和证书设置。")
        if response.status_code in (401, 403):
            action = "请在配置页重新导入登录态" if self.is_server else "请重新登录"
            return BreezeResult(
                False,
                f"Breeze 拒绝访问（HTTP {response.status_code}），{action}；若仍失败请检查站点访问限制。",
                need_login=True,
            )
        if response.status_code == 429:
            return BreezeResult(False, "Breeze 请求过于频繁（HTTP 429），请等待后重试。")
        if response.status_code != 200:
            return BreezeResult(False, f"Breeze 返回 HTTP {response.status_code}，请稍后重试或检查接口变化。")
        try:
            data = response.json()
        except ValueError:
            return BreezeResult(False, "Breeze 返回的不是有效 JSON，可能遇到登录页或站点验证。")
        if not isinstance(data, dict):
            return BreezeResult(False, "Breeze 响应格式不正确，请更新插件或检查站点状态。")
        return data

    async def status(self) -> BreezeResult:
        response = await self._request("/api/credits/summary")
        return response if isinstance(response, BreezeResult) else parse_result(response)

    async def claim(self) -> BreezeResult:
        before = await self.status()
        if (
            not before.ok and before.need_login and self.auto_refresh
            and not self.is_server and (self.root / "profile").exists()
        ):
            refreshed = await self.refresh()
            if not refreshed.ok:
                return refreshed
            before = refreshed
        if not before.ok:
            return before
        if before.claimed_today:
            return replace(before, message="当前周期已经领取过了")
        if before.available is False:
            return replace(before, ok=False, message="当前周期暂不可领取，请等待下次重置。")
        response = await self._request("/api/credits/daily-claim", "POST")
        if isinstance(response, BreezeResult):
            return response
        result = parse_result(response, claim=True)
        # POST 不做盲目重试；网络异常后的下一轮会先查询服务端状态。
        return result

    async def prepare(self) -> BreezeResult:
        if self.is_server:
            self.browser.report(SERVER_LOGIN_HELP)
            return BreezeResult(True, SERVER_LOGIN_HELP)
        try:
            await self.browser.ensure()
        except BrowserSetupError as exc:
            self.browser.error = str(exc)
            self.browser.report(str(exc))
            return BreezeResult(False, str(exc))
        return BreezeResult(True, "浏览器依赖已准备完成；首次使用请点击「登录 Breeze」或导入 Cookie。")

    async def _browser_session(self, interactive: bool) -> BreezeResult:
        if self.is_server:
            if interactive:
                return BreezeResult(False, SERVER_LOGIN_HELP, need_login=True)
            result = await self.status()
            return replace(result, message="已保存的登录态验证通过。") if result.ok else result
        profile = self.root / "profile"
        if not interactive and not profile.exists():
            return BreezeResult(False, "没有可刷新的浏览器登录配置，请先登录或重新导入 Cookie。", need_login=True)
        prepared = await self.prepare()
        if not prepared.ok:
            return prepared
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError:
            return BreezeResult(
                False, "Playwright 无法加载，请重载插件以重新检查依赖。", need_login=True
            )
        context = None
        try:
            self.browser.report("正在打开 Breeze 登录页面。" if interactive else "正在刷新浏览器登录态。")
            async with async_playwright() as playwright:
                launch = {"user_data_dir": str(profile), "headless": not interactive}
                if self.browser.launch_channel != "chromium":
                    launch["channel"] = self.browser.launch_channel
                context = await playwright.chromium.launch_persistent_context(**launch)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(BASE_URL + "/app/home", wait_until="domcontentloaded", timeout=60000)
                    if interactive:
                        self.browser.report("请在运行 MaiBot 的机器上打开的浏览器窗口中完成登录。")
                    deadline = time.monotonic() + (self.login_timeout if interactive else 15)
                    while time.monotonic() < deadline:
                        if page.is_closed():
                            break
                        cookies = await context.cookies(BASE_URL)
                        try:
                            valid = normalize_cookies(cookies)
                        except CookieError:
                            valid = None
                        if valid:
                            response = await self._request("/api/credits/summary", cookies=valid)
                            result = response if isinstance(response, BreezeResult) else parse_result(response)
                            if result.ok:
                                self.cookies.save(valid)
                                return replace(
                                    result,
                                    message="登录成功，已验证并保存登录态" if interactive else "登录态已刷新并通过验证",
                                )
                            if not result.need_login:
                                return result
                        await asyncio.sleep(2)
                    return BreezeResult(False, "未检测到有效登录，请重新登录或导入 Cookie。", need_login=True)
                finally:
                    await context.close()
        except PlaywrightError as exc:
            # 不透传浏览器异常：它可能含 profile 路径、页面内容或敏感请求信息。
            self.browser.invalidate()
            return BreezeResult(
                False,
                browser_error_message(exc, self.browser.launch_channel),
                need_login=True,
            )

    async def login(self) -> BreezeResult:
        return await self._browser_session(True)

    async def refresh(self) -> BreezeResult:
        return await self._browser_session(False)
