"""The monthly dashboard: what actually happened, against what was expected.

**The one rule this module exists to enforce: obligations are never added to
anything.** An obligation is a declaration of what somebody expects to pay; a
transaction is a record of what happened. They answer different questions, and
summing them double-counts every commitment the statement also shows — rent
typed into the wizard and then imported from the bank is one payment, not two.

So there is exactly one sum here, over transactions, and the obligations appear
beside it as a checklist the transactions tick off. No function in this module
returns expectations and actuals added together, and none should be added.

Everything is scoped to one calendar month and one currency. The figures are
computed here rather than on each platform because Android and iOS must not be
able to disagree about a number the user is making decisions from
(kmp-arch-v2's reasoning, applied across the wire).
"""

from __future__ import annotations

import calendar
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.categorization import Category
from app.models.enums import ReviewReason, TransactionDirection
from app.models.money import StatementImport, Transaction
from app.models.planning import Debt
from app.models.setup import FinancialProfile, Investment, Obligation

# How many months the sparkline covers, including the one being viewed.
TREND_MONTHS = 10

# How far a payment may sit from the commitment it is matched to. A phone bill
# or a hydro bill is never the same twice, so exact matching would report
# "not seen" for things plainly paid; 15% is wide enough for a variable utility
# and narrow enough that a different payment of similar size does not qualify
# on amount alone — it must clear the name test too.
MATCH_TOLERANCE_BPS = 1500

# Tokens too generic to identify anything. "Payment to rent" and "monthly
# payment" would otherwise match on `payment` alone.
STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "monthly",
        "payment",
        "payments",
        "bill",
        "bills",
        "fee",
        "fees",
        "plan",
        "account",
        "inc",
        "ltd",
        "llc",
        "co",
        "corp",
    }
)

_WORD = re.compile(r"[a-z]+")

# The seeded category a movement against each balance is filed under. There is
# no `investments` category — money set aside is `savings` — so an investment
# card's movement is "set aside this month", which is what the figure honestly
# is. Slugs rather than ids: ids differ per environment, the taxonomy does not.
DEBT_PAYMENT_SLUG = "debt_payment"
SAVINGS_SLUG = "savings"
TRANSFERS_SLUG = "transfers"

# Money that only moved between the household's own accounts. A card bill paid
# from chequing is a debit on one statement and a credit on the other; money
# put into savings is a debit here and a deposit there. Counted as flows, a
# card bill is spent twice and earned once, and saving lowers what the month
# kept — the opposite of what the figure is for. So rows filed under these
# slugs are in neither income nor expenses (#73). Savings still shows, as what
# was set aside, on the investments card.
NOT_A_FLOW = (SAVINGS_SLUG, TRANSFERS_SLUG)


_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def parse_month(raw: str) -> date:
    """`YYYY-MM` as the first of that month, or `ValueError`.

    Shared by `/dashboard` and `/transactions` so the two cannot disagree about
    what a month is. Raises rather than returning None: a caller needs to tell
    "not asked for" from "asked for badly", and only the router knows that one
    is a 422.
    """
    found = _MONTH.match(raw.strip())
    if found is None:
        raise ValueError(f"expected YYYY-MM, got {raw!r}")
    year, month = int(found.group(1)), int(found.group(2))
    if not 1 <= month <= 12 or not 1970 <= year <= 2999:
        raise ValueError(f"expected YYYY-MM, got {raw!r}")
    return date(year, month, 1)


def month_bounds(month: date) -> tuple[date, date]:
    """First and last day of [month]'s calendar month, inclusive.

    Inclusive on both ends because `occurred_on` is a date, not a timestamp:
    a half-open range on dates invites an off-by-one that silently drops
    everything that happened on the 31st.
    """
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def months_back(month: date, count: int) -> list[date]:
    """[count] month-starts ending at [month], oldest first."""
    first = month.replace(day=1)
    out: list[date] = []
    year, m = first.year, first.month
    for _ in range(count):
        out.append(date(year, m, 1))
        m -= 1
        if m == 0:
            year, m = year - 1, 12
    return list(reversed(out))


