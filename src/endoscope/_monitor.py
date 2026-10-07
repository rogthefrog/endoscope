"""sys.monitoring plumbing: one shared tool ID, dispatching events to per-code watchers."""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from types import CodeType, FrameType
from typing import Any, Callable, Iterable

from endoscope._change import UNBOUND, Change

_M = sys.monitoring
_E = _M.events

# Prefer IDs no standard tool claims, so debuggers, coverage and profilers keep working.
_CANDIDATE_TOOL_IDS = (3, 4, _M.PROFILER_ID, _M.COVERAGE_ID, _M.OPTIMIZER_ID)
_TOOL_NAME = "endoscope"

_LOCAL_EVENTS = _E.PY_START | _E.LINE | _E.PY_YIELD | _E.PY_RETURN

Handler = Callable[[Change], Any]


@dataclass
class _Watcher:
    names: tuple[str, ...]
    handlers: tuple[Handler, ...]
    qualname: str


@dataclass
class _FrameState:
    snapshot: dict[str, Any]
    last_line: int | None = None


@dataclass
class _Registry:
    tool_id: int | None = None
    watchers: dict[CodeType, _Watcher] = field(default_factory=dict)
    # Keyed by id(frame); entries are reset on PY_START so id reuse is harmless.
    frames: dict[int, _FrameState] = field(default_factory=dict)


_registry = _Registry()
_lock = threading.Lock()
_tls = threading.local()


def _claim_tool_id() -> int:
    for tool_id in _CANDIDATE_TOOL_IDS:
        try:
            _M.use_tool_id(tool_id, _TOOL_NAME)
        except ValueError:
            continue
        _M.register_callback(tool_id, _E.PY_START, _on_start)
        _M.register_callback(tool_id, _E.LINE, _on_line)
        _M.register_callback(tool_id, _E.PY_YIELD, _on_yield)
        _M.register_callback(tool_id, _E.PY_RETURN, _on_return)
        _M.register_callback(tool_id, _E.PY_UNWIND, _on_unwind)
        return tool_id
    raise RuntimeError("endoscope: no free sys.monitoring tool ID available")


def register(code: CodeType, names: Iterable[str], handlers: Iterable[Handler], qualname: str) -> None:
    names = tuple(names) or code.co_varnames + code.co_cellvars
    with _lock:
        if _registry.tool_id is None:
            _registry.tool_id = _claim_tool_id()
        tool_id = _registry.tool_id
        _registry.watchers[code] = _Watcher(names, tuple(handlers), qualname)
        _M.set_local_events(tool_id, code, _LOCAL_EVENTS)
        # PY_UNWIND can only be enabled globally; needed to drop state of frames exiting via exception.
        _M.set_events(tool_id, _E.PY_UNWIND)


def unregister(code: CodeType) -> None:
    with _lock:
        if _registry.watchers.pop(code, None) is None or _registry.tool_id is None:
            return
        _M.set_local_events(_registry.tool_id, code, _E.NO_EVENTS)
        if not _registry.watchers:
            _M.set_events(_registry.tool_id, _E.NO_EVENTS)
            _registry.frames.clear()


def _snapshot(frame: FrameType, names: tuple[str, ...]) -> dict[str, Any]:
    f_locals = frame.f_locals
    return {name: f_locals.get(name, UNBOUND) for name in names}


def _in_handler() -> bool:
    return getattr(_tls, "in_handler", False)


def _check(code: CodeType, frame: FrameType, next_line: int | None) -> None:
    """Report rebinds since the last check, attributing them to the previously executed line."""
    watcher = _registry.watchers.get(code)
    state = _registry.frames.get(id(frame))
    if watcher is None or state is None:
        return
    f_locals = frame.f_locals
    changes = []
    for name in watcher.names:
        old = state.snapshot[name]
        new = f_locals.get(name, UNBOUND)
        if new is not old:
            state.snapshot[name] = new
            changes.append(
                Change(
                    var=name,
                    old=old,
                    new=new,
                    lineno=state.last_line if state.last_line is not None else code.co_firstlineno,
                    filename=code.co_filename,
                    func=watcher.qualname,
                    frame_id=id(frame),
                )
            )
    state.last_line = next_line
    if not changes:
        return
    _tls.in_handler = True
    try:
        for change in changes:
            for handler in watcher.handlers:
                handler(change)
    finally:
        _tls.in_handler = False


# Callbacks run with the monitored frame as their immediate caller, hence sys._getframe(1).


def _on_start(code: CodeType, instruction_offset: int) -> None:
    watcher = _registry.watchers.get(code)
    if watcher is None:
        return
    frame = sys._getframe(1)
    # Arguments are initial values, not rebinds, so they are snapshotted silently.
    _registry.frames[id(frame)] = _FrameState(_snapshot(frame, watcher.names))


def _on_line(code: CodeType, line_number: int) -> None:
    if not _in_handler():
        _check(code, sys._getframe(1), line_number)


def _on_yield(code: CodeType, instruction_offset: int, retval: object) -> None:
    if not _in_handler():
        frame = sys._getframe(1)
        state = _registry.frames.get(id(frame))
        _check(code, frame, state.last_line if state else None)


def _on_return(code: CodeType, instruction_offset: int, retval: object) -> None:
    frame = sys._getframe(1)
    try:
        if not _in_handler():
            _check(code, frame, None)
    finally:
        _registry.frames.pop(id(frame), None)


def _on_unwind(code: CodeType, instruction_offset: int, exception: BaseException) -> None:
    if code in _registry.watchers:
        _registry.frames.pop(id(sys._getframe(1)), None)
