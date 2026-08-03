# Scoping skeleton: tables, constraints and indexes only -- not wired to anything yet.
"""Pantry data model skeleton.

Companion document: ``docs/data-model.md`` (French, internal design note). Every
non-obvious decision taken here is argued there; comments below only record the
*why* that a reader of the schema cannot reconstruct on their own.

Deliberately absent from this file:

* ORM ``relationship()`` declarations. Loading strategy is a repository concern,
  and defaults set here would silently leak into every query in the codebase.
* Business logic. Unit conversion, FEFO allocation and lot merging live in the
  application layer; the schema only makes their invariants representable.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Without an explicit convention PostgreSQL names constraints itself, and Alembic
# then emits DROP statements against names it cannot reproduce on another database.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# Reusable type instances. Money is 2 decimals, quantities 3: never float, because a
# stock that drifts to -0.0000001 g leaves a ghost row no user can delete.
MONEY = Numeric(12, 2)
QUANTITY = Numeric(12, 3)
CANONICAL_QUANTITY = Numeric(14, 3)


class Base(DeclarativeBase):
    """Declarative base.

    ``type_annotation_map`` makes ``datetime`` mean ``timestamptz`` everywhere: a
    naive timestamp is a bug waiting for the first user who travels.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        Decimal: QUANTITY,
        dict[str, Any]: JSONB,
    }


# --------------------------------------------------------------------------- #
# Required PostgreSQL extensions
# --------------------------------------------------------------------------- #

# `ix_product_name_trgm` is a GIN index using `gin_trgm_ops`, which does not
# exist until `pg_trgm` is installed. Without this listener, `create_all` fails
# on a stock PostgreSQL image with:
#
#   operator class "gin_trgm_ops" does not exist for access method "gin"
#
# Emitting the extension from the metadata keeps the schema self-sufficient:
# tests, a throwaway container and the first Alembic migration all get it
# without anyone having to remember a manual step. Requires the connecting role
# to be a superuser or to hold CREATE on the database — true for the standard
# postgres image and for a database the deploy owns.
REQUIRED_EXTENSIONS: Final[tuple[str, ...]] = ("pg_trgm",)


@event.listens_for(Base.metadata, "before_create")
def _install_required_extensions(
    target: MetaData,
    connection: Connection,
    **kw: Any,
) -> None:
    """Install the extensions the schema depends on, before any table is built.

    A typed listener rather than ``DDL(...)``: the latter is unannotated in the
    SQLAlchemy stubs and would cost a ``type: ignore`` under strict mypy.
    """
    for extension in REQUIRED_EXTENSIONS:
        # Not an injection vector: REQUIRED_EXTENSIONS is a module constant and
        # never contains user input. Identifier quoting is applied regardless.
        connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


def pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """Native PostgreSQL enum storing member *values*, not Python member names."""
    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class MembershipRole(enum.StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


class QuantityDimension(enum.StrEnum):
    """Canonical unit per dimension: gram, millilitre, piece."""

    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


class StorageKind(enum.StrEnum):
    """Behaviour depends on the kind, not the label: a freezer suspends expiry."""

    FRIDGE = "fridge"
    FREEZER = "freezer"
    PANTRY = "pantry"
    CELLAR = "cellar"
    OTHER = "other"


class ProductSource(enum.StrEnum):
    OPEN_FOOD_FACTS = "open_food_facts"
    MANUAL = "manual"
    RECEIPT_IMPORT = "receipt_import"


class ExpiryDateKind(enum.StrEnum):
    """``use_by`` is a safety limit, ``best_before`` a quality one.

    Conflating them yields either anxious alerts on dry pasta or silence on minced
    meat; the wording of every notification depends on this distinction.
    """

    USE_BY = "use_by"
    BEST_BEFORE = "best_before"
    UNKNOWN = "unknown"


class StockEntrySource(enum.StrEnum):
    MANUAL = "manual"
    BARCODE_SCAN = "barcode_scan"
    RECEIPT_IMPORT = "receipt_import"
    SHOPPING_LIST = "shopping_list"
    RECIPE_LEFTOVER = "recipe_leftover"


class StockMovementKind(enum.StrEnum):
    INTAKE = "intake"
    CONSUMPTION = "consumption"
    WASTE = "waste"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"


class ShoppingItemOrigin(enum.StrEnum):
    MANUAL = "manual"
    LOW_STOCK = "low_stock"
    RECIPE = "recipe"


class ReceiptStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ReceiptLineMatchStatus(enum.StrEnum):
    PENDING = "pending"
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    IGNORED = "ignored"


class RecipeStatus(enum.StrEnum):
    GENERATED = "generated"
    SAVED = "saved"
    COOKED = "cooked"
    DISCARDED = "discarded"


class IngredientAvailability(enum.StrEnum):
    IN_STOCK = "in_stock"
    PARTIAL = "partial"
    MISSING = "missing"
    UNKNOWN = "unknown"


class LlmProviderMode(enum.StrEnum):
    """Who provides -- and pays for -- the model access.

    ``instance_owner`` is the only mode whose cost lands on the operator; the other
    two are billed to the household or cost nothing (self-hosted).
    """

    BYOK = "byok"
    OLLAMA = "ollama"
    INSTANCE_OWNER = "instance_owner"


class LlmPurpose(enum.StrEnum):
    """Receipt parsing needs vision, recipe generation does not.

    Hence a binding per purpose rather than a single provider per household.
    """

    RECIPE_GENERATION = "recipe_generation"
    RECEIPT_PARSING = "receipt_parsing"


class LlmConfigStatus(enum.StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    INVALID_CREDENTIALS = "invalid_credentials"
    DISABLED = "disabled"


# --------------------------------------------------------------------------- #
# Mixins
# --------------------------------------------------------------------------- #


class UuidPkMixin:
    """UUIDv7 primary key, generated application-side.

    Three reasons, in order: the PWA must be able to create a row *offline* and sync
    it without renumbering; a visible sequence would leak every household's activity
    volume once the instance is public; and unlike v4, v7 is time-ordered, so it does
    not destroy B-tree locality.
    """

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class HouseholdScopedMixin:
    """Tenant key, carried even where a join could derive it.

    Denormalised on purpose: it keeps every index local, makes future RLS policies a
    local predicate instead of a join, and lets any table be audited per tenant on
    its own. ``CASCADE`` because deleting a household is a GDPR erasure and must be
    total and atomic.
    """

    @declared_attr
    def household_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(ForeignKey("household.id", ondelete="CASCADE"), nullable=False)


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


class Household(UuidPkMixin, TimestampMixin, Base):
    """The unit of ownership: stock, lists and receipts belong here, never to a person."""

    __tablename__ = "household"

    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    default_currency: Mapped[str] = mapped_column(String(3), server_default=text("'CHF'"))
    # Locked by default: only this household may use the instance-wide API key.
    is_instance_owner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("default_currency ~ '^[A-Z]{3}$'", name="currency_format"),
        # At most one instance owner, enforced by the database rather than by an
        # operating convention: a mistake here makes the operator pay for a stranger.
        Index(
            "uq_household_instance_owner",
            text("(true)"),
            unique=True,
            postgresql_where=text("is_instance_owner"),
        ),
    )


class UserAccount(UuidPkMixin, TimestampMixin, Base):
    """A person's identity, deliberately independent of any household.

    Putting the household here would forbid dual membership (family home *and*
    flatshare) and force duplicating accounts -- hence passwords, hence half-working
    resets -- the day someone moves.
    """

    __tablename__ = "user_account"

    email: Mapped[str] = mapped_column(String(320))
    # Nullable: an account created through an external identity provider has none.
    password_hash: Mapped[str | None] = mapped_column(Text())
    display_name: Mapped[str] = mapped_column(String(120))
    last_login_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]

    __table_args__ = (
        # Serves the login lookup *and* guarantees uniqueness. A plain unique on
        # `email` would let "Kevin@" and "kevin@" both through.
        Index("uq_user_account_email_lower", text("lower(email)"), unique=True),
    )


class HouseholdMember(Base):
    """Membership of an account in a household, with its role.

    Composite primary key: nobody references a membership by id, and the key doubles
    as the index for the access check run on every single request.
    """

    __tablename__ = "household_member"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[MembershipRole] = mapped_column(pg_enum(MembershipRole, "membership_role"))
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # "Which households does this user belong to?" -- the primary key has the
        # wrong prefix for that question.
        Index("ix_household_member_user_id", "user_id"),
    )


