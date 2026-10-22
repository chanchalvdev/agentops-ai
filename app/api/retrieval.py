from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, ServicesDep
from app.rag.answer import GroundedAnswer
from app.retrieval.schemas import RetrievalFilters, RetrievalMode, RetrievalResult

router = APIRouter(prefix="/api/v1", tags=["retrieval"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=50)
    mode: RetrievalMode = "hybrid"
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=20)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)


@router.post(
    "/retrieval/search",
    response_model=RetrievalResult,
    summary="Hybrid (vector + full-text, RRF) search with per-result diagnostics",
)
async def search(
    body: SearchRequest, services: ServicesDep, principal: PrincipalDep
) -> RetrievalResult:
    return await services.retriever.search(
        principal.tenant_id, body.query, top_k=body.top_k, mode=body.mode, filters=body.filters
    )


@router.post(
    "/rag/answer",
    response_model=GroundedAnswer,
    summary="Non-agentic baseline: retrieve, answer, and validate citations",
)
async def answer(
    body: AnswerRequest, services: ServicesDep, principal: PrincipalDep
) -> GroundedAnswer:
    return await services.answerer.answer(
        principal.tenant_id, body.question, top_k=body.top_k, filters=body.filters
    )
