"""UDP discovery responder — replies to dashboard broadcast with announce payload."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Dict, Optional

from helper_functions.paella_remote.constants import ANNOUNCE_MAGIC, DISCOVER_MAGIC, PAELLA_REMOTE_VERSION, SCHEMA_VERSION
from helper_functions.paella_remote.config import RemoteServerConfig


class PaellaDiscoveryResponder:
    def __init__(self, config: RemoteServerConfig, get_announce_payload: callable):
        self._config = config
        self._get_payload = get_announce_payload
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if not self._config.discovery_enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, name="PaellaDiscovery", daemon=True)
        self._thread.start()
        print(f"Paella discovery listening on UDP port {self._config.discovery_port}")

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _listen_loop(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", self._config.discovery_port))
            self._sock.settimeout(1.0)
        except Exception as exc:
            print(f"Paella discovery failed to bind: {exc}")
            return

        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg.get("type") != DISCOVER_MAGIC:
                continue
            if self._config.discovery_secret:
                if msg.get("secret") != self._config.discovery_secret:
                    continue
            payload = self._get_payload()
            payload["type"] = ANNOUNCE_MAGIC
            payload["schema_version"] = SCHEMA_VERSION
            payload["paella_remote_version"] = PAELLA_REMOTE_VERSION
            payload["reply_epoch_ms"] = int(time.time() * 1000)
            try:
                self._sock.sendto(json.dumps(payload).encode("utf-8"), addr)
            except OSError:
                pass