# --------------------------------------------------------------------------- #
# Reference data (global, not tenant-scoped)
# --------------------------------------------------------------------------- #


class Unit(Base):
    """Measurement units and their factor to the canonical unit of their dimension.

    A reference table rather than an enum: an enum cannot carry the conversion
    factor, and adding "tablespoon" must not be a type migration. Seeded by an
    Alembic data migration so the reference stays versioned and reproducible.
    """

    __tablename__ = "unit"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    dimension: Mapped[QuantityDimension] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    factor_to_canonical: Mapped[Decimal] = mapped_column(Numeric(18, 9))
    symbol: Mapped[str] = mapped_column(String(16))
    is_canonical: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        # Redundant with the primary key on its own, but it is the target of the
        # composite foreign keys that make ('ml', 'mass') unrepresentable.
        UniqueConstraint("code", "dimension", name="uq_unit_code_dimension"),
        CheckConstraint("factor_to_canonical > 0", name="factor_positive"),
        Index(
            "uq_unit_canonical_per_dimension",
            "dimension",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
    )


class LlmProvider(Base):
    """Supported AI providers and what they require.

    A table, not an enum: adding a provider must be an additive migration, and a
    provider must be switchable off (``is_enabled``) without deleting the household
    configurations that point at it.
    """

    __tablename__ = "llm_provider"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    requires_api_key: Mapped[bool] = mapped_column(Boolean)
    requires_base_url: Mapped[bool] = mapped_column(Boolean)
    default_model: Mapped[str | None] = mapped_column(String(120))
    # Defaults only: for Ollama the real capability depends on the pulled model, so
    # the effective values live on the household's configuration row.
    default_supports_vision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    default_supports_structured_output: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    default_max_context_tokens: Mapped[int | None] = mapped_column(Integer)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))


class Product(Base):
    """Catalogue entry: *what* an item is, independently of how much is owned."""

    __tablename__ = "product"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    # NULL means the public, shared catalogue: scanning flour must not create one row
    # per household. NOT NULL isolates "the carrots from the market", which have no
    # barcode and no business being public. Only place in the schema where ownership
    # is optional -- and therefore the only cross-tenant reference the database
    # cannot guard with a composite foreign key (see docs/data-model.md, 5.2).
    household_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE")
    )
    gtin: Mapped[str | None] = mapped_column(String(14))
    name: Mapped[str] = mapped_column(Text())
    brand: Mapped[str | None] = mapped_column(Text())
    category_tag: Mapped[str | None] = mapped_column(Text())
    image_url: Mapped[str | None] = mapped_column(Text())

    net_content_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    net_content_unit_code: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("unit.code", ondelete="RESTRICT")
    )
    # The two scalars that allow crossing dimensions at all. Absent them, "2 onions"
    # and "300 g of onions" simply stay two lines -- which beats inventing a total.
    unit_weight_g: Mapped[Decimal | None] = mapped_column(QUANTITY)
    density_g_per_ml: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    default_shelf_life_days: Mapped[int | None] = mapped_column(SmallInteger)

    source: Mapped[ProductSource] = mapped_column(pg_enum(ProductSource, "product_source"))
    # Raw Open Food Facts response: their schema changes without notice, and keeping
    # it lets us extract a field we had not anticipated without rescanning anything.
    off_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    off_synced_at: Mapped[datetime | None]

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("gtin ~ '^[0-9]{8,14}$'", name="gtin_digits"),
        CheckConstraint(
            "(net_content_value IS NULL) = (net_content_unit_code IS NULL)",
            name="net_content_pair",
        ),
        # One barcode, one public product. Also serves the scan lookup.
        Index(
            "uq_product_gtin_global",
            "gtin",
            unique=True,
            postgresql_where=text("household_id IS NULL AND gtin IS NOT NULL"),
        ),
        Index(
            "uq_product_household_gtin",
            "household_id",
            "gtin",
            unique=True,
            postgresql_where=text("household_id IS NOT NULL AND gtin IS NOT NULL"),
        ),
        # Fuzzy matching of receipt labels against the catalogue. Without it a
        # 30-line receipt is 30 sequential scans. Requires the pg_trgm extension.
        Index(
            "ix_product_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # "My own products" in the manual entry screen; partial because most rows are
        # public and have nothing to do in this index.
        Index(
            "ix_product_household_id",
            "household_id",
            postgresql_where=text("household_id IS NOT NULL"),
        ),
    )


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #


