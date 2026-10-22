"""Cost accounting. Prices are configuration, versioned, and never invented.

Local Ollama models cost $0 per token (pricing_source="local"); the real costs are latency and
hardware. Unknown hosted models report cost=None with pricing_source="unavailable".
"""

from decimal import Decimal

from pydantic import BaseModel

from app.llm.schemas import Usage

PRICING_VERSION = "2026-10-22"

# USD per 1M tokens (input, output). Add hosted models here with the date you verified the price.
HOSTED_PRICES: dict[str, tuple[Decimal, Decimal]] = {}


class CostEstimate(BaseModel):
    cost_usd: Decimal | None
    pricing_source: str  # local | table | unavailable
    pricing_version: str = PRICING_VERSION


def estimate_cost(provider: str, model: str, usage: Usage) -> CostEstimate:
    if provider in ("ollama", "fake"):
        return CostEstimate(cost_usd=Decimal(0), pricing_source="local")
    price = HOSTED_PRICES.get(model)
    if price is None:
        return CostEstimate(cost_usd=None, pricing_source="unavailable")
    per_million = Decimal(1_000_000)
    cost = (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / per_million
    return CostEstimate(cost_usd=cost.quantize(Decimal("0.000001")), pricing_source="table")
