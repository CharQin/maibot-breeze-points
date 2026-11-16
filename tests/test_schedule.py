from datetime import datetime, timezone

import pytest
from _breeze_test_plugin.config import ScheduleSection
from _breeze_test_plugin.schedule import cycle_bounds, is_due, next_reset
from _breeze_test_plugin.storage import BusyError, DataLease, ScheduleState


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_shanghai_schedule_independent_of_system_timezone():
    settings = ScheduleSection()
    state = ScheduleState()
    assert not is_due(utc("2026-11-16T00:09:00"), settings, state)
    assert is_due(utc("2026-11-16T00:10:00"), settings, state)
    assert is_due(utc("2026-11-16T12:00:00"), settings, state)


def test_before_reset_still_belongs_to_previous_cycle():
    start, end, _ = cycle_bounds(utc("2026-11-15T23:59:00"), ScheduleSection())
    assert start.isoformat() == "2026-11-15T08:00:00+08:00"
    assert end.isoformat() == "2026-11-16T08:00:00+08:00"


def test_retry_limit_is_persistent_and_resets_next_cycle(tmp_path):
    now = utc("2026-11-16T00:10:00")
    settings = ScheduleSection()
    cycle = cycle_bounds(now, settings)[0].isoformat()
    state = ScheduleState(cycle=cycle, attempts=3, last_attempt=now.timestamp())
    state.save(tmp_path)
    loaded = ScheduleState.load(tmp_path)
    assert not is_due(utc("2026-11-16T02:10:00"), settings, loaded)
    assert is_due(utc("2026-11-17T00:10:00"), settings, loaded)


def test_retry_interval():
    now = utc("2026-11-16T00:10:00")
    settings = ScheduleSection()
    state = ScheduleState(cycle=cycle_bounds(now, settings)[0].isoformat(), attempts=1, last_attempt=now.timestamp())
    assert not is_due(utc("2026-11-16T00:24:59"), settings, state)
    assert is_due(utc("2026-11-16T00:25:00"), settings, state)


def test_server_reset_is_preferred_and_fallback_is_period_end():
    settings = ScheduleSection()
    now = utc("2026-11-16T00:10:00")
    server = "2026-11-17T01:00:00Z"
    until = next_reset(now, settings, server)
    assert until == utc("2026-11-17T01:00:00").timestamp()
    assert not is_due(utc("2026-11-17T00:10:00"), settings, ScheduleState(success_until=until))
    for invalid in (None, "bad", "2026-11-17T01:00:00", "2026-11-15T00:00:00Z"):
        assert next_reset(now, settings, invalid) == utc("2026-11-17T00:00:00").timestamp()


def test_claim_time_before_reset_runs_at_end_of_cycle():
    settings = ScheduleSection(claim_time="07:00")
    state = ScheduleState()
    assert not is_due(utc("2026-11-16T22:59:00"), settings, state)
    assert is_due(utc("2026-11-16T23:00:00"), settings, state)


def test_file_lock_prevents_second_instance_and_releases(tmp_path):
    with DataLease(tmp_path):
        with pytest.raises(BusyError):
            with DataLease(tmp_path):
                pytest.fail("同一账号不能同时获取锁")
    with DataLease(tmp_path):
        pass


@pytest.mark.parametrize("contents", ['{"attempts": -1}', '{"last_attempt": "bad"}', "[]", "bad"])
def test_corrupt_state_is_explicit(tmp_path, contents):
    (tmp_path / "schedule.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="schedule.json"):
        ScheduleState.load(tmp_path)
