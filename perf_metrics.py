"""Request-scoped performance timings for logs and ``Server-Timing`` headers."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from time import perf_counter
from typing import Iterator


_timings: ContextVar[list[tuple[str, float]] | None] = ContextVar(
    "request_timings", default=None
)


def begin_request() -> Token:
    """Start collecting timings in the current async request context."""
    return _timings.set([])


def add_timing(name: str, duration_ms: float) -> None:
    timings = _timings.get()
    if timings is not None:
        timings.append((name, max(0.0, duration_ms)))


@contextmanager
def timed(name: str) -> Iterator[None]:
    """Record elapsed wall time under *name* when request collection is active."""
    started = perf_counter()
    try:
        yield
    finally:
        add_timing(name, (perf_counter() - started) * 1000)


def current_timings() -> tuple[tuple[str, float], ...]:
    return tuple(_timings.get() or ())


def end_request(token: Token) -> None:
    _timings.reset(token)


def format_server_timing(timings: tuple[tuple[str, float], ...]) -> str:
    """Format timings for the standard Server-Timing response header.

    Repeated spans are combined so the header stays compact on multi-query
    routes. Metric names use underscores because Server-Timing names are tokens.
    """
    totals: dict[str, float] = {}
    order: list[str] = []
    for raw_name, duration_ms in timings:
        name = "".join(
            ch if ch.isalnum() or ch in "_-" else "_" for ch in raw_name
        ).strip("_")
        if not name:
            continue
        if name not in totals:
            totals[name] = 0.0
            order.append(name)
        totals[name] += duration_ms
    return ", ".join(f"{name};dur={totals[name]:.1f}" for name in order)