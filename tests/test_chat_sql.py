"""The AI coach's SQL sandbox (#70), attacked.

Two households: A (ten years, from #83's fixture) and B, whose merchant
"SECRET SHOP" and amount $9,999.99 must never reach a query run as A. Every
attack is run through the whole sandbox, and the walls are also tested one at
a time: with the validator bypassed, the temporary tables and the role still
hold; with the role skipped, the validator still refuses.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from structlog.testing import capture_logs

from app.models.enums import (
    DebtKind,
    GoalHorizon,
    ReviewReason,
    TransactionDirection,
)
from app.models.money import Transaction
from app.models.planning import Budget, BudgetLine, Debt, Goal
from app.models.setup import Obligation
from app.services import chat_sql, chat_tools, rollups
from app.services.chat_sql import (
    MAX_ROWS,
    RUN_SQL,
    SANDBOX_TABLES,
    SCHEMA_PROMPT,
    handler_for,
    run,
    validate,
)
from app.services.llm import ToolError, _schema_problem
from tests.conftest import requires_db
from tests.fixtures.long_history import COMMON_MERCHANT, ten_years
from tests.test_dashboard import (
    a_household,
    a_system_category_id,
    authenticate_as,
    tx,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

CAD = "CAD"
SECRET = "SECRET SHOP"
A_PHONE = "+14165578601"


async def two_households(api_client, db_session, *, long: bool = False):
    """A, with ten years when [long], else one month; and B with a secret."""
    a, a_account = await a_household(api_client, A_PHONE)
    if long:
        await ten_years(db_session, a, a_account)
    else:
        db_session.add_all(
            [
                tx(a, a_account, minor=1_250, month=8, day=3, description="COSTCO"),
                tx(a, a_account, minor=2_000, month=8, day=9, description="METRO"),
            ]
        )
    b, b_account = await a_household(api_client, "+14165578602")
    db_session.add(tx(b, b_account, minor=999_999, month=8, day=4, description=SECRET))
    await db_session.flush()
    await rollups.rebuild(db_session, a)
    await rollups.rebuild(db_session, b)
    return a, b


async def run_as(
    db_session,
    household,
    query: str,
    *,
    skip_validator: bool = False,
    skip_role: bool = False,
):
    """Run [query] as [household]; with a wall replaced, to prove the others
    hold without it. `run` itself has no way to turn a wall off — the tests
    patch the module, which no caller in `app/` can do by accident."""
    conn = await db_session.connection()
    with pytest.MonkeyPatch.context() as patch:
        if skip_validator:
            patch.setattr(
                chat_sql,
                "validate",
                lambda q: chat_sql.Validated(frozenset(SANDBOX_TABLES), q),
            )
        if skip_role:
            patch.setattr(chat_sql, "_ROLE_STATEMENT", "SELECT 1")
        return await run(conn, household, CAD, query)


def leaked(result) -> bool:
    text_ = json.dumps(result.rows)
    return SECRET in text_ or "9999.99" in text_


# --- The validator, on its own ----------------------------------------------------

REJECTED = {
    "a real table": "SELECT * FROM transaction",
    "a real table, quoted": 'SELECT * FROM "transaction"',
    "a sandbox name, quoted in another case": 'SELECT * FROM "Transactions"',
    "set_config": "SELECT set_config('role', 'postgres', true) FROM transactions",
    "current_setting": "SELECT current_setting('role') FROM transactions",
    "a UNION with a real table": (
        "SELECT merchant FROM transactions UNION SELECT merchant FROM transaction"
    ),
    "a CTE naming a real table": (
        "WITH t AS (SELECT * FROM transaction) SELECT * FROM t"
    ),
    "a CTE used before it exists": (
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x) SELECT * FROM a"
    ),
    "a schema-qualified sandbox name": "SELECT * FROM public.transactions",
    "the temp schema": "SELECT * FROM pg_temp.transactions",
    "the catalog by schema": "SELECT * FROM pg_catalog.pg_class",
    "the catalog by name": "SELECT relname FROM pg_class",
    "pg_stats": "SELECT * FROM pg_stats",
    "a reg cast": "SELECT 'transaction'::regclass FROM transactions",
    "current_user": "SELECT current_user FROM transactions",
    "pg_sleep": "SELECT pg_sleep(5) FROM transactions",
    "a file read": "SELECT pg_read_file('/etc/passwd') FROM transactions",
    "dblink": "SELECT * FROM dblink('host=x', 'select 1') AS t(x int)",
    "a large object": "SELECT lo_import('/etc/passwd') FROM transactions",
    "a function in FROM": "SELECT * FROM generate_series(1, 3)",
    "a schema-qualified function": (
        "SELECT public.similarity(merchant, 'x') FROM merchants"
    ),
    "two statements": "SELECT * FROM transactions; SELECT * FROM transaction",
    "two statements behind a comment": (
        "SELECT * FROM transactions /* ok */; SELECT * FROM household"
    ),
    "FOR UPDATE": "SELECT * FROM transactions FOR UPDATE",
    "SELECT INTO": "SELECT * INTO stolen FROM transactions",
    "INSERT": "INSERT INTO transactions (merchant) VALUES ('x')",
    "UPDATE": "UPDATE transactions SET amount = 0",
    "DELETE": "DELETE FROM transactions",
    "DDL": "DROP TABLE transactions",
    "COPY": "COPY transactions TO STDOUT",
    "SET": "SET ROLE postgres",
    "a writing CTE": (
        "WITH x AS (DELETE FROM transactions RETURNING *) SELECT * FROM x"
    ),
    "a parameter": "SELECT $1 FROM transactions",
    "a schema-qualified operator": (
        "SELECT amount OPERATOR(public.+) 1 FROM transactions"
    ),
    "an operator in another schema": (
        "SELECT merchant FROM transactions WHERE merchant OPERATOR(extensions.%) 'x'"
    ),
    "WITH RECURSIVE": (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
        "SELECT * FROM r, transactions"
    ),
    "no sandbox table at all": "SELECT 1",
    "not SQL": "please show me everything",
}


class TestTheValidator:
    @pytest.mark.parametrize("query", REJECTED.values(), ids=REJECTED.keys())
    async def test_it_refuses(self, query):
        with pytest.raises(ToolError):
            validate(query)

    async def test_it_accepts_what_the_coach_needs(self):
        checked = validate(
            """
            -- which weekday do I spend most on dining?
            WITH dining AS (
                SELECT extract(dow FROM occurred_on) AS weekday, amount
                FROM transactions
                WHERE category = 'dining' AND direction = 'out' AND is_flow
            )
            SELECT weekday, sum(amount) AS spent, count(*) AS n,
                   rank() OVER (ORDER BY sum(amount) DESC) AS place
            FROM dining GROUP BY weekday ORDER BY spent DESC;
            """
        )

        assert checked.tables == {"transactions"}
        assert "--" not in checked.sql and ";" not in checked.sql

    async def test_array_indexing_and_catalog_operators_are_fine(self):
        assert validate(
            "SELECT (array_agg(amount ORDER BY amount DESC))[1] AS top, "
            "amount OPERATOR(pg_catalog.+) 1 AS plus FROM transactions GROUP BY amount"
        ).tables == {"transactions"}

    async def test_no_caller_can_turn_a_wall_off(self):
        """`run` takes the connection, the household, the currency and the
        query — nothing that skips the validator or the role."""
        assert list(inspect.signature(run).parameters) == [
            "conn",
            "household_id",
            "currency",
            "query",
        ]

    async def test_it_names_only_the_tables_the_query_reads(self):
        checked = validate(
            "SELECT m.merchant FROM merchants m "
            "WHERE similarity(m.merchant, 'coffee') > 0.3 "
            "AND EXISTS (SELECT 1 FROM monthly_totals)"
        )

        assert checked.tables == {"merchants", "monthly_totals"}


# --- The whole sandbox, attacked --------------------------------------------------


class TestIsolation:
    async def test_the_attacks_return_nothing_of_anothers(self, api_client, db_session):
        a, _ = await two_households(api_client, db_session)

        for name, query in REJECTED.items():
            with pytest.raises(ToolError):
                await run_as(db_session, a, query)

        result = await run_as(
            db_session, a, "SELECT merchant, amount FROM transactions"
        )
        assert {r["merchant"] for r in result.rows} == {"COSTCO", "METRO"}
        assert not leaked(result)

    async def test_replacing_a_wall_really_removes_it(self, api_client, db_session):
        """Otherwise the one-wall-at-a-time tests below would prove nothing."""
        a, _ = await two_households(api_client, db_session)
        probe = "SELECT current_user AS u FROM transactions LIMIT 1"

        with pytest.raises(ToolError):
            await run_as(db_session, a, probe)  # current_user: refused
        as_reader = await run_as(db_session, a, probe, skip_validator=True)
        as_app = await run_as(db_session, a, probe, skip_validator=True, skip_role=True)

        assert as_reader.rows == [{"u": chat_sql.READER_ROLE}]
        assert as_app.rows[0]["u"] != chat_sql.READER_ROLE

    async def test_without_the_validator_the_tables_and_role_still_hold(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)

        for attack in (
            'SELECT merchant FROM public."transaction"',
            "SELECT merchant FROM transactions UNION ALL "
            'SELECT merchant FROM public."transaction"',
            # A role change inside the statement cannot widen that statement:
            # its permissions were checked before it started.
            "SELECT set_config('role', 'postgres', true) AS r, "
            '(SELECT count(*) FROM public."transaction") AS n',
            "SELECT * FROM pg_stats WHERE tablename = 'transaction'",
        ):
            try:
                result = await run_as(db_session, a, attack, skip_validator=True)
            except ToolError as refused:
                assert "permission denied" in str(refused) or "pg_stats" in attack
                continue
            assert not leaked(result), attack
            assert result.row_count == 0, attack

        mine = await run_as(
            db_session, a, "SELECT merchant FROM transactions", skip_validator=True
        )
        assert not leaked(mine)
        assert mine.row_count == 2

    async def test_without_the_role_the_validator_still_refuses(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)

        for query in (
            "SELECT * FROM transaction",
            "SELECT set_config('role', 'postgres', true) FROM transactions",
            "SELECT merchant FROM transactions "
            'UNION SELECT merchant FROM "transaction"',
        ):
            with pytest.raises(ToolError):
                await run_as(db_session, a, query, skip_role=True)

    async def test_the_temp_tables_alone_hold_only_this_household(
        self, api_client, db_session
    ):
        """Validator bypassed and role skipped: the sandbox names still mean
        only A's rows."""
        a, _ = await two_households(api_client, db_session)

        result = await run_as(
            db_session,
            a,
            "SELECT merchant FROM transactions "
            "UNION ALL SELECT merchant FROM merchants",
            skip_validator=True,
            skip_role=True,
        )

        assert not leaked(result)
        assert {r["merchant"] for r in result.rows} == {"COSTCO", "METRO"}


