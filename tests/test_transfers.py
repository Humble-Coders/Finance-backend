"""Money moving between a household's own accounts (#73).

A card bill paid from chequing, and money put into savings, are in neither
income nor expenses — imported in either order, and whatever the categorizer
first thought. The rules lean towards not pairing, because a wrong pair hides
real money: ties, doubtful rows and a person's own choices are left alone.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.models.categorization import Category, CategoryCorrection
from app.models.enums import ReviewReason, SourceKind, StatementImportStatus
from app.models.money import StatementImport, Transaction
from app.services import budget as budget_service
from app.services import filing, transfers
from tests.conftest import requires_db
from tests.test_budgets_api import a_ready_household
from tests.test_ledger_endpoint import authenticate_as

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

AUGUST = {"month": "2026-08"}

# What the fake categorizer files each merchant as — what a model plausibly
# answers, including the wrong answers this ticket exists to correct.
FILED_AS = {
    "payroll": "income",
    "visa payment": "debt_payment",
    "payment thank you": "income",
    "loblaws": "groceries",
    "cineplex": "entertainment",
    "transfer to savings": "savings",
    "deposit from chequing": "income",
    "amazon refund": "shopping",
    "loan payment": "debt_payment",
    "payment received": "income",
}


class Categorizer:
    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        answers = []
        for merchant, _amount in json.loads(user):
            name = merchant.lower()
            answers.append(
                next((slug for key, slug in FILED_AS.items() if key in name), "other")
            )
        return json.dumps(answers)

    @property
    def model(self) -> str:
        return "fake-categorizer"


@pytest.fixture(autouse=True)
def categorizer(monkeypatch):
    monkeypatch.setattr(filing, "build_client", lambda _s: Categorizer())


async def a_household(api_client, phone: str) -> dict[str, str]:
    """Signed in, past the terms, with one account of each kind pairing reads."""
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    await api_client.post("/me/consent", json={"version": version})
    accounts = {"household": me["household"]["id"]}
    for name, kind in (
        ("chequing", "chequing"),
        ("card", "credit_card"),
        ("loan", "loan"),
        ("investment", "investment"),
        ("savings", "savings"),
    ):
        response = await api_client.post("/accounts", json={"name": name, "kind": kind})
        assert response.status_code == 201, response.text
        accounts[name] = response.json()["id"]
    return accounts


def row(description: str, amount: str, day: int, *, credit=False, confidence=95):
    return {
        "occurred_on": f"2026-08-{day:02d}",
        "description": description,
        "amount": amount,
        "direction": "credit" if credit else "debit",
        "confidence": confidence,
    }


async def imported(api_client, db_session, household: str, account: str, rows):
    """A statement's rows saved against [account], as the app saves them."""
    record = StatementImport(
        household_id=uuid.UUID(household),
        source_kind=SourceKind.pdf_text,
        status=StatementImportStatus.awaiting_review,
    )
    db_session.add(record)
    await db_session.flush()
    response = await api_client.post(
        f"/statements/{record.id}/transactions",
        json={"account_id": account, "rows": rows},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def the_row(db_session, household: str, description: str) -> Transaction:
    result = await db_session.execute(
        select(Transaction).where(
            Transaction.household_id == uuid.UUID(household),
            Transaction.description == description,
        )
    )
    found = result.scalar_one()
    await db_session.refresh(found)
    return found


async def slug_of(db_session, row: Transaction) -> str | None:
    if row.category_id is None:
        return None
    return await db_session.scalar(
        select(Category.slug).where(Category.id == row.category_id)
    )


async def august(api_client) -> dict:
    response = await api_client.get("/dashboard", params=AUGUST)
    assert response.status_code == 200, response.text
    return response.json()


BANK = [
    row("PAYROLL ACME", "5000.00", 1, credit=True),
    row("VISA PAYMENT", "1000.00", 20),
]
CARD = [
    row("LOBLAWS 1042", "600.00", 5),
    row("CINEPLEX 33", "400.00", 9),
    row("PAYMENT THANK YOU", "1000.00", 22, credit=True),
]


async def assert_paid_once(api_client, db_session, household: str) -> None:
    """The card bill counted once: its purchases, and nothing for the payment."""
    body = await august(api_client)
    assert body["income"]["actual"] == "5000.00", "the card's payment is not income"
    assert body["expenses"]["actual"] == "1000.00", "the purchases, not twice"
    assert [e["slug"] for e in body["spend_by_category"]] == [
        "groceries",
        "entertainment",
    ]

    bank = await the_row(db_session, household, "VISA PAYMENT")
    card = await the_row(db_session, household, "PAYMENT THANK YOU")
    for side in (bank, card):
        assert await slug_of(db_session, side) == "transfers"
        assert side.needs_review is True
        assert side.review_reason == ReviewReason.own_transfer
    assert bank.transfer_pair_id == card.id
    assert card.transfer_pair_id == bank.id


class TestACardBill:
    async def test_bank_first_then_card(self, api_client, db_session):
        me = await a_household(api_client, "+14165578001")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        await assert_paid_once(api_client, db_session, me["household"])

    async def test_card_first_then_bank(self, api_client, db_session):
        me = await a_household(api_client, "+14165578002")
        await imported(api_client, db_session, me["household"], me["card"], CARD)
        saved = await imported(
            api_client, db_session, me["household"], me["chequing"], BANK
        )

        await assert_paid_once(api_client, db_session, me["household"])
        # Only VISA PAYMENT is this import's: the card's payment was already a
        # transfer (filed by the card rule), so no earlier row changed.
        assert saved["earlier_needs_review"] == 0

    async def test_an_earlier_row_sent_back_to_review_is_reported(
        self, api_client, db_session
    ):
        """The bank's payment was saved first and counted as spending; the
        card's import re-files it, and says so — its own `needs_review`
        counts only the card's rows."""
        me = await a_household(api_client, "+14165578006")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        saved = await imported(
            api_client, db_session, me["household"], me["card"], CARD
        )

        assert saved["earlier_needs_review"] == 1
        assert saved["needs_review"] == 1, "the card's own payment received"

    async def test_the_card_alone_files_its_payment_as_a_transfer(
        self, api_client, db_session
    ):
        """Its purchases are on the same statement, so the payment can only
        be money the household already had."""
        me = await a_household(api_client, "+14165578003")
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        body = await august(api_client)
        assert body["income"]["actual"] == "0.00"
        assert body["expenses"]["actual"] == "1000.00"
        card = await the_row(db_session, me["household"], "PAYMENT THANK YOU")
        assert await slug_of(db_session, card) == "transfers"
        assert card.review_reason == ReviewReason.own_transfer
        assert card.transfer_pair_id is None

    async def test_the_bank_alone_still_counts_the_payment(
        self, api_client, db_session
    ):
        """Without the card's statement it is the only record of that
        spending. When the statement arrives, the month corrects itself."""
        me = await a_household(api_client, "+14165578004")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        before = await august(api_client)
        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert await slug_of(db_session, bank) == "debt_payment"
        assert before["expenses"]["actual"] == "1000.00"

        await imported(api_client, db_session, me["household"], me["card"], CARD)

        await assert_paid_once(api_client, db_session, me["household"])

    async def test_a_refund_on_the_card_is_left_alone(self, api_client, db_session):
        me = await a_household(api_client, "+14165578005")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["card"],
            [row("AMAZON REFUND", "40.00", 12, credit=True)],
        )

        refund = await the_row(db_session, me["household"], "AMAZON REFUND")
        assert await slug_of(db_session, refund) == "shopping"
        assert refund.review_reason != ReviewReason.own_transfer


