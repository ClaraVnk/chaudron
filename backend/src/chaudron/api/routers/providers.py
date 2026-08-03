"""What the household's model provider can do, before anything is asked of it."""

from __future__ import annotations

from fastapi import APIRouter

from chaudron.api.deps import HouseholdDep, ProviderServiceDep
from chaudron.api.schemas import CapabilityFlagsOut, ProviderCapabilitiesOut
from chaudron.domain.llm_ports import DegradationNotice

router = APIRouter(prefix="/v1/providers", tags=["providers"])


def _reason(notice: DegradationNotice) -> str:
    """One readable French sentence per notice: what is reduced, and what to do."""
    return notice.reason if notice.remedy is None else f"{notice.reason} {notice.remedy}"


@router.get(
    "/capabilities",
    response_model=ProviderCapabilitiesOut,
    summary="Describe the household's model provider",
)
async def provider_capabilities(
    household_id: HouseholdDep, service: ProviderServiceDep
) -> ProviderCapabilitiesOut:
    """Always ``200``, including when nothing is configured.

    Having no provider is a normal state with a screen of its own, not an error: a
    4xx here would make the client render a failure for a household that has simply
    not finished setting up.
    """
    view = await service.status(household_id)
    return ProviderCapabilitiesOut(
        configured=view.configured,
        mode=view.mode,
        provider=view.provider,
        model=view.model,
        capabilities=CapabilityFlagsOut(
            vision=view.supports_vision,
            structured_output=view.supports_structured_output,
        ),
        degraded=view.degraded,
        degraded_reasons=[_reason(notice) for notice in view.notices],
    )