class StorageLocation(UuidPkMixin, HouseholdScopedMixin, Base):
    """Where a lot physically sits. Per-household, because "garage fridge" exists."""

    __tablename__ = "storage_location"

    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[StorageKind] = mapped_column(pg_enum(StorageKind, "storage_kind"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Archived rather than deleted: depleted lots still point here.
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        # Target of the composite foreign keys that make it impossible to store a lot
        # in another household's fridge.
        UniqueConstraint("household_id", "id", name="uq_storage_location_household_id"),
        # Two active "Fridge" is a typo; one active and one archived is history.
        Index(
            "uq_storage_location_name",
            "household_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )


class InventoryLot(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """A physical batch: *this* bag of flour, bought then, expiring then, stored there.

    "Stock item" and "lot" are two views of the same thing and one table carries
    both: "I have 1.5 kg of flour" is a SUM over active lots, not a stored row. A
    denormalised per-product aggregate would introduce a second truth to reconcile;
    it can come later as a materialised view, driven by a profile rather than a hunch.
    """

    __tablename__ = "inventory_lot"

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product.id", ondelete="RESTRICT"))
    storage_location_id: Mapped[uuid.UUID | None]

    # Dual storage. The display pair is what the user typed and is never recomputed;
    # the canonical pair is what we sum, compare and index. Conversion happens once,
    # on write, in the application layer -- never on read (the busiest screen would
    # pay a join for nothing) and never in a trigger (invisible, untestable).
    quantity_value: Mapped[Decimal] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    quantity_canonical: Mapped[Decimal] = mapped_column(CANONICAL_QUANTITY)
    initial_quantity_canonical: Mapped[Decimal] = mapped_column(CANONICAL_QUANTITY)

    # Nullable on purpose: blocking a scan on a date field loses the user in two
    # weeks. A dateless lot simply never appears in expiry alerts.
    best_before: Mapped[date | None]
    date_kind: Mapped[ExpiryDateKind] = mapped_column(pg_enum(ExpiryDateKind, "expiry_date_kind"))
    # Feeds the "eat within 3 days of opening" rule. The effective date is computed,
    # never stored: opening a jar would invalidate the stored value.
    opened_at: Mapped[date | None]

    acquired_on: Mapped[date | None]
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))

    entry_source: Mapped[StockEntrySource] = mapped_column(
        pg_enum(StockEntrySource, "stock_entry_source")
    )
    source_receipt_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("receipt_line.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text())
    # Not a deletion: a business state, and the input of every consumption statistic.
    depleted_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_inventory_lot_household_id"),
        ForeignKeyConstraint(
            ["household_id", "storage_location_id"],
            ["storage_location.household_id", "storage_location.id"],
            ondelete="RESTRICT",
        ),
        # Makes ('ml', 'mass') unrepresentable: the denormalised dimension cannot
        # contradict the unit it was derived from.
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("quantity_value > 0 AND quantity_canonical >= 0", name="quantity_positive"),
        # An active lot at zero is an inconsistency that silently falsifies the stock.
        CheckConstraint(
            "depleted_at IS NOT NULL OR quantity_canonical > 0",
            name="depleted_consistency",
        ),
        CheckConstraint("(unit_price IS NULL) = (currency IS NULL)", name="price_pair"),
        CheckConstraint("date_kind <> 'unknown' OR best_before IS NULL", name="date_kind_coherent"),
        # THE merge key. Scanning the same pack twice yields one lot, not two, and the
        # upsert is atomic so two phones scanning at once cannot race.
        # NULLS NOT DISTINCT (PostgreSQL 15+) is mandatory: without it two dateless
        # lots never conflict and the stock fragments on every scan.
        Index(
            "uq_inventory_lot_merge_key",
            "household_id",
            "product_id",
            "storage_location_id",
            "best_before",
            "quantity_dimension",
            unique=True,
            postgresql_where=text("depleted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        # Main screen: "show me my fridge". Busiest query of the application.
        Index(
            "ix_inventory_lot_location_active",
            "household_id",
            "storage_location_id",
            postgresql_where=text("depleted_at IS NULL"),
        ),
        # "Expiring soon" widget and the daily notification job.
        Index(
            "ix_inventory_lot_expiry_active",
            "household_id",
            "best_before",
            postgresql_where=text("depleted_at IS NULL AND best_before IS NOT NULL"),
        ),
        # "Do I have flour?" -- once per ingredient during recipe generation.
        Index(
            "ix_inventory_lot_product_active",
            "household_id",
            "product_id",
            postgresql_where=text("depleted_at IS NULL"),
        ),
        # "Delete this receipt and everything it created."
        Index("ix_inventory_lot_source_receipt_line", "source_receipt_line_id"),
    )


class StockMovement(UuidPkMixin, HouseholdScopedMixin, Base):
    """Append-only ledger of every quantity change.

    Justified by three product-level needs an ``UPDATE ... SET quantity = quantity - x``
    destroys: measuring waste, feeding suggestions with real consumption history, and
    offering undo on a one-handed tap in front of an open fridge.

    Invariant: ``InventoryLot.quantity_canonical`` is a cache of ``SUM(delta_canonical)``
    maintained in the same transaction. The ledger is the historical truth, the column
    the read truth. Drift is possible and is handled by a reconciliation job that
    alerts instead of silently correcting.
    """

    __tablename__ = "stock_movement"

    inventory_lot_id: Mapped[uuid.UUID]
    kind: Mapped[StockMovementKind] = mapped_column(
        pg_enum(StockMovementKind, "stock_movement_kind")
    )
    delta_canonical: Mapped[Decimal] = mapped_column(CANONICAL_QUANTITY)
    quantity_dimension: Mapped[QuantityDimension] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    recipe_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipe_suggestion.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(Text())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "inventory_lot_id"],
            ["inventory_lot.household_id", "inventory_lot.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("delta_canonical <> 0", name="delta_nonzero"),
        # Lot history display and reconciliation.
        Index(
            "ix_stock_movement_lot",
            "inventory_lot_id",
            text("occurred_at DESC"),
        ),
        # "Recent activity" screen and monthly waste statistics.
        Index(
            "ix_stock_movement_household_occurred",
            "household_id",
            text("occurred_at DESC"),
        ),
    )


# --------------------------------------------------------------------------- #
# Shopping
# --------------------------------------------------------------------------- #


class ShoppingList(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """What needs buying. Several coexist; exactly one is the default."""

    __tablename__ = "shopping_list"

    name: Mapped[str] = mapped_column(String(120))
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_shopping_list_household_id"),
        # Guaranteed by the database, not by an application convention that
        # concurrency will eventually work around.
        Index(
            "uq_shopping_list_default",
            "household_id",
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
    )


class ShoppingListItem(UuidPkMixin, HouseholdScopedMixin, Base):
    """A line on a list. Either a catalogue product or free text -- often free text.

    Forcing a catalogue product at add time turns a two-second gesture into a form;
    "some bread" is a legitimate and very common entry.
    """

    __tablename__ = "shopping_list_item"

    shopping_list_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    label: Mapped[str | None] = mapped_column(Text())

    quantity_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str | None] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension | None] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )

    origin: Mapped[ShoppingItemOrigin] = mapped_column(
        pg_enum(ShoppingItemOrigin, "shopping_item_origin")
    )
    origin_recipe_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipe_suggestion.id", ondelete="SET NULL")
    )
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    checked_at: Mapped[datetime | None]
    checked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "shopping_list_id"],
            ["shopping_list.household_id", "shopping_list.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("product_id IS NOT NULL OR label IS NOT NULL", name="target_present"),
        CheckConstraint(
            "(quantity_value IS NULL) = (quantity_unit_code IS NULL)"
            " AND (quantity_value IS NULL) = (quantity_dimension IS NULL)",
            name="quantity_triplet",
        ),
        # The only hot query: the list as currently displayed. Checked items stay for
        # history but leave the index.
        Index(
            "ix_shopping_list_item_pending",
            "household_id",
            "shopping_list_id",
            "sort_order",
            postgresql_where=text("checked_at IS NULL"),
        ),
        # "Is this product already on a list?" -- asked on every automatic add.
        Index(
            "ix_shopping_list_item_product",
            "product_id",
            postgresql_where=text("product_id IS NOT NULL"),
        ),
    )


