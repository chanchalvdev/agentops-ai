"""Per-request usage accounting, derived from the execution ledger (product.txt 12).

The ledger already records every model call (tokens, latency, provider attempts) and every tool
call (cache hits, retrieval timings), so this module only counts. Prices are never invented:
local models cost $0 (pricing source "local"); the optional hosted-equivalent figure uses the
reference prices from settings and is always labelled hypothetical, or None when unset.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.core.execution_context import LedgerEvent

HOSTED_EQUIVALENT_LABEL = "hypothetical: tokens x configured reference price, not a real bill"


class UsageSummary(BaseModel):
    model_calls: int = 0
    model_calls_by_model: dict[str, int] = {}
    failed_model_calls: int = 0
    retries: int = 0  # extra provider attempts inside successful calls (router retries/fallback)
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_cache_hits: int = 0
    embedding_calls: int = 0
    embedding_cache_hits: int = 0
    semantic_cache_hit: bool = False
    hosted_equivalent_usd: float | None = None


def hosted_equivalent(
    input_tokens: int,
    output_tokens: int,
    price_in_per_mtok: Decimal | None,
    price_out_per_mtok: Decimal | None,
) -> float | None:
    if price_in_per_mtok is None or price_out_per_mtok is None:
        return None
    cost = (input_tokens * price_in_per_mtok + output_tokens * price_out_per_mtok) / Decimal(
        1_000_000
    )
    return float(cost.quantize(Decimal("0.000001")))


def summarize(
    ledger: list[LedgerEvent] | list[dict[str, Any]],
    price_in_per_mtok: Decimal | None = None,
    price_out_per_mtok: Decimal | None = None,
) -> UsageSummary:
    s = UsageSummary()
    models: Counter[str] = Counter()
    for raw in ledger:
        e = raw if isinstance(raw, LedgerEvent) else LedgerEvent.model_validate(raw)
        if e.kind == "model_call":
            if e.status == "ok":
                s.model_calls += 1
                models[str(e.data.get("model", "?"))] += 1
                s.retries += max(0, int(e.data.get("attempts", 1)) - 1)
                s.input_tokens += e.input_tokens or 0
                s.output_tokens += e.output_tokens or 0
            elif e.status == "error":
                s.failed_model_calls += 1
        elif e.kind == "tool_call" and e.status == "ok":
            s.tool_calls += 1
            if e.data.get("cache") == "hit":
                s.tool_cache_hits += 1
                continue
            if e.name == "document_search":
                result = e.data.get("result", {})
                if result.get("embed_cached"):
                    s.embedding_cache_hits += 1
                elif result.get("timings_ms", {}).get("embed", 0) > 0:
                    s.embedding_calls += 1
        elif e.kind == "cache" and e.name == "semantic_cache":
            s.semantic_cache_hit = True
    s.model_calls_by_model = dict(models)
    s.hosted_equivalent_usd = hosted_equivalent(
        s.input_tokens, s.output_tokens, price_in_per_mtok, price_out_per_mtok
    )
    return s
