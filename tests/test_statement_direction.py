"""Money in or money out: the inputs and checks that decide a row's direction.

The model is faked. What is tested is what surrounds it: the account type and
column headings reaching every window, `Cr` / `Dr` amounts surviving the
verbatim check, and the running balance correcting a direction the model got
wrong — and leaving alone anything it cannot prove.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.enums import AccountKind, TransactionDirection
from app.schemas.statements import StatementParseIn
from app.services import statements
from app.services.statement_balance import reconcile, signed_balance
from app.services.statements import ParsedRow, parse_statement

DEBIT = TransactionDirection.debit
CREDIT = TransactionDirection.credit


class FakeModel:
    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "fake-1"

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        self.prompts.append(user)
        return self._answers.pop(0) if self._answers else "[]"


def answer(*items: dict) -> str:
    return json.dumps(list(items))


def item(amount, description, day, direction, balance=None):
    return {
        "date": day,
        "description": description,
        "amount": amount,
        "direction": direction,
        "balance": balance,
        "confidence": 90,
    }


def parsed(amount: str, direction, balance: str | None, day: int = 14) -> ParsedRow:
    return ParsedRow(
        occurred_on=date(2026, 8, day),
        description=f"ROW {amount}",
        amount=amount,
        direction=direction,
        confidence=90,
        balance=None if balance is None else Decimal(balance),
    )


# A two-column bank statement as a flattened text layer reads: nothing says
# which column 450.00 sat in.
CHEQUING = "\n".join(
    [
        "14 Aug   UPI-SWIGGY 12345        450.00     12,340.50",
        "15 Aug   NEFT SALARY ACME     52,000.00     64,340.50",
        "16 Aug   ATM WDL                2,000.00     62,340.50",
    ]
)


class TestWhatEveryWindowIsTold:
    @pytest.mark.asyncio
    async def test_the_account_type_and_columns_head_the_prompt(self):
        model = FakeModel()
        await parse_statement(
            model,
            CHEQUING,
            "INR",
            account_kind=AccountKind.credit_card,
            columns="Date | Details | Debit | Credit | Balance",
        )
        prompt = model.prompts[0]
        assert prompt.startswith("STATEMENT HEADER\n")
        assert "Account type: credit card" in prompt
        assert (
            "Columns, left to right: Date | Details | Debit | Credit | Balance"
            in prompt
        )
        assert prompt.endswith(CHEQUING)

    @pytest.mark.asyncio
    async def test_every_window_carries_them(self, monkeypatch):
        monkeypatch.setattr(statements, "CHUNK_CHARS", 80)
        model = FakeModel()
        await parse_statement(
            model,
            CHEQUING,
            "INR",
            account_kind=AccountKind.chequing,
            columns="Withdrawals | Deposits",
        )
        assert len(model.prompts) > 1
        for prompt in model.prompts:
            assert "Account type: bank account (chequing)" in prompt
            assert "Columns, left to right: Withdrawals | Deposits" in prompt

    @pytest.mark.asyncio
    async def test_columns_with_anything_but_words_are_not_passed_on(self):
        model = FakeModel()
        await parse_statement(model, CHEQUING, "INR", columns="Balance <script>")
        assert "Columns" not in model.prompts[0]

    @pytest.mark.asyncio
    async def test_french_headings_are_passed_on(self):
        model = FakeModel()
        await parse_statement(
            model, CHEQUING, "CAD", columns="Date | Retraits | Dépôts | Solde"
        )
        assert (
            "Columns, left to right: Date | Retraits | Dépôts | Solde"
            in model.prompts[0]
        )

    @pytest.mark.asyncio
    async def test_without_either_the_prompt_is_unchanged(self):
        model = FakeModel()
        await parse_statement(model, CHEQUING, "INR")
        assert model.prompts[0] == CHEQUING

    def test_the_request_refuses_digits_in_the_columns(self):
        with pytest.raises(ValidationError):
            StatementParseIn(
                source_kind="pdf_text", text="x", statement_columns="Account 4521"
            )
        ok = StatementParseIn(
            source_kind="pdf_text", text="x", statement_columns=" Debit | Credit "
        )
        assert ok.statement_columns == "Debit | Credit"


class TestCrAndDr:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "printed", ["86.40 Cr", "86.40CR", "86.40 Dr.", "(86.40)", "86.40-"]
    )
    async def test_an_amount_copied_with_its_marker_is_read(self, printed):
        text = f"14 Aug   REFUND STORE   {printed}"
        model = FakeModel(answer(item(printed, "REFUND STORE", "2026-08-14", "credit")))
        outcome = await parse_statement(model, text, "CAD")
        assert [row.amount for row in outcome.rows] == ["86.40"]
        assert outcome.unparsed_line_count == 0


class TestSignedBalance:
    def test_held_money(self):
        assert signed_balance("12,340.50", "12340.50", AccountKind.chequing) == Decimal(
            "12340.50"
        )
        assert signed_balance("1,204.55 Dr", "1204.55", AccountKind.savings) == Decimal(
            "-1204.55"
        )
        assert signed_balance(
            "1,204.55 Cr", "1204.55", AccountKind.chequing
        ) == Decimal("1204.55")
        assert signed_balance("(12.00)", "12.00", AccountKind.chequing) == Decimal(
            "-12.00"
        )
        assert signed_balance("8.10-", "8.10", AccountKind.chequing) == Decimal("-8.10")

    def test_owed_money_reads_cr_as_a_credit_balance(self):
        assert signed_balance("50.00 Cr", "50.00", AccountKind.credit_card) == Decimal(
            "-50.00"
        )
        assert signed_balance(
            "950.00 Dr", "950.00", AccountKind.credit_card
        ) == Decimal("950.00")


class TestReconcile:
    def test_oldest_first_corrects_the_wrong_guesses(self):
        rows = [
            parsed("100.00", CREDIT, "1000.00", 1),
            parsed("450.00", CREDIT, "550.00", 2),  # balance fell: money out
            parsed("2000.00", DEBIT, "2550.00", 3),  # balance rose: money in
            parsed("50.00", CREDIT, "2500.00", 4),  # balance fell by 50: money out
        ]
        out, corrected = reconcile(rows, AccountKind.chequing)
        assert [row.direction for row in out] == [CREDIT, DEBIT, CREDIT, DEBIT]
        assert corrected == 3

    def test_newest_first_is_recognised(self):
        rows = [
            parsed("50.00", CREDIT, "500.00", 4),
            parsed("2000.00", DEBIT, "550.00", 3),
            parsed("450.00", CREDIT, "-1450.00", 2),
            parsed("100.00", CREDIT, "-1000.00", 1),
        ]
        out, corrected = reconcile(rows, AccountKind.chequing)
        # 500 <- 550: down 50 (out). 550 <- -1450: up 2000 (in). -1450 <- -1000:
        # down 450 (out). The last row has nothing older to compare with.
        assert [row.direction for row in out] == [DEBIT, CREDIT, DEBIT, CREDIT]
        assert corrected == 3

    def test_a_card_balance_rises_with_a_purchase(self):
        rows = [
            parsed("0.01", DEBIT, "1000.00", 1),
            parsed("86.40", CREDIT, "1086.40", 2),  # purchase read as income
            parsed("500.00", DEBIT, "586.40", 3),  # payment read as spending
        ]
        out, corrected = reconcile(rows, AccountKind.credit_card)
        assert [row.direction for row in out][1:] == [DEBIT, CREDIT]
        assert corrected == 2

    def test_a_card_that_prints_what_is_owed_as_negative(self):
        rows = [
            parsed("0.01", DEBIT, "-1000.00", 1),
            parsed("86.40", CREDIT, "-1086.40", 2),
            parsed("500.00", DEBIT, "-586.40", 3),
        ]
        out, _ = reconcile(rows, AccountKind.credit_card)
        assert [row.direction for row in out][1:] == [DEBIT, CREDIT]

    def test_what_does_not_add_up_is_left_alone(self):
        rows = [
            parsed("100.00", CREDIT, "1000.00", 1),
            parsed("450.00", CREDIT, "500.00", 2),  # a row is missing between
            parsed("20.00", CREDIT, None, 3),
        ]
        out, corrected = reconcile(rows, AccountKind.chequing)
        assert out == rows
        assert corrected == 0

    def test_nothing_is_done_without_the_account_type(self):
        rows = [
            parsed("100.00", CREDIT, "1000.00", 1),
            parsed("450.00", CREDIT, "550.00", 2),
        ]
        assert reconcile(rows, None) == (rows, 0)


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_balances_correct_a_statement_read_backwards(self):
        """The reported bug: every withdrawal came back as money in."""
        model = FakeModel(
            answer(
                item("450.00", "UPI-SWIGGY", "2026-08-14", "credit", "12,340.50"),
                item(
                    "52,000.00", "NEFT SALARY ACME", "2026-08-15", "credit", "64,340.50"
                ),
                item("2,000.00", "ATM WDL", "2026-08-16", "credit", "62,340.50"),
            )
        )
        outcome = await parse_statement(
            model, CHEQUING, "INR", account_kind=AccountKind.chequing
        )
        assert [row.direction for row in outcome.rows] == [CREDIT, CREDIT, DEBIT]

    @pytest.mark.asyncio
    async def test_a_balance_that_is_not_in_the_text_proves_nothing(self):
        model = FakeModel(
            answer(
                item("450.00", "UPI-SWIGGY", "2026-08-14", "credit", "12,340.50"),
                item(
                    "52,000.00", "NEFT SALARY ACME", "2026-08-15", "credit", "99,999.99"
                ),
            )
        )
        outcome = await parse_statement(
            model, CHEQUING, "INR", account_kind=AccountKind.chequing
        )
        assert outcome.rows[1].balance is None
        assert [row.direction for row in outcome.rows] == [CREDIT, CREDIT]
