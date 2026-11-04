"""Example tools an agent can attach.

Tools are plain callables registered by name; an agent's configured
``tool_names`` decide which are available at runtime. The graph's writer node
invokes a tool when the question looks like it needs one (here: a calculator).
"""

from __future__ import annotations

import ast
import operator

from common.catalog import KNOWN_TOOL_NAMES

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand)
    raise ValueError("unsupported expression")


def _format_number(value: float) -> str:
    """21 * 2 is 42, not 42.0: print whole numbers without the decimal point."""
    return str(int(value)) if value.is_integer() else str(value)


def calculator(expression: str) -> str:
    """Evaluate a simple arithmetic expression safely (no eval())."""
    try:
        tree = ast.parse(expression, mode="eval")
        return _format_number(_safe_eval(tree.body))
    except Exception:
        return "could not evaluate expression"


def word_count(text: str) -> str:
    return str(len(text.split()))


TOOL_REGISTRY = {
    "calculator": calculator,
    "word_count": word_count,
}

# The gateway validates tool names against the shared catalog; make sure this
# registry implements exactly those names.
assert set(TOOL_REGISTRY) == set(KNOWN_TOOL_NAMES), (
    "TOOL_REGISTRY and common.catalog.KNOWN_TOOL_NAMES are out of sync"
)
