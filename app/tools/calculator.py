"""Safe arithmetic. Parses with `ast` and evaluates an allowlist of nodes; never calls eval()."""

import ast
import math
import operator
from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel, Field

from app.core.errors import InvalidToolArgumentsError
from app.core.execution_context import ExecutionContext
from app.tools.base import Tool

MAX_EXPRESSION_CHARS = 200
MAX_DEPTH = 20
MAX_EXPONENT = 100
MAX_MAGNITUDE = 1e100

_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
}


def _bad(message: str) -> InvalidToolArgumentsError:
    return InvalidToolArgumentsError(f"calculator: {message}")


def _eval(node: ast.AST, depth: int = 0) -> float:
    if depth > MAX_DEPTH:
        raise _bad("expression is nested too deeply")
    match node:
        case ast.Expression(body=body):
            return _eval(body, depth + 1)
        case ast.Constant(value=int() | float() as value) if not isinstance(value, bool):
            return value
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINARY:
            lhs, rhs = _eval(left, depth + 1), _eval(right, depth + 1)
            if isinstance(op, ast.Pow) and abs(rhs) > MAX_EXPONENT:
                raise _bad(f"exponent larger than {MAX_EXPONENT}")
            if isinstance(op, ast.Div | ast.FloorDiv | ast.Mod) and rhs == 0:
                raise _bad("division by zero")
            result = _BINARY[type(op)](lhs, rhs)
        case ast.UnaryOp(op=op, operand=operand) if type(op) in _UNARY:
            result = _UNARY[type(op)](_eval(operand, depth + 1))
        case ast.Call(func=ast.Name(id=fn), args=args, keywords=[]) if fn in _FUNCTIONS:
            values = [_eval(a, depth + 1) for a in args]
            if not values or len(values) > 10:
                raise _bad(f"{fn}() takes 1 to 10 arguments")
            try:
                result = _FUNCTIONS[fn](*values)
            except (TypeError, ValueError) as exc:
                raise _bad(f"{fn}() {exc}") from None
        case _:
            raise _bad(f"unsupported syntax: {type(node).__name__}")
    if isinstance(result, complex) or not math.isfinite(result) or abs(result) > MAX_MAGNITUDE:
        raise _bad("result is not a finite real number within range")
    return result


def evaluate(expression: str) -> float:
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise _bad(f"expression longer than {MAX_EXPRESSION_CHARS} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise _bad("not a valid arithmetic expression") from None
    return _eval(tree)


class CalculatorInput(BaseModel):
    expression: str = Field(
        min_length=1,
        max_length=MAX_EXPRESSION_CHARS,
        description="Arithmetic only: + - * / // % **, parentheses, abs round min max sqrt.",
    )


class CalculatorOutput(BaseModel):
    expression: str
    result: float


class Calculator(Tool[CalculatorInput, CalculatorOutput]):
    name = "calculator"
    description = (
        "Evaluate an arithmetic expression exactly. Use it for any calculation instead of "
        "doing arithmetic yourself."
    )
    input_model = CalculatorInput
    output_model = CalculatorOutput
    permissions: ClassVar[frozenset[str]] = frozenset()
    timeout_s = 2.0

    async def run(self, args: CalculatorInput, ctx: ExecutionContext) -> CalculatorOutput:
        return CalculatorOutput(expression=args.expression, result=evaluate(args.expression))
