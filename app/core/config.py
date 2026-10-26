"""Application settings, loaded from environment variables (prefix AGENTOPS_) or a .env file."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
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
    # Context window requested from Ollama. 8k fits qwen3:8b comfortably in 16 GB unified memory.
    llm_num_ctx: int = Field(default=8192, ge=2048, le=40960)
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

    # Agent execution limits (defaults for ExecutionLimits; a request may only tighten them)
    agent_max_steps: int = Field(default=8, ge=1, le=50)
    agent_max_graph_steps: int = Field(default=20, ge=1, le=100)
    agent_max_model_calls: int = Field(default=12, ge=1, le=100)
    agent_max_tool_calls: int = Field(default=10, ge=0, le=100)
    agent_max_corrections: int = Field(default=2, ge=0, le=5)
    agent_max_total_tokens: int = Field(default=48_000, ge=1)
    agent_max_cost_usd: Decimal | None = Field(
        default=None, gt=0
    )  # None = no monetary cap (local models cost $0)
    agent_deadline_s: float = Field(default=180.0, gt=0, le=900)
    # Where paused graph runs are persisted. "memory" loses pending approvals on restart.
    agent_checkpointer: Literal["postgres", "memory"] = "postgres"
    # History budget for the agent prompt (~4 chars/token), leaving room for tools and output.
    agent_max_context_chars: int = Field(default=20_000, ge=2000)

    # Sandboxed workspace (product.txt 9.1): each tenant may only touch <workspace_root>/<tenant>/.
    # Use a disposable directory, never your home directory or a real repository.
    workspace_root: Path = Path("workspace")
    # Comma-separated tenants granted workspace:read/write/exec. Empty outside prod = tenant_a.
    workspace_tenants: str = ""
    workspace_max_read_bytes: int = Field(default=20_000, ge=1000, le=1_000_000)
    workspace_max_write_bytes: int = Field(default=200_000, ge=1000, le=5_000_000)
    workspace_exec_timeout_s: float = Field(default=60.0, gt=0, le=600)
    workspace_max_output_bytes: int = Field(default=64_000, ge=1000, le=1_000_000)

    # Observability (product.txt 10). Traces always go to the local log sink; hosted backends are
    # optional and the app runs without them. Enabling one sends questions, answers, tool
    # arguments (minimized), model names, and timings to that vendor; never document text.
    telemetry_log: bool = True
    telemetry_capture_io: bool = True  # include question/answer text in hosted traces
    telemetry_timeout_s: float = Field(default=10.0, gt=0, le=60)
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    langsmith_api_key: SecretStr | None = None
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "agentops-ai"
    # "eval": only evaluation runs go to LangSmith (compare vendors on the eval subset only).
    langsmith_scope: Literal["eval", "all"] = "eval"

    # Evaluation (product.txt 11). Cases run as their own tenant against a known corpus.
    eval_tenant: str = Field(default="eval", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    eval_datasets_dir: Path = Path("datasets")
    eval_corpus_dirs: list[Path] = [Path("datasets/corpus")]
    eval_workspace_seed: Path = Path("datasets/eval_workspace")

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_size_chars")
        root = self.workspace_root.expanduser().resolve()
        if root in (Path("/"), Path.home().resolve()) or (root / ".git").exists():
            raise ValueError(
                "AGENTOPS_WORKSPACE_ROOT must be a dedicated directory, not / , your home "
                "directory, or a git repository"
            )
        if self.env == "prod":
            if not self.api_keys.get_secret_value():
                raise ValueError("AGENTOPS_API_KEYS is required when AGENTOPS_ENV=prod")
            if self.database_url == DEV_DATABASE_URL:
                raise ValueError("AGENTOPS_DATABASE_URL must be set explicitly in prod")
        return self

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.fallback_base_url and self.fallback_api_key and self.fallback_model)

    @property
    def workspace_tenant_ids(self) -> frozenset[str]:
        tenants = frozenset(t.strip() for t in self.workspace_tenants.split(",") if t.strip())
        if not tenants and self.env == "dev":
            return frozenset({"tenant_a"})  # dev demo default, like the built-in dev API keys
        return tenants


@lru_cache
def get_settings() -> Settings:
    return Settings()
