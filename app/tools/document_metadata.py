"""document_metadata: facts about one document, scoped to the caller's tenant."""

import uuid
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel

from app.core.execution_context import ExecutionContext
from app.db.models import DocumentStatus
from app.ingestion.pipeline import IngestionService
from app.tools.base import Tool


class DocumentMetadataInput(BaseModel):
    document_id: uuid.UUID


class DocumentMetadataOutput(BaseModel):
    document_id: uuid.UUID
    filename: str
    version: int
    is_latest: bool
    status: DocumentStatus
    page_count: int | None
    chunk_count: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DocumentMetadata(Tool[DocumentMetadataInput, DocumentMetadataOutput]):
    name = "document_metadata"
    description = (
        "Get a document's filename, version, whether it is the latest version, status, page "
        "count, labels, and timestamps. Use the document_id from a search result."
    )
    input_model = DocumentMetadataInput
    output_model = DocumentMetadataOutput
    permissions: ClassVar[frozenset[str]] = frozenset({"documents:read"})
    timeout_s = 5.0

    def __init__(self, ingestion: IngestionService) -> None:
        self._ingestion = ingestion

    async def run(
        self, args: DocumentMetadataInput, ctx: ExecutionContext
    ) -> DocumentMetadataOutput:
        # Ownership check lives in IngestionService.get: another tenant's ID is "not found".
        doc = await self._ingestion.get(ctx.tenant_id, args.document_id)
        return DocumentMetadataOutput(
            document_id=doc.id,
            filename=doc.filename,
            version=doc.version,
            is_latest=doc.is_latest,
            status=doc.status,
            page_count=doc.page_count,
            chunk_count=doc.chunk_count,
            metadata=doc.metadata_,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )
