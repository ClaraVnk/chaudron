"""Application settings, read from the environment once and validated at startup.

Every key documented in ``.env.example`` is declared here, under the
``CHAUDRON_`` prefix except for the four provider keys that keep their vendor
name (``ANTHROPIC_API_KEY`` and friends) because that is what the SDKs read.

Two behaviours are deliberate and load-bearing.

*Empty means unset.* ``.env.example`` ships every key with an empty value, and a
copied-then-partially-filled file is the normal case. A blank value therefore
falls back to the declared default instead of failing on ``int("")`` -- but a
blank value on a **required** field still fails, with "field required" rather
than a type error nobody can act on.

*Fail-fast.* :func:`get_settings` raises on the first invalid value. Nothing in
this module degrades gracefully: an instance that cannot decrypt household
credentials or cannot reach its database must refuse to start rather than serve
half of its endpoints (``docs/architecture.md`` section 5).
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated, Any, Final, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: AES-256-GCM, as documented on ``LlmProviderConfig.api_key_ciphertext``.
CREDENTIAL_KEY_BYTES: Final = 32

#: Short enough to be brute-forced offline if it ever signs a token.
MIN_SECRET_KEY_LENGTH: Final = 32

Environment = Literal["local", "ci", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

#: Comma-separated env values, decoded by a validator instead of by
#: pydantic-settings' JSON decoder -- which would reject ``a,b`` outright.
CommaSeparated = Annotated[list[str], NoDecode]


class ConfigurationError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


class Settings(BaseSettings):
    """The whole configuration surface of the backend."""

    model_config = SettingsConfigDict(
        env_prefix="CHAUDRON_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Unknown CHAUDRON_* variables are a typo far more often than a feature.
        extra="ignore",
        frozen=True,
    )

    # -- Application ------------------------------------------------------- #
    env: Environment = "local"
    log_level: LogLevel = "INFO"
    port: int = Field(default=8000, ge=1, le=65535)
    base_url: str = "http://localhost:8000"

    # -- Database ---------------------------------------------------------- #
    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)

    # -- Security ---------------------------------------------------------- #
    secret_key: SecretStr
    jwt_ttl_minutes: int = Field(default=60, ge=1)
    jwt_algorithm: str = "HS256"

    credential_encryption_key: SecretStr

    # -- instance_owner mode ----------------------------------------------- #
    instance_owner_household_id: str | None = None
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    mistral_api_key: SecretStr | None = Field(default=None, validation_alias="MISTRAL_API_KEY")

    llm_default_model: str = "claude-opus-5"
    llm_max_tokens: int = Field(default=4096, ge=1)
    llm_monthly_budget_usd: float = Field(default=0.0, ge=0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)

    # -- Ollama ------------------------------------------------------------- #
    ollama_allowed_hosts: CommaSeparated = Field(default_factory=list)
    ollama_timeout_seconds: float = Field(default=300.0, gt=0)

    # -- Open Food Facts ---------------------------------------------------- #
    # Their policy asks for an honest caller identity; the repository URL is the
    # contact point, so the default is usable rather than merely polite.
    off_user_agent: str = "Chaudron/0.1.0 (+https://github.com/ClaraVnk/chaudron)"
    off_base_url: str = "https://world.openfoodfacts.org"
    off_cache_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=0)

    # -- Inbound email ------------------------------------------------------ #
    inbound_email_webhook_key: SecretStr | None = None
    inbound_email_domain: str | None = None
    inbound_email_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)

    # -- CORS --------------------------------------------------------------- #
    cors_origins: CommaSeparated = Field(default_factory=list)
    cors_allow_credentials: bool = False

    # -- Normalisation and cross-field rules -------------------------------- #

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, data: Any) -> Any:
        """Treat a blank environment variable as an absent one.

        Applied before anything else so that defaults still apply and required
        fields still report themselves as missing.
        """
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if not (isinstance(value, str) and not value.strip())
        }

    @field_validator("ollama_allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_postgresql(cls, value: SecretStr) -> SecretStr:
        """PostgreSQL with the async driver, and nothing else (ADR-0003).

        Named explicitly rather than left to a connection error at first query:
        "no SQLite mode" is a decision, and a decision deserves a message.
        """
        url = value.get_secret_value()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "must be a postgresql+asyncpg:// DSN; Chaudron runs on PostgreSQL only "
                "and the application layer is async (ADR-0003)"
            )
        return value

    @field_validator("secret_key")
    @classmethod
    def _require_long_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_KEY_LENGTH} characters "
                "(generate one with `openssl rand -hex 32`)"
            )
        return value

    @field_validator("credential_encryption_key")
    @classmethod
    def _require_aes256_key(cls, value: SecretStr) -> SecretStr:
        """Reject at startup what would otherwise fail on the first key rotation.

        A key of the wrong size does not break anything until a household stores
        a provider credential -- days later, in a code path nobody is watching.
        """
        try:
            raw = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "must be base64-encoded (generate one with `openssl rand -base64 32`)"
            ) from exc
        if len(raw) != CREDENTIAL_KEY_BYTES:
            raise ValueError(
                f"must decode to exactly {CREDENTIAL_KEY_BYTES} bytes for AES-256-GCM, "
                f"got {len(raw)}"
            )
        return value

    @model_validator(mode="after")
    def _reject_wildcard_with_credentials(self) -> Settings:
        """``Access-Control-Allow-Origin: *`` plus credentials is not a valid pair.

        Browsers refuse the combination, so the practical outcome of configuring
        it is a CORS failure nobody can explain from the server logs.
        """
        if self.cors_allow_credentials and "*" in self.cors_origins:
            raise ValueError(
                "CHAUDRON_CORS_ALLOW_CREDENTIALS cannot be true while "
                "CHAUDRON_CORS_ORIGINS contains '*': list the origins explicitly"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read and validate the configuration once per process.

    The ``ValidationError`` is re-raised as a :class:`ConfigurationError` so the
    startup path has one exception type to report, and so the message never
    carries the offending *values* -- an invalid secret is still a secret.
    """
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"invalid Chaudron configuration: {problems}") from None
