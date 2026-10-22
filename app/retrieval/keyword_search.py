"""PostgreSQL full-text search over the generated `tsv` column (GIN-indexed)."""

import re

from sqlalchemy import Boolean, Text, cast, func, not_
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentChunk
from app.retrieval.schemas import Candidate, RetrievalFilters
from app.retrieval.vector_search import scoped_select, to_candidates

# Same tokens websearch_to_tsquery sees: quoted phrases (optionally negated) or bare words.
_TOKEN = re.compile(r'-?"[^"]*"|\S+')


def split_negations(query: str) -> tuple[str, list[str]]:
    """Separate `-term` / `-"phrase"` exclusions from the positive part of a query."""
    positive, negative = [], []
    for token in _TOKEN.findall(query):
        if token.startswith("-") and len(token) > 1:
            negative.append(token[1:])
        else:
            positive.append(token)
    return " ".join(positive), negative


async def keyword_search(
    session: AsyncSession, tenant_id: str, query: str, filters: RetrievalFilters, limit: int
) -> list[Candidate]:
    # websearch_to_tsquery parses user input safely (never raises; quotes become phrases) and
    # removes stopwords, but it ANDs every term, so a natural question like "how long are records
    # kept" matches nothing. Rewriting AND to OR makes it recall-oriented; ts_rank_cd still ranks
    # chunks matching more terms (and phrases) higher.
    # Negations are split out first: OR-ing "!term" would match nearly every chunk, so they are
    # applied as a separate NOT filter. All user text travels as bound parameters.
    positive, negative = split_negations(query)
    parsed = cast(func.websearch_to_tsquery("english", positive), Text)
    tsquery = cast(func.replace(parsed, " & ", " | "), TSQUERY)
    # Normalization 32 maps rank into [0, 1): rank / (rank + 1).
    rank = func.ts_rank_cd(DocumentChunk.tsv, tsquery, 32)
    stmt = (
        scoped_select(tenant_id, filters, rank.label("score"))
        .where(DocumentChunk.tsv.op("@@", return_type=Boolean)(tsquery))
        .order_by(rank.desc(), DocumentChunk.id)
        .limit(limit)
    )
    if negative:
        excluded = func.websearch_to_tsquery("english", " or ".join(negative))
        stmt = stmt.where(not_(DocumentChunk.tsv.op("@@", return_type=Boolean)(excluded)))
    return to_candidates((await session.execute(stmt)).all())