class TestWritesAreImpossible:
    async def test_a_write_forced_past_the_validator_fails_read_only(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)

        # lo_create writes; READ ONLY refuses it even with no role in the way.
        with pytest.raises(ToolError, match="read-only"):
            await run_as(
                db_session,
                a,
                "SELECT lo_create(0) AS oid",
                skip_validator=True,
                skip_role=True,
            )

    async def test_dml_cannot_even_be_wrapped(self, api_client, db_session):
        a, _ = await two_households(api_client, db_session)

        with pytest.raises(ToolError):
            await run_as(
                db_session,
                a,
                "DELETE FROM transactions RETURNING *",
                skip_validator=True,
            )
        assert (
            await run_as(db_session, a, "SELECT count(*) AS n FROM transactions")
        ).rows == [{"n": 2}]


class TestNothingOutlivesTheCall:
    async def test_tables_role_and_settings_are_gone_afterwards(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)
        conn = await db_session.connection()
        before = (
            await conn.execute(
                text(
                    "SELECT current_user, current_setting('transaction_read_only'), "
                    "current_setting('statement_timeout'), "
                    "current_setting('application_name')"
                )
            )
        ).one()

        await run(conn, a, CAD, "SELECT * FROM transactions, monthly_totals")
        with pytest.raises(ToolError):
            await run(conn, a, CAD, "SELECT x FROM transactions")
        # Even a role change the statement itself made (validator bypassed)
        # ends with the savepoint.
        # The session-level form too — the kind that once stuck to a pooled
        # connection: rolled back with the savepoint like any other.
        for local in ("true", "false"):
            changed = await run_as(
                db_session,
                a,
                f"SELECT set_config('role', 'postgres', {local}) AS r, "
                f"set_config('application_name', 'probe', {local}) AS t",
                skip_validator=True,
            )
            assert changed.rows == [{"r": "postgres", "t": "probe"}]

        after = (
            await conn.execute(
                text(
                    "SELECT current_user, current_setting('transaction_read_only'), "
                    "current_setting('statement_timeout'), "
                    "current_setting('application_name'), "
                    "to_regclass('pg_temp.transactions'), "
                    "to_regclass('pg_temp.monthly_totals')"
                )
            )
        ).one()
        assert tuple(after[:4]) == tuple(before)
        assert after[4] is None and after[5] is None
        # And the connection still writes.
        await conn.execute(text("CREATE TEMP TABLE probe_after (x int)"))


