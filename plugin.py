"""MaiBot SDK 插件入口。"""

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import tomllib
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, ConfigWidget, ConfigWidgetState, MaiBotPlugin

from .breeze import BreezeClient, BreezeResult
from .config import BreezeConfig
from .schedule import cycle_bounds, is_due, next_reset
from .storage import BusyError, DataLease, ScheduleState

COMMAND_PATTERN = r"^/(?:breeze|breeze积分)(?:\s+(?P<operation>\S+))?\s*$"
HELP = "Breeze 积分：/breeze status 查询；/breeze claim 领取；/breeze refresh 刷新登录态。浏览器登录请使用配置页。"
CONFIG_PATH = Path(__file__).with_name("config.toml")


def _mapping(value: Any) -> Mapping:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


def sender_key(message: Any) -> str:
    """从 Host 消息结构或 SDK MaiMessages 中读取身份，不能从命令文本推断。"""
    data = _mapping(message)
    if "raw_message" in data and isinstance(data["raw_message"], Mapping):
        raw_key = sender_key(data["raw_message"])
        if raw_key:
            return raw_key
    info = _mapping(data.get("message_info") or data.get("message_base_info"))
    user = _mapping(info.get("user_info"))
    platform = str(data.get("platform") or info.get("platform") or "").strip()
    user_id = str(user.get("user_id") or "").strip()
    return f"{platform}:{user_id}" if platform and user_id else ""


def format_result(result: BreezeResult) -> str:
    lines = [result.message]
    if result.ok:
        if result.balance is not None:
            lines.append(f"余额：{result.balance:g}")
        if result.claimed_today is not None:
            lines.append("本周期：已领取" if result.claimed_today else "本周期：未领取")
        if result.amount is not None:
            lines.append(f"每次可领：{result.amount:g}")
    return "\n".join(lines)


