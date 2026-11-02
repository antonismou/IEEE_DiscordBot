from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.todo.dates import ATHENS, add_interval, next_due, parse_when

# Monday 2026-11-02 10:00 Athens (UTC+2 in winter)
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)


def local(dt):
    return dt.astimezone(ATHENS).replace(tzinfo=None)


def test_relative_forms():
    assert parse_when("in 30m", NOW) == NOW + timedelta(minutes=30)
    assert parse_when("in 3h", NOW) == NOW + timedelta(hours=3)
    assert parse_when("in 2d", NOW) == NOW + timedelta(days=2)
    assert parse_when("in 1w", NOW) == NOW + timedelta(weeks=1)


def test_tomorrow_defaults_to_18_00_and_accepts_a_time():
    assert local(parse_when("tomorrow", NOW)) == datetime(2026, 11, 3, 18, 0)
    assert local(parse_when("tomorrow 09:30", NOW)) == datetime(2026, 11, 3, 9, 30)


def test_today_with_a_time_still_ahead():
    assert local(parse_when("today 18:00", NOW)) == datetime(2026, 11, 2, 18, 0)


def test_weekday_is_strictly_after_today():
    assert local(parse_when("fri", NOW)) == datetime(2026, 11, 6, 18, 0)
    assert local(parse_when("mon", NOW)) == datetime(2026, 11, 9, 18, 0)
    assert local(parse_when("friday 09:30", NOW)) == datetime(2026, 11, 6, 9, 30)


def test_absolute_dates():
    assert local(parse_when("2026-11-20", NOW)) == datetime(2026, 11, 20, 18, 0)
    assert local(parse_when("2026-11-20 14:00", NOW)) == datetime(2026, 11, 20, 14, 0)
    assert local(parse_when("20/11", NOW)) == datetime(2026, 11, 20, 18, 0)
    assert local(parse_when("20/11/2026 14:00", NOW)) == datetime(2026, 11, 20, 14, 0)


def test_case_and_spacing_are_forgiven():
    assert local(parse_when("  TOMORROW   09:30 ", NOW)) == datetime(2026, 11, 3, 9, 30)


def test_result_is_utc_aware():
    assert parse_when("tomorrow", NOW).utcoffset() == timedelta(0)


def test_dst_keeps_the_local_clock_time():
    # 29 March 2026: Athens moves to UTC+3, so 18:00 local is 15:00 UTC
    assert parse_when("2026-03-30 18:00", datetime(2026, 3, 1, tzinfo=timezone.utc)) == datetime(
        2026, 3, 30, 15, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("text", ["", "banana", "in 5x", "31/02", "today 08:00", "2026-11-01", "20/10", "25:00 tomorrow"])
def test_bad_or_past_input_raises_value_error(text):
    with pytest.raises(ValueError):
        parse_when(text, NOW)


def test_more_than_two_years_ahead_is_rejected():
    with pytest.raises(ValueError):
        parse_when("2029-01-01", NOW)


def test_add_interval():
    jan31 = datetime(2026, 1, 31, 16, 0, tzinfo=timezone.utc)
    assert local(add_interval(jan31, "monthly")) == datetime(2026, 2, 28, 18, 0)
    dec31 = datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc)
    assert local(add_interval(dec31, "monthly")) == datetime(2027, 1, 31, 18, 0)
    assert local(add_interval(jan31, "daily")) == datetime(2026, 2, 1, 18, 0)
    assert local(add_interval(jan31, "weekly")) == datetime(2026, 2, 7, 18, 0)


def test_weekly_keeps_local_time_across_dst():
    before = datetime(2026, 3, 25, 16, 0, tzinfo=timezone.utc)  # 18:00 Athens (UTC+2)
    assert add_interval(before, "weekly") == datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)  # 18:00 (UTC+3)


def test_unknown_interval_raises():
    with pytest.raises(ValueError):
        add_interval(NOW, "yearly")


def test_next_due_skips_to_the_future():
    old = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    result = next_due(old, "weekly", NOW)
    assert result > NOW
    assert result - NOW <= timedelta(weeks=1)