class TestDebtPaid:
    """Paying the card is paying debt, counted once whichever statements are
    in: the bank's debit alone, the card's payment received alone, and with
    both, the card's side — the bank's is a transfer by then (review of #74)."""

    async def test_the_bank_statement_alone(self, api_client, db_session):
        me = await a_household(api_client, "+14165578060")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        assert (await august(api_client))["debts"]["moved"] == "1000.00"

    async def test_the_card_statement_alone(self, api_client, db_session):
        me = await a_household(api_client, "+14165578061")
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        assert (await august(api_client))["debts"]["moved"] == "1000.00"

    async def test_both_statements_count_it_once(self, api_client, db_session):
        me = await a_household(api_client, "+14165578062")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        body = await august(api_client)
        assert body["debts"]["moved"] == "1000.00"
        point = next(p for p in body["trend"] if p["month"] == "2026-08-01")
        assert point["debt_paid"] == "1000.00", "the score reads this figure"

    async def test_a_loan_payment_stays_a_debt_payment(self, api_client, db_session):
        """The loan's statement lists no purchases, so the payment is the debt
        being paid: the bank's side keeps `debt_payment`, the loan's side is a
        transfer, and the payment is counted once."""
        me = await a_household(api_client, "+14165578063")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [row("LOAN PAYMENT", "350.00", 10)],
        )
        await imported(
            api_client,
            db_session,
            me["household"],
            me["loan"],
            [row("PAYMENT RECEIVED", "350.00", 11, credit=True)],
        )

        bank = await the_row(db_session, me["household"], "LOAN PAYMENT")
        loan = await the_row(db_session, me["household"], "PAYMENT RECEIVED")
        assert await slug_of(db_session, bank) == "debt_payment"
        assert bank.review_reason is None, "unchanged, so nothing to confirm"
        assert await slug_of(db_session, loan) == "transfers"
        assert loan.review_reason == ReviewReason.own_transfer
        assert bank.transfer_pair_id == loan.id
        body = await august(api_client)
        assert body["debts"]["moved"] == "350.00"
        assert body["income"]["actual"] == "0.00"
        assert body["expenses"]["actual"] == "350.00"


