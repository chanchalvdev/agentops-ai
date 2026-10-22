"""Typed application errors.

Every error carries a stable `code`, an HTTP status, and a `transient` flag. Retry logic looks only
at `transient`, so the decision "is this safe to retry?" lives with the error type, not scattered
through call sites. Messages are safe to show clients; internal detail goes to logs only.
"""


class AppError(Exception):
    code = "internal_error"
    status_code = 500
    transient = False

    def __init__(self, message: str = "Internal error", *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail  # logged, never returned to clients


class AuthenticationError(AppError):
    code, status_code = "unauthenticated", 401


class NotFoundError(AppError):
    code, status_code = "not_found", 404


class InvalidRequestError(AppError):
    code, status_code = "invalid_request", 422


class UnsupportedFileTypeError(AppError):
    code, status_code = "unsupported_file_type", 415


class FileTooLargeError(AppError):
    code, status_code = "file_too_large", 413


class EmptyDocumentError(AppError):
    code, status_code = "empty_document", 422


class DocumentParseError(AppError):
    code, status_code = "document_parse_error", 422


class ConflictError(AppError):
    code, status_code = "conflict", 409


# --- Provider (LLM / embedding) errors -------------------------------------------------------


class ProviderError(AppError):
    """Non-transient provider failure (bad request, unknown model). Retrying will not help."""

    code, status_code = "provider_error", 502


class ProviderUnavailableError(ProviderError):
    code, status_code, transient = "provider_unavailable", 503, True


class ProviderTimeoutError(ProviderError):
    code, status_code, transient = "provider_timeout", 504, True


class RateLimitError(ProviderError):
    code, status_code, transient = "rate_limited", 429, True


class InvalidModelOutputError(AppError):
    code, status_code = "invalid_model_output", 502


class EmbeddingError(AppError):
    code, status_code = "embedding_error", 502


class RetrievalError(AppError):
    code, status_code = "retrieval_error", 500