@dataclass(frozen=True)
class Flow:
    """A month's movement in one direction, and what was expected of it."""

    actual_minor_units: int = 0
    expected_minor_units: int | None = None
    # Last month's actual; None when last month has no rows at all, so a
    # client omits "vs last month" rather than claiming a rise from nothing.
    previous_minor_units: int | None = None


@dataclass(frozen=True)
class Stock:
    """A balance the user maintains, and this month's movement against it.

    Two numbers because they come from different places and only one of them
    is observed. We never see a portfolio's market value or a loan's
    outstanding principal — only what moved through the bank — so the balance
    stays whatever the wizard was told, and the movement is ours.
    """

    balance_minor_units: int = 0
    moved_minor_units: int = 0
    # Last month's movement; None when last month has no rows at all.
    previous_moved_minor_units: int | None = None
    # Credits under the same category this month: money taken back out of
    # savings. Observed, like `moved`, and never netted against it — both are
    # shown, because "put in 500, took out 500" is not "did nothing".
    withdrawn_minor_units: int = 0


@dataclass(frozen=True)
class Match:
    """The transaction a commitment was paid by, as far as we can tell."""

    transaction_id: uuid.UUID
    occurred_on: date
    amount_minor_units: int
    description: str | None


@dataclass(frozen=True)
class Commitment:
    """One obligation, and whether a payment for it turned up this month.

    [match] being None means **not seen**, which is not the same as not paid:
    a commitment settled in cash, from another account, or under a name the
    statement writes differently will not be found. The wording the client
    shows has to say "not seen this month" and never "unpaid".
    """

    name: str
    expected_minor_units: int
    match: Match | None = None
    due_day: int | None = None


@dataclass(frozen=True)
class MonthPoint:
    """One bar of the trend.

    [has_data] distinguishes a month that netted zero from a month we know
    nothing about. Drawing the second as a zero-height bar states a fact that
    was never observed, which is the one thing a chart must not do.
    """

    month: date
    net_minor_units: int | None = None
    # The month's parts, all None together when it has no rows: a gap, as for
    # `net`. Each is counted by the rule its dashboard figure uses.
    income_minor_units: int | None = None
    expenses_minor_units: int | None = None
    invested_minor_units: int | None = None
    withdrawn_minor_units: int | None = None
    debt_paid_minor_units: int | None = None

    @property
    def has_data(self) -> bool:
        return self.net_minor_units is not None


@dataclass(frozen=True)
class MonthFigures:
    """One month's totals, from one grouped query; see `figures_by_month`."""

    income: int = 0
    expenses: int = 0
    invested: int = 0
    withdrawn: int = 0
    debt_paid: int = 0

    @property
    def net(self) -> int:
        return self.income - self.expenses


@dataclass(frozen=True)
class DayPoint:
    """The month's running balance at the end of one day.

    In minus out, from the first of the month through [day] — so the last
    point is the month's net exactly, counted by the same rule.
    """

    day: date
    net_minor_units: int


@dataclass(frozen=True)
class Dashboard:
    month: date
    currency: str
    income: Flow = field(default_factory=Flow)
    expenses: Flow = field(default_factory=Flow)
    investments: Stock = field(default_factory=Stock)
    debts: Stock = field(default_factory=Stock)
    commitments: list[Commitment] = field(default_factory=list)
    trend: list[MonthPoint] = field(default_factory=list)
    daily: list[DayPoint] = field(default_factory=list)
    previous_net_minor_units: int | None = None
    pending_review: int = 0

    @property
    def net_minor_units(self) -> int:
        """The hero figure: what the month kept.

        Income minus expenses, and nothing else — commitments are already
        inside expenses when the statement shows them, and adding the wizard's
        figure on top is exactly the double count this module refuses.
        """
        return self.income.actual_minor_units - self.expenses.actual_minor_units


