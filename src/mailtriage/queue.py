"""In-memory per-mailbox fair queue.

Single-process. Survives only the lifetime of the container.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class Job:
    user_id: str
    message_id: str
    notification: dict[str, Any]


class QueueFullError(Exception):
    """The queue cannot accept more jobs right now."""


# Hard cap on simultaneously-queued jobs. Defends against an unexpected
# notification storm (Graph re-delivery loop, attacker who learned the
# clientState) consuming all RAM. Webhook returns 503 when full.
DEFAULT_MAX_QUEUED = 5000


class FairQueue:
    """One sub-queue per user_id; round-robin pop. Single-process; survives
    only the lifetime of the container.

    Per-message dedup is enforced via an `in_flight` set keyed by
    (user_id, message_id). `put` is a no-op for jobs already queued or
    in-flight. `done()` MUST be called after a worker finishes processing
    a job — otherwise repeated notifications for the same message are
    silently dropped forever.
    """

    def __init__(self, max_queued: int = DEFAULT_MAX_QUEUED) -> None:
        self._max_queued = max_queued
        self._size = 0
        self._subqueues: dict[str, deque[Job]] = {}
        self._order: deque[str] = deque()
        self._in_flight: set[tuple[str, str]] = set()
        self._not_empty = asyncio.Condition()

    async def put(self, job: Job) -> None:
        key = (job.user_id, job.message_id)
        async with self._not_empty:
            if key in self._in_flight:
                # Duplicate notification for a job already queued or being
                # processed. Silently drop — `done()` will reset the slot.
                return
            if self._size >= self._max_queued:
                raise QueueFullError(
                    f"queue full ({self._size}/{self._max_queued}); refusing job"
                )
            if job.user_id not in self._subqueues:
                self._subqueues[job.user_id] = deque()
                self._order.append(job.user_id)
            self._subqueues[job.user_id].append(job)
            self._in_flight.add(key)
            self._size += 1
            self._not_empty.notify()

    async def get(self) -> Job:
        async with self._not_empty:
            while not self._has_any():
                await self._not_empty.wait()
            for _ in range(len(self._order)):
                user_id = self._order[0]
                self._order.rotate(-1)
                sq = self._subqueues.get(user_id)
                if sq:
                    job = sq.popleft()
                    self._size -= 1
                    if not sq:
                        # Evict empty subqueue and its order slot to bound
                        # memory if user_ids ever churn.
                        del self._subqueues[user_id]
                        try:
                            self._order.remove(user_id)
                        except ValueError:
                            pass
                    return job
            raise RuntimeError("queue invariant violated")

    async def done(self, job: Job) -> None:
        """Release the dedup slot for this (user_id, message_id) so future
        notifications can be processed again. Workers MUST call this after
        each job — even on failure."""
        async with self._not_empty:
            self._in_flight.discard((job.user_id, job.message_id))

    def _has_any(self) -> bool:
        return self._size > 0

    @property
    def depth(self) -> int:
        return self._size

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)
