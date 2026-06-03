"""Start/stop uvicorn-backed remote server alongside the Paella GUI."""

from __future__ import annotations

import threading
import time
from typing import Optional, TYPE_CHECKING

from helper_functions.paella_remote.bridge import PaellaRemoteBridge
from helper_functions.paella_remote.config import load_remote_server_config
from helper_functions.paella_remote.discovery import PaellaDiscoveryResponder
from helper_functions.paella_remote.health import get_health_store
from helper_functions.paella_remote.server import create_app

if TYPE_CHECKING:
    from main_gui import MainApplicationWindow


def _install_health_hooks(main_window: "MainApplicationWindow") -> None:
    store = get_health_store()
    pump = main_window.syringe_widget

    if not getattr(pump, "_remote_health_hooked", False):
        original_handle_error = pump.handle_error

        def wrapped_handle_error(error_message, is_critical):
            store.record_comm_failure("pump", str(error_message))
            return original_handle_error(error_message, is_critical)

        pump.handle_error = wrapped_handle_error
        pump._remote_health_hooked = True

    smr = main_window.smr_widget
    if smr.fpga_command_queue is not None and not getattr(smr, "_remote_tcp_hooked", False):
        queue = smr.fpga_command_queue
        original_init = queue.initialize_connection

        def wrapped_init(*args, **kwargs):
            ok, msg = original_init(*args, **kwargs)
            if not ok:
                store.record_comm_failure("fpga_tcp", str(msg))
            return ok, msg

        queue.initialize_connection = wrapped_init
        smr._remote_tcp_hooked = True


class PaellaRemoteService:
    """Embeds FastAPI + uvicorn in a daemon thread when enabled in config."""

    def __init__(self, main_window: "MainApplicationWindow"):
        self._main_window = main_window
        self._config = load_remote_server_config()
        self._bridge: Optional[PaellaRemoteBridge] = None
        self._discovery: Optional[PaellaDiscoveryResponder] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._start_time: Optional[float] = None

    @property
    def config(self):
        return self._config

    def is_enabled(self) -> bool:
        return self._config.enabled

    def start(self) -> None:
        if not self._config.enabled:
            return
        try:
            import uvicorn
        except ImportError:
            print(
                "Paella remote server: install dependencies with "
                "pip install -r requirements-remote.txt"
            )
            return

        if self._thread is not None and self._thread.is_alive():
            return

        _install_health_hooks(self._main_window)
        self._start_time = time.time()
        self._bridge = PaellaRemoteBridge(self._main_window, self._config, self._start_time)
        self._discovery = PaellaDiscoveryResponder(
            self._config, self._bridge.get_announce_payload
        )
        self._discovery.start()

        app = create_app(self._bridge, self._config)
        config = uvicorn.Config(
            app,
            host=self._config.host,
            port=self._config.port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        self._server = uvicorn.Server(config)

        def run_server() -> None:
            try:
                self._server.run()
            except Exception as exc:
                print(f"Paella remote server stopped: {exc}")

        self._thread = threading.Thread(target=run_server, name="PaellaRemoteServer", daemon=True)
        self._thread.start()
        print(
            f"Paella remote API listening on http://0.0.0.0:{self._config.port} "
            f"(UDP discovery port {self._config.discovery_port})"
        )

    def stop(self) -> None:
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None
        if self._server is not None:
            self._server.should_exit = True
            self._server = None
        self._thread = None
