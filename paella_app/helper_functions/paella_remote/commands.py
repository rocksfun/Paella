"""Remote command registry — runs on Qt GUI thread via dispatcher."""

from __future__ import annotations

import time
from typing import Any, Dict, TYPE_CHECKING

from helper_functions.paella_remote.constants import CLEAN_PROTOCOL_MAP
from helper_functions.paella_remote.health import get_health_store

if TYPE_CHECKING:
    from main_gui import MainApplicationWindow


def _pump(main_window: "MainApplicationWindow"):
    return main_window.syringe_widget


def _fluidic_busy(pump) -> bool:
    if getattr(pump, "kickback_in_progress", False):
        return True
    if pump.current_fluidic_state == pump.FLUIDIC_STATE_CLEANING:
        return True
    if pump.routine_thread and pump.routine_thread.isRunning():
        return True
    if getattr(pump, "final_clean_sequence", None):
        return True
    if getattr(pump, "prime_system_sequence", None):
        return True
    return False


def execute_on_gui(main_window: "MainApplicationWindow", command: str, params: Dict[str, Any]) -> Dict[str, Any]:
    pump = _pump(main_window)
    base = {
        "ok": False,
        "command": command,
        "fluidic_state": getattr(pump, "current_fluidic_state", "UNKNOWN"),
        "pumps_connected": bool(pump.comm_thread and pump.comm_thread.isRunning()),
    }

    try:
        if command == "ping":
            return {**base, "ok": True, "message": "pong"}

        if command == "get_status":
            from helper_functions.paella_remote.status import collect_status_snapshot
            return {**base, "ok": True, "status": collect_status_snapshot(main_window)}

        if command == "set_kickback_volume":
            volume = float(params.get("volume_ul", pump.kickback_volume_ul))
            if volume <= 0:
                return {**base, "error": "invalid_volume"}
            result = pump.set_kickback_volume_programmatic(volume)
            return {**base, **result}

        if command == "run_kickback":
            volume = params.get("volume_ul")
            if volume is not None:
                pump.set_kickback_volume_programmatic(float(volume))
            rate = params.get("rate_ul_min")
            result = pump.run_kickback_programmatic(
                volume_ul=float(volume) if volume is not None else None,
                rate_ul_min=float(rate) if rate is not None else None,
            )
            return {**base, **result}

        if command == "run_clean":
            protocol = params.get("protocol", "")
            ack = bool(params.get("ack_sample_destroyed", False))
            result = pump.run_clean_programmatic(protocol, ack_sample_destroyed=ack)
            return {**base, **result}

        if command == "run_final_clean":
            if not params.get("confirmed", False):
                return {
                    **base,
                    "error": "confirmation_required",
                    "message": "Set confirmed=true after operator acknowledges sample destruction.",
                }
            result = pump.run_final_clean_programmatic(skip_confirmation=True)
            return {**base, **result}

        if command == "pumps_connect":
            port = params.get("port")
            result = pump.pumps_connect_programmatic(port=port)
            return {**base, **result}

        if command == "pumps_disconnect":
            result = pump.pumps_disconnect_programmatic()
            return {**base, **result}

        if command == "pumps_initialize":
            result = pump.pumps_initialize_programmatic()
            return {**base, **result}

        if command == "list_capabilities":
            return {
                **base,
                "ok": True,
                "capabilities": {
                    "commands": [
                        "ping",
                        "get_status",
                        "list_capabilities",
                        "run_clean",
                        "run_final_clean",
                        "run_kickback",
                        "set_kickback_volume",
                        "pumps_connect",
                        "pumps_disconnect",
                        "pumps_initialize",
                    ],
                    "clean_protocols": list(CLEAN_PROTOCOL_MAP.keys()),
                },
            }

        return {**base, "error": "unknown_command"}
    except Exception as exc:
        get_health_store().record_error(str(exc), "command")
        return {**base, "error": "exception", "message": str(exc)}
