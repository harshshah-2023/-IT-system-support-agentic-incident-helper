"""Central, typed configuration. Secrets only ever come from the environment or `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

# LangChain and LangSmith read LANGSMITH_* straight from the process environment.
load_dotenv(ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # Provider credentials: comma-separated values enable key rotation (see gateway.keys).
    openai_api_key: SecretStr | None = None
    openai_fallback_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None

    fast_model: str = "openai:gpt-4o-mini"
    reasoning_model: str = "openai:gpt-4o"
    fallback_model: str = "openai:gpt-4o-mini"
    judge_model: str = "openai:gpt-4o-mini"
    llm_timeout_seconds: float = 60.0

    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "incident-helper"

    runbook_path: Path = Path("data/runbook/larkspur_runbook.pdf")
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    rag_score_threshold: float = 0.62
    rag_top_k: int = 8
    rag_chunk_size: int = 900
    rag_chunk_overlap: int = 150

    cache_ttl_seconds: int = 900
    semantic_cache_threshold: float = 0.93
    gateway_chaos: str = "none"

    oncall_engineers: str = "asha.menon,daniel.okafor,mei.tanaka"
    # Each tool round costs four graph steps here (agent, guard, tools, output guard).
    guarded_recursion_limit: int = 60

    var_dir: Path = Field(default=ROOT / "var")
    output_dir: Path = Field(default=ROOT / "output")

    @field_validator("runbook_path")
    @classmethod
    def _absolute_runbook(cls, value: Path) -> Path:
        return value if value.is_absolute() else ROOT / value

    @property
    def oncall(self) -> frozenset[str]:
        return frozenset(name.strip() for name in self.oncall_engineers.split(",") if name.strip())

    @property
    def ops_db(self) -> Path:
        return self.var_dir / "ops.sqlite"

    @property
    def checkpoint_db(self) -> Path:
        return self.var_dir / "checkpoints.sqlite"

    @property
    def index_dir(self) -> Path:
        return self.var_dir / "index"

    @property
    def llm_configured(self) -> bool:
        """True when the provider of the fast model has a credential (local Ollama needs none)."""
        provider = self.fast_model.split(":", 1)[0]
        return provider == "ollama" or bool(self.key_for(provider))

    def key_for(self, provider: str) -> str:
        secret: SecretStr | None = getattr(self, f"{provider}_api_key", None)
        return secret.get_secret_value() if secret else ""

    def ensure_dirs(self) -> None:
        for path in (self.var_dir, self.output_dir, self.index_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
