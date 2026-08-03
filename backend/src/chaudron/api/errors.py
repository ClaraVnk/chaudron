"""RFC 9457 problem details, and the mapping from domain failures onto them.

One rule governs everything here: **no response ever carries a traceback, a SQL
fragment, or a provider credential** -- including in ``detail``. An unexpected
fault becomes an opaque 500 with a request identifier, and the actual cause goes
to the log, where the operator can read it and the internet cannot.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chaudron.domain.ports import (
    BarcodeNotFoundError,
    DomainError,
    ExpiryDateInconsistentError,
    HouseholdNotFoundError,
    InvalidBarcodeError,
    InvalidQuantityError,
    InventoryConflictError,
    InventoryItemNotFoundError,
    LocationNotFoundError,
    ProductCatalogUnavailableError,
    ProductNotFoundError,
    RetailerInternalBarcodeError,
    UnknownUnitError,
    UnsupportedRemovalReasonError,
    display_gtin,
)
from chaudron.infra.logging import request_id_var

logger = logging.getLogger(__name__)

PROBLEM_BASE_URI: Final = "https://chaudron.dev/problems/"
PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

#: One sentence for "no header", "malformed header" and "unknown household"
#: alike. Three distinguishable answers would let a caller confirm a household
#: identifier without any observable side effect -- an oracle the comment in
#: ``deps.py`` claimed to have closed while the code kept it open (audit
#: AUD-013). The wording says nothing about which of the three happened.
HOUSEHOLD_NOT_RESOLVED_DETAIL: Final = "The X-Household-Id header is missing or invalid."


class ProblemError(Exception):
    """An error that is safe to show a client, in the shape the contract fixes."""

    def __init__(
        self,
        *,
        slug: str,
        title: str,
        status: int,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(title)
        self.slug = slug
        self.title = title
        self.status = status
        self.detail = detail
        self.headers = headers
        self.extensions = extensions

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "type": f"{PROBLEM_BASE_URI}{self.slug}",
            "title": self.title,
            "status": self.status,
        }
        if self.detail is not None:
            body["detail"] = self.detail
        body.update(self.extensions)
        request_id = request_id_var.get()
        if request_id is not None:
            body["request_id"] = request_id
        return JSONResponse(
            body,
            status_code=self.status,
            headers=self.headers,
            media_type=PROBLEM_CONTENT_TYPE,
        )


class RequestBodyTooLarge(StarletteHTTPException):
    """Raised mid-read when a request body passes the configured bound.

    An :class:`HTTPException` subclass rather than a :class:`ProblemError`
    because of where it is raised: inside the ASGI ``receive`` callable, whose
    caller is FastAPI's body reader. That reader re-raises ``HTTPException``
    unchanged and rewrites every other exception into a generic ``400 There was
    an error parsing the body`` -- which would report a refused 50 MB upload as a
    malformed one. The registered handler turns it back into the RFC 9457 shape
    every other error in this application uses.
    """

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(status_code=413, detail="Request body too large")
        self.limit_bytes = limit_bytes


def unauthorized(detail: str = HOUSEHOLD_NOT_RESOLVED_DETAIL) -> ProblemError:
    return ProblemError(
        slug="household-not-resolved",
        title="Household not resolved",
        status=401,
        detail=detail,
    )


def problem_for_body_too_large(limit_bytes: int) -> ProblemError:
    """``413``, quoting the bound so a client can act rather than guess."""
    return ProblemError(
        slug="request-body-too-large",
        title="Request body too large",
        status=413,
        detail=f"The request body exceeds the {limit_bytes} byte limit of this endpoint.",
        limit_bytes=limit_bytes,
    )


def rate_limited(*, detail: str, retry_after: int) -> ProblemError:
    """``429`` with the one header a client needs to behave: ``Retry-After``.

    The delay is measured by the limiter that refused, not invented here.
    """
    return ProblemError(
        slug="rate-limited",
        title="Too many requests",
        status=429,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
        retry_after=retry_after,
    )


def problem_for(error: DomainError) -> ProblemError:
    """Translate a domain failure into its public form.

    A single mapping rather than per-route ``except`` blocks: a handler that
    forgets one becomes a 500, and a 500 on a knowable error is how a client
    ends up unable to tell "you asked for the wrong thing" from "we broke".
    """
    match error:
        case BarcodeNotFoundError():
            return ProblemError(
                slug="product-not-found",
                title="Product not found",
                status=404,
                detail=f"No product matches GTIN {display_gtin(error.gtin)}.",
                gtin=display_gtin(error.gtin),
            )
        case ProductNotFoundError():
            return ProblemError(
                slug="product-not-found",
                title="Product not found",
                status=404,
                detail="No product with this identifier is visible to this household.",
            )
        case LocationNotFoundError():
            return ProblemError(
                slug="location-not-found",
                title="Storage location not found",
                status=404,
                detail="No storage location with this identifier belongs to this household.",
            )
        case InventoryItemNotFoundError():
            return ProblemError(
                slug="inventory-item-not-found",
                title="Inventory item not found",
                status=404,
                detail="No inventory item with this identifier belongs to this household.",
            )
        case RetailerInternalBarcodeError():
            return ProblemError(
                slug="retailer-internal-barcode",
                title="Retailer-internal barcode",
                status=422,
                detail=(
                    "This barcode is a variable-weight in-store code; it encodes a price "
                    "and will never appear in a public product reference. Enter the "
                    "product manually."
                ),
                gtin=display_gtin(error.gtin),
            )
        case InvalidBarcodeError():
            return ProblemError(
                slug="invalid-barcode",
                title="Invalid barcode",
                status=422,
                detail="A barcode must be 8 to 14 digits.",
            )
        case ProductCatalogUnavailableError():
            return ProblemError(
                slug="product-catalog-unavailable",
                title="Product catalogue unavailable",
                status=503,
                detail=(
                    "Open Food Facts did not answer. Retry shortly, or enter the product manually."
                ),
                headers={"Retry-After": str(error.retry_after)},
            )
        case UnknownUnitError():
            return ProblemError(
                slug="unknown-unit",
                title="Unknown unit",
                status=422,
                detail=f"{error.code!r} is not a known measurement unit.",
            )
        case InvalidQuantityError():
            return ProblemError(
                slug="invalid-quantity",
                title="Invalid quantity",
                status=422,
                detail=str(error),
            )
        case ExpiryDateInconsistentError():
            return ProblemError(
                slug="expiry-date-inconsistent",
                title="Inconsistent expiry date",
                status=422,
                detail=str(error),
            )
        case UnsupportedRemovalReasonError():
            return ProblemError(
                slug="unsupported-removal-reason",
                title="Unsupported removal reason",
                status=422,
                detail="reason must be one of: consumed, wasted, correction.",
            )
        case InventoryConflictError():
            return ProblemError(
                slug="inventory-conflict",
                title="Concurrent inventory change",
                status=409,
                detail="Another change to the same lot won the race. Retry the request.",
            )
        case HouseholdNotFoundError():
            return unauthorized()
        case _:
            return ProblemError(
                slug="invalid-request",
                title="Invalid request",
                status=400,
                detail=str(error),
            )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the handlers that make every error path answer in one shape."""

    async def handle_problem(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProblemError)
        return exc.to_response()

    async def handle_domain_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return problem_for(exc).to_response()

    async def handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        # `loc` and `msg` only: pydantic's `input` echoes the submitted value,
        # which on a credential-bearing body would put it in a client log.
        errors = [
            {"loc": [str(part) for part in error["loc"]], "msg": error["msg"]}
            for error in exc.errors()
        ]
        return ProblemError(
            slug="validation-failed",
            title="Request validation failed",
            status=422,
            detail="The request body or parameters did not match the expected shape.",
            errors=errors,
        ).to_response()

    async def handle_body_too_large(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestBodyTooLarge)
        return problem_for_body_too_large(exc.limit_bytes).to_response()

    async def handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return ProblemError(
            slug="http-error",
            title=str(exc.detail),
            status=exc.status_code,
            headers=dict(exc.headers) if exc.headers else None,
        ).to_response()

    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The identifier is the only thing tying this opaque answer to the log
        # line that holds the traceback.
        incident = request_id_var.get() or str(uuid.uuid4())
        logger.exception(
            "unhandled_exception",
            extra={"path": request.url.path, "method": request.method, "incident": incident},
            exc_info=exc,
        )
        return ProblemError(
            slug="internal-error",
            title="Internal server error",
            status=500,
            detail="The request could not be processed. The incident has been logged.",
        ).to_response()

    app.add_exception_handler(ProblemError, handle_problem)
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    # Registered before its base class: Starlette resolves a handler by walking
    # the exception's MRO, so the specific one has to exist for it to be found.
    app.add_exception_handler(RequestBodyTooLarge, handle_body_too_large)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)