# --------------------------------------------------------------------------- #
# Receipt import
# --------------------------------------------------------------------------- #


class Receipt(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """A photographed till receipt and its machine interpretation, before validation."""

    __tablename__ = "receipt"

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    # Must be namespaced by household_id in object storage too: a correctly filtered
    # row protects nothing if the bucket is enumerable.
    image_object_key: Mapped[str] = mapped_column(Text())
    image_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[ReceiptStatus] = mapped_column(pg_enum(ReceiptStatus, "receipt_status"))

    merchant_name: Mapped[str | None] = mapped_column(Text())
    purchased_at: Mapped[datetime | None]
    total_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))

    # Provenance of the parse. Copied here rather than only referenced, because a
    # configuration can be edited or deleted while the artefact must stay
    # describable: provider_mode is what separates "our default model misread it"
    # from "a local vision-less model was asked to read an image".
    provider_mode: Mapped[LlmProviderMode | None] = mapped_column(
        pg_enum(LlmProviderMode, "llm_provider_mode")
    )
    provider_code: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    llm_provider_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_provider_config.id", ondelete="SET NULL")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # Micro-units of currency: a single call often costs less than a cent, and
    # rounding to two decimals makes cost tracking useless as soon as it is summed.
    cost_micro: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Kept: when a user reports an invented line, the raw output confronted with the
    # prompt version is the only debuggable artefact of a non-deterministic pipeline.
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parse_error: Mapped[str | None] = mapped_column(Text())
    parsed_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_receipt_household_id"),
        # Blocks the very common double import on mobile (photo re-sent after a
        # perceived timeout). A constraint rather than a check, because the two
        # uploads are concurrent.
        UniqueConstraint("household_id", "image_sha256", name="uq_receipt_household_sha256"),
        Index(
            "ix_receipt_household_purchased",
            "household_id",
            text("purchased_at DESC NULLS LAST"),
        ),
        # Worker queue. Deliberately not prefixed by household_id: it is a
        # cross-tenant queue, and the partial predicate keeps it a few rows long.
        Index(
            "ix_receipt_pending",
            "created_at",
            postgresql_where=text("status IN ('uploaded', 'parsing')"),
        ),
        # Operator billing: the only calls the operator actually pays for.
        Index(
            "ix_receipt_operator_cost",
            "created_at",
            postgresql_where=text("provider_mode = 'instance_owner'"),
        ),
    )