class TestSaving:
    async def test_money_set_aside_is_not_spent(self, api_client, db_session):
        me = await a_household(api_client, "+14165578010")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [
                row("PAYROLL ACME", "5000.00", 1, credit=True),
                row("LOBLAWS 1042", "600.00", 5),
                row("TRANSFER TO SAVINGS", "500.00", 15),
            ],
        )

        body = await august(api_client)
        assert body["expenses"]["actual"] == "600.00", "saving is not spending"
        assert body["net"] == "4400.00", "and it is kept"
        assert body["investments"]["moved"] == "500.00", "but it is set aside"

    async def test_the_savings_statement_s_deposit_is_not_income(
        self, api_client, db_session
    ):
        """Both sides imported: the chequing side stays savings, so the
        investments card still sees it; the deposit becomes a transfer."""
        me = await a_household(api_client, "+14165578011")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [
                row("PAYROLL ACME", "5000.00", 1, credit=True),
                row("TRANSFER TO SAVINGS", "500.00", 15),
            ],
        )
        await imported(
            api_client,
            db_session,
            me["household"],
            me["savings"],
            [row("DEPOSIT FROM CHEQUING", "500.00", 16, credit=True)],
        )

        body = await august(api_client)
        assert body["income"]["actual"] == "5000.00"
        assert body["expenses"]["actual"] == "0.00"
        assert body["investments"]["moved"] == "500.00"
        out = await the_row(db_session, me["household"], "TRANSFER TO SAVINGS")
        deposit = await the_row(db_session, me["household"], "DEPOSIT FROM CHEQUING")
        assert await slug_of(db_session, out) == "savings"
        assert out.review_reason is None, "unchanged, so nothing to confirm"
        assert await slug_of(db_session, deposit) == "transfers"
        assert deposit.review_reason == ReviewReason.own_transfer
        assert (out.transfer_pair_id, deposit.transfer_pair_id) == (
            deposit.id,
            out.id,
        )


