"""Money moving between a household's own accounts (#73).

A card bill paid from chequing, and money put into savings, are in neither
income nor expenses — imported in either order, and whatever the categorizer
first thought. The rules lean towards not pairing, because a wrong pair hides
real money: ties, doubtful rows and a person's own choices are left alone.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.models.categorization import Category, CategoryCorrection
from app.models.enums import ReviewReason, SourceKind, StatementImportStatus
from app.models.money import StatementImport, Transaction
from app.services import filing, transfers
from tests.conftest import requires_db
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
    """Signed in, past the terms, with a chequing, a card and a savings account."""
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    await api_client.post("/me/consent", json={"version": version})
    accounts = {"household": me["household"]["id"]}
    for name, kind in (
        ("chequing", "chequing"),
        ("card", "credit_card"),
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
        await imported(api_client, db_session, me["household"], me["chequing"], BANK)

        await assert_paid_once(api_client, db_session, me["household"])

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