class ReceiptLine(UuidPkMixin, HouseholdScopedMixin, Base):
    """One parsed line of a receipt, and its (fuzzy) match to a catalogue product.

    The link to stock exists in one direction only
    (``InventoryLot.source_receipt_line_id``): reciprocal foreign keys would create a
    cycle and two truths to keep in sync.
    """

    __tablename__ = "receipt_line"

    receipt_id: Mapped[uuid.UUID]
    line_no: Mapped[int] = mapped_column(SmallInteger)
    # Kept even after matching: it is the corpus that will improve matching, and the
    # only evidence of what was printed when a user disputes the result.
    raw_label: Mapped[str] = mapped_column(Text())

    quantity_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str | None] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension | None] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    total_price: Mapped[Decimal | None] = mapped_column(MONEY)

    matched_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    match_status: Mapped[ReceiptLineMatchStatus] = mapped_column(
        pg_enum(ReceiptLineMatchStatus, "receipt_line_match_status")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "receipt_id"],
            ["receipt.household_id", "receipt.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        # Receipt order is meaningful, and a re-parse must not duplicate lines.
        UniqueConstraint("receipt_id", "line_no", name="uq_receipt_line_no"),
        CheckConstraint("match_confidence BETWEEN 0 AND 1", name="confidence_range"),
        # "Lines to review" screen -- the main friction point of the receipt flow.
        Index(
            "ix_receipt_line_pending",
            "household_id",
            "created_at",
            postgresql_where=text("match_status IN ('pending', 'suggested')"),
        ),
    )


# --------------------------------------------------------------------------- #
# LLM access (per household -- there is no application-wide API key)
# --------------------------------------------------------------------------- #


