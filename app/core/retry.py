"""Bounded retry with exponential backoff and full jitter.

Only errors whose `transient` flag is True are retried. Callers must only wrap idempotent
operations (model calls and embeddings are; a future write tool is not).
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from app.core.errors import AppError

logger = logging.getLogger(__name__)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, AppError) and exc.transient


def backoff_delay(attempt: int, base_s: float, max_s: float) -> float:
    """Full jitter (AWS architecture blog): uniform(0, min(max, base * 2**attempt))."""
    return random.uniform(0, min(max_s, base_s * 2**attempt))  # noqa: S311 - not crypto


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
    should_retry: Callable[[BaseException], bool] = is_transient,
    operation: str = "operation",
) -> T:
    """Run `fn` at most `max_retries + 1` times."""
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            if attempt >= max_retries or not should_retry(exc):
                raise
            delay = backoff_delay(attempt, base_delay_s, max_delay_s)
            logger.warning(
                "retrying after transient failure",
                extra={
                    "operation": operation,
                    "attempt": attempt + 1,
                    "delay_s": round(delay, 3),
                    "error": type(exc).__name__,
                },
            )
            attempt += 1
            await asyncio.sleep(delay)
