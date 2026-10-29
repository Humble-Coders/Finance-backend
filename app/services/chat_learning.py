"""Holding back advice while the coach is still learning (#72).

PRD F8: for roughly the first month, or until there is enough data, advanced
recommendations are withheld and the app says it is still learning. Facts
about the data that exists are fine from day one — "what did I spend in
September?" is a fact; "how much should I save?" is advice, and an answer
built on a week of transactions would be confidence nobody has earned.

The threshold is `learning_state` (`app/services/learning.py`), the one the
budget, the score and the dashboard already use. This module only decides
what kind of question it is, without spending a model call on it, and what to
say while advice is held back:

* `classify` — `fact`, `advice` or `unclear`, from the words of the question.
  An `advice` question while learning never reaches a model.
* `prescriptive` — whether a reply instructs anyway; such a reply, while
  learning, is replaced.

The split (proposal, for the Product Owner): facts, totals, lists,
comparisons and explanations are allowed — "explain my score" explains, so it
is a fact. Budgeting, saving, affording, plans and improving are held back.
"""

from __future__ import annotations

import re

from app.services.learning import LearningState

__all__ = [
    "ADVICE",
    "FACT",
    "LEARNING_INSTRUCTION",
    "UNCLEAR",
    "classify",
    "learning_payload",
    "learning_reply",
    "prescriptive",
]

FACT, ADVICE, UNCLEAR = "fact", "advice", "unclear"

_ADVICE = re.compile(
    r"\bshould i\b|\bshould we\b|\bcan i afford\b|\bcould i afford\b|\bafford\b"
    r"|\bhow much should\b|\bwhat should\b|\bis it (a good|wise|smart)\b"
    r"|\bplan (for|to)\b|\bmake a plan\b|\bimprove\b|\bget better\b"
    r"|\bincrease my score\b"
    r"|\binvest\b|\bsave more\b|\bspend less\b|\bcut (back|down)\b"
    r"|\bpay (off|down)\b.{0,30}\bfirst\b|\bwhich (debt|card|loan)\b"
    r"|\brecommend|\badvice\b|\badvise\b|\bsuggest\b|\btips?\b"
    r"|\bhelp me (save|budget)\b"
    r"|\bset (a|my) budget\b|\bhow (do|can) i (save|budget|reach|get)\b",
    re.IGNORECASE,
)
_FACT = re.compile(
    r"\bhow much (did|do|have) (i|we)\b|\bwhat did (i|we)\b|\bwhat('s| is| was| were)\b"
    r"|\bshow( me)?\b|\blist\b|\bcompare|\bbiggest\b|\blargest\b|\btop\b"
    r"|\bhow many\b|\bwhen did\b|\bwhere did\b|\bexplain\b|\bwhy (did|is|was)\b"
    r"|\btotal\b|\baverage\b",
    re.IGNORECASE,
)
_PRESCRIPTIVE = re.compile(
    r"\byou should\b|\byou'?d (better|do well)\b|\bi (would |'d )?recommend\b"
    r"|\bi'?d suggest\b|\bi suggest\b|\bmy advice\b"
    r"|\bconsider (saving|cutting|paying|reducing|setting|moving|putting)\b"
    r"|\btry to\b|\baim (to|for)\b|\byou need to\b|\bmake sure you\b"
    r"|\bit would be (wise|best|a good idea)\b",
    re.IGNORECASE,
)

LEARNING_INSTRUCTION = (
    "This household is still learning: it has too little history for advice. "
    "Answer with facts about the data only. Give no advice, recommendations, "
    "budgets, plans or projections; if asked for one, say it comes once there "
    "is a full month of history."
)


def classify(question: str) -> str:
    """`advice` wins over `fact`: "what should I budget?" holds both."""
    if _ADVICE.search(question):
        return ADVICE
    if _FACT.search(question):
        return FACT
    return UNCLEAR


def prescriptive(reply: str) -> bool:
    return bool(_PRESCRIPTIVE.search(reply))


def learning_payload(state: LearningState) -> dict:
    """The progress, in the shape the budget and score return it
    (`LearningOut`), so the app reads one shape everywhere."""
    return {
        "ready": state.ready,
        "complete_months": state.complete_months,
        "transactions": state.transactions,
        "needs": dict(state.needs),
    }


def learning_reply(state: LearningState) -> str:
    months, rows = state.needs["complete_months"], state.needs["transactions"]
    have_months = min(state.complete_months, months)
    have_rows = min(state.transactions, rows)
    return (
        "I'm still learning your money, so I can't give advice yet — I can tell "
        "you what you've spent, earned and saved so far. Advice starts once I "
        f"have {months} full month{'s' if months != 1 else ''} and {rows} "
        f"transactions: {have_months} of {months} full month"
        f"{'s' if months != 1 else ''} · {have_rows} of {rows} transactions so far."
    )
