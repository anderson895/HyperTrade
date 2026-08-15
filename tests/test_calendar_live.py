"""Live checks against the real economic calendar feed.

Run with `pytest -m network`; excluded from the default run so the suite stays
offline-safe. The blackout fails closed, so the day this feed changes shape or
disappears is the day the bot quietly stops taking entries. That is the safe
direction to fail in, but it should not be discovered by wondering why nothing has
traded for a week.

The feed rate-limits: three fetches within a few seconds earned a 429. Everything
here therefore shares **one** download, which is also what the app does — the
calendar is cached for an hour, so a running bot fetches it 24 times a day at most.
"""

import httpx
import pytest

from src.data.calendar import (
    FEED_URL,
    TRACKED_COUNTRY,
    TRACKED_IMPACT,
    EconomicCalendar,
    _parse,
)

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
async def payload():
    """The raw feed, downloaded once for the whole module."""
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "HyperTrade"}) as client:
        response = await client.get(FEED_URL)
        if response.status_code == 429:
            pytest.skip("the calendar feed is rate-limiting this run")
        response.raise_for_status()
        return response.json()


def test_the_feed_is_a_list_of_events(payload):
    assert isinstance(payload, list)
    assert payload, "the feed came back empty"


def test_the_feed_still_carries_the_fields_the_filter_needs(payload):
    """The filter is `country == USD and impact == High`. If the feed renamed either
    value, `_parse` would silently return nothing and the blackout would never fire —
    which reads exactly like a quiet week."""
    assert {"title", "country", "date", "impact"} <= set(payload[0])

    impacts = {row.get("impact") for row in payload}
    countries = {row.get("country") for row in payload}
    assert TRACKED_IMPACT in impacts, f"feed no longer uses {TRACKED_IMPACT!r}: {impacts}"
    assert TRACKED_COUNTRY in countries, f"feed no longer uses {TRACKED_COUNTRY!r}: {countries}"


def test_the_real_feed_parses(payload):
    events = _parse(payload)
    for event in events:
        assert event.title
        assert event.when.tzinfo is not None, "a naive timestamp would be read as UTC"


def test_the_filter_keeps_a_handful_a_week_not_all_of_it(payload):
    """Sanity on the volume: a filter that let everything through would keep the bot
    permanently blacked out, and one that let nothing through would never fire."""
    kept = _parse(payload)
    assert len(kept) < 25, [event.title for event in kept]
    assert len(kept) < len(payload), "the filter is not excluding anything"


async def test_the_calendar_serves_the_cached_copy_rather_than_refetching(payload):
    """Proves the app makes one request per hour, not one per candle close — which
    is what keeps it under the rate limit."""
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=payload)

    calendar = EconomicCalendar(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    for _ in range(5):
        await calendar.events()
    await calendar.aclose()

    assert len(calls) == 1
