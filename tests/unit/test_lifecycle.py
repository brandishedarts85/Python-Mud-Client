from __future__ import annotations

import asyncio

from lifecycle import Subscription, TaskOwner


def test_subscription_close_is_idempotent():
    closed = []
    subscription = Subscription(lambda: closed.append(True))

    subscription.close()
    subscription.close()

    assert closed == [True]
    assert subscription.closed is True


def test_task_owner_cancels_and_awaits_owned_tasks():
    async def scenario():
        owner = TaskOwner("test")
        cancelled = asyncio.Event()

        async def sleeper():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = owner.create(sleeper(), name="owned-sleeper")
        await asyncio.sleep(0)
        assert task in owner.tasks

        await owner.cancel_and_wait()

        assert cancelled.is_set()
        assert task.done()
        assert task not in owner.tasks

    asyncio.run(scenario())


def test_task_owner_never_cancels_cleanup_task_itself():
    async def scenario():
        owner = TaskOwner("test")
        current = asyncio.current_task()
        assert current is not None
        owner.adopt(current)

        await owner.cancel_and_wait()

        assert not current.cancelled()

    asyncio.run(scenario())
