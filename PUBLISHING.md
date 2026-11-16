# 发布到 MaiBot 插件市场

本目录就是 GitHub 仓库根目录。上传本目录中的文件和子目录，确保仓库首页直接包含 `_manifest.json`、`plugin.py`、`LICENSE` 和 `README.md`，不要再包一层 `breeze_claim_plugin/`。

请保留子目录层级，尤其是 `assets/icon.svg` 和 `tests/`。不要把图标及所有测试文件移动到根目录：清单引用 `assets/icon.svg`，测试配置指定 `tests/`。

## 发布信息

| 字段 | 当前值 |
| --- | --- |
| 维护者 | `CharQin` |
| 发布仓库 | `https://github.com/CharQin/maibot-breeze-points` |
| 插件 ID | `mi-tuan.breeze-claim`，沿用现有身份，后续升级保持不变 |
| 版本 | `1.0.4` |
| Release Tag | `v1.0.4` 或 `1.0.4` |

- `_manifest.json` 中的作者及仓库已填写实际发布信息。仓库提交地址与 `urls.repository` 必须一致。
- 保留上游 MIT 版权和 README 中的来源说明；上游项目地址与当前插件的发布仓库可以不同。
- 首次登记前确定唯一、稳定的插件 `id`；登记后升级继续使用同一 ID。
- 当前插件版本为 `1.0.4`。`config_version` 是配置结构版本，可以继续为 `1.0.2`。
- 保留 `/config.toml` 等 `.gitignore` 规则，只提交空 Cookie 的配置示例，不提交运行数据、登录态或浏览器 profile。

## 验证并发布 Release

1. 安装 `requirements-dev.txt`，在仓库根目录运行 `python -m pytest -q` 和 `python -m ruff check .`。验证范围见 [VALIDATION.md](VALIDATION.md)。
2. 建议在目标 MaiBot 中完成真实账号登录或导入、余额查询和领取验证；尚未实测时应保留验证范围说明。
3. 提交源码，确认该提交的 `_manifest.json` 中 `version` 为 `1.0.4`，仓库地址和作者信息正确，GitHub 仓库可公开访问。
4. 在该提交创建 `v1.0.4` 或 `1.0.4` Tag，然后发布对应 GitHub Release。仅创建 Tag 或保存草稿不满足发布要求；GitHub 自动生成的源码包即可。

不要仅向仓库上传本地 ZIP：市场读取的是仓库根目录及 Release 对应提交的文件。安装包中的 `breeze_claim_plugin/` 是手动安装目录，不代表仓库也要增加这一层。

本地交付提供两种 ZIP：`maibot-breeze-points-v1.0.4-source.zip` 解压后直接得到仓库根目录内容；`breeze-claim-maibot-v1.0.4.zip` 内含用于手动安装的 `breeze_claim_plugin/` 文件夹。更新 GitHub 仓库时先解压源码包，再上传其中的文件及 `assets/`、`tests/` 子目录，包含 `.gitignore`。如果之前已将这些子目录拍平成根目录文件，应移除旧的根目录 `icon.svg`、`conftest.py` 和 `test_*.py` 副本。

## 提交插件

打开官方插件市场的 **Add Plugin / 添加插件** Issue 模板，填写清单中的插件 ID 和实际仓库地址。首次提交需 CI 验证并由维护者批准；失败后按 Issue 提示修复，再单独评论 `/recheck`。

后续版本保留插件 ID，更新三段式版本号并发布对应 Release。同一版本被收录后，不得改写其 Tag 或提交；修复应发布新版本号。

## 官方依据

- [提交与发布插件](https://docs.maibot.chat/develop/plugin/submission)
- [插件市场贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)
- [Add Plugin 提交入口](https://github.com/Mai-with-u/plugin-repo/issues/new/choose)
