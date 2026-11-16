"""本机登录及无桌面服务器 Cookie 导入工具。

在插件目录的上一级运行：python -m breeze_claim_plugin.cli --help
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .breeze import BreezeClient, CookieError, CookieStore
from .storage import BusyError, DataLease, ScheduleState


async def run(arguments: argparse.Namespace) -> int:
    root = arguments.data_dir.expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with DataLease(root):
            if arguments.operation == "import":
                source = arguments.source
                if source is None:
                    raise ValueError("import 操作必须提供 --source Cookie 文件路径。")
                if source.stat().st_size > 1024 * 1024:
                    raise ValueError("Cookie 文件超过 1 MiB。")
                CookieStore(root).save(json.loads(source.read_text("utf-8-sig")))
                # 允许更换账号后的下一轮重新查询；服务端已领取时不会再次 POST。
                state = ScheduleState.load(root)
                state.attempts = 0
                state.last_attempt = 0
                state.success_until = 0
                state.save(root)
                print(
                    json.dumps(
                        {"ok": True, "message": "Cookie 已导入，请在配置页刷新状态并验证登录。"}, ensure_ascii=False
                    )
                )
                return 0
            client = BreezeClient(
                root, browser_channel=arguments.channel, login_timeout=arguments.timeout,
                deployment_mode=arguments.deployment_mode,
            )
            try:
                result = await getattr(client, arguments.operation)()
                if result.ok and arguments.operation in {"login", "refresh"}:
                    state = ScheduleState.load(root)
                    state.attempts = 0
                    state.last_attempt = 0
                    state.success_until = 0
                    state.save(root)
                print(json.dumps(asdict(result), ensure_ascii=False, allow_nan=False))
                return 0 if result.ok else 1
            finally:
                await client.close()
    except (OSError, ValueError, CookieError, BusyError) as exc:
        # 不输出文件内容或 Cookie 值。
        message = (
            str(exc) if isinstance(exc, (CookieError, BusyError)) else "文件无法读取或格式无效，请检查参数和权限。"
        )
        print(json.dumps({"ok": False, "message": message}, ensure_ascii=False))
        return 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Breeze 登录和 Cookie 导入；data-dir 必须使用插件日志中的数据目录。",
    )
    parser.add_argument("operation", choices=["login", "refresh", "status", "import"])
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="MaiBot 为本插件分配的数据目录，或本机临时登录目录"
    )
    parser.add_argument("--channel", choices=["msedge", "chrome", "chromium"], default="msedge")
    parser.add_argument("--deployment-mode", choices=["auto", "desktop", "server"], default="auto")
    parser.add_argument("--timeout", type=int, default=300, choices=range(30, 601), metavar="30..600")
    parser.add_argument("--source", type=Path, help="import 操作的 Cookie JSON 来源文件")
    arguments = parser.parse_args()
    try:
        return asyncio.run(run(arguments))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
