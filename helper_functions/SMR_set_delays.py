"""SMR set delays helper module.

This module provides functionality to:
1. Display a dialog with 'Set Bias?' toggle and options to select settings
2. Check for existing TCP connection and use it to send data to the FPGA
3. Load selected settings (either manually selected or most recent sweep)
4. Override with values from smr_parameters_config.txt under the '[set_delays]' heading
5. Iterate through pll_delay values (0.0, 0.25, 0.5, 0.75, with 1.0 using 0.0 result)
6. For each delay value:
   - Send it to the FPGA
   - Wait 200ms
   - Monitor the UDP data stream measuring the magnitude (signal_of_interest='Magnitude')
   - Plot that delay value in a scatter plot
7. Perform binary search refinement to find optimal delay
8. Calculate and set final cic_bit_shift value
9. Save final settings to CSV with type='setDelays'
"""

import os
import sys
import time
import queue
import math
from typing import Any, Dict, Tuple, Optional, List

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QTimer
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False

import numpy as np

# Try to import nidaqmx for DAQ control
try:
    import nidaqmx
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False
    nidaqmx = None

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
    get_daq_info,
)
from helper_functions.SMR_settings_io import (  # noqa: E402
    read_smr_settings,
    LoadSettingsDialog,
    write_smr_settings,
)
from helper_functions.SMR_sweep_frequencies import (  # noqa: E402
    _load_smr_connection_config,
    test_existing_tcp_connection,
    initialize_tcp_connection,
    initialize_udp_connection,
    _load_smr_parameters,
    _map_parameters_to_register_args,
    generate_set_all_parameters_string,
)

REFERENCES_DIR = os.path.join(_PROJECT_ROOT, "references")
SMR_PARAMETERS_CONFIG_PATH = os.path.join(REFERENCES_DIR, "smr_parameters_config.txt")