class TestOnlyWhatIsNamedIsCopied:
    async def test_a_monthly_totals_query_copies_no_transactions(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)

        with capture_logs() as logs:
            await run_as(db_session, a, "SELECT sum(total) AS t FROM monthly_totals")

        [line] = [e for e in logs if e["event"] == "chat_sql"]
        assert line["tables"] == ["monthly_totals"]
        assert line["outcome"] == "ok"


class TestTenYears:
    async def test_a_yearly_total_and_a_merchant_total_are_fast_and_right(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session, long=True)

        started = time.perf_counter()
        yearly = await run_as(
            db_session,
            a,
            "SELECT extract(year FROM month)::int AS y, sum(total) AS t "
            "FROM monthly_totals WHERE direction = 'out' AND is_flow "
            "GROUP BY 1 ORDER BY 1",
        )
        yearly_seconds = time.perf_counter() - started
        started = time.perf_counter()
        merchant = await run_as(
            db_session,
            a,
            "SELECT sum(amount) AS t, count(*) AS n FROM transactions "
            f"WHERE merchant = '{COMMON_MERCHANT}' AND direction = 'out'",
        )
        merchant_seconds = time.perf_counter() - started

        assert yearly_seconds < 2 and merchant_seconds < 2
        tool = await chat_tools.spending_summary(
            db_session,
            a,
            CAD,
            date(2026, 1, 15),
            from_month="2016-01",
            to_month="2025-12",
            group_by="year",
        )
        assert [(str(r["y"]), r["t"]) for r in yearly.rows] == [
            (row["p"], row["t"]) for row in tool["rows"]
        ]
        raw = (
            await db_session.execute(
                select(
                    func.sum(Transaction.amount_minor_units),
                    func.count(Transaction.id),
                ).where(
                    Transaction.household_id == a,
                    Transaction.merchant == COMMON_MERCHANT,
                    Transaction.currency == CAD,
                    Transaction.direction == TransactionDirection.debit,
                    ~(
                        Transaction.needs_review.is_(True)
                        & (
                            Transaction.review_reason
                            == ReviewReason.suspected_duplicate
                        )
                    ),
                )
            )
        ).one()
        assert Decimal(merchant.rows[0]["t"]) * 100 == raw[0]
        assert merchant.rows[0]["n"] == raw[1]

    async def test_a_ten_year_listing_is_50_rows_and_the_true_count(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session, long=True)

        result = await run_as(
            db_session,
            a,
            "SELECT occurred_on, merchant, amount FROM transactions "
            "ORDER BY occurred_on DESC",
        )

        countable = await db_session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.household_id == a,
                Transaction.currency == CAD,
                ~(
                    Transaction.needs_review.is_(True)
                    & (Transaction.review_reason == ReviewReason.suspected_duplicate)
                ),
            )
        )
        assert len(result.rows) == MAX_ROWS
        assert result.row_count == countable > 35_000
        assert result.truncated is True

    async def test_a_query_past_two_seconds_is_cancelled(self, api_client, db_session):
        a, _ = await two_households(api_client, db_session, long=True)

        with capture_logs() as logs, pytest.raises(ToolError, match="2 second"):
            await run_as(
                db_session,
                a,
                "SELECT count(*) AS n FROM transactions x, transactions y "
                "WHERE x.amount + y.amount > 0",
            )
        assert [e["outcome"] for e in logs if e["event"] == "chat_sql"] == ["timeout"]

    async def test_totals_agree_with_the_dashboard(self, api_client, db_session):
        a, _ = await two_households(api_client, db_session, long=True)

        result = await run_as(
            db_session,
            a,
            "SELECT (SELECT sum(total) FROM monthly_totals WHERE month = '2020-03-01' "
            "  AND direction = 'out' AND is_flow) AS from_totals, "
            "(SELECT sum(amount) FROM transactions WHERE occurred_on >= '2020-03-01' "
            "  AND occurred_on < '2020-04-01' AND direction = 'out' AND is_flow) "
            "  AS from_rows",
        )

        authenticate_as(phone=A_PHONE)  # B was signed in last
        body = (await api_client.get("/dashboard?month=2020-03")).json()
        row = result.rows[0]
        assert row["from_totals"] == row["from_rows"] == body["expenses"]["actual"]


