import pytest

from app.core.errors import InvalidToolArgumentsError
from app.tools.calculator import evaluate


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("2 ** 10", 1024),
        ("-4 + +2", -2),
        ("7 // 2 + 7 % 2", 4),
        ("15 / 60 * 100", 25),
        ("sqrt(16) + abs(-3)", 7),
        ("round(2.567, 2)", 2.57),
        ("max(1, 5, 3) - min(4, 2)", 3),
        ("1e3 / 4", 250),
    ],
)
def test_arithmetic(expression: str, expected: float) -> None:
    assert evaluate(expression) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",  # calls outside the allowlist
        "open('/etc/passwd').read()",
        "(1).__class__.__bases__",  # attribute access
        "x + 1",  # bare names
        "[1, 2][0]",  # subscripts / containers
        "lambda: 1",
        "'a' * 3",  # strings
        "True + 1",  # bools are not numbers here
        "1j * 1j",  # complex
        "sqrt(x=4)",  # keyword arguments
        "1 if 2 else 3",
        "1; 2",  # statements
    ],
)
def test_rejects_anything_but_arithmetic(expression: str) -> None:
    with pytest.raises(InvalidToolArgumentsError):
        evaluate(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "9 ** 9 ** 9",  # exponent cap stops CPU/memory blow-up
        "10 ** 101",  # magnitude cap
        "1 / 0",
        "5 % 0",
        "sqrt(-1)",
        "-" * 30 + "1",  # nesting depth
        "1 + " * 60 + "1",  # length
        "1e308 * 10",  # overflow to inf
    ],
)
def test_resource_and_domain_limits(expression: str) -> None:
    with pytest.raises(InvalidToolArgumentsError):
        evaluate(expression)
