"""System Configuration I/O helper module.

This module provides functionality to read and parse the system configuration file.
The system config file uses a TOML-style format and contains system-wide settings
such as system name, camera information, DAQ settings, and reference paths.
"""

import os
from typing import Any, Dict, List, Optional


import sys

# Default absolute path for Windows systems
_DEFAULT_WIN_CONFIG_PATH = "C:/Paella local/system_config.txt"

# Search for config file in multiple locations
if os.path.exists(_DEFAULT_WIN_CONFIG_PATH):
    SYSTEM_CONFIG_PATH = _DEFAULT_WIN_CONFIG_PATH
else:
    # Try next to the executable if running as a bundle
    if hasattr(sys, '_MEIPASS'):
        # sys.executable is the path to the .exe wrapper
        exe_dir = os.path.dirname(sys.executable)
        exe_config_path = os.path.join(exe_dir, "system_config.txt")
        if os.path.exists(exe_config_path):
            SYSTEM_CONFIG_PATH = exe_config_path
        else:
            # Fallback to internal references (might not exist if excluded in spec)
            SYSTEM_CONFIG_PATH = os.path.join(sys._MEIPASS, "references", "system_config.txt")
    else:
        # Development mode fallback
        SYSTEM_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "references", "system_config.txt")


def parse_toml_config(content: str) -> Dict[str, Dict[str, Any]]:
    """Parse a simple TOML-style config file into a nested dictionary.

    This matches the lightweight parser used elsewhere in the codebase.
    Supports:
    - Sections: ``[section]``
    - Key/value pairs with string, bool, int, or float values.
    - Arrays (simple list format)

    Args:
        content: The raw content of the TOML file as a string.

    Returns:
        A nested dictionary where keys are section names and values are
        dictionaries of key-value pairs within that section.
    """
    config: Dict[str, Dict[str, Any]] = {}
    current_section: Optional[str] = None

    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            if current_section not in config:
                config[current_section] = {}
            continue

        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            # Strip quotes.
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            elif value.startswith("[") and value.endswith("]"):
                # Parse array: ["item1", "item2"] or ['item1', 'item2']
                array_content = value[1:-1].strip()
                if array_content:
                    # Split by comma and strip quotes from each item
                    items = [item.strip().strip('"').strip("'") for item in array_content.split(",")]
                    value = items
                else:
                    value = []
            else:
                # Try to parse bool.
                lower = value.lower()
                if lower == "true":
                    value = True
                elif lower == "false":
                    value = False
                else:
                    # Try int, then float.
                    try:
                        value = int(value)
                    except ValueError:
                        try:
                            value = float(value)
                        except ValueError:
                            # Leave as raw string.
                            pass

            if current_section:
                config.setdefault(current_section, {})[key] = value
            else:
                config.setdefault("root", {})[key] = value

    return config


