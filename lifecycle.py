"""Small UI-neutral lifecycle primitives.

The client intentionally avoids a framework-sized task/subscription system.  These
helpers make ownership explicit while remaining usable by both the session core
and the Qt integration layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


logger = logging.getLogger("mudclient.lifecycle")


class Subscription:
    """An idempotent handle for one event subscription."""

    def __init__(self, close_fn: Callable[[], None]) -> None:
        self._close_fn: Callable[[], None] | None = close_fn

    @property
    def closed(self) -> bool:
        return self._close_fn is None

    def close(self) -> None:
        close_fn = self._close_fn
        if close_fn is None:
            return
        self._close_fn = None
        close_fn()

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


class TaskOwner:
    """Own asyncio tasks and provide one deterministic cancellation/await path.

    A task may also have a named reference in its owner when policy needs one
    (for example the session reconnect task).  That does not change ownership:
    every task created/adopted here is still retired through this object.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(self._tasks)

    def create(self, awaitable: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        return self.adopt(task)

    def adopt(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(
                "owned task failed (%s / %s)",
                self.label,
                task.get_name(),
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def cancel_and_wait(
        self,
        *,
        extra: Iterable[asyncio.Task[Any] | None] = (),
        exclude: Iterable[asyncio.Task[Any] | None] = (),
    ) -> None:
        """Cancel all unfinished owned/extra tasks and await their completion.

        ``extra`` lets an owner retire older/manual references that predate this
        helper or were installed by tests.  The current task is always excluded
        so an owner cannot indirectly cancel the cleanup operation performing
        the retirement.
        """
        current = asyncio.current_task()
        excluded = {task for task in exclude if task is not None}
        if current is not None:
            excluded.add(current)

        candidates = set(self._tasks)
        candidates.update(task for task in extra if task is not None)
        tasks = [
            task
            for task in candidates
            if task not in excluded and not task.done()
        ]

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
