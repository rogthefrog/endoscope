import inspect
import logging
import sys
import threading

import pytest

import endoscope
from endoscope import UNBOUND, Change, raise_if, unwatch, watch


def line_of(func, marker):
    """Absolute line number of the line in func's source tagged with `# <marker>`."""
    lines, start = inspect.getsourcelines(func)
    for offset, text in enumerate(lines):
        if text.rstrip().endswith(f"# {marker}"):
            return start + offset
    raise AssertionError(f"marker {marker!r} not found")


@pytest.fixture
def recorded():
    changes: list[Change] = []
    yield changes


def summary(changes):
    return [(c.var, c.old, c.new) for c in changes]


def test_reports_rebinds_with_line_numbers(recorded):
    @watch("x", on_change=recorded.append)
    def f(n):
        x = 1  # first
        y = 5
        x = x + n  # second
        return x + y

    assert f(2) == 8
    assert summary(recorded) == [("x", UNBOUND, 1), ("x", 1, 3)]
    assert [c.lineno for c in recorded] == [line_of(f, "first"), line_of(f, "second")]
    assert all(c.func.endswith("f") and c.filename == __file__ for c in recorded)


def test_change_on_last_line_is_reported(recorded):
    @watch("x", on_change=recorded.append)
    def f():
        x = 1
        x = 2; return x  # last

    f()
    assert summary(recorded) == [("x", UNBOUND, 1), ("x", 1, 2)]
    assert recorded[-1].lineno == line_of(f, "last")


def test_same_object_is_not_a_rebind(recorded):
    @watch("x", on_change=recorded.append)
    def f():
        x = 5
        x = x
        x += 0

    f()
    assert summary(recorded) == [("x", UNBOUND, 5)]


def test_in_place_mutation_is_ignored(recorded):
    @watch("items", on_change=recorded.append)
    def f():
        items = []
        items.append(1)
        items[0] = 2

    f()
    assert summary(recorded) == [("items", UNBOUND, [2])]


def test_arguments_are_initial_values(recorded):
    @watch("n", on_change=recorded.append)
    def f(n):
        n = n * 10
        return n

    f(3)
    assert summary(recorded) == [("n", 3, 30)]


def test_watch_all_locals_by_default(recorded):
    @watch(on_change=recorded.append)
    def f(a):
        b = a + 1
        c = b + 1
        a = c

    f(1)
    assert summary(recorded) == [("b", UNBOUND, 2), ("c", UNBOUND, 3), ("a", 1, 3)]


def test_bare_decorator_logs(caplog):
    @watch
    def f():
        x = 41
        x += 1

    with caplog.at_level(logging.DEBUG, logger="endoscope"):
        f()
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "x = 42 (was 41)" in messages[1]


def test_del_reports_unbound(recorded):
    @watch("x", on_change=recorded.append)
    def f():
        x = 1
        del x
        return 0

    f()
    assert summary(recorded) == [("x", UNBOUND, 1), ("x", 1, UNBOUND)]


def test_loop_reports_every_rebind(recorded):
    @watch("total", on_change=recorded.append)
    def f(values):
        total = 0
        for v in values:
            total += v  # accumulate
        return total

    f([1000, 2000, 3000])
    assert [c.new for c in recorded] == [0, 1000, 3000, 6000]
    assert {c.lineno for c in recorded[1:]} == {line_of(f, "accumulate")}


def test_multiple_handlers_called_in_order():
    calls = []

    @watch("x", on_change=[lambda c: calls.append(("a", c.new)), lambda c: calls.append(("b", c.new))])
    def f():
        x = 1

    f()
    assert calls == [("a", 1), ("b", 1)]


def test_raise_if_raises_at_offending_line():
    @watch("balance", on_change=raise_if(lambda c: c.new < 0, "balance went negative"))
    def withdraw(balance, amount):
        balance = balance - amount  # withdraw
        return balance

    assert withdraw(100, 30) == 70
    with pytest.raises(AssertionError, match="balance went negative") as exc_info:
        withdraw(100, 130)
    assert f"line {line_of(withdraw, 'withdraw')}" in str(exc_info.value)
    assert "balance = -30 (was 100)" in str(exc_info.value)