def countable(household_id: uuid.UUID, currency: str) -> list:
    """The rows a total may include — on the dashboard, in a budget's spend,
    and in the "still learning" count. One rule, so no two of them disagree.

    Two exclusions, both about not stating something we do not know:

    * **Another currency.** Adding CAD to USD produces a number that is not
      money. Rows in a currency other than the household's are left out rather
      than converted, because converting needs a rate and a date we do not have.
    * **An unresolved suspected duplicate.** The row is there *because* we think
      it is the same payment twice. Counting it inflates the month until
      somebody confirms it is not a duplicate, at which point it counts. A
      figure that corrects itself downward after review is worse than one that
      was never wrong.
    """
    return [
        Transaction.household_id == household_id,
        Transaction.currency == currency,
        ~and_(
            Transaction.needs_review.is_(True),
            Transaction.review_reason == ReviewReason.suspected_duplicate,
        ),
    ]


def is_flow():
    """The rows income and expenses are made of: everything `countable` that
    is not money moving between the household's own accounts (`NOT_A_FLOW`).

    An unfiled row is a flow: it is money in or out until somebody says
    otherwise, as it always was. The query must outer-join `Category` on the
    row's category — this reads its slug, and a household's own category of
    that slug means the same thing.

    A condition rather than a filter, used inside the sums, so a month whose
    only rows are transfers still has data — it is a month we hold a statement
    for, in which nothing came in or went out.
    """
    return or_(Category.slug.is_(None), Category.slug.not_in(NOT_A_FLOW))


def _in_month(month: date) -> list:
    first, last = month_bounds(month)
    return [Transaction.occurred_on >= first, Transaction.occurred_on <= last]


def _sums_by_direction(household_id: uuid.UUID, currency: str, month: date) -> Select:
    flow = is_flow()
    return (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                flow,
                                Transaction.direction == TransactionDirection.credit,
                            ),
                            Transaction.amount_minor_units,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                flow,
                                Transaction.direction == TransactionDirection.debit,
                            ),
                            Transaction.amount_minor_units,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.count(Transaction.id),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*countable(household_id, currency), *_in_month(month))
    )


def _tokens(text: str) -> set[str]:
    """Identifying words, lowercased. Short and generic ones carry no signal."""
    return {
        word
        for word in _WORD.findall(text.lower())
        if len(word) >= 3 and word not in STOPWORDS
    }


def _within_tolerance(expected: int, actual: int) -> bool:
    if expected <= 0:
        return False
    return abs(actual - expected) * 10_000 <= expected * MATCH_TOLERANCE_BPS


def match_commitment(
    obligation_name: str,
    expected_minor_units: int,
    candidates: list[Transaction],
) -> Transaction | None:
    """The payment that settles this commitment, or None.

    **Both tests must pass: the name and the amount.** Either alone produces
    false positives, and a false positive here is the expensive mistake — the
    screen would tell somebody their rent went out when it did not. A false
    negative only says "not seen", which the user can read past.

    Among several, the closest amount wins; a tie goes to the earliest, since
    a recurring commitment is usually paid at the start of its period.
    """
    wanted = _tokens(obligation_name)
    if not wanted:
        return None

    viable = [
        row
        for row in candidates
        if _within_tolerance(expected_minor_units, row.amount_minor_units)
        and wanted
        <= _tokens(
            " ".join(
                filter(
                    None, (row.merchant, row.normalized_description, row.description)
                )
            )
        )
    ]
    if not viable:
        return None
    return min(
        viable,
        key=lambda row: (
            abs(row.amount_minor_units - expected_minor_units),
            row.occurred_on,
        ),
    )


