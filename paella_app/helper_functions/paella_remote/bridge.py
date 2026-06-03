"""Thread-safe bridge between Qt main window and async remote server."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Slot

from helper_functions.paella_remote.commands import execute_on_gui
from helper_functions.paella_remote.config import RemoteServerConfig
from helper_functions.paella_remote.status import collect_status_snapshot

if TYPE_CHECKING:
    from main_gui import MainApplicationWindow


class PaellaRemoteBridge(QObject):
    """Caches status on the GUI thread; dispatches commands on the GUI thread."""

    def __init__(
        self,
        main_window: "MainApplicationWindow",
        config: RemoteServerConfig,
        service_start_time: float,
    ):
        super().__init__(None)
        self._main_window = main_window
        self._config = config
        self._service_start_time = service_start_time
        self._lock = threading.RLock()
        self._cached_status: Dict[str, Any] = {"type": "status", "system_id": "starting"}
        self._command_busy = False

        interval_ms = max(100, int(1000 / max(config.status_interval_hz, 0.1)))
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status_cache)
        self._status_timer.start(interval_ms)

        self._pending_command_result: Optional[Dict[str, Any]] = None
        self._command_done = threading.Event()
        self._pending_cmd: tuple[str, Dict[str, Any]] = ("", {})

    def _refresh_status_cache(self) -> None:
        snapshot = collect_status_snapshot(
            self._main_window, service_start_time=self._service_start_time
        )
        with self._lock:
            self._cached_status = snapshot

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._cached_status))

    def get_announce_payload(self) -> Dict[str, Any]:
        from helper_functions.paella_remote.network_util import get_local_lan_ip
        status = self.get_status()
        return {
            "system_id": status.get("system_id"),
            "hostname": status.get("hostname"),
            "api_port": self._config.port,
            "api_host": get_local_lan_ip(),
            "fluidic_state": status.get("fluidic_state"),
            "operator": status.get("operator"),
        }

    def execute_command(self, command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        if self._command_busy:
            return {"ok": False, "error": "command_queue_busy", "command": command}

        self._command_busy = True
        self._command_done.clear()
        self._pending_command_result = None
        self._pending_cmd = (command, params)
        QTimer.singleShot(0, self._run_pending_command)
        if not self._command_done.wait(timeout=120.0):
            self._command_busy = False
            return {"ok": False, "error": "command_timeout", "command": command}
        result = self._pending_command_result or {"ok": False, "error": "no_result"}
        self._command_busy = False
        return result

    @Slot()
    def _run_pending_command(self) -> None:
        command, params = self._pending_cmd
        try:
            self._pending_command_result = execute_on_gui(self._main_window, command, params)
        except Exception as exc:
            self._pending_command_result = {
                "ok": False,
                "error": "exception",
                "message": str(exc),
                "command": command,
            }
        finally:
            self._command_done.set()

    def shutdown(self) -> None:
        self._status_timer.stop()
