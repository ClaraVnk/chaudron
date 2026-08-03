"""Open Food Facts adapter for the :class:`~chaudron.domain.ports.ProductCatalog` port.

Four constraints from ADR-0008 shape this file, and none of them is optional.

*API v3.* v2 is deprecated and its error contract differs: v3 answers a missing
code with HTTP 404 and ``result.id == "product_not_found"``.

*The rate limit is global to the instance, not per user.* Centralising the calls
in the backend -- the right call for caching -- means every household shares one
IP, and therefore one budget of roughly fifteen requests per minute. Exceeding it
gets the IP banned, and the ban is served as HTML rather than JSON, so a parser
that assumes JSON reports a nonsense error.

*A non-answer is not an absence.* Only a catalogue that replied "I do not know
this code" may be cached as a miss; a timeout must not poison the cache for a
month.

*The caller identifies itself honestly*, per their API policy.

A fifth constraint was added after the security audit: **what comes back is
untrusted.** Open Food Facts is a wiki, and ADR-0008 writes its answers into the
catalogue table shared by every household. A hostile contributor therefore edits a
row that other people read, and that row reaches an LLM prompt (AUD-006) and an
``<img src>`` in their browser (AUD-017). Text is reduced to bounded single lines
here, at the boundary, and the image URL is refused unless it is an HTTPS URL on
the catalogue's own domain -- so the poison never enters the shared table in the
first place, rather than being neutralised at every one of its readers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, Final, Self

import httpx

from chaudron.config import Settings
from chaudron.domain.ports import CatalogRecord, ProductCatalogUnavailableError, display_gtin
from chaudron.infra.untrusted_text import sanitize_optional

logger = logging.getLogger(__name__)

#: Observed ceiling is ~15/min before an IP ban. Ten leaves room for the retries
#: and health probes that also leave this process, and a ban costs far more than
#: the throughput it saves. Not configurable on purpose: raising it is a decision
#: about somebody else's free service, not about this deployment.
MAX_CALLS_PER_MINUTE: Final = 10
RATE_LIMIT_WINDOW_SECONDS: Final = 60.0

#: How long a caller may sit in the queue before being told to come back. Longer
#: than this and the user is staring at a spinner in front of an open fridge.
MAX_QUEUE_WAIT_SECONDS: Final = 3.0

#: Sent when we throttle ourselves or upstream throttles us without saying when.
DEFAULT_RETRY_AFTER_SECONDS: Final = 30

_REQUEST_TIMEOUT_SECONDS: Final = 10.0

#: Redirects are followed -- the API answers on a country subdomain and moving the
#: base URL between them is an operator's prerogative -- but only a couple, and only
#: within the catalogue's own domain. Unbounded following turns a compromised
#: upstream into a way to make this server dial an address of its choosing.
_MAX_REDIRECTS: Final = 2

#: Ceilings on the free text the wiki supplies. A product name is a name; a label
#: longer than this is either vandalism or a description in the wrong field, and in
#: both cases truncating it loses nothing a cook needed.
_MAX_NAME_CHARS: Final = 200
_MAX_BRAND_CHARS: Final = 120
_MAX_CATEGORY_CHARS: Final = 120
#: Long enough for the deepest real Open Food Facts image path, short enough that a
#: URL cannot become a payload in its own right.
_MAX_IMAGE_URL_CHARS: Final = 500


class RateLimiter:
    """Sliding-window limiter shared by every request served by this process.

    A token bucket would allow a burst that is exactly what gets an IP banned;
    the window refuses the burst instead of smoothing it.
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, max_wait: float) -> None:
        """Reserve a slot, waiting at most ``max_wait`` seconds.

        Raises :class:`TimeoutError` rather than queueing indefinitely: an
        unbounded queue turns a rate limit into a request pile-up.
        """
        deadline = time.monotonic() + max_wait
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return
                wait_for = self._window - (now - self._calls[0])
            if time.monotonic() + wait_for > deadline:
                raise TimeoutError
            await asyncio.sleep(min(wait_for, max(0.0, deadline - time.monotonic())))


