# Breeze 积分助手

为 MaiBot 添加 BreezeBlue 每日积分自动领取、余额查询和手动领取，支持云服务器与本地部署。由 [dsh-breeze-claim](https://github.com/mi-tuan/dsh-breeze-claim) 适配而来。

## 安装

适用于 **MaiBot 1.3.19～1.4.x / SDK 2.20～2.23**。

1. 将 `breeze_claim_plugin` 文件夹放入 MaiBot 的 `plugins/` 目录，重启或重载插件。
2. MaiBot 自动安装 Python 依赖。打开 **插件管理 → Breeze 积分助手 → 配置**，按下面的方式登录。

升级时替换插件文件，保留原有 `config.toml` 和插件数据目录，然后重启 MaiBot。

## 登录

### 云服务器 / Docker

服务器无需安装 Edge、Chromium 或图形桌面，也无需运行安装命令。

1. 在自己的电脑浏览器打开 [Breeze](https://breezeblue.ai/app/home) 并登录。
2. 按 **F12 → 应用/Application → Cookie → breezeblue.ai**，复制 `breeze_blue_session` 的**值（Value）**。
3. 在 MaiBot 插件配置页的 **Breeze 会话 Cookie** 中粘贴，**保存配置**，再点击 **导入登录态**。显示验证成功和余额后即可使用。

也可直接粘贴完整 Cookie 请求头，或原 DSH 版 `cookies.json` 的内容。导入失败会保留原登录态；过期后重新导入。导入成功后可清空输入框并保存，已保存的登录态仍可用。请勿分享含 Cookie 的配置文件。

### 本地电脑

在插件配置页点击 **登录 Breeze**，在运行 MaiBot 的电脑上弹出的 Edge 窗口里登录即可。可将“登录浏览器”改为已安装的 `chrome`；选择 `chromium` 时插件按需自动下载。

部署模式默认 `auto`：有桌面时使用本地浏览器，无桌面时使用服务器方式。云端建议直接设为 `server`；本地识别不准时设为 `desktop`。页面的“部署与登录”会显示当前方式。

## 使用与配置

默认每天 **北京时间 08:10** 自动领取；启动后检查补领，失败每隔 15 分钟重试，每周期最多 3 次。也可以点击 **现在领取**。

| 常用配置 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 插件总开关 |
| `schedule.enabled` | `true` | 自动领取开关 |
| `schedule.claim_time` | `"08:10"` | 每日领取时间，格式 `HH:MM` |
| `schedule.timezone` | `"Asia/Shanghai"` | 调度时区 |
| `account.deployment_mode` | `"auto"` | 自动检测；`server` 云端；`desktop` 本地桌面 |
| `account.session_cookie` | `""` | 会话值、Cookie 头或 Cookie JSON，保存后点击导入 |
| `account.browser_channel` | `"msedge"` | 仅桌面模式：`msedge` / `chrome` / `chromium` |
| `account.auto_install_browser` | `true` | 仅桌面模式准备浏览器，服务器自动跳过 |
| `commands.enabled` | `false` | 聊天命令开关 |
| `commands.allowed_users` | `[]` | 例如 `["qq:123456789"]`，空列表拒绝聊天操作 |

所有配置均有中文说明，保存后生效。完整示例见 [config.example.toml](config.example.toml)。插件管理一个共享 Breeze 账号。

启用聊天命令并配置允许用户后，可用 `/breeze 查询`、`/breeze 领取`、`/breeze 刷新`，也支持 `status`、`claim`、`refresh`。服务器上的“刷新”仅验证已保存的登录态，过期后需重新导入。

## 常见问题

- **云端提示没有 Edge / 还在准备 Chromium**：升级后把部署模式设为 `server` 并保存，会停止旧的浏览器任务，然后按上面的云端步骤导入。
- **本地看不到登录按钮**：将部署模式设为 `desktop`；MaiBot 必须在有图形桌面的用户会话中运行。
- **导入失败**：确认复制的是 `breeze_blue_session` 的完整值、已保存配置，且服务器可以访问 Breeze；登录过期后重新复制。
- **没有自动领取**：检查两个启用开关、领取时间和“最近操作”。