class TestEveryTableIsThisHouseholdsAlone:
    async def test_b_has_a_row_in_every_table_and_a_sees_none_of_them(
        self, api_client, db_session
    ):
        """A dropped household filter in any one table's copy would show
        here: B is given a row in all seven, each carrying the secret."""
        a, b = await two_households(api_client, db_session)
        groceries = await a_system_category_id(db_session, "groceries")
        budget = Budget(
            household_id=b,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            currency=CAD,
            expected_income_minor_units=999_999,
        )
        db_session.add(budget)
        await db_session.flush()
        db_session.add_all(
            [
                BudgetLine(
                    budget_id=budget.id,
                    category_id=groceries,
                    allocated_minor_units=999_999,
                    currency=CAD,
                ),
                Goal(
                    household_id=b,
                    name=f"{SECRET} GOAL",
                    horizon=GoalHorizon.short_term,
                    target_minor_units=999_999,
                    saved_minor_units=0,
                    currency=CAD,
                ),
                Debt(
                    household_id=b,
                    name=f"{SECRET} CARD",
                    kind=DebtKind.credit_card,
                    balance_minor_units=999_999,
                    currency=CAD,
                ),
                Obligation(
                    household_id=b,
                    name=f"{SECRET} RENT",
                    monthly_amount_minor_units=999_999,
                    currency=CAD,
                    position=0,
                ),
            ]
        )
        await db_session.flush()
        await rollups.rebuild(db_session, b)

        for name in SANDBOX_TABLES:
            result = await run_as(db_session, a, f"SELECT * FROM {name}")
            assert not leaked(result), name
        # And B's own sandbox does hold them, so the check above can fail.
        for name in ("goals", "debts", "obligations", "budget_lines"):
            mine = await run_as(db_session, b, f"SELECT * FROM {name}")
            assert leaked(mine), name


