"""Turning redacted statement text into transaction rows.

The device sends text; this makes rows out of it. Three things here are load
bearing, and all three exist because a model is doing the reading.

**The amount must already be in the text.** Every amount a row claims is checked
against the text that was submitted, verbatim. A model asked to read a table
will occasionally produce a number that was never on it — plausible, correctly
formatted, and wrong. In a product that tells people what they spent, a
fabricated figure is the worst possible output, worse than no row at all, so a
row whose amount cannot be found is dropped and counted rather than returned.

**Long statements are split, and the seams overlap.** A window boundary falling
mid-table would otherwise lose the row it lands on. Overlap means a row can be
returned twice, so the merge is part of the design rather than an afterthought.

**Nothing here logs its input.** Every string passing through this module is
somebody's statement.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date

import structlog

from app.core.money import MoneyError, normalize
from app.models.enums import TransactionDirection
from app.services.llm import LlmClient, LlmError

__all__ = [
    "ParsedRow",
    "ParseOutcome",
    "parse_statement",
    "MAX_TEXT_CHARS",
    "MAX_ROWS",
]

log = structlog.get_logger()

# A statement this long is not a statement. The caps exist so one request cannot
# spend an unbounded amount of somebody's money on tokens — and, just as much,
# so it cannot run for longer than anyone is still listening. 200k characters is
# roughly a 70-page statement.
MAX_TEXT_CHARS = 200_000
MAX_ROWS = 2_000
# Windows are read one after another, so without a ceiling a large statement is
# an unbounded number of sequential 60-second calls: over an hour in one HTTP
# request, billed the whole way, long after the client gave up and every proxy
# in between dropped the connection.
MAX_WINDOWS = 20
# The budget is checked between calls, so a parse can overshoot by at most one
# call. Exact enough for something whose purpose is "stop, nobody is waiting".
PARSE_BUDGET_SECONDS = 180.0

# Windows are sized in characters rather than tokens: an exact token count needs
# the provider's tokenizer, and being approximately right is enough when the
# overlap covers the error.
CHUNK_CHARS = 12_000
OVERLAP_LINES = 6

_SYSTEM_PROMPT = """\
You read bank and credit-card statements and return the transactions in them.

Return ONLY a JSON array. Each element:
  {"date": "YYYY-MM-DD", "description": "...", "amount": "123.45",
   "direction": "debit" | "credit", "confidence": 0-100}

Rules:
- Copy every amount EXACTLY as it appears in the text. Never round, never
  reformat, never calculate one.
- "debit" is money leaving the account, "credit" is money arriving. A credit-card
  statement's purchases are debits.
- Ignore running balances, subtotals, totals, interest summaries and page
  headers. Only individual transactions.
- If the year is not on a line, take it from the statement period. If you cannot
  tell, omit the row rather than guessing a year.
- confidence is how sure you are of THAT row: 90+ for a clean tabular line,
  below 60 when you are inferring.
- If there are no transactions, return [].
"""

_PROMPT_VERSION = "2026-09-21.1"


@dataclass(frozen=True)
class ParsedRow:
    occurred_on: date
    description: str
    # A decimal string, always. Never a float, never minor units — this crosses
    # into the API layer where the money contract is decimal strings (PRD §4.4).
    amount: str
    direction: TransactionDirection
    confidence: int


@dataclass(frozen=True)
class ParseOutcome:
    rows: list[ParsedRow]
    # Lines the model offered that did not survive validation. Reported, not
    # hidden: it is the difference between "your statement had 3 transactions"
    # and "we could only read 3 of them".
    unparsed_line_count: int
    model: str
    prompt_version: str = _PROMPT_VERSION


def _windows(text: str) -> list[str]:
    """Split into overlapping windows on line boundaries."""
    lines = text.splitlines()
    if not lines:
        return []

    windows: list[str] = []
    start = 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and size < CHUNK_CHARS:
            size += len(lines[end]) + 1
            end += 1
        windows.append("\n".join(lines[start:end]))
        if end >= len(lines):
            break
        # Step back a few lines so a transaction split by the boundary appears
        # whole in the next window. The duplicate this creates is removed later.
        start = max(end - OVERLAP_LINES, start + 1)
    return windows


def _amount_forms(amount: str) -> set[str]:
    """How this amount might be written on a statement.

    Only formatting variants, never a different number. Widening this to "close
    enough" would defeat the check it exists for.
    """
    forms = {amount}
    if "." in amount:
        whole, _, fraction = amount.partition(".")
        grouped = f"{int(whole):,}" if whole.lstrip("-").isdigit() else whole
        forms.add(f"{grouped}.{fraction}")
    return forms


def _appears_verbatim(amount: str, haystack: str) -> bool:
    """Whether the model's amount is actually in the text it was given."""
    return any(form in haystack for form in _amount_forms(amount))


