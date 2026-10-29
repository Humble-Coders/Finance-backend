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

# Asking for advice outright, whatever the tense.
_STRONG_ADVICE = re.compile(
    r"\bshould (i|we)\b|\bhow much should\b|\bwhat should\b"
    r"|\b(can|could) (i|we) afford\b|\brecommend|\badvice\b|\badvise\b|\btips?\b"
    r"|\bhelp me (save|budget|plan)\b|\bset (a|my) budget\b|\bmake a plan\b"
    r"|\bis it (a good|wise|smart|worth)\b"
    r"|\bhow (do|can|could|should) (i|we) (save|budget|reach|get|improve|pay|cut)\b"
    r"|\bhow to (save|budget|improve|pay|cut)\b",
    re.IGNORECASE,
)
# Words that are advice about what to do next — but a fact when the question
# looks back: "how much did I invest?", "did my score improve?".
_WEAK_ADVICE = re.compile(
    r"\bafford\b|\bplan (for|to)\b|\bimprove\b|\bget better\b|\bincrease my score\b"
    r"|\binvest\b|\bsave more\b|\bspend less\b|\bcut (back|down)\b"
    r"|\bpay (off|down)\b.{0,30}\bfirst\b|\bwhich (debt|card|loan)\b|\bsuggest\b",
    re.IGNORECASE,
)
# A question about what already happened.
_PAST = re.compile(
    r"\b(did|was|were|had)\b|\bhave (i|we)\b|\bhas (my|our|the)\b",
    re.IGNORECASE,
)
_FACT = re.compile(
    r"\bhow much (did|do|have) (i|we)\b|\bwhat did (i|we)\b"
    r"|\bwhat('s| is| was| were)\b"
    r"|\bshow( me)?\b|\blist\b|\bcompare|\bbiggest\b|\blargest\b|\btop\b"
    r"|\bhow many\b|\bwhen did\b|\bwhere did\b|\bexplain\b|\bwhy (did|is|was)\b"
    r"|\btotal\b|\baverage\b",
    re.IGNORECASE,
)
# What a person does with money — so "try to" or "make sure you" is advice
# only when it is about money, not "try to look at the Transactions screen".
_MONEY_ACTION = (
    r"(sav\w*|spend\w*|cut\w*|keep\w*|pay\w*|reduc\w*|limit\w*|budget\w*|"
    r"put\w*|set\w* aside|mov\w*|transfer\w*|invest\w*|stop\w*|cancel\w*|"
    r"lower\w*|increas\w*|build\w*)"
)
_PRESCRIPTIVE = re.compile(
    r"\byou should\b|\byou'?d (better|do well)\b|\bi (would |'d )?recommend\b"
    r"|\bi'?d suggest\b|\bi suggest\b|\bmy advice\b"
    r"|\bconsider (saving|cutting|paying|reducing|setting|moving|putting)\b"
    rf"|\b(try to|aim to|you need to|make sure you|be sure to)\s+(\w+\s+){{0,2}}?"
    rf"{_MONEY_ACTION}\b"
    r"|\baim (to|for)\s+(\$|\d)"
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
    """Asking outright for advice is advice ("what should I budget?" holds a
    fact word too). A word about what to do next is advice only when the
    question does not look back: "should I invest?" is advice, "how much did
    I invest?" a fact."""
    if _STRONG_ADVICE.search(question):
        return ADVICE
    looks_back = bool(_PAST.search(question))
    if _WEAK_ADVICE.search(question) and not looks_back:
        return ADVICE
    if _FACT.search(question) or looks_back:
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
