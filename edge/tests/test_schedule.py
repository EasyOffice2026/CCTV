from datetime import datetime, time
from zoneinfo import ZoneInfo

from cctv_contracts.v1 import WorkingHours
from cctv_edge.schedule import BranchSchedule

# 2026-11-02 is a Monday (weekday 0). Kuwait is UTC+3 all year.


def kuwait(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Asia/Kuwait"))


def test_same_day_hours():
    schedule = BranchSchedule(
        [WorkingHours(weekday=0, opens_at=time(9), closes_at=time(17))], "Asia/Kuwait"
    )
    assert schedule.is_open(kuwait(2, 9))
    assert schedule.is_open(kuwait(2, 16, 59))
    assert not schedule.is_open(kuwait(2, 17))
    assert not schedule.is_open(kuwait(2, 8, 59))
    assert not schedule.is_open(kuwait(3, 12))  # Tuesday


def test_hours_past_midnight():
    # Monday 18:00 until Tuesday 02:00.
    schedule = BranchSchedule(
        [WorkingHours(weekday=0, opens_at=time(18), closes_at=time(2))], "Asia/Kuwait"
    )
    assert schedule.is_open(kuwait(2, 23))
    assert schedule.is_open(kuwait(3, 1, 30))
    assert not schedule.is_open(kuwait(3, 2))
    assert not schedule.is_open(kuwait(2, 12))


def test_no_hours_means_always_open():
    assert BranchSchedule([], "Asia/Kuwait").is_open(kuwait(2, 3))
