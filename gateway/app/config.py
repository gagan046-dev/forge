"""Gateway settings, read from FORGE_* environment variables (SPEC 2.1)."""

from __future__ import annotations

import json
from functools import cached_property, lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelRole = Literal["chat", "autocomplete", "embedding"]


class ModelSpec(BaseModel):
    """One entry of gitops/models.yaml, delivered to the gateway as FORGE_MODELS_JSON."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    alias: str
    name: str
    role: ModelRole
    context_length: int | None = Field(default=None, alias="contextLength")
    dimensions: int | None = None


# Mirrors gitops/models.yaml so a local `uvicorn` run works without extra setup.
DEFAULT_MODELS_JSON = json.dumps(
    [
        {"alias": "forge-chat", "name": "qwen2.5-coder:7b", "role": "chat", "contextLength": 8192},
        {
            "alias": "forge-autocomplete",
            "name": "qwen2.5-coder:1.5b-base",
            "role": "autocomplete",
            "contextLength": 4096,
        },
        {"alias": "forge-embed", "name": "nomic-embed-text:v1.5", "role": "embedding", "dimensions": 768},
    ]
)

_models_adapter = TypeAdapter(list[ModelSpec])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FORGE_", extra="ignore")

    database_url: str = "postgresql+asyncpg://forge:forge@localhost:5432/forge"
    redis_url: str = "redis://localhost:6379/0"
    ollama_url: str = "http://ollama.forge-inference:11434"
    models_json: str = DEFAULT_MODELS_JSON
    env: Literal["dev", "prod"] = "dev"

    # No defaults on purpose: a gateway must never run with a guessable admin password or session key.
    admin_password: SecretStr = Field(min_length=8)
    session_secret: SecretStr = Field(min_length=16)

    max_inflight: int = Field(default=2, ge=1)
    max_queue: int = Field(default=32, ge=1)
    queue_timeout_s: float = Field(default=120, gt=0)
    wake_timeout_s: float = Field(default=240, gt=0)
    idle_seconds: int = Field(default=600, ge=0)
    gpu_exporter_url: str = "http://gpu-exporter.forge-inference:9400"
    default_rpm: int = Field(default=120, ge=1)
    default_daily_tokens: int = Field(default=2_000_000, ge=1)
    # Comma-separated host list; use `rag_allowed_hosts_list` for the parsed form.
    rag_allowed_hosts: str = "github.com,gitlab.com,codeberg.org"
    log_level: str = "INFO"

    @cached_property
    def models(self) -> list[ModelSpec]:
        return _models_adapter.validate_json(self.models_json)

    @property
    def rag_allowed_hosts_list(self) -> list[str]:
        return [h.strip().lower() for h in self.rag_allowed_hosts.split(",") if h.strip()]

    @property
    def ollama_base(self) -> str:
        return self.ollama_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
