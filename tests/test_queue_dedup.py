"""Tests for FairQueue — round-robin fairness, dedup, eviction, bound."""

from __future__ import annotations

import asyncio

import pytest

from mailtriage.queue import FairQueue, Job, QueueFullError


def _job(user="u1", mid="m1") -> Job:
    return Job(user_id=user, message_id=mid, notification={})


@pytest.mark.asyncio
async def test_basic_put_get():
    q = FairQueue()
    await q.put(_job())
    j = await q.get()
    assert j.message_id == "m1"


@pytest.mark.asyncio
async def test_dedup_blocks_duplicate_inflight():
    """Two notifications for the same (user, msg) should result in one job;
    the second is silently dropped while the first is in-flight."""
    q = FairQueue()
    await q.put(_job())
    await q.put(_job())          # duplicate while in queue
    j = await q.get()
    await q.put(_job())          # duplicate while in flight
    await q.done(j)
    # After done(), the next put for the same key is allowed again.
    await q.put(_job())
    j2 = await q.get()
    assert j2.message_id == "m1"


@pytest.mark.asyncio
async def test_round_robin_fairness_between_users():
    """Strict equality on the order: u1's first job pops first (it was
    enqueued first), then u2 (round-robin rotation), then u1's second.
    The previous version of this assertion had an `or` clause that was
    always true — it would have passed even if the queue drained u1
    completely first."""
    q = FairQueue()
    await q.put(_job("u1", "a"))
    await q.put(_job("u1", "b"))
    await q.put(_job("u2", "c"))
    j1 = await q.get()
    j2 = await q.get()
    j3 = await q.get()
    assert [j1.user_id, j2.user_id, j3.user_id] == ["u1", "u2", "u1"]
    assert [j1.message_id, j2.message_id, j3.message_id] == ["a", "c", "b"]


@pytest.mark.asyncio
async def test_empty_subqueue_evicted():
    """After the last job for a user is taken, the per-user deque + order
    entry should be released so memory does not grow with churning users."""
    q = FairQueue()
    await q.put(_job("u1", "a"))
    await q.get()
    assert "u1" not in q._subqueues
    assert "u1" not in q._order


@pytest.mark.asyncio
async def test_max_queued_enforced():
    q = FairQueue(max_queued=2)
    await q.put(_job("u1", "a"))
    await q.put(_job("u1", "b"))
    with pytest.raises(QueueFullError):
        await q.put(_job("u1", "c"))


@pytest.mark.asyncio
async def test_done_releases_dedup_slot():
    q = FairQueue()
    await q.put(_job("u1", "a"))
    j = await q.get()
    await q.done(j)
    # After done, future puts for the same key go through.
    await q.put(_job("u1", "a"))
    j2 = await q.get()
    assert (j2.user_id, j2.message_id) == ("u1", "a")
