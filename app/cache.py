"""A one-value cache with a time to live.

Holds the beat distribution over every recording, the only query that reads all annotations.
It lives here rather than inside the request handler so the policy — how long a value is good
for, and what clears it — can be tested without a database.
"""

import time


class TimedCache[T]:
    """Keeps one value for `seconds`. With `seconds` <= 0 it stores nothing and always misses.

    Deliberately not locked. FastAPI runs the synchronous endpoints in a thread pool, so
    several requests can miss at the same moment and each query the database; the only shared
    mutation is rebinding one tuple, which is atomic in CPython, so a miss storm costs repeated
    work but never a corrupt read. A lock would replace that with a queue on every request,
    which is the worse trade for a query this size.
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._entry: tuple[float, T] | None = None

    def get(self) -> T | None:
        """The stored value, or None when it is absent, expired, or caching is switched off."""
        if self._entry is None or self.seconds <= 0:
            return None
        stored_at, value = self._entry
        return value if time.monotonic() - stored_at < self.seconds else None

    def set(self, value: T) -> None:
        # monotonic(), not time(): it cannot jump backwards when the clock is adjusted.
        self._entry = (time.monotonic(), value) if self.seconds > 0 else None

    def invalidate(self) -> None:
        self._entry = None
