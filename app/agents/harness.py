"""The execution harness: the single gate every model call and tool call goes through.

Order of checks before any operation: cancellation -> deadline -> budget -> permission ->
arguments. Each attempt, including rejections, is appended to the execution ledger. The model is
never trusted to enforce any of this itself; the custom loop and every LangGraph node call here.
"""

import asyncio
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import (
    AppError,
    ApprovalRequiredError,
    BudgetExhaustedError,
    DeadlineExceededError,
    ExecutionCancelledError,
    InvalidToolArgumentsError,
    ProviderTimeoutError,
    ToolExecutionError,
    ToolPermissionError,
    ToolTimeoutError,
)
from app.core.execution_context import EventKind, ExecutionContext
from app.core.retry import retry_async
from app.llm.provider import LLMRouter
from app.llm.schemas import LLMRequest, LLMResponse, ModelRole, ToolCall
from app.llm.usage import estimate_cost
from app.tools.registry import AnyTool, ToolRegistry

logger = logging.getLogger(__name__)


class ToolOutcome(BaseModel):
    call_id: str
    name: str
    output: dict[str, Any]
    latency_ms: float
    attempts: int


def call_signature(call: ToolCall) -> str:
    """Identity of a call for repeat detection: tool name plus canonical arguments."""
    return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"


def _argument_problems(exc: ValidationError) -> str:
    # Field locations and error types only: never echo submitted values back into the prompt.
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['type']}" for e in exc.errors()
    )


