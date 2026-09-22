"""The cache policy on its own: no database, no HTTP.

tests/test_api.py checks that the endpoint uses it and that ingest clears it. These check the
rules it follows, including expiry, which an API test cannot reach without waiting 30 seconds.
"""

import time

from app.cache import TimedCache


def test_a_stored_value_comes_back():
    cache = TimedCache[str](seconds=30)
    cache.set("rows")
    assert cache.get() == "rows"


def test_empty_until_something_is_stored():
    assert TimedCache[str](seconds=30).get() is None


def test_invalidate_drops_the_value():
    cache = TimedCache[str](seconds=30)
    cache.set("rows")
    cache.invalidate()
    assert cache.get() is None


def test_a_value_expires():
    # A real, very short window rather than a patched clock: time.monotonic is also read by
    # psycopg's pool threads, so replacing it globally would reach further than this test.
    cache = TimedCache[str](seconds=0.05)
    cache.set("rows")
    time.sleep(0.06)
    assert cache.get() is None


def test_zero_seconds_stores_nothing():
    """Setting DISTRIBUTION_CACHE_SECONDS=0 must switch caching off, not cache forever."""
    cache = TimedCache[str](seconds=0)
    cache.set("rows")
    assert cache.get() is None
    assert cache._entry is None  # not merely hidden on read: never stored