def _coerce(raw: object, window: str, currency: str) -> ParsedRow | None:
    """One model-produced row, or None if it cannot be trusted.

    Every rejection here is silent by design — the caller counts them. A row
    that fails any check is not a row we half-keep.
    """
    if not isinstance(raw, dict):
        return None
    try:
        occurred_on = date.fromisoformat(str(raw["date"]))
        amount = normalize(str(raw["amount"]), currency)
        direction = TransactionDirection(str(raw["direction"]))
        description = str(raw["description"]).strip()
    except (KeyError, ValueError, MoneyError):
        return None

    if not description:
        return None
    # The check this module exists for. `normalize` was applied above, so compare
    # against what the model actually wrote as well as its canonical form.
    if not (
        _appears_verbatim(amount, window)
        or _appears_verbatim(str(raw["amount"]).strip(), window)
    ):
        return None

    confidence = raw.get("confidence", 50)
    confidence = confidence if isinstance(confidence, int) else 50
    return ParsedRow(
        occurred_on=occurred_on,
        description=description,
        amount=amount,
        direction=direction,
        confidence=max(0, min(100, confidence)),
    )


def _rows_from(answer: str, window: str, currency: str) -> tuple[list[ParsedRow], int]:
    """Parse one model answer, returning the good rows and the rejected count."""
    text = answer.strip()
    # Models wrap JSON in fences however firmly you ask them not to.
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        payload = json.loads(text)
    except ValueError:
        log.warning("statement_answer_not_json")
        return [], 0
    if not isinstance(payload, list):
        return [], 0

    rows, rejected = [], 0
    for item in payload:
        row = _coerce(item, window, currency)
        if row is None:
            rejected += 1
        else:
            rows.append(row)
    return rows, rejected


def _key(row: ParsedRow) -> tuple[date, str, str]:
    return (row.occurred_on, row.amount, " ".join(row.description.lower().split()))


def _merge(per_window: list[list[ParsedRow]]) -> list[ParsedRow]:
    """Remove the duplicates the overlap creates — and nothing else.

    The naive version of this, a set over every row, is wrong in a way that is
    invisible until it costs someone money: two genuinely identical transactions
    on one statement — two $5.00 coffees at the same shop on the same day, two
    $2.50 fares, two $20 withdrawals — look exactly like one row seen through
    two overlapping windows. Deduplicating globally deletes one of them, reports
    nothing, and understates what the person spent.

    So count per window and keep the **maximum**, never the union. A row on a
    seam appears once in each of two windows, so the maximum is one. Two real
    coffees appear twice in every window that contains that region, so the
    maximum is two. The only way to see a count of N is for some single window
    to have read N of them.
    """
    counts: dict[tuple[date, str, str], int] = {}
    first: dict[tuple[date, str, str], ParsedRow] = {}
    order: list[tuple[date, str, str]] = []

    for window_rows in per_window:
        for key, count in Counter(_key(row) for row in window_rows).items():
            if key not in counts:
                order.append(key)
                first[key] = next(r for r in window_rows if _key(r) == key)
            counts[key] = max(counts[key], count) if key in counts else count

    return [row for key in order for row in [first[key]] * counts[key]]


async def parse_statement(client: LlmClient, text: str, currency: str) -> ParseOutcome:
    """Read a whole statement, however many model calls that takes.

    Raises `LlmError` if the model fails: the caller marks the import failed and
    tells the user, who still has the file and can simply try again. That is the
    whole reason a failed import is not a problem here — nothing was half-saved,
    because the source of truth never left their device.
    """
    windows = _windows(text)
    if len(windows) > MAX_WINDOWS:
        raise LlmError(
            f"statement needs {len(windows)} passes, "
            f"more than the {MAX_WINDOWS} allowed"
        )

    started = time.monotonic()
    per_window: list[list[ParsedRow]] = []
    rejected = 0
    total_rows = 0

    for window in windows:
        if time.monotonic() - started > PARSE_BUDGET_SECONDS:
            raise LlmError("statement took longer to read than the time budget allows")
        answer = await client.complete(
            system=_SYSTEM_PROMPT,
            user=window,
            # Roughly four times the window's line count in tokens; JSON rows are
            # much smaller than the text they came from.
            max_output_tokens=8_000,
        )
        window_rows, window_rejected = _rows_from(answer, window, currency)
        per_window.append(window_rows)
        rejected += window_rejected
        total_rows += len(window_rows)
        if total_rows > MAX_ROWS:
            raise LlmError("statement produced more rows than a statement can have")

    merged = _merge(per_window)
    log.info(
        "statement_parsed",
        model=client.model,
        prompt_version=_PROMPT_VERSION,
        windows=len(windows),
        rows=len(merged),
        rejected=rejected,
        seconds=round(time.monotonic() - started, 1),
    )
    return ParseOutcome(rows=merged, unparsed_line_count=rejected, model=client.model)
