"""Watch local variables of a function for rebinds, without adding logging or asserts inside it."""

from __future__ import annotations

import logging
import os
import reprlib
from typing import Any, Callable, Iterable, TypeVar, overload

from endoscope import _monitor
from endoscope._change import UNBOUND, Change

__all__ = ["watch", "unwatch", "Change", "UNBOUND", "log_change", "raise_if", "DISABLE_ENV_VAR"]

DISABLE_ENV_VAR = "ENDOSCOPE_DISABLED"

F = TypeVar("F", bound=Callable[..., Any])
Handler = Callable[[Change], Any]

logger = logging.getLogger("endoscope")


def _disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def log_change(change: Change) -> None:
    """Default handler: log the rebind at DEBUG level on the "endoscope" logger."""
    logger.debug(
        "%s:%d %s: %s = %s (was %s)",
        change.filename,
        change.lineno,
        change.func,
        change.var,
        reprlib.repr(change.new),
        reprlib.repr(change.old),
    )


def raise_if(
    predicate: Callable[[Change], bool],
    message: str | None = None,
    exc: type[BaseException] = AssertionError,
) -> Handler:
    """Build a handler that raises exc at the offending line when predicate(change) is true."""

    def check(change: Change) -> None:
        if predicate(change):
            detail = message or "raise_if condition met"
            raise exc(
                f"{detail}: {change.func} line {change.lineno}: "
                f"{change.var} = {reprlib.repr(change.new)} (was {reprlib.repr(change.old)})"
            )

    return check


@overload
def watch(func: F, /) -> F: ...
@overload
def watch(*names: str, on_change: Handler | Iterable[Handler] | None = None) -> Callable[[F], F]: ...


def watch(*names: Any, on_change: Handler | Iterable[Handler] | None = None) -> Any:
    """Call on_change handlers whenever a watched local variable of the decorated function is rebound.

    Usage:
        @watch                                   # all locals, logged via log_change
        @watch("total", "items")                 # selected locals, logged via log_change
        @watch("total", on_change=[log_change, raise_if(lambda c: c.new < 0)])

    Each handler is called as handler(change: Change); its return value is ignored and any
    exception it raises propagates out of the watched function at the offending line.

    Only rebinding is detected (the name points to a different object); in-place mutation is not.
    Function arguments are treated as initial values, so only later rebinds of them are reported.

    If the ENDOSCOPE_DISABLED environment variable is truthy when the decorator is applied,
    the function is returned untouched, with zero overhead.
    """
    if len(names) == 1 and callable(names[0]):
        return watch()(names[0])
    if not all(isinstance(name, str) for name in names):
        raise TypeError("watch() takes variable names as strings")

    if on_change is None:
        handlers: tuple[Handler, ...] = (log_change,)
    elif callable(on_change):
        handlers = (on_change,)
    else:
        handlers = tuple(on_change)

    def decorate(func: F) -> F:
        if _disabled():
            return func
        code = func.__code__
        unknown = set(names) - set(code.co_varnames + code.co_cellvars)
        if unknown:
            raise ValueError(f"{func.__qualname__} has no local variable(s): {', '.join(sorted(unknown))}")
        _monitor.register(code, names, handlers, func.__qualname__)
        return func

    return decorate


def unwatch(func: Callable[..., Any]) -> None:
    """Stop watching func. No-op if it is not watched."""
    _monitor.unregister(func.__code__)
