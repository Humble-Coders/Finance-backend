"""`GET /dashboard`.

The test that matters most here is the one asserting a number does NOT move:
an obligation typed into the wizard and then imported from the bank is one
payment, and the whole design exists so it is counted once.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.auth import AuthenticatedUser, current_user
from app.models.categorization import Category
from app.models.enums import ReviewReason, TransactionDirection, TransactionSource
from app.models.money import Transaction
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

DASHBOARD = "/dashboard"
MONTH = {"month": "2026-08"}


def authenticate_as(*, phone: str) -> None:
    from app.main import app

    app.dependency_overrides[current_user] = lambda: AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        email=None,
        phone=phone,
        claims={"app_metadata": {"provider": "phone"}},
    )


async def a_household(api_client, phone: str) -> tuple[uuid.UUID, uuid.UUID]:
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    await api_client.post("/me/consent", json={"version": version})
    account = await api_client.post(
        "/accounts", json={"name": "Chequing", "kind": "chequing"}
    )
    return uuid.UUID(me["household"]["id"]), uuid.UUID(account.json()["id"])


def tx(
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    minor: int,
    day: int = 5,
    month: int = 8,
    credit: bool = False,
    description: str = "SOMETHING",
    needs_review: bool = False,
    reason: ReviewReason | None = None,
    category_id: uuid.UUID | None = None,
    currency: str = "CAD",
) -> Transaction:
    return Transaction(
        household_id=household_id,
        account_id=account_id,
        occurred_on=date(2026, month, day),
        amount_minor_units=minor,
        currency=currency,
        direction=(
            TransactionDirection.credit if credit else TransactionDirection.debit
        ),
        description=description,
        normalized_description=description.lower(),
        merchant=description,
        source=TransactionSource.upload,
        needs_review=needs_review,
        review_reason=reason,
        category_id=category_id,
    )


async def setup_wizard(api_client, **body) -> None:
    payload = {"income": "5000.00", "monthly_expense": "3000.00"} | body
    response = await api_client.put("/financial-setup", json=payload)
    assert response.status_code == 200, response.text


class TestTheFiguresAreMonthly:
    async def test_only_the_month_asked_for_is_counted(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574001")
        db_session.add_all(
            [
                tx(household, account, minor=10_000, month=7, day=31),
                tx(household, account, minor=20_000, month=8, day=1),
                tx(household, account, minor=40_000, month=8, day=31),
                tx(household, account, minor=80_000, month=9, day=1),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["expenses"]["actual"] == "600.00", "the 1st and 31st are inside"

    async def test_an_empty_month_is_zeroes_and_not_a_404(self, api_client):
        await a_household(api_client, "+14165574002")

        response = await api_client.get(DASHBOARD, params=MONTH)

        assert response.status_code == 200
        body = response.json()
        assert body["net"] == "0.00"
        assert body["income"]["actual"] == "0.00"
        assert body["commitments"] == []

    async def test_the_month_defaults_to_now(self, api_client):
        await a_household(api_client, "+14165574003")
        body = (await api_client.get(DASHBOARD)).json()
        assert body["month"].endswith("-01"), "a month is identified by its first day"

    async def test_a_malformed_month_is_refused(self, api_client):
        await a_household(api_client, "+14165574004")
        for bad in ("2026-13", "august", "2026", "2026-00"):
            response = await api_client.get(DASHBOARD, params={"month": bad})
            assert response.status_code == 422, bad
            assert response.json()["detail"]["code"] == "invalid_month"


class TestObligationsAreNeverAddedToSpending:
    """The reason this endpoint is shaped the way it is."""

    async def test_setting_up_a_commitment_does_not_change_the_spend(
        self, api_client, db_session
    ):
        """Typing rent into the wizard must not move a figure that reports
        what the bank did. It is an expectation, not a payment."""
        household, account = await a_household(api_client, "+14165574010")
        db_session.add(tx(household, account, minor=50_000, description="GROCERIES"))
        await db_session.commit()

        before = (await api_client.get(DASHBOARD, params=MONTH)).json()
        await setup_wizard(
            api_client,
            obligations=[{"name": "Rent", "monthly_amount": "1800.00"}],
        )
        after = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert before["expenses"]["actual"] == "500.00"
        assert (
            after["expenses"]["actual"] == "500.00"
        ), "an obligation entered the sum; this is the double count"
        assert after["net"] == before["net"]

    async def test_a_commitment_and_its_payment_are_counted_once(
        self, api_client, db_session
    ):
        """The scenario in full: rent typed at setup, then the same rent
        arriving on a statement."""
        household, account = await a_household(api_client, "+14165574011")
        await setup_wizard(
            api_client,
            obligations=[{"name": "Rent", "monthly_amount": "1800.00"}],
        )
        db_session.add(
            tx(
                household,
                account,
                minor=180_000,
                day=2,
                description="HARBOURVIEW PROPERTIES RENT",
            )
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["expenses"]["actual"] == "1800.00", "once, not 3600.00"
        assert body["expenses"]["expected"] == "3000.00"


class TestExpectedVersusActual:
    async def test_the_wizard_figures_arrive_as_expectations(self, api_client):
        await a_household(api_client, "+14165574020")
        await setup_wizard(api_client, income="5000.00", monthly_expense="3000.00")

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["income"]["expected"] == "5000.00"
        assert body["expenses"]["expected"] == "3000.00"
        assert body["income"]["actual"] == "0.00", "nothing has happened yet"

    async def test_expectations_are_null_before_the_wizard(self, api_client):
        """Null and not zero: a client shows "of X expected" or nothing, and
        "of 0.00 expected" is a sentence nobody means."""
        await a_household(api_client, "+14165574021")

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["income"]["expected"] is None
        assert body["expenses"]["expected"] is None


class TestTheHeroFigure:
    async def test_net_is_income_minus_expenses(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574030")
        db_session.add_all(
            [
                tx(household, account, minor=400_000, credit=True),
                tx(household, account, minor=150_000),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["income"]["actual"] == "4000.00"
        assert body["expenses"]["actual"] == "1500.00"
        assert body["net"] == "2500.00"

    async def test_a_month_that_spent_more_than_it_earned_goes_negative(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574031")
        db_session.add_all(
            [
                tx(household, account, minor=100_000, credit=True),
                tx(household, account, minor=250_000),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["net"] == "-1500.00"


class TestTheCommitmentChecklist:
    """Matching, not merging. A match adds nothing to any total — it only says
    a commitment was seen going out."""

    async def test_a_commitment_is_matched_to_its_payment(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574040")
        await setup_wizard(
            api_client, obligations=[{"name": "Rent", "monthly_amount": "1800.00"}]
        )
        db_session.add(
            tx(
                household,
                account,
                minor=180_000,
                day=2,
                description="HARBOURVIEW PROPERTIES RENT",
            )
        )
        await db_session.commit()

        [rent] = (await api_client.get(DASHBOARD, params=MONTH)).json()["commitments"]

        assert rent["name"] == "Rent"
        assert rent["expected"] == "1800.00"
        assert rent["match"]["occurred_on"] == "2026-08-02"
        assert rent["match"]["amount"] == "1800.00"

    async def test_a_commitment_with_no_payment_reports_nothing_seen(
        self, api_client, db_session
    ):
        """The useful half: no sum could ever tell you a thing did not happen."""
        household, account = await a_household(api_client, "+14165574041")
        await setup_wizard(
            api_client,
            obligations=[
                {"name": "Rent", "monthly_amount": "1800.00"},
                {"name": "Car payment", "monthly_amount": "400.00"},
            ],
        )
        db_session.add(
            tx(
                household,
                account,
                minor=180_000,
                description="HARBOURVIEW PROPERTIES RENT",
            )
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()
        by_name = {item["name"]: item for item in body["commitments"]}

        assert by_name["Rent"]["match"] is not None
        assert by_name["Car payment"]["match"] is None

    async def test_a_similar_amount_alone_is_not_a_match(self, api_client, db_session):
        """A false positive tells somebody their rent went out when it did
        not, which is far worse than reporting it unseen."""
        household, account = await a_household(api_client, "+14165574042")
        await setup_wizard(
            api_client, obligations=[{"name": "Rent", "monthly_amount": "1800.00"}]
        )
        db_session.add(
            tx(household, account, minor=180_000, description="BEST BUY ELECTRONICS")
        )
        await db_session.commit()

        [rent] = (await api_client.get(DASHBOARD, params=MONTH)).json()["commitments"]

        assert rent["match"] is None

    async def test_the_right_name_at_the_wrong_amount_is_not_a_match(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574043")
        await setup_wizard(
            api_client, obligations=[{"name": "Rent", "monthly_amount": "1800.00"}]
        )
        db_session.add(
            tx(household, account, minor=2_500, description="RENT A CAR DOWNTOWN")
        )
        await db_session.commit()

        [rent] = (await api_client.get(DASHBOARD, params=MONTH)).json()["commitments"]

        assert rent["match"] is None

    async def test_a_variable_bill_still_matches_within_tolerance(
        self, api_client, db_session
    ):
        """Hydro is never the same twice. Exact matching would report every
        utility unseen, every month."""
        household, account = await a_household(api_client, "+14165574044")
        await setup_wizard(
            api_client,
            obligations=[{"name": "Toronto Hydro", "monthly_amount": "140.00"}],
        )
        db_session.add(
            tx(household, account, minor=15_100, description="TORONTO HYDRO")
        )
        await db_session.commit()

        [hydro] = (await api_client.get(DASHBOARD, params=MONTH)).json()["commitments"]

        assert hydro["match"] is not None
        assert (
            hydro["match"]["amount"] == "151.00"
        ), "the real figure, not the expected one"

    async def test_one_payment_settles_only_one_commitment(
        self, api_client, db_session
    ):
        """Two commitments of a similar size must not both claim the same
        debit, or the checklist says two things were paid when one was."""
        household, account = await a_household(api_client, "+14165574045")
        await setup_wizard(
            api_client,
            obligations=[
                {"name": "Insurance", "monthly_amount": "100.00"},
                {"name": "Insurance", "monthly_amount": "100.00"},
            ],
        )
        db_session.add(
            tx(household, account, minor=10_000, description="SUNLIFE INSURANCE")
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        matched = [item for item in body["commitments"] if item["match"] is not None]
        assert len(matched) == 1

    async def test_a_payment_in_another_month_does_not_count(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574046")
        await setup_wizard(
            api_client, obligations=[{"name": "Rent", "monthly_amount": "1800.00"}]
        )
        db_session.add(
            tx(household, account, minor=180_000, month=7, description="RENT")
        )
        await db_session.commit()

        [rent] = (await api_client.get(DASHBOARD, params=MONTH)).json()["commitments"]

        assert rent["match"] is None

    async def test_an_incoming_payment_never_settles_a_commitment(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574047")
        await setup_wizard(
            api_client, obligations=[{"name": "Rent", "monthly_amount": "1800.00"}]
        )
        db_session.add(
            tx(
                household,
                account,
                minor=180_000,
                credit=True,
                description="RENT REFUND",
            )
        )
        await db_session.commit()

        [rent] = (await api_client.get(DASHBOARD, params=MONTH)).json()["commitments"]

        assert rent["match"] is None

    async def test_matching_changes_no_total(self, api_client, db_session):
        """Stated outright, because it is the invariant the design rests on."""
        household, account = await a_household(api_client, "+14165574048")
        db_session.add(
            tx(
                household,
                account,
                minor=180_000,
                description="HARBOURVIEW PROPERTIES RENT",
            )
        )
        await db_session.commit()

        await setup_wizard(api_client)
        without = (await api_client.get(DASHBOARD, params=MONTH)).json()
        await setup_wizard(
            api_client, obligations=[{"name": "Rent", "monthly_amount": "1800.00"}]
        )
        with_commitment = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert with_commitment["commitments"][0]["match"] is not None
        assert with_commitment["expenses"]["actual"] == without["expenses"]["actual"]
        assert with_commitment["net"] == without["net"]


class TestTheTrend:
    async def test_a_month_with_no_rows_is_a_gap_and_not_a_zero(
        self, api_client, db_session
    ):
        """A zero-height bar states a fact nobody observed. The client has to
        be able to tell "netted nothing" from "we know nothing"."""
        household, account = await a_household(api_client, "+14165574050")
        db_session.add_all(
            [
                tx(household, account, minor=100_000, credit=True, month=8),
                tx(household, account, minor=100_000, credit=True, month=6),
            ]
        )
        await db_session.commit()

        trend = (await api_client.get(DASHBOARD, params=MONTH)).json()["trend"]
        by_month = {point["month"]: point["net"] for point in trend}

        assert by_month["2026-08-01"] == "1000.00"
        assert by_month["2026-06-01"] == "1000.00"
        assert by_month["2026-07-01"] is None, "July had no rows; it is not a zero"

    async def test_a_month_that_truly_netted_zero_is_not_a_gap(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574051")
        db_session.add_all(
            [
                tx(household, account, minor=50_000, credit=True, month=7),
                tx(household, account, minor=50_000, month=7, day=6),
            ]
        )
        await db_session.commit()

        trend = (await api_client.get(DASHBOARD, params=MONTH)).json()["trend"]
        by_month = {point["month"]: point["net"] for point in trend}

        assert by_month["2026-07-01"] == "0.00", "observed zero, not absence"

    async def test_the_trend_ends_at_the_month_being_viewed(self, api_client):
        await a_household(api_client, "+14165574052")
        trend = (await api_client.get(DASHBOARD, params=MONTH)).json()["trend"]

        assert trend[-1]["month"] == "2026-08-01"
        assert trend[0]["month"] == "2025-11-01", "ten months, oldest first"
        assert [point["month"] for point in trend] == sorted(
            point["month"] for point in trend
        )

    async def test_the_previous_month_is_null_when_there_is_nothing_to_compare(
        self, api_client, db_session
    ):
        """Otherwise the client prints a rise from zero, which is infinite."""
        household, account = await a_household(api_client, "+14165574053")
        db_session.add(tx(household, account, minor=100_000, credit=True))
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["net"] == "1000.00"
        assert body["previous_net"] is None

    async def test_the_previous_month_is_reported_when_there_is(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574054")
        db_session.add_all(
            [
                tx(household, account, minor=100_000, credit=True, month=8),
                tx(household, account, minor=60_000, credit=True, month=7),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["previous_net"] == "600.00"


class TestWhatIsLeftOutOfTheTotals:
    async def test_an_unresolved_suspected_duplicate_is_not_counted(
        self, api_client, db_session
    ):
        """It is in the queue *because* we think it is the same payment twice.
        A figure that silently corrects itself downward after review is worse
        than one that was never wrong."""
        household, account = await a_household(api_client, "+14165574060")
        db_session.add_all(
            [
                tx(household, account, minor=50_000, description="LOBLAWS 1042"),
                # Same day and amount under a different name, which is what a
                # suspected duplicate IS — and what the dedup index allows
                # through, since it is not a byte-for-byte repeat.
                tx(
                    household,
                    account,
                    minor=50_000,
                    description="LOBLAWS #1042 TORONTO",
                    needs_review=True,
                    reason=ReviewReason.suspected_duplicate,
                ),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["expenses"]["actual"] == "500.00", "counted once, not twice"
        assert body["pending_review"] == 1

    async def test_a_row_merely_awaiting_review_still_counts(
        self, api_client, db_session
    ):
        """Low confidence about a category is not doubt that money moved."""
        household, account = await a_household(api_client, "+14165574061")
        db_session.add(
            tx(
                household,
                account,
                minor=50_000,
                needs_review=True,
                reason=ReviewReason.low_confidence,
            )
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["expenses"]["actual"] == "500.00"
        assert body["pending_review"] == 1

    async def test_another_currency_is_not_added_in(self, api_client, db_session):
        """Adding CAD to USD produces a number that is not money, and
        converting needs a rate and a date we do not have."""
        household, account = await a_household(api_client, "+14165574062")
        db_session.add_all(
            [
                tx(household, account, minor=50_000),
                tx(household, account, minor=90_000, currency="USD"),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["currency"] == "CAD"
        assert body["expenses"]["actual"] == "500.00"

    async def test_another_household_is_never_included(self, api_client, db_session):
        stranger, stranger_account = await a_household(api_client, "+14165574063")
        db_session.add(tx(stranger, stranger_account, minor=900_000))
        await db_session.commit()

        mine, my_account = await a_household(api_client, "+14165574064")
        db_session.add(tx(mine, my_account, minor=1_000))
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["expenses"]["actual"] == "10.00"


class TestTheBalanceCards:
    async def test_holdings_and_debts_come_from_the_wizard(self, api_client):
        await a_household(api_client, "+14165574070")
        await setup_wizard(
            api_client,
            investments=[{"name": "TFSA", "amount": "40000.00"}],
            debts=[{"name": "Car loan", "balance": "12000.00"}],
        )

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["investments"]["balance"] == "40000.00"
        assert body["debts"]["balance"] == "12000.00"
        assert body["investments"]["moved"] == "0.00", "nothing moved this month"

    async def test_movement_comes_from_what_the_bank_shows(
        self, api_client, db_session
    ):
        """The balance is what the wizard was told; the movement is observed.
        We never see a portfolio's value or a loan's principal."""
        household, account = await a_household(api_client, "+14165574071")
        await setup_wizard(
            api_client,
            investments=[{"name": "TFSA", "amount": "40000.00"}],
            debts=[{"name": "Car loan", "balance": "12000.00"}],
        )
        savings = await db_session.scalar(
            select(Category.id).where(
                Category.slug == "savings", Category.household_id.is_(None)
            )
        )
        repayment = await db_session.scalar(
            select(Category.id).where(
                Category.slug == "debt_payment", Category.household_id.is_(None)
            )
        )
        db_session.add_all(
            [
                tx(household, account, minor=50_000, category_id=savings),
                tx(household, account, minor=40_000, category_id=repayment),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert body["investments"]["moved"] == "500.00"
        assert body["debts"]["moved"] == "400.00"
        assert body["investments"]["balance"] == "40000.00", "the balance does not move"


class TestTheDailyBalance:
    """The home chart: the month's running in-minus-out, one point a day."""

    async def test_it_is_a_running_sum_that_ends_at_the_net(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574901")
        db_session.add_all(
            [
                tx(household, account, minor=500_000, day=1, credit=True),
                tx(household, account, minor=120_000, day=3),
                tx(household, account, minor=30_000, day=3),
                tx(household, account, minor=50_000, day=20),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()
        daily = {point["day"]: point["net"] for point in body["daily"]}

        assert daily["2026-08-01"] == "5000.00"
        assert daily["2026-08-02"] == "5000.00", "a quiet day keeps yesterday's"
        assert daily["2026-08-03"] == "3500.00", "a day's rows are summed"
        assert daily["2026-08-19"] == "3500.00"
        assert daily["2026-08-20"] == "3000.00"
        assert body["daily"][-1]["net"] == body["net"]

    async def test_a_past_month_runs_to_its_last_day(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574902")
        db_session.add(tx(household, account, minor=1_000, day=9))
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        days = [point["day"] for point in body["daily"]]
        assert days[0] == "2026-08-01", "from the first, not the first row"
        assert days[-1] == "2026-08-31"
        assert len(days) == 31

    async def test_it_counts_what_the_net_counts(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574903")
        db_session.add_all(
            [
                tx(household, account, minor=10_000, day=2),
                tx(
                    household,
                    account,
                    minor=99_900,
                    day=4,
                    needs_review=True,
                    reason=ReviewReason.suspected_duplicate,
                ),
                tx(household, account, minor=77_700, day=6, currency="USD"),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=MONTH)).json()

        assert {point["net"] for point in body["daily"][1:]} == {"-100.00"}

    async def test_a_month_with_nothing_in_it_has_no_line(self, api_client):
        await a_household(api_client, "+14165574904")
        body = (await api_client.get(DASHBOARD, params=MONTH)).json()
        assert body["daily"] == []

    async def test_the_running_month_stops_at_today(self, api_client, db_session):
        from app.services import dashboard as service

        household, account = await a_household(api_client, "+14165574905")
        db_session.add(tx(household, account, minor=2_500, day=3))
        await db_session.commit()

        built = await service.build(
            db_session, household, "CAD", date(2026, 8, 1), today=date(2026, 8, 10)
        )

        assert built.daily[-1].day == date(2026, 8, 10)
        assert len(built.daily) == 10

    async def test_a_month_not_yet_started_has_no_line(self, api_client, db_session):
        from app.services import dashboard as service

        household, account = await a_household(api_client, "+14165574906")
        db_session.add(tx(household, account, minor=2_500, day=3))
        await db_session.commit()

        built = await service.build(
            db_session, household, "CAD", date(2026, 8, 1), today=date(2026, 7, 31)
        )

        assert built.daily == []