def test_raise_if_default_message_and_custom_exception():
    @watch("x", on_change=raise_if(lambda c: c.old is not UNBOUND, exc=ValueError))
    def f():
        x = 1
        x = 2

    with pytest.raises(ValueError, match="^raise_if condition met: .* x = 2 \\(was 1\\)$"):
        f()


def test_handler_exception_propagates_and_state_is_cleaned():
    def handler(change):
        if change.new == "bad":
            raise RuntimeError("nope")

    @watch("x", on_change=handler)
    def f(v):
        x = v
        return x

    with pytest.raises(RuntimeError):
        f("bad")
    assert f("good") == "good"


def test_recursion_tracks_frames_separately(recorded):
    @watch("acc", on_change=recorded.append)
    def fact(n):
        acc = 1
        if n > 1:
            acc = n * fact(n - 1)
        return acc

    assert fact(3) == 6
    assert [c.new for c in recorded] == [1, 1, 1, 2, 6]
    assert len({c.frame_id for c in recorded if c.new == 1}) == 3


def test_generator(recorded):
    @watch("x", on_change=recorded.append)
    def gen():
        x = 0
        while True:
            x = yield x  # yield

    g = gen()
    next(g)
    g.send(5)
    g.send(7)
    g.close()
    assert summary(recorded) == [("x", UNBOUND, 0), ("x", 0, 5), ("x", 5, 7)]
    assert {c.lineno for c in recorded[1:]} == {line_of(gen, "yield")}


def test_closure_cell_variables(recorded):
    @watch("x", on_change=recorded.append)
    def f():
        x = 1

        def bump():
            nonlocal x
            x = 10

        bump()
        return x

    assert f() == 10
    assert summary(recorded) == [("x", UNBOUND, 1), ("x", 1, 10)]


def test_threads_tracked_separately():
    seen = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)  # keep all frames alive at once so their ids are distinct

    def handler(change):
        with lock:
            seen.append((change.frame_id, change.new))

    @watch("x", on_change=handler)
    def f(base):
        x = base
        barrier.wait()
        for i in range(100):
            x = base + i + 1
        return x

    threads = [threading.Thread(target=f, args=(1000 * k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    by_frame = {}
    for frame_id, value in seen:
        by_frame.setdefault(frame_id, []).append(value)
    assert sorted(len(v) for v in by_frame.values()) == [101] * 4
    for values in by_frame.values():
        assert values == list(range(values[0], values[0] + 101))


def test_unknown_variable_name_rejected():
    with pytest.raises(ValueError, match="nope"):

        @watch("nope")
        def f():
            x = 1


def test_unwatch(recorded):
    @watch("x", on_change=recorded.append)
    def f():
        x = 1

    unwatch(f)
    f()
    assert recorded == []


def test_unwatched_functions_are_untouched(recorded):
    @watch("x", on_change=recorded.append)
    def f():
        x = 1
        return helper()

    def helper():
        x = 2
        return x

    f()
    assert summary(recorded) == [("x", UNBOUND, 1)]


def test_events_inside_handlers_are_ignored(recorded):
    @watch("x", on_change=recorded.append)
    def inner():
        x = 1

    def handler(change):
        inner()

    @watch("y", on_change=handler)
    def outer():
        y = 1

    outer()
    assert recorded == []
    inner()
    assert summary(recorded) == [("x", UNBOUND, 1)]


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_var_disables_at_decoration_time(monkeypatch, recorded, value):
    monkeypatch.setenv(endoscope.DISABLE_ENV_VAR, value)

    def f():
        x = 1

    assert watch("x", on_change=recorded.append)(f) is f
    monkeypatch.delenv(endoscope.DISABLE_ENV_VAR)
    f()
    assert recorded == []


def test_tool_id_claimed_by_name():
    @watch("x", on_change=lambda c: None)
    def f():
        x = 1

    tool_id = endoscope._monitor._registry.tool_id
    assert sys.monitoring.get_tool(tool_id) == "endoscope"
    assert tool_id != sys.monitoring.DEBUGGER_ID
