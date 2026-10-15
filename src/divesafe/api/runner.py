"""Background assessment runs (ADR 0019).

Tasks live in a set this object owns, not in Starlette's `BackgroundTasks`, so shutdown can wait
for them, cancel them and mark them failed before the model provider closes. A run stores a result
only through the compare-and-set `complete`; every other ending stores a fixed failure code and
never exception text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from fastapi.concurrency import run_in_threadpool

from divesafe.domain import AssessmentRecord
from divesafe.orchestration import StartPassedError
from divesafe.services import RepositoryUnavailableError
from divesafe.services.runs import FailureCode, RunStore

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 300.0
CLEANUP_BATCH = 200


class RunManager:
    def __init__(self, store: RunStore, *, deadline: timedelta, grace: timedelta) -> None:
        self._store = store
        self._deadline = deadline
        self._grace = grace
        self._tasks: set[asyncio.Task[None]] = set()
        self._cleanup: asyncio.Task[None] | None = None
        self.closing = False

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def start(
        self,
        run_id: str,
        run_token: str,
        actor: str,
        work: Callable[[], Awaitable[AssessmentRecord]],
    ) -> None:
        if self.closing:
            raise RuntimeError("the server is shutting down")
        task = asyncio.get_running_loop().create_task(
            self._wrapped(run_id, run_token, actor, work), name=f"run-{run_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wrapped(
        self,
        run_id: str,
        run_token: str,
        actor: str,
        work: Callable[[], Awaitable[AssessmentRecord]],
    ) -> None:
        code: FailureCode | None = None
        try:
            async with asyncio.timeout(self._deadline.total_seconds()):
                record = await work()
            stored = await run_in_threadpool(self._store.complete, record, run_token)
            if stored:
                logger.info("assessment stored", extra={"assessment_id": run_id, "actor": actor})
                return
            # The lease ran out or the id was already finished: nothing was stored.
            logger.error("run lost its lease before completing", extra={"assessment_id": run_id})
            code = FailureCode.TIMEOUT
        except asyncio.CancelledError:
            code = FailureCode.SHUTDOWN
            raise
        except TimeoutError as exc:
            code = FailureCode.TIMEOUT
            self._log_failure(run_id, actor, exc)
        except StartPassedError as exc:
            code = FailureCode.START_PASSED
            self._log_failure(run_id, actor, exc)
        except RepositoryUnavailableError as exc:
            code = FailureCode.STORAGE
            self._log_failure(run_id, actor, exc)
        except BaseException as exc:  # a run must always end in a stored code
            code = FailureCode.INTERNAL
            self._log_failure(run_id, actor, exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            if code is not None:
                await self._mark_failed(run_id, run_token, code)

    @staticmethod
    def _log_failure(run_id: str, actor: str, exc: BaseException) -> None:
        logger.error(
            "run failed",
            extra={"assessment_id": run_id, "actor": actor, "error_type": type(exc).__name__},
        )

    async def _mark_failed(self, run_id: str, run_token: str, code: FailureCode) -> None:
        try:
            await asyncio.shield(run_in_threadpool(self._store.fail, run_id, run_token, code))
        except Exception as exc:  # the lease will expire and cleanup will mark it
            logger.error(
                "could not mark a run failed",
                extra={"assessment_id": run_id, "error_type": type(exc).__name__},
            )

    def start_cleanup(self, retention: timedelta) -> None:
        self._cleanup = asyncio.get_running_loop().create_task(self._cleanup_loop(retention))

    async def _cleanup_loop(self, retention: timedelta) -> None:
        while True:
            try:
                await run_in_threadpool(
                    lambda: self._store.cleanup(retention=retention, batch=CLEANUP_BATCH)
                )
            except Exception as exc:
                logger.error("run cleanup failed", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

    async def shutdown(self) -> None:
        """Refuse new runs, give the running ones a grace period, cancel the rest and wait until
        they have marked themselves failed."""
        self.closing = True
        if self._cleanup is not None:
            self._cleanup.cancel()
        pending = set(self._tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self._grace.total_seconds())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
