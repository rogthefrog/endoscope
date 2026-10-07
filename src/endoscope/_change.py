from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class _Unbound:
    """Sentinel for a variable that has no value: not yet assigned, or deleted."""

    _instance: _Unbound | None = None

    def __new__(cls) -> _Unbound:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNBOUND"

    def __bool__(self) -> bool:
        return False


UNBOUND: Any = _Unbound()


@dataclass(frozen=True)
class Change:
    """A rebind of a watched local variable, passed to every on_change handler."""

    var: str
    """Variable name."""
    old: Any
    """Previous value, or UNBOUND if this is the first assignment."""
    new: Any
    """New value, or UNBOUND if the variable was deleted."""
    lineno: int
    """Line whose execution caused the rebind."""
    filename: str
    func: str
    """Qualified name of the watched function."""
    frame_id: int
    """Identifies the invocation; distinguishes recursive, threaded or generator frames."""
