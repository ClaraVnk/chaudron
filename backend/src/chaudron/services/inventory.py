"""Inventory use cases: adding, correcting and removing stock.

Two rules live here and nowhere else.

*Conversion happens once, on write.* A quantity is stored twice -- as typed, and
in the canonical unit of its dimension. Recomputing on read would join the unit
reference on the busiest screen of the application, and a trigger would make the
rule invisible and untestable (``docs/data-model.md`` section 6.1).

*Every quantity change is written to the ledger.* ``stock_movement`` is what
makes waste measurable and undo possible; an ``UPDATE … SET quantity = …`` that
skips it destroys both, silently and irreversibly.

**Freezing and thawing write no ledger entry, and that is a decision rather than
an omission.** ``stock_movement`` is a quantity ledger --
``inventory_lot.quantity_canonical`` is documented as a cache of
``SUM(delta_canonical)``, and ``ck_stock_movement_delta_nonzero`` refuses a row
that moves nothing. A freeze moves no quantity, so the only entry it could write
is one the database rejects; writing a ``-x`` and a ``+x`` pair to get around
that would fabricate two events that did not happen and double every "how much
moved" figure computed from the table. The state change keeps its own record, and
a better one: ``frozen_at`` and ``thawed_at`` are dates rather than flags, so the
lot carries *when* as well as *what*. ``StockMovementKind.TRANSFER`` stays unused
by this feature; ``docs/data-model.md`` §7.5 says why in the same words.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from chaudron.domain.ports import (
    UNSET,
    ExpiryDateInconsistentError,
    ExpiryDateKind,
    InvalidQuantityError,
    InventoryFilter,
    InventoryItem,
    InventoryItemNotFoundError,
    InventoryPage,
    InventoryRepository,
    LocationNotFoundError,
    LocationRepository,
    LocationSummary,
    LotAlreadyExpiredError,
    LotAlreadyFrozenError,
    LotAlreadyThawedError,
    LotDraft,
    LotFreezingState,
    LotNotFrozenError,
    LotUpdate,
    Maybe,
    MissingProductReferenceError,
    ProductDraft,
    ProductNotFoundError,
    ProductRepository,
    StockEntrySource,
    StockMovementKind,
    StorageKind,
    UnitInfo,
    UnitRegistry,
    UnknownUnitError,
    UnsetType,
    UnsupportedRemovalReasonError,
    normalize_gtin,
)

#: ``numeric(12,3)`` / ``numeric(14,3)``: three decimals, and never a float.
_QUANTUM = Decimal("0.001")

#: The reasons the API contract accepts on a deletion, and the ledger entry each
#: one produces. Waste and consumption must not be conflated: telling the two
#: apart is the entire point of the "how much did we throw away" screen.
REMOVAL_KINDS: dict[str, StockMovementKind] = {
    "consumed": StockMovementKind.CONSUMPTION,
    "wasted": StockMovementKind.WASTE,
    "correction": StockMovementKind.ADJUSTMENT,
}

#: The contract's word for what a ``PATCH`` does to a quantity (contract v1.1
#: section 6: "un ajustement manuel porte le motif correction"). Named rather
#: than spelled inline so the value that crosses the service boundary is the
#: contract's vocabulary and not the ledger's free-text ``reason``.
_ADJUSTMENT_REASON: Final = "correction"


@dataclass(frozen=True, slots=True)
class AddItemCommand:
    """Add stock for an existing product, or for one created on the spot.

    Exactly one of ``product_id`` and ``new_product`` is set; the API layer
    enforces it on the request body, and this layer refuses the other cases
    rather than trusting it.
    """

    amount: Decimal
    unit: str
    product_id: uuid.UUID | None = None
    new_product: ProductDraft | None = None
    location_id: uuid.UUID | None = None
    expires_on: date | None = None
    expiry_kind: ExpiryDateKind | None = None
    opened_at: date | None = None
    source: StockEntrySource = StockEntrySource.MANUAL
    #: The receipt line this stock came off, when it came off one. Carried on the
    #: draft rather than written afterwards so that the lot is never briefly in the
    #: database without its provenance -- the budget's coverage figure reads that
    #: column, and a row that is momentarily "stock added without a receipt" is a
    #: row a concurrent read can count wrong.
    source_receipt_line_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateItemCommand:
    amount: Maybe[Decimal] = UNSET
    unit: Maybe[str] = UNSET
    location_id: Maybe[uuid.UUID | None] = UNSET
    expires_on: Maybe[date | None] = UNSET
    expiry_kind: Maybe[ExpiryDateKind] = UNSET
    opened_at: Maybe[date | None] = UNSET


@dataclass(frozen=True, slots=True)
class DepletionSignal:
    """A lot that this mutation took to zero, and the ledger reason that did it.

    Deliberately *not* a :class:`chaudron.domain.shopping.DepletionEvent`: this
    module reports what happened to the stock and knows nothing about shopping
    lists. Whether a reason is worth proposing a repurchase for is contract 6bis'
    question, and it is answered in one place --
    :class:`chaudron.services.shopping_import.DepletionService` -- so ``reason``
    is carried out verbatim, ``correction`` included. A caller that filtered it
    here would be a second copy of a rule that must have exactly one.

    ``None`` rather than an instance of this is how "nothing ran out" is said: a
    lot that was already at zero is not depleted again by being removed twice.
    """

    product_id: uuid.UUID
    reason: str


@dataclass(frozen=True, slots=True)
class UpdatedItem:
    """The lot as it now stands, and whether the update emptied it.

    A pair rather than two calls, because the caller needs both and asking twice
    would mean reading the lot again after it changed.
    """

    item: InventoryItem
    depletion: DepletionSignal | None


class LocationMove(enum.StrEnum):
    """What freezing or thawing did about *where* the lot is filed.

    A code rather than a sentence, and that is the layering rather than a
    preference: three of these four are things the interface has to explain to a
    household in its own language, and a service that returned French prose would
    be the layer that decides how an application talks. The one thing this layer
    owes the caller is which of the four happened.
    """

    #: Filed in the household's freezer (or fridge, on a thaw).
    MOVED = "moved"
    #: Already there. Nothing to do, and not a failure.
    ALREADY_THERE = "already_there"
    #: No location of that kind, or more than one. The application does not guess
    #: between two freezers, and it does not create a location nobody asked for.
    UNRESOLVED = "unresolved"
    #: The destination already holds an identical lot, and
    #: ``uq_inventory_lot_merge_key`` forbids two. The freeze stands; the lot
    #: stays where it is.
    OCCUPIED = "occupied"


#: French, because it is read by a household rather than by an operator -- the
#: same register as `_DEGRADED_REASONS` in `services/providers.py`. It says
#: *safe* first and *worse* second, in that order: a warning that leads with
#: quality is one a hungry reader turns into "do not eat this", and throwing away
#: safe food is the failure this application exists to reduce.
_OVERDUE_IN_THE_FREEZER: Final = (
    "Ce lot est resté au congélateur au-delà de la durée indicative de sa "
    "famille. À -18 °C il reste sûr : ce repère porte sur la qualité, pas sur le "
    "risque. Attendez-vous à une texture et à un goût dégradés. Les 3 jours qui "
    "commencent maintenant, eux, sont bien une limite de sécurité."
)


@dataclass(frozen=True, slots=True)
class FreezingOutcome:
    """The lot after a freeze or a thaw, and the two things the user must be told.

    ``freezing_note`` is why this operation refuses less than it might. The
    guideline table has a figure for three families and none for the other six,
    and two of those six -- eggs in shell, a sealed tin -- carry a note that says
    *do not do this*. The temptation is to refuse those outright, and it was
    considered and rejected: the household is reporting a fact about its own
    freezer, not requesting permission, and a refusal does not keep the eggs out
    of the freezer -- it only keeps the application from knowing they are in it,
    which is precisely the blindness this feature exists to end. Compare the three
    refusals above, which all protect a *computed date* from becoming a lie. The
    inadvisable families threaten no date at all: ``home_frozen_days`` is NULL, so
    nothing is proposed. So the note is carried out verbatim, on the operation and
    on every listing, and an adult decides.

    ``proposes_a_date`` says whether the household should now expect one, and it
    is a separate field on purpose: "no date proposed" and "keeps indefinitely"
    are the two readings of a null expiry, and only one of them is true.
    """

    item: InventoryItem
    move: LocationMove
    #: Where it went, when it went anywhere. ``None`` for every other outcome.
    moved_to: LocationSummary | None
    freezing_note: str | None
    proposes_a_date: bool
    #: Set when thawing a lot that had already run past its frozen duration, and
    #: it exists because the rule is right while the *screen* would have lied.
    #:
    #: Chicken frozen four months ago reads as expired: `frozen_at + 90` is a
    #: month behind. Thawing it makes the date `today + 3`, so the interface goes
    #: from "expired" to "three days left" and looks as though the freezer door
    #: rescued it. It did not, and it did not need to -- the two numbers measure
    #: different things. Ninety days is a **quality** figure, and the guideline
    #: table says so for the whole frozen family: at -18 °C food stays safe well
    #: past its date, what degrades is texture and taste. Three days is a
    #: **safety** limit, and it starts now whatever the meat has been through.
    #:
    #: So the arithmetic is not clamped -- a `min()` here would hold the lot
    #: permanently expired and refuse a safe food on quality grounds, which is
    #: how an application teaches a household to ignore it. What is added is the
    #: sentence the numbers cannot carry: this was frozen longer than the
    #: indicative period, it is safe, and it will not be as good.
    quality_warning: str | None = None


class InventoryService:
    def __init__(
        self,
        inventory: InventoryRepository,
        products: ProductRepository,
        locations: LocationRepository,
        units: UnitRegistry,
    ) -> None:
        self._inventory = inventory
        self._products = products
        self._locations = locations
        self._units = units

    async def list_items(self, household_id: uuid.UUID, criteria: InventoryFilter) -> InventoryPage:
        return await self._inventory.list_items(household_id, criteria)

    async def add_item(self, household_id: uuid.UUID, command: AddItemCommand) -> InventoryItem:
        """Add stock, merging into an existing lot when the merge key matches.

        Scanning the same pack twice yields one lot rather than two: eight
        identical yoghurt lines make the inventory unreadable, which is how a
        user stops opening the application at all (``docs/data-model.md`` 7.2).
        """
        product_id = await self._resolve_product(household_id, command)
        if command.location_id is not None and not await self._locations.exists(
            household_id, command.location_id
        ):
            raise LocationNotFoundError(command.location_id)

        unit = await self._require_unit(command.unit)
        canonical = self._to_canonical(command.amount, unit)
        date_kind = _resolve_expiry_kind(command.expires_on, command.expiry_kind)

        draft = LotDraft(
            product_id=product_id,
            storage_location_id=command.location_id,
            quantity_value=_quantize(command.amount),
            quantity_unit_code=unit.code,
            quantity_dimension=unit.dimension,
            quantity_canonical=canonical,
            best_before=command.expires_on,
            date_kind=date_kind,
            opened_at=command.opened_at,
            entry_source=command.source,
            source_receipt_line_id=command.source_receipt_line_id,
        )

        existing = await self._inventory.find_mergeable(household_id, draft)
        if existing is None:
            item = await self._inventory.add(household_id, draft)
        else:
            merged_canonical = existing.quantity_canonical + canonical
            # The displayed pair is re-expressed in the unit already on the lot,
            # not replaced by the incoming one: "1 kg" plus "500 g" reads as
            # "1.5 kg", and the two stored quantities never contradict each other.
            existing_unit = await self._require_unit(existing.quantity_unit_code)
            item = await self._inventory.apply(
                household_id,
                existing.id,
                LotUpdate(
                    quantity_canonical=merged_canonical,
                    quantity_value=_quantize(merged_canonical / existing_unit.factor_to_canonical),
                ),
            )

        await self._inventory.record_movement(
            household_id,
            item.id,
            kind=StockMovementKind.INTAKE,
            delta_canonical=canonical,
            dimension=unit.dimension,
            reason=None,
        )
        return item

    async def update_item(
        self, household_id: uuid.UUID, item_id: uuid.UUID, command: UpdateItemCommand
    ) -> UpdatedItem:
        state = await self._inventory.get_state(household_id, item_id)
        if state is None:
            raise InventoryItemNotFoundError(item_id)

        if isinstance(command.location_id, uuid.UUID) and not await self._locations.exists(
            household_id, command.location_id
        ):
            raise LocationNotFoundError(command.location_id)

        unit_code = command.unit if isinstance(command.unit, str) else state.quantity_unit_code
        unit = await self._require_unit(unit_code)
        amount = command.amount if isinstance(command.amount, Decimal) else state.quantity_value

        quantity_changed = not isinstance(command.amount, UnsetType) or not isinstance(
            command.unit, UnsetType
        )
        canonical = self._to_canonical(amount, unit) if quantity_changed else None

        best_before = (
            state.best_before if isinstance(command.expires_on, UnsetType) else command.expires_on
        )
        requested_kind = (
            command.expiry_kind if isinstance(command.expiry_kind, ExpiryDateKind) else None
        )
        # A stored ``unknown`` is not a preference, it is the absence of one; it
        # must not make "just set the date" fail on the coherence check.
        if requested_kind is None and state.date_kind is not ExpiryDateKind.UNKNOWN:
            requested_kind = state.date_kind
        date_kind = _resolve_expiry_kind(best_before, requested_kind)

        update = LotUpdate(
            quantity_value=_quantize(amount) if quantity_changed else UNSET,
            quantity_unit_code=unit.code if quantity_changed else UNSET,
            quantity_dimension=unit.dimension if quantity_changed else UNSET,
            quantity_canonical=canonical if canonical is not None else UNSET,
            storage_location_id=command.location_id,
            best_before=command.expires_on,
            date_kind=date_kind,
            opened_at=command.opened_at,
        )
        item = await self._inventory.apply(household_id, item_id, update)

        if canonical is not None:
            delta = canonical - state.quantity_canonical
            if delta != 0:
                await self._inventory.record_movement(
                    household_id,
                    item_id,
                    kind=StockMovementKind.ADJUSTMENT,
                    delta_canonical=delta,
                    dimension=unit.dimension,
                    reason="manual correction",
                )

        # An adjustment reports a depletion only if it actually reached zero --
        # not on every ``PATCH``. Today it never can: `_to_canonical` refuses a
        # non-positive amount, so emptying a lot goes through `remove_item`. The
        # test is written against the quantity rather than against that
        # invariant, because the day a zero becomes representable this is the
        # line that must already be right. The reason is `correction` (contract
        # v1.1 section 6, "un ajustement manuel porte le motif correction"), which
        # contract 6bis never proposes a repurchase for -- and that filter lives
        # in `DepletionService`, not here.
        depletion = (
            DepletionSignal(product_id=state.product_id, reason=_ADJUSTMENT_REASON)
            if canonical is not None and canonical == 0
            else None
        )
        return UpdatedItem(item=item, depletion=depletion)

    # -- Home freezing ------------------------------------------------------ #

    async def freeze_item(self, household_id: uuid.UUID, item_id: uuid.UUID) -> FreezingOutcome:
        """Record that the household put this lot in its own freezer.

        Three refusals, and they are tested in this order because that is the
        order of the harm.

        1. **A thawed lot is never refrozen** (ANSES). It is first because it is
           the only one whose failure is measured in a hospital admission, and
           because a lot that was frozen and thawed satisfies the second test too
           -- reading them the other way round would answer "already frozen",
           which is true, useless, and hides the rule that matters.
        2. **An already-frozen lot is refused.** Not tidiness: ``effective_expiry``
           counts from ``frozen_at``, so accepting a second freeze would hand a
           lot frozen ten weeks ago another three months on the strength of a
           duplicate tap. The application would be extending a date.
        3. **An expired lot is refused.** Freezing halts spoilage; it does not
           reverse it, and the arithmetic is what makes the refusal necessary
           rather than pedantic -- the printed date is *voided* by freezing, so
           accepting would replace a use-by crossed last Thursday with ninety
           days.

        What is **not** refused is a family this table has no figure for --
        including the two it advises against freezing at all. See
        :class:`FreezingOutcome`.
        """
        state = await self._require_freezing_state(household_id, item_id)

        if state.thawed_at is not None:
            raise LotAlreadyThawedError(item_id)
        if state.frozen_at is not None:
            raise LotAlreadyFrozenError(item_id)
        today = _today()
        if state.effective_expiry is not None and state.effective_expiry < today:
            raise LotAlreadyExpiredError(item_id)

        destination, move = await self._relocate(household_id, state, StorageKind.FREEZER)
        item = await self._inventory.apply(
            household_id,
            item_id,
            LotUpdate(
                frozen_at=today,
                storage_location_id=UNSET if destination is None else destination.id,
            ),
        )
        return FreezingOutcome(
            item=item,
            move=move,
            moved_to=destination,
            freezing_note=state.freezing_note,
            proposes_a_date=state.home_frozen_days is not None,
        )

    async def thaw_item(self, household_id: uuid.UUID, item_id: uuid.UUID) -> FreezingOutcome:
        """Record that this lot came out of the freezer, and start the short clock.

        Refuses twice. A lot already carrying ``thawed_at`` is out already, and
        writing today over that date would erase how long it has been out --
        which is the number the three-day rule is counted from.

        A lot that was never cold is refused too, and *never cold* is not simply
        ``frozen_at IS NULL``: a product sold frozen was never frozen by this
        household and can still be thawed (migration ``0020``). Both cases are
        covered by asking whether there is anything to thaw at all.

        The lot moves to the household's fridge on the same terms the freeze
        moved it to the freezer, and for a reason beyond symmetry: the three days
        ANSES allows a thawed food are three days *refrigerated*. Filing it there
        is what makes the date the application then displays true of where the
        food actually is.
        """
        state = await self._require_freezing_state(household_id, item_id)

        if state.thawed_at is not None:
            raise LotAlreadyThawedError(item_id)
        if state.frozen_at is None and not state.sold_frozen:
            raise LotNotFrozenError(item_id)

        destination, move = await self._relocate(household_id, state, StorageKind.FRIDGE)
        item = await self._inventory.apply(
            household_id,
            item_id,
            LotUpdate(
                thawed_at=_today(),
                storage_location_id=UNSET if destination is None else destination.id,
            ),
        )
        # Read *before* the write, from the state this method already loaded: it
        # is the date the lot carried while it was still frozen, so a past one
        # means the freezer duration had run out. After the write the same
        # question would answer about the three-day clock instead.
        overdue = state.effective_expiry is not None and state.effective_expiry < _today()

        return FreezingOutcome(
            item=item,
            move=move,
            moved_to=destination,
            freezing_note=state.freezing_note,
            # Always: the thawed branch of the rule is three days for every
            # family, and it needs no figure from the guideline table.
            proposes_a_date=True,
            quality_warning=_OVERDUE_IN_THE_FREEZER if overdue else None,
        )

    async def _require_freezing_state(
        self, household_id: uuid.UUID, item_id: uuid.UUID
    ) -> LotFreezingState:
        state = await self._inventory.get_freezing_state(household_id, item_id)
        if state is None:
            raise InventoryItemNotFoundError(item_id)
        return state

    async def _relocate(
        self, household_id: uuid.UUID, state: LotFreezingState, kind: StorageKind
    ) -> tuple[LocationSummary | None, LocationMove]:
        """Where this lot should now be filed, and why it is or is not moving.

        Never guesses. A household with two freezers gets no move at all, because
        the application was told that a door was opened and not which one; filing
        the chicken in whichever row sorted first would be a fact invented to
        avoid returning "I do not know".

        The last case is the one that is not about judgement at all:
        ``uq_inventory_lot_merge_key`` covers the destination, so the move can be
        *impossible* rather than merely unwise. The state change still happens --
        the food really is in the freezer -- and the lot stays where it is.
        """
        destination = await self._locations.sole_of_kind(household_id, kind)
        if destination is None:
            return None, LocationMove.UNRESOLVED
        if destination.id == state.storage_location_id:
            return None, LocationMove.ALREADY_THERE
        if await self._inventory.location_holds_twin(household_id, state.id, destination.id):
            return None, LocationMove.OCCUPIED
        return destination, LocationMove.MOVED

    async def remove_item(
        self, household_id: uuid.UUID, item_id: uuid.UUID, reason: str
    ) -> DepletionSignal | None:
        """Deplete a lot and say why, in the ledger.

        Not a ``DELETE``: consumption history is what feeds waste statistics and
        what makes a suggestion account for what the household actually eats.

        Returns what ran out, or ``None`` when the lot held nothing to run out of
        -- removing an already-empty lot is bookkeeping, not a depletion, and it
        must not offer to buy anything.
        """
        kind = REMOVAL_KINDS.get(reason)
        if kind is None:
            raise UnsupportedRemovalReasonError(reason)

        state = await self._inventory.get_state(household_id, item_id)
        if state is None:
            raise InventoryItemNotFoundError(item_id)

        emptied = state.quantity_canonical != 0
        if emptied:
            await self._inventory.record_movement(
                household_id,
                item_id,
                kind=kind,
                delta_canonical=-state.quantity_canonical,
                dimension=state.quantity_dimension,
                reason=reason,
            )
        await self._inventory.deplete(household_id, item_id)
        return DepletionSignal(product_id=state.product_id, reason=reason) if emptied else None

    # -- Products ----------------------------------------------------------- #

    async def _resolve_product(self, household_id: uuid.UUID, command: AddItemCommand) -> uuid.UUID:
        """Return the product to stock, creating a private one when asked to.

        Creating it here rather than in the handler keeps the whole operation in
        the request's single transaction: a product created for a lot that then
        fails validation must not survive as an orphan in the catalogue.
        """
        if command.new_product is not None:
            if command.new_product.default_unit_code is not None:
                await self._require_unit(command.new_product.default_unit_code)
            created = await self._products.create_private(
                household_id,
                ProductDraft(
                    name=command.new_product.name.strip(),
                    brand=(
                        command.new_product.brand.strip() if command.new_product.brand else None
                    ),
                    gtin=(
                        None
                        if command.new_product.gtin is None
                        else normalize_gtin(command.new_product.gtin)
                    ),
                    default_unit_code=command.new_product.default_unit_code,
                    source=command.new_product.source,
                ),
            )
            return created.id

        if command.product_id is None:
            raise MissingProductReferenceError()
        if await self._products.get_visible(household_id, command.product_id) is None:
            raise ProductNotFoundError(command.product_id)
        return command.product_id

    # -- Quantities --------------------------------------------------------- #

    async def _require_unit(self, code: str) -> UnitInfo:
        unit = await self._units.get(code)
        if unit is None:
            raise UnknownUnitError(code)
        return unit

    def _to_canonical(self, amount: Decimal, unit: UnitInfo) -> Decimal:
        if amount <= 0:
            raise InvalidQuantityError("a quantity must be strictly positive")
        canonical = _quantize(amount * unit.factor_to_canonical)
        if canonical <= 0:
            # 0.0001 g rounds to zero at three decimals, and a lot at zero that
            # is not depleted violates ck_inventory_lot_depleted_consistency.
            raise InvalidQuantityError(
                f"{amount} {unit.code} rounds to zero {unit.dimension.value} at three decimals"
            )
        return canonical


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def _today() -> date:
    """The server's calendar date, as ``frozen_at`` and ``thawed_at`` are dates.

    Not taken from the client. These two columns are read by
    :func:`~chaudron.domain.shelf_life.effective_expiry_sql` as the *start* of a
    duration, so a caller able to choose them is a caller able to choose the
    expiry date -- and a ``frozen_at`` in the future would buy three months plus
    the lie. The same reason keeps them off ``PATCH``: the refusals above have
    nowhere to run there.
    """
    return datetime.now(UTC).date()


def _resolve_expiry_kind(
    expires_on: date | None, requested: ExpiryDateKind | None
) -> ExpiryDateKind:
    """Keep the date and its kind coherent, as ``ck_inventory_lot_date_kind_coherent`` demands.

    A date with an explicit ``unknown`` kind is a contradiction and is refused
    rather than silently dropped -- losing a use-by date is a safety matter, not
    a formatting one. A date with no kind at all defaults to ``best_before``:
    guessing ``use_by`` would raise an alarm on a bag of pasta, and the wording
    of every notification depends on the distinction.
    """
    if expires_on is None:
        return ExpiryDateKind.UNKNOWN
    if requested is None:
        return ExpiryDateKind.BEST_BEFORE
    if requested is ExpiryDateKind.UNKNOWN:
        raise ExpiryDateInconsistentError("an expiry date cannot have the kind 'unknown'")
    return requested
