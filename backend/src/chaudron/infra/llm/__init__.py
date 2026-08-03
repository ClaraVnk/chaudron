"""Model-provider adapters (ADR-0005) and their per-household wiring (ADR-0007).

Layout:

* :mod:`chaudron.infra.llm.base` -- the provider-independent half: degradation,
  bounded retry, and the two port implementations every provider shares.
* ``*_provider`` / :mod:`chaudron.infra.llm.openai_compatible` -- one transport per
  vendor: its wire format, its capability table, and its failure translation.
* :mod:`chaudron.infra.llm.http` -- the SSRF guard and the bounded HTTP client.
* :mod:`chaudron.infra.llm.factory` -- household configuration to port.
* :mod:`chaudron.infra.llm.contract` -- the conformance-suite registry.

Nothing in this package is imported by the domain, and nothing in it returns a
vendor object to a caller.
"""

from __future__ import annotations
