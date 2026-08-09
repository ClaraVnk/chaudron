"""Data-subject rights over a household: access, portability and erasure.

GDPR articles 15, 20 and 17, and the one open item revision ``0016`` did not
touch (``docs/security-pentest-2026-08-04.md``, O-01). ``docs/security-model.md``
section 8.5 said of the first two "To be built. Nothing exists.", and of the third
that the ``CASCADE`` was ready and nothing could trigger it.

Five decisions are taken here rather than described, because each one is a place
where a plausible implementation would be wrong.

**The export is generated from the schema, not from a hand-written list.** Every
table carrying ``household_id`` is walked, every column of it is exported, and the
only exceptions are the five in :data:`WITHHELD_COLUMNS` -- each a credential,
each with the reason attached. A hand-written projection would silently stop
disclosing the column somebody adds next month, and under-disclosure is an
article 15 failure in exactly the way over-disclosure is a security one. The
inverse risk is real too, which is why the deny-list is asserted against the model
in ``tests/api/test_privacy.py``: a new secret column that nobody denies fails the
build rather than appearing in an export.

**The Open Food Facts catalogue is not the household's to hand over or to
erase.** ``product.household_id IS NULL`` is shared reference data with no tenant
(``docs/data-model.md``, ADR-0008), and the security model's own table of legal
bases says so: *"Not applicable. No personal data: it is a shared external
reference."* So the private products are exported as the household's rows, and
the public ones a household's rows *point at* are exported separately, in a
reduced form, labelled as reference data. Erasure removes the first and leaves
the second: the ``CASCADE`` on a nullable foreign key already behaves that way,
and this module does not second-guess it.

**Nothing is anonymised to make the numbers balance.** Section 8.4 proposes
keeping ``receipt_line.raw_label`` long-term "after anonymising the link to the
household", and that proposal is deliberately **not** applied at erasure time. The
column is ``NOT NULL``, so there is no link to null; and a till label with a
timestamp and a merchant is re-identifiable whatever the schema calls it. Revision
``0014`` refused to invent a registrant and ``0016`` refused to invent a consent
date, both because a fabricated fact is worse than a missing one. Erasure is the
same trap seen from the other side: a row kept because it "no longer names
anybody" is a claim about re-identification that nobody here is in a position to
make. Erasure erases.

**A refusal beats a partial erasure.** Section 8.5 requires the receipt images to
go before the row, and calls a partial erasure presented as complete a
non-compliance. This application retains no image -- revision ``0012`` made
``receipt.image_object_key`` nullable because every path writes NULL -- and there
is no object-storage client in the repository to call. Rather than delete the rows
and let the response imply a bucket was cleaned, :meth:`PrivacyService.erase`
refuses when it finds a retained key and says what has to happen first.

**One log line, no audit table.** Section 8.6 wants the erasure recorded. The
record is a structured log line carrying the household identifier and a count of
rows per table -- strictly less than what ``infra/logging.py`` already writes on
every request, and outside the database the erasure just emptied. A table would
either be scoped to the household and cascade away with it, or survive it and
retain an identifier of the subject who asked to be forgotten; migration ``0017``
argues that at length. **Nothing else is logged from this module**: no name, no
email, no product, no member. The counts are integers, and ``_scrub`` leaves
integers alone.

Account erasure
---------------

:meth:`PrivacyService.erase_account` was added after the four decisions above and
does not revisit any of them. It answers the half of pentest item ``O-01`` that
was left open -- ``user_account`` held an e-mail address, a display name and a
password hash that no route could ever delete -- and it takes two further
decisions, both of which are refusals.

**It refuses rather than deciding what becomes of a household.** ``O-01`` records
the open question as *"what becomes of a household whose last member leaves"* and
``services/memberships.py`` answers it for one membership by declining to create
the state: ``LastOwnerError`` refuses the removal that would leave a household with
no owner. This method applies **that same rule to all of the account's memberships
at once**, and adds nothing to it. If every household the account belongs to still
has an owner once the account is gone, the memberships fall away with it and the
households carry on, untouched, with the other members' data intact. If any one of
them would be left with nobody who can administer it, the whole erasure is refused
and the household is named, so the person can erase it themselves
(``DELETE /v1/households``, which is theirs to call) and come back.

Two properties of that shape are worth being explicit about, because both are
costs.

*The refusal is stated over the household, not over the account's role.*
``LastOwnerError`` fires when the **target** is the last owner; this fires when the
household would have **no** owner, which is the same thing on every state this API
can produce -- an invitation cannot grant ``owner`` and a removal cannot take the
last one away, so a household with members always has one -- and errs towards
refusing on a state it cannot.

*A person who solely owns three households makes four calls.* That is the price of
not inventing a policy, and it is also the price of ``infra/db.py``'s rule that one
transaction serves one household: erasing them here would mean either several
transactions in one request or a tenant posted per household, and the first breaks
the rule row-level security depends on while the second is not possible at all.
There is no promote-another-member-to-owner route yet either, so today the
actionable half of the refusal is the erasure. Both are product gaps this module
names rather than papers over.

**It reaches the one table no cascade can.** ``rate_limit_bucket`` deliberately
carries no ``household_id`` (revision ``0018``) and its ``bucket_key`` is *"a
household id, a client address, or a normalised e-mail"*. The e-mail rows are
personal data with nothing to cascade from, so both erasures delete their own keys
explicitly through :func:`~chaudron.infra.rate_limits.forget_bucket_keys` -- the
account its address, the household its identifier -- inside the same transaction.
Everything else in that table is bounded by ``scripts/purge_retained_data.py``.

**The log line carries no account identifier**, unlike the household's. That is not
an inconsistency: ``infra/logging.py`` puts ``household_id`` on every request it
serves, so ``household_erased`` discloses nothing new to the operator's journal,
and it writes no ``user_id`` anywhere. Recording one *here*, on the erasure of an
account, would be the only place in the system where a person's identifier outlives
their account -- which is exactly what migration ``0017`` refused to build a table
for. The counts go to the log; the identifiers go to the person, in the response.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import BigInteger, Column, Table, bindparam, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Base, MembershipRole
from chaudron.infra.db import set_transaction_household
from chaudron.infra.rate_limits import forget_bucket_keys

__all__ = [
    "EXPORT_VERSION",
    "TENANT_TABLES",
    "WITHHELD_COLUMNS",
    "AccountErasureBlockedError",
    "AccountErasureReceipt",
    "EndedMembership",
    "ErasureBlockedError",
    "ErasureReceipt",
    "IncompleteErasureError",
    "OwnerlessHousehold",
    "PrivacyService",
]

logger = logging.getLogger(__name__)

#: Bumped when the *shape* of the document changes, never when a column is added
#: or removed. A recipient parsing an export needs to know which reading rules
#: apply; it does not need a new version every time the schema grows a field.
EXPORT_VERSION: Final = 1

_TENANT_COLUMN: Final = "household_id"

#: The six tables addressed by name rather than through the generic walk below.
#: Taken from ``Base.metadata`` rather than through ``Model.__table__``, which the
#: SQLAlchemy stubs type as ``FromClause`` -- enough for a query and not enough for
#: ``delete()`` or for reading ``.name`` under ``mypy --strict``.
_HOUSEHOLD: Final[Table] = Base.metadata.tables["household"]
_HOUSEHOLD_MEMBER: Final[Table] = Base.metadata.tables["household_member"]
_USER_ACCOUNT: Final[Table] = Base.metadata.tables["user_account"]
_PRODUCT: Final[Table] = Base.metadata.tables["product"]
_RECEIPT: Final[Table] = Base.metadata.tables["receipt"]
_RATE_LIMIT_BUCKET: Final[Table] = Base.metadata.tables["rate_limit_bucket"]

#: Every table that belongs to a household, in dependency order. Read from the
#: model for the reason ``tests/tenancy/test_schema_tenant_guard.py`` gives: the
#: table nobody remembers is the one the leak -- or here, the omission -- comes
#: from.
TENANT_TABLES: Final[tuple[Table, ...]] = tuple(
    table for table in Base.metadata.sorted_tables if _TENANT_COLUMN in table.c
)

#: Columns a household holds and this export does not hand back, with the reason.
#:
#: All five are credential material, and that is the only admissible reason to be
#: on this list. An export is a file: it is mailed, backed up, put on a USB stick
#: and read by whoever finds it. A ciphertext plus the key identifier that names
#: which master key opens it is an offline attack somebody can keep; the four
#: characters of ``*_last4`` beside it are not, and they stay, because they are
#: what lets a household recognise which of its own keys is installed.
#:
#: ``token_hash`` is not decryptable, and it is still not exported: it is the
#: lookup key of a live bearer credential (``uq_machine_token_token_hash``), and a
#: document that carries one is a document that can be replayed against the
#: instance if the hashing is ever weakened or the digest is ever accepted
#: directly.
WITHHELD_COLUMNS: Final[Mapping[tuple[str, str], str]] = {
    ("llm_provider_config", "api_key_ciphertext"): (
        "SECRET: AES-256-GCM ciphertext of a third party's API key. Exported as "
        "api_key_last4, which identifies the key without being one."
    ),
    ("llm_provider_config", "api_key_encryption_key_id"): (
        "Names which master key opens the ciphertext above; useless alone and "
        "half of an offline attack beside it."
    ),
    ("shopping_export_target", "token_ciphertext"): (
        "SECRET: AES-256-GCM ciphertext of a third party's personal token. Exported as token_last4."
    ),
    ("shopping_export_target", "token_encryption_key_id"): (
        "Names which master key opens the ciphertext above; useless alone and "
        "half of an offline attack beside it."
    ),
    ("machine_token", "token_hash"): (
        "The lookup key of a live bearer credential. The token itself left this "
        "instance once, at creation; prefix and last4 are what identify it here."
    ),
    ("household_invitation", "token_hash"): (
        "The lookup key of a pending invitation, which is a bearer credential for "
        "membership of this household. Same reasoning as machine_token.token_hash, "
        "and it bites harder: an export naming a live invitation would be a way in "
        "for whoever reads the file. prefix and last4 identify it here."
    ),
}

#: Held about the household and outside the export, with the reason. Rendered into
#: the document itself: article 15(1) is a right to know what is processed, and a
#: silent omission answers it less well than a named one.
_WITHHELD_ELSEWHERE: Final[Mapping[str, str]] = {
    "household.calendar_feed_epoch": (
        "A revocation counter, not personal data, and one component of the CalDAV "
        "credential this household's feed is derived from."
    ),
    "user_session": (
        "Sessions belong to an account rather than to a household, so they are "
        "outside the scope of this document. They hold the digest of a live "
        "cookie."
    ),
    "user_account.password_hash": (
        "A credential, and never disclosed to anybody, the owner included."
    ),
    "product.off_payload (public catalogue rows)": (
        "The raw Open Food Facts record for a shared catalogue entry. Public "
        "reference data with no household, reproducible from the barcode."
    ),
}


#: Every household this account belongs to, with its role and how many *other*
#: owners each one has -- past the row-level security on ``household_member``,
#: through the ``SECURITY DEFINER`` function migration ``0026`` creates. Read that
#: revision for why a plain query cannot answer this: the read spans every
#: household the account belongs to, ``infra/db.py`` gives a transaction exactly
#: one, and with no tenant posted the policy shows zero rows -- so the direct
#: version reports that the account belongs to nothing and is safe to erase, which
#: is the most dangerous wrong answer on offer.
#:
#: The identifier is bound, never interpolated, and the function returns counts
#: rather than the other members' rows.
_ACCOUNT_SURVEY: Final = (
    text("SELECT * FROM chaudron_account_erasure_survey(:user_id)")
    .bindparams(bindparam("user_id", type_=_USER_ACCOUNT.c.id.type))
    .columns(
        household_id=_HOUSEHOLD.c.id.type,
        household_name=_HOUSEHOLD.c.name.type,
        role=_HOUSEHOLD_MEMBER.c.role.type,
        other_owner_count=BigInteger(),
    )
)


class ErasureBlockedError(Exception):
    """Erasure would leave data behind that this application cannot reach."""

    def __init__(self, retained_images: int) -> None:
        super().__init__(
            f"{retained_images} receipt(s) in this household still name a stored "
            f"image. Deleting the rows would leave those objects in place, and an "
            f"erasure that is reported as complete while objects survive is a "
            f"non-compliance rather than a bug. This deployment reintroduced image "
            f"retention (revision 0012 turned it off); it has to delete the objects "
            f"and clear receipt.image_object_key before the erasure can be honoured."
        )
        self.retained_images = retained_images


class IncompleteErasureError(RuntimeError):
    """The household row is gone and rows that should have cascaded are not.

    Raised **after** the delete and before anything is committed, so the
    transaction rolls back and the caller gets a failure rather than a receipt
    that says the data is gone. There is no worse outcome for this feature than
    reporting a successful erasure over surviving rows, so the post-condition is
    checked rather than assumed: what makes the ``CASCADE`` total is a property of
    seventeen foreign keys, and no test in production runs.
    """


@dataclass(frozen=True, slots=True)
class OwnerlessHousehold:
    """A household this account's erasure would leave with nobody to administer it."""

    household_id: uuid.UUID
    name: str
    #: The role the account being erased holds there. Reported so the message can
    #: tell "you are its only owner" from "it has no owner at all", which is a
    #: state no route can produce and which this refusal still declines to create.
    role: MembershipRole


