"""Graph optimization switches (product.txt 12), resolved once from settings.

Every switch defaults to the Day 3 behaviour, so a benchmark can turn them on one profile at a time
(datasets/experiments.json) and measure each against the same code.
"""

from dataclasses import asdict, dataclass
from typing import Any

from app.core.config import Settings


@dataclass(frozen=True)
class AgentTuning:
    query_rewrite: bool = True  # small-model search queries in addition to the question
    search_top_k: int = 5  # passages per query in the graph's retrieval step
    max_evidence_chars: int = 30_000  # total passage text in one answer prompt
    rule_routing: bool = False  # code decides when tools are needed (policies.py)
    route_simple_to_small: bool = False  # first answer draft of a simple lookup on the small model

    @classmethod
    def from_settings(cls, s: Settings) -> "AgentTuning":
        return cls(
            query_rewrite=s.agent_query_rewrite,
            search_top_k=s.agent_search_top_k,
            max_evidence_chars=s.agent_max_evidence_passage_chars,
            rule_routing=s.agent_rule_routing,
            route_simple_to_small=s.agent_route_simple_to_small,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
