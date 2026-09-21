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
from app.services.statements import ParseOutcome, TooManyRowsError, parse_statement

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


class TestTheYearSurvivesChunking:
    """The defect four review passes missed, because it lived in the prompt.

    Statements print `14 Aug` on every line and the year exactly once, in the
    period header. A window is a slice of the text, so only the first one had
    it — and the prompt's own rule is to omit a row rather than guess a year.
    Three quarters of a long statement would simply be absent: not rejected,
    not counted, reported as a clean import.
    """

    HEADER = "ROYAL BANK OF CANADA\nStatement period: 1 Aug 2026 to 31 Aug 2026"

    def statement(self) -> str:
        body = [
            f"{day:02d} Aug   TIM HORTONS #4821 {day:04d}          12.40"
            for day in range(1, 900)
        ]
        return "\n".join([self.HEADER, ""] + body)

    @pytest.mark.asyncio
    async def test_the_period_from_the_device_reaches_every_window(self):
        """The mechanism, as opposed to the fallback.

        Ticket 3.2's redactor drops the block above the first transaction on
        page 1 — the same block a scraped header comes from, by the same
        definition — so by the time text reaches us there is usually no header
        left to scrape. The device reads the period before it redacts and sends
        it as a field; every window, the first included, is told.
        """
        model = FakeModel()
        redacted = "\n".join(
            f"{day:02d} Aug   TIM HORTONS #4821 {day:04d}          12.40"
            for day in range(1, 900)
        )

        await parse_statement(
            model, redacted, CURRENCY, (date(2026, 8, 1), date(2026, 8, 31))
        )

        assert len(model.prompts) > 1
        assert all("2026-08-01" in prompt for prompt in model.prompts)

    @pytest.mark.asyncio
    async def test_redacted_text_with_no_period_has_no_year_anywhere(self):
        """What the previous fix silently did against a real client.

        Kept as a record: with the header gone and no period field, nothing in
        the text says which year this is, and the prompt tells the model to omit
        every row rather than guess.
        """
        model = FakeModel()
        redacted = "14 Aug   TIM HORTONS #4821          12.40"

        await parse_statement(model, redacted, CURRENCY)

        assert all("2026" not in prompt for prompt in model.prompts)

    @pytest.mark.asyncio
    async def test_every_window_after_the_first_is_given_the_year(self):
        model = FakeModel()

        await parse_statement(model, self.statement(), CURRENCY)

        assert len(model.prompts) > 1, "this statement should have been split"
        assert all("2026" in prompt for prompt in model.prompts)

    @pytest.mark.asyncio
    async def test_the_carried_header_does_not_widen_the_amount_check(self):
        """The header is added to the prompt, never to the text an amount is
        checked against.

        Otherwise carrying it into every window would quietly weaken the guard
        everywhere: an opening balance printed once could vouch for a figure the
        model invented three windows later. The first window is a different
        case — the header really is part of that text, and a balance really is
        printed on the statement. A substring check cannot tell a balance from a
        purchase, and never could; what it can do is refuse a number that is not
        on the page at all.
        """

        class OnlyLaterWindows(FakeModel):
            async def complete(self, *, system, user, max_output_tokens):
                self.prompts.append(user)
                if "STATEMENT HEADER" not in user:
                    return "[]"
                return rows(row("4321.00", "NOT A REAL ROW", "2026-08-14"))

        model = OnlyLaterWindows()

        outcome = await parse_statement(model, self.statement(), CURRENCY)

        assert len(model.prompts) > 1
        assert any("STATEMENT HEADER" in p for p in model.prompts)
        assert outcome.rows == [], "the header vouched for an invented amount"

    def test_the_header_stops_at_the_first_transaction(self):
        header = statements._header(self.statement().splitlines())

        assert "Statement period" in header
        assert "TIM HORTONS" not in header