class LlmProviderConfig(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """One household's access to a model: mode, endpoint, model, encrypted key.

    Credential and assignment are separate entities on purpose (see
    ``LlmPurposeBinding``): an API key must exist in exactly one place, or the
    household that uses it for two purposes has to rotate it twice -- and the day it
    rotates only one, half the application fails with ``invalid_credentials`` for no
    visible reason.
    """

    __tablename__ = "llm_provider_config"

    label: Mapped[str] = mapped_column(String(80))
    mode: Mapped[LlmProviderMode] = mapped_column(pg_enum(LlmProviderMode, "llm_provider_mode"))
    provider_code: Mapped[str] = mapped_column(
        String(40), ForeignKey("llm_provider.code", ondelete="RESTRICT")
    )
    model: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str | None] = mapped_column(Text())

    # SECRET. Never expose this column through the API, in any schema, in any form,
    # decrypted or not. `deferred=True` is the enforcement, not the documentation: a
    # plain `select(LlmProviderConfig)` does NOT load it, so reading the ciphertext
    # requires an explicit `undefer()` -- a deliberate, greppable, reviewable gesture.
    # Contents: AES-256-GCM, key from the environment (podman secret) and NEVER from
    # the database, with (household_id, id) as additional authenticated data so a
    # ciphertext copied onto another row fails to decrypt.
    api_key_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        deferred=True,
        comment=(
            "SECRET: AES-256-GCM ciphertext. Encryption key comes from the "
            "environment, never from this database. Never returned by the API; "
            "users only ever see api_key_last4."
        ),
    )
    # The only part a user ever sees again: enough to recognise which of their keys
    # is installed, useless to anyone else.
    api_key_last4: Mapped[str | None] = mapped_column(String(4))
    # Without a key version, rotating the encryption key means re-encrypting
    # everything at once, with the application stopped.
    api_key_encryption_key_id: Mapped[str | None] = mapped_column(String(32))
    api_key_set_at: Mapped[datetime | None]

    # Effective capabilities, seeded from LlmProvider defaults then corrected by a
    # connection probe. No reference table can cover Ollama, where the same endpoint
    # may serve a vision model or not depending on a free-text model name -- only
    # asking the endpoint can tell. This is what lets the UI disable receipt import
    # instead of failing at runtime.
    supports_vision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    supports_structured_output: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    max_context_tokens: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[LlmConfigStatus] = mapped_column(pg_enum(LlmConfigStatus, "llm_config_status"))
    last_verified_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text())
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_llm_provider_config_household_id"),
        Index(
            "uq_llm_provider_config_label",
            "household_id",
            text("lower(label)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        # A ciphertext without a key id is undecryptable after the first rotation;
        # a last4 without a ciphertext shows the user a key that does not exist.
        CheckConstraint(
            "(api_key_ciphertext IS NULL) = (api_key_last4 IS NULL)"
            " AND (api_key_ciphertext IS NULL)"
            " = (api_key_encryption_key_id IS NULL)",
            name="secret_triplet",
        ),
        # Makes "the instance key is never copied into the database" checkable by the
        # database itself, rather than written in a document nobody will re-read.
        CheckConstraint(
            "(mode = 'byok' AND api_key_ciphertext IS NOT NULL)"
            " OR (mode = 'ollama' AND api_key_ciphertext IS NULL"
            " AND base_url IS NOT NULL)"
            " OR (mode = 'instance_owner' AND api_key_ciphertext IS NULL)",
            name="mode_requirements",
        ),
        CheckConstraint("char_length(api_key_last4) = 4", name="last4_length"),
        Index(
            "ix_llm_provider_config_household_active",
            "household_id",
            postgresql_where=text("archived_at IS NULL"),
        ),
        # "Your key no longer works" banner, shown on every page: must be free.
        Index(
            "ix_llm_provider_config_invalid",
            "household_id",
            postgresql_where=text("status = 'invalid_credentials'"),
        ),
    )


class LlmPurposeBinding(Base):
    """Which configuration serves which purpose, for one household.

    Composite primary key ``(household_id, purpose)``: at most one active
    configuration per purpose, with no "is_active" flag to demote and therefore no
    window where two are active at once.

    The composite foreign key below is not hygiene, it is a security control: without
    it a guessed identifier would be enough to spend another household's API credit.
    """

    __tablename__ = "llm_purpose_binding"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[LlmPurpose] = mapped_column(
        pg_enum(LlmPurpose, "llm_purpose"), primary_key=True
    )
    llm_provider_config_id: Mapped[uuid.UUID]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "llm_provider_config_id"],
            ["llm_provider_config.household_id", "llm_provider_config.id"],
            ondelete="CASCADE",
        ),
    )


