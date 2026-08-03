"""Inventory: listing, adding, correcting and removing stock."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from chaudron.api.deps import HouseholdDep, InventoryServiceDep
from chaudron.api.schemas import (
    InventoryCreateIn,
    InventoryItemOut,
    InventoryPageOut,
    InventoryPatchIn,
    LocationRefOut,
    ProductOut,
    QuantityOut,
    RemovalReason,
)
from chaudron.domain.ports import (
    UNSET,
    InventoryFilter,
    InventoryItem,
    Maybe,
    ProductDraft,
    StockEntrySource,
    UnsetType,
    display_gtin,
)
from chaudron.services.inventory import AddItemCommand, UpdateItemCommand

router = APIRouter(prefix="/v1/inventory", tags=["inventory"])

#: A page big enough to be useful, small enough that no single request can pin a
#: connection for a second. The contract's default is 50.
MAX_PAGE_SIZE = 200


def _to_out(item: InventoryItem) -> InventoryItemOut:
    return InventoryItemOut(
        id=item.id,
        product=ProductOut(
            id=item.product.id,
            name=item.product.name,
            brand=item.product.brand,
            gtin=None if item.product.gtin is None else display_gtin(item.product.gtin),
            image_url=item.product.image_url,
        ),
        location=(
            None
            if item.location is None
            else LocationRefOut(
                id=item.location.id, name=item.location.name, kind=item.location.kind
            )
        ),
        quantity=QuantityOut(amount=item.quantity_value, unit=item.quantity_unit_code),
        expires_on=item.best_before,
        expiry_kind=item.date_kind,
        opened_at=item.opened_at,
        source=item.entry_source,
        created_at=item.created_at,
    )


@router.get("", response_model=InventoryPageOut, summary="List the current stock")
async def list_inventory(
    household_id: HouseholdDep,
    service: InventoryServiceDep,
    location_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    expiring_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InventoryPageOut:
    page = await service.list_items(
        household_id,
        InventoryFilter(
            location_id=location_id,
            query=q,
            expiring_within_days=expiring_within_days,
            limit=limit,
            offset=offset,
        ),
    )
    return InventoryPageOut(total=page.total, items=[_to_out(item) for item in page.items])


@router.post(
    "",
    response_model=InventoryItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add stock",
)
async def add_inventory_item(
    household_id: HouseholdDep, service: InventoryServiceDep, payload: InventoryCreateIn
) -> InventoryItemOut:
    item = await service.add_item(
        household_id,
        AddItemCommand(
            amount=payload.amount,
            unit=payload.unit,
            product_id=payload.product_id,
            new_product=(
                None
                if payload.product is None
                else ProductDraft(
                    name=payload.product.name,
                    brand=payload.product.brand,
                    gtin=payload.product.gtin,
                    default_unit_code=payload.product.default_unit,
                )
            ),
            location_id=payload.location_id,
            expires_on=payload.expires_on,
            expiry_kind=payload.expiry_kind,
            opened_at=payload.opened_at,
            source=StockEntrySource(payload.source),
        ),
    )
    return _to_out(item)


@router.patch("/{item_id}", response_model=InventoryItemOut, summary="Correct a stock item")
async def patch_inventory_item(
    household_id: HouseholdDep,
    service: InventoryServiceDep,
    item_id: uuid.UUID,
    payload: InventoryPatchIn,
) -> InventoryItemOut:
    provided = payload.model_fields_set

    def maybe[T](field: str, value: T) -> Maybe[T]:
        """``UNSET`` unless the client actually sent the field.

        Without this, ``PATCH {"amount": 2}`` would read as "and clear the expiry
        date", because every unsent optional field defaults to ``None``.
        """
        return value if field in provided else UNSET

    item = await service.update_item(
        household_id,
        item_id,
        UpdateItemCommand(
            amount=_required(maybe("amount", payload.amount)),
            unit=_required(maybe("unit", payload.unit)),
            location_id=maybe("location_id", payload.location_id),
            expires_on=maybe("expires_on", payload.expires_on),
            expiry_kind=_required(maybe("expiry_kind", payload.expiry_kind)),
            opened_at=maybe("opened_at", payload.opened_at),
        ),
    )
    return _to_out(item)


@router.delete(
    "/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a stock item",
)
async def delete_inventory_item(
    household_id: HouseholdDep,
    service: InventoryServiceDep,
    item_id: uuid.UUID,
    reason: RemovalReason = "consumed",
) -> Response:
    await service.remove_item(household_id, item_id, reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _required[T](value: Maybe[T | None]) -> Maybe[T]:
    """Drop an explicit ``null`` on a field that cannot be cleared.

    ``amount``, ``unit`` and ``expiry_kind`` are always present on a lot; sending
    ``null`` for them is meaningless, and treating it as "leave alone" is kinder
    than a validation error nobody can act on.
    """
    if isinstance(value, UnsetType) or value is None:
        return UNSET
    return value
