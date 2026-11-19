"""Shared timing utilities for benchmark modules."""

import time
from collections.abc import Callable


def best_time(func: Callable[[], object], repeats: int = 1) -> float:
    """Measure ``func()`` over ``repeats`` executions and return the minimum duration."""
    if not callable(func):
        raise TypeError(f"func must be callable, got {type(func).__name__}")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        func()
        best = min(best, time.perf_counter() - start)
    return best