class SetDelaysOptionsDialog(QDialog):
    """Dialog for Set Delays options: Set Bias toggle and settings selection method."""
    
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.parent_widget = parent  # Store reference to pySMR widget
        self.set_bias = True
        self.use_most_recent_sweep = False
        self.selected_settings: Optional[Dict[str, str]] = None
        self.setup_ui()
        self._update_view_results_button_visibility()
    
    def setup_ui(self) -> None:
        """Set up the dialog UI."""
        self.setWindowTitle("Set Delays Options")
        self.setMinimumSize(400, 200)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Set Bias checkbox
        self.set_bias_checkbox = QCheckBox("Set Bias?")
        self.set_bias_checkbox.setChecked(True)  # Default to checked/True
        self.set_bias_checkbox.setStyleSheet("""
            QCheckBox {
                font-size: 12pt;
                font-weight: bold;
                padding: 10px;
            }
            QCheckBox::indicator {
                width: 30px;
                height: 30px;
                border: 2px solid #999;
                border-radius: 5px;
                background-color: #808080;
            }
            QCheckBox::indicator:checked {
                background-color: #2196F3;
                border: 2px solid #1976D2;
            }
            QCheckBox::indicator:unchecked {
                background-color: #808080;
                border: 2px solid #666666;
            }
        """)
        layout.addWidget(self.set_bias_checkbox)
        
        # Buttons layout
        buttons_layout = QVBoxLayout()
        buttons_layout.setSpacing(10)
        
        # Manually select settings file button
        self.manual_select_button = QPushButton("Manually select settings file")
        self.manual_select_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 12px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:pressed {
                background-color: #1565C0;
            }
        """)
        self.manual_select_button.clicked.connect(self.on_manual_select_clicked)
        buttons_layout.addWidget(self.manual_select_button)
        
        # Use most recent sweep button
        self.use_recent_button = QPushButton("Use most recent sweep")
        self.use_recent_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 12px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        self.use_recent_button.clicked.connect(self.on_use_recent_clicked)
        buttons_layout.addWidget(self.use_recent_button)
        
        layout.addLayout(buttons_layout)
        
        # View latest results button (only show if results exist)
        self.view_results_button = QPushButton("View latest results")
        self.view_results_button.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 12px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
            QPushButton:pressed {
                background-color: #E65100;
            }
        """)
        self.view_results_button.clicked.connect(self._on_view_results_clicked)
        self.view_results_button.hide()  # Hidden by default
        layout.addWidget(self.view_results_button)
        
        layout.addStretch()
        
        # Cancel button
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def on_manual_select_clicked(self) -> None:
        """Handle manual select button click."""
        # Read saved settings
        settings_list = read_smr_settings()
        if not settings_list:
            QMessageBox.warning(
                self,
                "No Settings Found",
                "No saved settings found. Please save a sweep setting first."
            )
            return
        
        # Show dialog to select settings
        dialog = LoadSettingsDialog(settings_list, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.get_selected_settings()
            if selected:
                self.set_bias = self.set_bias_checkbox.isChecked()
                self.use_most_recent_sweep = False
                self.selected_settings = selected
                self.accept()
    
    def on_use_recent_clicked(self) -> None:
        """Handle use most recent sweep button click."""
        # Read saved settings
        settings_list = read_smr_settings()
        if not settings_list:
            QMessageBox.warning(
                self,
                "No Settings Found",
                "No saved settings found. Please save a sweep setting first."
            )
            return
        
        # Find most recent sweep (settings_type contains 'sweep', sorted by date and time)
        sweep_settings = [
            s for s in settings_list
            if s.get("settings_type", "").lower() == "sweep"
        ]
        
        if not sweep_settings:
            QMessageBox.warning(
                self,
                "No Sweep Settings Found",
                "No sweep settings found in the CSV file. Please save a sweep setting first."
            )
            return
        
        # Sort by date and time (most recent first)
        # Date format: YYYY-MM-DD, Time format: HH:MM:SS
        def get_datetime_key(settings: Dict[str, str]) -> Tuple[str, str]:
            date_str = settings.get("date", "0000-00-00")
            time_str = settings.get("time", "00:00:00")
            return (date_str, time_str)
        
        sweep_settings.sort(key=get_datetime_key, reverse=True)
        most_recent = sweep_settings[0]
        
        self.set_bias = self.set_bias_checkbox.isChecked()
        self.use_most_recent_sweep = True
        self.selected_settings = most_recent
        self.accept()
    
    def _update_view_results_button_visibility(self) -> None:
        """Update visibility of View latest results button based on whether results exist."""
        has_results = False
        if self.parent_widget is not None:
            if (hasattr(self.parent_widget, 'set_delays_window') and 
                self.parent_widget.set_delays_window is not None):
                # Check if the window has been run (has delay_magnitude_map data)
                if hasattr(self.parent_widget.set_delays_window, 'delay_magnitude_map'):
                    has_results = len(self.parent_widget.set_delays_window.delay_magnitude_map) > 0
        
        if hasattr(self, 'view_results_button'):
            self.view_results_button.setVisible(has_results)
    
    def _on_view_results_clicked(self) -> None:
        """Handle View latest results button click for Set Delays."""
        if self.parent_widget is not None:
            if (hasattr(self.parent_widget, 'set_delays_window') and 
                self.parent_widget.set_delays_window is not None):
                self.parent_widget.set_delays_window.show()
                self.parent_widget.set_delays_window.raise_()
                self.parent_widget.set_delays_window.activateWindow()
    
    def get_result(self) -> Tuple[bool, Dict[str, str]]:
        """Get dialog result.
        
        Returns:
            Tuple of (set_bias, selected_settings)
        """
        return (self.set_bias, self.selected_settings)


def _load_set_delays_overrides() -> Dict[str, Any]:
    """Load override parameters from [set_delays] section of config file.
    
    Returns:
        Dictionary of parameter overrides from [set_delays] section.
    """
    if not os.path.exists(SMR_PARAMETERS_CONFIG_PATH):
        print(f"Warning: Config file not found: {SMR_PARAMETERS_CONFIG_PATH}")
        return {}
    
    try:
        with open(SMR_PARAMETERS_CONFIG_PATH, mode="r", encoding="utf-8") as file:
            content = file.read()
        config = parse_toml_config(content)
        overrides = config.get("set_delays", {}).copy()
        return overrides
    except Exception as e:
        print(f"Warning: Error loading [set_delays] section: {e}")
        import traceback
        traceback.print_exc()
        return {}


def _apply_set_delays_overrides(base_params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply [set_delays] overrides to base parameters.
    
    Args:
        base_params: Base parameters dictionary (from selected sweep setting)
        
    Returns:
        Dictionary with overrides applied.
    """
    overrides = _load_set_delays_overrides()
    result = base_params.copy()
    result.update(overrides)
    # Ensure run is always true for delay sweep
    result["run"] = True
    return result


def _convert_params_to_settings_dict(params: Dict[str, Any]) -> Dict[str, str]:
    """Convert parameters dictionary to settings dictionary format (capitalized keys, string values).
    
    Args:
        params: Parameters dictionary with lowercase keys
        
    Returns:
        Settings dictionary with capitalized keys and string values
    """
    settings_dict = {}
    for key, value in params.items():
        # Convert lowercase keys to capitalized format
        if key == "run":
            settings_dict["Run"] = str(value)
        elif key == "enable_agc":
            settings_dict["Enable_AGC"] = str(value)
        elif key == "send_data_to_pc":
            settings_dict["Send_data_to_pc"] = str(value)
        elif key == "run_nco_at_fixed_freq":
            settings_dict["Run_NCO_at_fixed_freq"] = str(value)
        elif key == "impulse":
            settings_dict["Impulse"] = str(value)
        elif key == "input_source":
            settings_dict["Input_source"] = str(value)
        elif key == "signal_of_interest":
            settings_dict["Signal_of_interest"] = str(value)
        elif key == "dac_a_output":
            settings_dict["DAC_A_output"] = str(value)
        elif key == "dac_b_output":
            settings_dict["DAC_B_output"] = str(value)
        elif key == "pll_datarate_decimation":
            settings_dict["PLL_datarate_decimation"] = str(value)
        elif key == "frequency":
            settings_dict["Frequency"] = str(value)
        elif key == "minimum_frequency":
            settings_dict["Minimum_frequency"] = str(value)
        elif key == "maximum_frequency":
            settings_dict["Maximum_frequency"] = str(value)
        elif key == "cic_rate":
            settings_dict["CIC_rate"] = str(value)
        elif key == "cic_bit_shift":
            settings_dict["CIC_bit_shift"] = str(value)
        elif key == "pll_delay":
            settings_dict["PLL_delay"] = str(value)
        elif key == "pll_drive_amplitude":
            settings_dict["PLL_drive_amplitude"] = str(value)
        elif key == "feedback_delay":
            settings_dict["Feedback_delay"] = str(value)
        elif key == "feedback_gain":
            settings_dict["Feedback_gain"] = str(value)
        elif key == "resonator_q":
            settings_dict["Resonator_Q"] = str(value)
        elif key == "loop_bandwidth":
            settings_dict["Loop_bandwidth"] = str(value)
        elif key == "loop_order":
            settings_dict["Loop_order"] = str(value)
        elif key == "smr_driver_id":
            settings_dict["smr_driver_id"] = str(value)
    return settings_dict


def _convert_settings_dict_to_params(settings_dict: Dict[str, str]) -> Dict[str, Any]:
    """Convert settings dictionary from CSV to parameter dictionary.
    
    Args:
        settings_dict: Dictionary from CSV row (all values are strings)
        
    Returns:
        Dictionary with converted types suitable for _map_parameters_to_register_args
    """
    def _str_to_bool(s: str) -> bool:
        return s.lower() in ("true", "1", "yes", "on")
    
    def _str_to_int(s: str) -> int:
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return 0
    
    def _str_to_float(s: str) -> float:
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0
    
    # Convert all values to appropriate types
    params = {}
    for key, value in settings_dict.items():
        if key in ["Run", "Enable_AGC", "Send_data_to_pc", "Run_NCO_at_fixed_freq", "Impulse"]:
            params[key.lower()] = _str_to_bool(value)
        elif key in ["smr_driver_id", "CIC_rate", "CIC_bit_shift", "Feedback_delay", "Loop_order"]:
            params[key.lower()] = _str_to_int(value)
        elif key in ["Frequency", "Minimum_frequency", "Maximum_frequency", "PLL_delay",
                     "PLL_drive_amplitude", "Feedback_gain", "Resonator_Q", "Loop_bandwidth"]:
            params[key.lower()] = _str_to_float(value)
        elif key in ["Input_source", "Signal_of_interest", "DAC_A_output", "DAC_B_output",
                     "PLL_datarate_decimation"]:
            params[key.lower()] = str(value)
        else:
            # Keep other fields as strings
            params[key.lower()] = str(value)
    
    return params


def _send_params_with_run_state(
    tcp_queue: FPGACommandQueue,
    params: Dict[str, Any],
    smr_driver_id: int,
    run_state: bool,
    pySMR_widget: Optional[Any] = None
) -> bool:
    """Send full parameter set to FPGA with specified run state.
    
    Args:
        tcp_queue: TCP command queue instance
        params: Parameters dictionary (already has [set_delays] overrides and pll_delay)
        smr_driver_id: SMR driver ID
        run_state: True to set run=True, False to set run=False
        pySMR_widget: Optional reference to pySMR widget for updating controls
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Copy params and set run state
        params_to_send = params.copy()
        params_to_send["run"] = run_state
        
        # Map to register args and calculate register values
        args, _ = _map_parameters_to_register_args(params_to_send)
        register_values = calculate_register_values(**args)
        
        # Generate full SetAllParametersString
        set_all_string = generate_set_all_parameters_string(register_values, smr_driver_id)
        
        # Send all commands to FPGA
        futures = []
        for line in set_all_string.split("\n"):
            if line.strip():
                command = line.strip() + "\r\n"
                future = tcp_queue.submit_command(command=command, wait_response=True, timeout=1.0)
                futures.append(future)
        
        # Wait for all commands to complete
        all_success = True
        for future in futures:
            try:
                success, response = future.result(timeout=2.0)
                if not success:
                    all_success = False
            except Exception:
                all_success = False
        
        # Update SMR settings controls if pySMR_widget is provided and run=True
        if pySMR_widget is not None and all_success and run_state:
            try:
                # Convert params dict to format expected by _load_settings_into_widget
                settings_dict = _convert_params_to_settings_dict(params_to_send)
                
                # Update the SMR settings widget with current parameters
                if hasattr(pySMR_widget, '_ensure_smr_settings_widget') and hasattr(pySMR_widget, '_load_settings_into_widget'):
                    widget = pySMR_widget._ensure_smr_settings_widget()
                    # Use the existing method to load all parameters
                    pySMR_widget._load_settings_into_widget(widget, settings_dict)
                    
                    # Also update quick controls if they exist
                    if hasattr(pySMR_widget, '_sync_quick_controls_from_widget'):
                        pySMR_widget._sync_quick_controls_from_widget()
            except Exception as e:
                print(f"Warning: Error updating SMR settings controls: {e}")
                import traceback
                traceback.print_exc()
        
        return all_success
    except Exception as e:
        print(f"Error sending parameters to FPGA: {e}")
        return False


def _send_pll_delay_to_fpga(
    tcp_queue: FPGACommandQueue,
    pll_delay: float,
    base_params: Dict[str, Any],
    smr_driver_id: int,
    pySMR_widget: Optional[Any] = None
) -> bool:
    """Send full parameter set with specific pll_delay value to FPGA.
    
    This function:
    1. Takes base_params (selected settings)
    2. Applies [set_delays] overrides
    3. Applies the current delay value
    4. Sends ALL parameters to FPGA (full SetAllParametersString)
    5. Updates SMR settings controls if pySMR_widget is provided
    
    Args:
        tcp_queue: TCP command queue instance
        pll_delay: PLL delay value to set
        base_params: Base parameters dictionary (selected settings, already has [set_delays] overrides)
        smr_driver_id: SMR driver ID
        pySMR_widget: Optional reference to pySMR widget for updating controls
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Start with base params (which already has [set_delays] overrides applied)
        params = base_params.copy()
        # Update with current delay value
        params["pll_delay"] = pll_delay
        
        # Send with run=True
        return _send_params_with_run_state(tcp_queue, params, smr_driver_id, True, pySMR_widget)
    except Exception as e:
        print(f"Error sending pll_delay to FPGA: {e}")
        return False


class SetDelaysWindow(QMainWindow):
    """Window for displaying delay sweep results with scatter plot."""
    
    def __init__(
        self,
        tcp_queue: Optional[FPGACommandQueue] = None,
        udp_manager: Optional[UDPDataManager] = None,
        parent: Optional[QWidget] = None,
        pySMR_widget: Optional[Any] = None
    ) -> None:
        super().__init__(parent)
        self.tcp_queue = tcp_queue
        self.udp_manager = udp_manager
        self.pySMR_widget = pySMR_widget
        # Initial delays to test: 0, 0.25, 0.5, 0.75 (delay 1.0 will use delay 0.0 result)
        self.initial_delay_values = [0.0, 0.25, 0.5, 0.75]
        self.delay_values: List[float] = []  # Will be populated with all delays to test
        self.delay_0_magnitude: Optional[float] = None  # Store magnitude for delay=0 to use for delay=1
        # Track delay -> mean_magnitude mapping for all tested delays
        self.delay_magnitude_map: Dict[float, float] = {}
        self.current_delay_index = 0
        self.settings_params: Optional[Dict[str, Any]] = None
        self.smr_driver_id = 0
        self.udp_subscriber_id: Optional[int] = None
        self.udp_sub_queue = None
        self.udp_magnitudes: List[float] = []  # Magnitude values collected from UDP
        self.udp_start_time: Optional[float] = None
        self.udp_timer = QTimer()
        self.udp_timer.timeout.connect(self._check_udp_data)
        # Binary search state
        self.binary_search_iteration = 0
        self.binary_search_max_iterations = 5
        self.binary_search_low = 0.0
        self.binary_search_high = 0.0
        # Progress tracking
        self.total_delays_per_voltage = 4 + 5  # 4 initial + 5 binary search iterations
        self.total_voltages = 10  # 0.5V to 5V in 0.5V steps
        # Bias voltage sweep state
        self.set_bias = False
        self.bias_voltages: List[float] = []  # [0.5, 1.0, 1.5, ..., 5.0]
        self.current_bias_index = 0
        self.bias_results: Dict[float, Dict[str, Any]] = {}  # bias -> {best_delay, best_magnitude, cic_bit_shift, rmse}
        self.daq_name: Optional[str] = None
        self.substrate_bias_address: Optional[str] = None
        self.rmse_frequencies: List[float] = []  # For RMSE measurement
        self.rmse_start_time: Optional[float] = None
        self.rmse_timer = QTimer()
        self.rmse_timer.timeout.connect(self._check_rmse_data)
        # Track delay_magnitude_map per bias voltage for plotting
        self.bias_delay_maps: Dict[float, Dict[float, float]] = {}  # bias -> {delay -> magnitude}
        self.current_bias_voltage: Optional[float] = None
        self._automated_setup_mode = False
        self._automated_setup_main_window = None
        self._load_daq_info()
        self.setup_ui()
        
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
        except Exception:
            pass
    
    def _set_substrate_bias_voltage(self, voltage: float) -> None:
        """Set substrate bias voltage on DAQ analog output and update pySMR control.
        
        Args:
            voltage: Voltage value in volts to set on the analog output.
        """
        if not NIDAQMX_AVAILABLE:
            print(f"Warning: nidaqmx not available, cannot set substrate bias voltage")
            return

        if self.daq_name is None or self.substrate_bias_address is None:
            print(f"Warning: DAQ info not available (daq_name={self.daq_name}, substrate_bias_address={self.substrate_bias_address})")
            return

        try:
            # Construct full channel name (e.g., "Dev1/ao0")
            channel_name = f"{self.daq_name}/{self.substrate_bias_address}"

            with nidaqmx.Task() as task:
                # Add analog output channel
                task.ao_channels.add_ao_voltage_chan(channel_name)
                # Write voltage value
                task.write(voltage)
            
            # Update pySMR widget's substrate bias control if available
            if self.pySMR_widget is not None:
                # Try to update substrate_bias_control
                if hasattr(self.pySMR_widget, 'substrate_bias_control'):
                    # Temporarily disable callback to avoid triggering DAQ update (we just set it)
                    old_callback = getattr(self.pySMR_widget.substrate_bias_control, '_value_changed_callback', None)
                    self.pySMR_widget.substrate_bias_control._value_changed_callback = None
                    self.pySMR_widget.substrate_bias_control.set_value(voltage)
                    self.pySMR_widget.substrate_bias_control._value_changed_callback = old_callback
                # Also try quick_substrate_bias_control if it exists
                elif hasattr(self.pySMR_widget, 'quick_substrate_bias_control'):
                    old_callback = getattr(self.pySMR_widget.quick_substrate_bias_control, '_value_changed_callback', None)
                    self.pySMR_widget.quick_substrate_bias_control._value_changed_callback = None
                    self.pySMR_widget.quick_substrate_bias_control.set_value(voltage)
                    self.pySMR_widget.quick_substrate_bias_control._value_changed_callback = old_callback
        except Exception as e:
            print(f"Error setting substrate bias voltage: {e}")
    
    def setup_ui(self) -> None:
        """Set up the user interface."""
        self.setWindowTitle("SMR Set Delays")
        self.setGeometry(100, 100, 1200, 600)  # Wider for two plots
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # Progress bars section
        progress_section = QWidget()
        progress_layout = QVBoxLayout(progress_section)
        progress_layout.setContentsMargins(10, 10, 10, 10)
        progress_layout.setSpacing(10)
        
        # Current voltage progress bar (shown for both single and bias sweep modes)
        self.voltage_progress_label = QLabel("Progress:")
        self.voltage_progress_label.setStyleSheet("font-size: 11pt; font-weight: bold;")
        progress_layout.addWidget(self.voltage_progress_label)
        
        self.voltage_progress_bar = QProgressBar()
        self.voltage_progress_bar.setRange(0, 100)
        self.voltage_progress_bar.setValue(0)
        self.voltage_progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #999;
                border-radius: 5px;
                text-align: center;
                font-size: 10pt;
                height: 25px;
            }
            QProgressBar::chunk {
                background-color: #2196F3;
                border-radius: 3px;
            }
        """)
        progress_layout.addWidget(self.voltage_progress_bar)
        
        # Overall progress bar (only shown if set_bias is True)
        self.overall_progress_label = QLabel("Overall progress:")
        self.overall_progress_label.setStyleSheet("font-size: 11pt; font-weight: bold;")
        self.overall_progress_label.hide()  # Hidden by default
        progress_layout.addWidget(self.overall_progress_label)
        
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.overall_progress_bar.setValue(0)
        self.overall_progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #999;
                border-radius: 5px;
                text-align: center;
                font-size: 10pt;
                height: 25px;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 3px;
            }
        """)
        self.overall_progress_bar.hide()  # Hidden by default
        progress_layout.addWidget(self.overall_progress_bar)
        
        layout.addWidget(progress_section)
        
        # Plot widgets - side by side
        if not PYQTGRAPH_AVAILABLE:
            error_label = QLabel("PyQtGraph not available. Plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(error_label)
        else:
            plots_layout = QHBoxLayout()
            
            # Left plot: Magnitude vs Delay
            self.plot_widget = pg.PlotWidget()
            self.plot_widget.setLabel('left', 'Magnitude')
            self.plot_widget.setLabel('bottom', 'PLL Delay')
            self.plot_widget.setTitle('Magnitude vs PLL Delay')
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.scatter_items: Dict[float, pg.ScatterPlotItem] = {}  # bias -> scatter item
            plots_layout.addWidget(self.plot_widget)
            
            # Right plot: RMSE vs Bias Voltage (only shown when set_bias=True)
            self.rmse_plot_widget = pg.PlotWidget()
            self.rmse_plot_widget.setLabel('left', 'RMSE (Noise)')
            self.rmse_plot_widget.setLabel('bottom', 'Substrate Bias Voltage (V)')
            self.rmse_plot_widget.setTitle('RMSE vs Substrate Bias Voltage')
            self.rmse_plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.rmse_scatter_items: Dict[float, pg.ScatterPlotItem] = {}  # bias -> scatter item
            self.rmse_plot_widget.hide()  # Hidden by default
            plots_layout.addWidget(self.rmse_plot_widget)
            
            layout.addLayout(plots_layout)
        
        # Status report at bottom
        self.status_report_label = QLabel("")
        self.status_report_label.setStyleSheet("""
            font-size: 11pt;
            font-weight: bold;
            color: #2E7D32;
            padding: 10px;
            background-color: #E8F5E9;
            border: 1px solid #4CAF50;
            border-radius: 5px;
        """)
        self.status_report_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.status_report_label.setWordWrap(True)
        self.status_report_label.hide()  # Hidden until sweep completes
        layout.addWidget(self.status_report_label)
        
        layout.addStretch()
    
    def start_delay_sweep(
        self, 
        settings_params: Dict[str, Any], 
        smr_driver_id: int,
        original_settings_params: Optional[Dict[str, Any]] = None,
        set_bias: bool = False
    ) -> None:
        """Start the delay sweep process.
        
        Args:
            settings_params: Parameters dictionary from selected settings (already has [set_delays] overrides)
            smr_driver_id: SMR driver ID
            original_settings_params: Optional original parameters before [set_delays] overrides
                                    (if None, will try to extract from settings_params)
            set_bias: If True, sweep multiple bias voltages (0.5V to 5V in 0.5V steps)
        """
        # Safety check: If pySMR_widget has automated setup mode, ensure it's set on this window
        if self.pySMR_widget is not None:
            if hasattr(self.pySMR_widget, '_automated_setup_mode') and self.pySMR_widget._automated_setup_mode:
                if not (hasattr(self, '_automated_setup_mode') and self._automated_setup_mode):
                    self._automated_setup_mode = True
                    if hasattr(self.pySMR_widget, '_automated_setup_main_window'):
                        self._automated_setup_main_window = self.pySMR_widget._automated_setup_main_window
        
        # Store base params (with [set_delays] overrides already applied from main/pySMR)
        self.base_settings_params = settings_params.copy()
        # Store original loaded params (before [set_delays] overrides) for cic_rate_factor calculation
        if original_settings_params is not None:
            self.original_settings_params = original_settings_params.copy()
        else:
            # If not provided, try to reconstruct by removing [set_delays] overrides
            # This is a fallback - ideally original_settings_params should be provided
            self.original_settings_params = settings_params.copy()
        self.smr_driver_id = smr_driver_id
        self.set_bias = set_bias
        
        # Clear status report from any previous run
        self.status_report_label.setText("")
        self.status_report_label.hide()
        
        # Show/hide RMSE plot based on set_bias
        if PYQTGRAPH_AVAILABLE:
            if set_bias:
                self.rmse_plot_widget.show()
            else:
                self.rmse_plot_widget.hide()
        
        if set_bias:
            # Initialize bias voltage sweep
            self.bias_voltages = [0.5 + i * 0.5 for i in range(10)]  # [0.5, 1.0, 1.5, ..., 5.0]
            self.current_bias_index = 0
            self.bias_results = {}
            self.bias_delay_maps = {}
            # Show both progress bars for bias sweep mode
            self.voltage_progress_label.setText("Current voltage progress:")
            self.voltage_progress_label.show()
            self.voltage_progress_bar.show()
            self.voltage_progress_bar.setValue(0)
            # Show overall progress bar
            self.overall_progress_label.show()
            self.overall_progress_bar.show()
            # Initialize overall progress
            total_tests = len(self.bias_voltages) * self.total_delays_per_voltage
            self.overall_progress_bar.setMaximum(100) # Ensure it is 0-100%
            self.overall_progress_bar.setValue(0)
            self._update_progress_bars()
            self._start_bias_voltage_sweep()
        else:
            # Single delay sweep (original behavior)
            self.current_delay_index = 0
            self.delay_magnitude_map = {}
            self.binary_search_iteration = 0
            self.delay_values = self.initial_delay_values.copy()
            # Show voltage progress bar, hide overall progress bar
            self.voltage_progress_label.setText("Progress:")
            self.voltage_progress_label.show()
            self.voltage_progress_bar.show()
            self.voltage_progress_bar.setValue(0)
            # Hide overall progress bar
            self.overall_progress_label.hide()
            self.overall_progress_bar.hide()
            self._update_progress_bars()
            
            # Send run=False with first delay's parameters, wait 50ms, then start
            if self.delay_values:
                first_delay = self.delay_values[0]
                params = self.base_settings_params.copy()
                params = _apply_set_delays_overrides(params)
                params["pll_delay"] = first_delay
                
                _send_params_with_run_state(
                    self.tcp_queue,
                    params,
                    self.smr_driver_id,
                    False,  # run=False
                    None  # Don't update widget during prep
                )
                
                # Wait 50ms before starting
                QTimer.singleShot(50, self._process_next_delay)
            else:
                self._process_next_delay()
    
    def _start_bias_voltage_sweep(self) -> None:
        """Start bias voltage sweep - set first bias voltage and begin delay sweep."""
        if self.current_bias_index >= len(self.bias_voltages):
            # All bias voltages tested, find best one
            self._finish_bias_voltage_sweep()
            return
        
        bias_voltage = self.bias_voltages[self.current_bias_index]
        self.current_bias_voltage = bias_voltage
        
        # Update voltage progress label
        self.voltage_progress_label.setText(f"Current voltage progress ({bias_voltage}V):")
        
        # Set substrate bias voltage
        self._set_substrate_bias_voltage(bias_voltage)
        
        # Initialize delay sweep for this bias voltage
        self.current_delay_index = 0
        self.delay_magnitude_map = {}
        self.binary_search_iteration = 0
        self.delay_values = self.initial_delay_values.copy()
        self.delay_0_magnitude = None
        
        # Reset voltage progress bar
        self.voltage_progress_bar.setValue(0)
        self._update_progress_bars()
        
        # Wait a bit for bias to settle, then start delay sweep
        QTimer.singleShot(200, self._process_next_delay)
    
    def _process_next_delay(self) -> None:
        """Process the next delay value in the sequence."""
        if self.current_delay_index >= len(self.delay_values):
            # Check if we need to start binary search refinement
            if self.binary_search_iteration == 0:
                # Initial sweep complete, start binary search
                self._start_binary_search()
            elif self.binary_search_iteration < self.binary_search_max_iterations:
                # Continue binary search
                self._continue_binary_search()
            else:
                # Binary search complete, find and set best delay
                self._finish_delay_sweep()
            return
        
        if not self.tcp_queue or not self.tcp_queue.is_connected():
            return
        
        if not self.udp_manager or not self.udp_manager.is_connected():
            return
        
        delay = self.delay_values[self.current_delay_index]
        
        # Update progress bars
        self._update_progress_bars()
        
        # For each delay step, start with base settings and apply:
        # 1. [set_delays] overrides (in case config changed)
        # 2. Current delay value
        params = self.base_settings_params.copy()
        # Apply [set_delays] overrides (ensures run=true and any other overrides)
        params = _apply_set_delays_overrides(params)
        # Update with current delay value
        params["pll_delay"] = delay
        
        # Send full parameter set to FPGA and update controls
        success = _send_pll_delay_to_fpga(
            self.tcp_queue,
            delay,
            params,
            self.smr_driver_id,
            self.pySMR_widget
        )
        
        if not success:
            self.current_delay_index += 1
            # Continue to next delay even if this one failed
            QTimer.singleShot(50, self._process_next_delay)
            return
        
        # Start monitoring UDP immediately (during the 200ms wait)
        self._start_udp_monitoring(delay)
    
    def _start_udp_monitoring(self, delay: float) -> None:
        """Start monitoring UDP data stream for the specified delay.
        
        Args:
            delay: Current delay value being measured
        """
        if not self.udp_manager:
            return
        
        # Store delay for processing later
        self.current_delay = delay
        
        # Subscribe to UDP stream
        self.udp_subscriber_id, self.udp_sub_queue = self.udp_manager.subscribe_queue(maxsize=1000)
        self.udp_magnitudes = []
        self.udp_start_time = time.time()
        
        # Start timer to check UDP data periodically
        self.udp_timer.start(50)  # Check every 50ms
    
    def _check_udp_data(self) -> None:
        """Check for UDP data and collect magnitude values."""
        if not self.udp_sub_queue or self.udp_start_time is None:
            return
        
        # Collect available packets
        # Note: packet.parsed_frequencies contains magnitude values when signal_of_interest='Magnitude'
        try:
            while True:
                packet = self.udp_sub_queue.get_nowait()
                if packet.parsed_frequencies:
                    self.udp_magnitudes.extend(packet.parsed_frequencies)
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Warning: Error reading UDP packet: {e}")
        
        # Check if 200ms have elapsed
        elapsed = time.time() - self.udp_start_time
        if elapsed >= 0.2:
            # Done collecting, process results
            self.udp_timer.stop()
            delay = getattr(self, 'current_delay', 0.0)
            self._process_magnitude_data(delay, self.udp_magnitudes)
            
            # Cleanup
            if self.udp_subscriber_id is not None:
                self.udp_manager.unsubscribe(self.udp_subscriber_id)
                self.udp_subscriber_id = None
            self.udp_sub_queue = None
            self.udp_magnitudes = []
            self.udp_start_time = None
    
    def _process_magnitude_data(self, delay: float, magnitudes: List[float]) -> None:
        """Process collected magnitude data and update plot.
        
        Args:
            delay: Current delay value
            magnitudes: List of magnitude measurements (from UDP when signal_of_interest='Magnitude')
        """
        if magnitudes:
            # Use mean magnitude as the measurement for this delay
            mean_magnitude = float(np.mean(magnitudes))
            # Store in mapping
            self.delay_magnitude_map[delay] = mean_magnitude
            
            # If delay is 0.0, store it for use with delay 1.0 (wrapping)
            if abs(delay - 0.0) < 1e-6:
                self.delay_0_magnitude = mean_magnitude
                # Also store for delay 1.0 since they wrap
                self.delay_magnitude_map[1.0] = mean_magnitude
            # If delay is 1.0 and we haven't tested it yet, use delay 0.0 result
            elif abs(delay - 1.0) < 1e-6 and self.delay_0_magnitude is not None:
                # Use the magnitude from delay 0.0 (they wrap)
                self.delay_magnitude_map[1.0] = self.delay_0_magnitude
                mean_magnitude = self.delay_0_magnitude
            
            # If bias voltage sweep mode, store delay map for this bias
            if self.set_bias and self.current_bias_voltage is not None:
                if self.current_bias_voltage not in self.bias_delay_maps:
                    self.bias_delay_maps[self.current_bias_voltage] = {}
                self.bias_delay_maps[self.current_bias_voltage][delay] = mean_magnitude
            
            # Update plot with all tested delays
            self._update_plot()
        else:
            # Store placeholder value
            self.delay_magnitude_map[delay] = 0.0
            # If delay is 0.0, also store for 1.0
            if abs(delay - 0.0) < 1e-6:
                self.delay_0_magnitude = 0.0
                self.delay_magnitude_map[1.0] = 0.0
        
        # Move to next delay
        self.current_delay_index += 1
        
        # Update progress bars
        self._update_progress_bars()
        
        # Before processing next delay, send run=False with next delay's parameters, wait 50ms
        if self.current_delay_index < len(self.delay_values):
            next_delay = self.delay_values[self.current_delay_index]
            params = self.base_settings_params.copy()
            params = _apply_set_delays_overrides(params)
            params["pll_delay"] = next_delay
            
            # Send run=False with next delay's parameters
            _send_params_with_run_state(
                self.tcp_queue,
                params,
                self.smr_driver_id,
                False,  # run=False
                None  # Don't update widget during wait
            )
            
            # Wait 50ms before processing next delay
            QTimer.singleShot(50, self._process_next_delay)
        else:
            # No more delays in current list, proceed to next stage
            QTimer.singleShot(50, self._process_next_delay)
    
    def _update_progress_bars(self) -> None:
        """Update both progress bars based on current sweep state."""
        if self.set_bias:
            # Bias voltage sweep mode
            # Calculate current voltage progress in steps of 11% (1/9 ≈ 0.11)
            # 4 steps for initial sweep + 5 steps for binary search = 9 total steps
            if self.binary_search_iteration == 0:
                # In initial delay sweep: each step is 1/9 ≈ 11%
                completed_steps = self.current_delay_index
            else:
                # In binary search: 4 initial steps completed + current binary search iteration
                completed_steps = 4 + self.binary_search_iteration
            
            # Calculate progress: (completed_steps / 9) * 100, rounded to nearest integer
            voltage_progress = int(round((completed_steps / float(self.total_delays_per_voltage)) * 100))
            
            # Clamp to 0-100
            voltage_progress = max(0, min(100, voltage_progress))
            self.voltage_progress_bar.setValue(voltage_progress)
            
            # Calculate overall progress
            # Total tests = number of voltages * delays per voltage
            total_tests = len(self.bias_voltages) * self.total_delays_per_voltage
            # Completed tests = (completed voltages * delays per voltage) + completed steps for current voltage
            completed_tests = self.current_bias_index * self.total_delays_per_voltage + completed_steps
            overall_progress = int((completed_tests / float(total_tests)) * 100) if total_tests > 0 else 0
            overall_progress = max(0, min(100, overall_progress))
            self.overall_progress_bar.setValue(overall_progress)
        else:
            # Single delay sweep mode
            # Calculate voltage progress in steps of 11% (1/9 ≈ 0.11)
            # 4 steps for initial sweep + 5 steps for binary search = 9 total steps
            if self.binary_search_iteration == 0:
                # In initial delay sweep: each step is 1/9 ≈ 11%
                completed_steps = self.current_delay_index
            else:
                # In binary search: 4 initial steps completed + current binary search iteration
                completed_steps = 4 + self.binary_search_iteration
            
            # Calculate progress: (completed_steps / 9) * 100, rounded to nearest integer
            voltage_progress = int(round((completed_steps / float(self.total_delays_per_voltage)) * 100))
            
            # Clamp to 0-100
            voltage_progress = max(0, min(100, voltage_progress))
            self.voltage_progress_bar.setValue(voltage_progress)
    
    def _update_plot(self) -> None:
        """Update the scatter plot with all tested delays and magnitudes."""
        if not PYQTGRAPH_AVAILABLE:
            return
        
        if self.set_bias and self.bias_delay_maps:
            # Multi-bias mode: plot each bias voltage with unique color
            self.plot_widget.clear()
            
            # Generate colors for each bias voltage
            colors = [
                (0, 0, 255),      # Blue
                (255, 0, 0),      # Red
                (0, 255, 0),      # Green
                (255, 165, 0),    # Orange
                (128, 0, 128),    # Purple
                (255, 192, 203),  # Pink
                (0, 255, 255),    # Cyan
                (255, 255, 0),    # Yellow
                (165, 42, 42),    # Brown
                (0, 128, 128),    # Teal
            ]
            
            all_magnitudes = []
            for idx, (bias_voltage, delay_map) in enumerate(sorted(self.bias_delay_maps.items())):
                if not delay_map:
                    continue
                
                delays = sorted(delay_map.keys())
                magnitudes = [delay_map[d] for d in delays]
                all_magnitudes.extend(magnitudes)
                
                # Create scatter plot for this bias voltage
                color = colors[idx % len(colors)]
                scatter_item = pg.ScatterPlotItem(
                    delays,
                    magnitudes,
                    pen=None,
                    brush=color,
                    symbol='o',
                    size=10,
                    pxMode=True,
                    antialias=True,
                    name=f"{bias_voltage}V"
                )
                self.plot_widget.addItem(scatter_item)
                self.scatter_items[bias_voltage] = scatter_item
            
            # Auto-scale plot
            if all_magnitudes:
                mag_min = min(all_magnitudes)
                mag_max = max(all_magnitudes)
                mag_range = mag_max - mag_min
                if mag_range > 0:
                    padding = mag_range * 0.1
                    self.plot_widget.setYRange(mag_min - padding, mag_max + padding)
                else:
                    margin = abs(mag_min) * 0.01 if mag_min != 0 else 1.0
                    self.plot_widget.setYRange(mag_min - margin, mag_max + margin)
        elif self.delay_magnitude_map:
            # Single sweep mode: use original scatter item
            # Get all delays and magnitudes, sorted by delay
            delays = sorted(self.delay_magnitude_map.keys())
            magnitudes = [self.delay_magnitude_map[d] for d in delays]
            
            # Check if scatter_item exists (might not in multi-bias mode)
            if hasattr(self, 'scatter_item'):
                self.scatter_item.setData(delays, magnitudes)
            else:
                # Create scatter item if it doesn't exist
                self.scatter_item = pg.ScatterPlotItem(
                    delays,
                    magnitudes,
                    pen=None,
                    brush='b',
                    symbol='o',
                    size=10,
                    pxMode=True,
                    antialias=True
                )
                self.plot_widget.addItem(self.scatter_item)
            
            # Auto-scale plot
            if len(magnitudes) > 0:
                mag_min = min(magnitudes)
                mag_max = max(magnitudes)
                mag_range = mag_max - mag_min
                if mag_range > 0:
                    padding = mag_range * 0.1
                    self.plot_widget.setYRange(mag_min - padding, mag_max + padding)
                else:
                    # All magnitudes are the same, add small margin
                    margin = abs(mag_min) * 0.01 if mag_min != 0 else 1.0
                    self.plot_widget.setYRange(mag_min - margin, mag_max + margin)
    
    def _start_binary_search(self) -> None:
        """Start binary search refinement after initial sweep."""
        # Find the delay with maximum magnitude from initial sweep
        if not self.delay_magnitude_map:
            print("Error: No magnitude data collected")
            return
        
        # Check if all magnitude values are equivalent
        magnitudes = list(self.delay_magnitude_map.values())
        if all(math.isclose(m, magnitudes[0], rel_tol=1e-9) for m in magnitudes):
            if self.set_bias:
                print(f"Warning: All magnitude values for {self.current_bias_voltage}V are equivalent. Skipping optimal delay selection.")
                self._measure_rmse_and_continue()
                return
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    "Unable to identify optimal delay value"
                )
                return
        
        # Ensure delay 1.0 has the same magnitude as delay 0.0 (wrapping)
        # Pad with delay 1.0 using delay 0.0 result
        if 0.0 in self.delay_magnitude_map:
            self.delay_magnitude_map[1.0] = self.delay_magnitude_map[0.0]
        
        # Get initial delays and their magnitudes
        # Include both 0.0 and 1.0 in the list for binary search (they have the same magnitude)
        # The initial_delay_values list contains [0.0, 0.25, 0.5, 0.75], and we add 1.0
        initial_delays = sorted([d for d in self.delay_magnitude_map.keys() 
                                if d in self.initial_delay_values or abs(d - 1.0) < 1e-6])
        if len(initial_delays) < 2:
            print("Error: Need at least 2 initial delay measurements")
            return
        
        # Find delay with maximum magnitude
        max_delay = max(initial_delays, key=lambda d: self.delay_magnitude_map[d])
        max_magnitude = self.delay_magnitude_map[max_delay]
        
        # Find adjacent delay with next-highest magnitude
        # Get index of max_delay in sorted initial_delays
        sorted_delays = sorted(initial_delays)
        max_idx = sorted_delays.index(max_delay)
        
        # Check neighbors
        candidates = []
        if max_idx > 0:
            candidates.append((sorted_delays[max_idx - 1], self.delay_magnitude_map[sorted_delays[max_idx - 1]]))
        if max_idx < len(sorted_delays) - 1:
            candidates.append((sorted_delays[max_idx + 1], self.delay_magnitude_map[sorted_delays[max_idx + 1]]))
        
        if not candidates:
            print("Error: No adjacent delays found")
            return
        
        # Find the candidate with highest magnitude (next-highest adjacent)
        next_highest_delay, next_highest_magnitude = max(candidates, key=lambda x: x[1])
        
        # Set up binary search between max_delay and next_highest_delay
        # Always set low < high for consistency
        if max_delay < next_highest_delay:
            self.binary_search_low = max_delay
            self.binary_search_high = next_highest_delay
        else:
            self.binary_search_low = next_highest_delay
            self.binary_search_high = max_delay
        
        self.binary_search_iteration = 1
        # Update progress bars
        self._update_progress_bars()
        
        # Calculate midpoint and add to delay_values
        midpoint = (self.binary_search_low + self.binary_search_high) / 2.0
        self.delay_values = [midpoint]
        self.current_delay_index = 0
        
        # Send run=False with midpoint parameters, wait 50ms, then start processing
        params = self.base_settings_params.copy()
        params = _apply_set_delays_overrides(params)
        params["pll_delay"] = midpoint
        
        _send_params_with_run_state(
            self.tcp_queue,
            params,
            self.smr_driver_id,
            False,  # run=False
            None  # Don't update widget during wait
        )
        
        # Wait 50ms before processing the midpoint
        QTimer.singleShot(50, self._process_next_delay)
    
    def _continue_binary_search(self) -> None:
        """Continue binary search refinement."""
        # Get the last tested delay (the midpoint we just tested)
        if not self.delay_values:
            print("Error: No delay to process in binary search")
            return
        
        last_delay = self.delay_values[0]  # The midpoint we just tested
        last_magnitude = self.delay_magnitude_map.get(last_delay, 0.0)
        
        # Get magnitudes at low and high bounds
        low_magnitude = self.delay_magnitude_map.get(self.binary_search_low, 0.0)
        high_magnitude = self.delay_magnitude_map.get(self.binary_search_high, 0.0)
        
        # Determine which side to keep based on which has higher magnitude
        # We want to keep the side with the higher magnitude
        if last_magnitude > low_magnitude and last_magnitude > high_magnitude:
            # Midpoint is highest, keep the side with the higher of the two bounds
            if low_magnitude > high_magnitude:
                # Low side is better, keep low side, midpoint becomes new high
                self.binary_search_high = last_delay
            else:
                # High side is better, keep high side, midpoint becomes new low
                self.binary_search_low = last_delay
        elif low_magnitude > high_magnitude:
            # Low side (including midpoint) is better, narrow to low side
            self.binary_search_high = last_delay
        else:
            # High side (including midpoint) is better, narrow to high side
            self.binary_search_low = last_delay
        
        # Calculate next midpoint
        midpoint = (self.binary_search_low + self.binary_search_high) / 2.0
        
        # Avoid testing the same delay twice
        if midpoint in self.delay_magnitude_map:
            # If we've already tested this exact value, we're done
            self.binary_search_iteration = self.binary_search_max_iterations
            self._finish_delay_sweep()
            return
        
        self.binary_search_iteration += 1
        # Update progress bars
        self._update_progress_bars()
        
        # Add midpoint to delay_values
        self.delay_values = [midpoint]
        self.current_delay_index = 0
        
        # Send run=False with midpoint parameters, wait 50ms, then process it
        params = self.base_settings_params.copy()
        params = _apply_set_delays_overrides(params)
        params["pll_delay"] = midpoint
        
        _send_params_with_run_state(
            self.tcp_queue,
            params,
            self.smr_driver_id,
            False,  # run=False
            None  # Don't update widget during wait
        )
        
        # Wait 50ms before processing the midpoint
        QTimer.singleShot(50, self._process_next_delay)
    
    def _calculate_cic_bit_shift(self, best_delay: float, best_magnitude: float) -> int:
        """Calculate cic_bit_shift value based on delay sweep results.
        
        Args:
            best_delay: The delay value that gave maximum magnitude
            best_magnitude: The maximum magnitude value observed
            
        Returns:
            Calculated cic_bit_shift value
        """
        # Get original cic_rate from settings file (before [set_delays] overrides)
        original_cic_rate = self.original_settings_params.get("cic_rate", 2500)
        
        # Get cic_rate from [set_delays] section of config file
        set_delays_overrides = _load_set_delays_overrides()
        set_delays_cic_rate = set_delays_overrides.get("cic_rate", 2500)
        
        # Calculate cic_rate_factor: (original_cic_rate / set_delays_cic_rate) raised to the power of 6
        if set_delays_cic_rate == 0:
            print(f"Warning: set_delays cic_rate is 0, using default 2500")
            set_delays_cic_rate = 2500
        cic_rate_factor = (original_cic_rate / float(set_delays_cic_rate)) ** 6
        
        # Calculate cic_mag_offset: log10((cic_rate_factor * best_magnitude * 2^32) / 12500000)
        # 2^32 = 4294967296
        two_power_32 = 2 ** 32  # 4294967296
        numerator = cic_rate_factor * best_magnitude * two_power_32
        denominator = 12500000.0
        cic_mag_offset = math.log10(numerator / denominator)
        
        # Calculate final cic_bit_shift: 20 - roundDown((8.60206 - cic_mag_offset) / 0.60206)
        intermediate_value = (8.60206 - cic_mag_offset) / 0.60206
        rounded_down = math.floor(intermediate_value)
        cic_bit_shift = 20 - rounded_down
        
        return int(cic_bit_shift)
    
    def _finish_delay_sweep(self) -> None:
        """Finish delay sweep and set the best delay value with calculated cic_bit_shift."""
        if not self.delay_magnitude_map:
            print("Error: No magnitude data collected")
            return
        
        # Find delay with maximum magnitude
        best_delay = max(self.delay_magnitude_map.keys(), key=lambda d: self.delay_magnitude_map[d])
        best_magnitude = self.delay_magnitude_map[best_delay]
        
        # Update progress bars (voltage progress should be at 100% now)
        self._update_progress_bars()
        
        # Calculate cic_bit_shift
        calculated_cic_bit_shift = self._calculate_cic_bit_shift(best_delay, best_magnitude)
        
        # Create new parameter set using original loaded settings, with exceptions:
        # - pll_delay: use best_delay from binary search
        # - cic_bit_shift: use calculated value
        # Start with original loaded parameters (before [set_delays] overrides)
        final_params = self.original_settings_params.copy()
        final_params["pll_delay"] = best_delay
        final_params["cic_bit_shift"] = calculated_cic_bit_shift
        # Ensure run is true and smr_driver_id is set for final parameter set
        final_params["run"] = True
        if "smr_driver_id" not in final_params:
            final_params["smr_driver_id"] = self.smr_driver_id
        
        # Send the final parameter set to FPGA
        success = _send_params_with_run_state(
            self.tcp_queue,
            final_params,
            self.smr_driver_id,
            True,  # run=True
            self.pySMR_widget
        )
        
        if not success:
            if self.set_bias:
                # Continue to next bias voltage even if this failed
                self._measure_rmse_and_continue()
            return
        
        if self.set_bias:
            # Bias voltage sweep mode: measure RMSE, then continue to next bias or finish
            # Store results for this bias voltage
            self.bias_results[self.current_bias_voltage] = {
                "best_delay": best_delay,
                "best_magnitude": best_magnitude,
                "cic_bit_shift": calculated_cic_bit_shift,
                "rmse": None  # Will be set after RMSE measurement
            }
            # Start RMSE measurement
            self._start_rmse_measurement()
        else:
            # Single sweep mode: save and finish
            # Note: _save_final_settings will handle closing for automated setup
            self._save_final_settings(final_params, best_delay, best_magnitude, calculated_cic_bit_shift)
    
    def _start_rmse_measurement(self) -> None:
        """Start RMSE measurement for current bias voltage (monitor frequency for 2 seconds)."""
        if not self.udp_manager:
            self._measure_rmse_and_continue()
            return
        
        # Need to change signal_of_interest to "PLL Frequency" for RMSE measurement
        # Get current best delay and cic_bit_shift from this bias voltage's results
        if self.current_bias_voltage not in self.bias_results:
            self._measure_rmse_and_continue()
            return
        
        best_result = self.bias_results[self.current_bias_voltage]
        best_delay = best_result["best_delay"]
        calculated_cic_bit_shift = best_result["cic_bit_shift"]
        
        # Create parameter set with signal_of_interest='PLL Frequency' for RMSE measurement
        # Use original settings, but override with best delay, cic_bit_shift, and signal_of_interest
        rmse_params = self.original_settings_params.copy()
        rmse_params["pll_delay"] = best_delay
        rmse_params["cic_bit_shift"] = calculated_cic_bit_shift
        rmse_params["signal_of_interest"] = "PLL Frequency"  # Change to frequency for RMSE
        rmse_params["run"] = True
        if "smr_driver_id" not in rmse_params:
            rmse_params["smr_driver_id"] = self.smr_driver_id
        
        # Send parameters with signal_of_interest='PLL Frequency'
        success = _send_params_with_run_state(
            self.tcp_queue,
            rmse_params,
            self.smr_driver_id,
            True,  # run=True
            None  # Don't update widget during RMSE measurement
        )
        
        if not success:
            print(f"Warning: Failed to set parameters for RMSE measurement")
            # Continue anyway
        
        # Wait a bit for parameters to take effect
        QTimer.singleShot(200, self._start_rmse_data_collection)
    
    def _start_rmse_data_collection(self) -> None:
        """Start collecting UDP data for RMSE calculation."""
        # Subscribe to UDP stream
        self.udp_subscriber_id, self.udp_sub_queue = self.udp_manager.subscribe_queue(maxsize=1000)
        self.rmse_frequencies = []
        self.rmse_start_time = time.time()
        
        # Start timer to check UDP data periodically
        self.rmse_timer.start(50)  # Check every 50ms
    
    def _check_rmse_data(self) -> None:
        """Check for UDP frequency data and collect for RMSE calculation."""
        if not self.udp_sub_queue or self.rmse_start_time is None:
            return
        
        # Collect available packets - when measuring RMSE, we need frequency data
        # Note: packet.parsed_frequencies contains frequency values when signal_of_interest='PLL Frequency'
        try:
            while True:
                packet = self.udp_sub_queue.get_nowait()
                if packet.parsed_frequencies:
                    self.rmse_frequencies.extend(packet.parsed_frequencies)
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Warning: Error reading UDP packet for RMSE: {e}")
        
        # Check if 2 seconds have elapsed
        elapsed = time.time() - self.rmse_start_time
        if elapsed >= 2.0:
            # Done collecting, calculate RMSE
            self.rmse_timer.stop()
            rmse = self._calculate_rmse(self.rmse_frequencies)
            
            # Store RMSE for this bias voltage
            if self.current_bias_voltage is not None:
                if self.current_bias_voltage in self.bias_results:
                    self.bias_results[self.current_bias_voltage]["rmse"] = rmse
            
            # Update RMSE plot
            self._update_rmse_plot()
            
            # Cleanup
            if self.udp_subscriber_id is not None:
                self.udp_manager.unsubscribe(self.udp_subscriber_id)
                self.udp_subscriber_id = None
            self.udp_sub_queue = None
            self.rmse_frequencies = []
            self.rmse_start_time = None
            
            # Continue to next bias voltage or finish
            self._measure_rmse_and_continue()
    
    def _calculate_rmse(self, frequencies: List[float]) -> float:
        """Calculate RMSE of frequency signal.
        
        Args:
            frequencies: List of frequency measurements
            
        Returns:
            RMSE value
        """
        if not frequencies or len(frequencies) < 2:
            return 0.0
        
        # Calculate mean frequency
        mean_freq = float(np.mean(frequencies))
        
        # Calculate RMSE: sqrt(mean((freq - mean_freq)^2))
        squared_diff = [(f - mean_freq) ** 2 for f in frequencies]
        mean_squared_diff = float(np.mean(squared_diff))
        rmse = math.sqrt(mean_squared_diff)
        
        return rmse
    
    def _update_rmse_plot(self) -> None:
        """Update the RMSE vs bias voltage plot."""
        if not PYQTGRAPH_AVAILABLE or not self.set_bias:
            return
        
        if not self.bias_results:
            return
        
        # Get all bias voltages with RMSE values
        bias_voltages_with_rmse = [b for b in self.bias_results.keys() if self.bias_results[b].get("rmse") is not None]
        
        if not bias_voltages_with_rmse:
            return
        
        # Use the same color scheme as the magnitude plot
        colors = [
            (0, 0, 255),      # Blue
            (255, 0, 0),      # Red
            (0, 255, 0),      # Green
            (255, 165, 0),    # Orange
            (128, 0, 128),    # Purple
            (255, 192, 203),  # Pink
            (0, 255, 255),    # Cyan
            (255, 255, 0),    # Yellow
            (165, 42, 42),    # Brown
            (0, 128, 128),    # Teal
        ]
        
        # Clear existing scatter items
        self.rmse_plot_widget.clear()
        self.rmse_scatter_items.clear()
        
        # Determine the color index for each bias voltage by matching the order used in magnitude plot
        # The magnitude plot uses sorted(self.bias_delay_maps.items()) to assign colors
        if hasattr(self, 'bias_delay_maps') and self.bias_delay_maps:
            # Use the same sorted order as the magnitude plot
            sorted_bias_voltages = sorted(self.bias_delay_maps.keys())
            # Create a mapping from bias voltage to color index
            bias_to_color_idx = {bias: idx for idx, bias in enumerate(sorted_bias_voltages)}
        else:
            # Fallback: use sorted bias voltages
            sorted_bias_voltages = sorted(bias_voltages_with_rmse)
            bias_to_color_idx = {bias: idx for idx, bias in enumerate(sorted_bias_voltages)}
        
        # Create a scatter plot item for each bias voltage with matching color
        all_rmse_values = []
        for bias_voltage in sorted(bias_voltages_with_rmse):
            rmse_value = self.bias_results[bias_voltage]["rmse"]
            all_rmse_values.append(rmse_value)
            
            # Get color index for this bias voltage (same as used in magnitude plot)
            color_idx = bias_to_color_idx.get(bias_voltage, 0)
            color = colors[color_idx % len(colors)]
            
            # Create scatter plot item for this single point
            scatter_item = pg.ScatterPlotItem(
                [bias_voltage],
                [rmse_value],
                pen=None,
                brush=color,
                symbol='o',
                size=10,
                pxMode=True,
                antialias=True,
                name=f"{bias_voltage}V"
            )
            self.rmse_plot_widget.addItem(scatter_item)
            self.rmse_scatter_items[bias_voltage] = scatter_item
        
        # Auto-scale plot
        if len(all_rmse_values) > 0:
            rmse_min = min(all_rmse_values)
            rmse_max = max(all_rmse_values)
            rmse_range = rmse_max - rmse_min
            if rmse_range > 0:
                padding = rmse_range * 0.1
                self.rmse_plot_widget.setYRange(rmse_min - padding, rmse_max + padding)
            else:
                margin = abs(rmse_min) * 0.01 if rmse_min != 0 else 1.0
                self.rmse_plot_widget.setYRange(rmse_min - margin, rmse_max + margin)
    
    def _measure_rmse_and_continue(self) -> None:
        """After RMSE measurement, move to next bias voltage or finish."""
        if not self.set_bias:
            return
        
        # Move to next bias voltage
        self.current_bias_index += 1
        if self.current_bias_index < len(self.bias_voltages):
            # Continue to next bias voltage
            self._start_bias_voltage_sweep()
        else:
            # All bias voltages tested, find best one
            self._finish_bias_voltage_sweep()
    
    def _finish_bias_voltage_sweep(self) -> None:
        """Finish bias voltage sweep - find best bias (lowest RMSE) and save only that setting."""
        if not self.bias_results:
            return
        
        # Find bias voltage with lowest RMSE
        valid_results = {
            b: r for b, r in self.bias_results.items()
            if r.get("rmse") is not None
        }
        
        if not valid_results:
            return
        
        best_bias_voltage = min(valid_results.keys(), key=lambda b: valid_results[b]["rmse"])
        best_result = valid_results[best_bias_voltage]
        
        # Update progress bars to 100%
        self.voltage_progress_bar.setValue(100)
        self.overall_progress_bar.setValue(self.overall_progress_bar.maximum())
        
        # Set substrate bias to best voltage
        self._set_substrate_bias_voltage(best_bias_voltage)
        
        # Create final parameter set using original loaded settings, with:
        # - pll_delay: from best bias result
        # - cic_bit_shift: from best bias result
        # - substrate_bias: best_bias_voltage (will be saved in CSV)
        final_params = self.original_settings_params.copy()
        final_params["pll_delay"] = best_result["best_delay"]
        final_params["cic_bit_shift"] = best_result["cic_bit_shift"]
        final_params["run"] = True
        if "smr_driver_id" not in final_params:
            final_params["smr_driver_id"] = self.smr_driver_id
        
        # Send the final parameter set to FPGA
        success = _send_params_with_run_state(
            self.tcp_queue,
            final_params,
            self.smr_driver_id,
            True,  # run=True
            self.pySMR_widget
        )
        
        if success:
            # Save only the best setting to CSV
            # Note: _save_final_settings will handle closing for automated setup
            self._save_final_settings(final_params, best_result["best_delay"], best_result["best_magnitude"], 
                                    best_result["cic_bit_shift"], substrate_bias=best_bias_voltage)
            # Safety check: ensure window closes in automated setup mode even if _save_final_settings didn't trigger it
            # This handles edge cases where _save_final_settings might fail silently before reaching the automated setup check
            is_automated = hasattr(self, '_automated_setup_mode') and self._automated_setup_mode
            
            # Fallback: Check pySMR_widget if flag is not set on self
            if not is_automated and self.pySMR_widget is not None and hasattr(self.pySMR_widget, '_automated_setup_mode') and self.pySMR_widget._automated_setup_mode:
                is_automated = True
                self._automated_setup_mode = True
                if hasattr(self.pySMR_widget, '_automated_setup_main_window'):
                    self._automated_setup_main_window = self.pySMR_widget._automated_setup_main_window
            
            if is_automated:
                # Double-check that window is still visible (hasn't been closed yet)
                if self.isVisible():
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(2000, self._close_for_automated_setup)
    
    def _save_final_settings(
        self,
        final_params: Dict[str, Any],
        best_delay: float,
        best_magnitude: float,
        calculated_cic_bit_shift: int,
        substrate_bias: Optional[float] = None
    ) -> None:
        """Save final settings to CSV with type='setDelays'.
        
        Args:
            final_params: Final parameter dictionary
            best_delay: Best delay value
            best_magnitude: Best magnitude value
            calculated_cic_bit_shift: Calculated CIC bit shift
            substrate_bias: Optional substrate bias voltage (if None, will try to get from pySMR_widget)
        """
        try:
            # Convert final_params to format expected by write_smr_settings (capitalized keys)
            fpga_parameters = _convert_params_to_settings_dict(final_params)
            
            # Get substrate_bias
            if substrate_bias is None:
                substrate_bias = 3.0  # Default
                if self.pySMR_widget is not None:
                    if hasattr(self.pySMR_widget, 'substrate_bias_control'):
                        substrate_bias = self.pySMR_widget.substrate_bias_control.get_value()
                    elif hasattr(self.pySMR_widget, 'quick_substrate_bias_control'):
                        substrate_bias = self.pySMR_widget.quick_substrate_bias_control.get_value()
            
            # Get operator if available from pySMR_widget
            operator = None
            if self.pySMR_widget is not None and hasattr(self.pySMR_widget, 'operator'):
                operator = self.pySMR_widget.operator
            
            # Save settings to CSV
            save_success = write_smr_settings(
                settings_type="setDelays",
                substrate_bias=substrate_bias,
                fpga_parameters=fpga_parameters,
                operator=operator,
            )
            
            # Update progress bars to 100%
            if self.set_bias:
                # Bias sweep mode: update overall progress bar
                self.overall_progress_bar.setValue(self.overall_progress_bar.maximum())
            else:
                # Single sweep mode: update voltage progress bar
                self.voltage_progress_bar.setValue(100)
            
            # Update status report
            self._update_status_report(best_delay, calculated_cic_bit_shift, substrate_bias if self.set_bias else None)
            
            # If in automated setup mode, close window and notify main window after saving
            # If in automated setup mode, close window and notify main window after saving
            is_automated = hasattr(self, '_automated_setup_mode') and self._automated_setup_mode
            
            # Fallback: Check pySMR_widget if flag is not set on self
            if not is_automated and self.pySMR_widget is not None and hasattr(self.pySMR_widget, '_automated_setup_mode') and self.pySMR_widget._automated_setup_mode:
                is_automated = True
                self._automated_setup_mode = True
                if hasattr(self.pySMR_widget, '_automated_setup_main_window'):
                    self._automated_setup_main_window = self.pySMR_widget._automated_setup_main_window
            
            if is_automated:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(1000, self._close_for_automated_setup)
        except Exception as e:
            print(f"Error saving settings to CSV: {e}")
            import traceback
            traceback.print_exc()
            # Update progress bars to 100% even on error
            if self.set_bias:
                self.overall_progress_bar.setValue(self.overall_progress_bar.maximum())
            else:
                self.voltage_progress_bar.setValue(100)
            # Even on error, if in automated setup mode, close window
            # Even on error, if in automated setup mode, close window
            is_automated = hasattr(self, '_automated_setup_mode') and self._automated_setup_mode
            
            # Fallback: Check pySMR_widget if flag is not set on self
            if not is_automated and self.pySMR_widget is not None and hasattr(self.pySMR_widget, '_automated_setup_mode') and self.pySMR_widget._automated_setup_mode:
                is_automated = True
            
            if is_automated:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(1000, self._close_for_automated_setup)
    
    def _close_for_automated_setup(self) -> None:
        """Close window and notify main window that Set Delays is complete (for automated setup)."""
        # Guard against duplicate calls
        if hasattr(self, '_closing_for_automated_setup') and self._closing_for_automated_setup:
            return
        
        self._closing_for_automated_setup = True
        
        # Check if we're in automated setup mode
        is_automated = hasattr(self, '_automated_setup_mode') and self._automated_setup_mode
        
        # Fallback: Check pySMR_widget if flag is not set on self
        if not is_automated and self.pySMR_widget is not None:
            if hasattr(self.pySMR_widget, '_automated_setup_mode') and self.pySMR_widget._automated_setup_mode:
                is_automated = True
                self._automated_setup_mode = True
                if hasattr(self.pySMR_widget, '_automated_setup_main_window'):
                    self._automated_setup_main_window = self.pySMR_widget._automated_setup_main_window
        
        if is_automated:
            # Get main window reference
            main_window = None
            if hasattr(self, '_automated_setup_main_window'):
                main_window = self._automated_setup_main_window
            elif self.pySMR_widget is not None and hasattr(self.pySMR_widget, '_automated_setup_main_window'):
                main_window = self.pySMR_widget._automated_setup_main_window
            
            # Notify main window that Set Delays is complete BEFORE closing window
            # This ensures the notification happens even if _check_set_delays_complete() detects the closed window
            if main_window is not None and hasattr(main_window, '_stage4_smr_complete'):
                main_window._stage4_smr_complete()
            
            # Close the Set Delays window after notification
            QTimer.singleShot(100, self.close)
        else:
            self._closing_for_automated_setup = False  # Reset flag if not automated
    
    def _update_status_report(
        self,
        best_delay: float,
        calculated_cic_bit_shift: int,
        optimal_voltage: Optional[float] = None
    ) -> None:
        """Update the status report label with optimal settings.
        
        Args:
            best_delay: Optimal delay value
            calculated_cic_bit_shift: Optimal CIC bit shift value
            optimal_voltage: Optional optimal substrate bias voltage (if bias sweep was used)
        """
        # Build status message
        status_parts = ["Set delays completed, optimal settings found and pushed to SMR:"]
        
        if optimal_voltage is not None:
            status_parts.append(f"  Voltage: {optimal_voltage:.1f}V")
        
        status_parts.append(f"  Delay: {best_delay:.4f}")
        status_parts.append(f"  CIC Bit Shift: {calculated_cic_bit_shift}")
        
        status_text = "\n".join(status_parts)
        self.status_report_label.setText(status_text)
        self.status_report_label.show()


def main() -> None:
    """Standalone entry point for SMR set delays."""
    # 1. Initialize TCP connection
    tcp_success, tcp_queue, tcp_msg = initialize_tcp_connection()
    print(tcp_msg)
    
    if not tcp_success:
        print("Error: Failed to initialize TCP connection. Exiting.")
        return
    
    # 2. Initialize UDP connection
    udp_success, udp_manager, udp_msg = initialize_udp_connection()
    print(udp_msg)
    
    if not udp_success:
        print("Error: Failed to initialize UDP connection. Exiting.")
        return
    
    # 3. Show GUI to select sweep setting
    app = QApplication(sys.argv)
    
    # Read saved settings
    settings_list = read_smr_settings()
    if not settings_list:
        print("Error: No saved settings found. Please save a sweep setting first.")
        return
    
    # Show dialog to select settings
    dialog = LoadSettingsDialog(settings_list)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        print("No settings selected. Exiting.")
        return
    
    selected_settings = dialog.get_selected_settings()
    if not selected_settings:
        print("Error: No settings selected. Exiting.")
        return
    
    # Convert settings dictionary to parameters
    settings_params = _convert_settings_dict_to_params(selected_settings)
    
    # Get smr_driver_id
    smr_driver_id = int(settings_params.get("smr_driver_id", 0))
    
    # Apply [set_delays] overrides
    params = _apply_set_delays_overrides(settings_params)
    
    # Send initial settings to FPGA (all parameters except pll_delay which we'll vary)
    args, _ = _map_parameters_to_register_args(params)
    register_values = calculate_register_values(**args)
    set_all_string = generate_set_all_parameters_string(register_values, smr_driver_id)
    
    # Send all commands to FPGA
    futures = []
    for line in set_all_string.split("\n"):
        if line.strip():
            command = line.strip() + "\r\n"
            future = tcp_queue.submit_command(command=command, wait_response=True, timeout=1.0)
            futures.append(future)
    
    # Wait for all commands to complete
    for future in futures:
        try:
            success, response_bytes = future.result(timeout=2.0)
            if not success:
                print(f"Warning: Some TCP commands failed when sending initial settings.")
        except Exception as e:
            print(f"Warning: Error waiting for TCP response: {e}")
    
    print("Initial settings sent to FPGA successfully.")
    
    # 4. Create and show delay sweep window
    window = SetDelaysWindow(tcp_queue=tcp_queue, udp_manager=udp_manager)
    window.show()
    
    # Start the delay sweep
    # Pass both the overridden params and the original settings_params (before overrides)
    window.start_delay_sweep(params, smr_driver_id, original_settings_params=settings_params)
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
