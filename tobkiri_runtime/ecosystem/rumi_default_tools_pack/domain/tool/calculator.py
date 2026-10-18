"""Bounded arithmetic shared by the Calculator's owned execution paths."""

from __future__ import annotations

import ast
import math
import operator
from typing import Callable

MAX_EXPRESSION_LENGTH = 2048
MAX_NODES = 128
MAX_DEPTH = 32
MAX_INTEGER_BITS = 4096
_BINARY: dict[type[ast.operator], Callable] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def calculate(expression: str) -> int | float:
    """Evaluate finite arithmetic, checking allocation bounds before powers."""
    if (
        not isinstance(expression, str)
        or not 0 < len(expression) <= MAX_EXPRESSION_LENGTH
        or not expression.isascii()
    ):
        raise ValueError("Calculator expression must be bounded ASCII text")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, RecursionError) as error:
        raise ValueError("Unsupported calculator expression") from error
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ValueError("Calculator expression has too many operations")

    def bounded(value: object) -> int | float:
        if type(value) is int and value.bit_length() <= MAX_INTEGER_BITS:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise ValueError("Calculator result is outside the supported range")

    def evaluate(node: ast.AST, depth: int = 0) -> int | float:
        if depth > MAX_DEPTH:
            raise ValueError("Calculator expression is too deeply nested")
        if isinstance(node, ast.Constant):
            return bounded(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd):
            value = evaluate(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left = evaluate(node.left, depth + 1)
            right = evaluate(node.right, depth + 1)
            if isinstance(node.op, ast.Pow):
                if abs(right) > 100:
                    raise ValueError("Calculator exponent is too large")
                if (
                    type(left) is int
                    and type(right) is int
                    and right > 0
                    and left.bit_length() * right > MAX_INTEGER_BITS
                ):
                    raise ValueError("Calculator power is too large")
            if (
                isinstance(node.op, ast.Mult)
                and type(left) is int
                and type(right) is int
                and left
                and right
                and left.bit_length() + right.bit_length() > MAX_INTEGER_BITS
            ):
                raise ValueError("Calculator product is too large")
            try:
                return bounded(_BINARY[type(node.op)](left, right))
            except ArithmeticError as error:
                raise ValueError("Calculator arithmetic is outside the supported range") from error
        raise ValueError("Unsupported calculator expression")

    return evaluate(tree.body)
