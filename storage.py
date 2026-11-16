"""原子文件写入、跨 Runner 进程互斥和持久化调度状态。"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class BusyError(Exception):
    """另一 Runner 或 CLI 正在访问同一账号。"""


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DataLease:
    """系统级非阻塞文件锁，进程退出后由系统释放，覆盖插件热重载窗口。"""

    def __init__(self, root: Path):
        self.path = root / "operation.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise BusyError("账号正被另一个插件实例或登录工具使用，请稍后重试。") from exc
        self.handle = handle
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


@dataclass
class ScheduleState:
    cycle: str = ""
    attempts: int = 0
    last_attempt: float = 0
    success_until: float = 0

    @classmethod
    def load(cls, root: Path) -> "ScheduleState":
        path = root / "schedule.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            state = cls(**{key: data[key] for key in asdict(cls()) if key in data})
            if not isinstance(state.cycle, str) or not isinstance(state.attempts, int) or state.attempts < 0:
                raise ValueError
            import math

            if any(
                not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
                for value in (state.last_attempt, state.success_until)
            ):
                raise ValueError
            return state
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("schedule.json 格式损坏，请备份后删除该文件并重载插件。") from exc

    def save(self, root: Path) -> None:
        write_json(root / "schedule.json", asdict(self))
