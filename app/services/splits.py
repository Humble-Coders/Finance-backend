"""Split expense: dividing a bill exactly, and reading a receipt.

**The shares are the server's** (`CLAUDE.md` → Financial math). The apps show a
preview while the person is choosing, worked out by shared
`SplitMath` the same way; what is saved, and what goes in the WhatsApp
message, is what this module works out. Both are integer minor units and add
up to the bill exactly, to the minor unit.

* **Equally:** the total over the people; the minor units left over go one
  each to the first people in the list — the person themselves first.
* **By items:** each item shared evenly by the people it is assigned to, the
  same way; then tax and tip follow what each person had, in proportion to
  their items. The minor units that proportion leaves over go to whoever's
  share was rounded down the most, ties to the earlier person.

**A receipt** is read on the phone, its card numbers and contact details
masked there; only that text comes here. The model returns the items, tax,
tip and total, and every amount it claims must be printed in that text
(`statements._appears_verbatim`) — a figure it made up never reaches a bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog

from app.core.money import MoneyError, normalize
from app.services.ai_budget import _answer
from app.services.llm import LlmClient
from app.services.statements import _appears_verbatim, _bare, _ungrouped

log = structlog.get_logger()

__all__ = [
    "MAX_RECEIPT_CHARS",
    "MAX_RECEIPT_READS",
    "ReceiptItem",
    "ReceiptRead",
    "equal_shares",
    "item_shares",
    "read_receipt",
]

PROMPT_VERSION = "2026-11-04.1"
# Receipts read by the AI a month, per household: the cost guard.
MAX_RECEIPT_READS = 40
MAX_RECEIPT_CHARS = 8_000
MAX_OUTPUT_TOKENS = 1_500
MAX_ITEMS = 60


def equal_shares(total: int, people: int) -> list[int]:
    """[total] over [people], the left-over minor units to the first ones."""
    if people <= 0:
        raise ValueError("a split needs someone to split with")
    base, left = divmod(total, people)
    return [base + 1 if i < left else base for i in range(people)]


def _in_proportion(amount: int, weights: list[int]) -> list[int]:
    """[amount] in proportion to [weights]; largest remainders get the
    left-over minor units, ties to the earlier. Evenly when no weight."""
    whole = sum(weights)
    if amount == 0:
        return [0] * len(weights)
    if whole <= 0:
        return equal_shares(amount, len(weights))
    raw = [amount * w for w in weights]
    shares = [r // whole for r in raw]
    left = amount - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] % whole), i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def item_shares(
    people: int, items: list[tuple[int, list[int]]], tax: int, tip: int
) -> list[int]:
    """Each person's share of an itemised bill. [items] are (amount, the
    indices of the people sharing it); tax and tip follow each person's items."""
    subtotals = [0] * people
    for amount, who in items:
        if not who:
            raise ValueError("an item belongs to someone")
        for person, share in zip(sorted(set(who)), equal_shares(amount, len(set(who)))):
            subtotals[person] += share
    extras = _in_proportion(tax + tip, subtotals)
    return [s + e for s, e in zip(subtotals, extras)]


# --- Reading a receipt ----------------------------------------------------------


@dataclass(frozen=True)
class ReceiptItem:
    name: str
    quantity: int
    # Decimal string, the line's total.
    amount: str


@dataclass(frozen=True)
class ReceiptRead:
    merchant: str | None
    occurred_on: date | None
    items: list[ReceiptItem]
    subtotal: str | None
    tax: str | None
    tip: str | None
    total: str | None
    # Lines the model offered that are not on the receipt.
    unreadable: int


_SYSTEM = """You read a shop or restaurant receipt and return what is on it.
Return ONLY a JSON object:
{"merchant": "<the shop's name, or null>",
 "date": "YYYY-MM-DD or null",
 "items": [{"name": "...", "quantity": 1, "amount": "12.50"}],
 "subtotal": "...", "tax": "...", "tip": "...", "total": "..."}
Rules:
- One element per line item the customer was charged for. "amount" is the
  line's total as printed (quantity times price when both are printed).
- Copy every amount's digits EXACTLY as printed, as a plain number with no
  currency symbol. Never round, never calculate one.
- "tax" is every tax, service charge and fee together, as printed; "tip" any
  tip or gratuity. null when the receipt does not print it.
- Never return payment lines, change, card details, discounts' running totals
  or loyalty points as items.
- If it is not a receipt, return {"items": []}."""


def _amount(raw: object, text: str, currency: str) -> str | None:
    """A model's amount, as a decimal string, only if it is printed in [text]."""
    if raw is None:
        return None
    printed = _bare(str(raw))
    if not printed or not _appears_verbatim(printed, text):
        return None
    try:
        return normalize(_ungrouped(printed), currency)
    except MoneyError:
        return None


def _date(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


async def read_receipt(client: LlmClient, text: str, currency: str) -> ReceiptRead:
    """The receipt's items and totals, each amount checked against [text].
    Raises `LlmError` when the model fails."""
    raw = await client.complete(
        system=_SYSTEM, user=text, max_output_tokens=MAX_OUTPUT_TOKENS
    )
    answer = _answer(raw)
    items: list[ReceiptItem] = []
    unreadable = 0
    for item in (answer.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(item, dict):
            unreadable += 1
            continue
        amount = _amount(item.get("amount"), text, currency)
        name = str(item.get("name") or "").strip()[:120]
        if amount is None or not name:
            unreadable += 1
            continue
        try:
            quantity = max(int(item.get("quantity") or 1), 1)
        except (TypeError, ValueError):
            quantity = 1
        items.append(ReceiptItem(name=name, quantity=quantity, amount=amount))
    merchant = answer.get("merchant")
    log.info(
        "receipt_read",
        prompt_version=PROMPT_VERSION,
        model=client.model,
        items=len(items),
        unreadable=unreadable,
    )
    return ReceiptRead(
        merchant=str(merchant).strip()[:120] if merchant else None,
        occurred_on=_date(answer.get("date")),
        items=items,
        subtotal=_amount(answer.get("subtotal"), text, currency),
        tax=_amount(answer.get("tax"), text, currency),
        tip=_amount(answer.get("tip"), text, currency),
        total=_amount(answer.get("total"), text, currency),
        unreadable=unreadable,
    )
