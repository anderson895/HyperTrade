"""High-impact economic events, for the news blackout.

Leverage through a data release is how accounts get liquidated: CPI and FOMC move
BTC several percent in seconds, the book thins out, and a stop that normally fills
within a tick fills wherever the next resting order happens to be. The strategy has
no view on any of that — it reads a candle close — so the only sound response is to
stand aside.

The schedule comes from ForexFactory's weekly JSON feed: free, no key, and it
carries an impact rating so "high impact, United States" can be selected rather than
every minor print. Only the *timing* is used. Forecast and actual values are ignored
on purpose — trading the number is a different system to this one.

The decision itself (`blackout_reason`) is a pure function over a list of events, so
it is tested against fixed clocks rather than against whatever the calendar happens
to hold this week.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

#: Only these matter for BTC. The feed also carries Low and Medium, which are
#: routine prints that do not move the book enough to stand aside for.
TRACKED_IMPACT = "High"
#: US releases. BTC trades against the dollar and reacts to the Fed, not to the
#: Australian trade balance.
TRACKED_COUNTRY = "USD"

#: The feed is a weekly file that changes rarely; refetching every hour is plenty
#: and keeps a stopped-then-started bot from hammering it. The feed rate-limits at
#: roughly a request a minute (429 with `Retry-After: 67`), so this is also what
#: keeps a restart loop from being locked out.
CACHE_SECONDS = 3600.0

#: How stale a cached calendar may be before it is refused outright. The feed covers
#: *this week*, so a copy older than this may simply not contain today's releases —
#: and a calendar that has quietly run out of events looks exactly like a quiet week.
#: Past this point the blackout fails closed instead.
MAX_STALE_SECONDS = 24 * 3600.0


class CalendarUnavailable(RuntimeError):
    """The calendar could not be read, so no claim about news can be made.

    Distinct from "no events found": not knowing is not the same as knowing there
    is nothing, and the two must not collapse into each other. The engine refuses
    to trade on this rather than assuming the coast is clear.
    """


@dataclass(frozen=True)
class Event:
    title: str
    when: datetime  # always timezone-aware, normalised to UTC


def _parse(payload: object) -> list[Event]:
    """Pull the tracked events out of a feed response.

    Anything unparseable is skipped rather than raised on — one malformed row in a
    third-party file must not take the blackout offline, which would stop trading
    entirely under the fail-closed policy.
    """
    if not isinstance(payload, list):
        raise CalendarUnavailable(f"calendar feed returned {type(payload).__name__}, not a list")

    events: list[Event] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if row.get("impact") != TRACKED_IMPACT or row.get("country") != TRACKED_COUNTRY:
            continue
        raw = row.get("date")
        if not isinstance(raw, str):
            continue
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            log.debug("calendar row has an unreadable date: %r", raw)
            continue
        if when.tzinfo is None:
            continue  # a naive timestamp could be any zone; guessing would be worse
        events.append(Event(title=str(row.get("title", "economic release")),
                            when=when.astimezone(timezone.utc)))

    return sorted(events, key=lambda event: event.when)


def blackout_reason(
    events: list[Event], now: datetime, before_minutes: int, after_minutes: int
) -> str | None:
    """Why trading is closed right now, or None to carry on.

    The window is asymmetric by default — wider before than after — because the
    approach to a release is when liquidity withdraws, while the minutes afterwards
    are when it comes back.
    """
    for event in events:
        opens = event.when.timestamp() - before_minutes * 60
        closes = event.when.timestamp() + after_minutes * 60
        if opens <= now.timestamp() <= closes:
            minutes = (event.when.timestamp() - now.timestamp()) / 60
            when = (
                f"in {minutes:.0f} min" if minutes >= 0 else f"{abs(minutes):.0f} min ago"
            )
            return f"news blackout: {event.title} {when}"
    return None


class EconomicCalendar:
    """Fetches the weekly feed and caches it."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        conn: sqlite3.Connection | None = None,
        url: str = FEED_URL,
        cache_seconds: float = CACHE_SECONDS,
        max_stale_seconds: float = MAX_STALE_SECONDS,
    ) -> None:
        """`conn` persists the cache across restarts. Without one the calendar still
        works, it just refetches on every launch — which the feed's rate limit
        punishes."""
        self._url = url
        self._conn = conn
        self._cache_seconds = cache_seconds
        self._max_stale_seconds = max_stale_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": "HyperTrade"}
        )
        self._events: list[Event] | None = None
        #: Wall clock, not monotonic — it has to survive a process restart.
        self._fetched_at = 0.0
        self._load_cache()

    # --- the cross-restart cache -----------------------------------------

    def _load_cache(self) -> None:
        if self._conn is None:
            return
        try:
            row = self._conn.execute(
                "SELECT fetched_ms, payload FROM calendar_cache WHERE id = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            log.debug("no calendar cache to read: %s", exc)
            return
        if row is None:
            return

        try:
            events = _parse(json.loads(row["payload"]))
        except (CalendarUnavailable, ValueError) as exc:
            log.debug("stored calendar is unreadable, ignoring it: %s", exc)
            return

        self._events = events
        self._fetched_at = row["fetched_ms"] / 1000.0
        log.debug("loaded %d calendar events cached %.0f min ago",
                  len(events), (time.time() - self._fetched_at) / 60)

    def _store_cache(self, payload: object) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO calendar_cache (id, fetched_ms, payload) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "fetched_ms = excluded.fetched_ms, payload = excluded.payload",
                (int(time.time() * 1000), json.dumps(payload)),
            )
            self._conn.commit()
        except (sqlite3.Error, TypeError) as exc:
            # A cache that will not write is a performance problem, not a trading
            # one — the in-memory copy still stands.
            log.warning("could not store the calendar cache: %s", exc)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def events(self) -> list[Event]:
        """The tracked events, refetching only once the cache has aged out.

        A failed refetch falls back to the cached copy: a calendar an hour stale is
        still a good calendar, and far better than halting on a blip. Only a failure
        with nothing cached raises.
        """
        age = time.time() - self._fetched_at
        if self._events is not None and age < self._cache_seconds:
            return self._events

        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            payload = response.json()
            events = _parse(payload)
        except CalendarUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — httpx, JSON, and socket errors alike
            # A copy from within the last day still covers this week's releases, and
            # standing down over a rate limit would be a worse outcome than using it.
            if self._events is not None and age < self._max_stale_seconds:
                log.warning(
                    "could not refresh the economic calendar, using a copy %.0f min old: %s",
                    age / 60, exc,
                )
                return self._events
            raise CalendarUnavailable(f"could not read the economic calendar: {exc}") from exc

        self._events = events
        self._fetched_at = time.time()
        self._store_cache(payload)
        log.info("economic calendar: %d high-impact US events this week", len(events))
        return events

    async def reason_not_to_trade(self, before_minutes: int, after_minutes: int) -> str | None:
        """Blackout reason for right now. Raises `CalendarUnavailable` if unknown."""
        return blackout_reason(
            await self.events(), datetime.now(timezone.utc), before_minutes, after_minutes
        )
