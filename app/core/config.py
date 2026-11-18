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
    # Durable indexing: a worker reclaims documents left PENDING (the request's own task never
    # ran, e.g. a crash) or PROCESSING past their lease (the worker died mid-document).
    indexing_worker_enabled: bool = True
    indexing_poll_s: float = Field(default=15.0, gt=0)
    indexing_lease_s: float = Field(default=600.0, ge=5)
    indexing_max_attempts: int = Field(default=3, ge=1)

    # Retrieval
    retrieval_top_k: int = Field(default=6, ge=1, le=50)
    retrieval_candidate_k: int = Field(default=30, ge=1, le=200)
    retrieval_mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    # Drop chunks below this cosine similarity unless a keyword also matched. Off by default; the
    # tuned value lives in datasets/experiments.json with the measurements that justify it.
    retrieval_min_vector_score: float | None = Field(default=None, ge=0, le=1)
    rrf_k: int = Field(default=60, ge=1)
    max_evidence_chars: int = Field(default=8000, ge=500)

    # Optimization switches (product.txt 12). All off by default so a benchmark measures each one
    # against the same code; datasets/experiments.json holds the profiles.
    agent_query_rewrite: bool = True  # small-model search queries on top of the question
    agent_search_top_k: int = Field(default=5, ge=3, le=10)  # per query in the graph's retrieval
    agent_max_evidence_passage_chars: int = Field(default=30_000, ge=500)  # total, graph only
    agent_rule_routing: bool = False  # code decides when tools are needed; forces the calculator
    agent_route_simple_to_small: bool = False  # simple lookups answered by the small model
    cache_query_embeddings: bool = False
    cache_tool_results: bool = False
    cache_ttl_s: float = Field(default=600.0, gt=0)
    cache_max_entries: int = Field(default=1024, ge=1)
    # Answer reuse for paraphrased questions: off unless explicitly enabled.
    semantic_cache_enabled: bool = False
    semantic_cache_min_similarity: float = Field(default=0.97, ge=0.8, le=1)
    semantic_cache_ttl_s: float = Field(default=3600.0, gt=0)

    # Optional reference prices (USD per 1M tokens) for a hypothetical "what would this cost hosted"
    # comparison, always labelled as such. Unset means the figure is reported as unavailable.
    reference_price_input_per_mtok: Decimal | None = Field(default=None, ge=0)
    reference_price_output_per_mtok: Decimal | None = Field(default=None, ge=0)

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
    # A pending approval with no decision for this long expires; its run ends "cancelled".
    # A streamed run whose client disconnects keeps running this long, so the client can
    # reconnect (GET /executions/{id}/stream with Last-Event-ID); 0 = cancel at once.
    sse_reconnect_grace_s: float = Field(default=30.0, ge=0, le=600)
    approval_ttl_s: float = Field(default=86_400.0, gt=0)
    # A decided approval whose run is still paused this long after the decision lost its
    # resume (process died): it goes back to pending. Must exceed the longest possible resume.
    approval_resume_grace_s: float = Field(default=1_200.0, gt=0)
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
    # "docker": every command runs in a throwaway container (no network, no capabilities,
    # read-only rootfs, resource limits). Build the image: docker/sandbox.Dockerfile.
    workspace_exec_backend: Literal["host", "docker"] = "host"
    workspace_sandbox_image: str = "agentops-sandbox:1"
    workspace_sandbox_cpus: str = "1"
    workspace_sandbox_memory: str = "512m"
    workspace_receipt_ttl_s: float = Field(default=7 * 86_400.0, ge=3600)

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
    # A run still "running" after this long lost its process (killed, machine slept) and is
    # marked failed when runs are listed or a new run starts. A full live run takes ~40 min.
    eval_stale_after_s: float = Field(default=4 * 3600, ge=600)

    # Monitoring (product.txt 13): references the drift check compares against, versioned files.
    monitoring_eval_baseline: Path = Path("datasets/baselines/golden-v1-graph-qwen3-8b-day4.json")
    monitoring_retrieval_baseline: Path = Path("datasets/baselines/retrieval-golden-v1.json")
    monitoring_thresholds: Path = Path("datasets/drift_thresholds.json")
    experiments_file: Path = Path("datasets/experiments.json")
    # Tenants allowed to run evaluations/experiments and read system-wide monitoring.
    # Comma-separated; empty outside prod = tenant_a (dev/test), empty in prod = nobody.
    admin_tenants: str = ""

    # Rate limiting (product.txt 16). In-process token buckets per tenant and route class: correct
    # for one API process (this project's deployment); several replicas would need a shared store.
    rate_limit_enabled: bool = True
    rate_limit_agent_per_min: int = Field(default=30, ge=1)  # agent runs, approvals
    rate_limit_upload_per_min: int = Field(default=60, ge=1)  # document uploads, reindex
    rate_limit_expensive_per_min: int = Field(default=6, ge=1)  # evaluations, experiments, checks
    rate_limit_workspace_per_min: int = Field(default=120, ge=1)  # workspace exec / writes

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
    def admin_tenant_ids(self) -> frozenset[str]:
        tenants = frozenset(t.strip() for t in self.admin_tenants.split(",") if t.strip())
        if not tenants and self.env != "prod":
            return frozenset({"tenant_a"})
        return tenants

    @property
    def workspace_tenant_ids(self) -> frozenset[str]:
        tenants = frozenset(t.strip() for t in self.workspace_tenants.split(",") if t.strip())
        if not tenants and self.env == "dev":
            return frozenset({"tenant_a"})  # dev demo default, like the built-in dev API keys
        return tenants


@lru_cache
def get_settings() -> Settings:
    return Settings()
