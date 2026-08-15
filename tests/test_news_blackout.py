"""The news blackout: reading the calendar, and standing aside because of it.

The decision is a pure function over a list of events and a clock, so almost all of
this runs against fixed timestamps rather than against whatever CPI happens to be
scheduled the week the suite is run.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.broker import PaperBroker
from src.config import AppSettings
from src.core.models import Timeframe
from src.data.calendar import (
    CalendarUnavailable,
    EconomicCalendar,
    Event,
    _parse,
    blackout_reason,
)
from src.db import connect
from src.engine import BotEngine
from test_engine import BTC, LONG_SIGNAL, FakeInfo, StubStrategy, make_candles

NOON = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    """A clock `minutes` away from the release."""
    return NOON + timedelta(minutes=minutes)


CPI = [Event(title="CPI m/m", when=NOON)]


# --- the decision ---------------------------------------------------------


@pytest.mark.parametrize(
    "minutes, blocked",
    [
        (-31, False),  # just outside the leading edge
        (-30, True),   # exactly on it
        (-5, True),
        (0, True),     # the release itself
        (10, True),
        (15, True),    # exactly on the trailing edge
        (16, False),   # just past it
    ],
)
def test_the_window_runs_from_before_the_release_to_after_it(minutes, blocked):
    reason = blackout_reason(CPI, at(minutes), before_minutes=30, after_minutes=15)
    assert (reason is not None) == blocked


def test_the_reason_names_the_event_and_how_far_off_it_is():
    assert "CPI m/m" in blackout_reason(CPI, at(-12), 30, 15)
    assert "in 12 min" in blackout_reason(CPI, at(-12), 30, 15)
    assert "8 min ago" in blackout_reason(CPI, at(8), 30, 15)


def test_an_empty_calendar_blocks_nothing():
    """No events is a real answer, and it means carry on."""
    assert blackout_reason([], at(0), 30, 15) is None


def test_a_zero_window_still_blocks_the_release_itself():
    assert blackout_reason(CPI, NOON, 0, 0) is not None
    assert blackout_reason(CPI, at(1), 0, 0) is None


# --- reading the feed -----------------------------------------------------


def row(**overrides) -> dict:
    base = {
        "title": "CPI m/m",
        "country": "USD",
        "date": "2026-08-12T08:30:00-04:00",
        "impact": "High",
    }
    return {**base, **overrides}


def test_only_high_impact_us_events_are_tracked():
    """The feed carries every currency and every impact level; almost none of it
    moves BTC enough to stand aside for."""
    events = _parse([
        row(),
        row(title="Bank Lending y/y", country="JPY"),
        row(title="Retail Sales", impact="Low"),
        row(title="Something", impact="Medium"),
        row(title="Bank Holiday", impact="Holiday"),
    ])
    assert [event.title for event in events] == ["CPI m/m"]


def test_the_feed_timezone_is_honoured():
    """The feed publishes New York time. Read as UTC it would be four hours out -
    a blackout that opens after the release it was meant to precede."""
    event = _parse([row()])[0]
    assert event.when == datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)


def test_events_come_back_in_time_order():
    events = _parse([
        row(title="later", date="2026-08-13T08:30:00-04:00"),
        row(title="earlier", date="2026-08-12T08:30:00-04:00"),
    ])
    assert [event.title for event in events] == ["earlier", "later"]


@pytest.mark.parametrize("bad", [
    row(date="not a date"),
    row(date=None),
    row(date="2026-08-12T08:30:00"),  # naive: could be any zone, so unusable
    "not even a dict",
])
def test_one_unusable_row_does_not_take_the_calendar_down(bad):
    """Fail-closed means a malformed row would otherwise stop trading entirely."""
    events = _parse([bad, row(title="CPI m/m")])
    assert [event.title for event in events] == ["CPI m/m"]


def test_a_feed_that_is_not_a_list_is_refused():
    with pytest.raises(CalendarUnavailable):
        _parse({"error": "rate limited"})


# --- fetching and caching -------------------------------------------------


def calendar_over(handler, **kwargs) -> EconomicCalendar:
    transport = httpx.MockTransport(handler)
    return EconomicCalendar(client=httpx.AsyncClient(transport=transport), **kwargs)


async def test_the_feed_is_fetched_and_parsed():
    calendar = calendar_over(
        lambda request: httpx.Response(200, json=[row(), row(country="GBP")])
    )
    events = await calendar.events()
    assert [event.title for event in events] == ["CPI m/m"]
    await calendar.aclose()


async def test_the_feed_is_not_refetched_while_the_cache_is_warm():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=[row()])

    calendar = calendar_over(handler, cache_seconds=3600)
    await calendar.events()
    await calendar.events()
    await calendar.events()
    assert len(calls) == 1
    await calendar.aclose()


async def test_a_failed_refresh_falls_back_to_the_cached_copy():
    """An hour-stale calendar is still a good calendar. Halting on a blip would
    stop trading for a reason that has nothing to do with the market."""
    responses = [httpx.Response(200, json=[row()]), httpx.Response(503)]
    calendar = calendar_over(lambda request: responses.pop(0), cache_seconds=0)

    first = await calendar.events()
    second = await calendar.events()  # the 503
    assert [event.title for event in second] == [event.title for event in first]
    await calendar.aclose()


async def test_a_failure_with_nothing_cached_raises():
    """Not knowing is not the same as knowing there is nothing."""
    calendar = calendar_over(lambda request: httpx.Response(503))
    with pytest.raises(CalendarUnavailable):
        await calendar.events()
    await calendar.aclose()


async def test_unreadable_json_raises_rather_than_reading_as_empty():
    calendar = calendar_over(
        lambda request: httpx.Response(200, content=b"<html>rate limited</html>")
    )
    with pytest.raises(CalendarUnavailable):
        await calendar.events()
    await calendar.aclose()


async def test_a_copy_older_than_a_day_is_refused_rather_than_trusted():
    """The feed covers *this week*. A copy from last week may hold no upcoming
    events at all, which reads identically to "nothing scheduled" — and would let
    the bot trade straight through CPI."""
    responses = [httpx.Response(200, json=[row()]), httpx.Response(503)]
    calendar = calendar_over(
        lambda request: responses.pop(0), cache_seconds=0, max_stale_seconds=0
    )

    await calendar.events()
    with pytest.raises(CalendarUnavailable):
        await calendar.events()
    await calendar.aclose()


# --- surviving a restart --------------------------------------------------


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


async def test_the_calendar_survives_a_restart(conn):
    """Regression: the feed rate-limits (429, Retry-After ~67s) and the blackout
    fails closed, so a cold start that gets limited stood the bot down until the
    next candle close - four hours on the default timeframe."""
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=[row()])

    first = calendar_over(handler, conn=conn)
    assert [event.title for event in await first.events()] == ["CPI m/m"]
    await first.aclose()

    # A new process: same database, and the feed now refuses everything.
    second = calendar_over(lambda request: httpx.Response(429), conn=conn)
    events = await second.events()
    await second.aclose()

    assert [event.title for event in events] == ["CPI m/m"]
    assert len(calls) == 1, "the second start should not have needed the network"


async def test_a_restart_past_the_staleness_limit_still_fails_closed(conn):
    calendar = calendar_over(lambda request: httpx.Response(200, json=[row()]), conn=conn)
    await calendar.events()
    await calendar.aclose()

    conn.execute("UPDATE calendar_cache SET fetched_ms = 0")  # 1970
    conn.commit()

    stale = calendar_over(lambda request: httpx.Response(429), conn=conn)
    with pytest.raises(CalendarUnavailable):
        await stale.events()
    await stale.aclose()


async def test_a_corrupt_cache_row_is_ignored_not_fatal(conn):
    conn.execute(
        "INSERT INTO calendar_cache (id, fetched_ms, payload) VALUES (1, ?, ?)",
        (999_999_999_999, "{not json"),
    )
    conn.commit()

    calendar = calendar_over(lambda request: httpx.Response(200, json=[row()]), conn=conn)
    assert [event.title for event in await calendar.events()] == ["CPI m/m"]
    await calendar.aclose()


async def test_the_cache_is_written_on_a_successful_fetch(conn):
    calendar = calendar_over(lambda request: httpx.Response(200, json=[row()]), conn=conn)
    await calendar.events()
    await calendar.aclose()

    stored = conn.execute("SELECT fetched_ms, payload FROM calendar_cache").fetchone()
    assert stored is not None
    assert "CPI m/m" in stored["payload"]


async def test_no_database_still_works(conn):
    """The console runner and the tests build calendars without one."""
    calendar = calendar_over(lambda request: httpx.Response(200, json=[row()]))
    assert len(await calendar.events()) == 1
    await calendar.aclose()


# --- the engine standing aside --------------------------------------------


class FakeCalendar:
    def __init__(self, reason: str | None = None, *, unavailable: bool = False):
        self._reason = reason
        self._unavailable = unavailable
        self.calls = 0

    async def reason_not_to_trade(self, before_minutes, after_minutes) -> str | None:
        self.calls += 1
        if self._unavailable:
            raise CalendarUnavailable("feed is down")
        return self._reason


def build(conn, calendar, **overrides):
    settings = AppSettings(
        timeframe=Timeframe.H1,
        risk_usdc=50.0,
        leverage=5,
        paper_starting_balance=1_000.0,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)

    broker = PaperBroker("BTC", BTC, 1_000.0, slippage=0.0, fee_bps=0.0, conn=conn)
    engine = BotEngine(
        settings=settings,
        info=FakeInfo(make_candles(5)),
        broker=broker,
        strategy=StubStrategy(LONG_SIGNAL),
        asset=BTC,
        conn=conn,
        calendar=calendar,
        throttle_requests=False,
    )
    return engine, broker


async def test_no_entry_inside_the_blackout(conn):
    calendar = FakeCalendar("news blackout: CPI m/m in 12 min")
    engine, broker = build(conn, calendar)
    await engine.prepare()

    await engine._on_candle_close()

    assert await broker.managed_position() is None
    assert engine.strategy.calls == 0  # not even consulted


async def test_an_entry_is_taken_outside_the_blackout(conn):
    engine, broker = build(conn, FakeCalendar(None))
    await engine.prepare()

    await engine._on_candle_close()

    assert await broker.managed_position() is not None


async def test_an_unreadable_calendar_stands_aside(conn, caplog):
    """Fail closed: not knowing whether CPI is five minutes away is not a reason
    to assume it is not."""
    engine, broker = build(conn, FakeCalendar(unavailable=True))
    await engine.prepare()

    await engine._on_candle_close()

    assert await broker.managed_position() is None
    assert "unavailable" in (await engine._reason_not_to_trade())


async def test_the_calendar_is_not_consulted_when_the_setting_is_off(conn):
    calendar = FakeCalendar("would have blocked")
    engine, broker = build(conn, calendar, news_blackout_enabled=False)
    await engine.prepare()

    await engine._on_candle_close()

    assert calendar.calls == 0
    assert await broker.managed_position() is not None


async def test_no_calendar_wired_means_no_blackout(conn):
    """The console runner and most tests pass none; that must not fail closed."""
    engine, broker = build(conn, None)
    await engine.prepare()

    await engine._on_candle_close()

    assert await broker.managed_position() is not None


async def test_the_configured_window_reaches_the_calendar(conn):
    seen = {}

    class Recording(FakeCalendar):
        async def reason_not_to_trade(self, before_minutes, after_minutes):
            seen["before"] = before_minutes
            seen["after"] = after_minutes
            return None

    engine, _ = build(
        conn, Recording(), news_blackout_before_min=45, news_blackout_after_min=5
    )
    await engine.prepare()
    await engine._on_candle_close()

    assert seen == {"before": 45, "after": 5}


async def test_a_resting_entry_is_pulled_when_a_release_comes_into_range(conn):
    """Blocking new entries is not enough once orders rest: one placed in a quiet
    hour is still on the book when CPI lands, and a limit order into a release is
    filled by exactly the sweep the blackout exists to avoid."""
    engine, broker = build(conn, FakeCalendar(None), post_only_entry=True)
    await engine.prepare()
    await engine._on_candle_close()
    assert broker.pending_entry() is not None

    engine.calendar = FakeCalendar("news blackout: CPI m/m in 12 min")
    await engine._pull_entry_for_news()

    assert broker.pending_entry() is None


async def test_a_resting_entry_survives_a_clear_calendar(conn):
    engine, broker = build(conn, FakeCalendar(None), post_only_entry=True)
    await engine.prepare()
    await engine._on_candle_close()

    await engine._pull_entry_for_news()

    assert broker.pending_entry() is not None


async def test_an_unreadable_calendar_also_pulls_the_entry(conn):
    """Fail closed here too: not knowing whether CPI is minutes away is not a
    reason to leave an order sitting where a sweep would fill it."""
    engine, broker = build(conn, FakeCalendar(None), post_only_entry=True)
    await engine.prepare()
    await engine._on_candle_close()

    engine.calendar = FakeCalendar(unavailable=True)
    await engine._pull_entry_for_news()

    assert broker.pending_entry() is None


async def test_the_calendar_is_not_consulted_with_nothing_resting(conn):
    """A lookup per poll with no order to cancel is noise for no benefit."""
    calendar = FakeCalendar(None)
    engine, _ = build(conn, calendar)
    await engine.prepare()

    await engine._pull_entry_for_news()

    assert calendar.calls == 0


def test_the_blackout_window_is_symmetric_by_default():
    """The spec this app implements was backtested against 30 minutes either side."""
    settings = AppSettings()
    assert settings.news_blackout_before_min == 30
    assert settings.news_blackout_after_min == 30


async def test_an_open_position_is_left_alone_through_a_release(conn):
    """Standing aside holds back new entries. Closing a live position on news would
    realise a loss the stop might never have taken."""
    engine, broker = build(conn, FakeCalendar(None))
    await engine.prepare()
    await engine._on_candle_close()
    assert await broker.managed_position() is not None

    engine.calendar = FakeCalendar("news blackout: FOMC in 3 min")
    await engine._on_candle_close()

    assert await broker.managed_position() is not None