class Harness:
    def __init__(self, llm: LLMRouter, tools: ToolRegistry) -> None:
        self.llm, self.tools = llm, tools

    # --- policy -----------------------------------------------------------------------------

    def _reject(self, ctx: ExecutionContext, kind: EventKind, name: str, exc: AppError) -> AppError:
        ctx.record(kind, name, "rejected", error_code=exc.code, message=exc.message)
        return exc

    @staticmethod
    def _inactive(ctx: ExecutionContext) -> AppError | None:
        if ctx.cancelled:
            return ExecutionCancelledError(ctx.cancel_reason or "cancelled")
        if ctx.remaining_s() <= 0:
            return DeadlineExceededError("Execution deadline exceeded")
        return None

    def ensure_active(
        self, ctx: ExecutionContext, kind: EventKind = "policy", name: str = "check"
    ) -> None:
        exc = self._inactive(ctx)
        if exc is not None:
            raise self._reject(ctx, kind, name, exc)

    def _ensure_token_and_cost_budget(self, ctx: ExecutionContext, name: str) -> None:
        limits, counters = ctx.limits, ctx.counters
        if counters.usage.total_tokens >= limits.max_total_tokens:
            raise self._reject(
                ctx, "model_call", name, BudgetExhaustedError("Token budget exhausted")
            )
        if limits.max_cost_usd is not None:
            if not counters.cost_known:
                # Fail closed: an unpriced call means we can no longer prove we are under the cap.
                raise self._reject(
                    ctx, "model_call", name, BudgetExhaustedError("Cost unknown under a cost cap")
                )
            if counters.cost_usd >= limits.max_cost_usd:
                raise self._reject(
                    ctx, "model_call", name, BudgetExhaustedError("Cost budget exhausted")
                )

    def begin_step(self, ctx: ExecutionContext, name: str = "step") -> int:
        self.ensure_active(ctx, "step", name)
        if ctx.counters.steps >= ctx.limits.max_steps:
            raise self._reject(ctx, "step", name, BudgetExhaustedError("Step limit reached"))
        ctx.counters.steps += 1
        ctx.record("step", name, "ok", data={"step": ctx.counters.steps})
        return ctx.counters.steps

    def consume_correction(self, ctx: ExecutionContext, kind: str) -> int:
        """Allow a bounded correction attempt for one failure kind; raise when none remain."""
        self.ensure_active(ctx, "correction", kind)
        used = ctx.counters.corrections.get(kind, 0)
        if used >= ctx.limits.max_corrections:
            raise self._reject(
                ctx, "correction", kind, BudgetExhaustedError(f"Correction limit reached: {kind}")
            )
        ctx.counters.corrections[kind] = used + 1
        ctx.record("correction", kind, "ok", data={"attempt": used + 1})
        return used + 1

    # --- model calls ------------------------------------------------------------------------

    async def call_model(
        self, ctx: ExecutionContext, role: ModelRole, request: LLMRequest, *, name: str = "llm"
    ) -> LLMResponse:
        self.ensure_active(ctx, "model_call", name)
        if ctx.counters.model_calls >= ctx.limits.max_model_calls:
            raise self._reject(
                ctx, "model_call", name, BudgetExhaustedError("Model call limit reached")
            )
        self._ensure_token_and_cost_budget(ctx, name)

        remaining = ctx.remaining_s()
        tokens_left = ctx.limits.max_total_tokens - ctx.counters.usage.total_tokens
        request = request.model_copy(
            update={
                "timeout_s": min(request.timeout_s, remaining),
                "max_output_tokens": min(request.max_output_tokens, tokens_left),
            }
        )
        ctx.counters.model_calls += 1  # counted when attempted, so failures still consume budget
        started = time.perf_counter()
        try:
            async with asyncio.timeout(remaining):
                response = await self.llm.chat(role, request)
        except TimeoutError:
            exc: AppError = (
                DeadlineExceededError("Execution deadline exceeded")
                if ctx.remaining_s() <= 0
                else ProviderTimeoutError("Model call timed out")
            )
            ctx.record("model_call", name, "error", error_code=exc.code, message=exc.message)
            raise exc from None
        except AppError as exc:
            ctx.record("model_call", name, "error", error_code=exc.code, message=exc.message)
            raise

        ctx.counters.usage = ctx.counters.usage + response.usage
        cost = estimate_cost(response.provider, response.model, response.usage)
        if cost.cost_usd is None:
            ctx.counters.cost_known = False
        else:
            ctx.counters.cost_usd += cost.cost_usd
        ctx.record(
            "model_call",
            name,
            "ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            data={
                "role": role,
                "model": response.model,
                "provider": response.provider,
                "tool_calls": [c.name for c in response.tool_calls],
                "attempts": response.attempts,
                "pricing_source": cost.pricing_source,
            },
        )
        return response

    # --- tool calls -------------------------------------------------------------------------

    def authorize_tool_call(
        self, ctx: ExecutionContext, call: ToolCall
    ) -> tuple[AnyTool, BaseModel]:
        """Resolve, authorize, and validate a requested call without executing it."""
        name = call.name or "<missing>"
        tool = self.tools.get(call.name)
        if tool is None or call.name not in ctx.allowed_tools:
            # Unknown and disallowed tools look the same to the model: no tool discovery by probing.
            raise self._reject(
                ctx, "tool_call", name, ToolPermissionError(f"Tool not permitted: {name}")
            )
        missing = tool.permissions - ctx.permissions
        if missing:
            raise self._reject(
                ctx,
                "tool_call",
                name,
                ToolPermissionError(
                    f"Tool not permitted: {name}", detail=f"missing {sorted(missing)}"
                ),
            )
        if call.raw_arguments is not None:
            raise self._reject(
                ctx, "tool_call", name, InvalidToolArgumentsError("Arguments are not a JSON object")
            )
        try:
            args = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            raise self._reject(
                ctx,
                "tool_call",
                name,
                InvalidToolArgumentsError(
                    f"Invalid arguments for {name}: {_argument_problems(exc)}"
                ),
            ) from None
        return tool, args

    async def call_tool(self, ctx: ExecutionContext, call: ToolCall) -> ToolOutcome:
        name = call.name or "<missing>"
        self.ensure_active(ctx, "tool_call", name)
        if ctx.counters.tool_calls >= ctx.limits.max_tool_calls:
            raise self._reject(
                ctx, "tool_call", name, BudgetExhaustedError("Tool call limit reached")
            )
        tool, args = self.authorize_tool_call(ctx, call)
        try:
            needs_approval = tool.approval_needed(args, ctx)
        except AppError as exc:
            raise self._reject(ctx, "tool_call", tool.name, exc) from None
        if needs_approval and call.id not in ctx.approved_call_ids:
            ctx.record(
                "approval",
                tool.name,
                "pending",
                data={"call_id": call.id, "arguments": tool.ledger_arguments(args)},
            )
            raise ApprovalRequiredError(f"{tool.name} requires human approval")

        ctx.counters.tool_calls += 1
        attempts = 0

        async def attempt() -> BaseModel:
            nonlocal attempts
            attempts += 1
            inactive = self._inactive(ctx)  # recorded once, by the error handler below
            if inactive is not None:
                raise inactive
            budget = min(tool.timeout_s, ctx.remaining_s())
            ctx.current_call_id = call.id
            try:
                async with asyncio.timeout(budget):
                    result = await tool.run(args, ctx)
                validated: BaseModel = tool.output_model.model_validate(result.model_dump())
                return validated
            except ValidationError as exc:
                raise ToolExecutionError(
                    f"{tool.name} returned an invalid result", detail=str(exc)
                ) from None
            except TimeoutError:
                if ctx.remaining_s() <= 0:
                    raise DeadlineExceededError("Execution deadline exceeded") from None
                raise ToolTimeoutError(f"{tool.name} timed out after {budget:.1f}s") from None
            except AppError:
                raise
            except Exception as exc:
                raise tool.map_error(exc) from exc
            finally:
                ctx.current_call_id = None

        started = time.perf_counter()
        try:
            output = await retry_async(
                attempt,
                # Non-idempotent tools run once: a timed-out write may still have happened.
                max_retries=1 if tool.idempotent else 0,
                base_delay_s=0.2,
                max_delay_s=1.0,
                operation=f"tool.{tool.name}",
            )
        except AppError as exc:
            ctx.record(
                "tool_call",
                tool.name,
                "error",
                error_code=exc.code,
                message=exc.message,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                data={"call_id": call.id, "attempts": attempts},
            )
            if exc.detail:
                logger.warning("tool failed", extra={"tool": tool.name, "detail": exc.detail})
            raise
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        ctx.record(
            "tool_call",
            tool.name,
            "ok",
            latency_ms=latency_ms,
            data={
                "call_id": call.id,
                "attempts": attempts,
                "arguments": tool.ledger_arguments(args),
                **({"result": summary} if (summary := tool.ledger_output(output)) else {}),
            },
        )
        return ToolOutcome(
            call_id=call.id,
            name=tool.name,
            output=output.model_dump(mode="json"),
            latency_ms=latency_ms,
            attempts=attempts,
        )
