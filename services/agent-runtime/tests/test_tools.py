"""Tool behaviour that users see directly in chat answers."""

from __future__ import annotations

import pytest
from agent_runtime.tools import calculator


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("21 * 2", "42"),  # whole numbers have no ".0"
        ("10 / 4", "2.5"),  # real fractions keep their decimals
        ("2 + 3 * 4", "14"),
        ("-5 + 2", "-3"),
        ("1 / 0", "could not evaluate expression"),
        ("__import__('os')", "could not evaluate expression"),  # never eval()
    ],
)
def test_calculator(expression, expected):
    assert calculator(expression) == expected
