"""The AI coach's suggested questions, answered without AI (#84).

Most people open the coach with the same few questions — what did I spend
last month, what is my biggest category. Those are facts the server holds, so
they are answered here from #83's tools and a fixed sentence: no model, no
tokens, no consent check, and no chance of a wrong number.

Every figure in an answer is one a tool (or the Budget tab) returned. The
sentences only place them — and, for a person to read, format them through
`money.display`. Nothing is added, subtracted or rounded here. `figures` lists
each amount exactly as the tool returned it, so 6.4's grounding check and the
app can rely on them.

A suggestion with nothing to say — no spending last month, no budget — is left
out of the list rather than answered with zeros.

Answers are cached per (household, suggestion, month, `ledger_version`): a
write to the ledger moves the version, so the next answer is fresh on every
server, and a restart only costs recomputing. `budget_left` is the exception —
a budget changes without the ledger (a line set by hand), so it is read fresh
each time; it is one `budget_for` call, as opening the Budget tab is.
"""

from __future__ import annotations

import calendar
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import display, from_minor_units
from app.models.categorization import Category
from app.models.identity import Household
from app.services import budget as budget_service
from app.services import chat_tools, rollups
from app.services.capabilities import currency_for, resolve
from app.services.learning import learning_state

__all__ = [
    "SUGGESTIONS",
    "Answer",
    "Suggestion",
    "UnknownSuggestion",
    "answer",
    "available",
]


@dataclass(frozen=True)
class Suggestion:
    id: str
    text: str


@dataclass(frozen=True)
class Answer:
    text: str
    # Every amount in [text], as the tool returned it (a decimal string), in
    # the order the text states them. A tuple, because answers are cached and
    # shared: a caller that could append to a list would change the answer
    # every later caller is given.
    figures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "figures", tuple(self.figures))


class UnknownSuggestion(LookupError):
    """No suggestion has this id."""


# English, keyed by id so a translation can replace the text without the code.
SUGGESTIONS: tuple[Suggestion, ...] = (
    Suggestion("spent_last_month", "How much did I spend last month?"),
    Suggestion("biggest_category", "What's my biggest spending category this month?"),
    Suggestion("vs_last_month", "How does this month compare with last month?"),
    Suggestion("spent_this_year", "How much have I spent this year?"),
    Suggestion("budget_left", "How much is left in my budget this month?"),
    Suggestion("top_merchants_year", "Where did most of my money go this year?"),
)
_BY_ID = {s.id: s for s in SUGGESTIONS}

# The Budget tab's own feature (`app/api/budgets.FEATURE`). `budget_left` is
# its figure, so a household without the Budget tab is not shown it here.
BUDGET_FEATURE = "auto_budget"

# Answers whose figures depend only on the ledger. Bounded: an entry is a
# sentence, and a household asks a handful a month.
_CACHE_SIZE = 5_000
_cache: OrderedDict[tuple, Answer | None] = OrderedDict()


@dataclass(frozen=True)
class _Context:
    session: AsyncSession
    household: Household
    currency: str
    today: date

    @property
    def this_month(self) -> date:
        return self.today.replace(day=1)

    @property
    def last_month(self) -> date:
        first = self.this_month
        return date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)

    def money(self, amount: str) -> str:
        return display(amount, self.currency)


def _ym(month: date) -> str:
    return month.strftime("%Y-%m")


def _month_name(month: date) -> str:
    return calendar.month_name[month.month]


def _is_zero(amount: str) -> bool:
    return amount.strip("-0.") == ""


def _one_off_clause(ctx: _Context, one_off: str, figures: list[str]) -> str:
    if _is_zero(one_off):
        return ""
    figures.append(one_off)
    return f" That includes {ctx.money(one_off)} of one-off purchases."


async def _category_name(ctx: _Context, slug: str) -> str:
    if slug == chat_tools.UNFILED:
        return "spending not yet categorised"
    # The household's own category of that slug, else the shared one.
    result = await ctx.session.execute(
        select(Category.name)
        .where(
            Category.slug == slug,
            or_(
                Category.household_id.is_(None),
                Category.household_id == ctx.household.id,
            ),
        )
        .order_by(Category.household_id.is_(None))
        .limit(1)
    )
    return result.scalar_one_or_none() or slug.replace("_", " ")


# --- The six answers ---------------------------------------------------------------


async def _spent_last_month(ctx: _Context) -> Answer | None:
    month = _ym(ctx.last_month)
    result = await chat_tools.spending_summary(
        ctx.session,
        ctx.household.id,
        ctx.currency,
        ctx.today,
        from_month=month,
        to_month=month,
        group_by="none",
    )
    if result["n"] == 0:
        return None
    figures = [result["total"]]
    text = f"You spent {ctx.money(result['total'])} in {_month_name(ctx.last_month)}."
    return Answer(text + _one_off_clause(ctx, result["one_off"], figures), figures)


