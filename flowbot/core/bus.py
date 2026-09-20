"""A tiny async pub/sub bus.

The data layer publishes; the signal engine, the trader and the dashboard
subscribe. Subscribers get their own bounded queue so a slow consumer (a
browser on a bad connection) can never stall the market-data path - it just
drops its oldest events.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[[str, Any], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._queues: list[tuple[asyncio.Queue, set[str] | None]] = []
        self.dropped = 0

    # -- callback style (in-process, ordered) ------------------------------
    def on(self, topic: str, handler: Handler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    def off(self, topic: str, handler: Handler) -> None:
        if topic in self._handlers and handler in self._handlers[topic]:
            self._handlers[topic].remove(handler)

    # -- queue style (fan-out to websockets) -------------------------------
    def subscribe(self, topics: set[str] | None = None, maxsize: int = 512) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.append((q, topics))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues = [(qq, t) for qq, t in self._queues if qq is not q]

    async def publish(self, topic: str, payload: Any) -> None:
        for handler in self._handlers.get(topic, ()):
            try:
                result = handler(topic, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # a broken subscriber must not kill the feed
                log.exception("handler failed for topic %s", topic)

        for q, topics in self._queues:
            if topics is not None and topic not in topics:
                continue
            if q.full():
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - race
                    pass
            try:
                q.put_nowait((topic, payload))
            except asyncio.QueueFull:  # pragma: no cover - race
                self.dropped += 1


class RingBuffer:
    """Fixed-size history used for tapes, equity curves and recent bars."""

    def __init__(self, maxlen: int) -> None:
        self._dq: deque = deque(maxlen=maxlen)

    def push(self, item: Any) -> None:
        self._dq.append(item)

    def extend(self, items) -> None:
        self._dq.extend(items)

    def clear(self) -> None:
        self._dq.clear()

    def tail(self, n: int) -> list:
        if n >= len(self._dq):
            return list(self._dq)
        return list(self._dq)[-n:]

    def __len__(self) -> int:
        return len(self._dq)

    def __iter__(self):
        return iter(self._dq)

    def __getitem__(self, idx):
        return list(self._dq)[idx]
