"""A household-isolated sandbox for SQL the AI coach writes itself (#70).

The fallback, not the first resort: 6.1's fixed tools answer most questions in
one call. This answers the long tail — "which weekday do I spend most on
dining?" — by letting the model write one SELECT. That query runs against a
database holding every household's money, so the rule that cannot bend is:
**one household never sees another's data, whatever SQL the model writes**,
including SQL written to try.

The guarantee comes from what the query can reach, not from the model behaving.
Four walls, each of which would stop a leak on its own:

1. **The validator** (`validate`, PostgreSQL's own parser via `pglast`): one
   SELECT; only the sandbox's table names (and the query's own CTEs, in
   scope); only allowlisted functions; no casts to `reg*` types, no
   parameters, no locking, no INTO. A rejection goes back to the model as a
   `ToolError` it can correct.
2. **Only this household's rows exist.** Inside one transaction the server
   copies into temporary tables only the sandbox tables the query names,
   filtered by the household id — bound by the server, never by the model.
3. **A role that can read nothing else.** `SET LOCAL ROLE finai_chat_reader`
   (migration d7b3f9a1c426), granted SELECT on those temporary tables alone:
   a real table is "permission denied".
4. **Read-only and bounded.** `SET TRANSACTION READ ONLY`, a 2-second
   `statement_timeout`, and the query wrapped so at most 50 rows come back,
   with the true count and `truncated`.

The model's SQL runs as exactly **one** statement (a prepared statement
refuses two), and nothing runs after it but a rollback: within a statement
Postgres checks permissions before executing, so a `set_config('role', …)`
inside it cannot widen what that same statement may read.

Everything happens in a savepoint that is always rolled back: the temporary
tables, the grants and every `SET LOCAL` go with it. **Never** `SET` or
`SET SESSION`: on Supabase's transaction pooler a session setting sticks to a
shared connection, and once broke production writes for days.

Logged: one `chat_sql` line — household, tables copied, rows copied, rows
returned, duration, outcome, and a hash of the query. Never the query text or
its results, which carry merchant names and amounts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

import structlog
from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream
from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    column,
    func,
    or_,
    select,
    table,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.money import exponent_for, sql_major_units
from app.models.categorization import Category
from app.models.derived import MonthlyCategoryTotal
from app.models.enums import TransactionDirection
from app.models.money import Account, Transaction
from app.models.planning import Budget, BudgetLine, Debt, Goal
from app.models.setup import Obligation
from app.services.dashboard import NOT_A_FLOW, countable
from app.services.llm import ToolError, ToolSpec, tool_result

__all__ = [
    "MAX_ROWS",
    "READER_ROLE",
    "RUN_SQL",
    "SANDBOX_TABLES",
    "SCHEMA_PROMPT",
    "SqlResult",
    "Validated",
    "handler_for",
    "run",
    "validate",
]

log = structlog.get_logger()

READER_ROLE = "finai_chat_reader"
MAX_ROWS = 50
TIMEOUT = "2s"
# The copy is the server's own query over the household's own rows, but it
# is bounded too: a household with an unusually long history must not hold a
# connection open while the model waits.
COPY_TIMEOUT = "10s"
# What switches to the reader role. A module constant so the tests that prove
# the other walls hold without it can replace it — never an argument a caller
# could pass.
_ROLE_STATEMENT = f"SET LOCAL ROLE {READER_ROLE}"
UNFILED = "uncategorized"


# --- 1. The validator -------------------------------------------------------------

# What the model may call. Aggregates, window functions, and date, text and
# arithmetic functions — nothing that reads settings, files, other databases,
# or the catalog, sleeps, or has side effects.
ALLOWED_FUNCTIONS = frozenset(
    # aggregates
    """count sum avg min max bool_and bool_or every string_agg array_agg stddev
    stddev_pop stddev_samp variance var_pop var_samp percentile_cont
    percentile_disc mode"""
    # window
    """ row_number rank dense_rank percent_rank cume_dist ntile lag lead
    first_value last_value nth_value"""
    # dates
    """ date_trunc date_part extract age make_date to_char to_date now date
    justify_days make_interval"""
    # text
    """ lower upper initcap length char_length substring substr left right trim
    btrim ltrim rtrim position strpos replace concat concat_ws split_part
    starts_with regexp_like similarity word_similarity"""
    # numbers
    """ abs round trunc floor ceil ceiling sign mod div greatest least coalesce
    nullif sqrt power""".split()
)

# Types a value may be cast to. Not `regclass` and friends: a cast to them reads
# the catalog, and is the classic way to reach things by name.
ALLOWED_TYPES = frozenset(
    """int int2 int4 int8 integer smallint bigint numeric decimal float4 float8
    real text varchar bpchar char date time timestamp timestamptz interval bool
    boolean""".split()
)

# SQLValueFunction: only the date and time ones. CURRENT_USER and its kind
# answer questions about the database, not about money.
_SAFE_VALUE_FUNCTIONS = frozenset(
    {
        "SVFOP_CURRENT_DATE",
        "SVFOP_CURRENT_TIME",
        "SVFOP_CURRENT_TIME_N",
        "SVFOP_CURRENT_TIMESTAMP",
        "SVFOP_CURRENT_TIMESTAMP_N",
        "SVFOP_LOCALTIME",
        "SVFOP_LOCALTIME_N",
        "SVFOP_LOCALTIMESTAMP",
        "SVFOP_LOCALTIMESTAMP_N",
    }
)

# Every node a SELECT over the sandbox needs. Anything else — a function in
# FROM, a parameter, COLLATE, an INSERT inside a CTE — is refused, so a node
# type nobody thought about is closed by default.
_ALLOWED_NODES = frozenset(
    """SelectStmt ResTarget ColumnRef A_Star A_Const Integer Float String Boolean
    A_Expr BoolExpr NullTest BooleanTest CaseExpr CaseWhen CoalesceExpr
    MinMaxExpr FuncCall TypeCast TypeName RangeVar Alias JoinExpr RangeSubselect
    SubLink SortBy WindowDef WithClause CommonTableExpr SQLValueFunction
    GroupingSet RowExpr A_ArrayExpr A_Indirection A_Indices Interval""".split()
)


def _name(parts) -> list[str]:
    return [p.sval.lower() for p in parts if isinstance(p, ast.String)]


class _Validator:
    def __init__(self) -> None:
        self.tables: set[str] = set()

    def statement(self, stmt: ast.SelectStmt, visible: frozenset[str]) -> None:
        """A SELECT, its WITH first: each CTE sees only the sandbox and the CTEs
        before it, as Postgres resolves them; the body sees all of them."""
        scope = visible
        if stmt.withClause is not None:
            if stmt.withClause.recursive:
                raise ToolError("WITH RECURSIVE is not allowed")
            for cte in stmt.withClause.ctes or ():
                if not isinstance(cte.ctequery, ast.SelectStmt):
                    raise ToolError("a WITH query must be a SELECT")
                self.select(cte.ctequery, scope)
                scope = scope | {cte.ctename}
        self.select(stmt, scope, skip_with=True)

    def select(
        self, stmt: ast.SelectStmt, visible: frozenset[str], *, skip_with=False
    ) -> None:
        if stmt.intoClause is not None:
            raise ToolError("SELECT INTO is not allowed")
        if stmt.lockingClause:
            raise ToolError("FOR UPDATE and other locks are not allowed")
        if stmt.withClause is not None and not skip_with:
            # A WITH nested in a subquery: allowed, scoped the same way.
            self.statement(stmt, visible)
            return
        for slot in stmt.__slots__:
            if slot == "withClause":
                continue
            self.node(getattr(stmt, slot), visible)

    def node(self, value, visible: frozenset[str]) -> None:
        if isinstance(value, tuple | list):
            for item in value:
                self.node(item, visible)
            return
        if not isinstance(value, ast.Node):
            return
        kind = type(value).__name__
        if kind not in _ALLOWED_NODES:
            raise ToolError(f"{kind} is not allowed in the sandbox")
        if isinstance(value, ast.SelectStmt):
            self.select(value, visible)
            return
        if isinstance(value, ast.RangeVar):
            self.relation(value, visible)
        elif isinstance(value, ast.FuncCall):
            self.function(value)
        elif isinstance(value, ast.A_Expr):
            self.operator(value)
        elif isinstance(value, ast.TypeCast):
            self.type_name(value.typeName)
        elif isinstance(value, ast.SQLValueFunction):
            op = getattr(value.op, "name", str(value.op))
            if op not in _SAFE_VALUE_FUNCTIONS:
                raise ToolError(
                    "only date and time values like CURRENT_DATE are allowed"
                )
        for slot in value.__slots__:
            self.node(getattr(value, slot), visible)

    def relation(self, value: ast.RangeVar, visible: frozenset[str]) -> None:
        if value.catalogname or value.schemaname:
            raise ToolError("use the sandbox tables by name, without a schema")
        # Exactly as Postgres will resolve it: unquoted names arrive folded to
        # lower case, and a quoted "Transactions" is not `transactions`.
        name = value.relname
        if name not in visible:
            raise ToolError(
                f"unknown table {name!r}; the tables are {sorted(SANDBOX_TABLES)}"
            )
        if name in SANDBOX_TABLES:
            self.tables.add(name)

    def function(self, value: ast.FuncCall) -> None:
        parts = _name(value.funcname)
        # EXTRACT and friends arrive as pg_catalog.<name>; nothing else may
        # name a schema.
        if len(parts) == 2 and parts[0] == "pg_catalog":
            parts = parts[1:]
        if len(parts) != 1 or parts[0] not in ALLOWED_FUNCTIONS:
            raise ToolError(f"function {'.'.join(parts)!r} is not allowed")

    def operator(self, value: ast.A_Expr) -> None:
        """`OPERATOR(schema.op)` names a schema like a function can: only
        pg_catalog's, as for functions. An unqualified operator resolves with
        pg_catalog first, so it cannot be shadowed."""
        parts = _name(value.name or ())
        if len(parts) > 1 and parts[0] != "pg_catalog":
            raise ToolError("an operator may not name a schema")

    def type_name(self, value: ast.TypeName) -> None:
        parts = _name(value.names)
        if len(parts) == 2 and parts[0] == "pg_catalog":
            parts = parts[1:]
        if len(parts) != 1 or parts[0] not in ALLOWED_TYPES:
            raise ToolError(f"casting to {'.'.join(parts)!r} is not allowed")


@dataclass(frozen=True)
class Validated:
    # The sandbox tables the query names: the only ones copied.
    tables: frozenset[str]
    # The query as PostgreSQL's grammar read it, written back out: what runs
    # is exactly what was checked — no comment, quote or stray semicolon in
    # the original can change it once it is wrapped.
    sql: str


def validate(query: str) -> Validated:
    """[query] checked, or `ToolError` saying why it may not run. Parsed by
    PostgreSQL's own grammar, so comments, quoting and odd spacing are seen
    as Postgres would see them."""
    try:
        statements = parse_sql(query)
    except ParseError as exc:
        raise ToolError(f"the SQL does not parse: {exc}") from exc
    if len(statements) != 1:
        raise ToolError("send exactly one SELECT statement")
    stmt = statements[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        raise ToolError("only a SELECT is allowed")
    checker = _Validator()
    checker.statement(stmt, frozenset(SANDBOX_TABLES))
    if not checker.tables:
        raise ToolError("the query must read at least one sandbox table")
    return Validated(frozenset(checker.tables), RawStream()(stmt))


# --- 2. The sandbox tables --------------------------------------------------------


@dataclass(frozen=True)
class _Sandbox:
    columns: tuple[tuple[str, str], ...]
    # The household's rows, as (household_id, currency) -> SELECT of `columns`.
    source: Callable[[uuid.UUID, str], object]
    describe: str


def _direction(column_):
    return case((column_ == TransactionDirection.debit, "out"), else_="in")


def _flow(slug):
    return or_(slug.is_(None), slug.not_in(NOT_A_FLOW))


def _monthly_totals(household_id: uuid.UUID, currency: str):
    t = MonthlyCategoryTotal
    return select(
        t.month,
        func.coalesce(t.category_slug, UNFILED),
        case((t.direction == "debit", "out"), else_="in"),
        _flow(t.category_slug),
        sql_major_units(t.total_minor_units, currency),
        sql_major_units(t.one_off_minor_units, currency),
        t.transaction_count,
    ).where(t.household_id == household_id, t.currency == currency)


def _transactions(household_id: uuid.UUID, currency: str):
    return (
        select(
            Transaction.occurred_on,
            sql_major_units(Transaction.amount_minor_units, currency),
            _direction(Transaction.direction),
            _flow(Category.slug),
            Transaction.currency,
            Transaction.merchant,
            Transaction.description,
            func.coalesce(Category.slug, UNFILED),
            Category.name,
            cast(Account.kind, String),
            Transaction.needs_review,
            Transaction.is_one_off,
        )
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*countable(household_id, currency))
    )


def _budget_lines(household_id: uuid.UUID, currency: str):
    spent = (
        select(
            MonthlyCategoryTotal.month,
            MonthlyCategoryTotal.category_id,
            func.sum(MonthlyCategoryTotal.total_minor_units).label("spent"),
        )
        .where(
            MonthlyCategoryTotal.household_id == household_id,
            MonthlyCategoryTotal.currency == currency,
            MonthlyCategoryTotal.direction == "debit",
        )
        .group_by(MonthlyCategoryTotal.month, MonthlyCategoryTotal.category_id)
        .subquery()
    )
    return (
        select(
            Budget.period_start,
            Category.slug,
            sql_major_units(BudgetLine.allocated_minor_units, currency),
            sql_major_units(func.coalesce(spent.c.spent, 0), currency),
        )
        .join(Budget, Budget.id == BudgetLine.budget_id)
        .join(Category, Category.id == BudgetLine.category_id)
        .outerjoin(
            spent,
            and_(
                spent.c.month == Budget.period_start,
                spent.c.category_id == BudgetLine.category_id,
            ),
        )
        .where(Budget.household_id == household_id, BudgetLine.currency == currency)
    )


def _goals(household_id: uuid.UUID, currency: str):
    return select(
        Goal.name,
        cast(Goal.kind, String),
        sql_major_units(Goal.target_minor_units, currency),
        sql_major_units(Goal.saved_minor_units, currency),
        sql_major_units(Goal.monthly_contribution_minor_units, currency),
        Goal.target_date,
        Goal.priority,
        Goal.achieved_at,
    ).where(Goal.household_id == household_id, Goal.currency == currency)


def _debts(household_id: uuid.UUID, currency: str):
    return select(
        Debt.name,
        cast(Debt.kind, String),
        sql_major_units(Debt.balance_minor_units, currency),
        sql_major_units(Debt.minimum_payment_minor_units, currency),
        Debt.interest_rate_bps,
        Debt.due_day,
        Debt.balance_as_of,
    ).where(Debt.household_id == household_id, Debt.currency == currency)


def _obligations(household_id: uuid.UUID, currency: str):
    return (
        select(
            Obligation.name,
            sql_major_units(Obligation.monthly_amount_minor_units, currency),
            Obligation.due_day,
            Category.slug,
        )
        .outerjoin(Category, Category.id == Obligation.category_id)
        .where(Obligation.household_id == household_id, Obligation.currency == currency)
    )


def _merchants(household_id: uuid.UUID, currency: str):
    spending = and_(
        Transaction.direction == TransactionDirection.debit, _flow(Category.slug)
    )
    return (
        select(
            Transaction.merchant,
            func.count(Transaction.id),
            sql_major_units(
                func.coalesce(
                    func.sum(case((spending, Transaction.amount_minor_units), else_=0)),
                    0,
                ),
                currency,
            ),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*countable(household_id, currency), Transaction.merchant.is_not(None))
        .group_by(Transaction.merchant)
    )


_MONEY = "numeric(20,2)"

SANDBOX_TABLES: dict[str, _Sandbox] = {
    "monthly_totals": _Sandbox(
        (
            ("month", "date"),
            ("category", "text"),
            ("direction", "text"),
            ("is_flow", "boolean"),
            ("total", _MONEY),
            ("one_off", _MONEY),
            ("count", "integer"),
        ),
        _monthly_totals,
        "money summed by month (first day), category and direction. "
        "Prefer this for any period question.",
    ),
    "transactions": _Sandbox(
        (
            ("occurred_on", "date"),
            ("amount", _MONEY),
            ("direction", "text"),
            ("is_flow", "boolean"),
            ("currency", "text"),
            ("merchant", "text"),
            ("description", "text"),
            ("category", "text"),
            ("category_name", "text"),
            ("account_kind", "text"),
            ("needs_review", "boolean"),
            ("is_one_off", "boolean"),
        ),
        _transactions,
        "one row per transaction. Use for anything about days, merchants or "
        "single purchases.",
    ),
    "budget_lines": _Sandbox(
        (
            ("month", "date"),
            ("category", "text"),
            ("allocated", _MONEY),
            ("spent", _MONEY),
        ),
        _budget_lines,
        "each month's budget by category, and what that category spent.",
    ),
    "goals": _Sandbox(
        (
            ("name", "text"),
            ("kind", "text"),
            ("target", _MONEY),
            ("saved", _MONEY),
            ("monthly_contribution", _MONEY),
            ("target_date", "date"),
            ("priority", "integer"),
            ("achieved_at", "date"),
        ),
        _goals,
        "savings goals.",
    ),
    "debts": _Sandbox(
        (
            ("name", "text"),
            ("kind", "text"),
            ("balance", _MONEY),
            ("minimum_payment", _MONEY),
            ("interest_rate_bps", "integer"),
            ("due_day", "integer"),
            ("balance_as_of", "date"),
        ),
        _debts,
        "debts; interest_rate_bps is the yearly rate in basis points (1999 = 19.99%).",
    ),
    "obligations": _Sandbox(
        (
            ("name", "text"),
            ("monthly_amount", _MONEY),
            ("due_day", "integer"),
            ("category", "text"),
        ),
        _obligations,
        "fixed monthly commitments from setup (rent, subscriptions).",
    ),
    "merchants": _Sandbox(
        (("merchant", "text"), ("count", "integer"), ("total_out", _MONEY)),
        _merchants,
        "each merchant's name, how many transactions, and the spending there. "
        "Match a word to the household's own names with "
        "similarity(merchant, 'coffee').",
    ),
}


def _money_type(currency_exponent: int) -> str:
    return f"numeric(20,{currency_exponent})"


async def _copy(
    conn: AsyncConnection, name: str, household_id: uuid.UUID, currency: str
) -> int:
    """Create temp table [name] and fill it with this household's rows."""
    sandbox = SANDBOX_TABLES[name]
    money = _money_type(exponent_for(currency))
    columns = ", ".join(
        f'"{col}" {money if kind == _MONEY else kind}' for col, kind in sandbox.columns
    )
    # Names are ours (the keys above), never the model's.
    await conn.execute(text(f"CREATE TEMP TABLE {name} ({columns}) ON COMMIT DROP"))
    target = table(name, *(column(col) for col, _ in sandbox.columns), schema="pg_temp")
    result = await conn.execute(
        target.insert().from_select(
            [col for col, _ in sandbox.columns], sandbox.source(household_id, currency)
        )
    )
    await conn.execute(text(f"GRANT SELECT ON pg_temp.{name} TO {READER_ROLE}"))
    return result.rowcount or 0