async def _biggest_category(ctx: _Context) -> Answer | None:
    month = _ym(ctx.this_month)
    result = await chat_tools.spending_summary(
        ctx.session,
        ctx.household.id,
        ctx.currency,
        ctx.today,
        from_month=month,
        to_month=month,
        group_by="category",
    )
    if not result["rows"]:
        return None
    top = result["rows"][0]
    name = await _category_name(ctx, top["cat"])
    return Answer(
        f"Your biggest category this month is {name}: {ctx.money(top['t'])} "
        f"of the {ctx.money(result['total'])} you've spent so far.",
        [top["t"], result["total"]],
    )


async def _vs_last_month(ctx: _Context) -> Answer | None:
    last, this = _ym(ctx.last_month), _ym(ctx.this_month)
    result = await chat_tools.period_compare(
        ctx.session,
        ctx.household.id,
        ctx.currency,
        ctx.today,
        a_from=last,
        a_to=last,
        b_from=this,
        b_to=this,
    )
    total = result["total"]
    if _is_zero(total["a"]) or _is_zero(total["b"]):
        # Nothing to compare: one of the two months has no spending.
        return None
    diff = total["diff"]
    if _is_zero(diff):
        change = "exactly the same."
    else:
        # The difference as the tool worked it out, its sign put in words.
        size = diff.lstrip("-")
        change = f"{ctx.money(size)} {'less' if diff.startswith('-') else 'more'}."
    return Answer(
        f"So far this month you've spent {ctx.money(total['b'])}, against "
        f"{ctx.money(total['a'])} in all of {_month_name(ctx.last_month)} — "
        f"{change}",
        [total["b"], total["a"], *([] if _is_zero(diff) else [diff.lstrip("-")])],
    )


async def _spent_this_year(ctx: _Context) -> Answer | None:
    result = await chat_tools.spending_summary(
        ctx.session,
        ctx.household.id,
        ctx.currency,
        ctx.today,
        from_month=f"{ctx.today.year}-01",
        to_month=_ym(ctx.this_month),
        group_by="none",
    )
    if result["n"] == 0:
        return None
    figures = [result["total"]]
    text = f"You've spent {ctx.money(result['total'])} so far in {ctx.today.year}."
    return Answer(text + _one_off_clause(ctx, result["one_off"], figures), figures)


async def _budget_left(ctx: _Context) -> Answer | None:
    feature = (await resolve(ctx.session, ctx.household)).features.get(BUDGET_FEATURE)
    if feature is None or not feature.enabled:
        return None
    learning = await learning_state(
        ctx.session, ctx.household.id, ctx.currency, ctx.today
    )
    view = await budget_service.budget_for(
        ctx.session,
        ctx.household.id,
        ctx.currency,
        ctx.this_month,
        ctx.today,
        ready=learning.ready,
    )
    spending_lines = view.total_allocated - (
        view.savings.allocated if view.savings is not None else 0
    )
    left = view.left_to_spend
    if left is None or spending_lines <= 0:
        # No budget to have anything left of.
        return None
    amount = from_minor_units(left, ctx.currency)
    if left >= 0:
        return Answer(
            f"You have {ctx.money(amount)} left to spend in your budget this month.",
            [amount],
        )
    over = from_minor_units(-left, ctx.currency)
    return Answer(f"You're {ctx.money(over)} over your budget this month.", [over])


async def _top_merchants_year(ctx: _Context) -> Answer | None:
    result = await chat_tools.top_merchants(
        ctx.session,
        ctx.household.id,
        ctx.currency,
        ctx.today,
        from_date=f"{ctx.today.year}-01-01",
        to_date=ctx.today.isoformat(),
        limit=3,
    )
    rows = result["rows"]
    if not rows:
        return None
    named = [f"{r['m']} ({ctx.money(r['t'])})" for r in rows]
    listed = (
        named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"
    )
    return Answer(
        f"So far this year, most of your money went to {listed}.",
        [r["t"] for r in rows],
    )


_ANSWERS: dict[str, Callable[[_Context], Awaitable[Answer | None]]] = {
    "spent_last_month": _spent_last_month,
    "biggest_category": _biggest_category,
    "vs_last_month": _vs_last_month,
    "spent_this_year": _spent_this_year,
    "budget_left": _budget_left,
    "top_merchants_year": _top_merchants_year,
}
# Read fresh every time: a budget changes without the ledger changing.
_UNCACHED = frozenset({"budget_left"})


# --- Entry points ------------------------------------------------------------------


async def answer(
    session: AsyncSession, household: Household, suggestion_id: str, today: date
) -> Answer | None:
    """The answer to [suggestion_id] for [household] now, or None when it has
    nothing to say. Never builds a model client. Raises `UnknownSuggestion`."""
    if suggestion_id not in _BY_ID:
        raise UnknownSuggestion(suggestion_id)
    ctx = _Context(session, household, await currency_for(session, household), today)
    if suggestion_id in _UNCACHED:
        return await _ANSWERS[suggestion_id](ctx)

    version = await rollups.ledger_version(session, household.id)
    key = (household.id, suggestion_id, ctx.this_month, ctx.currency, version)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    result = await _ANSWERS[suggestion_id](ctx)
    _cache[key] = result
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return result


async def available(
    session: AsyncSession, household: Household, today: date
) -> list[Suggestion]:
    """The suggestions [household] has something to say about, in order."""
    return [
        s
        for s in SUGGESTIONS
        if await answer(session, household, s.id, today) is not None
    ]
