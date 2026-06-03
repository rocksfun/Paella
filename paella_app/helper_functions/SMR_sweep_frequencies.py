"""SMR sweep frequencies helper module.

This module can be:

- Imported to initialize SMR TCP/UDP connections and generate FPGA parameter
  strings for sweeps.
- Run directly to:
  1. Initialize a TCP connection using the same logic as ``pySMR.py`` with
     addresses from ``SMR_config.txt``.
  2. Initialize a UDP multicast connection using the existing logic.
  3. Load SMR parameters from the ``[sweep]`` section of
     ``smr_parameters_config.txt`` (falling back to ``[default]``) and use
     ``FPGA_UserParametersToRegisterValues`` to translate them into the
     SetAllParametersString to be sent to the FPGA via TCP.
  4. Display user controls for:
     - minimum frequency
     - maximum frequency
     - coarse BW
     - substrate bias

Only basic wiring is implemented here; sweep behavior will be implemented
later.
"""

import os
import sys
import struct
import math
import time
import csv
import queue
from datetime import datetime
from typing import Any, Dict, Tuple, Optional, List

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)
from PySide6.QtCore import Qt, QTimer
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False

try:
    from scipy.optimize import curve_fit
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

try:
    import nidaqmx
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False



# Ensure project root is on sys.path when this file is run directly
if hasattr(sys, '_MEIPASS'):
    _PROJECT_ROOT = sys._MEIPASS
else:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

from helper_functions.FPGA_tcp_manager import FPGACommandQueue  # noqa: E402
from helper_functions.UDP_data_manager import UDPDataManager, UDPPacket  # noqa: E402
from helper_functions.FPGA_UserParametersToRegisterValues import (  # noqa: E402
    calculate_register_values,
)
from helper_functions.SYSTEM_pull_config_io import (  # noqa: E402
    parse_toml_config,
    load_system_config,
    get_reference_paths,
    get_system_name,
    get_daq_info,
    get_operators,
)
from helper_functions.SMR_settings_io import (  # noqa: E402
    write_smr_settings,
    read_smr_settings,
)


REFERENCES_DIR = os.path.join(_PROJECT_ROOT, "references")
SMR_CONFIG_PATH = os.path.join(REFERENCES_DIR, "SMR_config.txt")
SMR_PARAMETERS_CONFIG_PATH = os.path.join(REFERENCES_DIR, "smr_parameters_config.txt")


def _load_smr_connection_config() -> Dict[str, Any]:
    """Load SMR TCP/UDP connection parameters from ``SMR_config.txt``."""
    if not os.path.exists(SMR_CONFIG_PATH):
        raise FileNotFoundError(f"SMR config file not found: {SMR_CONFIG_PATH}")

    with open(SMR_CONFIG_PATH, mode="r", encoding="utf-8") as file:
        content = file.read()
    config = parse_toml_config(content)

    conn = config.get("connection", {})
    return {
        "nios_ip": conn.get("nios_ip", "192.168.100.2"),
        "multicast_ip": conn.get("multicast_ip", "224.1.1.1"),
        "host_ip": conn.get("host_ip", "192.168.100.1"),
        "udp_port": int(conn.get("udp_port", 5007)),
        "remote_port": int(conn.get("remote_port", 30)),
    }


def test_existing_tcp_connection() -> bool:
    """Test if an existing TCP connection is still active by sending a disable 'run' command.
    
    Returns:
        True if connection is active (response received), False otherwise
    """
    try:
        # Use command queue to test connection
        queue = FPGACommandQueue()
        
        if not queue.is_connected():
            return False
        
        # Load parameters to generate disable 'run' command
        params = _load_smr_parameters(section="sweep")
        params["run"] = False
        
        # Map to register args and calculate register values
        args, smr_driver_id = _map_parameters_to_register_args(params)
        register_values = calculate_register_values(**args)
        
        # Generate command for smr_driver_mode register (setting_constant = 0)
        smr_driver_id_offset = smr_driver_id * (2**8)
        register_id = smr_driver_id_offset + 0  # setting_constant for smr_driver_mode
        register_value = register_values.get("smr_driver_mode", 0)
        command = f"Pw{register_id},{register_value}\r\n"
        
        # Send command via queue and wait for response
        future = queue.submit_command(command=command, wait_response=True, timeout=1.0)
        
        try:
            success, response = future.result(timeout=2.0)
            return success and len(response) > 0
        except Exception:
            return False
    except Exception as e:
        print(f"Error testing existing TCP connection: {e}")
        return False


def initialize_tcp_connection() -> Tuple[bool, Optional[object], str]:
    """Initialize a TCP connection using the command queue.

    Returns:
        Tuple of (success, queue_instance_or_none, message).
    """
    queue = FPGACommandQueue()
    
    # Test existing connection if queue is already connected
    if queue.is_connected():
        print("Testing existing TCP connection...")
        if test_existing_tcp_connection():
            print("Existing TCP connection is active. Using existing connection.")
            return True, queue, "Using existing TCP connection to FPGA."
        else:
            print("Existing TCP connection is not responding. Establishing new connection...")
    
    # Establish new connection
    conn = _load_smr_connection_config()

    success, message = queue.initialize_connection(
        nios_ip=conn["nios_ip"],
        multicast_ip=conn["multicast_ip"],
        host_ip=conn["host_ip"],
        udp_port=conn["udp_port"],
        remote_port=conn["remote_port"],
    )

    if success:
        return True, queue, message

    return False, None, message


def initialize_udp_connection() -> Tuple[bool, Optional[object], str]:
    """Initialize a UDP multicast connection using UDPDataManager.

    This uses the centralized UDP data manager to establish the connection.

    Returns:
        Tuple of (success, udp_manager_or_none, message).
    """
    conn = _load_smr_connection_config()
    
    try:
        # Use UDPDataManager singleton
        udp_manager = UDPDataManager()
        success, msg = udp_manager.initialize_connection(
            multicast_ip=conn["multicast_ip"],
            host_ip=conn["host_ip"],
            udp_port=conn["udp_port"]
        )
        
        if success:
            return True, udp_manager, msg
        else:
            return False, None, msg
    except Exception as exc:  # pylint: disable=broad-except
        return False, None, f"UDP connection failed: {exc}"


def _load_smr_parameters(section: str = "sweep") -> Dict[str, Any]:
    """Load SMR parameter set from ``smr_parameters_config.txt``.

    Args:
        section: Name of the section to load (e.g., ``\"sweep\"``).
            Values from this section override those in ``[default]``.

    Returns:
        Dictionary of parameter names to values.
    """
    if not os.path.exists(SMR_PARAMETERS_CONFIG_PATH):
        raise FileNotFoundError(
            f"SMR parameters config file not found: {SMR_PARAMETERS_CONFIG_PATH}"
        )

    with open(SMR_PARAMETERS_CONFIG_PATH, mode="r", encoding="utf-8") as file:
        content = file.read()
    config = parse_toml_config(content)

    base = config.get("default", {}).copy()
    overrides = config.get(section, {})
    base.update(overrides)
    return base


