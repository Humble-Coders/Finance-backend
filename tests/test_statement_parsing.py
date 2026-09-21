"""Reading a statement: the guards, not the model.

The model is faked throughout. What is worth testing is everything around it —
the check that an amount was really on the statement, the window overlap, and
the fact that none of this ever logs what it read.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
import structlog

from app.core.money import MoneyError
from app.models.enums import TransactionDirection
from app.services import statements
from app.services.llm import LlmError
from app.services.statements import ParseOutcome, parse_statement

CURRENCY = "CAD"


class FakeModel:
    """Answers with whatever the test hands it, once per window."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "fake-1"

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        self.prompts.append(user)
        return self._answers.pop(0) if self._answers else "[]"


def rows(*items: dict) -> str:
    return json.dumps(list(items))


def row(amount="12.40", description="TIM HORTONS", day="2026-08-14", direction="debit"):
    return {
        "date": day,
        "description": description,
        "amount": amount,
        "direction": direction,
        "confidence": 95,
    }


STATEMENT = "\n".join(
    [
        "Date        Description                 Amount",
        "2026-08-14  TIM HORTONS #4821            12.40",
        "2026-08-15  LOBLAWS 1234                134.02",
        "2026-08-16  PAYROLL DEPOSIT           2,410.00",
    ]
)


class TestItReadsWhatIsThere:
    @pytest.mark.asyncio
    async def test_returns_the_rows_the_model_found(self):
        model = FakeModel(rows(row(), row("134.02", "LOBLAWS 1234", "2026-08-15")))
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert [r.amount for r in outcome.rows] == ["12.40", "134.02"]
        assert outcome.rows[0].occurred_on == date(2026, 8, 14)
        assert outcome.rows[0].direction is TransactionDirection.debit
        assert outcome.model == "fake-1"
        assert outcome.unparsed_line_count == 0

    @pytest.mark.asyncio
    async def test_an_amount_written_with_a_thousands_separator_still_matches(self):
        """The model returns 2410.00; the statement shows 2,410.00."""
        model = FakeModel(
            rows(row("2410.00", "PAYROLL DEPOSIT", "2026-08-16", "credit"))
        )
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert [r.amount for r in outcome.rows] == ["2410.00"]

    @pytest.mark.asyncio
    async def test_json_wrapped_in_a_code_fence_is_still_json(self):
        model = FakeModel("```json\n" + rows(row()) + "\n```")
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert len(outcome.rows) == 1


class TestItRefusesWhatIsNotThere:
    """The reason this module exists.

    A model reading a table will occasionally produce a number that was never on
    it — correctly formatted, plausible, and wrong. In a product that tells
    people what they spent, that is the worst output there is.
    """

    @pytest.mark.asyncio
    async def test_an_invented_amount_is_dropped_and_counted(self):
        model = FakeModel(rows(row(), row("99.99", "SOMETHING ELSE")))
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert [r.amount for r in outcome.rows] == ["12.40"]
        assert outcome.unparsed_line_count == 1

    @pytest.mark.asyncio
    async def test_a_row_with_an_unreadable_date_is_dropped(self):
        model = FakeModel(rows(row(day="last Tuesday")))
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert outcome.rows == []
        assert outcome.unparsed_line_count == 1

    @pytest.mark.asyncio
    async def test_an_amount_that_is_not_money_is_dropped(self):
        model = FakeModel(rows(row(amount="twelve forty")))
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert outcome.rows == []

    @pytest.mark.asyncio
    async def test_an_answer_that_is_not_json_yields_nothing_rather_than_raising(self):
        model = FakeModel("I'm sorry, I can't help with that.")
        outcome = await parse_statement(model, STATEMENT, CURRENCY)

        assert outcome == ParseOutcome(rows=[], unparsed_line_count=0, model="fake-1")


class TestLongStatements:
    @pytest.mark.asyncio
    async def test_a_transaction_on_the_seam_is_returned_once(self, monkeypatch):
        """Windows overlap so a boundary cannot swallow a row; the overlap then
        offers the same row twice, and the merge is what stops it being saved
        twice."""
        monkeypatch.setattr(statements, "CHUNK_CHARS", 120)
        monkeypatch.setattr(statements, "OVERLAP_LINES", 3)

        long_statement = "\n".join(
            f"2026-08-{day:02d}  MERCHANT {day}   {day}.50" for day in range(1, 21)
        )
        seam = {
            "date": "2026-08-05",
            "description": "MERCHANT 5",
            "amount": "5.50",
            "direction": "debit",
            "confidence": 90,
        }
        # Every window claims the same seam row.
        model = FakeModel(*[rows(seam)] * 20)

        outcome = await parse_statement(model, long_statement, CURRENCY)

        assert len(model.prompts) > 1, "the statement should have been split"
        assert len(outcome.rows) == 1

    @pytest.mark.asyncio
    async def test_an_absurd_number_of_rows_stops_rather_than_billing_forever(
        self, monkeypatch
    ):
        monkeypatch.setattr(statements, "CHUNK_CHARS", 120)
        monkeypatch.setattr(statements, "MAX_ROWS", 5)
        long_statement = "\n".join(
            f"2026-08-{day:02d}  MERCHANT {day}   {day}.50" for day in range(1, 21)
        )
        many = rows(
            *[row(f"{n}.50", f"MERCHANT {n}", f"2026-08-{n:02d}") for n in range(1, 10)]
        )
        model = FakeModel(*[many] * 20)

        with pytest.raises(LlmError):
            await parse_statement(model, long_statement, CURRENCY)


class TestItNeverLogsTheStatement:
    @pytest.mark.asyncio
    async def test_no_statement_content_reaches_the_log(self):
        model = FakeModel(rows(row()))

        with structlog.testing.capture_logs() as logs:
            await parse_statement(model, STATEMENT, CURRENCY)

        rendered = repr(logs)
        assert "TIM HORTONS" not in rendered
        assert "12.40" not in rendered
        assert "LOBLAWS" not in rendered


def test_money_errors_are_not_swallowed_silently_elsewhere():
    """A guard on an assumption this module makes: `normalize` raises, not returns."""
    with pytest.raises(MoneyError):
        from app.core.money import normalize

        normalize("not money", CURRENCY)
