from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from cctv_contracts.v1 import WorkingHours


class BranchSchedule:
    """Working hours in the branch's local time zone.

    A day whose `closes_at` is not after `opens_at` runs past midnight. With no working
    hours configured the branch counts as always open, so after-hours rules stay silent
    instead of alerting all day.
    """

    def __init__(self, hours: Sequence[WorkingHours], timezone: str) -> None:
        self.hours = list(hours)
        self.tz = ZoneInfo(timezone)

    def is_open(self, when: datetime) -> bool:
        if not self.hours:
            return True
        local = when.astimezone(self.tz)
        day, now = local.weekday(), local.time()
        for h in self.hours:
            if h.opens_at < h.closes_at:
                if day == h.weekday and h.opens_at <= now < h.closes_at:
                    return True
            elif (day == h.weekday and now >= h.opens_at) or (
                day == (h.weekday + 1) % 7 and now < h.closes_at
            ):
                return True
        return False
