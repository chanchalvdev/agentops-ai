"""Application settings, loaded from environment variables (prefix AGENTOPS_) or a .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_DATABASE_URL = "postgresql+asyncpg://agentops:agentops@localhost:5433/agentops"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOPS_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    database_url: str = DEV_DATABASE_URL
    # "key:tenant,key2:tenant2". Required in prod; dev falls back to two well-known dev keys.
    api_keys: SecretStr = SecretStr("")

    # LLM
    llm_provider: Literal["ollama", "fake"] = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    llm_model_main: str = "qwen3:8b"
    llm_model_small: str = "qwen3:1.7b"
    llm_timeout_s: float = 120.0
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    llm_max_output_tokens: int = 1024
    # Optional OpenAI-compatible fallback (Groq, OpenRouter, OpenAI...); off unless all 3 are set.
    fallback_base_url: str | None = None
    fallback_api_key: SecretStr | None = None
    fallback_model: str | None = None

    # Embeddings. The dimension is baked into the schema; changing it needs a migration + reindex.
    embedding_provider: Literal["ollama", "fake"] = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    embedding_batch_size: int = Field(default=32, ge=1, le=256)

    # Ingestion
    max_upload_bytes: int = 10 * 1024 * 1024
    chunk_strategy: Literal["structured", "fixed"] = "structured"
    chunk_size_chars: int = Field(default=1200, ge=200, le=8000)
    chunk_overlap_chars: int = Field(default=200, ge=0, le=2000)

    # Retrieval
    retrieval_top_k: int = Field(default=6, ge=1, le=50)
    retrieval_candidate_k: int = Field(default=30, ge=1, le=200)
    rrf_k: int = Field(default=60, ge=1)
    max_evidence_chars: int = Field(default=8000, ge=500)

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_size_chars")
        if self.env == "prod":
            if not self.api_keys.get_secret_value():
                raise ValueError("AGENTOPS_API_KEYS is required when AGENTOPS_ENV=prod")
            if self.database_url == DEV_DATABASE_URL:
                raise ValueError("AGENTOPS_DATABASE_URL must be set explicitly in prod")
        return self

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.fallback_base_url and self.fallback_api_key and self.fallback_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