class TestGenuineDuplicates:
    """The failure the naive dedup causes, and the reason for the per-window count.

    Two identical transactions on one statement look exactly like one row seen
    through two overlapping windows. Collapsing them deletes real spending and
    reports nothing — the worst shape of bug this product can have, because the
    number it produces is confident and wrong.
    """

    @pytest.mark.asyncio
    async def test_two_identical_transactions_both_survive(self):
        statement = "\n".join(
            [
                "2026-08-14  TIM HORTONS #4821             5.00",
                "2026-08-14  TIM HORTONS #4821             5.00",
            ]
        )
        coffee = row("5.00", "TIM HORTONS", "2026-08-14")
        model = FakeModel(rows(coffee, coffee))

        outcome = await parse_statement(model, statement, CURRENCY)

        assert len(outcome.rows) == 2, "a real second coffee is not a duplicate"

    @pytest.mark.asyncio
    async def test_a_repeat_is_kept_while_the_seam_duplicate_is_dropped(
        self, monkeypatch
    ):
        """Both behaviours at once, which is the only way to prove the rule is
        'maximum per window' rather than 'always one' or 'always all'."""
        monkeypatch.setattr(statements, "CHUNK_CHARS", 120)
        monkeypatch.setattr(statements, "OVERLAP_LINES", 3)
        statement = "\n".join(
            f"2026-08-{day:02d}  MERCHANT {day}   {day}.50" for day in range(1, 21)
        )
        twice = row("5.50", "MERCHANT 5", "2026-08-05")
        # Every window reports the same pair, exactly as two real transactions
        # inside one window would.
        model = FakeModel(*[rows(twice, twice)] * 20)

        outcome = await parse_statement(model, statement, CURRENCY)

        assert len(outcome.rows) == 2


class TestOcrShapedInput:
    """OCR returns blocks, not one line per transaction.

    The obvious windowing — step back a fixed number of lines — is right for a
    PDF text layer with hundreds of short lines and badly wrong for four long
    OCR blocks, where it fails to advance and re-sends almost the whole window.
    This is the input class the on-device decision made more common, so it is
    the one worth pinning.
    """

    def test_long_lines_do_not_multiply_the_windows(self):
        statement = "\n".join(["x" * 3000] * 60)

        windows = statements._windows(statement)
        sent = sum(len(w) for w in windows)

        assert len(windows) <= statements.MAX_WINDOWS, "would be refused outright"
        assert sent < len(statement) * 1.6, "billing multiples of the statement"

    def test_short_lines_still_barely_overlap(self):
        statement = "\n".join(["x" * 60] * 400)

        sent = sum(len(w) for w in statements._windows(statement))

        assert sent < len(statement) * 1.2

    def test_one_enormous_line_is_split_rather_than_sent_whole(self):
        """A badly-segmented scan can return a whole statement as one line."""
        windows = statements._windows("y" * 150_000)

        assert len(windows) > 1
        assert all(len(w) <= statements.CHUNK_CHARS for w in windows)

    def test_a_transaction_across_a_character_split_survives_whole(self):
        """The splitting fix's own failure mode, pinned.

        Cutting at a raw offset leaves each piece filling a window on its own,
        so the line-based overlap carries nothing — and a transaction on the
        boundary is in no window at all. Not rejected, not counted: never shown
        to the model, and silently missing from the person's spending.
        """
        blob = ("A" * 11_980) + "2026-08-14  TIM HORTONS  12.40" + ("B" * 40_000)

        windows = statements._windows(blob)

        assert any("TIM HORTONS  12.40" in w for w in windows)


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
    async def test_a_statement_needing_too_many_passes_is_refused_before_any_call(
        self, monkeypatch
    ):
        """Unbounded windows meant an unbounded request: 84 sequential calls at a
        60-second timeout is over an hour, billed the whole way, long after the
        client gave up."""
        monkeypatch.setattr(statements, "CHUNK_CHARS", 60)
        monkeypatch.setattr(statements, "MAX_WINDOWS", 2)
        statement = "\n".join(
            f"2026-08-{day:02d}  MERCHANT {day}   {day}.50" for day in range(1, 21)
        )
        model = FakeModel()

        with pytest.raises(LlmError, match="passes"):
            await parse_statement(model, statement, CURRENCY)

        assert model.prompts == [], "nothing should have been sent"

    @pytest.mark.asyncio
    async def test_the_time_budget_stops_a_parse_that_outlives_its_reader(
        self, monkeypatch
    ):
        monkeypatch.setattr(statements, "CHUNK_CHARS", 60)
        monkeypatch.setattr(statements, "PARSE_BUDGET_SECONDS", -1.0)
        statement = "\n".join(
            f"2026-08-{day:02d}  MERCHANT {day}   {day}.50" for day in range(1, 6)
        )

        with pytest.raises(LlmError, match="time budget"):
            await parse_statement(FakeModel(), statement, CURRENCY)

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

        with pytest.raises(TooManyRowsError):
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
