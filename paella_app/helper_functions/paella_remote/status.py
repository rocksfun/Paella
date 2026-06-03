"""Build JSON-serializable status snapshots from the main Paella window."""

from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, TYPE_CHECKING

from helper_functions.SYSTEM_pull_config_io import get_system_name, load_system_config
from helper_functions.paella_remote.constants import PAELLA_REMOTE_VERSION, SCHEMA_VERSION
from helper_functions.paella_remote.health import get_health_store

if TYPE_CHECKING:
    from main_gui import MainApplicationWindow


def _saving_elapsed_sec(smr) -> int | None:
    if not getattr(smr, "is_saving", False):
        return None
    start = getattr(smr, "saving_start_time", None)
    if start is None:
        return None
    return max(0, int(time.time() - start))


def collect_status_snapshot(
    main_window: "MainApplicationWindow",
    *,
    service_start_time: float | None = None,
) -> Dict[str, Any]:
    """Read widget state on the Qt main thread and return agreed status fields only."""
    smr = main_window.smr_widget
    pump = main_window.syringe_widget

    try:
        config = load_system_config()
        system_name = get_system_name(config) or "unknown"
    except Exception:
        system_name = "unknown"

    uptime_sec = None
    if service_start_time is not None:
        uptime_sec = int(time.time() - service_start_time)

    sample_path = getattr(smr, "selected_sample_path", None)
    experiment_string = getattr(smr, "experiment_string", None)
    is_saving = bool(getattr(smr, "is_saving", False))
    experiment_active = bool(is_saving or sample_path or experiment_string)

    return {
        "type": "status",
        "schema_version": SCHEMA_VERSION,
        "system_id": system_name,
        "hostname": socket.gethostname(),
        "paella_version": PAELLA_REMOTE_VERSION,
        "uptime_sec": uptime_sec,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "epoch_ms": int(time.time() * 1000),
        "operator": getattr(main_window, "operator", None) or getattr(smr, "operator", None),
        "fluidic_state": getattr(pump, "current_fluidic_state", "UNKNOWN"),
        "pumps_connected": bool(pump.comm_thread and pump.comm_thread.isRunning()),
        "pump_com_port": (
            getattr(pump.comm_thread, "port", None)
            if pump.comm_thread and pump.comm_thread.isRunning()
            else None
        ),
        "kickback_volume_ul": getattr(pump, "kickback_volume_ul", None),
        "kickback_in_progress": bool(getattr(pump, "kickback_in_progress", False)),
        "health": get_health_store().snapshot(),
        "experiment": {
            "active": experiment_active,
            "sample_path": sample_path,
            "experiment_string": experiment_string,
            "saving_elapsed_sec": _saving_elapsed_sec(smr),
            "is_saving": is_saving,
        },
    }
