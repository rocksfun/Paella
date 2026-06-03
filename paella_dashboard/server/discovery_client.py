"""UDP broadcast discovery client for the central dashboard."""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Dict, List

from protocol.constants import ANNOUNCE_MAGIC, DEFAULT_DISCOVERY_PORT, DISCOVER_MAGIC


def scan_network(
    *,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    discovery_secret: str = "",
    timeout_sec: float = 3.0,
) -> List[Dict[str, Any]]:
    """Broadcast PAELLA_DISCOVER and collect PAELLA_ANNOUNCE replies."""
    systems: Dict[str, Dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        sock.bind(("", 0))

        payload = {"type": DISCOVER_MAGIC, "epoch_ms": int(time.time() * 1000)}
        if discovery_secret:
            payload["secret"] = discovery_secret

        message = json.dumps(payload).encode("utf-8")
        sock.sendto(message, ("<broadcast>", discovery_port))
        sock.sendto(message, ("255.255.255.255", discovery_port))

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(8192)
                msg = json.loads(data.decode("utf-8"))
            except (socket.timeout, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg.get("type") != ANNOUNCE_MAGIC:
                continue
            key = f"{msg.get('system_id')}@{msg.get('api_host')}:{msg.get('api_port')}"
            msg["discovered_at"] = int(time.time() * 1000)
            systems[key] = msg
    finally:
        sock.close()

    return list(systems.values())