# --- 3 and 4. Running it ----------------------------------------------------------


@dataclass(frozen=True)
class SqlResult:
    rows: list[dict]
    row_count: int
    truncated: bool
    tables: tuple[str, ...]

    def as_tool_result(self) -> str:
        return tool_result(
            {
                "rows": self.rows,
                "row_count": self.row_count,
                "truncated": self.truncated,
            }
        )


def _plain(value):
    """A result value as JSON the model reads: amounts as decimal strings,
    dates as ISO text — never a float."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    if isinstance(value, dt.date | dt.datetime | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    return str(value)


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()[:16]


async def run(
    conn: AsyncConnection,
    household_id: uuid.UUID,
    currency: str,
    query: str,
) -> SqlResult:
    """Run the model's [query] over this household's sandbox.

    [conn] is any connection in a transaction; everything here happens in a
    savepoint rolled back before returning, so nothing outlives the call.
    Every wall always applies: there is no argument that turns one off.
    """
    started = time.monotonic()
    digest = _query_hash(query)
    outcome, copied, returned, tables = "error", 0, 0, ()
    try:
        checked = validate(query)
        tables, sql = tuple(sorted(checked.tables)), checked.sql
        savepoint = await conn.begin_nested()
        try:
            await conn.execute(text(f"SET LOCAL statement_timeout = '{COPY_TIMEOUT}'"))
            for name in tables:
                copied += await _copy(conn, name, household_id, currency)
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text(_ROLE_STATEMENT))
            await conn.execute(text(f"SET LOCAL statement_timeout = '{TIMEOUT}'"))
            # On lines of its own, so even text that never went through the
            # parser (the tests that replace the validator) cannot comment the
            # wrapper away.
            wrapped = (
                f"SELECT *, count(*) OVER () AS _total FROM (\n{sql}\n) AS q "
                f"LIMIT {MAX_ROWS}"
            )
            # exec_driver_sql: the model's text verbatim, as one prepared
            # statement — which Postgres refuses if it holds two.
            result = await conn.exec_driver_sql(wrapped)
            raw = result.mappings().all()
        finally:
            await savepoint.rollback()

        total = int(raw[0]["_total"]) if raw else 0
        rows = [
            {key: _plain(value) for key, value in row.items() if key != "_total"}
            for row in raw
        ]
        returned = len(rows)
        outcome = "ok"
        return SqlResult(rows, total, total > len(rows), tables)
    except ToolError:
        outcome = "rejected"
        raise
    except DBAPIError as exc:
        reason = _db_reason(exc)
        outcome = "timeout" if "statement timeout" in reason else "error"
        raise ToolError(reason) from None
    except Exception as exc:  # noqa: BLE001 — every failure goes back as a tool error
        # Not a database error: the driver itself gave up — asyncpg cannot
        # resolve the types of some catalog columns (`anyarray`) and says so.
        # Still the query's fault, never a crash in the chat: no details, only
        # the kind, which carries no data.
        outcome = "error"
        raise ToolError(f"the query could not be run ({type(exc).__name__})") from None
    finally:
        log.info(
            "chat_sql",
            household_id=str(household_id),
            tables=list(tables),
            rows_copied=copied,
            row_count=returned,
            duration_ms=round((time.monotonic() - started) * 1000),
            outcome=outcome,
            query_hash=digest,
        )


def _db_reason(exc: DBAPIError) -> str:
    """What went wrong, for the model to correct: Postgres's own first line
    (it names a column or a type, never another household's data — none is
    reachable)."""
    message = str(getattr(exc, "orig", exc)).splitlines()[0]
    message = message.split(">: ", 1)[-1]
    if "canceling statement due to statement timeout" in message:
        return "the query took longer than the 2 second statement timeout; narrow it"
    return f"the query failed: {message}"


# --- The tool, for 6.4 ------------------------------------------------------------

SCHEMA_PROMPT = (
    "run_sql runs one read-only PostgreSQL SELECT over this household's data only. "
    "Use it ONLY when no other tool can answer. At most 50 rows come back, with "
    "row_count (the true total) and truncated; when truncated, say the answer is "
    "a summary. Amounts are in the household's currency, as decimals. Spending "
    "and income are rows with is_flow = true (transfers and savings are not "
    "spending). direction is 'out' (money out) or 'in'. Tables:\n"
    + "\n".join(
        f"- {name}({', '.join(col for col, _ in sandbox.columns)}): {sandbox.describe}"
        for name, sandbox in SANDBOX_TABLES.items()
    )
)

RUN_SQL = ToolSpec(
    name="run_sql",
    description=(
        "Fallback: one read-only SELECT over this household's sandbox tables, "
        "for questions no other tool answers. See the table list in the "
        "instructions."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "One SELECT."}},
        "required": ["query"],
        "additionalProperties": False,
    },
)


def handler_for(
    household_id: uuid.UUID,
    currency: str,
    connect: Callable[[], object] | None = None,
) -> Callable[[dict], Awaitable[object]]:
    """`run_sql` bound to one household, as `llm.run_tools` takes it.

    Runs on a connection of its own (never the request's session), in a
    transaction that is rolled back. The household comes from the server;
    no argument names one.
    """

    async def handler(arguments: dict) -> object:
        from app.db import get_engine

        opener = connect or get_engine().connect
        async with opener() as conn:
            transaction = await conn.begin()
            try:
                result = await run(conn, household_id, currency, arguments["query"])
            finally:
                await transaction.rollback()
        return {
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
        }

    return handler