class TestNoFalsePairs:
    async def test_two_equally_near_partners_pair_neither(self, api_client, db_session):
        me = await a_household(api_client, "+14165578020")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["card"],
            [
                row("PAYMENT THANK YOU A", "1000.00", 18, credit=True),
                row("PAYMENT THANK YOU B", "1000.00", 22, credit=True),
            ],
        )
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert bank.transfer_pair_id is None
        assert await slug_of(db_session, bank) == "debt_payment"

    async def test_two_rows_wanting_one_partner_are_both_left(
        self, api_client, db_session
    ):
        me = await a_household(api_client, "+14165578021")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["card"],
            [row("PAYMENT THANK YOU", "1000.00", 20, credit=True)],
        )
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [
                row("VISA PAYMENT A", "1000.00", 18),
                row("VISA PAYMENT B", "1000.00", 22),
            ],
        )

        for description in ("VISA PAYMENT A", "VISA PAYMENT B"):
            bank = await the_row(db_session, me["household"], description)
            assert bank.transfer_pair_id is None, description
        card = await the_row(db_session, me["household"], "PAYMENT THANK YOU")
        assert card.transfer_pair_id is None

    async def test_the_nearest_partner_wins(self, api_client, db_session):
        me = await a_household(api_client, "+14165578022")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["card"],
            [
                row("PAYMENT THANK YOU A", "1000.00", 17, credit=True),
                row("PAYMENT THANK YOU B", "1000.00", 21, credit=True),
            ],
        )
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        nearer = await the_row(db_session, me["household"], "PAYMENT THANK YOU B")
        assert bank.transfer_pair_id == nearer.id

    async def test_more_than_five_days_apart_is_not_a_pair(
        self, api_client, db_session
    ):
        me = await a_household(api_client, "+14165578023")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["card"],
            [row("PAYMENT THANK YOU", "1000.00", 26, credit=True)],
        )
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert bank.transfer_pair_id is None

    async def test_a_category_the_person_chose_is_never_changed(
        self, api_client, db_session
    ):
        """They have a rule for VISA PAYMENT: it is a debt payment to them."""
        me = await a_household(api_client, "+14165578024")
        debt = await db_session.scalar(
            select(Category.id).where(
                Category.household_id.is_(None), Category.slug == "debt_payment"
            )
        )
        db_session.add(
            CategoryCorrection(
                household_id=uuid.UUID(me["household"]),
                merchant_pattern="visa payment",
                corrected_category_id=debt,
            )
        )
        await db_session.flush()
        await imported(api_client, db_session, me["household"], me["card"], CARD)
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert await slug_of(db_session, bank) == "debt_payment"
        assert bank.transfer_pair_id is None
        assert bank.review_reason != ReviewReason.own_transfer

    async def test_a_doubtful_amount_is_never_paired(self, api_client, db_session):
        me = await a_household(api_client, "+14165578025")
        await imported(api_client, db_session, me["household"], me["card"], CARD)
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [row("VISA PAYMENT", "1000.00", 20, confidence=40)],
        )

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert bank.transfer_pair_id is None
        assert bank.review_reason == ReviewReason.low_confidence

    async def test_spending_is_never_refiled_by_a_coincidence(
        self, api_client, db_session
    ):
        """Groceries on chequing the same size as a card credit is still
        groceries: the categorizer called it spending."""
        me = await a_household(api_client, "+14165578026")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["card"],
            [row("PAYMENT THANK YOU", "600.00", 6, credit=True)],
        )
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [row("LOBLAWS 1042", "600.00", 5)],
        )

        groceries = await the_row(db_session, me["household"], "LOBLAWS 1042")
        assert await slug_of(db_session, groceries) == "groceries"
        assert groceries.transfer_pair_id is None

    async def test_a_suspected_duplicate_is_never_a_partner(
        self, api_client, db_session
    ):
        """VISA PMT is flagged as a possible copy of VISA PAYMENT. Were it a
        candidate, the card's payment would have two equally near partners
        and pair with neither."""
        me = await a_household(api_client, "+14165578029")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [row("VISA PMT", "1000.00", 20)],
        )
        copy = await the_row(db_session, me["household"], "VISA PMT")
        assert copy.review_reason == ReviewReason.suspected_duplicate

        await imported(api_client, db_session, me["household"], me["card"], CARD)

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        card = await the_row(db_session, me["household"], "PAYMENT THANK YOU")
        copy = await the_row(db_session, me["household"], "VISA PMT")
        assert bank.transfer_pair_id == card.id
        assert copy.transfer_pair_id is None
        assert copy.review_reason == ReviewReason.suspected_duplicate

    async def test_an_investment_purchase_never_hides_pay(self, api_client, db_session):
        """An investment statement lists purchases as debits. One matching a
        pay cheque to the cent is still not the pay cheque."""
        me = await a_household(api_client, "+14165578032")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [row("PAYROLL ACME", "2000.00", 15, credit=True)],
        )
        await imported(
            api_client,
            db_session,
            me["household"],
            me["investment"],
            [row("BUY XEQT 40 UNITS", "2000.00", 16)],
        )

        pay = await the_row(db_session, me["household"], "PAYROLL ACME")
        assert await slug_of(db_session, pay) == "income"
        assert pay.transfer_pair_id is None
        assert (await august(api_client))["income"]["actual"] == "2000.00"

    async def test_another_household_s_row_is_never_a_partner(
        self, api_client, db_session
    ):
        other = await a_household(api_client, "+14165578027")
        await imported(api_client, db_session, other["household"], other["card"], CARD)
        me = await a_household(api_client, "+14165578028")
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert bank.transfer_pair_id is None