class AccountErasureBlockedError(Exception):
    """Erasing this account would leave at least one household without an owner.

    The refusal ``services/memberships.py`` already makes for a single membership
    (``LastOwnerError``), made once for all of them. It names the households, because
    a refusal the person cannot act on is worse than no refusal: each one is theirs
    to erase with ``DELETE /v1/households`` before coming back here.
    """

    def __init__(self, households: Sequence[OwnerlessHousehold]) -> None:
        names = ", ".join(f"{household.name!r}" for household in households)
        super().__init__(
            f"{len(households)} household(s) would be left with nobody who can "
            f"administer them: {names}. Erasing an account ends its memberships, and "
            f"a household with no owner can no longer be configured, invited to or "
            f"erased by anybody -- so its data would be retained with no one able to "
            f"reach it, which is the opposite of what an erasure is for. Erase each "
            f"of them first (DELETE /v1/households, which you may call as their "
            f"owner), or have another owner take them over, then erase this account."
        )
        self.households = tuple(households)


@dataclass(frozen=True, slots=True)
class EndedMembership:
    """One membership that fell away with the account, and the household it opened."""

    household_id: uuid.UUID
    name: str
    role: MembershipRole


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    """What was removed, so the person who asked has something to keep."""

    household_id: uuid.UUID
    erased_at: datetime
    #: Table name to the number of rows removed, counted before the delete.
    rows_erased: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class AccountErasureReceipt:
    """What the erasure of one account removed.

    Carries the households it used to open rather than a row count, because that is
    the part the person cannot check afterwards: the account is gone, and with it
    every screen that could have listed them.
    """

    user_id: uuid.UUID
    erased_at: datetime
    memberships_ended: tuple[EndedMembership, ...]
    #: Rate-limit buckets keyed on this account's address, in every scope. See
    #: :func:`~chaudron.infra.rate_limits.forget_bucket_keys`.
    rate_limit_rows_forgotten: int


