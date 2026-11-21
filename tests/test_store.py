from __future__ import annotations

import asyncio

from forge_dash.models import Sample
from forge_dash.store import DROPPED, MetricsStore


def _sample(ts: float) -> Sample:
    return Sample(
        ts=ts,
        cpu_pct=1.0,
        cpu_count=1,
        load1=0.0,
        load5=0.0,
        load15=0.0,
        mem_total_bytes=1,
        mem_used_bytes=0,
        swap_total_bytes=0,
        swap_used_bytes=0,
        net_rx_bps=0.0,
        net_tx_bps=0.0,
        disk_read_bps=0.0,
        disk_write_bps=0.0,
        uptime_s=0.0,
    )


def test_publish_reaches_every_subscriber() -> None:
    store = MetricsStore(["a"], retention_s=60)
    one, two = store.subscribe(), store.subscribe()
    store.publish("hello")
    assert one.get_nowait() == "hello"
    assert two.get_nowait() == "hello"
    store.unsubscribe(one)
    assert store.subscriber_count == 1


def test_slow_subscriber_is_dropped_with_a_sentinel() -> None:
    store = MetricsStore(["a"], retention_s=60)
    slow = store.subscribe()
    for i in range(300):
        store.publish(str(i))
    assert store.subscriber_count == 0
    drained: list[str] = []
    while True:
        try:
            drained.append(slow.get_nowait())
        except asyncio.QueueEmpty:
            break
    assert drained[-1] == DROPPED


def test_latest_and_retention() -> None:
    store = MetricsStore(["a"], retention_s=10)
    assert store.latest("a") is None
    for ts in (0.0, 5.0, 11.0, 16.0):
        store.add("a", _sample(ts))
    latest = store.latest("a")
    assert latest is not None
    assert latest.ts == 16.0
    assert store.history("a", 0).ts == [11.0, 16.0]
