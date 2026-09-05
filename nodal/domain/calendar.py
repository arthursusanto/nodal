"""Facility operating calendars (§2, §6).

Hours are defined in the facility's IANA timezone and evaluated against UTC instants.
A facility with no calendar is open 24/7. DST edge rule (§2): nonexistent local times
resolve forward, ambiguous ones to the first occurrence — both fall out of evaluating
with PEP 495 `fold=0`.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

MINUTES_PER_DAY = 24 * 60


class DayWindow(BaseModel):
    """An open interval within one local day, in minutes from local midnight."""

    model_config = ConfigDict(frozen=True)

    start_minute: int = Field(ge=0, lt=MINUTES_PER_DAY)
    end_minute: int = Field(gt=0, le=MINUTES_PER_DAY)

    @model_validator(mode="after")
    def _ordered(self) -> "DayWindow":
        if self.end_minute <= self.start_minute:
            raise ValueError("end_minute must be after start_minute")
        return self


class OperatingCalendar(BaseModel):
    """Weekly windows keyed by local weekday (0=Monday), plus whole-day exceptions.

    An exception (keyed by local ISO date) replaces that day's windows entirely;
    an empty list means closed all day.
    """

    model_config = ConfigDict(frozen=True)

    week: dict[int, list[DayWindow]] = Field(default_factory=dict)
    exceptions: dict[str, list[DayWindow]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_weekdays(self) -> "OperatingCalendar":
        for day in self.week:
            if not 0 <= day <= 6:
                raise ValueError(f"weekday {day} out of range 0..6")
        for key in self.exceptions:
            date.fromisoformat(key)  # raises on malformed keys
        return self

    def windows_for(self, local_date: date) -> list[DayWindow]:
        key = local_date.isoformat()
        if key in self.exceptions:
            return self.exceptions[key]
        return self.week.get(local_date.weekday(), [])


def is_open(calendar: OperatingCalendar | None, tz: str, at_utc: datetime) -> bool:
    require_aware(at_utc)
    if calendar is None:
        return True
    local = at_utc.astimezone(ZoneInfo(tz))
    minute = local.hour * 60 + local.minute
    return any(w.start_minute <= minute < w.end_minute for w in calendar.windows_for(local.date()))


def require_aware(value: datetime) -> datetime:
    """§2: naive datetimes are never silently interpreted; they are errors."""
    if value.tzinfo is None:
        raise ValueError("naive datetime: all engine timestamps must be timezone-aware")
    return value


def next_open(
    calendar: OperatingCalendar | None,
    tz: str,
    at_utc: datetime,
    max_days: int = 60,
) -> datetime | None:
    """Earliest UTC instant >= at_utc at which the facility is open.

    Returns `at_utc` itself if already open, or None if no window exists within
    `max_days` local days (a facility closed that long is effectively unreachable).

    DST handling: each window start is evaluated under both PEP 495 folds and the
    earliest candidate >= at_utc that is actually open wins. A start swallowed by a
    spring-forward gap maps to the shifted (post-gap) instant, which the `is_open`
    check then validates; a window entirely inside the gap is correctly skipped.
    A fall-back repeated hour yields two real candidates and the earlier one wins.
    """
    require_aware(at_utc)
    if is_open(calendar, tz, at_utc):
        return at_utc
    assert calendar is not None  # None calendar is always open
    zone = ZoneInfo(tz)
    local = at_utc.astimezone(zone)
    for day_offset in range(max_days):
        day = local.date() + timedelta(days=day_offset)
        candidates: list[datetime] = []
        for window in calendar.windows_for(day):
            for fold in (0, 1):
                nominal = datetime(
                    day.year,
                    day.month,
                    day.day,
                    window.start_minute // 60,
                    window.start_minute % 60,
                    tzinfo=zone,
                    fold=fold,
                )
                candidate = max(nominal.astimezone(ZoneInfo("UTC")), at_utc)
                if is_open(calendar, tz, candidate):
                    candidates.append(candidate)
        if candidates:
            return min(candidates)
    return None