class TestTypedIn:
    async def test_a_payment_typed_in_pairs_with_the_imported_card(
        self, api_client, db_session
    ):
        me = await a_household(api_client, "+14165578030")
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        typed = await api_client.post(
            "/transactions",
            json={
                "account_id": me["chequing"],
                "occurred_on": "2026-08-20",
                "amount": "1000.00",
                "direction": "debit",
                "description": "VISA PAYMENT",
            },
        )
        assert typed.status_code == 201, typed.text

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        card = await the_row(db_session, me["household"], "PAYMENT THANK YOU")
        assert bank.transfer_pair_id == card.id
        assert await slug_of(db_session, bank) == "transfers"

    async def test_a_category_typed_in_is_kept(self, api_client, db_session):
        me = await a_household(api_client, "+14165578031")
        await imported(api_client, db_session, me["household"], me["card"], CARD)
        debt = await db_session.scalar(
            select(Category.id).where(
                Category.household_id.is_(None), Category.slug == "debt_payment"
            )
        )

        typed = await api_client.post(
            "/transactions",
            json={
                "account_id": me["chequing"],
                "occurred_on": "2026-08-20",
                "amount": "1000.00",
                "direction": "debit",
                "description": "VISA PAYMENT",
                "category_id": str(debt),
            },
        )
        assert typed.status_code == 201, typed.text

        bank = await the_row(db_session, me["household"], "VISA PAYMENT")
        assert await slug_of(db_session, bank) == "debt_payment"
        assert bank.transfer_pair_id is None


class TestEveryTotalAgrees:
    async def test_net_trend_daily_and_breakdown_count_alike(
        self, api_client, db_session
    ):
        """Income, expenses, net, the trend's month, the day-by-day line and
        the breakdown — one rule, so one answer."""
        me = await a_household(api_client, "+14165578040")
        await imported(
            api_client,
            db_session,
            me["household"],
            me["chequing"],
            [*BANK, row("TRANSFER TO SAVINGS", "500.00", 15)],
        )
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        body = await august(api_client)
        point = next(p for p in body["trend"] if p["month"] == "2026-08-01")

        assert (point["income"], point["expenses"], point["net"]) == (
            body["income"]["actual"],
            body["expenses"]["actual"],
            body["net"],
        )
        assert body["daily"][-1]["net"] == body["net"]
        cents = sum(int(e["spent"].replace(".", "")) for e in body["spend_by_category"])
        assert f"{cents // 100}.{cents % 100:02d}" == body["expenses"]["actual"]
        assert body["net"] == "4000.00"