async def figures_by_month(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    months: list[date],
) -> dict[date, MonthFigures]:
    """Each month's income, expenses and category movements, for the months
    that have rows at all.

    One grouped query rather than one per month and figure: six months of five
    figures is thirty round trips otherwise. Months absent from the result have
    no data, which callers render as a gap and never as zero.

    Counted by `countable`, the rule every figure on the dashboard uses, so a
    trend bar and the card it sits under can never disagree; income and
    expenses are the flows among those rows (`is_flow`). Invested and
    debt-paid match `_moved_by_category`: debits filed under that slug, the
    household's own category of the slug included.
    """
    if not months:
        return {}
    start, _ = month_bounds(months[0])
    _, end = month_bounds(months[-1])
    bucket = func.date_trunc("month", Transaction.occurred_on)
    credit = Transaction.direction == TransactionDirection.credit
    debit = Transaction.direction == TransactionDirection.debit
    amount = Transaction.amount_minor_units
    flow = is_flow()

    def total(*when) -> object:
        return func.coalesce(func.sum(case((and_(*when), amount), else_=0)), 0)

    result = await session.execute(
        select(
            bucket,
            total(credit, flow),
            total(debit, flow),
            total(debit, Category.slug == SAVINGS_SLUG),
            total(credit, Category.slug == SAVINGS_SLUG),
            total(debit, Category.slug == DEBT_PAYMENT_SLUG),
        )
        # Outer: an unfiled row still counts toward income and expenses.
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            *countable(household_id, currency),
            Transaction.occurred_on >= start,
            Transaction.occurred_on <= end,
        )
        .group_by(bucket)
    )
    return {
        row[0].date(): MonthFigures(
            income=int(row[1]),
            expenses=int(row[2]),
            invested=int(row[3]),
            withdrawn=int(row[4]),
            debt_paid=int(row[5]),
        )
        for row in result
    }