class OpenFoodFactsCatalog:
    """Resolve a barcode against Open Food Facts, once, politely."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._base_url = settings.off_base_url.rstrip("/")
        self._trusted_suffix = _domain_suffix(self._base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": settings.off_user_agent, "Accept": "application/json"},
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
        )
        self._limiter = limiter or RateLimiter(MAX_CALLS_PER_MINUTE, RATE_LIMIT_WINDOW_SECONDS)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def lookup(self, gtin: str) -> CatalogRecord | None:
        """Return the catalogue entry, or ``None`` when it answered "unknown"."""
        try:
            await self._limiter.acquire(MAX_QUEUE_WAIT_SECONDS)
        except TimeoutError:
            raise ProductCatalogUnavailableError(
                "the Open Food Facts request budget for this instance is exhausted",
                retry_after=DEFAULT_RETRY_AFTER_SECONDS,
            ) from None

        # The path carries the barcode as printed, not as stored: the upstream
        # index is keyed on the unpadded form.
        url = f"{self._base_url}/api/v3/product/{display_gtin(gtin)}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("open_food_facts_unreachable", extra={"error": str(exc)})
            raise ProductCatalogUnavailableError(
                "Open Food Facts is unreachable", retry_after=DEFAULT_RETRY_AFTER_SECONDS
            ) from exc

        if response.history and not _within(response.url.host, self._trusted_suffix):
            # A redirect off the catalogue's own domain is a body we must not read
            # and a hop we did not intend. It cannot un-dial the first request, but
            # it stops the chain and keeps the answer out of the shared table.
            logger.warning(
                "open_food_facts_redirected_off_domain",
                extra={"final_host": response.url.host},
            )
            raise ProductCatalogUnavailableError(
                "Open Food Facts redirected off its own domain",
                retry_after=DEFAULT_RETRY_AFTER_SECONDS,
            )

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise ProductCatalogUnavailableError(
                "Open Food Facts has rate-limited this instance",
                retry_after=_retry_after(response),
            )
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise ProductCatalogUnavailableError(
                "Open Food Facts returned a server error",
                retry_after=DEFAULT_RETRY_AFTER_SECONDS,
            )

        payload = _decode(response)

        if response.status_code == httpx.codes.NOT_FOUND:
            # v3 distinguishes "no such product" from "the request was wrong";
            # only the former is a cacheable absence.
            if _result_id(payload) == "product_not_found":
                return None
            raise ProductCatalogUnavailableError(
                "Open Food Facts rejected the request", retry_after=DEFAULT_RETRY_AFTER_SECONDS
            )
        if response.status_code != httpx.codes.OK:
            raise ProductCatalogUnavailableError(
                f"Open Food Facts answered {response.status_code}",
                retry_after=DEFAULT_RETRY_AFTER_SECONDS,
            )

        product = payload.get("product")
        if not isinstance(product, dict):
            raise ProductCatalogUnavailableError(
                "Open Food Facts answered without a product",
                retry_after=DEFAULT_RETRY_AFTER_SECONDS,
            )
        return _to_record(gtin, product, trusted_suffix=self._trusted_suffix)


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Parse the body as JSON, or report the service as unavailable.

    A banned IP is served an HTML page with a 2xx or 4xx status; decoding it as
    JSON is the failure mode ADR-0008 records as observed in practice.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProductCatalogUnavailableError(
            "Open Food Facts answered with a non-JSON body, which is what a ban looks like",
            retry_after=DEFAULT_RETRY_AFTER_SECONDS,
        ) from exc
    if not isinstance(payload, dict):
        raise ProductCatalogUnavailableError(
            "Open Food Facts answered with an unexpected JSON shape",
            retry_after=DEFAULT_RETRY_AFTER_SECONDS,
        )
    return payload


def _result_id(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if isinstance(result, dict):
        identifier = result.get("id")
        if isinstance(identifier, str):
            return identifier
    return None


def _retry_after(response: httpx.Response) -> int:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        # An HTTP-date is legal here and worth honouring eventually; until then a
        # known-good default beats crashing on a header we asked for.
        return DEFAULT_RETRY_AFTER_SECONDS


def _first_string(product: dict[str, Any], *keys: str, limit: int) -> str | None:
    """The first key that carries usable text, reduced to one bounded line.

    The reduction is not cosmetic. These values are written by anyone with a wiki
    account, stored in a table shared by every household, and interpolated into an
    LLM prompt; a name allowed to contain newlines could forge a prompt section
    (AUD-006), and one allowed to be unbounded could crowd out the instructions.
    """
    for key in keys:
        value = product.get(key)
        if isinstance(value, str):
            cleaned = sanitize_optional(value, limit=limit)
            if cleaned is not None:
                return cleaned
    return None


def _raw_string(product: dict[str, Any], *keys: str) -> str | None:
    """The first key carrying non-blank text, *unmodified*.

    For values that are parsed rather than displayed. A URL must not go through
    :func:`_first_string`: collapsing and truncating it would turn a too-long URL
    into a short, well-formed, wrong one that then passes every later check.
    Validation of the whole value is the caller's job.
    """
    for key in keys:
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _domain_suffix(base_url: str) -> str:
    """The registrable-ish domain of the configured catalogue, lower-cased.

    Deliberately a heuristic and not a public-suffix lookup: the last two labels of
    ``world.openfoodfacts.org`` give ``openfoodfacts.org``, which is what the image
    hosts and the country subdomains share, and a public-suffix list is a dependency
    and a data file to keep current for one comparison. A base URL that is already
    two labels long is used whole rather than reduced to its TLD -- the failure
    direction that matters is never widening the trust.
    """
    host = (httpx.URL(base_url).host or "").lower()
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _within(host: str | None, suffix: str) -> bool:
    """Whether ``host`` is ``suffix`` itself or a subdomain of it."""
    if not host or not suffix:
        return False
    lowered = host.lower()
    return lowered == suffix or lowered.endswith(f".{suffix}")


def _safe_image_url(raw: str | None, trusted_suffix: str) -> str | None:
    """Keep the image URL only if the browser can be asked to load it safely.

    The PWA puts this straight in an ``<img src>``. It is not an XSS -- React
    escapes the attribute and no browser runs ``javascript:`` there -- but a wiki
    contributor who can choose the host makes the victim's browser announce its IP,
    User-Agent and referrer to that host at the moment they scan the product
    (AUD-017). So: HTTPS only, and on the catalogue's own domain. Anything else
    becomes ``None``, which the interface already handles as "no photograph".
    """
    if raw is None:
        return None
    if len(raw) > _MAX_IMAGE_URL_CHARS:
        return None
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL, ValueError:
        return None
    if url.scheme != "https" or url.userinfo:
        return None
    if not _within(url.host, trusted_suffix):
        return None
    return str(url)


def _to_record(gtin: str, product: dict[str, Any], *, trusted_suffix: str) -> CatalogRecord:
    """Project the upstream document onto the fields the domain uses.

    The whole document is carried along in ``payload``: their schema moves
    without notice, and a field we did not anticipate cannot be recovered later
    without rescanning the article. That raw copy is storage only -- nothing reads
    it into a prompt or a page, and the projected fields above are the sanitised
    ones every reader actually uses.
    """
    categories = product.get("categories_tags")
    category_tag = (
        sanitize_optional(categories[0], limit=_MAX_CATEGORY_CHARS)
        if isinstance(categories, list) and categories and isinstance(categories[0], str)
        else None
    )

    net_value: Decimal | None = None
    net_unit: str | None = None
    raw_quantity = product.get("product_quantity")
    raw_unit = product.get("product_quantity_unit")
    if raw_quantity is not None and isinstance(raw_unit, str):
        try:
            net_value = Decimal(str(raw_quantity))
        except InvalidOperation, ValueError:
            net_value = None
        else:
            net_unit = raw_unit.strip().lower()

    return CatalogRecord(
        gtin=gtin,
        # French first: this instance serves a French-speaking household, and the
        # generic field is often an English or a machine-generated label.
        name=_first_string(
            product, "product_name_fr", "product_name", "generic_name_fr", limit=_MAX_NAME_CHARS
        )
        or display_gtin(gtin),
        brand=_first_string(product, "brands", limit=_MAX_BRAND_CHARS),
        image_url=_safe_image_url(
            _raw_string(product, "image_front_url", "image_url"), trusted_suffix
        ),
        category_tag=category_tag,
        net_content_value=net_value,
        net_content_unit_code=net_unit,
        payload=product,
    )
