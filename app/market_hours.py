"""Trading calendars for the supported instruments.

All session rules are expressed in exchange local time (America/New_York) and converted
with ``zoneinfo``, so US daylight-saving transitions (March / November) are handled
automatically — a 09:30 ET open is 14:30 UTC in winter and 13:30 UTC in summer.

GOLD   – spot gold / COMEX gold on CME Globex:
         Sunday 18:00 ET -> Friday 17:00 ET, with a daily maintenance break 17:00-18:00 ET.
         Closed on New Year's Day, Good Friday and Christmas Day; early halt at 13:30 ET on
         other US federal-style holidays (MLK, Presidents, Memorial, Juneteenth, July 4,
         Labor, Thanksgiving).
NASDAQ – Nasdaq-100 index cash session:
         Monday-Friday 09:30-16:00 ET on exchange trading days; closed on NYSE/Nasdaq
         holidays; 13:00 ET early close on the day after Thanksgiving, Christmas Eve and
         July 3 (when they are regular trading days).

Holiday dates are computed algorithmically (including Easter for Good Friday and the
weekend-observance rules), so no annual data update is required. Exchanges occasionally
add ad-hoc closures (e.g. national days of mourning) – add those to ``EXTRA_CLOSURES``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

INSTRUMENTS = ("GOLD", "NASDAQ")

# Ad-hoc full-day closures: {instrument: {date, ...}}
EXTRA_CLOSURES: dict[str, set[date]] = {"GOLD": set(), "NASDAQ": set()}

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def timeframe_seconds(tf: str) -> int:
    try:
        return TIMEFRAMES[tf]
    except KeyError as e:
        raise ValueError(f"unsupported timeframe {tf!r}; use one of {sorted(TIMEFRAMES)}") from e


# --------------------------------------------------------------------------- holidays
def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def us_holidays(year: int) -> dict[date, str]:
    """NYSE/Nasdaq full-day holidays for ``year``."""
    h: dict[date, str] = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:  # NYSE does not observe on the preceding Friday (Dec 31)
        h[_observed(ny)] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    h[_observed(date(year, 12, 25))] = "Christmas Day"
    # New Year's of next year falling on Saturday is not observed (handled above)
    return h


def _is_us_holiday(d: date) -> str | None:
    return us_holidays(d.year).get(d)


@lru_cache(maxsize=64)
def nasdaq_early_closes(year: int) -> set[date]:
    out: set[date] = set()
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    out.add(thanksgiving + timedelta(days=1))
    for d in (date(year, 12, 24), date(year, 7, 3)):
        if d.weekday() < 5 and d not in us_holidays(year):
            out.add(d)
    return {d for d in out if d.weekday() < 5 and d not in us_holidays(year)}


GOLD_FULL_CLOSURES = {"New Year's Day", "Good Friday", "Christmas Day"}


# --------------------------------------------------------------------------- sessions
def _is_open_local(instrument: str, local: datetime) -> tuple[bool, str]:
    d = local.date()
    t = local.time()
    wd = local.weekday()  # Mon=0 .. Sun=6
    if d in EXTRA_CLOSURES.get(instrument, set()):
        return False, "exchange closure"

    if instrument == "NASDAQ":
        if wd >= 5:
            return False, "weekend"
        hol = _is_us_holiday(d)
        if hol:
            return False, f"holiday: {hol}"
        close = time(13, 0) if d in nasdaq_early_closes(d.year) else time(16, 0)
        if t < time(9, 30):
            return False, "pre-market"
        if t >= close:
            return False, "after hours" if close == time(16, 0) else "early close"
        return True, "regular session"

    if instrument == "GOLD":
        # Weekend: Fri 17:00 -> Sun 18:00
        if wd == 5 or (wd == 4 and t >= time(17, 0)) or (wd == 6 and t < time(18, 0)):
            return False, "weekend"
        # Daily maintenance break
        if time(17, 0) <= t < time(18, 0):
            return False, "daily maintenance break"
        # Holidays. A Globex "trade date" that is a holiday affects the calendar day itself.
        hol = _is_us_holiday(d)
        if hol in GOLD_FULL_CLOSURES:
            # closed the whole calendar day; reopens 18:00 ET that evening
            if t < time(18, 0):
                return False, f"holiday: {hol}"
        elif hol:
            if time(13, 30) <= t < time(18, 0):
                return False, f"holiday early halt: {hol}"
        return True, "globex session"

    raise ValueError(f"unknown instrument {instrument!r}")


def is_market_open(instrument: str, at: datetime | None = None) -> bool:
    return market_state(instrument, at).is_open


@dataclass(frozen=True)
class MarketState:
    instrument: str
    is_open: bool
    reason: str
    as_of: datetime
    next_change: datetime | None  # next open if closed, next close if open (UTC)

    def as_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "is_open": self.is_open,
            "reason": self.reason,
            "as_of": self.as_of.isoformat(),
            "next_change": self.next_change.isoformat() if self.next_change else None,
        }


def _to_utc(at: datetime | None) -> datetime:
    if at is None:
        return datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; pass an aware UTC datetime")
    return at.astimezone(UTC)


def market_state(instrument: str, at: datetime | None = None) -> MarketState:
    at = _to_utc(at)
    is_open, reason = _is_open_local(instrument, at.astimezone(ET))
    return MarketState(instrument, is_open, reason, at, _next_change(instrument, at, is_open))


def _next_change(instrument: str, at: datetime, currently_open: bool) -> datetime | None:
    # All session boundaries fall on :00 or :30 ET, so step on half-hour marks.
    local = at.astimezone(ET)
    step_start = local.replace(second=0, microsecond=0)
    step_start += timedelta(minutes=(30 - step_start.minute % 30) % 30 or 0)
    if step_start <= local:
        step_start += timedelta(minutes=30)
    # Walk in UTC (so DST gaps/overlaps never cause skipped/duplicate steps)
    cur = step_start.astimezone(UTC)
    for _ in range(24 * 2 * 10):  # 10 days max
        if _is_open_local(instrument, cur.astimezone(ET))[0] != currently_open:
            return cur
        cur += timedelta(minutes=30)
    return None


def candle_close_time(ts_open: datetime, timeframe: str) -> datetime:
    return ts_open + timedelta(seconds=timeframe_seconds(timeframe))


def floor_to_timeframe(at: datetime, timeframe: str) -> datetime:
    secs = timeframe_seconds(timeframe)
    epoch = int(at.timestamp())
    return datetime.fromtimestamp(epoch - epoch % secs, tz=UTC)
