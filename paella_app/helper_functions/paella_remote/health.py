"""Central health / alarm buffer for remote status streaming."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List


class HealthStore:
    """Thread-safe ring buffer of errors and comm events."""

    def __init__(self, max_comm: int = 20, max_errors: int = 5):
        self._lock = threading.RLock()
        self._comm_failures: Deque[Dict[str, Any]] = deque(maxlen=max_comm)
        self._last_error: str = ""
        self._udp_queue_drops: int = 0

    def record_error(self, message: str, source: str = "app") -> None:
        with self._lock:
            self._last_error = f"[{source}] {message}"[:500]

    def record_comm_failure(self, subsystem: str, message: str) -> None:
        with self._lock:
            entry = {
                "subsystem": subsystem,
                "message": message[:300],
                "epoch_ms": int(time.time() * 1000),
            }
            self._comm_failures.append(entry)
            self._last_error = f"[{subsystem}] {message}"[:500]

    def record_udp_queue_drop(self, count: int = 1) -> None:
        with self._lock:
            self._udp_queue_drops += count

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_error": self._last_error,
                "comm_failures": list(self._comm_failures),
                "udp_queue_drops": self._udp_queue_drops,
            }


_store = HealthStore()


def get_health_store() -> HealthStore:
    return _store
