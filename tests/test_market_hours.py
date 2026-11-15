from datetime import date, datetime, timezone

from app.market_hours import is_market_open, market_state, us_holidays

U = timezone.utc


def dt(*a):
    return datetime(*a, tzinfo=U)


def test_nasdaq_dst_shift():
    # Winter: 09:30 ET = 14:30 UTC
    assert not is_market_open("NASDAQ", dt(2026, 1, 13, 14, 29))
    assert is_market_open("NASDAQ", dt(2026, 1, 13, 14, 30))
    # Summer (EDT): 09:30 ET = 13:30 UTC
    assert is_market_open("NASDAQ", dt(2026, 7, 14, 13, 30))
    assert not is_market_open("NASDAQ", dt(2026, 7, 14, 20, 0))  # 16:00 EDT


def test_dst_transition_week():
    # US DST starts 2026-03-08; Mon 2026-03-09 opens at 13:30 UTC
    assert is_market_open("NASDAQ", dt(2026, 3, 9, 13, 30))
    # Friday before: still EST, 13:30 UTC = 08:30 ET -> closed
    assert not is_market_open("NASDAQ", dt(2026, 3, 6, 13, 30))


def test_weekend():
    assert not is_market_open("NASDAQ", dt(2026, 11, 14, 16, 0))  # Saturday
    assert not is_market_open("GOLD", dt(2026, 11, 14, 16, 0))
    # Gold reopens Sunday 18:00 ET = 23:00 UTC (EST)
    assert not is_market_open("GOLD", dt(2026, 11, 15, 22, 59))
    assert is_market_open("GOLD", dt(2026, 11, 15, 23, 0))


def test_gold_daily_break():
    # Tue 17:30 ET (EST) = 22:30 UTC
    assert not is_market_open("GOLD", dt(2026, 11, 17, 22, 30))
    assert is_market_open("GOLD", dt(2026, 11, 17, 23, 5))


def test_holidays():
    h = us_holidays(2026)
    assert h[date(2026, 4, 3)] == "Good Friday"
    assert h[date(2026, 7, 3)] == "Independence Day"  # July 4 is a Saturday -> observed Fri
    assert h[date(2026, 11, 26)] == "Thanksgiving"
    assert not is_market_open("NASDAQ", dt(2026, 11, 26, 16, 0))
    assert not is_market_open("GOLD", dt(2026, 12, 25, 15, 0))
    # Thanksgiving: gold trades in the morning, halts at 13:30 ET
    assert is_market_open("GOLD", dt(2026, 11, 26, 15, 0))
    assert not is_market_open("GOLD", dt(2026, 11, 26, 19, 0))
    # New Year 2022 fell on Saturday: NYSE did not close on Fri Dec 31 2021
    assert date(2021, 12, 31) not in us_holidays(2021)


def test_early_close():
    # Day after Thanksgiving 2026 (Fri Nov 27): Nasdaq closes 13:00 ET = 18:00 UTC
    assert is_market_open("NASDAQ", dt(2026, 11, 27, 17, 59))
    assert not is_market_open("NASDAQ", dt(2026, 11, 27, 18, 0))


def test_next_change():
    st = market_state("NASDAQ", dt(2026, 11, 14, 12, 0))  # Saturday
    assert not st.is_open
    assert st.next_change == dt(2026, 11, 16, 14, 30)
