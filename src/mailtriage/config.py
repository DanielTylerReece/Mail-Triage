from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Microsoft Graph / Entra
    tenant_id: str
    client_id: str
    client_secret: SecretStr

    # Webhook
    webhook_url: str
    webhook_client_state: str
    port: int = Field(default=8088, ge=1, le=65535)

    # LLM provider
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_timeout_seconds: int = Field(default=60, ge=1, le=600)
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    # Scaling
    concurrency: int = Field(default=1, ge=1, le=64)

    # Dry-run mode. When true, the dispatcher classifies and audits as
    # normal but does NOT actually move messages. Use it for the first
    # week of operation to validate your rules and the LLM classifier
    # against real mail before flipping to live action. Set to false
    # (or unset) for normal operation.
    dry_run: bool = False

    # Retention — daily purge of audit DB rows older than this many days.
    # Set to 0 to disable retention entirely (audit grows monotonically).
    audit_retention_days: int = Field(default=90, ge=0, le=10000)

    # Paths
    config_dir: Path = Path("/app/config")
    data_dir: Path = Path("/var/lib/mailtriage")

    # ----- validators -----

    @field_validator("tenant_id", "client_id", "webhook_url",
                     "webhook_client_state", "llm_model",
                     mode="before")
    @classmethod
    def _strip_str(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("config_dir", "data_dir", mode="before")
    @classmethod
    def _expand_path(cls, v):
        if v is None:
            return v
        s = str(v).strip()
        return Path(os.path.expandvars(os.path.expanduser(s))).resolve()

    @model_validator(mode="after")
    def _check_provider_key(self):
        """Fail at startup, not at first webhook, if the chosen provider
        has no API key."""
        if self.llm_provider == "anthropic":
            key = self.anthropic_api_key.get_secret_value().strip()
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic"
                )
        elif self.llm_provider == "openai":
            key = self.openai_api_key.get_secret_value().strip()
            if not key:
                raise ValueError(
                    "OPENAI_API_KEY is required when LLM_PROVIDER=openai"
                )
        return self

    @property
    def mailboxes_file(self) -> Path:
        return self.config_dir / "mailboxes.txt"

    @property
    def rules_file(self) -> Path:
        return self.config_dir / "rules.txt"

    @property
    def categories_file(self) -> Path:
        return self.config_dir / "categories.txt"

    @property
    def classifier_prompt_file(self) -> Path:
        return self.config_dir / "classifier-prompt.md"

    @property
    def audit_db(self) -> Path:
        return self.data_dir / "audit.db"

    @property
    def subscriptions_state(self) -> Path:
        return self.data_dir / "subscriptions.json"


def load_mailboxes(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"mailboxes file not found: {path}")
    out: list[str] = []
    # utf-8-sig: silently consume a BOM left by a Windows text editor.
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "@" not in line:
            log.warning("ignoring malformed mailbox line: %r", line)
            continue
        out.append(line.lower())
    if not out:
        raise ValueError(f"mailboxes file {path} contains no usable addresses")
    return out


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
    return _settings


def reset_for_tests() -> None:
    """Test-only: forget cached settings so the next get_settings() re-reads."""
    global _settings
    _settings = None
