"""Recipe suggestions, and the translation of provider failures into HTTP.

One rule governs every path out of this module: **nothing a provider said reaches
the client**. A vendor SDK puts the key it was called with in its own error message
(security review, SEC-003), and the domain errors that cross this boundary may quote
a snippet of a model's answer. Responses therefore carry sentences written here, and
the log line goes through :func:`chaudron.infra.llm.redaction.redact`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from chaudron.api.deps import HouseholdDep, RecipeServiceDep, enforce_recipe_limits
from chaudron.api.errors import ProblemError
from chaudron.api.schemas import (
    RecipeIngredientOut,
    RecipeSuggestionOut,
    SuggestRecipesIn,
    SuggestRecipesOut,
)
from chaudron.domain.llm_ports import (
    LlmError,
    ProviderCapabilityUnavailable,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from chaudron.infra.llm.redaction import redact
from chaudron.services.recipes import SuggestionSet, SuggestRecipesCommand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/recipes", tags=["recipes"])


@router.post(
    "/suggest",
    response_model=SuggestRecipesOut,
    summary="Suggest recipes from the current stock",
    # The only endpoint whose cost is money rather than milliseconds. The guard
    # runs before the household's provider is even resolved, and holds a
    # concurrency slot for the whole call (``api/throttling.py``).
    dependencies=[Depends(enforce_recipe_limits)],
)
async def suggest_recipes(
    household_id: HouseholdDep, service: RecipeServiceDep, payload: SuggestRecipesIn
) -> SuggestRecipesOut:
    try:
        result = await service.suggest(
            household_id,
            SuggestRecipesCommand(
                location_ids=tuple(payload.location_ids),
                max_suggestions=payload.max_suggestions,
                notes=payload.notes.strip() or None,
            ),
        )
    except LlmError as error:
        _log(error)
        # `from None`: the chain would carry the provider's own message into the
        # traceback of any handler that logs it.
        raise _problem_for(error) from None
    return _to_out(result)


def _to_out(result: SuggestionSet) -> SuggestRecipesOut:
    return SuggestRecipesOut(
        provider_mode=result.provider_mode,
        model=result.model,
        suggestions=[
            RecipeSuggestionOut(
                id=recipe.id,
                title=recipe.title,
                summary=recipe.summary,
                duration_minutes=recipe.duration_minutes,
                servings=recipe.servings,
                ingredients=[
                    RecipeIngredientOut(
                        name=ingredient.name,
                        amount=ingredient.amount,
                        unit=ingredient.unit,
                        in_stock=ingredient.in_stock,
                    )
                    for ingredient in recipe.ingredients
                ],
                steps=list(recipe.steps),
                uses_expiring_soon=recipe.uses_expiring_soon,
            )
            for recipe in result.recipes
        ],
    )


def _log(error: LlmError) -> None:
    """Record the failure with the three facts a support ticket needs, scrubbed.

    ``provider`` and ``model`` come from our own context object, never from the
    provider's message; that message is redacted before it is written at all.
    """
    context = error.context
    logger.warning(
        "recipe_provider_failure",
        extra={
            "error": type(error).__name__,
            "provider": None if context is None else context.provider,
            "model": None if context is None else context.model,
            "failure_mode": None if context is None else context.failure_mode,
            "detail": redact(str(error)),
        },
    )


def _problem_for(error: LlmError) -> ProblemError:
    """Map a provider failure onto the status code the client can act on.

    Every ``detail`` below is written here. None of them interpolates ``error``:
    that is what keeps a household's API key out of an HTTP response.
    """
    match error:
        case ProviderNotConfigured():
            # Also catches ProviderNotPermitted: a household that may not use the
            # instance's own key needs the configuration screen, same as one that
            # configured nothing at all.
            return ProblemError(
                slug="provider-not-configured",
                title="Model provider not configured",
                status=409,
                detail=(
                    "This household has no usable model provider. Configure one before "
                    "asking for recipe suggestions."
                ),
            )
        case ProviderCapabilityUnavailable():
            return ProblemError(
                slug="provider-capability-unavailable",
                title="Model capability unavailable",
                status=409,
                detail=(
                    "The configured model cannot do what this request needs. Choose a "
                    "model that supports it."
                ),
                # Our own token from the capability taxonomy, not provider text.
                capability=error.capability,
            )
        case ProviderQuotaExceeded():
            return ProblemError(
                slug="provider-quota-exceeded",
                title="Model provider quota exceeded",
                status=429,
                detail=(
                    "The model provider refused the request for rate-limit or credit "
                    "reasons. Retry later, or check the account behind the key."
                ),
                headers=_retry_after(error),
            )
        case ProviderUnavailable():
            return ProblemError(
                slug="provider-unavailable",
                title="Model provider unavailable",
                status=503,
                detail="The model provider did not answer. Retry shortly.",
            )
        case ProviderResponseInvalid():
            return ProblemError(
                slug="provider-response-invalid",
                title="Unreadable model response",
                status=502,
                detail=(
                    "The model provider answered with something this application could "
                    "not read. Retry, or choose another model."
                ),
            )
        case _:
            return ProblemError(
                slug="provider-error",
                title="Model provider failure",
                status=502,
                detail="The model provider could not serve this request.",
            )


def _retry_after(error: ProviderQuotaExceeded) -> dict[str, str] | None:
    """Forward a retry delay when the domain error carries one.

    No adapter attaches it today. Read defensively rather than assumed absent, so
    the header appears the moment one does -- and so this code does not invent a
    delay nobody measured.
    """
    delay: Any = getattr(error, "retry_after", None)
    if isinstance(delay, int) and delay > 0:
        return {"Retry-After": str(delay)}
    return None
