"""Outbound mail: what the configuration refuses at startup, and what a header cannot carry.

No network, no relay, no container. Two things are worth testing about a mail
adapter that has never been asked to send anything, and both are the reasons this
feature is safe to have at all:

* **the configuration fails fast, or it fails at the worst possible moment.** The
  first message this application ever sends is a password reset, in an incident,
  for somebody who cannot get into their account. A relay configured without a
  sender address, or with credentials on an unencrypted socket, must be refused
  when the process starts and not discovered from a bounce.
* **an address is the only attacker-chosen input that reaches a header**, so a
  bare CRLF in it is header injection with this instance's name on it.

The end-to-end behaviour -- which message goes where, and what it carries -- is in
``tests/api/test_password_reset.py``, against the real routes.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import SecretStr, ValidationError

from chaudron.config import Settings
from chaudron.domain.email_ports import (
    InvalidRecipientError,
    OutboundMessage,
    validate_recipient,
)
from chaudron.infra.email import SmtpMailer, SmtpSettings, build_mailer
from chaudron.services.account_email import RESET_TOKEN_QUERY_PARAM, reset_link

_SECRET_KEY: Final = "test-secret-key-that-is-long-enough-for-validation"
_ENCRYPTION_KEY: Final = base64.b64encode(b"0" * 32).decode()


def _settings(**overrides: Any) -> Settings:
    """A valid configuration, with the mail keys under test overridden.

    Built by calling :class:`Settings` rather than by ``model_copy``, which skips
    validation entirely -- and validation is the whole subject of this file.
    """
    base: dict[str, Any] = {
        "env": "local",
        "database_url": SecretStr("postgresql+asyncpg://u:p@localhost:5432/chaudron"),
        "secret_key": SecretStr(_SECRET_KEY),
        "credential_encryption_key": SecretStr(_ENCRYPTION_KEY),
    }
    return Settings(**(base | overrides))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_mail_is_optional_and_absent_by_default() -> None:
    """ "No relay" is a normal configuration for self-hosted software, not an error."""
    settings = _settings()
    assert settings.email_enabled is False
    assert build_mailer(settings) is None


def test_a_relay_without_a_sender_address_is_refused_at_startup() -> None:
    with pytest.raises(ValidationError, match="CHAUDRON_SMTP_FROM is required"):
        _settings(smtp_host="relay.example.test")


def test_a_sender_address_carrying_a_newline_is_refused() -> None:
    """``From`` is a header; a newline in it appends headers of its own."""
    with pytest.raises(ValidationError, match="control characters"):
        _settings(
            smtp_host="relay.example.test",
            smtp_from="chaudron@example.test\r\nBcc: everyone@example.test",
        )


def test_a_sender_that_is_not_an_address_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be an email address"):
        _settings(smtp_host="relay.example.test", smtp_from="chaudron")


def test_credentials_on_a_clear_connection_are_refused() -> None:
    """SMTP ``AUTH`` is base64, and the same socket then carries the reset link."""
    with pytest.raises(ValidationError, match="cannot be 'none'"):
        _settings(
            smtp_host="relay.example.test",
            smtp_from="chaudron@example.test",
            smtp_security="none",
            smtp_username="chaudron",
            smtp_password=SecretStr("hunter2hunter2"),
        )


def test_a_username_without_a_password_is_refused_at_startup() -> None:
    """The quiet middle, made loud.

    Left alone, this configuration starts, advertises password reset as
    available, answers ``202``, skips ``login()`` because
    ``infra/email/smtp.py`` gates it on both credentials, and has the relay
    reject the message on a worker thread long after the response. The operator
    sees one warning naming an exception class. Every reset mail is dropped while
    the interface says it was sent.

    It was easy to reach: ``.env.example`` tells the operator to put the password
    in the podman secret ``chaudron-smtp-password``, and no quadlet mounted it.
    """
    with pytest.raises(ValidationError, match="CHAUDRON_SMTP_PASSWORD is empty"):
        _settings(
            smtp_host="relay.example.test",
            smtp_from="chaudron@example.test",
            smtp_username="chaudron",
        )


def test_a_blank_password_is_refused_exactly_as_an_absent_one() -> None:
    """The shape the deployment actually produces.

    An unmounted secret leaves ``CHAUDRON_SMTP_PASSWORD=`` in the environment
    file, and ``_drop_blank_values`` turns that into ``None`` before any
    validator runs -- so the refusal has to catch ``None``, which is what makes
    this case and the one above the same case.
    """
    with pytest.raises(ValidationError, match="CHAUDRON_SMTP_PASSWORD is empty"):
        _settings(
            smtp_host="relay.example.test",
            smtp_from="chaudron@example.test",
            smtp_username="chaudron",
            smtp_password="   ",
        )


def test_an_anonymous_relay_needs_no_password() -> None:
    """The refusal is about the *combination*, not about requiring a password.

    A relay with neither credential is a normal self-hosted arrangement -- a
    local submission service that authenticates by network position -- and must
    keep working.
    """
    settings = _settings(smtp_host="relay.example.test", smtp_from="chaudron@example.test")

    assert settings.email_enabled is True
    assert settings.smtp_username is None
    assert build_mailer(settings) is not None


def test_credentials_on_a_clear_loopback_connection_are_allowed() -> None:
    """There is no network for anything to be read from, and it is a common setup."""
    settings = _settings(
        smtp_host="localhost",
        smtp_from="chaudron@example.test",
        smtp_security="none",
        smtp_username="chaudron",
        smtp_password=SecretStr("hunter2hunter2"),
        smtp_port=25,
    )
    assert settings.email_enabled is True


def test_a_production_instance_refuses_to_mail_plain_http_reset_links() -> None:
    """A reset token is a credential; the same rule the session cookie already gets."""
    with pytest.raises(ValidationError, match="must be an https:// URL in production"):
        _settings(
            env="production",
            base_url="https://chaudron.example.test",
            public_app_url="http://app.example.test",
            smtp_host="relay.example.test",
            smtp_from="chaudron@example.test",
        )


def test_a_production_instance_with_https_links_starts() -> None:
    settings = _settings(
        env="production",
        base_url="https://api.example.test",
        public_app_url="https://app.example.test/",
        smtp_host="relay.example.test",
        smtp_from="chaudron@example.test",
    )
    assert settings.app_url == "https://app.example.test", "the trailing slash is normalised away"


def test_the_link_base_falls_back_to_the_api_url() -> None:
    """The same-origin deployment, which is the ordinary one, configures nothing."""
    settings = _settings(base_url="https://chaudron.example.test/")
    assert settings.app_url == "https://chaudron.example.test"


def test_a_configured_relay_produces_a_mailer_carrying_the_settings() -> None:
    settings = _settings(
        smtp_host="relay.example.test",
        smtp_port=465,
        smtp_security="implicit-tls",
        smtp_from="chaudron@example.test",
        smtp_username="chaudron",
        smtp_password=SecretStr("hunter2hunter2"),
    )
    mailer = build_mailer(settings)
    assert isinstance(mailer, SmtpMailer)


# --------------------------------------------------------------------------- #
# Header injection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("victim@example.test\r\nBcc: everyone@example.test", id="crlf"),
        pytest.param("victim@example.test\nBcc: everyone@example.test", id="lf"),
        pytest.param("victim@example.test\x00", id="nul"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("no-at-sign", id="no-at"),
        pytest.param("@example.test", id="no-local-part"),
        pytest.param("victim@", id="no-domain"),
        pytest.param("a" * 310 + "@example.test", id="oversized"),
    ],
)
def test_an_address_that_could_not_be_a_header_is_refused(address: str) -> None:
    with pytest.raises(InvalidRecipientError):
        validate_recipient(address)


def test_the_refusal_does_not_quote_the_offending_value() -> None:
    """The message is built by a caller into a log line; echoing the CRLF spreads it."""
    with pytest.raises(InvalidRecipientError) as caught:
        validate_recipient("victim@example.test\r\nBcc: everyone@example.test")
    assert "everyone" not in str(caught.value)
    assert "\r" not in str(caught.value)
    assert "\n" not in str(caught.value)


def test_an_ordinary_address_survives_untouched() -> None:
    assert validate_recipient("  Owner@Example.test  ") == "Owner@Example.test"


def test_a_composed_message_has_exactly_one_recipient_and_no_html() -> None:
    """Composition only. Nothing here opens a socket.

    ``_compose`` is private, and reaching for it is deliberate: this asserts the
    envelope, which is the part an injected address would corrupt, without the
    ``_deliver`` half that would need a relay.
    """
    mailer = SmtpMailer(
        SmtpSettings(host="relay.example.test", port=587, sender="chaudron@example.test")
    )
    envelope = mailer._compose(
        "owner@example.test",
        OutboundMessage(to="owner@example.test", subject="Sujet", body="Corps"),
    )

    assert envelope.get_all("To") == ["owner@example.test"]
    assert envelope.get_all("Bcc") is None
    assert envelope.get_all("From") == ["chaudron@example.test"]
    assert envelope.get_content_type() == "text/plain"
    assert not envelope.is_multipart(), "no HTML alternative, and therefore no renderer"
    assert "Corps" in envelope.get_content()


def test_a_reset_link_keeps_the_path_the_interface_is_served_under() -> None:
    """`CHAUDRON_PUBLIC_APP_URL` may carry a path, and the link must not eat it.

    The deployment this repository ships serves the application under ``/app/``
    and a static landing page at the root. That page carries no bundle -- it is
    deliberately free of JavaScript so that it renders even when the application
    build is broken -- so a reset link pointing at the root opens a document that
    cannot read the token, and the only account-recovery path there is does
    nothing at all. Silently: the user sees a marketing page and assumes they
    misread the mail.

    Nothing asserted this until the link was already wrong in the documented
    deployment. `reset_link` never mishandled the path; what failed is that no
    test said the path was load-bearing, so the value it depends on stayed
    described as an optional override in `.env.example`.
    """
    link = reset_link("https://chaudron.example.test/app", "a" * 64)

    assert link.startswith("https://chaudron.example.test/app/?"), link
    assert f"{RESET_TOKEN_QUERY_PARAM}={'a' * 64}" in link


def test_the_shipped_reverse_proxy_redacts_the_reset_token_from_its_access_log() -> None:
    """The parameter carrying a live credential must be dropped by name.

    A cross-file assertion, and worth the awkwardness. `ops/Caddyfile.example`
    filters `gtin`, `barcode`, `code` and `token` out of the logged query string
    -- a list written when `token` was the only credential that travelled that
    way. The reset flow arrived afterwards under a *different* name, and nobody
    re-read the list: every click on a reset link wrote a working token into
    journald, where the retention is thirty days against the token's one hour.

    Keyed on :data:`RESET_TOKEN_QUERY_PARAM` rather than on the literal, so
    renaming the parameter fails here instead of quietly un-redacting it. That is
    the whole point: the defect was not a wrong filter, it was a filter and a
    parameter name that nothing held together.
    """
    caddyfile = Path(__file__).resolve().parents[3] / "ops" / "Caddyfile.example"
    text = caddyfile.read_text(encoding="utf-8")

    assert f"delete {RESET_TOKEN_QUERY_PARAM}" in text, (
        f"ops/Caddyfile.example does not drop {RESET_TOKEN_QUERY_PARAM!r} from its "
        "access log, so a live password-reset token is written to disk on every click"
    )
