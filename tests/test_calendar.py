from datetime import UTC, datetime

from nodal.domain.calendar import DayWindow, OperatingCalendar, is_open, next_open

BUSINESS = OperatingCalendar(
    week={d: [DayWindow(start_minute=8 * 60, end_minute=18 * 60)] for d in range(5)},
    exceptions={"2026-09-07": []},  # a Monday, closed
)


def test_no_calendar_is_always_open() -> None:
    ts = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)  # Sunday, 3am
    assert is_open(None, "America/Chicago", ts)
    assert next_open(None, "America/Chicago", ts) == ts


def test_open_within_local_window() -> None:
    # 2026-09-02 is a Wednesday. 15:00 UTC = 10:00 America/Chicago (CDT).
    assert is_open(BUSINESS, "America/Chicago", datetime(2026, 9, 2, 15, 0, tzinfo=UTC))
    # 12:00 UTC = 07:00 local: before opening.
    assert not is_open(BUSINESS, "America/Chicago", datetime(2026, 9, 2, 12, 0, tzinfo=UTC))


def test_next_open_same_day() -> None:
    early = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)  # 07:00 local
    opens = next_open(BUSINESS, "America/Chicago", early)
    assert opens == datetime(2026, 9, 2, 13, 0, tzinfo=UTC)  # 08:00 CDT


def test_next_open_skips_weekend_and_exception() -> None:
    # Friday 2026-09-04 22:00 local -> Saturday/Sunday closed, Monday 09-07 is an
    # exception (closed), so next open is Tuesday 09-08 08:00 local.
    late_friday = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)  # Fri 22:00 America/Chicago
    opens = next_open(BUSINESS, "America/Chicago", late_friday)
    assert opens == datetime(2026, 9, 8, 13, 0, tzinfo=UTC)


def test_dst_gap_resolves_forward() -> None:
    # US spring-forward 2026-03-08: local 02:00-03:00 does not exist in
    # America/Chicago. A 02:00-04:00 window's nominal start resolves forward.
    gap_cal = OperatingCalendar(week={6: [DayWindow(start_minute=120, end_minute=240)]})
    before = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)  # 01:00 CST
    opens = next_open(gap_cal, "America/Chicago", before)
    assert opens is not None
    assert opens >= before
    assert is_open(gap_cal, "America/Chicago", opens)


def test_next_open_none_when_never_open() -> None:
    closed = OperatingCalendar(week={})
    assert next_open(closed, "UTC", datetime(2026, 9, 2, tzinfo=UTC)) is None


def test_dst_fallback_repeated_hour_uses_second_pass() -> None:
    # US fall-back 2026-11-01 (Sunday): 01:00-02:00 local occurs twice in
    # America/Chicago. Window 01:45-03:00; query at 01:30 CST (the second pass,
    # 07:30Z). The true next open is 01:45 CST = 07:45Z — not an hour later.
    cal = OperatingCalendar(week={6: [DayWindow(start_minute=105, end_minute=180)]})
    opens = next_open(cal, "America/Chicago", datetime(2026, 11, 1, 7, 30, tzinfo=UTC))
    assert opens == datetime(2026, 11, 1, 7, 45, tzinfo=UTC)


def test_dst_fallback_narrow_window_not_skipped() -> None:
    # Narrow window 01:45-02:00 inside the repeated hour: it must be found on the
    # second pass, not skipped to next week.
    cal = OperatingCalendar(week={6: [DayWindow(start_minute=105, end_minute=120)]})
    opens = next_open(cal, "America/Chicago", datetime(2026, 11, 1, 7, 30, tzinfo=UTC))
    assert opens == datetime(2026, 11, 1, 7, 45, tzinfo=UTC)


def test_naive_datetime_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="timezone-aware"):
        is_open(BUSINESS, "America/Chicago", datetime(2026, 9, 2, 15, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        next_open(BUSINESS, "America/Chicago", datetime(2026, 9, 2, 15, 0))
