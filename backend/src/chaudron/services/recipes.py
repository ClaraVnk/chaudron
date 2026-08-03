"""Recipe suggestions: from what the household really owns to a persisted answer.

Three rules live here and are the reason this layer exists at all.

*The stock comes from the database, never from the client.* The request names
locations; the contents of those locations are read server-side.

*``in_stock`` is recomputed.* The model is asked to flag which ingredients the
household already has, and its answer is thrown away: a suggestion that claims the
eggs are in the fridge when they are not sends someone to cook with nothing. The
flag returned to the client is derived from the inventory that was actually sent.

*Every generated suggestion is written down.* ``recipe_suggestion`` keeps the
provider mode, the model, the prompt version, the latency and the stock snapshot
that produced it. Without that row, "why did it suggest that?" and "the suggestions
got worse last week" are unanswerable, and a quality complaint cannot be told apart
from a small local model doing its best.

The write goes through the request's session rather than a repository: this slice
adds one writer and one table, and a repository port with a single implementation
and a single caller would be indirection without a second reader to justify it.
"""

from __future__ import annotations

import logging
import time
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.llm_ports import InventoryItem as ProviderInventoryItem
from chaudron.domain.llm_ports import RecipeRequest
from chaudron.domain.llm_ports import RecipeSuggestion as GeneratedRecipe
from chaudron.domain.models import RecipeStatus, RecipeSuggestion
from chaudron.domain.ports import (
    InventoryFilter,
    InventoryItem,
    InventoryRepository,
    LocationNotFoundError,
    LocationRepository,
)
from chaudron.infra.llm.prompts import PROMPT_VERSION
from chaudron.services.providers import ActiveProvider, ProviderService

logger = logging.getLogger(__name__)

#: The suggestions are written for the household, not for the developer.
RESPONSE_LANGUAGE: Final = "fr"

#: How many stock lines one call may carry. Past this the prompt is mostly noise,
#: the answer is not better, and every item is tokens somebody pays for. The
#: provider layer trims further when the model's context window demands it.
MAX_INVENTORY_ITEMS: Final = 300

#: Below this length a substring match is a coincidence ("ail" inside "bouteille"),
#: and a false "in stock" is exactly the failure this recomputation exists to avoid.
_MIN_SUBSTRING_MATCH: Final = 4


@dataclass(frozen=True, slots=True)
class SuggestRecipesCommand:
    location_ids: tuple[uuid.UUID, ...] = ()
    max_suggestions: int = 3
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class SuggestedIngredient:
    name: str
    amount: str | None
    unit: str | None
    in_stock: bool


@dataclass(frozen=True, slots=True)
class SuggestedRecipe:
    id: uuid.UUID
    title: str
    summary: str
    duration_minutes: int | None
    servings: int | None
    ingredients: tuple[SuggestedIngredient, ...]
    steps: tuple[str, ...]
    uses_expiring_soon: bool


@dataclass(frozen=True, slots=True)
class SuggestionSet:
    provider_mode: str
    model: str
    recipes: tuple[SuggestedRecipe, ...]


