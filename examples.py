"""Endoscope examples, from simple to more elaborate.

Run with:  poetry run python examples.py

Remember that endoscope detects *rebinding* (the name points to a new object), not in-place mutation.
The list and dict examples therefore build new objects (`rows = [...]`, `config = {**config, ...}`)
rather than calling `.append()` or assigning `config[key] = ...`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from endoscope import UNBOUND, Change, log_change, raise_if, watch

# ---------------------------------------------------------------------------------------------------------------
# 1. int: inventory stock must never go negative.
# ---------------------------------------------------------------------------------------------------------------


@watch("stock", on_change=[log_change, raise_if(lambda c: c.new < 0, "oversold")])
def fulfil(stock: int, orders: list[int]) -> int:
    for quantity in orders:
        stock -= quantity
    return stock


# ---------------------------------------------------------------------------------------------------------------
# 2. str: an order status may only move along allowed transitions.
# ---------------------------------------------------------------------------------------------------------------

ALLOWED_TRANSITIONS = {
    "pending": {"paid", "cancelled"},
    "paid": {"shipped", "refunded"},
    "shipped": {"delivered", "returned"},
    "delivered": {"returned"},
    "returned": {"refunded"},
    "cancelled": set(),
    "refunded": set(),
}


def illegal_transition(c: Change) -> bool:
    return c.old is not UNBOUND and c.new not in ALLOWED_TRANSITIONS[c.old]


@watch(
    "status",
    on_change=[
        log_change,
        raise_if(illegal_transition, "illegal status transition", exc=ValueError),
    ],
)
def process_events(events: list[str]) -> str:
    status = "pending"
    for event in events:
        if event == "payment_received":
            status = "paid"
        elif event == "label_printed":
            status = "shipped"
        elif event == "courier_confirmed":
            status = "delivered"
        elif event == "customer_cancelled":
            status = "cancelled"
    return status


# ---------------------------------------------------------------------------------------------------------------
# 3. list: report how many rows each step of a cleaning pipeline drops, without logging after every step.
# ---------------------------------------------------------------------------------------------------------------


def log_row_count(c: Change) -> None:
    before = 0 if c.old is UNBOUND else len(c.old)
    after = 0 if c.new is UNBOUND else len(c.new)
    logging.info(
        "line %d: %s %d -> %d rows (%+d)",
        c.lineno,
        c.var,
        before,
        after,
        after - before,
    )


@watch(
    "rows",
    on_change=[
        log_row_count,
        # Cleaning may only drop or reorder rows; producing more rows than it received is a bug.
        raise_if(
            lambda c: c.old is not UNBOUND and len(c.new) > len(c.old),
            "cleaning step added rows",
        ),
    ],
)
def clean_signups(raw: list[dict]) -> list[dict]:
    rows = list(raw)
    rows = [r for r in rows if r.get("email")]
    rows = [{**r, "email": r["email"].strip().lower()} for r in rows]
    rows = list(
        {r["email"]: r for r in rows}.values()
    )  # dedupe by email, keeping the latest
    rows = [r for r in rows if r.get("age", 0) >= 18]
    rows = sorted(rows, key=lambda r: r["email"])
    return rows


# ---------------------------------------------------------------------------------------------------------------
# 4. dict: trace which layer of configuration set or overrode each key.
# ---------------------------------------------------------------------------------------------------------------

REQUIRED_KEYS = {"host", "port", "timeout"}


def log_config_diff(c: Change) -> None:
    old = {} if c.old is UNBOUND else c.old
    for key in sorted(c.new.keys() | old.keys()):
        if key not in old:
            logging.info("line %d: + %s = %r", c.lineno, key, c.new[key])
        elif key not in c.new:
            logging.info("line %d: - %s", c.lineno, key)
        elif old[key] != c.new[key]:
            logging.info(
                "line %d: ~ %s = %r (was %r)", c.lineno, key, c.new[key], old[key]
            )


@watch(
    "config",
    on_change=[
        log_config_diff,
        # Once a required key is present, no later layer may remove it.
        raise_if(
            lambda c: c.old is not UNBOUND
            and bool(REQUIRED_KEYS & c.old.keys() - c.new.keys()),
            "required config key removed",
            exc=KeyError,
        ),
    ],
)
def load_config(file_config: dict, environ: dict, cli_args: dict) -> dict:
    config = {"host": "localhost", "port": 8080, "timeout": 30, "debug": False}
    config = {**config, **file_config}
    env_overrides = {
        k.removeprefix("APP_").lower(): v
        for k, v in environ.items()
        if k.startswith("APP_")
    }
    config = {**config, **env_overrides}
    config = {**config, **{k: v for k, v in cli_args.items() if v is not None}}
    config = {k: v for k, v in config.items() if v != ""}  # empty string means "unset"
    return config


# ---------------------------------------------------------------------------------------------------------------
# 5. Several variables, a reusable audit trail, and per-variable rules: pricing a shopping cart.
# ---------------------------------------------------------------------------------------------------------------


@dataclass
class AuditTrail:
    """A handler that records every change, so a failing computation can be replayed step by step."""

    entries: list[Change] = field(default_factory=list)

    def __call__(self, c: Change) -> None:
        self.entries.append(c)

    def dump(self) -> None:
        for c in self.entries:
            new = f"{len(c.new)} items" if isinstance(c.new, list) else c.new
            logging.info("  line %3d  %-9s -> %s", c.lineno, c.var, new)


def pricing_rules(c: Change) -> bool:
    if c.var == "lines":
        return c.old is not UNBOUND and len(c.new) < len(
            c.old
        )  # pricing must never drop a line item
    if c.var in ("subtotal", "discount", "total"):
        return c.new < 0  # no negative amounts
    return False


audit = AuditTrail()

COUPONS = {
    "SAVE10": ("percent", 10),
    "FIVEOFF": ("fixed", 500),
    "BOGUS": ("fixed", 100_000),
}


@watch(
    "lines",
    "subtotal",
    "discount",
    "total",
    on_change=[
        audit,
        raise_if(pricing_rules, "pricing rule violated", exc=ArithmeticError),
    ],
)
def price_cart(cart: list[dict], coupon: str | None, tax_rate: float) -> dict:
    """All amounts in cents."""
    lines = [{**item, "amount": item["unit_cents"] * item["qty"]} for item in cart]
    lines = [
        {**line, "amount": line["amount"] * 9 // 10} if line["qty"] >= 10 else line
        for line in lines
    ]  # bulk
    subtotal = sum(line["amount"] for line in lines)

    discount = 0
    if coupon in COUPONS:
        kind, value = COUPONS[coupon]
        discount = subtotal * value // 100 if kind == "percent" else value

    total = subtotal - discount
    total = round(total * (1 + tax_rate))
    return {"lines": lines, "subtotal": subtotal, "discount": discount, "total": total}


# ---------------------------------------------------------------------------------------------------------------


def run(title: str, func, *args) -> None:
    logging.info("\n=== %s", title)
    try:
        logging.info("result: %r", func(*args))
    except Exception as e:  # noqa: BLE001 - examples show the error and carry on
        logging.info("%s: %s", type(e).__name__, e)


def main() -> None:
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")

    run("1. stock, ok", fulfil, 10, [3, 4])
    run("1. stock, oversold", fulfil, 10, [3, 4, 5])

    run(
        "2. status, ok",
        process_events,
        ["payment_received", "label_printed", "courier_confirmed"],
    )
    run(
        "2. status, ships a cancelled order",
        process_events,
        ["customer_cancelled", "label_printed"],
    )

    signups = [
        {"email": " Ann@Example.com", "age": 34},
        {"email": "bob@example.com", "age": 17},
        {"email": "", "age": 50},
        {"email": "ann@example.com ", "age": 35},
        {"email": "cy@example.com", "age": 22},
    ]
    run("3. cleaning pipeline", clean_signups, signups)

    run(
        "4. config layers",
        load_config,
        {"host": "db.internal", "retries": 3},
        {"HOME": "/home/me", "APP_TIMEOUT": "60", "APP_DEBUG": "true"},
        {"port": 9000, "host": None},
    )
    run("4. config, CLI blanks a required key", load_config, {}, {}, {"timeout": ""})

    cart = [
        {"sku": "pen", "unit_cents": 150, "qty": 12},
        {"sku": "notebook", "unit_cents": 899, "qty": 2},
    ]
    run("5. cart with SAVE10", price_cart, cart, "SAVE10", 0.08)
    audit.dump()
    audit.entries.clear()
    run(
        "5. cart with a coupon worth more than the cart",
        price_cart,
        cart,
        "BOGUS",
        0.08,
    )
    audit.dump()


if __name__ == "__main__":
    main()