async def build(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date | None = None,
) -> Dashboard:
    """Everything the dashboard shows for one month.

    Reads the wizard's figures as *expectations* and the transactions as
    *actuals*, and never adds the two together. See this module's docstring.
    """
    first, last = month_bounds(month)

    credits, debits, counted = (
        await session.execute(_sums_by_direction(household_id, currency, month))
    ).one()

    pending = await session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.household_id == household_id,
            Transaction.needs_review.is_(True),
            *_in_month(month),
        )
    )

    profile = await session.scalar(
        select(FinancialProfile).where(FinancialProfile.household_id == household_id)
    )

    holdings = await session.scalar(
        select(func.coalesce(func.sum(Investment.amount_minor_units), 0)).where(
            Investment.household_id == household_id,
            Investment.currency == currency,
        )
    )
    outstanding = await session.scalar(
        select(func.coalesce(func.sum(Debt.balance_minor_units), 0)).where(
            Debt.household_id == household_id,
            Debt.currency == currency,
        )
    )

    moved = await _moved_by_category(
        session, household_id, currency, month, (SAVINGS_SLUG, DEBT_PAYMENT_SLUG)
    )

    # Every debit in the month, once, and then matched in memory. The
    # alternative is a query per obligation, which is a query per row of a list
    # the user controls the length of.
    debits_this_month = list(
        (
            await session.execute(
                select(Transaction).where(
                    *countable(household_id, currency),
                    *_in_month(month),
                    Transaction.direction == TransactionDirection.debit,
                )
            )
        )
        .scalars()
        .all()
    )

    obligations = list(
        (
            await session.execute(
                select(Obligation)
                .where(
                    Obligation.household_id == household_id,
                    Obligation.currency == currency,
                )
                .order_by(Obligation.position, Obligation.name)
            )
        )
        .scalars()
        .all()
    )

    # A transaction settles at most one commitment. Without this, two
    # commitments of a similar size both claim the same payment and the
    # checklist says two things were paid when one was.
    claimed: set[uuid.UUID] = set()
    commitments: list[Commitment] = []
    for obligation in obligations:
        available = [row for row in debits_this_month if row.id not in claimed]
        found = match_commitment(
            obligation.name, obligation.monthly_amount_minor_units, available
        )
        if found is not None:
            claimed.add(found.id)
        commitments.append(
            Commitment(
                name=obligation.name,
                expected_minor_units=obligation.monthly_amount_minor_units,
                due_day=obligation.due_day,
                match=(
                    Match(
                        transaction_id=found.id,
                        occurred_on=found.occurred_on,
                        amount_minor_units=found.amount_minor_units,
                        description=found.description,
                    )
                    if found is not None
                    else None
                ),
            )
        )

    window = months_back(month, TREND_MONTHS)
    figures = await figures_by_month(session, household_id, currency, window)
    trend = [
        MonthPoint(
            month=point,
            net_minor_units=found.net,
            income_minor_units=found.income,
            expenses_minor_units=found.expenses,
            invested_minor_units=found.invested,
            withdrawn_minor_units=found.withdrawn,
            debt_paid_minor_units=found.debt_paid,
        )
        if (found := figures.get(point)) is not None
        else MonthPoint(month=point)
        for point in window
    ]

    previous = figures.get(months_back(month, 2)[0])
    this_month = figures.get(first, MonthFigures())
    previous_net = previous.net if previous is not None else None
    # UTC, as the router decides which month is running: a day boundary in
    # another zone would put tomorrow's point on today's chart.
    daily = await _daily(
        session, household_id, currency, month, today or datetime.now(UTC).date()
    )

    return Dashboard(
        month=first,
        currency=currency,
        income=Flow(
            actual_minor_units=int(credits),
            expected_minor_units=(
                profile.monthly_income_minor_units if profile is not None else None
            ),
            previous_minor_units=previous.income if previous is not None else None,
        ),
        expenses=Flow(
            actual_minor_units=int(debits),
            expected_minor_units=(
                profile.monthly_expense_minor_units if profile is not None else None
            ),
            previous_minor_units=previous.expenses if previous is not None else None,
        ),
        investments=Stock(
            balance_minor_units=int(holdings or 0),
            moved_minor_units=moved.get(SAVINGS_SLUG, 0),
            previous_moved_minor_units=(
                previous.invested if previous is not None else None
            ),
            withdrawn_minor_units=this_month.withdrawn,
        ),
        debts=Stock(
            balance_minor_units=int(outstanding or 0),
            moved_minor_units=moved.get(DEBT_PAYMENT_SLUG, 0),
            previous_moved_minor_units=(
                previous.debt_paid if previous is not None else None
            ),
        ),
        commitments=commitments,
        trend=trend,
        daily=daily,
        previous_net_minor_units=previous_net,
        pending_review=int(pending or 0),
    )


async def _daily(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
) -> list[DayPoint]:
    """The month's running balance, one point per day, for the home chart.

    Counted by `countable` and `is_flow`, the rules the net figure uses, so the
    line ends exactly where "net this month" says — two figures on one card that
    disagreed would each make the other look wrong. One grouped query; the
    running sum is taken here.

    Every day from the first, not only days with rows: a balance is defined
    on a quiet day (it is the day before's), and a line with gaps would
    claim nobody knew. Up to today for the month that is running — a point
    for tomorrow would be a figure nobody observed — and to the month's end
    otherwise. Empty for a month with no rows at all, so the client draws no
    chart rather than a flat line that looks like a measured zero.
    """
    first, last = month_bounds(month)
    if first > today:
        return []
    flow = is_flow()
    result = await session.execute(
        select(
            Transaction.occurred_on,
            func.sum(
                case(
                    (
                        and_(
                            flow, Transaction.direction == TransactionDirection.credit
                        ),
                        Transaction.amount_minor_units,
                    ),
                    (
                        and_(flow, Transaction.direction == TransactionDirection.debit),
                        -Transaction.amount_minor_units,
                    ),
                    else_=0,
                )
            ),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*countable(household_id, currency), *_in_month(month))
        .group_by(Transaction.occurred_on)
    )
    by_day = {row[0]: int(row[1]) for row in result}
    if not by_day:
        return []

    end = min(last, today)
    points: list[DayPoint] = []
    running = 0
    for offset in range((end - first).days + 1):
        day = first + timedelta(days=offset)
        running += by_day.get(day, 0)
        points.append(DayPoint(day=day, net_minor_units=running))
    return points


