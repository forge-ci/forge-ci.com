"""In-memory sample store with a fan-out to live subscribers.

State is process-local by design: the service runs as one worker, history is a
rolling window rebuilt from the agents within minutes of a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import TYPE_CHECKING, final

from forge_dash.models import METRIC_FIELDS, Sample, SeriesColumns

if TYPE_CHECKING:
    from collections.abc import Iterable

_QUEUE_DEPTH = 256


@final
class MetricsStore:
    def __init__(self, host_ids: Iterable[str], retention_s: float) -> None:
        self._retention_s = retention_s
        self._samples: dict[str, deque[Sample]] = {h: deque() for h in host_ids}
        self._subscribers: set[asyncio.Queue[str]] = set()

    def knows(self, host_id: str) -> bool:
        return host_id in self._samples

    def latest(self, host_id: str) -> Sample | None:
        samples = self._samples[host_id]
        return samples[-1] if samples else None

    def add(self, host_id: str, sample: Sample) -> None:
        samples = self._samples[host_id]
        samples.append(sample)
        cutoff = sample.ts - self._retention_s
        while samples and samples[0].ts < cutoff:
            samples.popleft()

    def history(self, host_id: str, since: float) -> SeriesColumns:
        rows = [s for s in self._samples[host_id] if s.ts >= since]
        values = [s.metrics() for s in rows]
        return SeriesColumns(
            ts=[s.ts for s in rows],
            columns={name: [v[i] for v in values] for i, name in enumerate(METRIC_FIELDS)},
        )

    # --- live fan-out -------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, message: str) -> None:
        """Deliver to every subscriber; a subscriber that has fallen behind is dropped
        (its stream sees the drop sentinel and reconnects with a fresh history)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(DROPPED)


DROPPED = "\x00dropped"