class RecipeService:
    def __init__(
        self,
        session: AsyncSession,
        providers: ProviderService,
        inventory: InventoryRepository,
        locations: LocationRepository,
    ) -> None:
        self._session = session
        self._providers = providers
        self._inventory = inventory
        self._locations = locations

    async def suggest(
        self, household_id: uuid.UUID, command: SuggestRecipesCommand
    ) -> SuggestionSet:
        """Ask the household's provider for recipes, and record what it answered.

        Raises the domain provider errors unchanged; the API layer owns the mapping
        onto status codes, and none of them may quote a provider's own message.
        """
        provider = await self._providers.for_recipes(household_id)
        stock = await self._stock(household_id, command.location_ids)
        today = datetime.now(UTC).date()
        inventory = tuple(_to_provider_item(item, today) for item in stock)

        request = RecipeRequest(
            inventory=inventory,
            max_suggestions=command.max_suggestions,
            notes=command.notes or None,
            language=RESPONSE_LANGUAGE,
        )
        started = time.perf_counter()
        generated = await provider.generator.suggest(request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        notice = getattr(generated, "degradation_notice", None)
        if isinstance(notice, str):
            # Not returned to the client: the answer shape is fixed by the contract,
            # and the persistent banner is served by the capabilities endpoint.
            logger.info("recipe_suggestions_degraded", extra={"notice": notice})

        stocked = tuple(_normalise(item.product.name) for item in stock)
        snapshot = _snapshot(request, command)
        recipes = tuple(
            self._persist(
                household_id,
                provider,
                recipe,
                stocked=stocked,
                snapshot=snapshot,
                latency_ms=latency_ms,
            )
            for recipe in generated[: command.max_suggestions]
        )
        await self._session.flush()
        return SuggestionSet(
            provider_mode=provider.config.mode.value,
            model=provider.config.model,
            recipes=recipes,
        )

    async def _stock(
        self, household_id: uuid.UUID, location_ids: tuple[uuid.UUID, ...]
    ) -> list[InventoryItem]:
        """The lots the suggestions may draw on, bounded and tenant-checked.

        An unknown location is a 404 rather than an empty inventory: silently
        suggesting recipes from nothing would look like a broken model instead of a
        stale identifier in the client.
        """
        if not location_ids:
            page = await self._inventory.list_items(
                household_id, InventoryFilter(limit=MAX_INVENTORY_ITEMS)
            )
            return list(page.items)

        collected: dict[uuid.UUID, InventoryItem] = {}
        for location_id in location_ids:
            if not await self._locations.exists(household_id, location_id):
                raise LocationNotFoundError(location_id)
            page = await self._inventory.list_items(
                household_id,
                InventoryFilter(location_id=location_id, limit=MAX_INVENTORY_ITEMS),
            )
            for item in page.items:
                collected[item.id] = item
        return list(collected.values())[:MAX_INVENTORY_ITEMS]

    def _persist(
        self,
        household_id: uuid.UUID,
        provider: ActiveProvider,
        recipe: GeneratedRecipe,
        *,
        stocked: Sequence[str],
        snapshot: dict[str, Any],
        latency_ms: int,
    ) -> SuggestedRecipe:
        suggestion_id = uuid.uuid7()
        ingredients = tuple(
            SuggestedIngredient(
                name=ingredient.name,
                amount=ingredient.amount,
                unit=ingredient.unit,
                in_stock=_is_in_stock(ingredient.name, stocked),
            )
            for ingredient in recipe.ingredients
        )
        summary = recipe.summary or ""
        payload: dict[str, Any] = {
            "title": recipe.title,
            "summary": summary,
            # The contract exposes one total duration; the table splits prep from
            # cook. The split is not invented here, so it stays in the payload.
            "duration_minutes": recipe.duration_minutes,
            "servings": recipe.servings,
            "uses_expiring_soon": recipe.uses_expiring_soon,
            "steps": list(recipe.steps),
            "ingredients": [
                {
                    "name": ingredient.name,
                    "amount": ingredient.amount,
                    "unit": ingredient.unit,
                    # As shown to the user, not as claimed by the model.
                    "in_stock": ingredient.in_stock,
                }
                for ingredient in ingredients
            ],
        }
        self._session.add(
            RecipeSuggestion(
                id=suggestion_id,
                household_id=household_id,
                title=recipe.title,
                summary=recipe.summary,
                servings=recipe.servings,
                payload=payload,
                stock_snapshot=snapshot,
                provider_mode=provider.config.mode,
                provider_code=provider.config.provider_code,
                model=provider.config.model,
                prompt_version=PROMPT_VERSION,
                llm_provider_config_id=provider.config_id,
                # Zero, not an estimate: no adapter surfaces provider usage through
                # the `RecipeGenerator` port yet, and a made-up token count in a cost
                # table is worse than an honest zero. The latency is measured, so the
                # row is still a usable diagnostic.
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
                cost_micro=0,
                latency_ms=latency_ms,
                status=RecipeStatus.GENERATED,
            )
        )
        return SuggestedRecipe(
            id=suggestion_id,
            title=recipe.title,
            summary=summary,
            duration_minutes=recipe.duration_minutes,
            servings=recipe.servings,
            ingredients=ingredients,
            steps=tuple(recipe.steps),
            uses_expiring_soon=recipe.uses_expiring_soon,
        )


def _snapshot(request: RecipeRequest, command: SuggestRecipesCommand) -> dict[str, Any]:
    """Exactly what was sent, so a bad suggestion can be explained afterwards.

    The most sensitive row in the database: a complete inventory of a home. It falls
    under the same retention policy as receipt images (``domain/models.py``).
    """
    return {
        "language": request.language,
        "notes": request.notes,
        "max_suggestions": request.max_suggestions,
        "location_ids": [str(location_id) for location_id in command.location_ids],
        "items": [
            {
                "name": item.name,
                "quantity": item.quantity,
                "unit": item.unit,
                "expires_in_days": item.expires_in_days,
            }
            for item in request.inventory
        ],
    }


def _to_provider_item(item: InventoryItem, today: date) -> ProviderInventoryItem:
    return ProviderInventoryItem(
        name=item.product.name,
        quantity=_amount(item.quantity_value),
        unit=item.quantity_unit_code,
        expires_in_days=None if item.best_before is None else (item.best_before - today).days,
    )


def _amount(value: Decimal) -> str:
    """``1.500`` -> ``1.5``, and never an exponent: this ends up in a prompt."""
    text = format(value, "f")
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".") or "0"


def _normalise(label: str) -> str:
    """Case- and accent-insensitive form, so "Crème" matches "creme"."""
    decomposed = unicodedata.normalize("NFKD", label.casefold())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.split())


def _is_in_stock(name: str, stocked: Sequence[str]) -> bool:
    """Whether the household owns this ingredient, on our own reading of its stock.

    Deliberately conservative: an exact match, or a containment either way once the
    shorter side is long enough to mean something. Marking something in stock that
    is not is the expensive mistake -- the user goes to cook and stops halfway --
    while the reverse merely adds a line to a shopping list.
    """
    candidate = _normalise(name)
    if not candidate:
        return False
    return any(
        candidate == entry
        or (len(candidate) >= _MIN_SUBSTRING_MATCH and candidate in entry)
        or (len(entry) >= _MIN_SUBSTRING_MATCH and entry in candidate)
        for entry in stocked
    )
