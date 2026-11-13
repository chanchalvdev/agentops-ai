"""Experiment profiles: named, versioned sets of setting overrides (datasets/experiments.json).

Only settings that change how the agent retrieves, routes, caches, or chunks may be overridden.
Anything else (database, API keys, workspace root, limits) is rejected at load time, so a profile
can never widen what the agent is allowed to do, and the API only ever accepts a profile *name*.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.config import Settings
from app.core.errors import InvalidRequestError, NotFoundError

OVERRIDABLE = frozenset(
    {
        "agent_query_rewrite",
        "agent_search_top_k",
        "agent_max_evidence_passage_chars",
        "agent_rule_routing",
        "agent_route_simple_to_small",
        "cache_query_embeddings",
        "cache_tool_results",
        "cache_ttl_s",
        "semantic_cache_enabled",
        "semantic_cache_min_similarity",
        "retrieval_mode",
        "retrieval_min_vector_score",
        "retrieval_candidate_k",
        "rrf_k",
        "chunk_strategy",
        "chunk_size_chars",
        "chunk_overlap_chars",
        "llm_model_main",
        "llm_model_small",
        # A profile that changes chunking must use its own evaluation tenant: ingestion is
        # idempotent by content, so the default tenant would keep the old chunks.
        "eval_tenant",
    }
)
CHUNKING = frozenset({"chunk_strategy", "chunk_size_chars", "chunk_overlap_chars"})


class Profile(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    description: str
    purpose: str = "benchmark"  # benchmark | regression-demo | probe | rejected
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _allowed(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - OVERRIDABLE
        if unknown:
            raise ValueError(f"settings not overridable by a profile: {sorted(unknown)}")
        if set(value) & CHUNKING and "eval_tenant" not in value:
            raise ValueError("a profile that changes chunking must set its own eval_tenant")
        return value


class ExperimentFile(BaseModel):
    version: str
    profiles: list[Profile]


def load_profiles(path: Path) -> dict[str, Profile]:
    if not path.is_file():
        raise NotFoundError("Experiments file not found")
    try:
        data = ExperimentFile.model_validate(json.loads(path.read_text()))
    except ValueError as exc:
        raise InvalidRequestError(f"{path.name}: invalid experiments file: {exc}") from None
    names = [p.name for p in data.profiles]
    if len(names) != len(set(names)):
        raise InvalidRequestError(f"{path.name}: duplicate profile names")
    return {p.name: p for p in data.profiles}


def apply(settings: Settings, profile: Profile) -> Settings:
    """A validated copy of `settings` with the profile's overrides (validators run again)."""
    return Settings.model_validate({**settings.model_dump(), **profile.overrides})