def _map_parameters_to_register_args(params: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Map high-level SMR parameters to ``calculate_register_values`` arguments.

    Returns:
        Tuple of (argument_dict, smr_driver_id).
    """
    # Boolean helpers.
    def _to_bool(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            lower = val.strip().lower()
            if lower in ("true", "1", "yes", "on"):
                return True
            if lower in ("false", "0", "no", "off"):
                return False
        return False

    smr_driver_id = int(params.get("smr_driver_id", 0))

    # Map string-valued combos to indices used in calculate_register_values.
    input_source_map = {"channel_a": 0, "channel_b": 1}
    dac_output_map = {
        "off": 0,
        "PLL NCO": 1,
        "Feedback": 2,
        "Feedthrough": 3,
        "Mixed data": 4,
    }
    signal_of_interest_map = {
        "PLL Frequency": 0,
        "error signal": 1,
        "magnitude": 2,
        "agc error signal": 3,
        "mixdown": 4,
    }
    pll_datarate_decimation_map = {
        "1": 0,
        "2": 1,
        "4": 2,
        "8": 3,
        "16": 4,
        "32": 5,
    }

    input_source_str = str(params.get("input_source", "channel_a"))
    dac_a_output_str = str(params.get("dac_a_output", "off"))
    dac_b_output_str = str(params.get("dac_b_output", "off"))
    soi_str = str(params.get("signal_of_interest", "PLL Frequency"))
    pll_rate_str = str(params.get("pll_datarate_decimation", "1"))

    args: Dict[str, Any] = {
        "Run": _to_bool(params.get("run", False)),
        "Enable_AGC": _to_bool(params.get("enable_agc", True)),
        "Send_data_to_pc": _to_bool(params.get("send_data_to_pc", True)),
        "Run_NCO_at_fixed_freq": _to_bool(params.get("run_nco_at_fixed_freq", False)),
        "Impulse": _to_bool(params.get("impulse", False)),
        "Input_source": input_source_map.get(input_source_str, 0),
        "Signal_of_interest": signal_of_interest_map.get(soi_str, 0),
        "DAC_A_output": dac_output_map.get(dac_a_output_str, 0),
        "DAC_B_output": dac_output_map.get(dac_b_output_str, 0),
        "PLL_datarate_decimation": pll_datarate_decimation_map.get(pll_rate_str, 0),
        "Frequency": float(params.get("frequency", 1_000_000.0)),
        "Minimum_frequency": float(params.get("minimum_frequency", 999_000.0)),
        "Maximum_frequency": float(params.get("maximum_frequency", 10_010_000.0)),
        "CIC_rate": int(params.get("cic_rate", 32_767)),
        "CIC_bit_shift": int(params.get("cic_bit_shift", 16)),
        "PLL_delay": float(params.get("pll_delay", 0.0)),
        "PLL_drive_amplitude": float(params.get("pll_drive_amplitude", 0.1)),
        "Feedback_delay": int(params.get("feedback_delay", 0)),
        "Feedback_gain": float(params.get("feedback_gain", 0.1)),
        "Resonator_Q": float(params.get("resonator_q", 0.0)),
        "Loop_bandwidth": float(params.get("loop_bandwidth", 10_000_000.0)),
        "Loop_order": int(params.get("loop_order", 1)),
    }

    return args, smr_driver_id


def generate_set_all_parameters_string(
    register_values: Dict[str, int], smr_driver_id: int
) -> str:
    """Generate SetAllParametersString from register values.

    This mirrors the logic used inside ``FPGAParameterWidget``.
    """
    smr_driver_id_offset = smr_driver_id * (2**8)

    setting_constants = [
        0,
        3,
        1,
        2,
        14,
        17,
        18,
        5,
        19,
        6,
        20,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
    ]

    output_names = [
        "smr_driver_mode",
        "phase_increment_upon_reset",
        "phase_increment_minimum",
        "phase_increment_maximum",
        "decimator_control",
        "gain_proportional",
        "gain_integral",
        "delay",
        "nco_gain",
        "feedback_delay",
        "feedback_gain",
        "sos_filter_0_b0",
        "sos_filter_0_b1",
        "sos_filter_0_b2",
        "sos_filter_0_a1",
        "sos_filter_0_a2",
        "sos_filter_1_b0",
        "sos_filter_1_b1",
        "sos_filter_1_b2",
        "sos_filter_1_a1",
        "sos_filter_1_a2",
    ]

    result_lines = []
    for name, setting_constant in zip(output_names, setting_constants):
        register_id = smr_driver_id_offset + setting_constant
        register_value = register_values.get(name, 0)
        result_lines.append(f"Pw{register_id},{register_value}")

    return "\n".join(result_lines)


def _get_devices_path() -> Optional[str]:
    """Get devices_path from system config file.
    
    Returns:
        devices_path string or None on error.
    """
    try:
        paths = get_reference_paths()
        devices_path = paths.get("devices_path")
        
        if not devices_path:
            print("Warning: devices_path not found in config")
            return None
        
        return devices_path
            
    except Exception as e:
        print(f"Error getting devices_path: {e}")
        return None


def _get_chip_and_system_name() -> Tuple[Optional[str], Optional[str]]:
    """Get chip name and system name from config files.
    
    Uses the same logic as main_gui.py when checking active_devices.
    
    Returns:
        Tuple of (chip_name, system_name) or (None, None) on error.
    """
    try:
        config = load_system_config()
        system_name = get_system_name(config)
        
        if not system_name:
            print("Warning: System name not found in config")
            return None, system_name
        
        paths = get_reference_paths(config)
        active_devices_path = paths.get("active_devices_path")
        
        if not active_devices_path:
            print("Warning: Active devices path not configured")
            return None, system_name
        
        if not os.path.exists(active_devices_path):
            print(f"Warning: Active devices file not found: {active_devices_path}")
            return None, system_name
        
        # Read TSV file
        # Format: first column is device_name, second column is system_name
        matching_rows = []
        try:
            with open(active_devices_path, mode='r', encoding='utf-8') as tsv_file:
                # Use csv.reader with tab delimiter
                reader = csv.reader(tsv_file, delimiter='\t')
                
                # Process all rows (no header in file)
                for row in reader:
                    if len(row) >= 2:
                        device_name = row[0].strip()
                        row_system_name = row[1].strip()
                        # Match if system name in second column matches
                        if row_system_name == system_name:
                            matching_rows.append(device_name)
        except Exception as e:
            print(f"Error reading device file: {e}")
            return None, system_name
        
        # Return chip name (first match if single match, None if multiple or no matches)
        if len(matching_rows) == 1:
            return matching_rows[0], system_name
        elif len(matching_rows) > 1:
            print(f"Warning: Multiple chips logged for this system. Using first: {matching_rows[0]}")
            return matching_rows[0], system_name
        else:
            print("Warning: No chips logged for this system")
            return None, system_name
            
    except Exception as e:
        print(f"Error getting chip and system name: {e}")
        return None, None


def generate_changed_registers_string(
    current_register_values: Dict[str, int],
    previous_register_values: Optional[Dict[str, int]],
    smr_driver_id: int
) -> str:
    """Generate string with only changed register commands.
    
    Args:
        current_register_values: Current register values dictionary
        previous_register_values: Previous register values dictionary (None for first call)
        smr_driver_id: SMR driver ID for register offset calculation
        
    Returns:
        String with changed register commands, or empty string if nothing changed.
        If previous_register_values is None, returns all registers (for initialization).
    """
    smr_driver_id_offset = smr_driver_id * (2**8)

    setting_constants = [
        0, 3, 1, 2, 14, 17, 18, 5, 19, 6, 20,
        22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ]

    output_names = [
        "smr_driver_mode",
        "phase_increment_upon_reset",
        "phase_increment_minimum",
        "phase_increment_maximum",
        "decimator_control",
        "gain_proportional",
        "gain_integral",
        "delay",
        "nco_gain",
        "feedback_delay",
        "feedback_gain",
        "sos_filter_0_b0",
        "sos_filter_0_b1",
        "sos_filter_0_b2",
        "sos_filter_0_a1",
        "sos_filter_0_a2",
        "sos_filter_1_b0",
        "sos_filter_1_b1",
        "sos_filter_1_b2",
        "sos_filter_1_a1",
        "sos_filter_1_a2",
    ]

    result_lines = []
    
    # If no previous values, send all registers (initialization)
    if previous_register_values is None:
        for name, setting_constant in zip(output_names, setting_constants):
            register_id = smr_driver_id_offset + setting_constant
            register_value = current_register_values.get(name, 0)
            result_lines.append(f"Pw{register_id},{register_value}")
    else:
        # Only send registers that have changed
        for name, setting_constant in zip(output_names, setting_constants):
            current_value = current_register_values.get(name, 0)
            previous_value = previous_register_values.get(name, 0)
            
            if current_value != previous_value:
                register_id = smr_driver_id_offset + setting_constant
                result_lines.append(f"Pw{register_id},{current_value}")

    return "\n".join(result_lines)


def build_sweep_parameters_string(section: str = "sweep") -> str:
    """Load SMR sweep parameters and build the SetAllParametersString.

    Args:
        section: Config section name to load (default: ``\"sweep\"``).

    Returns:
        The SetAllParametersString suitable for sending over TCP to the FPGA.
    """
    params = _load_smr_parameters(section=section)
    args, smr_driver_id = _map_parameters_to_register_args(params)
    register_values = calculate_register_values(**args)
    return generate_set_all_parameters_string(register_values, smr_driver_id)


class SMRSweepControlWidget(QWidget):
    """Simple control panel for SMR sweep parameters."""

    def __init__(
        self,
        tcp_socket: Optional[object] = None,
        udp_socket: Optional[object] = None,
        parent: Optional[QWidget] = None,
        pySMR_widget: Optional[object] = None,
        operator: Optional[str] = None,
    ) -> None:  # type: ignore[name-defined]
        super().__init__(parent)
        self.pySMR_widget = pySMR_widget  # Store reference to pySMR widget if called from pySMR
        self.operator = operator  # Store operator from main_gui if provided
        # Use provided TCP connection (command queue) if available, otherwise initialize new one
        if tcp_socket is not None:
            # tcp_socket parameter is actually a queue instance from parent
            self.tcp_command_queue = tcp_socket
        else:
            # Initialize TCP connection using command queue
            tcp_success, queue_instance, tcp_msg = initialize_tcp_connection()
            if tcp_success:
                self.tcp_command_queue = queue_instance
                print(f"TCP connection: {tcp_msg}")
            else:
                # Connection failed, will need to establish new connection when sweep starts
                self.tcp_command_queue = None
                print(f"TCP connection failed: {tcp_msg}")
        # UDP socket parameter is actually UDPDataManager instance
        if isinstance(udp_socket, UDPDataManager):
            self.udp_data_manager = udp_socket
        else:
            # Create new UDP manager (will be initialized when sweep starts if needed)
            self.udp_data_manager = UDPDataManager()
        self._load_default_values()
        self._load_daq_info()
        self._setup_ui()

    def _load_default_values(self) -> None:
        """Load default values from smr_parameters_config.txt [sweep] section."""
        # Default values if not found in config
        self.default_min_freq = 750_000.0  # 750 kHz
        self.default_max_freq = 1_100_000.0  # 1.1 MHz
        self.default_coarse_bw = 100.0
        self.default_substrate_bias = 3.0  # 3 V
        self.default_sweep_amplitude = 0.002

        try:
            params = _load_smr_parameters(section="sweep")
            
            # Load values from config, using defaults if not found
            self.default_min_freq = float(params.get("minimum_frequency", self.default_min_freq))
            self.default_max_freq = float(params.get("maximum_frequency", self.default_max_freq))
            self.default_coarse_bw = float(params.get("coarse_bw", self.default_coarse_bw))
            self.default_substrate_bias = float(params.get("substrate_bias", self.default_substrate_bias))
            self.default_sweep_amplitude = float(params.get("sweep_amplitude", self.default_sweep_amplitude))
        except Exception:  # pylint: disable=broad-except
            # If config file doesn't exist or parsing fails, use hardcoded defaults
            pass

    def _load_daq_info(self) -> None:
        """Load DAQ information from system config for substrate bias control."""
        self.daq_name = None
        self.substrate_bias_address = None
        
        try:
            config = load_system_config()
            daq_info = get_daq_info(config)
            
            if "daq_name" in daq_info:
                self.daq_name = daq_info["daq_name"]
            if "substrate_bias" in daq_info:
                self.substrate_bias_address = daq_info["substrate_bias"]
        except Exception:  # pylint: disable=broad-except
            # If config file doesn't exist or parsing fails, substrate bias will be disabled
            pass

    def _setup_ui(self) -> None:
        """Set up the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Wide and narrow sweep buttons at the top
        buttons_layout = QHBoxLayout()
        self.wide_sweep_button = QPushButton("Wide sweep")
        self.wide_sweep_button.clicked.connect(self._on_wide_sweep_clicked)
        buttons_layout.addWidget(self.wide_sweep_button)

        self.narrow_sweep_button = QPushButton("Narrow sweep")
        self.narrow_sweep_button.clicked.connect(self._on_narrow_sweep_clicked)
        buttons_layout.addWidget(self.narrow_sweep_button)
        
        buttons_layout.addStretch()  # Push buttons to the left
        layout.addLayout(buttons_layout)

        group = QGroupBox("SMR Sweep Controls")
        form = QFormLayout(group)

        self.min_freq_spin = QDoubleSpinBox()
        self.min_freq_spin.setRange(0.0, 1e7)
        self.min_freq_spin.setDecimals(0)
        self.min_freq_spin.setSingleStep(25000)
        self.min_freq_spin.setSuffix(" Hz")
        self.min_freq_spin.setValue(self.default_min_freq)

        self.max_freq_spin = QDoubleSpinBox()
        self.max_freq_spin.setRange(0.0, 2e7)
        self.max_freq_spin.setDecimals(0)
        self.max_freq_spin.setSingleStep(25000)
        self.max_freq_spin.setSuffix(" Hz")
        self.max_freq_spin.setValue(self.default_max_freq)

        self.coarse_bw_spin = QDoubleSpinBox()
        self.coarse_bw_spin.setRange(0.0, 5000)
        self.coarse_bw_spin.setDecimals(0)
        self.coarse_bw_spin.setSingleStep(10)
        self.coarse_bw_spin.setSuffix(" Hz")
        self.coarse_bw_spin.setValue(self.default_coarse_bw)

        self.substrate_bias_spin = QDoubleSpinBox()
        self.substrate_bias_spin.setRange(0, 5)
        self.substrate_bias_spin.setDecimals(1)
        self.substrate_bias_spin.setSingleStep(0.5)
        self.substrate_bias_spin.setSuffix(" V")
        self.substrate_bias_spin.setValue(self.default_substrate_bias)
        # Connect value change signal to update DAQ output
        self.substrate_bias_spin.valueChanged.connect(self._set_substrate_bias_voltage)

        self.sweep_amplitude_spin = QDoubleSpinBox()
        self.sweep_amplitude_spin.setRange(0.0, 0.05)
        self.sweep_amplitude_spin.setDecimals(3)
        self.sweep_amplitude_spin.setSingleStep(0.001)
        self.sweep_amplitude_spin.setValue(self.default_sweep_amplitude)

        form.addRow("Minimum frequency:", self.min_freq_spin)
        form.addRow("Maximum frequency:", self.max_freq_spin)
        form.addRow("Coarse BW:", self.coarse_bw_spin)
        form.addRow("Substrate bias:", self.substrate_bias_spin)
        form.addRow("Sweep amplitude:", self.sweep_amplitude_spin)
        
        # Add operator dropdown only if operator is not provided (standalone mode)
        self.operator_combo = None
        if self.operator is None:
            self.operator_combo = QComboBox()
            # Populate operators from config
            try:
                config = load_system_config()
                operators = get_operators(config)
                if operators:
                    self.operator_combo.addItems(operators)
                else:
                    self.operator_combo.addItem("No operators configured")
            except Exception:  # pylint: disable=broad-except
                self.operator_combo.addItem("Error loading operators")
            form.addRow("Operator:", self.operator_combo)

        layout.addWidget(group)
        
        # Verbose console report checkbox
        self.verbose_console_checkbox = QCheckBox("Verbose console report")
        self.verbose_console_checkbox.setChecked(False)  # Default to False
        layout.addWidget(self.verbose_console_checkbox)
        
        layout.addStretch()
        
        # Status area and Run sweep button at the bottom
        bottom_layout = QHBoxLayout()
        self.status_label = QLabel("Status: Ready")
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        bottom_layout.addWidget(self.status_label)
        
        bottom_layout.addStretch()  # Push button to the right
        
        # Run sweep button
        self.run_sweep_button = QPushButton("Run sweep")
        self.run_sweep_button.clicked.connect(self._on_run_sweep_clicked)
        bottom_layout.addWidget(self.run_sweep_button)
        
        layout.addLayout(bottom_layout)
        
        # Reference to sweep window
        self.sweep_window = None
        
        # Set initial substrate bias voltage
        self._set_substrate_bias_voltage(self.default_substrate_bias)

    def _set_substrate_bias_voltage(self, voltage: float) -> None:
        """Set substrate bias voltage on DAQ analog output.

        Args:
            voltage: Voltage value in volts to set on the analog output.
        """
        if not NIDAQMX_AVAILABLE:
            return

        if self.daq_name is None or self.substrate_bias_address is None:
            return

        try:
            # Construct full channel name (e.g., "Dev1/ao0")
            channel_name = f"{self.daq_name}/{self.substrate_bias_address}"

            with nidaqmx.Task() as task:
                # Add analog output channel
                task.ao_channels.add_ao_voltage_chan(channel_name)
                # Write voltage value
                task.write(voltage)
        except Exception as e:
            print(f"Error setting substrate bias voltage: {e}")

    def _get_most_recent_sweep_results(self) -> Optional[tuple]:
        """Get the most recent sweep results (frequency, Q, substrate_bias) from sweep settings for the current chip.
        
        Returns:
            Tuple of (frequency, Q, substrate_bias) if found, None otherwise.
            frequency: float in Hz
            Q: float (Resonator_Q value)
            substrate_bias: float in volts
        """
        try:
            # Read all settings for the current chip
            settings_list = read_smr_settings()
            
            if not settings_list:
                return None
            
            # Filter for sweep settings
            sweep_settings = [
                s for s in settings_list
                if s.get("settings_type", "").lower() == "sweep"
            ]
            
            if not sweep_settings:
                return None
            
            # Sort by date and time (most recent first)
            # Parse date and time for sorting
            def get_sort_key(setting):
                date_str = setting.get("date", "")
                time_str = setting.get("time", "")
                try:
                    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
                    return dt
                except (ValueError, TypeError):
                    # If parsing fails, put at end
                    return datetime.min
            
            sweep_settings.sort(key=get_sort_key, reverse=True)
            
            # Get the most recent sweep setting
            most_recent = sweep_settings[0]
            
            # Extract frequency
            frequency_str = most_recent.get("Frequency", "")
            if not frequency_str:
                return None
            
            try:
                frequency = float(frequency_str)
            except (ValueError, TypeError):
                return None
            
            # Extract Q (Resonator_Q)
            q_str = most_recent.get("Resonator_Q", "")
            try:
                q_value = float(q_str) if q_str else 0.0
            except (ValueError, TypeError):
                q_value = 0.0
            
            # Extract substrate bias
            bias_str = most_recent.get("substrate_bias", "")
            try:
                substrate_bias = float(bias_str) if bias_str else 3.0
            except (ValueError, TypeError):
                substrate_bias = 3.0
            
            return (frequency, q_value, substrate_bias)
                
        except Exception as e:
            print(f"Error getting most recent sweep results: {e}")
            return None

    def _on_wide_sweep_clicked(self) -> None:
        """Handle Wide Sweep button click - load default values from config to UI."""
        # Reset to default values from config
        self.min_freq_spin.setValue(self.default_min_freq)
        self.max_freq_spin.setValue(self.default_max_freq)
        self.coarse_bw_spin.setValue(self.default_coarse_bw)
        self.substrate_bias_spin.setValue(self.default_substrate_bias)
        self.sweep_amplitude_spin.setValue(self.default_sweep_amplitude)
        
        self.status_label.setText("Status: Wide sweep settings loaded")

    def _on_narrow_sweep_clicked(self) -> None:
        """Handle Narrow Sweep button click - load narrow range from last sweep to UI."""
        # Get the most recent sweep results
        sweep_results = self._get_most_recent_sweep_results()
        if sweep_results is not None:
            recent_freq, recent_q, recent_bias = sweep_results
            # Set min/max frequency to +/-50kHz from the resonant frequency
            min_freq = recent_freq - 50000.0
            max_freq = recent_freq + 50000.0
            
            # Ensure values are within valid range
            if min_freq < 0:
                min_freq = 0.0
            if max_freq > 2e7:
                max_freq = 2e7
            
            # Update the spin boxes
            self.min_freq_spin.setValue(min_freq)
            self.max_freq_spin.setValue(max_freq)
            
            # Update substrate bias if available
            self.substrate_bias_spin.setValue(recent_bias)
            
            print(f"Set sweep frequency range to {min_freq:.0f} - {max_freq:.0f} Hz "
                  f"and substrate bias to {recent_bias:.1f} V "
                  f"based on previous sweep (resonant frequency: {recent_freq:.0f} Hz, Q: {recent_q:.0f})")
            self.status_label.setText(f"Status: Narrow sweep settings loaded (f={recent_freq:.0f} Hz)")
        else:
            print("Warning: No previous sweep results found. Using current values.")
            self.status_label.setText("Status: No previous sweep found - using current values")

    def _on_run_sweep_clicked(self) -> None:
        """Handle Run Sweep button click - start the sweep with current UI values."""
        self._start_sweep()

    def _start_sweep(self) -> None:
        """Start the sweep with current values from spin boxes."""
        if self.sweep_window is None or not self.sweep_window.isVisible():
            # Get current values from spin boxes
            min_freq = self.min_freq_spin.value()
            max_freq = self.max_freq_spin.value()
            coarse_bw = self.coarse_bw_spin.value()
            sweep_amplitude = self.sweep_amplitude_spin.value()
            
            substrate_bias = self.substrate_bias_spin.value()
            verbose_console = self.verbose_console_checkbox.isChecked()
            
            # Get operator: use provided operator if available, otherwise get from dropdown
            operator = self.operator
            if operator is None and self.operator_combo is not None:
                operator = self.operator_combo.currentText()
                # Handle placeholder text
                if operator in ("No operators configured", "Error loading operators", "Config file not found"):
                    operator = None
            
            # Always pass UDP manager (it's always initialized in __init__)
            self.sweep_window = SweepWindow(
                min_freq, max_freq, coarse_bw,
                sweep_amplitude=sweep_amplitude,
                substrate_bias=substrate_bias,
                verbose_console=verbose_console,
                tcp_socket=self.tcp_command_queue,
                udp_socket=self.udp_data_manager,
                parent=self,
                pySMR_widget=self.pySMR_widget,
                operator=operator
            )
            # Pass automated setup mode flag to sweep window
            if hasattr(self, '_automated_setup_mode'):
                self.sweep_window._automated_setup_mode = self._automated_setup_mode
            if hasattr(self, '_automated_setup_main_window'):
                self.sweep_window._automated_setup_main_window = self._automated_setup_main_window
            # Notify pySMR widget about the new sweep window
            if self.pySMR_widget is not None and hasattr(self.pySMR_widget, 'last_sweep_window'):
                self.pySMR_widget.last_sweep_window = self.sweep_window
                # Update button visibility if dialog is open
                if (hasattr(self.pySMR_widget, 'sweep_control_dialog') and 
                    self.pySMR_widget.sweep_control_dialog is not None and
                    hasattr(self.pySMR_widget, 'view_results_button')):
                    self.pySMR_widget.view_results_button.setVisible(True)
            # If automated setup status window exists, ensure it stays on top
            if (self.pySMR_widget is not None and 
                hasattr(self.pySMR_widget, '_automated_setup_status_window') and 
                self.pySMR_widget._automated_setup_status_window):
                # Set parent to status window so sweep window appears below it
                self.sweep_window.setParent(self.pySMR_widget._automated_setup_status_window)
                self.sweep_window.setWindowFlags(
                    Qt.WindowType.Window |
                    Qt.WindowType.WindowTitleHint |
                    Qt.WindowType.WindowMinMaxButtonsHint |
                    Qt.WindowType.WindowCloseButtonHint
                )
            self.sweep_window.show()
            self.sweep_window.start_sweep()
            # Ensure status window is raised after sweep window is shown
            if (self.pySMR_widget is not None and 
                hasattr(self.pySMR_widget, '_automated_setup_status_window') and 
                self.pySMR_widget._automated_setup_status_window):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(100, lambda: self.pySMR_widget._automated_setup_status_window.raise_())
        else:
            # Window already open, bring to front
            self.sweep_window.raise_()
            self.sweep_window.activateWindow()
            # Ensure status window is raised
            if (self.pySMR_widget is not None and 
                hasattr(self.pySMR_widget, '_automated_setup_status_window') and 
                self.pySMR_widget._automated_setup_status_window):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(100, lambda: self.pySMR_widget._automated_setup_status_window.raise_())


class SweepWindow(QMainWindow):
    """Window for displaying sweep results with Magnitude and Phase plots."""

    def __init__(
        self,
        min_frequency: float,
        max_frequency: float,
        coarse_bw: float,
        sweep_amplitude: float = 0.005,
        substrate_bias: float = 3.0,
        verbose_console: bool = False,
        tcp_socket: Optional[object] = None,
        udp_socket: Optional[object] = None,
        parent: Optional[QWidget] = None,
        pySMR_widget: Optional[object] = None,
        operator: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.coarse_bw = coarse_bw
        self.sweep_amplitude = sweep_amplitude
        self.substrate_bias = substrate_bias
        self.verbose_console = verbose_console
        self.operator = operator  # Store operator for saving settings
        # Store reference to parent widget (SMRSweepControlWidget) and pySMR widget
        self.parent_sweep_widget = parent
        self.pySMR_widget = pySMR_widget
        
        # Load fine_bw_min and fine_bw_max from config
        self.fine_bw_min = 5.0  # Default minimum
        self.fine_bw_max = 50.0  # Default maximum
        try:
            params = _load_smr_parameters(section="sweep")
            self.fine_bw_min = float(params.get("fine_bw_min", self.fine_bw_min))
            self.fine_bw_max = float(params.get("fine_bw_max", self.fine_bw_max))
        except Exception:  # pylint: disable=broad-except
            # If config file doesn't exist or parsing fails, use defaults
            pass
        # Use command queue for TCP communication
        if tcp_socket is not None:
            # tcp_socket parameter is actually a queue instance from initialize_tcp_connection
            self.tcp_command_queue = tcp_socket
        else:
            self.tcp_command_queue = FPGACommandQueue()
        # UDP socket parameter is actually UDPDataManager instance
        if isinstance(udp_socket, UDPDataManager):
            self.udp_data_manager = udp_socket
        else:
            # Create new UDP manager (will be initialized when sweep starts if needed)
            self.udp_data_manager = UDPDataManager()
        self.udp_subscriber_id = None
        self.udp_subscriber_queue = None
        self.current_frequency = min_frequency
        self.sweep_running = False
        # Sweep stage tracking
        self.sweep_stage = "coarse"  # "coarse" or "fine"
        # Store coarse sweep fit results for fine sweep
        self.coarse_fit_center: Optional[float] = None
        self.coarse_fit_fwhm: Optional[float] = None
        # Fine sweep initialization state
        self.waiting_for_fine_sweep_init = False
        self.fine_sweep_init_cycle = 0  # Current cycle (0-25)
        self.fine_sweep_init_max_cycles = 30  # Total cycles to perform
        self.fine_sweep_init_waiting_tcp = False  # Waiting for TCP response in init
        # TCP command futures for response tracking
        self.pending_tcp_futures = []  # List of futures for pending TCP commands
        self.pending_fine_sweep_init_futures = []  # List of futures for fine sweep init commands
        self.fine_sweep_init_waiting_udp = False  # Waiting for UDP packet in init
        self.sweep_timer = QTimer(self)
        self.sweep_timer.timeout.connect(self._sweep_step)
        
        # Data storage for plots (separate for coarse and fine)
        self.coarse_frequency_data = []
        self.coarse_magnitude_data = []
        self.coarse_phase_data = []
        self.fine_frequency_data = []
        self.fine_magnitude_data = []
        self.fine_phase_data = []
        # Current active data (points to coarse or fine based on stage)
        self.frequency_data = []
        self.magnitude_data = []
        self.phase_data = []
        
        # TCP response waiting state
        self.waiting_for_tcp_response = False
        self.tcp_response_timeout = 2.0  # 2 second timeout for TCP response
        self.tcp_response_wait_start_time: Optional[float] = None
        self.tcp_response_received_time: Optional[float] = None  # Timestamp when TCP response was received
        self.udp_packet_delay_ms = 7.0  # Minimum delay (ms) after TCP response before accepting UDP packets
        # Increased from 7ms to 15ms to account for command queue processing time and FPGA hardware settling
        # UDP packet waiting state
        self.waiting_for_packets = False
        self.packets_received = 0
        self.datapoints_needed = 0
        # Store first numeric values from each packet at current frequency
        self.current_frequency_values: List[float] = []
        # Timeout tracking for UDP packet collection
        self.packet_wait_start_time: Optional[float] = None
        self.packet_wait_timeout = 10.0  # 10 second timeout per frequency step
        # Track consecutive timeouts to detect persistent UDP issues
        self.consecutive_timeouts = 0
        self.max_consecutive_timeouts = 5  # Stop sweep after 5 consecutive timeouts
        # Track previous register values to only send changed registers
        self.previous_register_values: Optional[Dict[str, int]] = None
        
        self.setWindowTitle("SMR Frequency Sweep")
        self.setGeometry(100, 100, 1400, 900)
        self._setup_ui()
    
    def _verbose_print(self, *args, **kwargs) -> None:
        """Print message only if verbose_console is enabled."""
        if self.verbose_console:
            print(*args, **kwargs)

    def _setup_ui(self) -> None:
        """Set up the user interface with plots and current frequency display."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(10, 10, 10, 10)

        # Sweep stage and progress display
        stage_layout = QHBoxLayout()
        self.stage_label = QLabel("Sweep Stage: Coarse Sweep")
        self.stage_label.setStyleSheet("font-size: 12pt; font-weight: bold; padding: 5px;")
        stage_layout.addWidget(self.stage_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("font-size: 10pt; padding: 5px; min-height: 15px;")
        self.progress_bar.setMinimumWidth(200)
        self.progress_bar.setMinimumHeight(15)
        stage_layout.addWidget(self.progress_bar)
        stage_layout.addStretch()
        layout.addLayout(stage_layout)

        # Sweep parameters table
        params_table = QTableWidget()
        params_table.setRowCount(2)
        params_table.setColumnCount(3)
        params_table.setHorizontalHeaderLabels(["Min Frequency (Hz)", "Max Frequency (Hz)", "Bandwidth (Hz)"])
        params_table.setVerticalHeaderLabels(["Coarse Sweep", "Fine Sweep"])
        params_table.horizontalHeader().setStretchLastSection(False)  # Don't stretch last column
        params_table.setMaximumHeight(160)
        params_table.setMinimumHeight(120)
        params_table.setMinimumWidth(600)
        # Resize columns to fit contents
        params_table.resizeColumnsToContents()
        # Set size policy to not expand horizontally
        params_table.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # Initialize with empty values
        for row in range(2):
            for col in range(3):
                item = QTableWidgetItem("-")
                item.setTextAlignment(Qt.AlignCenter)
                params_table.setItem(row, col, item)
        self.params_table = params_table
        # Add table in a horizontal layout with stretch so it doesn't expand
        table_layout = QHBoxLayout()
        table_layout.addWidget(params_table)
        table_layout.addStretch()
        layout.addLayout(table_layout)

        # Current frequency display
        freq_layout = QHBoxLayout()
        freq_label = QLabel("Current frequency:")
        freq_label.setStyleSheet("font-size: 12pt; font-weight: bold;")
        self.current_freq_label = QLabel("0.000 Hz")
        self.current_freq_label.setStyleSheet(
            "font-size: 14pt; font-weight: bold; color: #0078d7; padding: 5px;"
        )
        freq_layout.addWidget(freq_label)
        freq_layout.addWidget(self.current_freq_label)
        freq_layout.addStretch()
        layout.addLayout(freq_layout)

        # Plots (only if pyqtgraph is available)
        if PYQTGRAPH_AVAILABLE:
            # Coarse sweep row
            coarse_row = QHBoxLayout()
            
            # Coarse raw magnitude plot (left)
            coarse_raw_magnitude_widget = pg.PlotWidget(title="Coarse: Raw Magnitude")
            coarse_raw_magnitude_widget.setLabel("left", "Magnitude")
            coarse_raw_magnitude_widget.setLabel("bottom", "Frequency (Hz)")
            coarse_raw_magnitude_widget.showGrid(x=True, y=True)
            self.coarse_raw_magnitude_plot = coarse_raw_magnitude_widget.plot([], [], pen=pg.mkPen(color="b", width=2), name="Raw Magnitude")
            coarse_row.addWidget(coarse_raw_magnitude_widget)
            
            # Coarse phase plot (center)
            coarse_phase_widget = pg.PlotWidget(title="Coarse: Phase")
            coarse_phase_widget.setLabel("left", "Phase")
            coarse_phase_widget.setLabel("bottom", "Frequency (Hz)")
            coarse_phase_widget.showGrid(x=True, y=True)
            self.coarse_phase_plot = coarse_phase_widget.plot([], [], pen=pg.mkPen(color="r", width=2))
            coarse_row.addWidget(coarse_phase_widget)
            
            # Coarse normalized/fit plot (right)
            coarse_normalized_widget = pg.PlotWidget(title="Coarse: Normalized & Fit")
            coarse_normalized_widget.setLabel("left", "Normalized Magnitude")
            coarse_normalized_widget.setLabel("bottom", "Frequency (Hz)")
            coarse_normalized_widget.showGrid(x=True, y=True)
            coarse_normalized_widget.addLegend()
            # Min-max normalized magnitude (white line)
            self.coarse_normalized_magnitude_plot = coarse_normalized_widget.plot([], [], pen=pg.mkPen(color="w", width=1), name="Normalized")
            # Lorentzian fit (green line)
            self.coarse_lorentzian_fit_plot = coarse_normalized_widget.plot([], [], pen=pg.mkPen(color="g", width=2, style=Qt.PenStyle.DashLine), name="Lorentzian Fit")
            coarse_row.addWidget(coarse_normalized_widget)
            
            # Coarse fit parameters (below plots)
            self.coarse_fit_params_label = QLabel("Coarse Lorentzian Fit:\nNot calculated")
            self.coarse_fit_params_label.setStyleSheet("font-size: 9pt; padding: 5px; border: 1px solid gray;")
            self.coarse_fit_params_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            self.coarse_fit_params_label.setMinimumWidth(200)
            coarse_row.addWidget(self.coarse_fit_params_label)
            
            layout.addLayout(coarse_row)
            
            # Fine sweep row
            fine_row = QHBoxLayout()
            
            # Fine raw magnitude plot (left)
            fine_raw_magnitude_widget = pg.PlotWidget(title="Fine: Raw Magnitude")
            fine_raw_magnitude_widget.setLabel("left", "Magnitude")
            fine_raw_magnitude_widget.setLabel("bottom", "Frequency (Hz)")
            fine_raw_magnitude_widget.showGrid(x=True, y=True)
            self.fine_raw_magnitude_plot = fine_raw_magnitude_widget.plot([], [], pen=pg.mkPen(color="b", width=2), name="Raw Magnitude")
            fine_row.addWidget(fine_raw_magnitude_widget)
            
            # Fine phase plot (center)
            fine_phase_widget = pg.PlotWidget(title="Fine: Phase")
            fine_phase_widget.setLabel("left", "Phase")
            fine_phase_widget.setLabel("bottom", "Frequency (Hz)")
            fine_phase_widget.showGrid(x=True, y=True)
            self.fine_phase_plot = fine_phase_widget.plot([], [], pen=pg.mkPen(color="r", width=2))
            fine_row.addWidget(fine_phase_widget)
            
            # Fine normalized/fit plot (right)
            fine_normalized_widget = pg.PlotWidget(title="Fine: Normalized & Fit")
            fine_normalized_widget.setLabel("left", "Normalized Magnitude")
            fine_normalized_widget.setLabel("bottom", "Frequency (Hz)")
            fine_normalized_widget.showGrid(x=True, y=True)
            fine_normalized_widget.addLegend()
            # Max-normalized magnitude (white line)
            self.fine_normalized_magnitude_plot = fine_normalized_widget.plot([], [], pen=pg.mkPen(color="w", width=1), name="Normalized")
            # Lorentzian fit (green line)
            self.fine_lorentzian_fit_plot = fine_normalized_widget.plot([], [], pen=pg.mkPen(color="g", width=2, style=Qt.PenStyle.DashLine), name="Lorentzian Fit")
            fine_row.addWidget(fine_normalized_widget)
            
            # Fine fit parameters (below plots)
            self.fine_fit_params_label = QLabel("Fine Lorentzian Fit:\nNot calculated")
            self.fine_fit_params_label.setStyleSheet("font-size: 9pt; padding: 5px; border: 1px solid gray;")
            self.fine_fit_params_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            self.fine_fit_params_label.setMinimumWidth(200)
            fine_row.addWidget(self.fine_fit_params_label)
            
            layout.addLayout(fine_row)
        else:
            # Fallback if pyqtgraph is not available
            no_plot_label = QLabel("PyQtGraph not available. Plots cannot be displayed.")
            no_plot_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(no_plot_label)

        # SMR Results display (shown after fine sweep completes)
        results_group = QGroupBox("SMR Results")
        results_group.setStyleSheet("font-size: 11pt; font-weight: bold; padding: 5px;")
        results_layout = QHBoxLayout(results_group)
        results_layout.setSpacing(30)
        
        # SMR Resonance Frequency
        freq_result_layout = QVBoxLayout()
        freq_result_label = QLabel("SMR Resonance Frequency:")
        freq_result_label.setStyleSheet("font-size: 10pt; font-weight: normal;")
        self.smr_resonance_freq_label = QLabel("— Hz")
        self.smr_resonance_freq_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #0078d7;")
        freq_result_layout.addWidget(freq_result_label)
        freq_result_layout.addWidget(self.smr_resonance_freq_label)
        results_layout.addLayout(freq_result_layout)
        
        # SMR Q
        q_result_layout = QVBoxLayout()
        q_result_label = QLabel("SMR Q:")
        q_result_label.setStyleSheet("font-size: 10pt; font-weight: normal;")
        self.smr_q_label = QLabel("—")
        self.smr_q_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #0078d7;")
        q_result_layout.addWidget(q_result_label)
        q_result_layout.addWidget(self.smr_q_label)
        results_layout.addLayout(q_result_layout)
        
        # SMR Amplitude
        amp_result_layout = QVBoxLayout()
        amp_result_label = QLabel("SMR Amplitude:")
        amp_result_label.setStyleSheet("font-size: 10pt; font-weight: normal;")
        self.smr_amplitude_label = QLabel("—")
        self.smr_amplitude_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #0078d7;")
        amp_result_layout.addWidget(amp_result_label)
        amp_result_layout.addWidget(self.smr_amplitude_label)
        results_layout.addLayout(amp_result_layout)
        
        # Buttons layout: Push settings to SMR and Proceed to set delays
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(10)
        
        # Push settings to SMR button
        self.push_settings_button = QPushButton("Push settings to SMR")
        self.push_settings_button.setEnabled(False)  # Disabled by default
        self.push_settings_button.setStyleSheet("""
            QPushButton {
                background-color: #808080;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 8px 16px;
                border: none;
                border-radius: 5px;
            }
            QPushButton:enabled {
                background-color: #ff8800;
            }
            QPushButton:enabled:hover {
                background-color: #ff9900;
            }
            QPushButton:enabled:pressed {
                background-color: #cc6600;
            }
        """)
        self.push_settings_button.clicked.connect(self._on_push_settings_clicked)
        buttons_layout.addWidget(self.push_settings_button)
        
        # Proceed to set delays button
        self.proceed_set_delays_button = QPushButton("Proceed to set delays")
        self.proceed_set_delays_button.setEnabled(False)  # Disabled by default
        self.proceed_set_delays_button.setStyleSheet("""
            QPushButton {
                background-color: #808080;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 8px 16px;
                border: none;
                border-radius: 5px;
            }
            QPushButton:enabled {
                background-color: #4CAF50;
            }
            QPushButton:enabled:hover {
                background-color: #45a049;
            }
            QPushButton:enabled:pressed {
                background-color: #3d8b40;
            }
        """)
        self.proceed_set_delays_button.clicked.connect(self._on_proceed_set_delays_clicked)
        buttons_layout.addWidget(self.proceed_set_delays_button)
        
        push_button_layout = QVBoxLayout()
        push_button_layout.addStretch()  # Push buttons to bottom
        push_button_layout.addLayout(buttons_layout)
        results_layout.addLayout(push_button_layout)
        
        results_layout.addStretch()
        layout.addWidget(results_group)

        layout.addStretch()

    def start_sweep(self) -> None:
        """Start the frequency sweep."""
        if self.sweep_running:
            return
        
        # Test existing TCP connection or establish new one if needed
        # Ensure command queue is initialized and connected
        if self.tcp_command_queue is None:
            self.tcp_command_queue = FPGACommandQueue()
        
        if not self.tcp_command_queue.is_connected():
            self._verbose_print("No TCP connection available. Attempting to establish new connection...")
            tcp_success, queue_instance, tcp_msg = initialize_tcp_connection()
            if tcp_success:
                self.tcp_command_queue = queue_instance
                self._verbose_print(f"TCP connection established: {tcp_msg}")
            else:
                self._verbose_print(f"Error: TCP connection failed. Cannot start sweep. {tcp_msg}")
                return
        else:
            # Test existing connection
            self._verbose_print("Testing existing TCP connection...")
            if test_existing_tcp_connection():
                self._verbose_print("TCP connection is active.")
            else:
                self._verbose_print("Existing TCP connection test failed. Attempting to establish new connection...")
                tcp_success, queue_instance, tcp_msg = initialize_tcp_connection()
                if tcp_success:
                    self.tcp_command_queue = queue_instance
                    self._verbose_print(f"New TCP connection established: {tcp_msg}")
                else:
                    self._verbose_print(f"Error: TCP connection failed. Cannot start sweep. {tcp_msg}")
                    return
        
        # Ensure UDP manager is connected
        if not self.udp_data_manager.is_connected():
            # Try to initialize connection
            conn = _load_smr_connection_config()
            success, msg = self.udp_data_manager.initialize_connection(
                multicast_ip=conn["multicast_ip"],
                host_ip=conn["host_ip"],
                udp_port=conn["udp_port"]
            )
            if not success:
                self._verbose_print(f"Error: UDP connection not available. Cannot start sweep. {msg}")
                return
        
        # Subscribe to UDP manager if not already subscribed
        if self.udp_subscriber_id is None:
            self.udp_subscriber_id, self.udp_subscriber_queue = self.udp_data_manager.subscribe_queue(maxsize=500)
            self._verbose_print(f"Subscribed to UDP manager (subscriber_id={self.udp_subscriber_id})")
        
        # Reset data
        self.coarse_frequency_data = []
        self.coarse_magnitude_data = []
        self.coarse_phase_data = []
        self.fine_frequency_data = []
        self.fine_magnitude_data = []
        self.fine_phase_data = []
        self.frequency_data = []
        self.magnitude_data = []
        self.phase_data = []
        self.current_frequency = self.min_frequency
        self.sweep_stage = "coarse"  # Start with coarse sweep
        self.waiting_for_tcp_response = False
        self.tcp_response_wait_start_time = None
        self.tcp_response_received_time = None  # Reset TCP response timestamp
        self.pending_tcp_futures = []  # Reset pending futures
        self.waiting_for_packets = False
        self.packets_received = 0
        self.datapoints_needed = 0
        self.current_frequency_values = []
        self.packet_wait_start_time = None
        self.consecutive_timeouts = 0  # Reset timeout counter
        self.previous_register_values = None  # Reset to send all registers on first step
        # Reset fit results
        self.coarse_fit_center = None
        self.coarse_fit_fwhm = None
        
        # Update UI
        self.stage_label.setText("Sweep Stage: Coarse Sweep")
        self.progress_bar.setValue(0)
        self.current_freq_label.setText(f"{self.current_frequency:.3f} Hz")
        
        # Reset SMR results display
        self.smr_resonance_freq_label.setText("— Hz")
        self.smr_q_label.setText("—")
        self.smr_amplitude_label.setText("—")
        
        # Update parameters table for coarse sweep
        item = QTableWidgetItem(f"{self.min_frequency:.0f}")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(0, 0, item)
        item = QTableWidgetItem(f"{self.max_frequency:.0f}")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(0, 1, item)
        item = QTableWidgetItem(f"{self.coarse_bw:.0f}")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(0, 2, item)
        
        # Clear fine sweep parameters
        item = QTableWidgetItem("-")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(1, 0, item)
        item = QTableWidgetItem("-")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(1, 1, item)
        item = QTableWidgetItem("-")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(1, 2, item)
        
        # Clear plots
        if PYQTGRAPH_AVAILABLE:
            self.coarse_raw_magnitude_plot.setData([], [])
            self.coarse_phase_plot.setData([], [])
            self.coarse_normalized_magnitude_plot.setData([], [])
            self.coarse_lorentzian_fit_plot.setData([], [])
            self.fine_raw_magnitude_plot.setData([], [])
            self.fine_phase_plot.setData([], [])
            self.fine_normalized_magnitude_plot.setData([], [])
            self.fine_lorentzian_fit_plot.setData([], [])
            self.coarse_fit_params_label.setText("Coarse Lorentzian Fit:\nNot calculated")
            self.fine_fit_params_label.setText("Fine Lorentzian Fit:\nNot calculated")
        
        # Start timer for stepping (will check for UDP packets)
        self.sweep_running = True
        self.sweep_timer.start(10)  # Check every 10ms for UDP packets
        # Trigger first step immediately
        self._sweep_step()

    def _sweep_step(self) -> None:
        """Perform one step of the frequency sweep."""
        # If waiting for fine sweep initialization, handle initialization cycles
        if self.waiting_for_fine_sweep_init:
            self._handle_fine_sweep_init_step()
            return
        
        # If waiting for TCP response, check for it
        if self.waiting_for_tcp_response:
            # Check for timeout
            if self.tcp_response_wait_start_time is not None:
                elapsed_time = time.time() - self.tcp_response_wait_start_time
                if elapsed_time > self.tcp_response_timeout:
                    self._verbose_print(f"Warning: Timeout waiting for TCP response at frequency {self.current_frequency:.3f} Hz. "
                          f"Proceeding anyway.")
                    self.waiting_for_tcp_response = False
                    self.tcp_response_wait_start_time = None
                    self.tcp_response_received_time = time.perf_counter()  # Store timestamp on timeout
                    # Start waiting for UDP packets
                    self._start_waiting_for_udp_packets()
                    return
                else:
                    # Check if all pending Futures are done
                    if self.pending_tcp_futures:
                        # Check if all futures are complete
                        all_done = all(f.done() for f in self.pending_tcp_futures)
                        if all_done:
                            # All commands completed, check for errors
                            errors = []
                            for i, future in enumerate(self.pending_tcp_futures):
                                try:
                                    success, response_bytes = future.result()
                                    if not success:
                                        errors.append(f"Command {i+1} failed")
                                except Exception as e:
                                    errors.append(f"Command {i+1} error: {str(e)}")
                            
                            if errors:
                                self._verbose_print(f"Warning: Some TCP commands failed at frequency {self.current_frequency:.3f} Hz: {errors}. "
                                      f"Proceeding anyway.")
                            
                            # All commands completed (with or without errors), store timestamp and start waiting for UDP packets
                            # Add a small delay to ensure FPGA hardware has applied all register changes
                            self.waiting_for_tcp_response = False
                            self.tcp_response_wait_start_time = None
                            self.tcp_response_received_time = time.perf_counter()  # Use perf_counter for consistency with UDP timestamps
                            self.pending_tcp_futures = []
                            # Start waiting for UDP packets
                            self._start_waiting_for_udp_packets()
                            return
                        # Not all futures done yet, continue waiting
                        return
                        self.tcp_response_wait_start_time = None
                        self.tcp_response_received_time = time.perf_counter()  # Store timestamp when skipping TCP wait
                        # Start waiting for UDP packets
                        self._start_waiting_for_udp_packets()
                        return
        
        # If waiting for UDP packets, check for them
        if self.waiting_for_packets:
            # Check for timeout
            if self.packet_wait_start_time is not None:
                elapsed_time = time.time() - self.packet_wait_start_time
                if elapsed_time > self.packet_wait_timeout:
                    # Timeout reached - proceed with whatever data we have
                    self.consecutive_timeouts += 1
                    self._verbose_print(f"Warning: Timeout waiting for UDP packets at frequency {self.current_frequency:.3f} Hz. "
                          f"Collected {len(self.current_frequency_values)} values, needed >{self.datapoints_needed}. "
                          f"Consecutive timeouts: {self.consecutive_timeouts}/{self.max_consecutive_timeouts}. "
                          f"Proceeding with available data.")
                    
                    # If too many consecutive timeouts, stop the sweep
                    if self.consecutive_timeouts >= self.max_consecutive_timeouts:
                        self._verbose_print(f"Error: Too many consecutive UDP timeouts ({self.consecutive_timeouts}). "
                              f"Stopping sweep. UDP connection may be lost.")
                        self.sweep_timer.stop()
                        self.sweep_running = False
                        return
                    
                    self.waiting_for_packets = False
                    self.packets_received = 0
                    self.packet_wait_start_time = None
                    self._process_frequency_data()
                    self._move_to_next_frequency()
                    return
            
            # Try to get packets from UDP manager queue (drain all available packets)
            if self.udp_subscriber_queue is not None:
                packets_processed_this_tick = 0
                max_packets_per_tick = 20  # Increased to process more packets per tick and prevent queue buildup
                
                while packets_processed_this_tick < max_packets_per_tick:
                    try:
                        udp_packet = self.udp_subscriber_queue.get_nowait()
                    except queue.Empty:
                        break  # No more packets available
                    
                    if udp_packet is not None:
                        raw_data = udp_packet.raw_bytes
                        timestamp = udp_packet.timestamp
                        
                        # Only process packets received at least 7ms after TCP response
                        # This ensures packets are from the current frequency setting, not the previous one
                        should_process = True
                        if self.tcp_response_received_time is not None and timestamp is not None:
                            # Both timestamps should be in the same reference frame
                            # UDP timestamp: kernel timestamp (epoch) or perf_counter (fallback)
                            # TCP timestamp: perf_counter (we set it consistently)
                            # If UDP uses kernel timestamp, we need to convert it
                            if timestamp > 1e10:  # Likely epoch timestamp (seconds since 1970)
                                # Convert epoch timestamp to perf_counter equivalent
                                # This is approximate - we use current time as reference
                                current_time = time.time()
                                current_perf = time.perf_counter()
                                # Estimate perf_counter value at TCP response time
                                tcp_epoch_approx = current_time - (current_perf - self.tcp_response_received_time)
                                time_since_tcp_response = (timestamp - tcp_epoch_approx) * 1000.0
                            else:  # Both are perf_counter - direct comparison
                                time_since_tcp_response = (timestamp - self.tcp_response_received_time) * 1000.0
                            
                            if time_since_tcp_response < self.udp_packet_delay_ms:
                                # Packet received too soon after TCP response, skip it but continue checking queue
                                should_process = False
                                # Debug: log skipped packets
                                if packets_processed_this_tick == 0:  # Only log first skipped packet to avoid spam
                                    self._verbose_print(f"Debug: Skipped packet (too soon: {time_since_tcp_response:.2f}ms < {self.udp_packet_delay_ms}ms)")
                        
                        if should_process:
                            # Reset consecutive timeout counter on successful packet reception
                            self.consecutive_timeouts = 0
                            
                            # Interpret datagram in little endian format
                            # Parse all integer values (signed 32-bit integers)
                            if len(raw_data) >= 4:
                                # Calculate number of 32-bit integers in the packet
                                num_values = len(raw_data) // 4
                                # Unpack all values as little endian signed 32-bit integers
                                all_values = struct.unpack(f'<{num_values}i', raw_data[:num_values * 4])
                                # Discard first entry and append the rest
                                if len(all_values) > 1:
                                    remaining_values = all_values[1:]
                                    # Convert to float and append all remaining values
                                    self.current_frequency_values.extend([float(v) for v in remaining_values])
                                self.packets_received += 1
                            
                            # If we've collected enough values (array length > N), process and move to next step
                            if len(self.current_frequency_values) > self.datapoints_needed:
                                self.waiting_for_packets = False
                                self.packets_received = 0
                                self.packet_wait_start_time = None
                                self.consecutive_timeouts = 0  # Reset on successful completion
                                self._process_frequency_data()
                                self._move_to_next_frequency()
                                return  # Exit early since we've processed enough
                    
                    packets_processed_this_tick += 1
            return
        
        # Check if we've completed the sweep
        if self.current_frequency > self.max_frequency:
            self.sweep_timer.stop()
            self.sweep_running = False
            self._on_sweep_complete()
            return
        
        # Update current frequency display
        self.current_freq_label.setText(f"{self.current_frequency:.3f} Hz")
        
        # Send FPGA settings for current frequency
        try:
            # Load sweep parameters and update frequency
            params = _load_smr_parameters(section="sweep")
            params["frequency"] = self.current_frequency
            params["minimum_frequency"] = self.current_frequency
            params["maximum_frequency"] = self.current_frequency
            # Use sweep amplitude as PLL drive amplitude
            params["pll_drive_amplitude"] = self.sweep_amplitude
            
            # Map to register args and calculate register values
            args, smr_driver_id = _map_parameters_to_register_args(params)
            register_values = calculate_register_values(**args)
            
            # Generate string with only changed registers
            changed_registers_string = generate_changed_registers_string(
                register_values,
                self.previous_register_values,
                smr_driver_id
            )
            
            # Send to FPGA via TCP (only changed registers)
            if self.tcp_command_queue is not None and self.tcp_command_queue.is_connected() and changed_registers_string:
                # Submit all commands and wait for ALL of them to complete
                futures = []
                for line in changed_registers_string.split("\n"):
                    if line.strip():
                        command = line.strip() + "\r\n"
                        future = self.tcp_command_queue.submit_command(
                            command=command,
                            wait_response=True,
                            timeout=1.0
                        )
                        futures.append(future)
                
                # Update previous register values for next comparison
                self.previous_register_values = register_values.copy()
                
                # Store all futures to track when all commands complete
                if futures:
                    self.pending_tcp_futures = futures  # Store all futures, not just the last one
                    # Start waiting for TCP responses before accepting UDP packets
                    self.waiting_for_tcp_response = True
                    self.tcp_response_wait_start_time = time.time()
                    return  # Return and wait for TCP responses in next timer tick
        except Exception as e:  # pylint: disable=broad-except
            self._verbose_print(f"Error sending FPGA settings: {e}")
            self.sweep_timer.stop()
            self.sweep_running = False
            return
        
        # If we get here and no TCP connection, start waiting for UDP packets directly
        # (If TCP connection exists, we would have already started waiting after receiving response)
        if self.tcp_command_queue is None or not self.tcp_command_queue.is_connected():
            self._start_waiting_for_udp_packets()
            return
    
    def _start_waiting_for_udp_packets(self) -> None:
        """Start waiting for UDP packets after TCP response is received."""
        # Calculate number of datapoints needed: N = (100e6/(2^15-1))*(2/bandwidth)
        # This threshold represents the total number of datapoints that must be collected
        # before DC0 and DC90 (and subsequent phase and magnitude) can be calculated.
        # Each UDP packet contains many datapoints which are appended together.
        # bandwidth is the current bandwidth (coarse_bw or fine_bw depending on stage)
        bandwidth = self.coarse_bw if self.sweep_stage == "coarse" else self._get_fine_bw()
        if bandwidth > 0:
            n_datapoints = int((100e6 / (2**15 - 1)) * (2 / bandwidth))
            # Ensure at least 1 datapoint
            n_datapoints = max(1, n_datapoints)
        else:
            n_datapoints = 1
        
        # Start waiting for UDP packets (only after TCP response received)
        self.datapoints_needed = n_datapoints
        self.packets_received = 0
        self.current_frequency_values = []  # Reset for new frequency
        self.packet_wait_start_time = time.time()  # Start timeout timer
        self.waiting_for_packets = True

    def _process_frequency_data(self) -> None:
        """Process collected UDP packet values to calculate magnitude and phase."""
        if not self.current_frequency_values:
            # No data collected, use placeholder values
            self.magnitude_data.append(0.0)
            self.phase_data.append(0.0)
            return
        
        # Decimate array into DC0_array (even indices) and DC90_array (odd indices)
        dc0_array = [self.current_frequency_values[i] for i in range(0, len(self.current_frequency_values), 2)]
        dc90_array = [self.current_frequency_values[i] for i in range(1, len(self.current_frequency_values), 2)]
        
        # Calculate mean for each array
        dc0_mean = sum(dc0_array) / len(dc0_array) if dc0_array else 0.0
        dc90_mean = sum(dc90_array) / len(dc90_array) if dc90_array else 0.0
        
        # Calculate magnitude: (DC0_mean^2 + DC90_mean^2)^0.5
        magnitude = (dc0_mean**2 + dc90_mean**2)**0.5
        
        # Store magnitude
        self.magnitude_data.append(magnitude)
        
        # Calculate phase: inverse tangent where y = DC90_mean and x = DC0_mean
        # Using atan2 for proper quadrant handling
        phase = math.atan2(dc90_mean, dc0_mean)
        
        # Store calculated values in the appropriate arrays
        self.phase_data.append(phase)

    def _move_to_next_frequency(self) -> None:
        """Move to the next frequency step and update plots."""
        # Add current frequency to data (magnitude and phase already calculated in _process_frequency_data)
        self.frequency_data.append(self.current_frequency)
        
        # Update plots if available (use correct plots based on stage)
        if PYQTGRAPH_AVAILABLE:
            if self.sweep_stage == "coarse":
                # Always plot raw magnitude in left plot
                self.coarse_raw_magnitude_plot.setData(self.frequency_data, self.magnitude_data)
                # Always plot phase in center plot
                self.coarse_phase_plot.setData(self.frequency_data, self.phase_data)
            else:  # fine
                # Always plot raw magnitude in left plot
                self.fine_raw_magnitude_plot.setData(self.frequency_data, self.magnitude_data)
                # Always plot phase in center plot
                self.fine_phase_plot.setData(self.frequency_data, self.phase_data)
        
        # Step to next frequency (use appropriate bandwidth for current stage)
        current_bw = self.coarse_bw if self.sweep_stage == "coarse" else self._get_fine_bw()
        self.current_frequency += current_bw
        
        # Update progress bar
        self._update_progress()
        
        # Check if next step would exceed max
        if self.current_frequency > self.max_frequency:
            self.sweep_timer.stop()
            self.sweep_running = False
            self._on_sweep_complete()
            return
        
        # Trigger next step (will send FPGA settings and wait for packets)
        self._sweep_step()

    def _get_fine_bw(self) -> float:
        """Calculate fine bandwidth based on config settings and FWHM from coarse fit.
        
        Logic:
        1. Calculate fine bw from coarse sweep lorentzian fit: fwhm / 30.0
        2. Bound that value by fine_bw_min (from config) and fine_bw_max (from config)
        """
        if self.coarse_fit_fwhm is not None:
            # Calculate fine bw from FWHM: fwhm / 30.0
            fwhm_based_bw = round(self.coarse_fit_fwhm / 30.0)
            # Bound by minimum of fine_bw_min and maximum of fine_bw_max
            # Clamp: ensure value is at least fine_bw_min and at most fine_bw_max
            result = max(self.fine_bw_min, min(fwhm_based_bw, self.fine_bw_max))
            return result
        # If no coarse fit, use fine_bw_min but still cap at fine_bw_max
        return min(self.fine_bw_min, self.fine_bw_max)
    
    def _update_progress(self) -> None:
        """Update progress bar based on current sweep stage."""
        if self.sweep_stage == "coarse":
            total_steps = (self.max_frequency - self.min_frequency) / self.coarse_bw
            current_step = (self.current_frequency - self.min_frequency) / self.coarse_bw
        else:  # fine
            fine_min = self.coarse_fit_center - 150 * self._get_fine_bw()
            fine_max = self.coarse_fit_center + 150 * self._get_fine_bw()
            fine_bw = self._get_fine_bw()
            total_steps = (fine_max - fine_min) / fine_bw
            current_step = (self.current_frequency - fine_min) / fine_bw
        
        if total_steps > 0:
            progress = int((current_step / total_steps) * 100)
            self.progress_bar.setValue(min(100, max(0, progress)))
    
    def _on_sweep_complete(self) -> None:
        """Handle sweep completion: perform fit, then start fine sweep or close connection."""
        if self.sweep_stage == "coarse":
            # Coarse sweep complete - perform fit and start fine sweep
            if len(self.frequency_data) > 0 and len(self.magnitude_data) > 0:
                self._perform_peak_detection_and_fit(is_coarse=True)
                # Fine sweep will be triggered after fit completes
            else:
                self._verbose_print("No data available for peak detection and fitting.")
                # Disable run if no data
                self._disable_run_command()
        else:
            # Fine sweep complete - perform final fit and disable run
            if len(self.frequency_data) > 0 and len(self.magnitude_data) > 0:
                self._perform_peak_detection_and_fit(is_coarse=False)
            else:
                self._verbose_print("No data available for final peak detection and fitting.")
            # Disable run after fine sweep (but keep connection open)
            self._disable_run_command()
    
    def _disable_run_command(self) -> None:
        """Send command to disable 'run' but keep TCP connection open."""
        # Send TCP command to disable 'run' (but don't close connection)
        if self.tcp_command_queue is not None and self.tcp_command_queue.is_connected():
            try:
                # Load current parameters
                params = _load_smr_parameters(section="sweep")
                # Set Run=False to disable
                params["run"] = False
                
                # Map to register args and calculate register values
                args, smr_driver_id = _map_parameters_to_register_args(params)
                register_values = calculate_register_values(**args)
                
                # Generate command for smr_driver_mode register (setting_constant = 0)
                smr_driver_id_offset = smr_driver_id * (2**8)
                register_id = smr_driver_id_offset + 0  # setting_constant for smr_driver_mode
                register_value = register_values.get("smr_driver_mode", 0)
                command = f"Pw{register_id},{register_value}\r\n"
                
                # Send command via queue
                future = self.tcp_command_queue.submit_command(
                    command=command,
                    wait_response=True,
                    timeout=1.0
                )
                self._verbose_print(f"Sweep complete: Sent command to disable 'run' (Pw{register_id},{register_value})")
                
                # Wait for TCP response
                try:
                    success, response_bytes = future.result(timeout=2.0)
                    if success and response_bytes:
                        # Convert hex to ASCII for display
                        try:
                            ascii_response = response_bytes.decode('ascii', errors='replace')
                            hex_response = response_bytes.hex(' ').upper()
                            self._verbose_print(f"TCP Response (disable run): Hex: {hex_response}, ASCII: {repr(ascii_response)}")
                        except Exception as e:
                            hex_response = response_bytes.hex(' ').upper()
                            self._verbose_print(f"TCP Response (disable run): Hex: {hex_response} (ASCII decode failed: {e})")
                    else:
                        self._verbose_print("TCP Response (disable run): No response received")
                except Exception as e:
                    self._verbose_print(f"Error waiting for TCP response (disable run): {e}")
                
            except Exception as e:
                self._verbose_print(f"Error sending disable 'run' command: {e}")

    def _start_fine_sweep(self) -> None:
        """Start fine sweep based on coarse sweep fit results."""
        if self.coarse_fit_center is None or self.coarse_fit_fwhm is None:
            self._verbose_print("Error: Cannot start fine sweep - coarse fit results not available.")
            self._disable_run_command()
            return
        
        # Calculate fine sweep parameters
        fine_bw = self._get_fine_bw()
        fine_min = self.coarse_fit_center - 150 * fine_bw
        fine_max = self.coarse_fit_center + 150 * fine_bw
        
        self._verbose_print(f"Starting fine sweep:")
        self._verbose_print(f"  Center: {self.coarse_fit_center:.3f} Hz")
        self._verbose_print(f"  FWHM: {self.coarse_fit_fwhm:.3f} Hz")
        self._verbose_print(f"  Fine BW: {fine_bw:.3f} Hz")
        self._verbose_print(f"  Range: {fine_min:.3f} Hz to {fine_max:.3f} Hz")
        
        # Update sweep parameters
        self.sweep_stage = "fine"
        self.min_frequency = fine_min
        self.max_frequency = fine_max
        self.current_frequency = fine_min
        
        # Clear data for fine sweep (but keep coarse data)
        self.fine_frequency_data = []
        self.fine_magnitude_data = []
        self.fine_phase_data = []
        self.frequency_data = []
        self.magnitude_data = []
        self.phase_data = []
        self.previous_register_values = None
        self.tcp_response_received_time = None  # Reset TCP response timestamp for fine sweep
        
        # Update UI
        self.stage_label.setText("Sweep Stage: Fine Sweep")
        self.progress_bar.setValue(0)
        self.current_freq_label.setText(f"{self.current_frequency:.3f} Hz")
        
        # Update parameters table for fine sweep
        fine_bw = self._get_fine_bw()
        item = QTableWidgetItem(f"{fine_min:.0f}")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(1, 0, item)
        item = QTableWidgetItem(f"{fine_max:.0f}")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(1, 1, item)
        item = QTableWidgetItem(f"{fine_bw:.0f}")
        item.setTextAlignment(Qt.AlignCenter)
        self.params_table.setItem(1, 2, item)
        
        # Clear fine plots
        if PYQTGRAPH_AVAILABLE:
            self.fine_raw_magnitude_plot.setData([], [])
            self.fine_phase_plot.setData([], [])
            self.fine_normalized_magnitude_plot.setData([], [])
            self.fine_lorentzian_fit_plot.setData([], [])
            self.fine_fit_params_label.setText("Fine Lorentzian Fit:\nNot calculated")
        
        # Start fine sweep initialization: 25 cycles of TCP command -> TCP reply -> UDP packet
        self.waiting_for_fine_sweep_init = True
        self.fine_sweep_init_cycle = 0
        self.fine_sweep_init_waiting_tcp = False
        self.fine_sweep_init_waiting_udp = False
        self.sweep_running = True
        self.sweep_timer.start(10)
        # Start first cycle
        self._start_fine_sweep_init_cycle(fine_min)
    
    def _start_fine_sweep_init_cycle(self, frequency: float) -> None:
        """Start one initialization cycle: send TCP command to set frequency."""
        if self.tcp_command_queue is None or not self.tcp_command_queue.is_connected():
            self._verbose_print("Error: TCP connection not available. Cannot send fine sweep initialization command.")
            self.waiting_for_fine_sweep_init = False
            return
        
        try:
            # Load sweep parameters and update frequency
            params = _load_smr_parameters(section="sweep")
            params["frequency"] = frequency
            params["minimum_frequency"] = frequency
            params["maximum_frequency"] = frequency
            # Use sweep amplitude as PLL drive amplitude
            params["pll_drive_amplitude"] = self.sweep_amplitude
            
            # Map to register args and calculate register values
            args, smr_driver_id = _map_parameters_to_register_args(params)
            register_values = calculate_register_values(**args)
            
            # Generate string with all registers (for initial setting)
            changed_registers_string = generate_changed_registers_string(
                register_values,
                None,  # No previous values, send all registers
                smr_driver_id
            )
            
            # Send to FPGA via TCP
            if changed_registers_string:
                futures = []
                for line in changed_registers_string.split("\n"):
                    if line.strip():
                        command = line.strip() + "\r\n"
                        future = self.tcp_command_queue.submit_command(
                            command=command,
                            wait_response=True,
                            timeout=1.0
                        )
                        futures.append(future)
                
                # Store all futures to track when all commands complete
                if futures:
                    self.pending_fine_sweep_init_futures = futures
                    # Now wait for TCP responses
                    self.fine_sweep_init_waiting_tcp = True
                    self.tcp_response_wait_start_time = time.time()
            else:
                self._verbose_print("Warning: No register commands to send for fine sweep initialization.")
                # Skip to UDP waiting if no commands
                self.fine_sweep_init_waiting_tcp = False
                self.fine_sweep_init_waiting_udp = True
                
        except Exception as e:  # pylint: disable=broad-except
            self._verbose_print(f"Error sending fine sweep initialization command: {e}")
            self.waiting_for_fine_sweep_init = False
    
    def _handle_fine_sweep_init_step(self) -> None:
        """Handle one step of fine sweep initialization (checking TCP/UDP responses)."""
        # If waiting for TCP response, check for it
        if self.fine_sweep_init_waiting_tcp:
            # Check for timeout
            if self.tcp_response_wait_start_time is not None:
                elapsed_time = time.time() - self.tcp_response_wait_start_time
                if elapsed_time > self.tcp_response_timeout:
                    self._verbose_print(f"Warning: Timeout waiting for TCP response in fine sweep init cycle "
                          f"{self.fine_sweep_init_cycle + 1}. Proceeding to UDP wait.")
                    self.fine_sweep_init_waiting_tcp = False
                    self.fine_sweep_init_waiting_udp = True
                    return
            
            # Check if all pending Futures are done
            if self.pending_fine_sweep_init_futures:
                # Check if all futures are complete
                all_done = all(f.done() for f in self.pending_fine_sweep_init_futures)
                if all_done:
                    # All commands completed, check for errors
                    errors = []
                    for i, future in enumerate(self.pending_fine_sweep_init_futures):
                        try:
                            success, response_bytes = future.result()
                            if not success:
                                errors.append(f"Command {i+1} failed")
                        except Exception as e:
                            errors.append(f"Command {i+1} error: {str(e)}")
                    
                    if errors:
                        self._verbose_print(f"Warning: Some TCP commands failed in fine sweep init cycle {self.fine_sweep_init_cycle + 1}: {errors}. Proceeding.")
                    
                    # All commands completed (with or without errors), now wait for UDP packet
                    self.fine_sweep_init_waiting_tcp = False
                    self.fine_sweep_init_waiting_udp = True
                    self.pending_fine_sweep_init_futures = []
                    return
                # Not all futures done yet, continue waiting
                return
        
        # If waiting for UDP packet, check for it
        if self.fine_sweep_init_waiting_udp:
            if self.udp_subscriber_queue is not None:
                # Try to get packet from UDP manager queue (non-blocking)
                try:
                    udp_packet = self.udp_subscriber_queue.get_nowait()
                except queue.Empty:
                    udp_packet = None
                
                if udp_packet is not None:
                    # UDP packet received, cycle complete
                    self.fine_sweep_init_cycle += 1
                    self.fine_sweep_init_waiting_udp = False
                    
                    # Check if we've completed all cycles
                    if self.fine_sweep_init_cycle >= self.fine_sweep_init_max_cycles:
                        self.waiting_for_fine_sweep_init = False
                        # Start fine sweep data collection
                        self._sweep_step()
                    else:
                        # Start next cycle
                        fine_min = self.min_frequency
                        self._start_fine_sweep_init_cycle(fine_min)
            return

    def _max_normalize(self, data: List[float]) -> Tuple[List[float], float]:
        """Perform max normalization on data (divide by maximum).
        
        Args:
            data: List of values to normalize
            
        Returns:
            Tuple of (normalized_data, max_value)
        """
        if not data:
            return [], 0.0
        
        max_val = max(data)
        
        if max_val == 0.0:
            # All values are zero, return zeros
            return [0.0] * len(data), 0.0
        
        # Normalize by dividing by max (peak will be at 1.0)
        normalized = [x / max_val for x in data]
        return normalized, max_val
    
    def _fit_lorentzian_with_quadratic_baseline(
        self,
        frequencies: List[float],
        magnitudes: List[float]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[List[float]], Optional[List[float]], Optional[List[float]]]:
        """Fit Lorentzian with quadratic baseline term included in the fit.
        
        This approach fits: magnitude = Lorentzian_peak + Quadratic_Baseline
        where baseline = a + b*f + c*f²
        
        Args:
            frequencies: List of frequency values
            magnitudes: List of magnitude values
        
        Returns:
            Tuple of (fit_parameters_dict, fit_frequencies, fit_magnitudes, baseline_values) or
            (None, None, None, None) on error.
            
            fit_parameters_dict contains:
            - 'amplitude': Lorentzian peak amplitude
            - 'center': Resonant frequency (Hz)
            - 'gamma': Half-width at half-maximum (Hz)
            - 'fwhm': Full width at half-maximum (2*gamma) (Hz)
            - 'baseline_offset': Baseline constant term (a)
            - 'baseline_slope': Baseline linear coefficient (b)
            - 'baseline_quadratic': Baseline quadratic coefficient (c)
        """
        if not SCIPY_AVAILABLE:
            return None, None, None, None
        
        import numpy as np
        from scipy.optimize import curve_fit
        
        freq_array = np.array(frequencies)
        mag_array = np.array(magnitudes)
        
        if len(frequencies) < 6:  # Need at least 6 points for 6 parameters
            self._verbose_print("Warning: Insufficient data for Lorentzian with quadratic baseline fit.")
            return None, None, None, None
        
        # Find peak for initial estimates
        max_magnitude = max(magnitudes)
        peak_index = magnitudes.index(max_magnitude)
        peak_frequency = frequencies[peak_index]
        freq_range = max(frequencies) - min(frequencies)
        freq_min = min(frequencies)
        freq_max = max(frequencies)
        
        # Estimate baseline from edge regions (excluding peak area)
        edge_fraction = 0.15
        n_edge = max(1, int(len(frequencies) * edge_fraction))
        
        # Left and right edge regions (away from peak)
        left_edge_freqs = frequencies[:n_edge]
        left_edge_mags = magnitudes[:n_edge]
        right_edge_freqs = frequencies[-n_edge:]
        right_edge_mags = magnitudes[-n_edge:]
        
        # Combine edge data for baseline estimation
        edge_freqs = left_edge_freqs + right_edge_freqs
        edge_mags = left_edge_mags + right_edge_mags
        
        # Fit quadratic baseline to edges for initial estimate
        if len(edge_freqs) >= 3:
            edge_freq_array = np.array(edge_freqs)
            edge_mag_array = np.array(edge_mags)
            # Quadratic fit: y = a + b*x + c*x²
            A_matrix = np.vstack([
                np.ones(len(edge_freq_array)),
                edge_freq_array,
                edge_freq_array**2
            ]).T
            baseline_coeffs = np.linalg.lstsq(A_matrix, edge_mag_array, rcond=None)[0]
            baseline_offset_est = baseline_coeffs[0]
            baseline_slope_est = baseline_coeffs[1]
            baseline_quadratic_est = baseline_coeffs[2]
        else:
            baseline_offset_est = np.mean(edge_mags) if edge_mags else np.mean(magnitudes)
            baseline_slope_est = 0.0
            baseline_quadratic_est = 0.0
        
        # Lorentzian with quadratic baseline: A*γ²/((f-f₀)²+γ²) + (a + b*f + c*f²)
        def lorentzian_with_baseline(f, amplitude, center, gamma, baseline_offset, baseline_slope, baseline_quadratic):
            lorentzian_term = amplitude * (gamma**2) / ((f - center)**2 + gamma**2)
            baseline_term = baseline_offset + baseline_slope * f + baseline_quadratic * (f**2)
            return lorentzian_term + baseline_term
        
        # Initial parameter estimates
        initial_params = [
            max_magnitude - baseline_offset_est,  # amplitude (peak above baseline)
            peak_frequency,  # center
            1500.0,  # gamma (initial estimate = 1500 Hz, middle of 500-3000 range)
            baseline_offset_est,  # baseline_offset
            baseline_slope_est,  # baseline_slope
            baseline_quadratic_est  # baseline_quadratic
        ]
        
        # Parameter bounds
        max_quadratic = max_magnitude / (freq_range**2) if freq_range > 0 else 1.0
        bounds = (
            [0, freq_min, 500.0, -max_magnitude, -max_magnitude / freq_range, -max_quadratic],
            [max_magnitude * 10, freq_max, 3000.0, max_magnitude, max_magnitude / freq_range, max_quadratic]
        )
        
        # Perform curve fitting
        try:
            popt, pcov = curve_fit(
                lorentzian_with_baseline,
                freq_array,
                mag_array,
                p0=initial_params,
                bounds=bounds,
                maxfev=10000,
                method='trf'  # Trust Region Reflective algorithm (handles bounds well)
            )
            
            # Extract parameters
            amplitude = popt[0]
            center = popt[1]
            gamma = popt[2]
            baseline_offset = popt[3]
            baseline_slope = popt[4]
            baseline_quadratic = popt[5]
            
            # Calculate uncertainties from covariance matrix
            perr = None
            try:
                perr = [abs(pcov[i, i])**0.5 for i in range(len(popt))]
            except Exception:
                pass
            
            # Build result dictionary
            result_dict = {
                'amplitude': amplitude,
                'center': center,
                'gamma': gamma,
                'fwhm': 2 * gamma,
                'baseline_offset': baseline_offset,
                'baseline_slope': baseline_slope,
                'baseline_quadratic': baseline_quadratic
            }
            
            # Add uncertainties if available
            if perr is not None:
                result_dict['amplitude_uncertainty'] = perr[0]
                result_dict['center_uncertainty'] = perr[1]
                result_dict['gamma_uncertainty'] = perr[2]
                result_dict['fwhm_uncertainty'] = 2 * perr[2]
            
            # Generate fit curve for plotting
            num_points = len(frequencies) * 5
            fit_frequencies = np.linspace(freq_min, freq_max, num_points).tolist()
            fit_magnitudes = [
                lorentzian_with_baseline(f, *popt) for f in fit_frequencies
            ]
            
            # Calculate baseline values at each frequency
            baseline_values = [
                baseline_offset + baseline_slope * f + baseline_quadratic * (f**2)
                for f in frequencies
            ]
            
            return result_dict, fit_frequencies, fit_magnitudes, baseline_values
            
        except Exception as e:
            self._verbose_print(f"Error fitting Lorentzian with quadratic baseline: {e}")
            import traceback
            traceback.print_exc()
            return None, None, None, None

    def _perform_peak_detection_and_fit(self, is_coarse: bool = True) -> None:
        """Perform peak detection on magnitude data and fit a Lorentzian curve."""
        if not SCIPY_AVAILABLE:
            self._verbose_print("Warning: scipy not available. Cannot perform Lorentzian fit.")
            if is_coarse:
                self.coarse_fit_params_label.setText("Coarse Lorentzian Fit:\nscipy not available")
            else:
                self.fine_fit_params_label.setText("Fine Lorentzian Fit:\nscipy not available")
            return
        
        if len(self.frequency_data) < 3 or len(self.magnitude_data) < 3:
            self._verbose_print("Warning: Insufficient data for peak detection and fitting.")
            if is_coarse:
                self.coarse_fit_params_label.setText("Coarse Lorentzian Fit:\nInsufficient data")
            else:
                self.fine_fit_params_label.setText("Fine Lorentzian Fit:\nInsufficient data")
            return
        
        # Store data in the appropriate arrays
        frequencies = self.frequency_data.copy()
        magnitudes = self.magnitude_data.copy()
        
        if is_coarse:
            self.coarse_frequency_data = frequencies.copy()
            self.coarse_magnitude_data = magnitudes.copy()
            self.coarse_phase_data = self.phase_data.copy()
        else:
            self.fine_frequency_data = frequencies.copy()
            self.fine_magnitude_data = magnitudes.copy()
            self.fine_phase_data = self.phase_data.copy()
        
        # Find peak: maximum magnitude
        max_magnitude = max(magnitudes)
        peak_index = magnitudes.index(max_magnitude)
        peak_frequency = frequencies[peak_index]
        
        self._verbose_print(f"Peak detected at frequency: {peak_frequency:.3f} Hz, magnitude: {max_magnitude:.6f}")
        
        # Perform max normalization before fitting
        normalized_magnitudes, mag_max = self._max_normalize(magnitudes)
        
        # Fit Lorentzian to normalized data
        import numpy as np
        from scipy.optimize import curve_fit
        
        # Magnitude Lorentzian function with offset: offset + A * gamma / sqrt((f - f0)^2 + gamma^2)
        # This is the magnitude of the complex Lorentzian response
        # Parameters: [amplitude, center_frequency, gamma, offset]
        def magnitude_lorentzian(f, amplitude, center, gamma, offset):
            return offset + amplitude * gamma / np.sqrt((f - center)**2 + gamma**2)
        
        # Initial parameter estimates
        initial_offset = min(normalized_magnitudes)
        # Amplitude is peak height above offset. Since max is 1.0, amplitude approx 1.0 - offset
        initial_amplitude = 1.0 - initial_offset
        # Center: peak frequency
        initial_center = peak_frequency
        
        # Estimate initial gamma from data: find FWHM by looking for half-max points
        half_max = (max(normalized_magnitudes) + initial_offset) / 2.0
        # Find points above half-max on left and right sides of peak
        left_indices = [i for i in range(peak_index) if normalized_magnitudes[i] >= half_max]
        right_indices = [i for i in range(peak_index + 1, len(normalized_magnitudes)) if normalized_magnitudes[i] >= half_max]
        
        if left_indices and right_indices:
            # Use first point below half-max on each side
            left_fwhm_idx = left_indices[0] - 1 if left_indices[0] > 0 else 0
            right_fwhm_idx = right_indices[-1] + 1 if right_indices[-1] < len(frequencies) - 1 else len(frequencies) - 1
            # Interpolate to find exact half-max frequencies
            if left_fwhm_idx >= 0 and right_fwhm_idx < len(frequencies):
                # Simple linear interpolation for half-max points
                if left_fwhm_idx < peak_index:
                    f_left = frequencies[left_fwhm_idx]
                    f_left_next = frequencies[min(left_fwhm_idx + 1, peak_index)]
                    mag_left = normalized_magnitudes[left_fwhm_idx]
                    mag_left_next = normalized_magnitudes[min(left_fwhm_idx + 1, peak_index)]
                    if mag_left_next != mag_left:
                        t = (half_max - mag_left) / (mag_left_next - mag_left)
                        fwhm_left = f_left + t * (f_left_next - f_left)
                    else:
                        fwhm_left = f_left
                else:
                    fwhm_left = frequencies[peak_index]
                
                if right_fwhm_idx > peak_index:
                    f_right = frequencies[right_fwhm_idx]
                    f_right_prev = frequencies[max(right_fwhm_idx - 1, peak_index)]
                    mag_right = normalized_magnitudes[right_fwhm_idx]
                    mag_right_prev = normalized_magnitudes[max(right_fwhm_idx - 1, peak_index)]
                    if mag_right_prev != mag_right:
                        t = (half_max - mag_right_prev) / (mag_right - mag_right_prev)
                        fwhm_right = f_right_prev + t * (f_right - f_right_prev)
                    else:
                        fwhm_right = f_right
                else:
                    fwhm_right = frequencies[peak_index]
                
                estimated_fwhm = abs(fwhm_right - fwhm_left)
                # Gamma is half of FWHM
                initial_gamma = estimated_fwhm / 2.0
                # Clamp to reasonable range [10, 3000]
                initial_gamma = max(10.0, min(3000.0, initial_gamma))
                self._verbose_print(f"Estimated initial gamma from data FWHM: {initial_gamma:.3f} Hz (FWHM: {estimated_fwhm:.3f} Hz)")
            else:
                initial_gamma = 1500.0
        else:
            # Fallback: estimate from frequency range
            freq_range = max(frequencies) - min(frequencies)
            initial_gamma = freq_range / 10.0  # Rough estimate: 10% of range
            initial_gamma = max(10.0, min(3000.0, initial_gamma))
            self._verbose_print(f"Estimated initial gamma from frequency range: {initial_gamma:.3f} Hz")
        
        initial_params = [initial_amplitude, initial_center, initial_gamma, initial_offset]
        
        # Parameter bounds: amplitude > 0, gamma in [10, 3000] Hz, center within frequency range, offset in [0, 1]
        # Lower bound for gamma reduced from 500 to 10 to allow narrower resonances
        freq_min = min(frequencies)
        freq_max = max(frequencies)
        freq_range = freq_max - freq_min
        # Bounds: [amplitude, center, gamma, offset]
        bounds = (
            [0, freq_min, 10.0, 0.0], 
            [2.0, freq_max, 3000.0, 1.0]
        )
        
        # Calculate weights: higher weight near peak center, lower weight at tails
        # Use Gaussian-like weighting: weight = exp(-((f - f_center)^2) / (2 * sigma_weight^2))
        # For curve_fit, we pass sigma (uncertainty), and weights are 1/sigma^2
        # So we want small sigma (high weight) near center, large sigma (low weight) at tails
        freq_array = np.array(frequencies)
        # Use a fraction of the frequency range as the weighting scale
        # Points within ~30% of range from center get high weight
        weight_scale = freq_range * 0.3
        # Calculate distance from initial peak estimate
        distances_from_peak = np.abs(freq_array - peak_frequency)
        # Calculate weights: Gaussian-like, normalized so max weight is 1.0
        # weight = exp(-(distance^2) / (2 * scale^2))
        weights = np.exp(-(distances_from_peak**2) / (2 * (weight_scale**2)))
        # Convert weights to sigma (uncertainty) for curve_fit
        # We want: weight = 1/sigma^2, so sigma = 1/sqrt(weight)
        # But we need to avoid division by zero, so add small epsilon
        # Also normalize so minimum sigma is reasonable (not too small)
        min_weight = 0.01  # Minimum weight at tails (1% of peak weight)
        weights = np.maximum(weights, min_weight)
        # Calculate sigma: larger sigma = lower weight
        # Use inverse relationship: sigma = base_sigma / sqrt(weight)
        # This ensures high weight (near 1) gives small sigma, low weight gives large sigma
        base_sigma = 0.1  # Base uncertainty for normalized data
        sigma_weights = base_sigma / np.sqrt(weights)
        
        try:
                # Perform curve fitting on normalized data with weights
                norm_array = np.array(normalized_magnitudes)
                popt, pcov = curve_fit(
                    magnitude_lorentzian,
                    freq_array,
                    norm_array,
                    p0=initial_params,
                    bounds=bounds,
                    sigma=sigma_weights,  # Pass weights as sigma (uncertainties)
                    absolute_sigma=False,  # Treat sigma as relative weights
                    maxfev=10000
                )
                
                amplitude, center, gamma, offset = popt
                fwhm = 2 * gamma
                
                # Check if fit parameters hit bounds (indicates potential fitting issues)
                bounds_lower = [0, freq_min, 10.0, 0.0]
                bounds_upper = [2.0, freq_max, 3000.0, 1.0]
                param_names = ['amplitude', 'center', 'gamma', 'offset']
                bound_warnings = []
                for i, (param_name, param_value, lower, upper) in enumerate(zip(param_names, popt, bounds_lower, bounds_upper)):
                    tolerance = 1e-6
                    if abs(param_value - lower) < tolerance:
                        bound_warnings.append(f"{param_name} hit lower bound ({lower})")
                    elif abs(param_value - upper) < tolerance:
                        bound_warnings.append(f"{param_name} hit upper bound ({upper})")
                
                if bound_warnings:
                    self._verbose_print(f"Warning: Fit parameters hit bounds: {', '.join(bound_warnings)}")
                    self._verbose_print(f"  This may indicate the fit is constrained. Consider adjusting bounds or function form.")
                
                # Calculate uncertainties from covariance matrix
                perr = None
                fit_quality_warning = None
                try:
                    perr = [abs(pcov[i, i])**0.5 for i in range(len(popt))]
                    # Check if uncertainties are reasonable (not too large relative to parameter values)
                    for i, (param_name, param_value, uncertainty) in enumerate(zip(param_names, popt, perr)):
                        if param_value != 0 and abs(uncertainty / param_value) > 0.5:  # >50% uncertainty
                            fit_quality_warning = f"Large uncertainty in {param_name} ({uncertainty/abs(param_value)*100:.1f}%)"
                            break
                except Exception as e:
                    self._verbose_print(f"Warning: Could not calculate parameter uncertainties: {e}")
                
                if fit_quality_warning:
                    self._verbose_print(f"Warning: {fit_quality_warning} - fit may not be reliable")
                
                # Generate fit curve for plotting (use finer resolution for smooth curve)
                num_points = len(frequencies) * 5  # 5x resolution for smooth fit
                fit_frequencies = [min(frequencies) + (max(frequencies) - min(frequencies)) * i / (num_points - 1) 
                                  for i in range(num_points)]
                fit_magnitudes = [magnitude_lorentzian(f, amplitude, center, gamma, offset) for f in fit_frequencies]
                
                # Update plots with new layout: raw magnitude (left), phase (center), normalized/fit (right)
                if PYQTGRAPH_AVAILABLE:
                    if is_coarse:
                        # Left plot: Raw magnitude (already plotted during sweep)
                        self.coarse_raw_magnitude_plot.setData(frequencies, magnitudes)
                        # Center plot: Phase (already plotted during sweep)
                        # Right plot: Normalized magnitude (white), fit (green)
                        self.coarse_normalized_magnitude_plot.setData(frequencies, normalized_magnitudes)
                        self.coarse_lorentzian_fit_plot.setData(fit_frequencies, fit_magnitudes)
                    else:
                        # Left plot: Raw magnitude (already plotted during sweep)
                        self.fine_raw_magnitude_plot.setData(frequencies, magnitudes)
                        # Center plot: Phase (already plotted during sweep)
                        # Right plot: Normalized magnitude (white), fit (green)
                        self.fine_normalized_magnitude_plot.setData(frequencies, normalized_magnitudes)
                        self.fine_lorentzian_fit_plot.setData(fit_frequencies, fit_magnitudes)
                
                # Display fit parameters (use correct label based on stage)
                if perr is not None:
                    param_text = (
                        f"Amplitude: {amplitude:.6f} ± {perr[0]:.6f}\n"
                        f"Peak Center: {center:.3f} ± {perr[1]:.3f} Hz\n"
                        f"Gamma (FWHM/2): {gamma:.3f} ± {perr[2]:.3f} Hz\n"
                        f"FWHM: {fwhm:.3f} Hz\n"
                        f"Offset: {offset:.6f}"
                    )
                else:
                    param_text = (
                        f"Amplitude: {amplitude:.6f}\n"
                        f"Peak Center: {center:.3f} Hz\n"
                        f"Gamma (FWHM/2): {gamma:.3f} Hz\n"
                        f"FWHM: {fwhm:.3f} Hz\n"
                        f"Offset: {offset:.6f}"
                    )
                
                if is_coarse:
                    self.coarse_fit_params_label.setText(f"Coarse Lorentzian Fit:\n{param_text}")
                else:
                    self.fine_fit_params_label.setText(f"Fine Lorentzian Fit:\n{param_text}")
                
                self._verbose_print(f"Lorentzian fit completed ({'coarse' if is_coarse else 'fine'}):\n{param_text}")
                
                # Store fit results for fine sweep if this is coarse sweep
                if is_coarse:
                    self.coarse_fit_center = center
                    self.coarse_fit_fwhm = fwhm
                    # Trigger fine sweep
                    self._start_fine_sweep()
                else:
                    # Fine sweep complete - update SMR results display
                    # 1. SMR Resonance Frequency (center frequency from fine sweep fit)
                    smr_resonance_freq = center
                    self.smr_resonance_freq_label.setText(f"{round(smr_resonance_freq)} Hz")
                    
                    # 2. SMR Q (center frequency / FWHM, rounded to nearest integer)
                    if fwhm > 0:
                        smr_q = round(smr_resonance_freq / fwhm)
                    else:
                        smr_q = 0
                    self.smr_q_label.setText(str(smr_q))
                    
                    # 3. SMR Amplitude (maximum raw magnitude value from fine sweep)
                    if self.fine_magnitude_data:
                        smr_amplitude = max(self.fine_magnitude_data)
                        self.smr_amplitude_label.setText(f"{smr_amplitude:.2e}")
                    else:
                        smr_amplitude = 0.0
                        self.smr_amplitude_label.setText("—")
                    
                    # Save sweep results to TSV file
                    self._save_sweep_results(
                        smr_resonance_freq=smr_resonance_freq,
                        smr_q=smr_q,
                        smr_amplitude=smr_amplitude
                    )
                    
                    # Save SMR settings from sweep
                    self._save_smr_settings_from_sweep(
                        smr_resonance_freq=smr_resonance_freq,
                        smr_q=smr_q,
                        smr_amplitude=smr_amplitude
                    )
            
        except Exception as e:
            error_msg = f"Error - {str(e)}"
            self._verbose_print(f"Error performing Lorentzian fit: {e}")
            if is_coarse:
                self.coarse_fit_params_label.setText(f"Coarse Lorentzian Fit:\n{error_msg}")
                # If coarse sweep fit failed, disable run
                self._disable_run_command()
            else:
                self.fine_fit_params_label.setText(f"Fine Lorentzian Fit:\n{error_msg}")
                # If fine sweep fit failed, disable run
                self._disable_run_command()
            return

    def closeEvent(self, event):
        """Clean up when window is closed - unsubscribe from UDP manager."""
        # Stop sweep if running
        if self.sweep_running:
            self.sweep_timer.stop()
            self.sweep_running = False
        
        # Unsubscribe from UDP manager to prevent orphaned queues
        if self.udp_subscriber_id is not None and self.udp_data_manager is not None:
            try:
                self.udp_data_manager.unsubscribe(self.udp_subscriber_id)
                self._verbose_print(f"Unsubscribed from UDP manager (subscriber_id={self.udp_subscriber_id})")
            except Exception as e:
                self._verbose_print(f"Error unsubscribing from UDP manager: {e}")
            finally:
                self.udp_subscriber_id = None
                self.udp_subscriber_queue = None
        
        event.accept()
    
    def _on_push_settings_clicked(self) -> None:
        """Handle Push settings to SMR button click."""
        try:
            # Read settings from CSV file
            settings_list = read_smr_settings()
            if not settings_list:
                self._verbose_print("Error: No settings found in CSV file. Cannot push settings.")
                return
            
            # Filter for settings with settings_type='sweep' and get the most recent one
            sweep_settings = [s for s in settings_list if s.get("settings_type", "").strip().lower() == "sweep"]
            if not sweep_settings:
                self._verbose_print("Error: No settings with settings_type='sweep' found in CSV file. Cannot push settings.")
                return
            
            # Get the most recent sweep settings (last entry in the filtered list)
            latest_settings = sweep_settings[-1]
            self._verbose_print(f"Using most recent sweep settings (row {len(sweep_settings)} of {len(sweep_settings)} sweep entries)")
            
            # Convert settings dictionary to register values and push to FPGA
            # Helper function to convert string to bool
            def _str_to_bool(s: str) -> bool:
                return s.lower() in ("true", "1", "yes", "on")
            
            # Helper function to convert string to int
            def _str_to_int(s: str) -> int:
                try:
                    return int(float(s))  # Handle "1.0" -> 1
                except (ValueError, TypeError):
                    return 0
            
            # Helper function to convert string to float
            def _str_to_float(s: str) -> float:
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return 0.0
            
            # Map string-valued combos to indices used in calculate_register_values
            input_source_map = {"channel_a": 0, "channel_b": 1}
            dac_output_map = {
                "off": 0,
                "PLL NCO": 1,
                "Feedback": 2,
                "Feedthrough": 3,
                "Mixed data": 4,
            }
            signal_of_interest_map = {
                "PLL Frequency": 0,
                "error signal": 1,
                "magnitude": 2,
                "agc error signal": 3,
                "mixdown": 4,
            }
            pll_datarate_decimation_map = {
                "1": 0,
                "2": 1,
                "4": 2,
                "8": 3,
                "16": 4,
                "32": 5,
            }
            
            # Extract values from settings dictionary
            input_source_str = str(latest_settings.get("Input_source", "channel_a"))
            dac_a_output_str = str(latest_settings.get("DAC_A_output", "off"))
            dac_b_output_str = str(latest_settings.get("DAC_B_output", "off"))
            soi_str = str(latest_settings.get("Signal_of_interest", "PLL Frequency"))
            pll_rate_str = str(latest_settings.get("PLL_datarate_decimation", "1"))
            
            # Build arguments for calculate_register_values
            args = {
                "Run": _str_to_bool(latest_settings.get("Run", "False")),
                "Enable_AGC": _str_to_bool(latest_settings.get("Enable_AGC", "True")),
                "Send_data_to_pc": _str_to_bool(latest_settings.get("Send_data_to_pc", "True")),
                "Run_NCO_at_fixed_freq": _str_to_bool(latest_settings.get("Run_NCO_at_fixed_freq", "False")),
                "Impulse": _str_to_bool(latest_settings.get("Impulse", "False")),
                "Input_source": input_source_map.get(input_source_str, 0),
                "Signal_of_interest": signal_of_interest_map.get(soi_str, 0),
                "DAC_A_output": dac_output_map.get(dac_a_output_str, 0),
                "DAC_B_output": dac_output_map.get(dac_b_output_str, 0),
                "PLL_datarate_decimation": pll_datarate_decimation_map.get(pll_rate_str, 0),
                "Frequency": _str_to_float(latest_settings.get("Frequency", "1000000.0")),
                "Minimum_frequency": _str_to_float(latest_settings.get("Minimum_frequency", "999000.0")),
                "Maximum_frequency": _str_to_float(latest_settings.get("Maximum_frequency", "1001000.0")),
                "CIC_rate": _str_to_int(latest_settings.get("CIC_rate", "32767")),
                "CIC_bit_shift": _str_to_int(latest_settings.get("CIC_bit_shift", "16")),
                "PLL_delay": _str_to_float(latest_settings.get("PLL_delay", "0.0")),
                "PLL_drive_amplitude": _str_to_float(latest_settings.get("PLL_drive_amplitude", "0.1")),
                "Feedback_delay": _str_to_int(latest_settings.get("Feedback_delay", "0")),
                "Feedback_gain": _str_to_float(latest_settings.get("Feedback_gain", "0.1")),
                "Resonator_Q": _str_to_float(latest_settings.get("Resonator_Q", "0.0")),
                "Loop_bandwidth": _str_to_float(latest_settings.get("Loop_bandwidth", "10000000.0")),
                "Loop_order": _str_to_int(latest_settings.get("Loop_order", "1")),
            }
            
            # Calculate register values
            smr_driver_id = _str_to_int(latest_settings.get("smr_driver_id", "0"))
            register_values = calculate_register_values(**args)
            
            # Generate SetAllParametersString and send to FPGA
            set_all_string = generate_set_all_parameters_string(register_values, smr_driver_id)
            
            if self.tcp_command_queue is not None and self.tcp_command_queue.is_connected():
                # Send all commands to FPGA
                futures = []
                for line in set_all_string.split("\n"):
                    if line.strip():
                        command = line.strip() + "\r\n"
                        future = self.tcp_command_queue.submit_command(
                            command=command,
                            wait_response=True,
                            timeout=1.0
                        )
                        futures.append(future)
                
                # Wait for all commands to complete
                for future in futures:
                    try:
                        success, response_bytes = future.result(timeout=2.0)
                        if not success:
                            self._verbose_print(f"Warning: Some TCP commands failed when pushing settings.")
                    except Exception as e:
                        self._verbose_print(f"Warning: Error waiting for TCP response: {e}")
                
                self._verbose_print("Settings pushed to FPGA successfully.")
            else:
                self._verbose_print("Error: TCP connection not available. Cannot push settings to FPGA.")
                return
            
            # If called from pySMR, update GUI controls
            if self.pySMR_widget is not None:
                try:
                    # Update Run checkbox
                    if hasattr(self.pySMR_widget, 'quick_run_checkbox'):
                        run_value = _str_to_bool(latest_settings.get("Run", "False"))
                        self.pySMR_widget.quick_run_checkbox.blockSignals(True)
                        self.pySMR_widget.quick_run_checkbox.setChecked(run_value)
                        self.pySMR_widget.quick_run_checkbox.blockSignals(False)
                        # Trigger the callback to update FPGA
                        self.pySMR_widget.on_quick_run_changed(run_value)
                    
                    # Update substrate bias
                    substrate_bias = _str_to_float(latest_settings.get("substrate_bias", str(self.substrate_bias)))
                    if hasattr(self.pySMR_widget, 'on_substrate_bias_changed'):
                        self.pySMR_widget.on_substrate_bias_changed(substrate_bias)
                    
                    # Update PLL delay
                    pll_delay = _str_to_float(latest_settings.get("PLL_delay", "0.0"))
                    if hasattr(self.pySMR_widget, 'on_quick_pll_delay_changed'):
                        self.pySMR_widget.on_quick_pll_delay_changed(pll_delay)
                    
                    # Update PLL drive amplitude
                    pll_drive_amplitude = _str_to_float(latest_settings.get("PLL_drive_amplitude", "0.1"))
                    if hasattr(self.pySMR_widget, 'on_quick_pll_drive_amplitude_changed'):
                        self.pySMR_widget.on_quick_pll_drive_amplitude_changed(pll_drive_amplitude)
                    
                    # Update SMR settings widget if it exists
                    if hasattr(self.pySMR_widget, '_ensure_smr_settings_widget'):
                        widget = self.pySMR_widget._ensure_smr_settings_widget()
                        # Manually update ALL widget fields from settings (including calculated values)
                        try:
                            # Helper to convert string to appropriate type
                            def _str_to_bool_w(s: str) -> bool:
                                return s.lower() in ("true", "1", "yes", "on")
                            def _str_to_int_w(s: str) -> int:
                                try:
                                    return int(float(s))
                                except (ValueError, TypeError):
                                    return 0
                            def _str_to_float_w(s: str) -> float:
                                try:
                                    return float(s)
                                except (ValueError, TypeError):
                                    return 0.0
                            
                            # Block all signals to avoid triggering updates during bulk update
                            # Update all checkboxes
                            if hasattr(widget, 'run_check'):
                                widget.run_check.blockSignals(True)
                                widget.run_check.setChecked(_str_to_bool_w(latest_settings.get("Run", "False")))
                                widget.run_check.blockSignals(False)
                            if hasattr(widget, 'enable_agc_check'):
                                widget.enable_agc_check.blockSignals(True)
                                widget.enable_agc_check.setChecked(_str_to_bool_w(latest_settings.get("Enable_AGC", "True")))
                                widget.enable_agc_check.blockSignals(False)
                            if hasattr(widget, 'send_data_to_pc_check'):
                                widget.send_data_to_pc_check.blockSignals(True)
                                widget.send_data_to_pc_check.setChecked(_str_to_bool_w(latest_settings.get("Send_data_to_pc", "True")))
                                widget.send_data_to_pc_check.blockSignals(False)
                            if hasattr(widget, 'run_nco_at_fixed_freq_check'):
                                widget.run_nco_at_fixed_freq_check.blockSignals(True)
                                widget.run_nco_at_fixed_freq_check.setChecked(_str_to_bool_w(latest_settings.get("Run_NCO_at_fixed_freq", "False")))
                                widget.run_nco_at_fixed_freq_check.blockSignals(False)
                            if hasattr(widget, 'impulse_check'):
                                widget.impulse_check.blockSignals(True)
                                widget.impulse_check.setChecked(_str_to_bool_w(latest_settings.get("Impulse", "False")))
                                widget.impulse_check.blockSignals(False)
                            
                            # Update combo boxes (set by text)
                            if hasattr(widget, 'input_source_combo'):
                                input_source_str = str(latest_settings.get("Input_source", "channel_a"))
                                index = widget.input_source_combo.findText(input_source_str)
                                if index >= 0:
                                    widget.input_source_combo.blockSignals(True)
                                    widget.input_source_combo.setCurrentIndex(index)
                                    widget.input_source_combo.blockSignals(False)
                            if hasattr(widget, 'signal_of_interest_combo'):
                                soi_str = str(latest_settings.get("Signal_of_interest", "PLL Frequency"))
                                index = widget.signal_of_interest_combo.findText(soi_str)
                                if index >= 0:
                                    widget.signal_of_interest_combo.blockSignals(True)
                                    widget.signal_of_interest_combo.setCurrentIndex(index)
                                    widget.signal_of_interest_combo.blockSignals(False)
                            if hasattr(widget, 'dac_a_output_combo'):
                                dac_a_str = str(latest_settings.get("DAC_A_output", "off"))
                                index = widget.dac_a_output_combo.findText(dac_a_str)
                                if index >= 0:
                                    widget.dac_a_output_combo.blockSignals(True)
                                    widget.dac_a_output_combo.setCurrentIndex(index)
                                    widget.dac_a_output_combo.blockSignals(False)
                            if hasattr(widget, 'dac_b_output_combo'):
                                dac_b_str = str(latest_settings.get("DAC_B_output", "off"))
                                index = widget.dac_b_output_combo.findText(dac_b_str)
                                if index >= 0:
                                    widget.dac_b_output_combo.blockSignals(True)
                                    widget.dac_b_output_combo.setCurrentIndex(index)
                                    widget.dac_b_output_combo.blockSignals(False)
                            if hasattr(widget, 'pll_datarate_decimation'):
                                # Ensure we treat the value as text, not as an index
                                # The combo box items are ['1', '2', '4', '8', '16', '32']
                                # We need to match by text value, not by index
                                pll_rate_str = str(latest_settings.get("PLL_datarate_decimation", "1")).strip()
                                
                                # First try to match the text directly
                                index = widget.pll_datarate_decimation.findText(pll_rate_str)
                                if index >= 0:
                                    widget.pll_datarate_decimation.blockSignals(True)
                                    widget.pll_datarate_decimation.setCurrentIndex(index)
                                    widget.pll_datarate_decimation.blockSignals(False)
                                else:
                                    # If findText fails, try converting to integer (handles "4.0" -> "4")
                                    # This prevents accidentally using a numeric value as an index
                                    try:
                                        value_num = int(float(pll_rate_str))
                                        # Check if this numeric value exists in the combo box items
                                        value_str_from_num = str(value_num)
                                        index = widget.pll_datarate_decimation.findText(value_str_from_num)
                                        if index >= 0:
                                            widget.pll_datarate_decimation.blockSignals(True)
                                            widget.pll_datarate_decimation.setCurrentIndex(index)
                                            widget.pll_datarate_decimation.blockSignals(False)
                                        else:
                                            # Last resort: check if the value is a valid index (0-5)
                                            # but only use it if it matches the expected item at that index
                                            # This prevents the bug where index 4 (which is "16") is used when value is "4"
                                            if 0 <= value_num < widget.pll_datarate_decimation.count():
                                                item_at_index = widget.pll_datarate_decimation.itemText(value_num)
                                                if item_at_index == value_str_from_num:
                                                    # The index matches the value, so it's safe to use
                                                    widget.pll_datarate_decimation.blockSignals(True)
                                                    widget.pll_datarate_decimation.setCurrentIndex(value_num)
                                                    widget.pll_datarate_decimation.blockSignals(False)
                                                else:
                                                    print(f"Warning: PLL_datarate_decimation value '{pll_rate_str}' would map to index {value_num} "
                                                          f"(item '{item_at_index}'), but expected '{value_str_from_num}'. "
                                                          f"This suggests the CSV may have stored an index instead of a value.")
                                            else:
                                                print(f"Warning: PLL_datarate_decimation value '{pll_rate_str}' not found in combo box items.")
                                    except (ValueError, TypeError):
                                        print(f"Warning: Could not parse PLL_datarate_decimation value '{pll_rate_str}'.")
                            
                            # Update all numeric fields (including calculated values from sweep)
                            if hasattr(widget, 'smr_driver_id'):
                                widget.smr_driver_id.blockSignals(True)
                                widget.smr_driver_id.setValue(_str_to_int_w(latest_settings.get("smr_driver_id", "0")))
                                widget.smr_driver_id.blockSignals(False)
                            if hasattr(widget, 'frequency'):
                                widget.frequency.blockSignals(True)
                                widget.frequency.setValue(_str_to_float_w(latest_settings.get("Frequency", "1000000.0")))
                                widget.frequency.blockSignals(False)
                            if hasattr(widget, 'minimum_frequency'):
                                widget.minimum_frequency.blockSignals(True)
                                widget.minimum_frequency.setValue(_str_to_float_w(latest_settings.get("Minimum_frequency", "999000.0")))
                                widget.minimum_frequency.blockSignals(False)
                            if hasattr(widget, 'maximum_frequency'):
                                widget.maximum_frequency.blockSignals(True)
                                widget.maximum_frequency.setValue(_str_to_float_w(latest_settings.get("Maximum_frequency", "1001000.0")))
                                widget.maximum_frequency.blockSignals(False)
                            if hasattr(widget, 'cic_rate'):
                                widget.cic_rate.blockSignals(True)
                                widget.cic_rate.setValue(_str_to_int_w(latest_settings.get("CIC_rate", "32767")))
                                widget.cic_rate.blockSignals(False)
                            if hasattr(widget, 'cic_bit_shift'):
                                widget.cic_bit_shift.blockSignals(True)
                                widget.cic_bit_shift.setValue(_str_to_int_w(latest_settings.get("CIC_bit_shift", "16")))  # This is calculated from sweep
                                widget.cic_bit_shift.blockSignals(False)
                            if hasattr(widget, 'pll_delay'):
                                widget.pll_delay.blockSignals(True)
                                widget.pll_delay.setValue(_str_to_float_w(latest_settings.get("PLL_delay", "0.0")))
                                widget.pll_delay.blockSignals(False)
                            if hasattr(widget, 'pll_drive_amplitude'):
                                widget.pll_drive_amplitude.blockSignals(True)
                                widget.pll_drive_amplitude.setValue(_str_to_float_w(latest_settings.get("PLL_drive_amplitude", "0.1")))
                                widget.pll_drive_amplitude.blockSignals(False)
                            if hasattr(widget, 'feedback_delay'):
                                widget.feedback_delay.blockSignals(True)
                                widget.feedback_delay.setValue(_str_to_int_w(latest_settings.get("Feedback_delay", "0")))
                                widget.feedback_delay.blockSignals(False)
                            if hasattr(widget, 'feedback_gain'):
                                widget.feedback_gain.blockSignals(True)
                                widget.feedback_gain.setValue(_str_to_float_w(latest_settings.get("Feedback_gain", "0.1")))
                                widget.feedback_gain.blockSignals(False)
                            if hasattr(widget, 'resonator_q'):
                                widget.resonator_q.blockSignals(True)
                                widget.resonator_q.setValue(_str_to_float_w(latest_settings.get("Resonator_Q", "0.0")))  # From sweep
                                widget.resonator_q.blockSignals(False)
                            if hasattr(widget, 'loop_bandwidth'):
                                widget.loop_bandwidth.blockSignals(True)
                                widget.loop_bandwidth.setValue(_str_to_float_w(latest_settings.get("Loop_bandwidth", "10000000.0")))
                                widget.loop_bandwidth.blockSignals(False)
                            if hasattr(widget, 'loop_order'):
                                widget.loop_order.blockSignals(True)
                                widget.loop_order.setValue(_str_to_int_w(latest_settings.get("Loop_order", "1")))
                                widget.loop_order.blockSignals(False)
                            
                            # Trigger update to recalculate register values
                            if hasattr(widget, 'update_values'):
                                widget.update_values()
                        except Exception as e:
                            self._verbose_print(f"Warning: Error updating SMR settings widget: {e}")
                    
                    self._verbose_print("Updated pySMR GUI controls with new settings.")
                except Exception as e:
                    self._verbose_print(f"Warning: Error updating pySMR GUI controls: {e}")
            
            # Close both windows
            self.close()  # Close SweepWindow
            if self.parent_sweep_widget is not None:
                # Find and close the parent dialog if it exists
                # The parent_sweep_widget is SMRSweepControlWidget, its parent is the dialog
                parent_dialog = self.parent_sweep_widget.parent()
                if parent_dialog is not None and hasattr(parent_dialog, 'close'):
                    parent_dialog.close()
                # Also try to close the widget itself if it's a window
                elif hasattr(self.parent_sweep_widget, 'close'):
                    self.parent_sweep_widget.close()
            
        except Exception as e:
            self._verbose_print(f"Error pushing settings to SMR: {e}")
            import traceback
            traceback.print_exc()
    
    def _auto_proceed_to_set_delays(self) -> None:
        """Automatically proceed to set delays (called during automated setup)."""
        self._verbose_print("Automated setup: Automatically proceeding to set delays...")
        self._proceed_to_set_delays_impl()
    
    def _on_proceed_set_delays_clicked(self) -> None:
        """Handle Proceed to set delays button click."""
        self._proceed_to_set_delays_impl()
    
    def _proceed_to_set_delays_impl(self) -> None:
        """Implementation of proceeding to set delays."""
        try:
            # Check if pySMR_widget is available
            if self.pySMR_widget is None:
                self._verbose_print("Error: pySMR widget not available. Cannot proceed to set delays.")
                return
            
            # Read settings from CSV file
            settings_list = read_smr_settings()
            if not settings_list:
                self._verbose_print("Error: No settings found in CSV file. Cannot proceed to set delays.")
                return
            
            # Filter for settings with settings_type='sweep' and get the most recent one
            sweep_settings = [s for s in settings_list if s.get("settings_type", "").strip().lower() == "sweep"]
            if not sweep_settings:
                self._verbose_print("Error: No settings with settings_type='sweep' found in CSV file. Cannot proceed to set delays.")
                return
            
            # Sort by date and time (most recent first)
            def get_datetime_key(settings):
                date_str = settings.get("date", "0000-00-00")
                time_str = settings.get("time", "00:00:00")
                return (date_str, time_str)
            
            sweep_settings.sort(key=get_datetime_key, reverse=True)
            most_recent = sweep_settings[0]
            
            self._verbose_print(f"Proceeding to set delays with most recent sweep settings (set_bias=True)")
            
            # Ensure proper propagation of automated setup mode flag to pySMR_widget
            # This is CRITICAL because run_set_delays_with_settings checks this flag on the pySMR_widget
            # to decide whether to set the flag on the newly created SetDelaysWindow.
            # Without this, SetDelaysWindow won't know it's in automated setup and won't close automatically.
            if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
                self._verbose_print(f"Propagating automated setup flags to pySMR_widget before starting Set Delays")
                self.pySMR_widget._automated_setup_mode = True
                if hasattr(self, '_automated_setup_main_window'):
                    self.pySMR_widget._automated_setup_main_window = self._automated_setup_main_window
            
            # Call pySMR_widget's method to run set delays programmatically
            success = self.pySMR_widget.run_set_delays_with_settings(
                selected_settings=most_recent,
                set_bias=True
            )
            
            if success:
                self._verbose_print("Set delays started successfully.")
                # Close the Initialize SMR sweep window
                self.close()
                # If in automated setup mode, notify main window that sweep is complete
                # Set Delays has been started, so Stage 3 is complete and we're in Stage 4
                if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
                    if hasattr(self, '_automated_setup_main_window'):
                        # Notify that sweep completed and set delays started
                        # This transitions us from Stage 3 to Stage 4
                        QTimer.singleShot(1000, lambda: self._automated_setup_main_window._stage3_complete() if hasattr(self._automated_setup_main_window, '_stage3_complete') else None)
            else:
                self._verbose_print("Error: Failed to start set delays.")
                # If in automated setup mode and set delays failed, still mark stage 3 as complete
                # so we can proceed (even if Set Delays failed)
                if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
                    if hasattr(self, '_automated_setup_main_window'):
                        QTimer.singleShot(1000, lambda: self._automated_setup_main_window._stage3_complete() if hasattr(self._automated_setup_main_window, '_stage3_complete') else None)
                
        except Exception as e:
            self._verbose_print(f"Error proceeding to set delays: {e}")
            import traceback
            traceback.print_exc()
    
    def _save_smr_settings_from_sweep(
        self,
        smr_resonance_freq: float,
        smr_q: int,
        smr_amplitude: float
    ) -> None:
        """Save SMR settings from sweep results using SMR_settings_io.
        
        Args:
            smr_resonance_freq: SMR resonance frequency in Hz from Lorentzian fit
            smr_q: SMR Q factor from Lorentzian fit
            smr_amplitude: SMR amplitude from Lorentzian fit
        """
        try:
            # Load '[default_PLL]' section directly (without [default] base), fallback to '[default]' if not found
            try:
                # Load config file and get 'default_PLL' section directly
                if not os.path.exists(SMR_PARAMETERS_CONFIG_PATH):
                    raise FileNotFoundError(
                        f"SMR parameters config file not found: {SMR_PARAMETERS_CONFIG_PATH}"
                    )
                
                with open(SMR_PARAMETERS_CONFIG_PATH, mode="r", encoding="utf-8") as file:
                    content = file.read()
                config = parse_toml_config(content)
                
                # Get 'default_PLL' section directly (not as override to 'default')
                # Note: Section name uses underscore, not space
                params = config.get("default_PLL", {}).copy()
                
                if not params:
                    # If 'default_PLL' section is empty or doesn't exist, fall back to 'default'
                    self._verbose_print("Warning: '[default_PLL]' section not found or empty, using '[default]' section")
                    params = _load_smr_parameters(section="default")
                else:
                    self._verbose_print("Loaded parameters from '[default_PLL]' section")
            except Exception as e:  # pylint: disable=broad-except
                # Fallback to default section if any error occurs
                self._verbose_print(f"Warning: Error loading '[default_PLL]' section ({e}), using '[default]' section")
                params = _load_smr_parameters(section="default")
            
            # Map config parameters to FPGA parameter format expected by write_smr_settings
            # Helper function to convert string values to appropriate types
            def _to_bool(val: Any) -> bool:
                if isinstance(val, bool):
                    return val
                if isinstance(val, (int, float)):
                    return bool(val)
                if isinstance(val, str):
                    lower = val.strip().lower()
                    if lower in ("true", "1", "yes", "on"):
                        return True
                    if lower in ("false", "0", "no", "off"):
                        return False
                return False
            
            # Get PLL drive magnitude from config
            pll_drive_magnitude = float(params.get("pll_drive_amplitude", 0.1))
            
            # Get sweep drive amplitude (the drive setting used in the sweep)
            sweep_drive_amplitude = self.sweep_amplitude
            
            # Calculate amplitude_factor: log2([PLL_drive_magnitude]/[Sweep_drive_amplitude])*([SMR_amplitude]/[1.5E9])
            # Note: Using math.log2 for base-2 logarithm
            if sweep_drive_amplitude > 0 and pll_drive_magnitude > 0:
                ratio = pll_drive_magnitude / sweep_drive_amplitude
                amplitude_ratio = smr_amplitude / 1.5e9
                amplitude_factor = math.log2(ratio) * amplitude_ratio
                self._verbose_print(f"Calculated amplitude_factor: {amplitude_factor:.6f} "
                      f"(PLL_drive_magnitude={pll_drive_magnitude}, "
                      f"Sweep_drive_amplitude={sweep_drive_amplitude}, "
                      f"SMR_amplitude={smr_amplitude:.2e})")
            else:
                # Fallback if division by zero or invalid values
                amplitude_factor = 0.0
                self._verbose_print(f"Warning: Invalid values for amplitude_factor calculation. "
                      f"Using default value 0.0")
            
            # Get CIC_rate from config
            cic_rate = int(params.get("cic_rate", 32767))
            
            # Calculate CIC_rate_factor: log2([CIC_rate]/(2^15))*3+24
            # Note: 2^15 = 32768
            if cic_rate > 0:
                cic_rate_normalized = cic_rate / (2**15)  # Divide by 2^15 = 32768
                cic_rate_factor = math.log2(cic_rate_normalized) * 3 + 24
                self._verbose_print(f"Calculated CIC_rate_factor: {cic_rate_factor:.6f} "
                      f"(CIC_rate={cic_rate})")
            else:
                # Fallback if invalid CIC_rate
                cic_rate_factor = 24.0  # Default to 24 if CIC_rate is invalid
                self._verbose_print(f"Warning: Invalid CIC_rate value ({cic_rate}). "
                      f"Using default CIC_rate_factor 24.0")
            
            # Calculate CIC_bit_shift: roundup(CIC_rate_factor + amplitude_factor + 1)
            # Note: Using math.ceil() for rounding up
            cic_bit_shift = int(math.ceil(cic_rate_factor + amplitude_factor + 1))
            self._verbose_print(f"Calculated CIC_bit_shift: {cic_bit_shift} "
                  f"(CIC_rate_factor={cic_rate_factor:.6f}, "
                  f"amplitude_factor={amplitude_factor:.6f})")
            
            # Get string values for combo box parameters (use as-is from config)
            input_source_str = str(params.get("input_source", "channel_a"))
            dac_a_output_str = str(params.get("dac_a_output", "off"))
            dac_b_output_str = str(params.get("dac_b_output", "off"))
            soi_str = str(params.get("signal_of_interest", "PLL Frequency"))
            pll_rate_str = str(params.get("pll_datarate_decimation", "1"))
            
            # Build FPGA parameters dictionary
            # Note: Combo box parameters are saved as strings (matching SMR_settings_io widget behavior)
            fpga_parameters: Dict[str, Any] = {
                "smr_driver_id": int(params.get("smr_driver_id", 0)),
                "Run": _to_bool(params.get("run", False)),
                "Enable_AGC": _to_bool(params.get("enable_agc", True)),
                "Send_data_to_pc": _to_bool(params.get("send_data_to_pc", True)),
                "Run_NCO_at_fixed_freq": _to_bool(params.get("run_nco_at_fixed_freq", False)),
                "Impulse": _to_bool(params.get("impulse", False)),
                "Input_source": input_source_str,
                "Signal_of_interest": soi_str,
                "DAC_A_output": dac_a_output_str,
                "DAC_B_output": dac_b_output_str,
                "PLL_datarate_decimation": pll_rate_str,
                "Frequency": float(smr_resonance_freq),  # From Lorentzian fit
                "Minimum_frequency": float(0.99 * smr_resonance_freq),  # 0.99 * Frequency
                "Maximum_frequency": float(1.01 * smr_resonance_freq),  # 1.01 * Frequency
                "CIC_rate": cic_rate,
                "CIC_bit_shift": cic_bit_shift,  # Calculated: roundup(CIC_rate_factor + amplitude_factor + 1)
                "PLL_delay": float(params.get("pll_delay", 0.0)),
                "PLL_drive_amplitude": float(pll_drive_magnitude),
                "Feedback_delay": int(params.get("feedback_delay", 0)),
                "Feedback_gain": float(params.get("feedback_gain", 0.1)),
                "Resonator_Q": float(smr_q),  # From Lorentzian fit (Q = center / FWHM)
                "Loop_bandwidth": float(params.get("loop_bandwidth", 10000000.0)),
                "Loop_order": int(params.get("loop_order", 1)),
            }
            
            # Save settings using SMR_settings_io
            success = write_smr_settings(
                settings_type="sweep",
                substrate_bias=self.substrate_bias,
                fpga_parameters=fpga_parameters,
                operator=self.operator  # Pass operator value
            )
            
            if success:
                self._verbose_print("SMR settings saved successfully from sweep results.")
                # Enable the push settings button and proceed to set delays button
                if hasattr(self, 'push_settings_button'):
                    self.push_settings_button.setEnabled(True)
                if hasattr(self, 'proceed_set_delays_button'):
                    self.proceed_set_delays_button.setEnabled(True)
                
                # If in automated setup mode, automatically proceed to set delays
                if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
                    # Wait a brief moment for settings to be fully saved, then proceed
                    QTimer.singleShot(500, self._auto_proceed_to_set_delays)
            else:
                self._verbose_print("Warning: Failed to save SMR settings from sweep results.")
                
        except Exception as e:
            self._verbose_print(f"Error saving SMR settings from sweep: {e}")
            import traceback
            traceback.print_exc()
    
    def _save_sweep_results(
        self,
        smr_resonance_freq: float,
        smr_q: int,
        smr_amplitude: float
    ) -> None:
        """Save fine sweep results to TSV file.
        
        Args:
            smr_resonance_freq: SMR resonance frequency in Hz
            smr_q: SMR Q factor
            smr_amplitude: SMR detection amplitude
        """
        try:
            # Get current date and time, format as YYYYMMDDHHMM (24-hour time)
            now = datetime.now()
            timestamp_str = now.strftime("%Y%m%d%H%M")
            
            # Get chip name and system name from config
            chip_name, system_name = _get_chip_and_system_name()
            
            # Get devices_path from config
            devices_path = _get_devices_path()
            
            # Validate required values
            if not chip_name:
                self._verbose_print("Warning: Chip name not found. Cannot save sweep results.")
                return
            
            if not system_name:
                self._verbose_print("Warning: System name not found. Cannot save sweep results.")
                return
            
            if not devices_path:
                self._verbose_print("Warning: devices_path not found in config. Cannot save sweep results.")
                return
            
            # Use empty string if values not found (for data row)
            chip_name_str = chip_name if chip_name else ""
            system_name_str = system_name if system_name else ""
            
            # Get sweep parameters
            sweep_drive_magnitude = self.sweep_amplitude
            substrate_bias_value = self.substrate_bias
            
            # Prepare metadata headers and values
            metadata_headers = ["timestamp", "chip", "system", "sweep_drive_magnitude", "substrate_bias", 
                               "Frequency (Hz)", "Q", "Detection amplitude"]
            metadata_values = [
                timestamp_str,
                chip_name_str,
                system_name_str,
                str(sweep_drive_magnitude),
                str(substrate_bias_value),
                str(round(smr_resonance_freq)),
                str(smr_q),
                f"{smr_amplitude:.2e}"
            ]
            
            # Prepare data column headers
            data_headers = ["ChA Frequency", "ChA Magnitude", "ChA Phase"]
            
            # Get fine sweep raw data
            fine_freq_data = self.fine_frequency_data if hasattr(self, 'fine_frequency_data') else []
            fine_mag_data = self.fine_magnitude_data if hasattr(self, 'fine_magnitude_data') else []
            fine_phase_data = self.fine_phase_data if hasattr(self, 'fine_phase_data') else []
            
            # Determine number of data points (use the length of the longest array)
            num_data_points = max(len(fine_freq_data), len(fine_mag_data), len(fine_phase_data))
            
            # Build file path: [devices_path]/[chip_name]/[chip_name]_[system_name]_[timestring].tsv
            # Create directory structure if it doesn't exist
            chip_dir = os.path.join(devices_path, chip_name)
            os.makedirs(chip_dir, exist_ok=True)
            
            # Build filename: [chip_name]_[system_name]_[timestring].tsv
            # Sanitize system_name for filename (replace spaces and special chars with underscores)
            safe_system_name = system_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            filename = f"{chip_name}_{safe_system_name}_{timestamp_str}.tsv"
            output_file = os.path.join(chip_dir, filename)
            
            # Write to TSV file (write mode - fresh file each time due to new structure)
            with open(output_file, mode='w', encoding='utf-8', newline='') as tsv_file:
                writer = csv.writer(tsv_file, delimiter='\t')
                
                # Row 1: Metadata column headers
                writer.writerow(metadata_headers)
                
                # Row 2: Metadata values
                writer.writerow(metadata_values)
                
                # Row 3: Data column headers
                writer.writerow(data_headers)
                
                # Rows 4+: Fine sweep data (one row per data point)
                if num_data_points > 0:
                    for i in range(num_data_points):
                        # Get data point values (use None if index out of range)
                        freq_val = fine_freq_data[i] if i < len(fine_freq_data) else None
                        mag_val = fine_mag_data[i] if i < len(fine_mag_data) else None
                        phase_val = fine_phase_data[i] if i < len(fine_phase_data) else None
                        
                        # Format to 5 decimal places (if value exists)
                        if freq_val is not None:
                            freq_str = f"{freq_val:.5f}"
                        else:
                            freq_str = ""
                        
                        if mag_val is not None:
                            mag_str = f"{mag_val:.5f}"
                        else:
                            mag_str = ""
                        
                        if phase_val is not None:
                            phase_str = f"{phase_val:.5f}"
                        else:
                            phase_str = ""
                        
                        # Write data row with just the three data columns
                        data_row = [freq_str, mag_str, phase_str]
                        writer.writerow(data_row)
            
            self._verbose_print(f"Sweep results saved to: {output_file}")
            
        except Exception as e:
            self._verbose_print(f"Error saving sweep results: {e}")
            import traceback
            traceback.print_exc()


def main() -> None:
    """Standalone entry point for SMR sweep control."""
    # 1. Initialize TCP connection.
    tcp_success, tcp_socket, tcp_msg = initialize_tcp_connection()
    print(tcp_msg)

    # 2. Initialize UDP connection.
    udp_success, udp_socket, udp_msg = initialize_udp_connection()
    print(udp_msg)

    # 3. Load SMR parameters and build SetAllParametersString.
    try:
        set_all_string = build_sweep_parameters_string(section="sweep")
        print("Generated SetAllParametersString for sweep parameters:")
        print(set_all_string)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Error building sweep parameters string: {exc}")

    # 4. Show simple control UI.
    app = QApplication(sys.argv)
    window = QWidget()
    window.setWindowTitle("SMR Sweep Frequencies")
    window_layout = QVBoxLayout(window)

    # Create status messages with checkmarks/X marks
    tcp_indicator = "✓" if tcp_success else "✗"
    udp_indicator = "✓" if udp_success else "✗"
    tcp_status = "OK" if tcp_success else "FAILED"
    udp_status = "OK" if udp_success else "FAILED"
    
    # Use HTML for rich text formatting with colored indicators
    info_label = QLabel()
    info_label.setTextFormat(Qt.RichText)
    info_html = (
        "SMR sweep connections initialized.<br>"
        f"TCP: <span style='color: {'green' if tcp_success else 'red'}; font-weight: bold;'>{tcp_indicator} {tcp_status}</span><br>"
        f"UDP: <span style='color: {'green' if udp_success else 'red'}; font-weight: bold;'>{udp_indicator} {udp_status}</span><br><br>"
        "Adjust sweep parameters below (sweep logic not yet implemented)."
    )
    info_label.setText(info_html)
    info_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    
    window_layout.addWidget(info_label)

    # Pass TCP and UDP sockets to the widget (only if connections were successful)
    tcp_sock = tcp_socket if tcp_success else None
    udp_sock = udp_socket if udp_success else None
    sweep_widget = SMRSweepControlWidget(tcp_socket=tcp_sock, udp_socket=udp_sock)
    window_layout.addWidget(sweep_widget)

    window.resize(500, 300)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()