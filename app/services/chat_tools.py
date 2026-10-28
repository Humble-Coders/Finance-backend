"""The AI coach's data tools: fixed questions over any date range, answered
as compact summaries (#83).

The model never reads raw transactions and never does arithmetic. It asks one
of these — "spending by year from 2016 to 2025", "the two periods side by
side" — and the server answers from the database: totals from the monthly
rollup (`rollups`), so ten years is a handful of rows; individual rows only
from `transaction_search`, and then at most 50 with the true count and total
of everything matched.

What every result promises, because a model reading it cannot check:

* **The dashboard's figures.** The household's own currency only, no
  unresolved suspected duplicates (`countable`), and — for spending and
  income — no money moved between the household's own accounts
  (`NOT_A_FLOW`), exactly as `/dashboard` counts.
* **Compact.** Short keys, money as decimal strings (`app/core/money.py`),
  no ids, no account numbers; always `row_count` and `truncated`.
* **Never a silent partial list.** When rows are cut, `truncated` is true and
  the totals still cover everything matched.

Wrong input — a date in the future, a range back to front — raises
`ToolError`, which goes back to the model to correct (`llm.run_tools`).
Nothing here calls a model; 6.4 registers these with the loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.models.categorization import Category
from app.models.derived import MonthlyCategoryTotal
from app.models.enums import TransactionDirection
from app.models.money import Transaction
from app.services.dashboard import NOT_A_FLOW, countable, parse_month
from app.services.llm import ToolError, ToolSpec

__all__ = [
    "MAX_SEARCH_ROWS",
    "MAX_TOP_MERCHANTS",
    "TOOLS",
    "data_range",
    "handlers_for",
    "period_compare",
    "spending_summary",
    "top_merchants",
    "transaction_search",
]

MAX_SEARCH_ROWS = 50
MAX_TOP_MERCHANTS = 10

# The name a row with no category is reported under.
UNFILED = "uncategorized"

_DIRECTIONS = {"out": "debit", "in": "credit"}
_GROUPS = ("year", "month", "category", "none")


# --- Input ----------------------------------------------------------------------


def _month(raw: object, field: str) -> date:
    try:
        return parse_month(str(raw))
    except ValueError as exc:
        raise ToolError(f"{field} must be a month as YYYY-MM") from exc


def _day(raw: object, field: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ToolError(f"{field} must be a date as YYYY-MM-DD") from exc


def _range(start: date, end: date, today: date, *, months: bool) -> tuple[date, date]:
    """[start, end] checked: in order, and not starting in the future. An end
    in the future is brought back to today — "this year" is a fair question
    in March — and the result says which range it covers."""
    latest = today.replace(day=1) if months else today
    if start > latest:
        raise ToolError("the range starts in the future; there is no data yet")
    if start > end:
        raise ToolError("the range is back to front: from is after to")
    return start, min(end, latest)


def _direction(raw: object) -> str:
    value = raw or "out"
    if value not in _DIRECTIONS:
        raise ToolError("direction must be 'out' (spending) or 'in' (income)")
    return str(value)


def _limit(raw: object, ceiling: int) -> int:
    if raw is None:
        return ceiling
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise ToolError(f"limit must be a whole number from 1 to {ceiling}")
    return min(raw, ceiling)


def _amount(raw: object, currency: str, field: str) -> int | None:
    if raw is None:
        return None
    try:
        return abs(to_minor_units(str(raw), currency, allow_rounding=True))
    except MoneyError as exc:
        raise ToolError(f"{field} must be an amount like 100 or 99.50") from exc


def _ym(day: date) -> str:
    return day.strftime("%Y-%m")


def _last_day(month: date) -> date:
    following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def _pct(before: int, after: int) -> str | None:
    """The change from [before] to [after] in percent, one decimal place, or
    None when there was nothing before — a change from zero has no percent."""
    if before == 0:
        return None
    change = (Decimal(after - before) * 100 / Decimal(before)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return str(change)


# --- Reading the rollup ----------------------------------------------------------


def _flow_totals(currency: str, household_id: uuid.UUID, direction: str) -> list:
    """The rollup rows a spending or income figure is made of: the dashboard's
    rule, applied to totals instead of transactions."""
    return [
        MonthlyCategoryTotal.household_id == household_id,
        MonthlyCategoryTotal.currency == currency,
        MonthlyCategoryTotal.direction == _DIRECTIONS[direction],
        or_(
            MonthlyCategoryTotal.category_slug.is_(None),
            MonthlyCategoryTotal.category_slug.not_in(NOT_A_FLOW),
        ),
    ]


def _slug():
    return func.coalesce(MonthlyCategoryTotal.category_slug, UNFILED)


async def spending_summary(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    today: date,
    *,
    from_month: str,
    to_month: str,
    group_by: str,
    categories: list[str] | None = None,
    direction: str | None = None,
) -> dict:
    """Money out (or in) over a range of months, grouped by year, month,
    category, or not at all. Reads the monthly rollup only."""
    start, end = _range(
        _month(from_month, "from_month"),
        _month(to_month, "to_month"),
        today,
        months=True,
    )
    if group_by not in _GROUPS:
        raise ToolError(f"group_by must be one of {list(_GROUPS)}")
    way = _direction(direction)

    key = {
        "year": func.extract("year", MonthlyCategoryTotal.month),
        "month": MonthlyCategoryTotal.month,
        "category": _slug(),
        "none": None,
    }[group_by]
    columns = [
        func.sum(MonthlyCategoryTotal.total_minor_units),
        func.sum(MonthlyCategoryTotal.one_off_minor_units),
        func.sum(MonthlyCategoryTotal.transaction_count),
    ]
    query = select(*([key] if key is not None else []), *columns).where(
        *_flow_totals(currency, household_id, way),
        MonthlyCategoryTotal.month >= start,
        MonthlyCategoryTotal.month <= end,
    )
    if categories:
        query = query.where(_slug().in_([str(c) for c in categories]))
    if key is not None:
        query = query.group_by(key)
        # Periods in time order; categories largest first.
        query = query.order_by(
            func.sum(MonthlyCategoryTotal.total_minor_units).desc()
            if group_by == "category"
            else key
        )
    result = (await session.execute(query)).all()

    rows = []
    total = one_off_total = count_total = 0
    for row in result:
        *label, amount, one_off, count = row
        amount, one_off, count = int(amount or 0), int(one_off or 0), int(count or 0)
        if group_by == "none" and count == 0:
            continue
        total, one_off_total = total + amount, one_off_total + one_off
        count_total += count
        entry = {
            "t": from_minor_units(amount, currency),
            "one_off": from_minor_units(one_off, currency),
            "n": count,
        }
        if group_by == "year":
            entry = {"p": str(int(label[0])), **entry}
        elif group_by == "month":
            entry = {"p": _ym(label[0]), **entry}
        elif group_by == "category":
            entry = {"cat": label[0], **entry}
        rows.append(entry)

    return {
        "from": _ym(start),
        "to": _ym(end),
        "cur": currency,
        "dir": way,
        "by": group_by,
        "rows": rows,
        "total": from_minor_units(total, currency),
        "one_off": from_minor_units(one_off_total, currency),
        "n": count_total,
        "row_count": len(rows),
        "truncated": False,
    }


async def period_compare(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    today: date,
    *,
    a_from: str,
    a_to: str,
    b_from: str,
    b_to: str,
    group_by: str = "none",
    direction: str | None = None,
) -> dict:
    """Two ranges of months side by side, with the differences worked out
    here — `diff` is b minus a, `pct` the change in percent — so the model
    never subtracts."""
    if group_by not in ("category", "none"):
        raise ToolError("group_by must be 'category' or 'none'")
    way = _direction(direction)
    ranges = [
        _range(_month(a_from, "a_from"), _month(a_to, "a_to"), today, months=True),
        _range(_month(b_from, "b_from"), _month(b_to, "b_to"), today, months=True),
    ]

    sums: list[dict[str, int]] = []
    for start, end in ranges:
        key = _slug() if group_by == "category" else literal("all")
        result = await session.execute(
            select(key, func.sum(MonthlyCategoryTotal.total_minor_units))
            .where(
                *_flow_totals(currency, household_id, way),
                MonthlyCategoryTotal.month >= start,
                MonthlyCategoryTotal.month <= end,
            )
            .group_by(key)
        )
        sums.append({label: int(amount or 0) for label, amount in result.all()})

    a, b = sums
    labels = sorted(set(a) | set(b), key=lambda k: -(abs(b.get(k, 0) - a.get(k, 0))))

    def entry(label: str) -> dict:
        before, after = a.get(label, 0), b.get(label, 0)
        return {
            **({"cat": label} if group_by == "category" else {}),
            "a": from_minor_units(before, currency),
            "b": from_minor_units(after, currency),
            "diff": from_minor_units(after - before, currency),
            "pct": _pct(before, after),
        }

    rows = [entry(label) for label in labels] if group_by == "category" else []
    total_a, total_b = sum(a.values()), sum(b.values())
    return {
        "a": {"from": _ym(ranges[0][0]), "to": _ym(ranges[0][1])},
        "b": {"from": _ym(ranges[1][0]), "to": _ym(ranges[1][1])},
        "cur": currency,
        "dir": way,
        "by": group_by,
        "rows": rows,
        "total": {
            "a": from_minor_units(total_a, currency),
            "b": from_minor_units(total_b, currency),
            "diff": from_minor_units(total_b - total_a, currency),
            "pct": _pct(total_a, total_b),
        },
        "row_count": len(rows),
        "truncated": False,
    }


# --- Reading transactions ----------------------------------------------------------


def _escaped(text: str) -> str:
    """[text] for ILIKE, with its own wildcards taken literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def transaction_search(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    today: date,
    *,
    from_date: str,
    to_date: str,
    merchant: str | None = None,
    category: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    direction: str | None = None,
    limit: int | None = None,
) -> dict:
    """Transactions matching a filter, newest first: at most [limit] rows, and
    always the count and totals of everything matched."""
    start, end = _range(
        _day(from_date, "from_date"), _day(to_date, "to_date"), today, months=False
    )
    cap = _limit(limit, MAX_SEARCH_ROWS)
    low = _amount(min_amount, currency, "min_amount")
    high = _amount(max_amount, currency, "max_amount")
    if low is not None and high is not None and low > high:
        raise ToolError("min_amount is more than max_amount")

    slug = func.coalesce(Category.slug, UNFILED)
    conditions = [
        *countable(household_id, currency),
        Transaction.occurred_on >= start,
        Transaction.occurred_on <= end,
    ]
    if direction is not None:
        conditions.append(
            Transaction.direction
            == TransactionDirection(_DIRECTIONS[_direction(direction)])
        )
    if merchant:
        conditions.append(
            Transaction.merchant.ilike(f"%{_escaped(str(merchant))}%", escape="\\")
        )
    if category:
        conditions.append(slug == str(category))
    if low is not None:
        conditions.append(Transaction.amount_minor_units >= low)
    if high is not None:
        conditions.append(Transaction.amount_minor_units <= high)

    def matched(*columns):
        return (
            select(*columns)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(*conditions)
        )

    debit = Transaction.direction == TransactionDirection.debit
    totals = (
        await session.execute(
            matched(
                func.count(Transaction.id),
                func.coalesce(
                    func.sum(case((debit, Transaction.amount_minor_units), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((~debit, Transaction.amount_minor_units), else_=0)), 0
                ),
            )
        )
    ).one()
    count, out_total, in_total = int(totals[0]), int(totals[1]), int(totals[2])

    result = await session.execute(
        matched(
            Transaction.occurred_on,
            Transaction.merchant,
            Transaction.amount_minor_units,
            Transaction.direction,
            slug,
        )
        .order_by(Transaction.occurred_on.desc(), Transaction.amount_minor_units.desc())
        .limit(cap)
    )
    rows = [
        {
            "d": occurred_on.isoformat(),
            "m": name or "",
            "a": from_minor_units(amount, currency),
            "dir": "out" if way == TransactionDirection.debit else "in",
            "cat": cat,
        }
        for occurred_on, name, amount, way, cat in result.all()
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "cur": currency,
        "rows": rows,
        "total_count": count,
        "total_out": from_minor_units(out_total, currency),
        "total_in": from_minor_units(in_total, currency),
        "row_count": count,
        "truncated": count > len(rows),
    }


async def top_merchants(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    today: date,
    *,
    from_date: str,
    to_date: str,
    category: str | None = None,
    limit: int | None = None,
) -> dict:
    """Where the money went: spending by merchant, largest first. Moves
    between the household's own accounts are not spending, and are left out
    as the dashboard leaves them out."""
    start, end = _range(
        _day(from_date, "from_date"), _day(to_date, "to_date"), today, months=False
    )
    cap = _limit(limit, MAX_TOP_MERCHANTS)
    slug = func.coalesce(Category.slug, UNFILED)
    conditions = [
        *countable(household_id, currency),
        Transaction.direction == TransactionDirection.debit,
        Transaction.merchant.is_not(None),
        Transaction.occurred_on >= start,
        Transaction.occurred_on <= end,
        or_(Category.slug.is_(None), Category.slug.not_in(NOT_A_FLOW)),
    ]
    if category:
        conditions.append(slug == str(category))

    by_merchant = (
        select(
            Transaction.merchant.label("m"),
            func.sum(Transaction.amount_minor_units).label("t"),
            func.count(Transaction.id).label("n"),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*conditions)
        .group_by(Transaction.merchant)
        .subquery()
    )
    result = await session.execute(
        select(
            by_merchant.c.m,
            by_merchant.c.t,
            by_merchant.c.n,
            func.count().over(),
            func.sum(by_merchant.c.t).over(),
        )
        .order_by(by_merchant.c.t.desc(), by_merchant.c.m)
        .limit(cap)
    )
    found = result.all()
    merchants = int(found[0][3]) if found else 0
    spent = int(found[0][4]) if found else 0
    rows = [
        {"m": name, "t": from_minor_units(int(total), currency), "n": int(n)}
        for name, total, n, _, _ in found
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "cur": currency,
        "rows": rows,
        "total": from_minor_units(spent, currency),
        "row_count": merchants,
        "truncated": merchants > len(rows),
    }


async def data_range(
    session: AsyncSession, household_id: uuid.UUID, currency: str
) -> tuple[date, date] | None:
    """The first and last month this household has money in, or None — for
    6.4's system prompt, so the model knows what range it may ask about."""
    result = await session.execute(
        select(
            func.min(MonthlyCategoryTotal.month), func.max(MonthlyCategoryTotal.month)
        ).where(
            MonthlyCategoryTotal.household_id == household_id,
            MonthlyCategoryTotal.currency == currency,
        )
    )
    first, last = result.one()
    return (first, last) if first is not None else None


# --- What the model is told ----------------------------------------------------------

_MONTH_FIELD = {"type": "string", "description": "A month as YYYY-MM."}
_DATE_FIELD = {"type": "string", "description": "A date as YYYY-MM-DD."}
_DIRECTION_FIELD = {
    "type": "string",
    "enum": ["out", "in"],
    "description": "out = spending (the default), in = income.",
}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="spending_summary",
        description=(
            "Total spending (or income) over a range of months, grouped by year, "
            "month or category, or one total. Use for any period question, "
            "however long. Totals include one-off purchases, also given apart."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_month": _MONTH_FIELD,
                "to_month": _MONTH_FIELD,
                "group_by": {"type": "string", "enum": list(_GROUPS)},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Category slugs to keep, e.g. ['groceries'].",
                },
                "direction": _DIRECTION_FIELD,
            },
            "required": ["from_month", "to_month", "group_by"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="period_compare",
        description=(
            "Two ranges of months side by side, with the difference and percent "
            "change already worked out. Use instead of subtracting."
        ),
        parameters={
            "type": "object",
            "properties": {
                "a_from": _MONTH_FIELD,
                "a_to": _MONTH_FIELD,
                "b_from": _MONTH_FIELD,
                "b_to": _MONTH_FIELD,
                "group_by": {"type": "string", "enum": ["category", "none"]},
                "direction": _DIRECTION_FIELD,
            },
            "required": ["a_from", "a_to", "b_from", "b_to"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="transaction_search",
        description=(
            f"Find transactions by date range, merchant, category or amount. "
            f"Returns at most {MAX_SEARCH_ROWS} rows, newest first, plus the count "
            f"and totals of everything matched; truncated=true means more matched "
            f"than are listed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_date": _DATE_FIELD,
                "to_date": _DATE_FIELD,
                "merchant": {
                    "type": "string",
                    "description": "Part of the merchant name; any case.",
                },
                "category": {"type": "string", "description": "A category slug."},
                "min_amount": {"type": "string", "description": "e.g. '100'."},
                "max_amount": {"type": "string", "description": "e.g. '250.00'."},
                "direction": _DIRECTION_FIELD,
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_ROWS},
            },
            "required": ["from_date", "to_date"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="top_merchants",
        description=(
            f"Where the money went: spending by merchant, largest first, at most "
            f"{MAX_TOP_MERCHANTS}, with the total and how many merchants there were."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_date": _DATE_FIELD,
                "to_date": _DATE_FIELD,
                "category": {"type": "string", "description": "A category slug."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TOP_MERCHANTS,
                },
            },
            "required": ["from_date", "to_date"],
            "additionalProperties": False,
        },
    ),
)

_FUNCTIONS = {
    "spending_summary": spending_summary,
    "period_compare": period_compare,
    "transaction_search": transaction_search,
    "top_merchants": top_merchants,
}


def handlers_for(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> dict[str, Callable[[dict], Awaitable[dict]]]:
    """The tools bound to one household, as `llm.run_tools` takes them.

    The household, its currency and today come from the server, never from
    the model's arguments — there is no argument that names a household.
    """

    def bind(function):
        async def handler(arguments: dict) -> dict:
            return await function(session, household_id, currency, today, **arguments)

        return handler

    return {name: bind(function) for name, function in _FUNCTIONS.items()}
