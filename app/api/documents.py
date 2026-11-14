import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalDep, ServicesDep, rate_limited
from app.core.errors import InvalidRequestError
from app.db.models import DocumentStatus

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str
    version: int
    is_latest: bool
    status: DocumentStatus
    error: str | None
    page_count: int | None
    chunk_count: int
    embedding_model: str | None
    chunking_config: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_at: datetime
    updated_at: datetime


class UploadResponse(BaseModel):
    document: DocumentOut
    duplicate: bool = Field(description="True when identical content was already uploaded")


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


def _parse_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    if len(raw) > 4096:
        raise InvalidRequestError("metadata must be at most 4 KB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError("metadata must be a JSON object") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str | int | float | bool) for k, v in value.items()
    ):
        raise InvalidRequestError("metadata must be a flat JSON object of scalar values")
    return value


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadResponse,
    summary="Upload a TXT, Markdown, or PDF document for indexing",
    dependencies=[rate_limited("upload")],
)
async def upload_document(
    services: ServicesDep,
    principal: PrincipalDep,
    background: BackgroundTasks,
    response: Response,
    file: Annotated[UploadFile, File()],
    metadata: Annotated[
        str | None, Form(description='JSON object, e.g. {"team":"payments"}')
    ] = None,
) -> UploadResponse:
    limit = services.settings.max_upload_bytes
    data = await file.read(limit + 1)  # read one byte past the limit to detect oversize cheaply
    doc, created = await services.ingestion.create_document(
        principal.tenant_id, file.filename, data, _parse_metadata(metadata)
    )
    if created:
        background.add_task(services.ingestion.process_document, doc.id)
    else:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/api/v1/documents/{doc.id}"
    return UploadResponse(document=DocumentOut.model_validate(doc), duplicate=not created)


@router.get("", response_model=DocumentList)
async def list_documents(
    services: ServicesDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> DocumentList:
    docs, total = await services.ingestion.list_documents(
        principal.tenant_id, limit=limit, offset=offset, status=status_filter
    )
    return DocumentList(
        items=[DocumentOut.model_validate(d) for d in docs], total=total, limit=limit, offset=offset
    )


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID, services: ServicesDep, principal: PrincipalDep
) -> DocumentOut:
    return DocumentOut.model_validate(
        await services.ingestion.get(principal.tenant_id, document_id)
    )


class ChunkOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    document_version: int
    chunk_index: int
    section: str | None
    page_start: int | None
    page_end: int | None
    char_start: int
    char_end: int
    text: str


@router.get(
    "/{document_id}/chunks/{chunk_id}",
    response_model=ChunkOut,
    summary="One source passage (what a citation points at)",
)
async def get_chunk(
    document_id: uuid.UUID, chunk_id: uuid.UUID, services: ServicesDep, principal: PrincipalDep
) -> ChunkOut:
    doc, chunk = await services.ingestion.chunk(principal.tenant_id, document_id, chunk_id)
    return ChunkOut(
        id=chunk.id,
        document_id=doc.id,
        filename=doc.filename,
        document_version=doc.version,
        chunk_index=chunk.chunk_index,
        section=chunk.section,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        text=chunk.text,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, services: ServicesDep, principal: PrincipalDep
) -> Response:
    await services.ingestion.delete(principal.tenant_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{document_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentOut,
    summary="Re-chunk and re-embed with the current chunking/embedding configuration",
    dependencies=[rate_limited("upload")],
)
async def reindex_document(
    document_id: uuid.UUID,
    services: ServicesDep,
    principal: PrincipalDep,
    background: BackgroundTasks,
) -> DocumentOut:
    doc = await services.ingestion.mark_for_reindex(principal.tenant_id, document_id)
    background.add_task(services.ingestion.process_document, doc.id)
    return DocumentOut.model_validate(doc)
