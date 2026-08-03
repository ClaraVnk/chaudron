"""Per-household caps on the two endpoints that spend something the caller does not own.

``POST /v1/recipes/suggest`` spends model tokens -- billed to the household in
``byok`` mode, to the operator in ``instance_owner`` mode -- or CPU on a
colocated Ollama that a single oversized request has already been observed to
OOM-kill (``infra/llm/settings.py``). ``GET /v1/products/lookup`` spends the Open
Food Facts budget, which ADR-0008 fixes at ten calls per minute **for the whole
instance**: one household looping on it takes barcode resolution away from every
other household on the same deployment.

**The key is the household, not the source address.** An address is shared by
every device behind a home router and is changed by turning a phone's data
connection off and on; the household identifier is at least the unit the cost is
attributed to. It is emphatically *not* a proof of identity -- the header is
provisional, see :func:`chaudron.api.deps.get_household_id` -- so what is built
here is a **spend cap per tenant, not an anti-abuse control**: somebody holding
three household identifiers gets three budgets. Closing that requires
authentication, which is a separate decision and deliberately not made here.

**Scope: one process, and nothing wider.** The counters are plain dictionaries
living on ``app.state``. Two uvicorn workers therefore grant two budgets, a
restart forgets every counter, and a second replica doubles everything again.
That is accepted for this slice, and only because of what these limits are *for*:
making a trivial loop from one caller stop being free. The instance-wide Open
Food Facts budget is still enforced where it belongs, in
``infra/openfoodfacts.py``, and it is not weakened by anything here. Before this
deployment grows a second worker or a second replica, the state has to move to
PostgreSQL -- a per-household window row updated with ``UPDATE ... RETURNING``
inside the request transaction -- or the limits stop meaning what they say. This
paragraph exists so that the day somebody adds ``--workers 4``, the regression is
written down rather than discovered.

Both limiters are synchronous and mutate their state without awaiting in
between, which is what makes them safe under a single event loop: no other task
can observe a half-updated bucket.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

#: What a caller waiting on a concurrency slot is told. The honest answer is "as
#: long as the inference in front of you takes", which is unknown and provider
#: dependent; half a minute is short enough to be worth retrying and long enough
#: not to invite an immediate retry storm.
CONCURRENCY_RETRY_AFTER_SECONDS: int = 30


class AtCapacityError(Exception):
    """A limiter refused. ``retry_after`` is in seconds, and is never zero."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"at capacity, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """``limit`` requests per ``window_seconds`` and per key, refilled continuously.

    A token bucket rather than a fixed window: a fixed window lets a caller spend
    the whole budget in the last second of one window and the whole of the next in
    the first second of the following one, which is twice the advertised rate at
    exactly the wrong moment. It also yields an exact ``Retry-After`` -- the time
    until one token is back -- instead of a guess.
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._capacity = float(limit)
        self._rate = limit / window_seconds
        self._window = window_seconds
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._next_sweep = clock() + window_seconds

    def acquire(self, key: str) -> None:
        """Spend one token for ``key``, or raise :class:`AtCapacityError`."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, updated=now)
            self._buckets[key] = bucket
        else:
            refilled = bucket.tokens + (now - bucket.updated) * self._rate
            bucket.tokens = min(self._capacity, refilled)
            bucket.updated = now

        if bucket.tokens < 1.0:
            self._sweep(now)
            raise AtCapacityError(max(1, math.ceil((1.0 - bucket.tokens) / self._rate)))
        bucket.tokens -= 1.0
        self._sweep(now)

    def _sweep(self, now: float) -> None:
        """Drop keys idle for a full window, so the dictionary cannot grow forever.

        Safe by arithmetic rather than by heuristic: a bucket untouched for a whole
        window has refilled to capacity, so forgetting it and re-creating it full
        are the same thing. Amortised to once per window; the cost is a dictionary
        rebuild, not a per-request scan.
        """
        if now < self._next_sweep:
            return
        self._next_sweep = now + self._window
        self._buckets = {
            key: bucket
            for key, bucket in self._buckets.items()
            if now - bucket.updated < self._window
        }


class ConcurrencyLimiter:
    """A cap on requests in flight, per key and for the whole process.

    Refusing is the point: queueing would turn a burst into a pile of open
    connections each holding a database session and an outbound HTTP call, which
    is the failure this exists to prevent. A caller gets ``429`` immediately and
    decides for itself whether to come back.

    The per-key cap answers "two browser tabs must not double the bill"; the
    process-wide cap answers "a small machine running Ollama must not be asked for
    six inferences at once".
    """

    def __init__(self, *, per_key: int, total: int) -> None:
        if per_key < 1 or total < 1:
            raise ValueError("concurrency caps must be at least 1")
        if total < per_key:
            raise ValueError("the process-wide cap cannot be below the per-key cap")
        self._per_key = per_key
        self._total = total
        self._in_flight: dict[str, int] = {}
        self._current_total = 0

    @contextmanager
    def slot(self, key: str) -> Iterator[None]:
        """Hold a slot for the duration of the block, or raise :class:`AtCapacityError`."""
        held = self._in_flight.get(key, 0)
        if held >= self._per_key or self._current_total >= self._total:
            raise AtCapacityError(CONCURRENCY_RETRY_AFTER_SECONDS)
        self._in_flight[key] = held + 1
        self._current_total += 1
        try:
            yield
        finally:
            remaining = self._in_flight[key] - 1
            if remaining:
                self._in_flight[key] = remaining
            else:
                del self._in_flight[key]
            self._current_total -= 1


@dataclass(frozen=True, slots=True)
class Throttles:
    """The limiters a single application instance owns, built once by the factory."""

    recipe_suggestions: RateLimiter
    recipe_inferences: ConcurrencyLimiter
    product_lookups: RateLimiter
