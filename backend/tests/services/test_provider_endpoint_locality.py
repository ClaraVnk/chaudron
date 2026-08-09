"""Where an Ollama endpoint actually is, and what the consent gate does about it.

The penetration test of 2026-08-04 left one design note for whoever built the
provider configuration route, and this file is that note held as a test:

    The consent gate exempts ``mode = 'ollama'`` on the reasoning that the model
    runs on a machine the household controls. But ``base_url`` is a per-row column,
    constrained only by the operator's ``ollama_allowed_hosts``. An operator who
    allowlists a hosted Ollama produces a configuration that transmits health data
    to a third party **with the consent gate disabled by mode**. […] the exemption
    should key on the endpoint being loopback or RFC 1918, not on the enum.

No database here: :func:`endpoint_is_local` is a pure function over a string and a
resolver, and the resolver is the only thing a test has to supply. The gate that
consumes it is exercised against real rows over HTTP in
``tests/api/test_provider_configuration.py``.

The resolvers below are doubles rather than the system one on purpose. A test that
asked the operating system what ``ollama`` resolves to would pass or fail according
to whose laptop it ran on, and the property under test -- "a name pointing at a
public address is not exempt" -- is exactly the one a developer's local DNS would
hide.
"""

from __future__ import annotations

import pytest

from chaudron.domain.llm_ports import ProviderUnavailable
from chaudron.infra.llm.http import Resolver
from chaudron.services.providers import endpoint_is_local


def answering(*addresses: str) -> Resolver:
    """A resolver that always returns *addresses*, whatever it is asked."""

    async def resolve(host: str, port: int) -> frozenset[str]:
        return frozenset(addresses)

    return resolve


async def refusing(host: str, port: int) -> frozenset[str]:
    """What ``system_resolver`` does with a name nothing answers for."""
    raise ProviderUnavailable(f"host {host!r} could not be resolved")


async def never_called(host: str, port: int) -> frozenset[str]:  # pragma: no cover - asserted below
    raise AssertionError("a literal address must be classified without a lookup")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:11434",
        "http://127.1.2.3:11434",
        "http://[::1]:11434",
        "http://10.89.0.7:11434",
        "http://192.168.1.40:11434",
        "http://172.16.4.4:11434",
        "http://169.254.10.10:11434",
        "http://[fd00::1]:11434",
        "https://192.168.1.40",
        # Reserved for documentation (RFC 5737, RFC 3849). Not RFC 1918, and
        # deliberately treated the same way: the test is whether a packet can leave
        # for somebody else's data centre, and one addressed here cannot -- no
        # network on the internet announces these. The predicate is "not globally
        # routable", of which RFC 1918 is the common case rather than the whole.
        "http://203.0.113.7:11434",
        "http://[2001:db8:1::1]:11434",
        # RFC 6761 reserves these for the loopback interface; a name server that
        # said otherwise would be misconfigured or hostile, and either way its
        # answer must not widen the exemption.
        "http://localhost:11434",
        "http://ollama.localhost:11434",
    ],
)
async def test_an_endpoint_on_a_network_somebody_here_owns_is_local(base_url: str) -> None:
    assert await endpoint_is_local(base_url, never_called) is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://93.184.216.34:11434",
        "http://[2606:4700::1111]:11434",
        "https://8.8.8.8",
    ],
)
async def test_a_publicly_routable_endpoint_is_not_local(base_url: str) -> None:
    """The finding, in one line: an Ollama can be somebody else's data centre."""
    assert await endpoint_is_local(base_url, never_called) is False


async def test_a_hosted_ollama_named_rather_than_addressed_is_not_local() -> None:
    """The exact row the penetration test described: same mode, a third party."""
    assert await endpoint_is_local("https://ollama.fly.dev", answering("35.1.2.3")) is False


async def test_a_container_name_is_resolved_rather_than_guessed_at() -> None:
    """The documented ADR-0007 topology must keep its exemption.

    ``http://ollama:11434`` is a Podman service name, not an address, and it points
    at an RFC 1918 address on the instance's own network. Refusing to look would
    demand an agreement for a configuration that genuinely transmits to nobody --
    which is the requirement ``docs/security-model.md`` §8.3 states in the same row
    that demands consent everywhere else.
    """
    assert await endpoint_is_local("http://ollama:11434", answering("10.89.0.14")) is True


async def test_a_name_pointing_anywhere_public_is_not_local() -> None:
    """The half the enum could not see: same mode, same protocol, a third party."""
    assert (
        await endpoint_is_local("https://ollama.example.com", answering("93.184.216.34")) is False
    )


async def test_one_public_address_among_several_is_enough_to_lose_the_exemption() -> None:
    """A name that can send the request either way sends it either way.

    ``all`` rather than ``any``, for the same reason ``assert_stable_resolution``
    requires a subset rather than an intersection: a set that is partly authorised
    is a set the socket may still take the wrong element of.
    """
    assert (
        await endpoint_is_local("http://ollama:11434", answering("10.89.0.14", "1.2.3.4")) is False
    )


async def test_an_unresolvable_host_is_not_local() -> None:
    """Fail closed. An endpoint nobody can resolve is one nobody can show to be local."""
    assert await endpoint_is_local("http://ollama:11434", refusing) is False


async def test_an_empty_answer_is_not_local() -> None:
    """``all()`` of nothing is ``True``, which would be the exemption granted by a
    resolver that answered with nothing at all."""
    assert await endpoint_is_local("http://ollama:11434", answering()) is False


async def test_a_scoped_link_local_answer_is_still_classified() -> None:
    """``fe80::1%eth0`` is a real ``getaddrinfo`` answer and ``ipaddress`` reads it.

    Asserted rather than assumed: ``infra/llm/http.py`` refuses to *dial* a scoped
    address, and it would be easy to conclude from that neighbour that this
    classifier cannot read one either -- and then to "fix" it into a refusal that
    demanded a consent for a link-local endpoint.
    """
    assert await endpoint_is_local("http://ollama:11434", answering("fe80::1%eth0")) is True


async def test_an_answer_that_is_not_an_address_at_all_is_not_local() -> None:
    """Whatever a resolver returns, an answer this cannot classify grants nothing.

    Unreachable through ``system_resolver``, which returns numeric forms. The branch
    is kept for the reason ``_dial_order`` keeps its own: a set silently narrowed by
    dropping what it could not read would be judged on something other than the
    answer.
    """
    assert await endpoint_is_local("http://ollama:11434", answering("ollama.internal")) is False


@pytest.mark.parametrize(
    "base_url",
    [None, "", "   ", "not a url at all", "http://", "http://ollama:not-a-port"],
)
async def test_a_url_that_cannot_be_read_is_not_local(base_url: str | None) -> None:
    """Every parse failure is an absent exemption, never a granted one."""
    assert await endpoint_is_local(base_url, never_called) is False
