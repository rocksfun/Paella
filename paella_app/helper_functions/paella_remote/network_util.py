"""Network helpers for discovery announce payloads."""

from __future__ import annotations

import socket


def get_local_lan_ip() -> str:
    """Best-effort LAN IPv4 for this machine (for dashboard connect URLs)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "127.0.0.1"
