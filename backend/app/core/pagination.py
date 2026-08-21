"""Common safe pagination helpers for read-side domain services."""
from __future__ import annotations


def page_bounds(page: int, page_size: int, *, max_page_size: int = 200) -> tuple[int, int]:
    safe_page = max(1, int(page))
    safe_size = min(max(1, int(page_size)), max_page_size)
    return (safe_page - 1) * safe_size, safe_size

