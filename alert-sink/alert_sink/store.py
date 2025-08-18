"""Bounded in-memory history of received notifications."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable

from alert_sink.model import ReceivedAlert

DEFAULT_CAPACITY = 500


class AlertStore:
    """Ring buffer of the most recent alerts.

    Bounded on purpose: a receiver that keeps every notification forever becomes
    the first thing to run out of memory during the incident it is supposed to be
    reporting on.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: deque[ReceivedAlert] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def add_all(self, alerts: Iterable[ReceivedAlert]) -> int:
        added = 0
        with self._lock:
            for alert in alerts:
                self._items.append(alert)
                added += 1
        return added

    def recent(self, limit: int | None = None) -> list[ReceivedAlert]:
        """Most recent alerts first."""
        with self._lock:
            items = list(reversed(self._items))
        if limit is None:
            return items
        if limit < 0:
            raise ValueError("limit must not be negative")
        return items[:limit]

    def firing(self) -> list[ReceivedAlert]:
        """Latest state per alert fingerprint, keeping only what is still firing.

        Alertmanager re-sends the same fingerprint when an alert resolves, so the
        newest notification wins and resolved alerts disappear from the list.
        """
        latest: dict[str, ReceivedAlert] = {}
        for alert in reversed(self.recent()):
            key = alert.fingerprint or f"{alert.alertname}/{alert.instance}"
            latest[key] = alert
        return [alert for alert in latest.values() if alert.status == "firing"]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
