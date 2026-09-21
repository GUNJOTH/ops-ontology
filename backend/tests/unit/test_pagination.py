"""单元测试：app.core.pagination.page_bounds。"""
from __future__ import annotations

import pytest
from app.core.pagination import page_bounds

pytestmark = pytest.mark.unit


def test_page_bounds_default() -> None:
    assert page_bounds(1, 50) == (0, 50)
    assert page_bounds(3, 50) == (100, 50)


def test_page_bounds_clamps_low_page() -> None:
    assert page_bounds(0, 50) == (0, 50)
    assert page_bounds(-4, 50) == (0, 50)


def test_page_bounds_clamps_page_size_low() -> None:
    assert page_bounds(2, 0) == (1, 1)
    assert page_bounds(2, -1) == (1, 1)


def test_page_bounds_clamps_page_size_high() -> None:
    assert page_bounds(1, 9999) == (0, 200)


def test_page_bounds_custom_max() -> None:
    assert page_bounds(2, 500, max_page_size=100) == (100, 100)


def test_page_bounds_coerces_numeric_strings() -> None:
    assert page_bounds("2", "10") == (10, 10)
