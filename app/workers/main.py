"""Background worker entrypoint.

Started by Render as:
    python -m app.workers.main

Consumes the Supabase queue (pgmq) and runs the extraction pipeline (PRD F2):
    document AI extraction -> redaction -> LLM normalization -> dedup
    -> categorization -> review queue -> source deletion

This work lives here rather than in a serverless function because it is
long-running and retry-heavy; a wall-clock timeout mid-pipeline would leave a
half-imported statement in someone's financial records (PRD §4.2).

SKELETON: the loop, shutdown handling and failure semantics are real; the
pipeline stages are not yet implemented.
"""

import asyncio
import signal
from contextlib import suppress

import structlog
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal

log = structlog.get_logger()

POLL_IDLE_SECONDS = 5
VISIBILITY_TIMEOUT_SECONDS = 600  # generous: extraction is slow by nature


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """Finish the in-flight job, then exit. Render sends SIGTERM on deploy."""
        log.info("shutdown_requested")
        self._stopping.set()

    async def run(self) -> None:
        log.info("worker_started", queue=self.settings.extraction_queue_name)
        while not self._stopping.is_set():
            try:
                handled = await self._process_one()
            except Exception:
                # Never let one bad job kill the worker; the message stays
                # invisible until its timeout, then redelivers.
                log.exception("job_failed")
                handled = False

            if not handled:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=POLL_IDLE_SECONDS
                    )
        log.info("worker_stopped")

    async def _process_one(self) -> bool:
        """Pop one message; return True if work was done."""
        async with SessionLocal() as session:
            result = await session.execute(
                text("SELECT * FROM pgmq.read(:queue, :vt, 1)"),
                {
                    "queue": self.settings.extraction_queue_name,
                    "vt": VISIBILITY_TIMEOUT_SECONDS,
                },
            )
            message = result.mappings().first()
            if message is None:
                return False

            log.info("job_received", msg_id=message["msg_id"])

            # TODO: run the F2 extraction pipeline here.

            await session.execute(
                text("SELECT pgmq.archive(:queue, :msg_id)"),
                {
                    "queue": self.settings.extraction_queue_name,
                    "msg_id": message["msg_id"],
                },
            )
            await session.commit()
            log.info("job_archived", msg_id=message["msg_id"])
            return True


async def _main() -> None:
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
