"""Pydantic configuration schema for the eCode360 plugin.

Secrets (the API key/secret pair) are NEVER read from config.yaml, because the
deploy script bakes config.yaml into the Lambda zip. They are resolved at
runtime from environment variables (see ``EcodePlugin.initialize``):

* On AWS Lambda, Terraform injects ``ECODE_API_KEY`` / ``ECODE_API_SECRET`` as
  (sensitive) Lambda environment variables.
* For local development, point ``secrets_file`` at a gitignored env file
  (``KEY=VALUE`` per line) and the plugin loads it into the environment.
"""

from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EcodePluginConfig(BaseModel):
    """Configuration schema for the eCode360 municipal code plugin."""

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    base_url: str = Field(
        default="https://api.ecode360.com",
        description="Base URL of the eCode360 (EcodeGateway) API",
    )
    customer_id: str = Field(
        ...,
        description=(
            "eCode360 customer/library ID identifying the municipality whose code "
            "this server serves (e.g. 'AN6998' for the Municipality of Anchorage)."
        ),
    )
    city_name: str = Field(..., description="Name of the municipality/organization")
    timeout: int = Field(
        default=60, ge=1, le=300, description="HTTP request timeout in seconds"
    )

    # ── Secret resolution (never the secret values themselves) ──────────
    api_key_env: str = Field(
        default="ECODE_API_KEY",
        description="Name of the environment variable holding the API key",
    )
    api_secret_env: str = Field(
        default="ECODE_API_SECRET",
        description="Name of the environment variable holding the API secret",
    )
    secrets_file: Optional[str] = Field(
        default=None,
        description=(
            "Optional path to a gitignored KEY=VALUE env file for LOCAL dev. "
            "Loaded into os.environ at init if the env vars are not already set. "
            "Leave unset on Lambda (secrets come from Lambda env vars)."
        ),
    )

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v:
            raise ValueError("base_url cannot be empty")
        result = urlparse(v)
        if not result.scheme or not result.netloc:
            raise ValueError("base_url must include scheme (http/https) and hostname")
        if result.scheme not in ("http", "https"):
            raise ValueError("base_url scheme must be http or https")
        return v.rstrip("/")

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("customer_id cannot be empty")
        if not v.isalnum():
            raise ValueError(
                "customer_id must be alphanumeric (e.g. 'AN6998'); got: " + repr(v)
            )
        return v

    model_config = ConfigDict(extra="forbid")
