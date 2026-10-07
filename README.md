# Endoscope

Peek at variables without adding a ton of logging or debugging code.

Decorate a function with `@watch` and every time a watched local variable is rebound, your handlers are
called. Requires Python 3.12+ (uses `sys.monitoring`).

```python
import logging
from endoscope import watch, raise_if, log_change

logging.basicConfig(level=logging.DEBUG)

@watch("total", on_change=[log_change, raise_if(lambda c: c.new < 0, "total went negative")])
def process(costs):
    total = 0
    for cost in costs:
        total += cost
    return total
```

## API

- `@watch` : watch all locals, log changes with `log_change`.
- `@watch("a", "b", on_change=handler_or_list)` : watch selected locals. Unknown names raise `ValueError`.
- `unwatch(func)` — stop watching.

### Handlers

A handler is any callable `handler(change: Change) -> None`, defined anywhere. Handlers are called in order and
return values are ignored. Anything a handler raises propagates out of the watched function at the offending line.

`Change` fields:

| field      | meaning                                                         |
|------------|-----------------------------------------------------------------|
| `var`      | variable name                                                   |
| `old`      | previous value, or `UNBOUND` on first assignment                |
| `new`      | new value, or `UNBOUND` after `del`                             |
| `lineno`   | line whose execution caused the rebind                          |
| `filename` | source file of the watched function                             |
| `func`     | qualified name of the watched function                          |
| `frame_id` | identifies the invocation (recursion, threads, generators)      |

Built-in helpers:

- `log_change`: logs at DEBUG on the `endoscope` logger.
- `raise_if(predicate, message=None, exc=AssertionError)`: builds a handler that raises when `predicate(change)` is true (see below).

### `raise_if`

`raise_if` does not check anything when it is called. It returns a handler that you pass to `on_change`.
That handler then runs on every rebind:

1. It calls `predicate(change)` with the `Change` described above.
2. If the result is falsy, nothing happens and execution continues.
3. If the result is truthy, it raises `exc` (default `AssertionError`) from inside the watched function, formatted as
   `"<message>: <func> line <lineno>: <var> = <new> (was <old>)"`. `message` defaults to `"raise_if condition met"`.

In other words, it's an assert that runs automatically after every rebind, except that the condition describes
the **forbidden** case.

```python
@watch("balance", on_change=raise_if(lambda c: c.new < 0, "balance went negative"))
def withdraw(balance, amount):
    balance = balance - amount
    return balance

withdraw(100, 130)
# AssertionError: balance went negative: withdraw line 3: balance = -30 (was 100)
```

Because the predicate gets the whole `Change`, it can check more than the new value:

```python
raise_if(lambda c: c.old is not UNBOUND and c.new < c.old)  # transition: must never decrease
raise_if(lambda c: c.var == "total" and c.new < 0)          # only constrain one of several watched variables
raise_if(lambda c: c.old is not UNBOUND, exc=ValueError)    # must never be reassigned after its first value
```

Things to know:

- The predicate runs for **every** watched variable in that `@watch`. If you watch several, use `c.var` to
  choose which one it applies to.
- On a first assignment, `c.old` is `UNBOUND`. After `del`, `c.new` is `UNBOUND`. Comparing `UNBOUND` with `<`
  raises `TypeError`, which propagates instead of `exc`. Guard with `c.old is not UNBOUND and ...` when needed.
- It raises explicitly instead of using an `assert` statement, so it still runs under `python -O`. Use
  `ENDOSCOPE_DISABLED` to turn it off.
- `raise_if` is only a convenience. Any handler that raises does the same job.

## Semantics

- Only **rebinding** is detected: the name now refers to a different object (`is not`). In-place mutation
  (`items.append(x)`) is not reported, and neither is `x = x`, or `x += 0` on a cached small int. This is
  for performance reasons (it would require `deepcopy`, which can be very expensive).
- Changes are detected between lines, so several rebinds on one line (`x = 1; x = 2`, or
  `for i in range(3): x = i`) are reported as a single change from the value before the line to the value after it.
- Function arguments are initial values; only later rebinds of them are reported.
- Only the decorated function's own frame is watched; functions it calls (and nested `def`s) are untraced. You can
  `watch` those if you want.
- Closure variables rebound via `nonlocal` from an inner function are seen by the outer frame.
- Watched functions called from inside a handler do not report their changes (so a watcher doesn't trigger itself).
- Endoscope claims a free `sys.monitoring` tool ID (never `DEBUGGER_ID`), so pdb/debuggers keep working.

## Disabling

Set `ENDOSCOPE_DISABLED=1` (or `true`/`yes`/`on`). It is checked when the decorator is applied (import time):
`@watch` then returns the original function untouched.

## Development

```
poetry install
poetry run pytest tests/
```
