"""The three promises ``docs/data-model.md`` §9.2 makes about a stored API key.

1. It is unreadable without the master key, which lives in the environment.
2. It is **bound to its row**: a ciphertext lifted from one household and pasted onto
   another does not decrypt. This is the property that makes write access to the
   database insufficient to steal a third party's billable secret, and the test that
   attempts exactly that replay is the reason this file exists.
3. Rotating ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` fails *cleanly* -- a domain error
   naming the variable and the way out, never a cryptography traceback.

No database and no network: this is the algebra of the cipher, and it must hold
before anything else in the BYOK path is worth testing.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from pydantic import SecretStr

from chaudron.domain.llm_ports import CredentialDecryptionError, ProviderNotConfigured
from chaudron.infra.crypto import (
    CREDENTIAL_ENCRYPTION_KEY_ENV_VAR,
    CredentialCipher,
    SealedCredential,
)
from tests.conftest import build_test_settings

_HOUSEHOLD = uuid.UUID(int=1)
_OTHER_HOUSEHOLD = uuid.UUID(int=2)
_CONFIG = uuid.UUID(int=10)
_OTHER_CONFIG = uuid.UUID(int=11)

#: Shaped like a real vendor key so the redaction suite has something to bite on.
_API_KEY = "sk-ant-api03-household-key-0000000000cafe"


def cipher(seed: bytes = b"A") -> CredentialCipher:
    return CredentialCipher(seed * 32)


def seal(
    box: CredentialCipher,
    *,
    household_id: uuid.UUID = _HOUSEHOLD,
    config_id: uuid.UUID = _CONFIG,
    api_key: str = _API_KEY,
) -> SealedCredential:
    stored = box.encrypt(api_key, household_id=household_id, config_id=config_id)
    return SealedCredential(
        household_id=household_id,
        config_id=config_id,
        ciphertext=stored.ciphertext,
        key_id=stored.key_id,
    )


def test_a_key_survives_the_round_trip() -> None:
    box = cipher()
    assert box.decrypt(seal(box)) == _API_KEY


def test_the_ciphertext_never_contains_the_key() -> None:
    stored = cipher().encrypt(_API_KEY, household_id=_HOUSEHOLD, config_id=_CONFIG)
    assert _API_KEY.encode() not in stored.ciphertext
    assert b"sk-ant" not in stored.ciphertext


def test_encrypting_twice_produces_different_ciphertexts() -> None:
    """A fresh nonce per write: equal keys must not be recognisable as equal."""
    box = cipher()
    first = box.encrypt(_API_KEY, household_id=_HOUSEHOLD, config_id=_CONFIG)
    second = box.encrypt(_API_KEY, household_id=_HOUSEHOLD, config_id=_CONFIG)
    assert first.ciphertext != second.ciphertext
    assert (
        box.decrypt(SealedCredential(_HOUSEHOLD, _CONFIG, second.ciphertext, second.key_id))
        == _API_KEY
    )


def test_only_the_last_four_characters_are_kept_in_clear() -> None:
    stored = cipher().encrypt(_API_KEY, household_id=_HOUSEHOLD, config_id=_CONFIG)
    assert stored.last4 == "cafe"
    assert len(stored.last4) == 4
    # What is stored in clear must not be enough to reconstruct anything else.
    assert stored.last4 not in _API_KEY[:-4]


def test_a_ciphertext_stolen_from_another_household_does_not_decrypt() -> None:
    """The replay ADR-0007 §9.2 exists to stop, attempted literally.

    An attacker with write access to the database copies the three secret columns of
    household A onto a configuration row of household B -- key id included, so the
    rotation check passes -- and asks Chaudron to use it. The AAD binds the ciphertext
    to ``(household_id, config_id)``, so GCM rejects it before releasing a byte.
    """
    box = cipher()
    victim = seal(box, household_id=_HOUSEHOLD, config_id=_CONFIG)

    replayed = SealedCredential(
        household_id=_OTHER_HOUSEHOLD,  # the thief's own household
        config_id=_CONFIG,
        ciphertext=victim.ciphertext,  # byte-for-byte the victim's row
        key_id=victim.key_id,
    )

    with pytest.raises(CredentialDecryptionError) as raised:
        box.decrypt(replayed)

    message = str(raised.value)
    assert _API_KEY not in message
    assert "does not belong to this household" in message
    assert raised.value.__cause__ is None


def test_a_ciphertext_moved_to_another_configuration_of_the_same_household_fails() -> None:
    """Both halves of the pair are authenticated, not just the household."""
    box = cipher()
    victim = seal(box, household_id=_HOUSEHOLD, config_id=_CONFIG)
    moved = SealedCredential(_HOUSEHOLD, _OTHER_CONFIG, victim.ciphertext, victim.key_id)

    with pytest.raises(CredentialDecryptionError):
        box.decrypt(moved)


def test_rotating_the_master_key_fails_cleanly_and_says_what_to_do() -> None:
    """The operator changed the podman secret; every stored key is now unreadable."""
    stored = seal(cipher(b"A"))
    rotated = cipher(b"B")

    with pytest.raises(CredentialDecryptionError) as raised:
        rotated.decrypt(stored)

    message = str(raised.value)
    assert CREDENTIAL_ENCRYPTION_KEY_ENV_VAR in message, "name the variable to look at"
    assert "enter the key again" in message.lower(), "a refusal must carry the remedy"
    assert raised.value.__cause__ is None, "no traceback may trail a credential failure"


def test_a_rotation_failure_is_a_provider_configuration_problem() -> None:
    """So the API layer answers 409 with its configuration screen, not a 500."""
    with pytest.raises(ProviderNotConfigured):
        cipher(b"B").decrypt(seal(cipher(b"A")))


def test_a_tampered_ciphertext_is_refused_rather_than_partially_decrypted() -> None:
    box = cipher()
    stored = seal(box)
    flipped = bytearray(stored.ciphertext)
    flipped[-1] ^= 0x01
    with pytest.raises(CredentialDecryptionError):
        box.decrypt(SealedCredential(_HOUSEHOLD, _CONFIG, bytes(flipped), stored.key_id))


def test_a_truncated_column_is_reported_rather_than_crashing() -> None:
    box = cipher()
    with pytest.raises(CredentialDecryptionError) as raised:
        box.decrypt(SealedCredential(_HOUSEHOLD, _CONFIG, b"too-short", box.key_id))
    assert "truncated" in str(raised.value)


def test_the_key_id_identifies_the_master_key_without_revealing_it() -> None:
    first, second = cipher(b"A"), cipher(b"A")
    assert first.key_id == second.key_id, "the same key must always get the same id"
    assert first.key_id != cipher(b"B").key_id
    assert len(first.key_id) <= 32, "the column is varchar(32)"
    assert base64.b64encode(b"A" * 32).decode() not in first.key_id


def test_the_cipher_does_not_print_its_key() -> None:
    """A settings dump, a traceback frame or a debugger must show nothing usable."""
    box = cipher()
    rendered = repr(box)
    assert "A" * 32 not in rendered
    assert box.key_id in rendered, "the id is the only thing worth showing"


def test_a_key_too_short_to_be_scrubbed_is_refused() -> None:
    """Below the redaction floor, a leak into a diagnostic could not be undone."""
    with pytest.raises(ValueError, match="at least"):
        cipher().encrypt("sk-1", household_id=_HOUSEHOLD, config_id=_CONFIG)


def test_the_cipher_is_built_from_the_validated_settings() -> None:
    settings = build_test_settings("postgresql+asyncpg://user:pass@localhost/db")
    box = CredentialCipher.from_settings(settings)
    assert box.decrypt(seal(box)) == _API_KEY


def test_a_master_key_of_the_wrong_size_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="32"):
        CredentialCipher(b"too-short")


def test_a_non_base64_master_key_is_refused_by_name() -> None:
    settings = build_test_settings("postgresql+asyncpg://user:pass@localhost/db")
    broken = settings.model_copy(update={"credential_encryption_key": SecretStr("not base64!")})
    with pytest.raises(ValueError, match=CREDENTIAL_ENCRYPTION_KEY_ENV_VAR):
        CredentialCipher.from_settings(broken)
