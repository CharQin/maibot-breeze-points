"""配置模型；Runner 据此生成 config.toml 和 WebUI 配置表单。"""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator


class PluginSection(PluginConfigBase):
    __ui_label__ = "基础设置"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.2", description="配置结构版本，由插件维护，请勿修改。")
    enabled: bool = Field(
        default=True,
        description="插件总开关；关闭后停止自动任务并禁用操作按钮和聊天命令。",
        json_schema_extra={"label": "启用插件"},
    )


class ScheduleSection(PluginConfigBase):
    __ui_label__ = "每日自动领取"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="每天到达领取时间后自动领取；错过时间会在启动后补领。",
        json_schema_extra={"label": "自动领取"},
    )
    claim_time: str = Field(
        default="08:10",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
        description="每日领取时间，24 小时制 HH:MM，按下面的时区执行。",
        json_schema_extra={"label": "领取时间", "placeholder": "08:10"},
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA 时区名，与服务器系统时区无关。",
        json_schema_extra={"label": "调度时区", "placeholder": "Asia/Shanghai"},
    )
    reset_time: str = Field(
        default="08:00",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
        description="领取周期的兜底重置时间；成功后优先使用 Breeze 返回的下次重置时间。",
        json_schema_extra={"label": "重置时间"},
    )
    poll_interval_seconds: int = Field(
        default=60,
        ge=15,
        le=3600,
        description="检查是否到点的间隔，单位秒。",
        json_schema_extra={"label": "检查间隔（秒）"},
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="每个重置周期最多自动尝试次数，重启后仍保留。",
        json_schema_extra={"label": "每周期最多尝试"},
    )
    retry_interval_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
        description="自动领取失败后的重试间隔，单位秒。",
        json_schema_extra={"label": "失败重试间隔（秒）"},
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("请填写有效 IANA 时区，例如 Asia/Shanghai；Windows 需要安装 tzdata。") from exc
        return value


class AccountSection(PluginConfigBase):
    __ui_label__ = "登录与网络"
    __ui_order__ = 2

    deployment_mode: Literal["auto", "server", "desktop"] = Field(
        default="auto",
        description="auto 自动检测；server 适用于云服务器、Docker 和无桌面环境，通过 Cookie 登录，"
        "无需浏览器；desktop 在运行 MaiBot 的机器上打开登录窗口。检测不准时可手动选择。",
        json_schema_extra={"label": "部署模式"},
    )
    session_cookie: str = Field(
        default="",
        repr=False,
        description="在自己电脑登录 Breeze，按 F12 → 应用/Application → Cookie，"
        "复制 breeze_blue_session 的值。粘贴后保存配置，再点击「导入登录态」。"
        "也接受完整 Cookie 请求头或 cookies.json 内容；导入成功后可清空此项并保存，已保存的登录态仍可用。"
        "此项会保存在 config.toml 中，请勿分享。",
        json_schema_extra={"label": "Breeze 会话 Cookie", "x-widget": "password", "input_type": "password"},
    )
    auto_install_browser: bool = Field(
        default=True,
        description="仅桌面模式生效：首次加载时准备所选浏览器。默认使用已安装的 Edge；"
        "选择 chromium 时可能自动下载。服务器模式始终跳过浏览器准备。",
        json_schema_extra={"label": "自动安装浏览器"},
    )
    browser_channel: Literal["chromium", "msedge", "chrome"] = Field(
        default="msedge",
        description="默认 msedge，直接使用已安装的 Microsoft Edge；chrome 使用已安装的 Chrome。"
        "主动选择 chromium 才使用 Chromium 自动准备流程。"
        "仅桌面模式生效，窗口出现在运行 MaiBot 的机器上。",
        json_schema_extra={"label": "登录浏览器"},
    )
    login_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=600,
        description="等待人工完成浏览器登录的最长秒数。",
        json_schema_extra={"label": "登录等待时间（秒）"},
    )
    auto_refresh: bool = Field(
        default=True,
        description="仅桌面模式：领取前检测到登录失效时，尝试用已保存的浏览器配置无头刷新一次。"
        "服务器模式使用导入的 Cookie，过期后需重新导入。",
        json_schema_extra={"label": "自动刷新登录态"},
    )
    request_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="单次 HTTP 请求超时，单位秒。",
        json_schema_extra={"label": "请求超时（秒）"},
    )


class CommandsSection(PluginConfigBase):
    __ui_label__ = "聊天命令权限"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False,
        description="是否启用 /breeze 命令；配置页按钮不受此开关影响。",
        json_schema_extra={"label": "启用聊天命令"},
    )
    allowed_users: list[str] = Field(
        default_factory=list,
        description="允许操作此共享 Breeze 账号的用户，格式 platform:user_id，"
        "例如 qq:123456789。空列表拒绝所有聊天操作。",
        json_schema_extra={"label": "允许的用户"},
    )

    @field_validator("allowed_users")
    @classmethod
    def validate_users(cls, value: list[str]) -> list[str]:
        result = []
        for entry in value:
            entry = entry.strip()
            platform, separator, user_id = entry.partition(":")
            if not separator or not platform or not user_id or any(c.isspace() for c in entry):
                raise ValueError("允许的用户必须使用 platform:user_id 格式，例如 qq:123456789。")
            if entry not in result:
                result.append(entry)
        return result


class BreezeConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    account: AccountSection = Field(default_factory=AccountSection)
    commands: CommandsSection = Field(default_factory=CommandsSection)