# --------------------------------------------------------------------------- #
# Recipe generation
# --------------------------------------------------------------------------- #


class RecipeSuggestion(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """A model-generated recipe, plus the full trace of how it was produced."""

    __tablename__ = "recipe_suggestion"

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(Text())
    summary: Mapped[str | None] = mapped_column(Text())
    servings: Mapped[int | None] = mapped_column(SmallInteger)
    prep_minutes: Mapped[int | None] = mapped_column(SmallInteger)
    cook_minutes: Mapped[int | None] = mapped_column(SmallInteger)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # What we sent to the model. Without it, "why did it suggest that when I had no
    # eggs?" is unanswerable. Also the most sensitive row in the database: it is a
    # complete inventory of a home, and falls under the same retention policy as
    # receipt images.
    stock_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)

    # Copied from the configuration rather than only referenced: the configuration
    # can change or be deleted, while a three-month-old suggestion must keep saying
    # what produced it. A poor suggestion from a small local model is a support case;
    # the same from the instance default is a product regression. Two different
    # queues, and nothing else in the schema separates them.
    provider_mode: Mapped[LlmProviderMode] = mapped_column(
        pg_enum(LlmProviderMode, "llm_provider_mode")
    )
    provider_code: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    llm_provider_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_provider_config.id", ondelete="SET NULL")
    )

    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Only aggregable for the operator when provider_mode = 'instance_owner'; in byok
    # it is the user's own spend at their own pricing, and in ollama it is zero.
    cost_micro: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Detects truncation, which otherwise looks like a badly written recipe.
    finish_reason: Mapped[str | None] = mapped_column(String(40))

    status: Mapped[RecipeStatus] = mapped_column(pg_enum(RecipeStatus, "recipe_status"))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    cooked_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_recipe_suggestion_household_id"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0"
            " AND cached_input_tokens >= 0 AND cost_micro >= 0",
            name="tokens_nonneg",
        ),
        Index(
            "ix_recipe_suggestion_household_created",
            "household_id",
            text("created_at DESC"),
        ),
        # Operator cost report. The partial predicate is not an optimisation, it is
        # the business definition of the index: byok and ollama calls are paid by the
        # household and have no place in an operator invoice.
        Index(
            "ix_recipe_suggestion_operator_cost",
            "created_at",
            "model",
            postgresql_where=text("provider_mode = 'instance_owner'"),
        ),
    )


class RecipeSuggestionIngredient(UuidPkMixin, HouseholdScopedMixin, Base):
    """The resolved projection of a recipe's ingredients.

    Exists because two flows need it: "add what is missing to the shopping list" and
    "I cooked this, deduct it from stock". Both require a fuzzy label-to-product
    resolution we do not want to redo on every render. The raw ingredients also stay
    in ``RecipeSuggestion.payload``; this table is the usable projection, not the
    source of truth.
    """

    __tablename__ = "recipe_suggestion_ingredient"

    recipe_suggestion_id: Mapped[uuid.UUID]
    position: Mapped[int] = mapped_column(SmallInteger)
    raw_label: Mapped[str] = mapped_column(Text())

    quantity_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str | None] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension | None] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )

    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    # Snapshot taken at generation time, not a live view of the stock.
    availability: Mapped[IngredientAvailability] = mapped_column(
        pg_enum(IngredientAvailability, "ingredient_availability")
    )
    is_optional: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "recipe_suggestion_id"],
            ["recipe_suggestion.household_id", "recipe_suggestion.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "recipe_suggestion_id",
            "position",
            name="uq_recipe_suggestion_ingredient_position",
        ),
        # "Add the missing ingredients to the list" in a single query.
        Index(
            "ix_recipe_ingredient_missing",
            "household_id",
            "product_id",
            postgresql_where=text("availability IN ('missing', 'partial')"),
        ),
    )