def load_system_config(config_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load and parse the system configuration file.

    Args:
        config_path: Optional path to the system config file.
                     If not provided, uses the default SYSTEM_CONFIG_PATH.

    Returns:
        A nested dictionary containing the parsed configuration.

    Raises:
        FileNotFoundError: If the config file does not exist.
        IOError: If there is an error reading the file.
    """
    if config_path is None:
        config_path = SYSTEM_CONFIG_PATH

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"System config file not found: {config_path}")

    with open(config_path, mode="r", encoding="utf-8") as file:
        content = file.read()

    return parse_toml_config(content)


def get_system_name(config: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[str]:
    """Get the system name from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        The system name string, or None if not found.
    """
    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return None

    if "system" in config and "system_name" in config["system"]:
        return str(config["system"]["system_name"])

    return None


def get_camera_info(config: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, str]]:
    """Get camera information from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        A dictionary with keys 'brightfield' and 'fluorescent', each containing
        'camera_name' and 'camera_serial' if available.
    """
    camera_info: Dict[str, Dict[str, str]] = {}

    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return camera_info

    if "brightfield" in config:
        brightfield_config = config["brightfield"]
        camera_info["brightfield"] = {}
        if "camera_name" in brightfield_config:
            camera_info["brightfield"]["camera_name"] = str(brightfield_config["camera_name"])
        if "camera_serial" in brightfield_config:
            camera_info["brightfield"]["camera_serial"] = str(brightfield_config["camera_serial"])

    if "fluorescent" in config:
        fluorescent_config = config["fluorescent"]
        camera_info["fluorescent"] = {}
        if "camera_name" in fluorescent_config:
            camera_info["fluorescent"]["camera_name"] = str(fluorescent_config["camera_name"])
        if "camera_serial" in fluorescent_config:
            camera_info["fluorescent"]["camera_serial"] = str(fluorescent_config["camera_serial"])

    return camera_info


def get_daq_info(config: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, str]:
    """Get DAQ information from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        A dictionary containing DAQ settings (e.g., 'daq_name', 'fpga_relay', 'substrate_bias').
    """
    daq_info: Dict[str, str] = {}

    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return daq_info

    if "daq" in config:
        daq_config = config["daq"]
        if "daq_name" in daq_config:
            daq_info["daq_name"] = str(daq_config["daq_name"])
        if "fpga_relay" in daq_config:
            daq_info["fpga_relay"] = str(daq_config["fpga_relay"])
        if "substrate_bias" in daq_config:
            daq_info["substrate_bias"] = str(daq_config["substrate_bias"])
        if "camera_trigger" in daq_config:
            daq_info["camera_trigger"] = str(daq_config["camera_trigger"])
        if "photodiode" in daq_config:
            daq_info["photodiode"] = str(daq_config["photodiode"])

    return daq_info


def get_reference_paths(config: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, str]:
    """Get reference paths from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        A dictionary containing reference paths such as 'active_devices_path',
        'devices_path', and 'smr_settings_path'.
    """
    paths: Dict[str, str] = {}

    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return paths

    if "references" in config:
        ref_config = config["references"]
        if "active_devices_path" in ref_config:
            paths["active_devices_path"] = str(ref_config["active_devices_path"])
        elif "active_device_path" in ref_config:
            # Handle both singular and plural forms
            paths["active_devices_path"] = str(ref_config["active_device_path"])

        if "devices_path" in ref_config:
            paths["devices_path"] = str(ref_config["devices_path"])

        if "smr_settings_path" in ref_config:
            paths["smr_settings_path"] = str(ref_config["smr_settings_path"])

    return paths


def get_operators(config: Optional[Dict[str, Dict[str, Any]]] = None) -> List[str]:
    """Get operators list from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        A list of operator names. Returns empty list if not found or on error.
    """
    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return []

    if "system" in config and "operators" in config["system"]:
        operators = config["system"]["operators"]
        if isinstance(operators, list):
            return [str(op) for op in operators]
        elif isinstance(operators, str):
            # Handle case where it's a single string
            return [operators]

def get_autoclean_summary_enabled(config: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    """Check if the auto clean summary popup is enabled in the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        True if enabled (default), False if explicitly disabled.
    """
    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return True

    if "system" in config and "autoclean_summary_popup" in config["system"]:
        return bool(config["system"]["autoclean_summary_popup"])

    return True

def get_camera_settings(config: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Get camera settings (mode, framerate) from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        A dictionary containing camera settings (e.g., 'camera_mode', 'bf_roi_framerate', 'bffl_roi_framerate').
    """
    settings: Dict[str, Any] = {
        "camera_mode": "BF+FL",  # Default values
        "bf_roi_framerate": 1600,
        "bffl_roi_framerate": 1250
    }

    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return settings

    if "camera_settings" in config:
        cam_config = config["camera_settings"]
        if "camera_mode" in cam_config:
            mode = str(cam_config["camera_mode"])
            # Normalize "BF" to "BF only" for compatibility
            if mode == "BF":
                mode = "BF only"
            settings["camera_mode"] = mode
        
        # New mode-specific framerates
        if "bf_roi_framerate" in cam_config:
            settings["bf_roi_framerate"] = cam_config["bf_roi_framerate"]
        if "bffl_roi_framerate" in cam_config:
            settings["bffl_roi_framerate"] = cam_config["bffl_roi_framerate"]
            
        # Backward compatibility for old single roi_framerate field
        if "roi_framerate" in cam_config:
            fr = cam_config["roi_framerate"]
            # If the mode-specific ones didn't exist, use this as fallback
            if "bf_roi_framerate" not in cam_config:
                settings["bf_roi_framerate"] = fr
            if "bffl_roi_framerate" not in cam_config:
                settings["bffl_roi_framerate"] = fr

    return settings


def get_syringe_pump_settings(config: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Get syringe pump settings (com_port, draw rates) from the system config.

    Args:
        config: Optional pre-loaded config dictionary. If not provided,
                the config will be loaded from the default path.

    Returns:
        A dictionary containing syringe pump settings.
    """
    settings: Dict[str, Any] = {
        "com_port": "COM11",
        "sample.bf_draw_rate": "2.0",
        "sample.bffl_draw_rate": "1.0",
        "bead.draw_rate": "2.0"
    }

    if config is None:
        try:
            config = load_system_config()
        except (FileNotFoundError, IOError):
            return settings

    if "syringe_pumps" in config:
        pump_config = config["syringe_pumps"]
        for key in settings:
            if key in pump_config:
                settings[key] = str(pump_config[key])

    return settings