async def _moved_by_category(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    slugs: tuple[str, ...],
) -> dict[str, int]:
    """Debits in the month under each of [slugs], by slug.

    This is the only honest way to the movement figures. A statement shows a
    transfer out; it does not show a portfolio's value or a loan's principal,
    so what moved is observable and what is held is not. The balances stay
    whatever the wizard was told.

    Matches the household's own category of that slug as well as the seeded
    one, since a household may carry an override with the same meaning.
    """
    result = await session.execute(
        select(
            Category.slug,
            func.coalesce(func.sum(Transaction.amount_minor_units), 0),
        )
        .join(Category, Category.id == Transaction.category_id)
        .where(
            *countable(household_id, currency),
            *_in_month(month),
            Transaction.direction == TransactionDirection.debit,
            Category.slug.in_(slugs),
            or_(
                Category.household_id.is_(None),
                Category.household_id == household_id,
            ),
        )
        .group_by(Category.slug)
    )
    return {slug: int(total) for slug, total in result}


@dataclass(frozen=True)
class CategorySpend:
    # None for the one entry that gathers every uncategorised row.
    category_id: uuid.UUID | None
    slug: str | None
    name: str | None
    spent_minor_units: int


async def spend_by_category(
    session: AsyncSession, household_id: uuid.UUID, currency: str, month: date
) -> list[CategorySpend]:
    """Where the month's money went: countable debits by category, largest first.

    One grouped query, counted by `countable` and `is_flow` exactly as
    `expenses.actual` is, so the entries — the uncategorised one included — sum
    to it to the cent. Transfers and savings are not in it: they are not money
    that went anywhere but another of the household's accounts.
    A household's own category is its own entry, beside the system one.
    """
    total = func.sum(Transaction.amount_minor_units)
    result = await session.execute(
        select(Transaction.category_id, Category.slug, Category.name, total)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            *countable(household_id, currency),
            *_in_month(month),
            Transaction.direction == TransactionDirection.debit,
            is_flow(),
        )
        .group_by(Transaction.category_id, Category.slug, Category.name)
        .order_by(total.desc(), Category.name.nulls_last())
    )
    return [
        CategorySpend(category_id, slug, name, int(spent))
        for category_id, slug, name, spent in result
    ]


@dataclass(frozen=True)
class AsOf:
    # The newest countable transaction date, or None with nothing yet.
    latest_transaction_on: date | None
    # When the newest import that saved at least one row was made.
    last_import_at: datetime | None


async def as_of(session: AsyncSession, household_id: uuid.UUID, currency: str) -> AsOf:
    """How current the figures are, household-wide (PRD F12).

    Until bank linking, every figure is only as fresh as the last statement
    imported; a dashboard that does not say so is quietly claiming to be live.
    An import that saved nothing (every row a duplicate, or abandoned) is not
    fresh data, so only imports with rows count. One query.
    """
    latest_transaction = (
        select(func.max(Transaction.occurred_on))
        .where(*countable(household_id, currency))
        .scalar_subquery()
    )
    last_import = (
        select(func.max(StatementImport.created_at))
        .where(
            StatementImport.household_id == household_id,
            select(Transaction.id)
            .where(Transaction.statement_import_id == StatementImport.id)
            .exists(),
        )
        .scalar_subquery()
    )
    latest_on, imported_at = (
        await session.execute(select(latest_transaction, last_import))
    ).one()
    return AsOf(latest_on, imported_at)
