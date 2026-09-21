"""The one place a 409 is recorded.

A conflict is the only refusal we send that is nobody's mistake: the request was
well-formed, and the account is in a state that will not accept it. On the phone
it reads as "Something went wrong", and until now the only record of *why* was
the response body — which lives on the user's device, not in our logs. Working
out which conflict a real user hit once took four days of Render logs and a
database session; it should take one grep.

Every 409 the API raises calls `log_conflict` first. The line carries the code
the client sees, a `reason` naming the branch that refused, and identifiers to
join on. **Never the phone number, email, address or token that caused it**
(CLAUDE.md → Privacy): the code and reason already say what happened, and the
user id says whose account to look at if someone asks.
"""

from __future__ import annotations

import structlog

__all__ = ["log_conflict"]

log = structlog.get_logger()

# One event name for every conflict, so `event=conflict` finds all of them
# whatever the code. `event` is structlog's own key for this first argument.
EVENT = "conflict"


def log_conflict(code: str, reason: str, **context: object) -> None:
    """Record a 409 that is about to be returned.

    `code` is what the client receives and routes on; `reason` is for us. The
    two are not the same thing: several different situations answer with one
    code on purpose — telling a caller which of two accounts holds a number
    would be a lookup service — and which one it was is the whole diagnostic
    value. Keep `reason` a stable identifier, not a sentence.

    At info: a conflict is a normal outcome, not a fault. It is the *rate* of
    one that means something.
    """
    log.info(EVENT, code=code, reason=reason, **context)