class TestWhatComesBack:
    async def test_amounts_are_decimal_strings_never_floats(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)

        result = await run_as(
            db_session,
            a,
            "SELECT sum(amount) AS total, avg(amount) AS mean, "
            "max(occurred_on) AS last, "
            "count(*) AS n, bool_or(is_one_off) AS any_one_off FROM transactions",
        )

        row = result.rows[0]
        assert row["total"] == "32.50"
        assert isinstance(row["mean"], str) and Decimal(row["mean"]) == Decimal("16.25")
        assert row["last"] == "2026-08-09"
        assert row["n"] == 2 and row["any_one_off"] is False
        assert not any(isinstance(v, float) for v in row.values())

    async def test_arrays_hold_decimal_strings_and_iso_dates(
        self, api_client, db_session
    ):
        a, _ = await two_households(api_client, db_session)

        result = await run_as(
            db_session,
            a,
            "SELECT array_agg(amount ORDER BY amount) AS amounts, "
            "array_agg(occurred_on ORDER BY occurred_on) AS days FROM transactions",
        )

        assert result.rows == [
            {"amounts": ["12.50", "20.00"], "days": ["2026-08-03", "2026-08-09"]}
        ]

    async def test_functions_resolve_only_in_pg_catalog_and_pg_trgm(
        self, api_client, db_session
    ):
        """A function added to `public` later can never be picked over a
        built-in: the query's search path is pg_catalog and pg_trgm's schema."""
        a, _ = await two_households(api_client, db_session)
        trgm = await db_session.scalar(
            text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'pg_trgm'"
            )
        )

        result = await run_as(
            db_session,
            a,
            "SELECT current_setting('search_path') AS p, "
            "similarity('coffee', 'coffe') > 0 AS s FROM merchants LIMIT 1",
            skip_validator=True,
        )

        assert result.rows[0]["p"] == f"pg_catalog, {trgm}"
        assert result.rows[0]["s"] is True

    async def test_a_slow_copy_says_so(self, api_client, db_session, monkeypatch):
        a, _ = await two_households(api_client, db_session, long=True)
        monkeypatch.setattr(chat_sql, "COPY_TIMEOUT", "1ms")

        with pytest.raises(ToolError, match="preparing the data took too long"):
            await run_as(db_session, a, "SELECT count(*) AS n FROM transactions")

    async def test_no_query_text_or_result_is_logged(self, api_client, db_session):
        a, _ = await two_households(api_client, db_session)

        with capture_logs() as logs:
            await run_as(
                db_session,
                a,
                "SELECT merchant, amount FROM transactions WHERE merchant = 'COSTCO'",
            )
            with pytest.raises(ToolError):
                await run_as(
                    db_session, a, "SELECT * FROM transaction WHERE x = 'COSTCO'"
                )

        written = json.dumps(logs, default=str)
        assert "COSTCO" not in written and "12.50" not in written
        assert "SELECT" not in written.upper().replace("CHAT_SQL", "")
        assert all(len(e["query_hash"]) == 16 for e in logs if e["event"] == "chat_sql")


