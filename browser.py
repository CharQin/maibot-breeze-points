"""在 Runner 的 Python 环境中准备 Playwright 浏览器，不阻塞插件加载。"""

import asyncio
import importlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


class BrowserSetupError(Exception):
    """可直接显示给用户的依赖准备错误，不包含安装程序的原始输出。"""


SERVER_LOGIN_HELP = (
    "服务器模式：无需安装浏览器。请在自己的电脑浏览器登录 Breeze，"
    "把 breeze_blue_session 的值填入「Breeze 会话 Cookie」，保存配置后点击「导入登录态」。"
)


def has_graphical_desktop() -> bool:
    """检测运行 Runner 的环境；显式配置可覆盖自动检测。"""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform == "win32":
        # Windows 服务的会话 0 无法向用户显示登录窗口。
        import ctypes

        session = ctypes.c_uint32()
        if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return False
        return session.value != 0
    if sys.platform == "darwin":
        try:
            return os.stat("/dev/console").st_uid == os.getuid() and os.getuid() != 0
        except OSError:
            return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_deployment_mode(mode: str) -> str:
    return ("desktop" if has_graphical_desktop() else "server") if mode == "auto" else mode


def browser_error_message(error: Exception, channel: str) -> str:
    message = str(error).lower()
    if any(part in message for part in ("missing x server", "$display", "cannot open display", "ozone_platform_x11")):
        return "当前运行 MaiBot 的环境没有可用的图形桌面；请将部署模式设为 server，在配置页导入登录态。"
    if any(part in message for part in ("processsingleton", "singletonlock", "profile is already in use")):
        return "浏览器登录配置正被占用，请关闭之前的 Breeze 登录窗口后重试。"
    if any(part in message for part in ("missing dependencies", "error while loading shared libraries")):
        return "缺少浏览器运行所需的系统库，请由管理员补齐系统库，或在有桌面的电脑登录后导入 Cookie。"
    if "executable doesn't exist" in message or "distribution" in message and "not found" in message:
        if channel != "chromium":
            name = "Microsoft Edge" if channel == "msedge" else "Google Chrome"
            return f"未找到 {name}，请在运行 MaiBot 的机器上安装该浏览器，或选择已安装的其他浏览器。"
        return "Chromium 文件不完整，请开启「自动安装浏览器」后重新点击登录，插件会自动补齐。"
    if any(part in message for part in ("net::err_", "timeout", "timed out")):
        return "Breeze 登录页面访问失败或超时，请检查运行 MaiBot 的机器的网络、代理和站点可用性。"
    return "浏览器启动或页面访问失败，请关闭之前的登录窗口后重试；无桌面服务器请导入 Cookie。"


class BrowserRuntime:
    def __init__(
        self,
        *,
        channel: str = "msedge",
        auto_install: bool = True,
        progress: Callable[[str], None] | None = None,
        install_timeout: float = 900,
    ):
        self.channel = channel
        self.launch_channel = channel
        self.auto_install = auto_install
        self.progress = ""
        self.error = ""
        self._notify = progress
        self._ready = False
        self._lock = asyncio.Lock()
        self._install_timeout = install_timeout

    def report(self, message: str) -> None:
        self.progress = message
        if self._notify is not None:
            self._notify(message)

    def invalidate(self) -> None:
        self._ready = False

    def _system_browser_channel(self) -> str | None:
        if os.name != "nt":
            return None
        roots = [os.environ.get(name) for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for channel, relative in (
            ("msedge", "Microsoft/Edge/Application/msedge.exe"),
            ("chrome", "Google/Chrome/Application/chrome.exe"),
        ):
            if any((Path(root) / relative).is_file() for root in roots if root):
                return channel
        return None

    async def ensure(self) -> None:
        async with self._lock:
            if self._ready:
                return
            self.error = ""
            try:
                importlib.import_module("playwright.async_api")
            except ImportError as exc:
                raise BrowserSetupError(
                    "Playwright 尚未就绪，请重载插件，让 MaiBot 根据插件清单自动安装 Python 依赖。"
                ) from exc
            if self.channel == "chromium" and self.auto_install:
                system_channel = self._system_browser_channel()
                if system_channel:
                    self.launch_channel = system_channel
                    name = "Edge" if system_channel == "msedge" else "Chrome"
                    self.report(f"浏览器已准备完成，自动复用本机 {name}，无需下载 Chromium。")
                else:
                    self.launch_channel = "chromium"
                    self.report("正在自动准备 Chromium，首次下载可能需要几分钟，请稍候。")
                    await self._install()
                    self.report("Chromium 已准备完成。")
            else:
                name = {"msedge": "Microsoft Edge", "chrome": "Google Chrome"}.get(self.channel)
                self.report(
                    f"已选择本机 {name}，无需下载 Chromium。" if name
                    else "自动安装已关闭，将使用已安装的 Chromium。"
                )
            self._ready = True

    async def _stop_installer(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            # python -m playwright 会再启动 Node；只杀父进程会遗留下载任务。
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    await asyncio.wait_for(killer.communicate(), 10)
                except asyncio.TimeoutError:
                    killer.kill()
                    await killer.communicate()
            except OSError:
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()

    async def _install(self) -> None:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **options,
            )
        except OSError as exc:
            raise BrowserSetupError("无法启动 Chromium 自动安装程序，请检查 Python 环境和执行权限。") from exc
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self._install_timeout)
        except asyncio.TimeoutError as exc:
            await self._stop_installer(process)
            raise BrowserSetupError("Chromium 自动安装超时，请检查下载网络；重新点击登录会再次尝试。") from exc
        except asyncio.CancelledError:
            await self._stop_installer(process)
            raise
        if process.returncode:
            detail = output.decode("utf-8", errors="replace").lower()
            if "enospc" in detail or "no space left" in detail:
                reason = "磁盘空间不足"
            elif any(part in detail for part in ("eacces", "eperm", "permission denied", "access is denied")):
                reason = "浏览器缓存目录没有写入权限"
            else:
                reason = "请检查下载网络、代理、证书和缓存目录权限"
            raise BrowserSetupError(
                f"Chromium 自动安装失败（退出码 {process.returncode}）：{reason}；重新点击登录会再次尝试。"
            )