class TestCost:
    async def test_the_queries_do_not_grow_with_the_rows(
        self, api_client, db_session, monkeypatch
    ):
        """Reads per save are fixed: candidates for every row come back from
        one query, not one per row."""
        me = await a_household(api_client, "+14165578050")
        await imported(api_client, db_session, me["household"], me["card"], CARD)

        reads: list[int] = []
        real = transfers.pair_transfers

        async def counted(session, household_id, saved_ids, **kwargs):
            calls = 0
            execute, scalar = session.execute, session.scalar

            async def counting_execute(*args, **kw):
                nonlocal calls
                calls += 1
                return await execute(*args, **kw)

            async def counting_scalar(*args, **kw):
                nonlocal calls
                calls += 1
                return await scalar(*args, **kw)

            monkeypatch.setattr(session, "execute", counting_execute)
            monkeypatch.setattr(session, "scalar", counting_scalar)
            try:
                return await real(session, household_id, saved_ids, **kwargs)
            finally:
                monkeypatch.setattr(session, "execute", execute)
                monkeypatch.setattr(session, "scalar", scalar)
                reads.append(calls)

        monkeypatch.setattr("app.api.statements.pair_transfers", counted)
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)
        many = [row(f"VISA PAYMENT {n}", "1000.00", 20) for n in range(30)]
        await imported(api_client, db_session, me["household"], me["chequing"], many)

        # The saved rows, their candidates, the household's rules, and the
        # transfers category when anything is re-filed: four at most, for one
        # row or thirty.
        assert len(reads) == 2
        assert max(reads) <= 4, reads


class TestTheBudgetAgreesWithHome:
    """The budget's debt line and Home's "debt paid" are one figure, whichever
    statements are in (second review of #74). The ready household already
    pays $300 a month on its Visa; this adds a $1,000 card bill."""

    @pytest.fixture(autouse=True)
    def pinned_today(self, monkeypatch):
        monkeypatch.setattr("app.api.budgets._today", lambda: date(2026, 9, 15))

    @staticmethod
    def september(row_: dict) -> dict:
        return {**row_, "occurred_on": row_["occurred_on"].replace("-08-", "-09-")}

    @pytest.mark.parametrize(
        ("phone", "bank", "card"),
        [
            ("+14165578070", True, False),
            ("+14165578071", False, True),
            ("+14165578072", True, True),
        ],
        ids=["bank only", "card only", "both"],
    )
    async def test_the_debt_line_is_home_s_debt_paid(
        self, api_client, db_session, phone, bank, card
    ):
        household, _, _ = await a_ready_household(api_client, db_session, phone)
        accounts = {}
        for name, kind in (("chq2", "chequing"), ("visa", "credit_card")):
            created = await api_client.post(
                "/accounts", json={"name": name, "kind": kind}
            )
            accounts[name] = created.json()["id"]
        if bank:
            await imported(
                api_client,
                db_session,
                str(household),
                accounts["chq2"],
                [self.september(row("VISA PAYMENT", "1000.00", 10))],
            )
        if card:
            await imported(
                api_client,
                db_session,
                str(household),
                accounts["visa"],
                [self.september(row("PAYMENT THANK YOU", "1000.00", 12, credit=True))],
            )

        budget = (await api_client.get("/budgets/2026-09")).json()
        home = (await api_client.get("/dashboard", params={"month": "2026-09"})).json()

        assert budget["debt"]["spent"] == home["debts"]["moved"] == "1300.00"

    async def test_the_learned_debt_line_counts_the_card_s_side(
        self, api_client, db_session
    ):
        """With both statements in, the bank's payment is a transfer; the
        months the debt line learns from must still see the payment."""
        household, _, _ = await a_ready_household(
            api_client, db_session, "+14165578073"
        )
        before = await budget_service._history(
            db_session, household, "CAD", date(2026, 9, 1)
        )
        card = await api_client.post(
            "/accounts", json={"name": "visa", "kind": "credit_card"}
        )
        await imported(
            api_client,
            db_session,
            str(household),
            card.json()["id"],
            [row("PAYMENT THANK YOU", "1000.00", 12, credit=True)],
        )

        after = await budget_service._history(
            db_session, household, "CAD", date(2026, 9, 1)
        )

        def debt(history) -> list[int]:
            window, totals = history
            return next(v for k, v in totals.items() if k.slug == "debt_payment")

        assert [a - b for a, b in zip(debt(after), debt(before), strict=True)] == [
            0,
            0,
            100_000,
        ], "June, July, August: the card's payment received is August's"