class PrivacyService:
    """Reads and erases one household, and holds no opinion about who may ask.

    Authorisation is ``api/deps.py`` (``OwnerHouseholdDep``) and tenancy is
    PostgreSQL (migration ``0004`` for the rows, ``0017`` for the household
    itself). This class is handed a household identifier that both have already
    agreed on.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ----------------------------------------------------------------- #
    # Article 15 and 20
    # ----------------------------------------------------------------- #

    async def export(self, household_id: uuid.UUID, *, requested_by: uuid.UUID) -> dict[str, Any]:
        """The household, as a structured document in a commonly used format.

        Article 20 asks for "a structured, commonly used and machine-readable
        format"; this is JSON with one key per table and the schema's own column
        names, which is the only naming a recipient can check against the
        published data model.

        *requested_by* is not an authorisation -- it has been checked already --
        it decides one thing: whose account fields the document carries. The
        household's other members appear with an identifier and a display name,
        which is what makes the rows readable; their email address and their sign-in
        history are their data and not this household's to hand out.
        """
        household = await self._household_row(household_id)
        sections: dict[str, list[dict[str, Any]]] = {}
        for table in TENANT_TABLES:
            sections[table.name] = await self._rows_of(table, household_id)

        return {
            "export_version": EXPORT_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "household_id": str(household_id),
            "household": household,
            "accounts": await self._member_accounts(household_id, requested_by=requested_by),
            "tables": sections,
            "referenced_public_products": await self._referenced_public_products(sections),
            "withheld": self._withheld_notice(),
        }

    async def _household_row(self, household_id: uuid.UUID) -> dict[str, Any]:
        row = (
            (await self._session.execute(select(_HOUSEHOLD).where(_HOUSEHOLD.c.id == household_id)))
            .mappings()
            .one()
        )
        return {
            name: _render(value)
            for name, value in row.items()
            if name != "calendar_feed_epoch"  # see _WITHHELD_ELSEWHERE
        }

    async def _rows_of(self, table: Table, household_id: uuid.UUID) -> list[dict[str, Any]]:
        withheld = {column for (name, column) in WITHHELD_COLUMNS if name == table.name}
        statement = (
            select(table)
            .where(table.c[_TENANT_COLUMN] == household_id)
            .order_by(*table.primary_key.columns)
        )
        rows = (await self._session.execute(statement)).mappings().all()
        return [
            {name: _render(value) for name, value in row.items() if name not in withheld}
            for row in rows
        ]

    async def _member_accounts(
        self, household_id: uuid.UUID, *, requested_by: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The accounts that currently belong to this household.

        Deliberately the *current* members and not every account identifier the
        rows happen to mention. An ``actor_user_id`` left behind by somebody who
        has since gone is exported as the bare identifier it is: resolving it to a
        name would disclose a person who is no longer part of this household to
        whoever now owns it.
        """
        members, accounts = _HOUSEHOLD_MEMBER, _USER_ACCOUNT
        rows = (
            await self._session.execute(
                select(
                    accounts.c.id,
                    accounts.c.display_name,
                    accounts.c.email,
                    accounts.c.last_login_at,
                )
                .join(members, members.c.user_id == accounts.c.id)
                .where(members.c.household_id == household_id)
                .order_by(accounts.c.id)
            )
        ).all()
        exported: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"id": str(row.id), "display_name": row.display_name}
            if row.id == requested_by:
                entry["email"] = row.email
                entry["last_login_at"] = _render(row.last_login_at)
            exported.append(entry)
        return exported

    async def _referenced_public_products(
        self, sections: Mapping[str, Sequence[Mapping[str, Any]]]
    ) -> list[dict[str, Any]]:
        """The shared catalogue entries this household's rows point at.

        Not the household's personal data -- ``docs/security-model.md`` section 8.3
        is explicit that the Open Food Facts cache carries none, which is why it
        has no tenant column -- and the export would be unreadable without it: a
        lot referencing a product identifier and nothing else says nothing about
        what is in the cupboard. Reduced to the fields that make a row legible; the
        raw upstream payload is public and reproducible from the barcode.
        """
        wanted: set[uuid.UUID] = set()
        for table_name, column_name in _PRODUCT_REFERENCES:
            for row in sections.get(table_name, ()):
                value = row.get(column_name)
                if isinstance(value, str):
                    wanted.add(uuid.UUID(value))
        if not wanted:
            return []

        products = _PRODUCT
        rows = (
            (
                await self._session.execute(
                    select(
                        products.c.id,
                        products.c.gtin,
                        products.c.name,
                        products.c.brand,
                        products.c.category_tag,
                        products.c.source,
                        products.c.off_synced_at,
                    )
                    .where(products.c.id.in_(wanted), products.c.household_id.is_(None))
                    .order_by(products.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [{name: _render(value) for name, value in row.items()} for row in rows]

    def _withheld_notice(self) -> dict[str, str]:
        notice = {
            f"{table}.{column}": reason for (table, column), reason in WITHHELD_COLUMNS.items()
        }
        notice.update(_WITHHELD_ELSEWHERE)
        return notice

    # ----------------------------------------------------------------- #
    # Article 17
    # ----------------------------------------------------------------- #

    async def erase(self, household_id: uuid.UUID) -> ErasureReceipt:
        """Delete the household and everything that belongs to it.

        The ``ON DELETE CASCADE`` on every tenant table does the work; this method
        exists to make three things true that the cascade alone does not.

        *The transaction is scoped before anything is deleted.* Posting the tenant
        explicitly arms migration ``0017``'s policy and, just as usefully, raises
        rather than proceeds if this transaction was already serving a different
        household (``infra/db.py``, :class:`~chaudron.infra.db.TenantScopeError`).

        *Data this application cannot reach blocks the erasure* rather than being
        silently left behind -- see :class:`ErasureBlockedError`.

        *The result is verified by reading the tables back.* If any row survived,
        the exception rolls the whole thing back: a receipt that says "erased" over
        surviving rows is the one outcome worse than a failure.
        """
        await set_transaction_household(self._session, household_id)

        retained = await self._retained_image_count(household_id)
        if retained:
            raise ErasureBlockedError(retained)

        rows_erased = await self._row_counts(household_id)
        if rows_erased[_HOUSEHOLD.name] != 1:
            # `api/deps.py` proved the caller owns a membership of this household,
            # so the row existed when the request was authorised. Not finding it
            # here means somebody erased it concurrently, and answering "erased"
            # would credit this request with work it did not do.
            raise IncompleteErasureError(f"household {household_id} is not there to erase")

        await self._session.execute(delete(_HOUSEHOLD).where(_HOUSEHOLD.c.id == household_id))
        # The one line the cascade cannot do for us. `rate_limit_bucket` carries no
        # tenant by design (revision `0018`), so no foreign key reaches the buckets
        # keyed on this household's identifier and they would outlive it. Counted
        # beside the rest: a receipt that omitted them would describe a smaller
        # erasure than the one that happened.
        rows_erased[_RATE_LIMIT_BUCKET.name] = await forget_bucket_keys(
            self._session, [str(household_id)]
        )

        survivors = {
            table: count for table, count in (await self._row_counts(household_id)).items() if count
        }
        if survivors:
            raise IncompleteErasureError(
                f"rows survived the erasure of household {household_id}: {survivors}"
            )

        erased_at = datetime.now(UTC)
        # The whole audit record, and everything it may carry: an identifier every
        # request already logs, and integers. See the module docstring.
        logger.info("household_erased", extra={"rows_erased": dict(rows_erased)})
        return ErasureReceipt(
            household_id=household_id, erased_at=erased_at, rows_erased=rows_erased
        )

    async def _retained_image_count(self, household_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(_RECEIPT)
            .where(
                _RECEIPT.c[_TENANT_COLUMN] == household_id,
                _RECEIPT.c.image_object_key.is_not(None),
            )
        )
        return int(count or 0)

    async def _row_counts(self, household_id: uuid.UUID) -> dict[str, int]:
        """How many rows each table holds for this household, the root included."""
        counts: dict[str, int] = {}
        for table in TENANT_TABLES:
            count = await self._session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c[_TENANT_COLUMN] == household_id)
            )
            counts[table.name] = int(count or 0)
        count = await self._session.scalar(
            select(func.count()).select_from(_HOUSEHOLD).where(_HOUSEHOLD.c.id == household_id)
        )
        counts[_HOUSEHOLD.name] = int(count or 0)
        return counts

    # ----------------------------------------------------------------- #
    # Article 17, over an account rather than a household
    # ----------------------------------------------------------------- #

    async def erase_account(self, user_id: uuid.UUID) -> AccountErasureReceipt:
        """Delete one account: its identity, its credentials, its memberships.

        The module docstring argues the policy. What this method does, in order, and
        none of it is interchangeable:

        1. **Read the account.** Its address is needed before the row goes, to reach
           the rate-limit buckets keyed on it -- and finding no row means somebody
           erased it concurrently, which is a failure rather than a success with
           nothing to do.
        2. **Survey every household it belongs to**, past row-level security
           (migration ``0026``), and refuse if any of them would be left with no
           owner. Before anything is written, so a refusal costs nothing and leaves
           nothing half-done.
        3. **Forget the buckets keyed on the address**, in the same transaction as
           the delete below. ``rate_limit_bucket`` has no tenant and no foreign key
           to an account, so this is the one place those rows can be reached.
        4. **Delete the row.** ``user_account`` is outside row-level security -- it
           has no tenant and could not have one -- so the ``WHERE`` clause is the
           whole of the authorisation the engine will see, and the identifier comes
           from the caller's own session (``api/deps.py``), never from a body.
           ``ON DELETE CASCADE`` takes all five things that *are* this account:
           its memberships, its browser sessions, its outstanding reset tokens, its
           machine tokens, and the invitations it issued. The last two are worth
           saying out loud. A machine token dies with the account that minted it --
           an appliance polling on it stops, which it would have done anyway at the
           next request, since ``chaudron_resolve_machine_token`` joins back to a
           membership that has just gone. And a pending invitation issued by an
           account that no longer exists must not stay redeemable: it is a
           membership of somebody else's household, offered by nobody.
           ``ON DELETE SET NULL`` handles the fourteen columns that merely *mention*
           an account -- who requested a suggestion, who invited whom, who moved
           stock -- and every one of those is somebody else's row, so the mention is
           cleared and the row stays.
        5. **Read it all back.** Through the same ``SECURITY DEFINER`` function, for
           the reason the survey needs it: a verification query on
           ``household_member`` would be answered by the policy rather than by the
           data, return zero rows whether or not any survived, and pass for ever.
           That is worse than no post-condition at all.
        """
        account = (
            (
                await self._session.execute(
                    select(_USER_ACCOUNT.c.id, _USER_ACCOUNT.c.email).where(
                        _USER_ACCOUNT.c.id == user_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if account is None:
            # `api/deps.py` resolved a live session for this account, so the row
            # existed when the request was authorised. Answering "erased" would
            # credit this request with work it did not do.
            raise IncompleteErasureError(f"account {user_id} is not there to erase")

        memberships = await self._household_survey(user_id)
        blocked = [
            OwnerlessHousehold(
                household_id=row.household_id, name=row.household_name, role=row.role
            )
            for row in memberships
            if row.other_owner_count == 0
        ]
        if blocked:
            raise AccountErasureBlockedError(blocked)

        forgotten = await forget_bucket_keys(self._session, _bucket_keys_for(account["email"]))
        await self._session.execute(delete(_USER_ACCOUNT).where(_USER_ACCOUNT.c.id == user_id))

        survivors = await self._household_survey(user_id)
        still_there = await self._session.scalar(
            select(func.count()).select_from(_USER_ACCOUNT).where(_USER_ACCOUNT.c.id == user_id)
        )
        if survivors or still_there:
            raise IncompleteErasureError(
                f"the erasure of account {user_id} left {len(survivors)} membership(s) "
                f"and {int(still_there or 0)} account row(s) behind"
            )

        erased_at = datetime.now(UTC)
        # Integers only, and no identifier of the person who asked to be forgotten.
        # See the module docstring: this is the one erasure whose subject
        # `infra/logging.py` does not already write on every request.
        logger.info(
            "account_erased",
            extra={
                "memberships_ended": len(memberships),
                "rate_limit_rows_forgotten": forgotten,
            },
        )
        return AccountErasureReceipt(
            user_id=user_id,
            erased_at=erased_at,
            memberships_ended=tuple(
                EndedMembership(
                    household_id=row.household_id, name=row.household_name, role=row.role
                )
                for row in memberships
            ),
            rate_limit_rows_forgotten=forgotten,
        )

    async def _household_survey(self, user_id: uuid.UUID) -> Sequence[Any]:
        """Every household this account belongs to, past the policy. See ``0026``."""
        return (await self._session.execute(_ACCOUNT_SURVEY, {"user_id": user_id})).all()


# --------------------------------------------------------------------------- #
# Reading the model
# --------------------------------------------------------------------------- #


def _bucket_keys_for(email: str) -> tuple[str, ...]:
    """The rate-limit keys this address can appear under.

    Both e-mail-keyed limiters charge against the **normalised** address today --
    ``routers/auth.py`` passes every one of them through ``normalise_email``, which
    is the trim-and-lower-case that ``uq_user_account_email_lower`` performs -- so
    the stored value and the key differ whenever the account was registered with
    capitals. Normalising here is therefore not belt-and-braces, it is the whole
    match: an account at ``Sujet@Example.Test`` has its buckets under
    ``sujet@example.test`` and a delete on the column value alone would find none.

    The stored spelling is kept beside it anyway, and that *is* belt-and-braces: it
    costs one element of an ``IN`` list and removes the need to trust that every
    limiter added later remembers to normalise. Duplicates collapse; a key that is
    not in the table deletes nothing.
    """
    stored = email.strip()
    return (stored, stored.lower())


def _references_product(column: Column[Any]) -> bool:
    return any(key.column.table.name == "product" for key in column.foreign_keys)


#: Every ``(table, column)`` in a tenant table that points at the catalogue.
#: Derived from the foreign keys rather than listed, so a new reference is picked
#: up by the export the day it is declared -- five of these exist today and each
#: one was added by a different feature.
_PRODUCT_REFERENCES: Final[tuple[tuple[str, str], ...]] = tuple(
    (table.name, column.name)
    for table in TENANT_TABLES
    for column in table.c
    if _references_product(column)
)


def _render(value: Any) -> Any:
    """One column value, as something ``json.dumps`` accepts and a human can read.

    ``Decimal`` becomes a string rather than a float: a quantity that arrives back
    as ``0.30000000000000004`` is not the quantity that was stored, and the whole
    point of a portability export is that the numbers survive the round trip.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return _render(value.value)
    if isinstance(value, Mapping):
        return {str(key): _render(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_render(item) for item in value]
    if isinstance(value, bytes | bytearray):  # pragma: no cover - every one is withheld
        raise AssertionError(
            "a binary column reached the export; every one of them is credential "
            "material and belongs in WITHHELD_COLUMNS"
        )
    return str(value)
