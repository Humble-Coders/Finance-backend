#!/usr/bin/env python3
"""Put a statement through the real parsing path and print what comes back.

Not a test — a way to answer the question nine review passes could not: does
the model actually read a Canadian bank statement? It calls the same
`parse_statement` the endpoint calls, so windowing, the verbatim-amount guard,
the overlap merge and the prompt are all the ones that ship.

    .venv/bin/python scripts/try_statement.py \
        tests/fixtures/rbc_chequing_2026_08.redacted.txt 2026-08-01 2026-08-31

The statement period is passed separately because that is how it arrives in
production: the device reads it before redacting, and the redactor removes the
block it came from (PRD F2, 2026-09-21).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

# Python puts the *script's* directory on sys.path, not the working directory,
# so `python scripts/try_statement.py` cannot see `app/`. Repo root first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services.llm import LlmError, build_client, close_client  # noqa: E402
from app.services.statements import parse_statement  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    text = open(sys.argv[1]).read()
    period = (
        (date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3]))
        if len(sys.argv) > 3
        else None
    )

    settings = get_settings()
    if not settings.llm_no_training_tier:
        print(
            "! LLM_NO_TRAINING_TIER is false — this key may train on what you "
            "send it.\n! Synthetic fixtures only. Never a real statement.\n"
        )

    try:
        client = build_client(settings)
    except LlmError as exc:
        # A hand-run script should say what to fix, not print a traceback at
        # somebody trying to answer a question about a bank statement.
        sys.exit(f"\n{exc}\nSet it in .env (it is gitignored; this repo is public).")

    try:
        outcome = await parse_statement(client, text, "CAD", period)
    finally:
        await close_client(client)

    print(f"model {outcome.model}  prompt {outcome.prompt_version}")
    print(f"{len(outcome.rows)} rows, {outcome.unparsed_line_count} rejected\n")
    print(f"{'date':<12}{'amount':>10}  {'dir':<7}description")
    print("-" * 78)
    for row in outcome.rows:
        print(
            f"{row.occurred_on.isoformat():<12}{row.amount:>10}  "
            f"{row.direction.value:<7}{row.description[:44]}"
            f"{'' if row.confidence >= 80 else f'   [confidence {row.confidence}]'}"
        )


if __name__ == "__main__":
    asyncio.run(main())
