from __future__ import annotations

import calendar
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo("Europe/Athens")
DEFAULT_TIME = time(18, 0)
MAX_AHEAD = timedelta(days=730)

HELP = (
    "I couldn't read that date. Try `in 3h`, `tomorrow 18:00`, `fri`, `2026-11-20 14:00` or `20/11 14:00`."
)

_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_WEEKDAYS = {
    name: index
    for index, names in enumerate(
        (("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"), ("thu", "thur", "thurs", "thursday"),
         ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday"))
    )
    for name in names
}
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_RELATIVE = re.compile(r"in (\d{1,4})([mhdw])")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?")


def _local(day, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=ATHENS).astimezone(timezone.utc)


def parse_when(text: str, now: datetime) -> datetime:
    """A UTC datetime for text like "in 3h", "tomorrow 09:30", "fri" or "20/11 14:00" (Athens time).

    Raises ValueError with a message fit to show the user."""
    words = " ".join(text.lower().split())
    if not words:
        raise ValueError(HELP)

    relative = _RELATIVE.fullmatch(words)
    if relative:
        result = now + timedelta(**{_UNITS[relative.group(2)]: int(relative.group(1))})
        return _check(result, now)

    at = DEFAULT_TIME
    time_match = re.search(r"(?:^| )(\d{1,2}:\d{2})$", words)
    if time_match:
        hour, minute = map(int, _TIME.fullmatch(time_match.group(1)).groups())
        if hour > 23 or minute > 59:
            raise ValueError(HELP)
        at = time(hour, minute)
        words = words[: time_match.start()].strip()

    today = now.astimezone(ATHENS).date()
    if words in ("", "today"):
        if not time_match:
            raise ValueError(HELP)
        day = today
    elif words == "tomorrow":
        day = today + timedelta(days=1)
    elif words in _WEEKDAYS:
        ahead = (_WEEKDAYS[words] - today.weekday()) % 7 or 7
        day = today + timedelta(days=ahead)
    elif (m := _ISO.fullmatch(words)):
        day = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif (m := _DMY.fullmatch(words)):
        day = _date(int(m.group(3) or today.year), int(m.group(2)), int(m.group(1)))
    else:
        raise ValueError(HELP)
    return _check(_local(day, at), now)


def _date(year: int, month: int, day: int):
    try:
        return datetime(year, month, day).date()
    except ValueError:
        raise ValueError(HELP) from None


def _check(result: datetime, now: datetime) -> datetime:
    if result <= now:
        raise ValueError("That time is already in the past.")
    if result - now > MAX_AHEAD:
        raise ValueError("That is more than two years away. Pick a nearer date.")
    return result


def add_interval(due: datetime, repeat: str) -> datetime:
    """`due` moved one day/week/month on, keeping the Athens clock time (month ends are clamped)."""
    local = due.astimezone(ATHENS)
    if repeat == "daily":
        day = local.date() + timedelta(days=1)
    elif repeat == "weekly":
        day = local.date() + timedelta(weeks=1)
    elif repeat == "monthly":
        year, month0 = divmod(local.year * 12 + local.month, 12)  # local.month is 1-based: this is next month
        month = month0 + 1
        day = local.date().replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1]))
    else:
        raise ValueError(f"Unknown repeat interval: {repeat}")
    return _local(day, local.time().replace(tzinfo=None))


def next_due(due: datetime, repeat: str, now: datetime) -> datetime:
    """The first repeat of `due` that is strictly after `now`."""
    result = add_interval(due, repeat)
    while result <= now:
        result = add_interval(result, repeat)
    return result


def tomorrow_morning(now: datetime) -> datetime:
    """09:00 Athens time on the next calendar day, as UTC."""
    return _local(now.astimezone(ATHENS).date() + timedelta(days=1), time(9, 0))