class TestTheTool:
    async def test_the_spec_and_the_prompt_describe_every_table(self):
        assert _schema_problem({"query": "SELECT 1"}, RUN_SQL.parameters) is None
        assert _schema_problem(
            {"query": "SELECT 1", "household_id": str(uuid.uuid4())}, RUN_SQL.parameters
        ), "no argument can name a household"
        for name in SANDBOX_TABLES:
            assert name in SCHEMA_PROMPT

    async def test_the_handler_runs_on_its_own_connection_and_rolls_back(self):
        """A real connection of its own, as 6.4 will use it: a household with
        no rows sees none, and nothing is left behind."""
        handler = handler_for(uuid.uuid4(), CAD)

        result = await handler({"query": "SELECT count(*) AS n FROM transactions"})

        assert result == {"rows": [{"n": 0}], "row_count": 1, "truncated": False}

    async def test_a_cancelled_call_stays_a_cancellation(self):
        """6.4's deadline cancels the task mid-query: what comes out is the
        cancellation, never a tool error the loop would send to the model."""
        from app.db import get_engine

        async with get_engine().connect() as conn:
            transaction = await conn.begin()
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(
                    chat_sql,
                    "validate",
                    lambda q: chat_sql.Validated(frozenset({"merchants"}), q),
                )
                task = asyncio.create_task(
                    run(
                        conn,
                        uuid.uuid4(),
                        CAD,
                        "SELECT count(*) AS n FROM generate_series(1, 2000000000)",
                    )
                )
                await asyncio.sleep(0.3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await conn.invalidate()
            del transaction

    async def test_the_reader_role_can_read_nothing_real(self, db_session):
        conn = await db_session.connection()
        privileges = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE grantee = :role"
                ),
                {"role": chat_sql.READER_ROLE},
            )
        ).scalar_one()

        assert privileges == 0
