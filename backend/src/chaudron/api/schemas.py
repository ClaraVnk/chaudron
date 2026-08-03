"""Request and response models. The strict boundary of the application.

Two contract details are load-bearing and easy to undo by accident.

``quantity.amount`` is a **string**. JSON numbers are IEEE 754 doubles in every
mainstream parser, and a stock quantity wrong by a factor of ten in a food
inventory is not a rounding detail.

Timestamps end in ``Z``. The contract shows ``2026-08-03T18:20:00Z``; Python's
``isoformat`` writes ``+00:00``, which is the same instant and a different
string, and clients do compare strings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from chaudron.domain.ports import ExpiryDateKind, StockEntrySource, StorageKind

#: The three entry sources the v1 contract exposes. The database knows five; the
#: other two belong to flows that are not in this slice.
ContractSource = Literal["manual", "barcode_scan", "receipt_import"]

RemovalReason = Literal["consumed", "wasted", "correction"]

#: numeric(12,3): three decimals, so "1" is rendered "1.000" as in the contract.
_QUANTUM = Decimal("0.001")


class StrictModel(BaseModel):
    """Unknown fields are rejected, not ignored.

    A client sending ``expiry_date`` instead of ``expires_on`` must be told, not
    silently given a lot without an expiry date.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessOut(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #


class LocationOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: StorageKind
    item_count: int


class LocationRefOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: StorageKind


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    brand: str | None
    gtin: str | None
    image_url: str | None


class ProductDetailOut(ProductOut):
    """The lookup response: the contract's fields plus what a prefill needs."""

    default_unit: str | None = None
    category_tag: str | None = None


class ProductCreateIn(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=300)]
    brand: Annotated[str | None, Field(default=None, max_length=200)]
    gtin: Annotated[str | None, Field(default=None, min_length=8, max_length=14)]
    default_unit: Annotated[str | None, Field(default=None, max_length=16)]


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


class QuantityOut(BaseModel):
    amount: Decimal
    unit: str

    @field_serializer("amount")
    def _as_string(self, value: Decimal) -> str:
        return str(value.quantize(_QUANTUM))


class InventoryItemOut(BaseModel):
    id: uuid.UUID
    product: ProductOut
    location: LocationRefOut | None
    quantity: QuantityOut
    expires_on: date | None
    expiry_kind: ExpiryDateKind
    opened_at: date | None
    source: StockEntrySource
    created_at: datetime

    @field_serializer("created_at")
    def _as_zulu(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class InventoryPageOut(BaseModel):
    total: int
    items: list[InventoryItemOut]


class InventoryCreateIn(StrictModel):
    product_id: uuid.UUID | None = None
    product: ProductCreateIn | None = None
    location_id: uuid.UUID | None = None
    amount: Decimal
    unit: Annotated[str, Field(min_length=1, max_length=16)]
    expires_on: date | None = None
    expiry_kind: ExpiryDateKind | None = None
    opened_at: date | None = None
    source: ContractSource = "manual"

    @model_validator(mode="after")
    def _exactly_one_product(self) -> InventoryCreateIn:
        if (self.product_id is None) == (self.product is None):
            raise ValueError("provide exactly one of product_id or product")
        return self


class InventoryPatchIn(StrictModel):
    """Every field optional. ``model_fields_set`` tells absent from explicitly null."""

    amount: Decimal | None = None
    unit: Annotated[str | None, Field(default=None, min_length=1, max_length=16)]
    location_id: uuid.UUID | None = None
    expires_on: date | None = None
    expiry_kind: ExpiryDateKind | None = None
    opened_at: date | None = None


# --------------------------------------------------------------------------- #
# Provider capabilities
# --------------------------------------------------------------------------- #


class CapabilityFlagsOut(BaseModel):
    """The two capabilities the v1 contract exposes.

    The domain knows four; the other two (prompt caching, long context) change what
    a call *costs* or *covers*, not what the interface may offer, so they reach the
    user as a degradation reason rather than as a flag.
    """

    vision: bool
    structured_output: bool


class ProviderCapabilitiesOut(BaseModel):
    """``configured: false`` with null identifiers is a normal state, not an error.

    ``degraded_reasons`` carries user-facing French sentences, not error codes: the
    PWA shows them in a permanent banner, before the user tries something rather
    than after it fails.
    """

    configured: bool
    mode: str | None
    provider: str | None
    model: str | None
    capabilities: CapabilityFlagsOut
    degraded: bool
    degraded_reasons: list[str]


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #

#: A ceiling on a request that spends real money -- the household's or the
#: operator's. The contract's example asks for three.
MAX_SUGGESTIONS: Final = 5

#: More locations than any plausible home has. The bound exists so one request
#: cannot fan out into an unbounded number of inventory queries.
MAX_LOCATION_FILTERS: Final = 20


class RecipeIngredientOut(BaseModel):
    """``in_stock`` is computed from the household's own stock, never from the model."""

    name: str
    amount: str | None
    unit: str | None
    in_stock: bool


class RecipeSuggestionOut(BaseModel):
    id: uuid.UUID
    title: str
    #: Never null in the contract: a model that returned no summary yields "".
    summary: str
    duration_minutes: int | None
    servings: int | None
    ingredients: list[RecipeIngredientOut]
    steps: list[str]
    uses_expiring_soon: bool


class SuggestRecipesIn(StrictModel):
    location_ids: Annotated[
        list[uuid.UUID], Field(default_factory=list, max_length=MAX_LOCATION_FILTERS)
    ]
    max_suggestions: Annotated[int, Field(default=3, ge=1, le=MAX_SUGGESTIONS)]
    notes: Annotated[str, Field(default="", max_length=500)]


class SuggestRecipesOut(BaseModel):
    provider_mode: str
    model: str
    suggestions: list[RecipeSuggestionOut]
