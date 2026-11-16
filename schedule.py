"""按账户重置周期计算调度，不依赖系统本地日期。"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import ScheduleSection
from .storage import ScheduleState


def cycle_bounds(now: datetime, settings: ScheduleSection) -> tuple[datetime, datetime, datetime]:
    local = now.astimezone(ZoneInfo(settings.timezone))
    hour, minute = map(int, settings.reset_time.split(":"))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=1)
    end = start + timedelta(days=1)
    hour, minute = map(int, settings.claim_time.split(":"))
    target = start.replace(hour=hour, minute=minute)
    if target < start:
        target += timedelta(days=1)
    return start, end, target


def is_due(now: datetime, settings: ScheduleSection, state: ScheduleState) -> bool:
    start, _, target = cycle_bounds(now, settings)
    current_cycle = start.isoformat()
    attempts = state.attempts if state.cycle == current_cycle else 0
    last_attempt = state.last_attempt if state.cycle == current_cycle else 0
    return (
        now >= target
        and now.timestamp() >= state.success_until
        and attempts < settings.max_attempts
        and (attempts == 0 or now.timestamp() - last_attempt >= settings.retry_interval_seconds)
    )


def next_reset(now: datetime, settings: ScheduleSection, upstream: str | None) -> float:
    if upstream:
        try:
            parsed = datetime.fromisoformat(upstream.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed > now:
                return parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return cycle_bounds(now, settings)[1].astimezone(timezone.utc).timestamp()