class BreezeClaimPlugin(MaiBotPlugin):
    config_model = BreezeConfig
    config_widgets = [
        ConfigWidget(id="account", kind="status", label="Breeze 账号状态"),
        ConfigWidget(id="schedule", kind="status", label="自动领取"),
        ConfigWidget(id="result", kind="status", label="最近操作"),
        ConfigWidget(id="environment", kind="status", label="部署与登录"),
        ConfigWidget(id="status", kind="action", label="刷新积分状态"),
        ConfigWidget(id="claim", kind="action", label="现在领取"),
        ConfigWidget(id="login", kind="action", label="登录 Breeze"),
        ConfigWidget(id="import_cookie", kind="action", label="导入登录态"),
        ConfigWidget(id="refresh", kind="action", label="验证 / 刷新登录态"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._client: BreezeClient | None = None
        self._scheduler: asyncio.Task | None = None
        self._job: asyncio.Task | None = None
        self._jobs: set[asyncio.Task] = set()
        self._busy = ""
        self._last: BreezeResult | None = None
        self._last_at: datetime | None = None
        self._state = ScheduleState()

    @property
    def settings(self) -> BreezeConfig:
        return cast(BreezeConfig, self.config)

    def normalize_plugin_config(self, config_data: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        data = deepcopy(dict(config_data or {}))
        legacy = _mapping(data.get("plugin")).get("config_version") == "1.0.0"
        if not legacy and CONFIG_PATH.is_file():
            # Host 1.3.19 会先重建旧配置，提前替换版本号；从原文件识别这次升级。
            try:
                with CONFIG_PATH.open("rb") as handle:
                    stored = tomllib.load(handle)
                legacy = _mapping(stored.get("plugin")).get("config_version") == "1.0.0"
            except (OSError, ValueError):
                pass
        if legacy:
            account = data.get("account")
            if isinstance(account, dict) and account.get("browser_channel") == "chromium":
                account["browser_channel"] = "msedge"
            section = data.setdefault("plugin", {})
            if isinstance(section, dict):
                section["config_version"] = "1.0.2"
        section = data.setdefault("plugin", {})
        if isinstance(section, dict):
            section["config_version"] = "1.0.2"
        normalized, changed = super().normalize_plugin_config(data)
        return normalized, changed or normalized != config_data

    def _create_client(self) -> None:
        account = self.settings.account
        self._client = BreezeClient(
            self.ctx.paths.data_dir,
            timeout=account.request_timeout_seconds,
            browser_channel=account.browser_channel,
            login_timeout=account.login_timeout_seconds,
            auto_refresh=account.auto_refresh,
            auto_install_browser=account.auto_install_browser,
            deployment_mode=account.deployment_mode,
            progress=lambda text: self.ctx.logger.info("Breeze：%s", text),
        )

    def _prepare_browser(self) -> None:
        if (
            self.settings.plugin.enabled and self.settings.account.auto_install_browser
            and self._client is not None and not self._client.is_server
        ):
            self._start_operation("prepare")

    async def on_load(self) -> None:
        self.ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self._state = ScheduleState.load(self.ctx.paths.data_dir)
        self._create_client()
        self._prepare_browser()
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="breeze-claim-scheduler")
        self.ctx.logger.info("Breeze 积分插件已加载，数据目录：%s", self.ctx.paths.data_dir)

    async def _stop(self) -> None:
        tasks = list(self._jobs)
        if self._scheduler is not None:
            tasks.append(self._scheduler)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduler = self._job = None
        self._jobs.clear()
        self._busy = ""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def on_unload(self) -> None:
        await self._stop()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        # Runner 已注入新配置。先取消旧任务、关闭旧客户端，再按新配置启动。
        await self._stop()
        self._create_client()
        self._prepare_browser()
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="breeze-claim-scheduler")
        self.ctx.logger.info("Breeze 配置已更新：%s", version)

    def _start_operation(self, operation: str, *, automatic: bool = False, stream_id: str = "") -> str:
        if not self.settings.plugin.enabled:
            return "插件已关闭，请先启用插件。"
        if self._busy:
            return "已有 Breeze 操作正在进行，请稍后重试。"
        if self._client is None:
            return "插件尚未完成初始化。"
        self._busy = operation
        self._job = asyncio.create_task(
            self._run_operation(operation, automatic=automatic, stream_id=stream_id),
            name=f"breeze-{operation}",
        )
        self._jobs.add(self._job)
        self._job.add_done_callback(self._jobs.discard)
        return ""

    async def _run_operation(self, operation: str, *, automatic: bool, stream_id: str) -> None:
        result = None
        try:
            with DataLease(self.ctx.paths.data_dir):
                self._state = ScheduleState.load(self.ctx.paths.data_dir)
                now = datetime.now(timezone.utc)
                if automatic:
                    # 获得跨进程锁后重新检查，避免热重载新旧 Runner 各领取一次。
                    if not is_due(now, self.settings.schedule, self._state):
                        return
                    cycle = cycle_bounds(now, self.settings.schedule)[0].isoformat()
                    if self._state.cycle != cycle:
                        self._state.cycle = cycle
                        self._state.attempts = 0
                    self._state.attempts += 1
                    self._state.last_attempt = now.timestamp()
                    self._state.save(self.ctx.paths.data_dir)
                client = self._client
                if client is None:
                    return
                result = (
                    await client.import_session(self.settings.account.session_cookie)
                    if operation == "import_cookie" else await getattr(client, operation)()
                )
                if result.ok and operation in {"login", "refresh", "import_cookie"}:
                    # 导入已验证账号后，清除旧账号的领取周期和重试限制。
                    self._state.attempts = 0
                    self._state.last_attempt = 0
                    self._state.success_until = 0
                if result.ok and (result.claimed or result.claimed_today is True):
                    self._state.success_until = next_reset(
                        datetime.now(timezone.utc),
                        self.settings.schedule,
                        result.next_reset_at,
                    )
                    self._state.save(self.ctx.paths.data_dir)
                # 登录/刷新成功解除当前周期的失败次数限制，稍后自动补领。
                elif result.ok and operation in {"login", "refresh", "import_cookie"}:
                    self._state.save(self.ctx.paths.data_dir)
        except BusyError as exc:
            result = BreezeResult(False, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = BreezeResult(False, f"操作失败（{type(exc).__name__}），请检查数据文件权限及插件依赖。")
        finally:
            if result is not None:
                self._last = result
                self._last_at = datetime.now(timezone.utc)
                self.ctx.logger.info("Breeze %s：%s", operation, result.message)
            self._busy = ""
        if stream_id and result is not None:
            try:
                await self.ctx.send.text(format_result(result), stream_id)
            except Exception:
                self.ctx.logger.warning("Breeze 操作已完成，但聊天结果发送失败，请在配置页查看。")

    async def _schedule_loop(self) -> None:
        await asyncio.sleep(2)
        while self._busy == "prepare":
            await asyncio.sleep(1)
        if self.settings.plugin.enabled:
            self._start_operation("status")
        while True:
            await asyncio.sleep(self.settings.schedule.poll_interval_seconds)
            if not self.settings.plugin.enabled or not self.settings.schedule.enabled or self._busy:
                continue
            try:
                self._state = ScheduleState.load(self.ctx.paths.data_dir)
                if is_due(datetime.now(timezone.utc), self.settings.schedule, self._state):
                    self._start_operation("claim", automatic=True)
            except Exception as exc:
                self._last = BreezeResult(False, f"调度状态读取失败（{type(exc).__name__}），请检查 schedule.json。")
                self.ctx.logger.warning(self._last.message)

    async def get_config_widget_state(self) -> dict[str, ConfigWidgetState]:
        # WebUI 每约两秒请求；这里仅返回内存，不访问网络或读取 Cookie。
        result = self._last
        account_text = "尚未查询，请点击「刷新积分状态」；首次使用请先登录或导入 Cookie。"
        tone = "neutral"
        if result is not None:
            account_text = format_result(result)
            tone = "success" if result.ok else ("warning" if result.need_login else "error")
            if result.next_reset_at:
                try:
                    reset = datetime.fromisoformat(result.next_reset_at.replace("Z", "+00:00"))
                    if reset.tzinfo is not None:
                        account_text += "\n下次重置：" + reset.astimezone(
                            ZoneInfo(self.settings.schedule.timezone)
                        ).strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, OverflowError):
                    pass
        schedule = self.settings.schedule
        schedule_text = (
            f"每天 {schedule.claim_time}（{schedule.timezone}），"
            f"每周期最多 {schedule.max_attempts} 次，失败间隔 {schedule.retry_interval_seconds} 秒。"
            if schedule.enabled
            else "自动领取已关闭。"
        )
        if not self.settings.plugin.enabled:
            schedule_text = "插件已关闭。"
        last_text = (
            (
                self._client.browser.progress
                if self._client and self._busy in {"prepare", "login", "refresh"} and self._client.browser.progress
                else "正在处理 Breeze 操作，请稍候。"
            )
            if self._busy else (result.message if result else "暂无操作记录。")
        )
        if self._last_at and not self._busy:
            last_text += "\n" + self._last_at.astimezone(ZoneInfo(schedule.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
        disabled = bool(self._busy) or not self.settings.plugin.enabled
        states = {
            "account": ConfigWidgetState(text=account_text[:2000], tone=tone),
            "schedule": ConfigWidgetState(text=schedule_text),
            "result": ConfigWidgetState(text=last_text[:2000]),
            "environment": ConfigWidgetState(
                text=(self._client.browser.progress or "登录时将检查浏览器依赖。") if self._client else "尚未初始化。",
                tone="error" if self._client and self._client.browser.error else "neutral",
            ),
        }
        states.update({
            name: ConfigWidgetState(disabled=disabled)
            for name in ("status", "claim", "login", "refresh", "import_cookie")
        })
        if self._client and self._client.is_server:
            states["login"].visible = False
        return states

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id not in {"status", "claim", "login", "refresh", "import_cookie"}:
            raise ValueError("未知操作。")
        error = self._start_operation(action_id)
        if error:
            raise ValueError(error)

    @Command(
        "breeze_credits",
        description="Breeze 积分查询、领取和登录态刷新；仅配置的允许用户可用。",
        pattern=COMMAND_PATTERN,
    )
    async def handle_breeze(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        if not self.settings.plugin.enabled or not self.settings.commands.enabled:
            return False, "Breeze 聊天命令未启用。", 2
        if not stream_id:
            return False, "缺少聊天流信息。", 2
        # 当前 Host 会注入平台和用户 ID；兼容 SDK 标准消息时再读取消息结构。
        platform = str(kwargs.get("platform") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        identity = f"{platform}:{user_id}" if platform and user_id else sender_key(message)
        if not identity or identity not in self.settings.commands.allowed_users:
            await self.ctx.send.text("你没有 Breeze 积分操作权限，请联系管理员配置允许的用户。", stream_id)
            return False, "没有操作权限。", 2
        groups = _mapping(kwargs.get("matched_groups"))
        operation = str(groups.get("operation") or "help").lower()
        operation = {"查询": "status", "状态": "status", "领取": "claim", "刷新": "refresh", "帮助": "help"}.get(
            operation,
            operation,
        )
        if operation not in {"status", "claim", "refresh"}:
            await self.ctx.send.text(HELP, stream_id)
            return True, HELP, 2
        error = self._start_operation(operation, stream_id=stream_id)
        text = error or "Breeze 操作已开始，完成后会发送结果。"
        await self.ctx.send.text(text, stream_id)
        return not bool(error), text, 2


def create_plugin() -> BreezeClaimPlugin:
    return BreezeClaimPlugin()
