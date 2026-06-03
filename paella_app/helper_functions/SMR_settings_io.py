"""SMR Settings I/O helper module.

This module provides functionality to read and write SMR settings to CSV files.
Settings are stored with metadata (date, time, chip_name, system_name, settings_type,
substrate_bias) followed by all FPGA user parameters.
"""

import os
import sys
import csv
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

# Ensure project root is on sys.path when this file is run directly
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

from helper_functions.FPGA_UserParametersToRegisterValues import (  # noqa: E402
    ScientificDoubleSpinBox,
    calculate_register_values,
)
from helper_functions.SYSTEM_pull_config_io import (  # noqa: E402
    load_system_config,
    get_reference_paths,
    get_system_name,
    get_operators,
)


def _get_smr_settings_path() -> Optional[str]:
    """Get smr_settings_path from system config file.

    Returns:
        smr_settings_path string or None on error.
    """
    try:
        paths = get_reference_paths()
        smr_settings_path = paths.get("smr_settings_path")

        if not smr_settings_path:
            print("Warning: smr_settings_path not found in config")
            return None

        return smr_settings_path

    except Exception as e:
        print(f"Error getting smr_settings_path: {e}")
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

        matching_rows = []
        try:
            with open(active_devices_path, mode="r", encoding="utf-8") as tsv_file:
                reader = csv.reader(tsv_file, delimiter="\t")

                for row in reader:
                    if len(row) >= 2:
                        device_name = row[0].strip()
                        row_system_name = row[1].strip()
                        if row_system_name == system_name:
                            matching_rows.append(device_name)
        except Exception as e:
            print(f"Error reading device file: {e}")
            return None, system_name

        if len(matching_rows) == 1:
            return matching_rows[0], system_name
        elif len(matching_rows) > 1:
            print(
                f"Warning: Multiple chips logged for this system. Using first: {matching_rows[0]}"
            )
            return matching_rows[0], system_name
        else:
            print("Warning: No chips logged for this system")
            return None, system_name

    except Exception as e:
        print(f"Error getting chip and system name: {e}")
        return None, None


def _get_csv_file_path(chip_name: str) -> Optional[str]:
    """Get the CSV file path for a given chip name.

    Args:
        chip_name: Name of the chip

    Returns:
        Full path to CSV file or None on error.
    """
    smr_settings_path = _get_smr_settings_path()
    if not smr_settings_path:
        return None

    if not chip_name:
        return None

    # Create directory if it doesn't exist
    os.makedirs(smr_settings_path, exist_ok=True)

    # Build filename: [chip_name].csv
    filename = f"{chip_name}.csv"
    return os.path.join(smr_settings_path, filename)


def _get_fpga_parameter_columns() -> List[str]:
    """Get list of all FPGA parameter column names in order.

    Returns:
        List of column names for FPGA parameters.
    """
    return [
        "smr_driver_id",
        "Run",
        "Enable_AGC",
        "Send_data_to_pc",
        "Run_NCO_at_fixed_freq",
        "Impulse",
        "Input_source",
        "Signal_of_interest",
        "DAC_A_output",
        "DAC_B_output",
        "PLL_datarate_decimation",
        "Frequency",
        "Minimum_frequency",
        "Maximum_frequency",
        "CIC_rate",
        "CIC_bit_shift",
        "PLL_delay",
        "PLL_drive_amplitude",
        "Feedback_delay",
        "Feedback_gain",
        "Resonator_Q",
        "Loop_bandwidth",
        "Loop_order",
    ]


def _get_all_column_headers() -> List[str]:
    """Get list of all CSV column headers in order.

    Returns:
        List of column headers (metadata + FPGA parameters).
    """
    metadata_headers = [
        "date",
        "time",
        "chip_name",
        "system_name",
        "operator",
        "settings_type",
        "substrate_bias",
    ]
    fpga_headers = _get_fpga_parameter_columns()
    return metadata_headers + fpga_headers


def write_smr_settings(
    settings_type: str,
    substrate_bias: float,
    fpga_parameters: Dict[str, Any],
    chip_name: Optional[str] = None,
    system_name: Optional[str] = None,
    operator: Optional[str] = None,
) -> bool:
    """Write SMR settings to CSV file.

    Args:
        settings_type: Type/category of settings (e.g., "sweep", "manual", "calibration")
        substrate_bias: Substrate bias value in volts
        fpga_parameters: Dictionary of FPGA parameter values (keys match column names)
        chip_name: Optional chip name (if None, will be retrieved from config)
        system_name: Optional system name (if None, will be retrieved from config)
        operator: Optional operator name

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Get chip_name and system_name if not provided
        if chip_name is None or system_name is None:
            retrieved_chip, retrieved_system = _get_chip_and_system_name()
            if chip_name is None:
                chip_name = retrieved_chip
            if system_name is None:
                system_name = retrieved_system

        if not chip_name:
            print("Error: Chip name not available. Cannot save settings.")
            return False

        if not system_name:
            print("Warning: System name not available. Using empty string.")

        # Get CSV file path
        csv_path = _get_csv_file_path(chip_name)
        if not csv_path:
            print("Error: Could not determine CSV file path.")
            return False

        # Get current date and time
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        # Prepare row data
        headers = _get_all_column_headers()
        row_data = {
            "date": date_str,
            "time": time_str,
            "chip_name": chip_name if chip_name else "",
            "system_name": system_name if system_name else "",
            "operator": operator if operator else "",
            "settings_type": settings_type,
            "substrate_bias": str(substrate_bias),
        }

        # Add FPGA parameters
        for param_name in _get_fpga_parameter_columns():
            value = fpga_parameters.get(param_name, "")
            # Convert to string, handling different types
            if isinstance(value, bool):
                row_data[param_name] = "True" if value else "False"
            elif isinstance(value, (int, float)):
                row_data[param_name] = str(value)
            else:
                row_data[param_name] = str(value) if value is not None else ""

        # Ensure the directory exists (should already exist from _get_csv_file_path, but double-check)
        csv_dir = os.path.dirname(csv_path)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)
        
        # Check if file exists to determine if we need to write headers
        file_exists = os.path.exists(csv_path)

        # Write to CSV (append mode)
        # If file doesn't exist, 'a' mode will create it
        with open(csv_path, mode="a", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=headers)

            # Write header if file is new
            if not file_exists:
                writer.writeheader()

            # Write row
            writer.writerow(row_data)

        return True

    except Exception as e:
        print(f"Error writing SMR settings: {e}")
        import traceback

        traceback.print_exc()
        return False


def read_smr_settings(chip_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read all SMR settings from CSV file for a given chip.

    Args:
        chip_name: Optional chip name (if None, will be retrieved from config)

    Returns:
        List of dictionaries, each containing one row of settings.
        Returns empty list on error or if file doesn't exist.
    """
    try:
        # Get chip_name if not provided
        if chip_name is None:
            retrieved_chip, _ = _get_chip_and_system_name()
            chip_name = retrieved_chip

        if not chip_name:
            print("Error: Chip name not available. Cannot read settings.")
            return []

        # Get CSV file path
        csv_path = _get_csv_file_path(chip_name)
        if not csv_path:
            print("Error: Could not determine CSV file path.")
            return []

        if not os.path.exists(csv_path):
            print(f"Settings file does not exist: {csv_path}")
            return []

        settings_list = []
        with open(csv_path, mode="r", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)

            for row in reader:
                # Convert row to dictionary, preserving all values as strings
                # Caller can convert types as needed
                settings_dict = dict(row)
                settings_list.append(settings_dict)

        # Sort by date and time in descending order (most recent first)
        # Date format: YYYY-MM-DD, Time format: HH:MM:SS
        def get_datetime_key(settings: Dict[str, Any]) -> Tuple[str, str]:
            date_str = settings.get("date", "0000-00-00")
            time_str = settings.get("time", "00:00:00")
            return (date_str, time_str)
        
        settings_list.sort(key=get_datetime_key, reverse=True)

        return settings_list

    except Exception as e:
        print(f"Error reading SMR settings: {e}")
        import traceback

        traceback.print_exc()
        return []


class LoadSettingsDialog(QDialog):
    """Dialog for loading saved SMR settings from CSV."""

    def __init__(
        self, settings_list: List[Dict[str, Any]], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.settings_list = settings_list
        self.selected_settings: Optional[Dict[str, str]] = None
        self.setup_ui()

    def setup_ui(self) -> None:
        """Set up the dialog UI with table and buttons."""
        self.setWindowTitle("Load SMR Settings")
        self.setMinimumSize(1000, 600)

        layout = QVBoxLayout(self)

        # Instructions label
        info_label = QLabel("Select a row and click 'Use Settings' to load those values:")
        info_label.setStyleSheet("font-size: 10pt; padding: 5px;")
        layout.addWidget(info_label)

        # Table widget
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        # Apply styling for better visual selection
        self.table.setStyleSheet("""
            QTableWidget {
                gridline-color: #d0d0d0;
                background-color: white;
                alternate-background-color: #f5f5f5;
            }
            QTableWidget::item {
                padding: 4px;
                border: none;
            }
            QTableWidget::item:selected {
                background-color: #0078d7;
                color: white;
                font-weight: bold;
            }
            QTableWidget::item:hover {
                background-color: #e3f2fd;
            }
            QHeaderView::section {
                background-color: #e0e0e0;
                padding: 6px;
                border: 1px solid #b0b0b0;
                font-weight: bold;
            }
        """)
        self.table.setAlternatingRowColors(True)

        if not self.settings_list:
            layout.addWidget(QLabel("No settings found."))
        else:
            # Get all column headers
            headers = _get_all_column_headers()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)

            # Populate table with data
            self.table.setRowCount(len(self.settings_list))
            for row_idx, settings_dict in enumerate(self.settings_list):
                for col_idx, header in enumerate(headers):
                    value = settings_dict.get(header, "")
                    item = QTableWidgetItem(str(value))
                    # Make items selectable
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                    self.table.setItem(row_idx, col_idx, item)

            # Resize columns to fit content
            self.table.resizeColumnsToContents()
            
            # Set minimum column width to prevent columns from being too narrow
            for col_idx in range(self.table.columnCount()):
                current_width = self.table.columnWidth(col_idx)
                self.table.setColumnWidth(col_idx, max(current_width, 80))

            # Make table scrollable
            self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            
            # Connect selection change to update button state
            self.table.itemSelectionChanged.connect(self.on_selection_changed)
            
            # Preselect the first row (most recent entry)
            if len(self.settings_list) > 0:
                self.table.selectRow(0)
                # Scroll to top to ensure first row is visible
                self.table.scrollToItem(self.table.item(0, 0))

        layout.addWidget(self.table)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.use_settings_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        if self.use_settings_button is not None:
            self.use_settings_button.setText("Use Settings")
            self.use_settings_button.clicked.connect(self.on_use_settings_clicked)
            # Enable button if settings list is not empty (first row will be preselected)
            if self.settings_list:
                self.use_settings_button.setEnabled(True)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def on_selection_changed(self) -> None:
        """Handle table selection change - enable/disable Use Settings button."""
        if not hasattr(self, 'use_settings_button') or self.use_settings_button is None:
            return
        selected_rows = self.table.selectedIndexes()
        has_selection = len(selected_rows) > 0
        self.use_settings_button.setEnabled(has_selection)
        
        # Provide visual feedback by highlighting the selected row
        if has_selection:
            row = selected_rows[0].row()
            # Scroll to selected row to ensure it's visible
            self.table.scrollToItem(self.table.item(row, 0))

    def on_use_settings_clicked(self) -> None:
        """Handle 'Use Settings' button click."""
        selected_rows = self.table.selectedIndexes()
        if not selected_rows:
            print("Please select a row first.")
            return

        # Accept the dialog (will trigger get_selected_settings)
        self.accept()

    def get_selected_settings(self) -> Optional[Dict[str, str]]:
        """Get the selected row's settings as a dictionary.

        Returns:
            Dictionary of settings or None if no row selected.
        """
        selected_rows = self.table.selectedIndexes()
        if not selected_rows:
            print("No row selected.")
            return None

        # Get the row index (all selected indexes are from the same row)
        row = selected_rows[0].row()

        if row < 0 or row >= len(self.settings_list):
            return None

        # Return the settings dictionary for the selected row
        return self.settings_list[row]


class SMRSettingsWidget(QWidget):
    """Widget for SMR settings input with save/load functionality."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setup_ui()
        self.update_values()

    def setup_ui(self) -> None:
        """Set up the user interface."""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Left column: Input parameters
        input_scroll = QScrollArea()
        input_scroll.setWidgetResizable(True)
        input_scroll.setMaximumWidth(300)
        input_widget = QWidget()
        input_layout = QVBoxLayout(input_widget)

        # Save and Load buttons at the top
        button_layout = QHBoxLayout()
        self.save_button = QPushButton("Save Settings")
        self.save_button.clicked.connect(self.on_save_clicked)
        self.load_button = QPushButton("Load Settings")
        self.load_button.clicked.connect(self.on_load_clicked)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.load_button)
        button_layout.addStretch()
        input_layout.addLayout(button_layout)

        # Metadata section (new fields)
        metadata_group = QGroupBox("Metadata")
        metadata_layout = QFormLayout()

        self.settings_type_input = QLineEdit()
        self.settings_type_input.setPlaceholderText("e.g., sweep, manual, calibration")
        metadata_layout.addRow("Settings type:", self.settings_type_input)

        self.substrate_bias_input = QDoubleSpinBox()
        self.substrate_bias_input.setRange(0.0, 5.0)
        self.substrate_bias_input.setDecimals(1)
        self.substrate_bias_input.setSingleStep(0.5)
        self.substrate_bias_input.setSuffix(" V")
        self.substrate_bias_input.setValue(3.0)
        metadata_layout.addRow("Substrate bias:", self.substrate_bias_input)

        # Operator dropdown
        self.operator_combo = QComboBox()
        try:
            config = load_system_config()
            operators = get_operators(config)
            if operators:
                self.operator_combo.addItems(operators)
            else:
                self.operator_combo.addItem("No operators configured")
        except Exception as e:
            print(f"Error loading operators: {e}")
            self.operator_combo.addItem("Error loading operators")
        metadata_layout.addRow("Operator:", self.operator_combo)

        metadata_group.setLayout(metadata_layout)
        input_layout.addWidget(metadata_group)

        # Create all input widgets (same as FPGA_UserParametersToRegisterValues)
        self.smr_driver_id = QSpinBox()
        self.smr_driver_id.setRange(0, 1000)
        self.smr_driver_id.setValue(0)
        self.run_check = QCheckBox()
        self.send_data_to_pc_check = QCheckBox()
        self.send_data_to_pc_check.setChecked(True)
        self.run_nco_at_fixed_freq_check = QCheckBox()
        self.input_source_combo = QComboBox()
        self.input_source_combo.addItems(["channel_a", "channel_b"])
        self.input_source_combo.setCurrentIndex(0)
        self.dac_a_output_combo = QComboBox()
        self.dac_a_output_combo.addItems(
            ["off", "PLL NCO", "Feedback", "Feedthrough", "Mixed data"]
        )
        self.dac_a_output_combo.setCurrentIndex(0)
        self.dac_b_output_combo = QComboBox()
        self.dac_b_output_combo.addItems(
            ["off", "PLL NCO", "Feedback", "Feedthrough", "Mixed data"]
        )
        self.dac_b_output_combo.setCurrentIndex(0)
        self.signal_of_interest_combo = QComboBox()
        self.signal_of_interest_combo.addItems(
            ["PLL Frequency", "error signal", "magnitude", "agc error signal", "mixdown"]
        )
        self.signal_of_interest_combo.setCurrentIndex(0)
        self.frequency = ScientificDoubleSpinBox()
        self.frequency.setRange(0.0, 1e7)
        self.frequency.setValue(1000000.0)
        self.frequency.setDecimals(3)
        self.cic_rate = QSpinBox()
        self.cic_rate.setRange(0, 100000)
        self.cic_rate.setValue(32767)
        self.cic_bit_shift = QSpinBox()
        self.cic_bit_shift.setRange(0, 100)
        self.cic_bit_shift.setValue(16)

        self.feedback_delay = QSpinBox()
        self.feedback_delay.setRange(0, 100000)
        self.feedback_delay.setValue(0)
        self.feedback_gain = QDoubleSpinBox()
        self.feedback_gain.setRange(0.0, 1e9)
        self.feedback_gain.setValue(0.1)
        self.feedback_gain.setDecimals(0)

        self.pll_datarate_decimation = QComboBox()
        self.pll_datarate_decimation.addItems(["1", "2", "4", "8", "16", "32"])
        self.pll_datarate_decimation.setCurrentIndex(0)
        self.minimum_frequency = ScientificDoubleSpinBox()
        self.minimum_frequency.setRange(0.0, 1e7)
        self.minimum_frequency.setValue(999000.0)
        self.minimum_frequency.setDecimals(3)
        self.maximum_frequency = ScientificDoubleSpinBox()
        self.maximum_frequency.setRange(0.0, 1e7)
        self.maximum_frequency.setValue(1001000.0)
        self.maximum_frequency.setDecimals(3)
        self.resonator_q = QDoubleSpinBox()
        self.resonator_q.setRange(0.0, 1e9)
        self.resonator_q.setValue(0.0)
        self.resonator_q.setDecimals(0)
        self.loop_order = QSpinBox()
        self.loop_order.setRange(1, 5)
        self.loop_order.setValue(1)
        self.loop_bandwidth = QDoubleSpinBox()
        self.loop_bandwidth.setRange(0.0, 1e9)
        self.loop_bandwidth.setValue(10000000.0)
        self.loop_bandwidth.setDecimals(0)
        self.pll_delay = QDoubleSpinBox()
        self.pll_delay.setRange(0.0, 1.0)
        self.pll_delay.setValue(0.0)
        self.pll_delay.setDecimals(3)
        self.pll_delay.setSingleStep(0.01)
        self.pll_drive_amplitude = QDoubleSpinBox()
        self.pll_drive_amplitude.setRange(0.0, 1.0)
        self.pll_drive_amplitude.setValue(0.1)
        self.pll_drive_amplitude.setDecimals(3)
        self.pll_drive_amplitude.setSingleStep(0.01)

        self.enable_agc_check = QCheckBox()
        self.enable_agc_check.setChecked(True)
        self.impulse_check = QCheckBox()

        # Main section
        main_group = QGroupBox("Main")
        main_group_layout = QFormLayout()
        main_group_layout.addRow("SmrDriverId:", self.smr_driver_id)
        main_group_layout.addRow("Run:", self.run_check)
        main_group_layout.addRow("Send data to PC:", self.send_data_to_pc_check)
        main_group_layout.addRow("Run NCO at fixed freq:", self.run_nco_at_fixed_freq_check)
        main_group_layout.addRow("Input source:", self.input_source_combo)
        main_group_layout.addRow("DAC A output:", self.dac_a_output_combo)
        main_group_layout.addRow("DAC B output:", self.dac_b_output_combo)
        main_group_layout.addRow("Signal of interest:", self.signal_of_interest_combo)
        main_group_layout.addRow("Frequency:", self.frequency)
        main_group_layout.addRow("CIC rate:", self.cic_rate)
        main_group_layout.addRow("CIC bit shift:", self.cic_bit_shift)
        main_group.setLayout(main_group_layout)
        input_layout.addWidget(main_group)

        # Feedback section
        feedback_group = QGroupBox("Feedback")
        feedback_layout = QFormLayout()
        feedback_layout.addRow("Feedback delay:", self.feedback_delay)
        feedback_layout.addRow("Feedback gain:", self.feedback_gain)
        feedback_group.setLayout(feedback_layout)
        input_layout.addWidget(feedback_group)

        # PLL section
        pll_group = QGroupBox("PLL")
        pll_layout = QFormLayout()
        pll_layout.addRow("PLL datarate decimation:", self.pll_datarate_decimation)
        pll_layout.addRow("Minimum frequency:", self.minimum_frequency)
        pll_layout.addRow("Maximum frequency:", self.maximum_frequency)
        pll_layout.addRow("Resonator Q:", self.resonator_q)
        pll_layout.addRow("Loop order:", self.loop_order)
        pll_layout.addRow("Loop bandwidth:", self.loop_bandwidth)
        pll_layout.addRow("PLL delay:", self.pll_delay)
        pll_layout.addRow("PLL drive amplitude:", self.pll_drive_amplitude)
        pll_layout.addRow("Impulse:", self.impulse_check)
        pll_layout.addRow("Enable AGC:", self.enable_agc_check)
        pll_group.setLayout(pll_layout)
        input_layout.addWidget(pll_group)

        input_layout.addStretch()
        input_scroll.setWidget(input_widget)
        main_layout.addWidget(input_scroll)

        # Middle column: Register Values
        register_scroll = QScrollArea()
        register_scroll.setWidgetResizable(True)
        register_scroll.setMaximumWidth(300)
        register_widget = QWidget()
        register_layout = QVBoxLayout(register_widget)

        register_title = QLabel("Register Values (U32 Format)")
        register_title.setStyleSheet("font-size: 14pt; font-weight: bold; padding: 10px;")
        register_layout.addWidget(register_title)

        self.output_display = QFormLayout()

        self.output_labels = {}
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

        for name in output_names:
            label = QLabel("0")
            label.setStyleSheet(
                """
                font-family: 'Courier New', monospace;
                font-size: 10pt;
                padding: 3px;
                background-color: #f9f9f9;
                border: 1px solid #ddd;
                border-radius: 3px;
            """
            )
            self.output_labels[name] = label
            display_name = name.replace("_", " ").title()
            self.output_display.addRow(f"{display_name}:", label)

        register_layout.addLayout(self.output_display)
        register_layout.addStretch()
        register_scroll.setWidget(register_widget)
        main_layout.addWidget(register_scroll)

        # Right column: SetAllParametersString
        set_all_scroll = QScrollArea()
        set_all_scroll.setWidgetResizable(True)
        set_all_scroll.setMaximumWidth(300)
        set_all_widget = QWidget()
        set_all_layout = QVBoxLayout(set_all_widget)

        set_all_title = QLabel("SetAllParametersString")
        set_all_title.setStyleSheet(
            "font-size: 14pt; font-weight: bold; padding: 10px;"
        )
        set_all_layout.addWidget(set_all_title)

        self.set_all_parameters_label = QLabel("")
        self.set_all_parameters_label.setStyleSheet(
            """
            font-family: 'Courier New', monospace;
            font-size: 9pt;
            padding: 5px;
            background-color: #f9f9f9;
            border: 1px solid #ddd;
            border-radius: 3px;
        """
        )
        self.set_all_parameters_label.setWordWrap(True)
        self.set_all_parameters_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_all_layout.addWidget(self.set_all_parameters_label)

        set_all_layout.addStretch()
        set_all_scroll.setWidget(set_all_widget)
        main_layout.addWidget(set_all_scroll)

        # Connect all inputs to update function
        for widget in [
            self.smr_driver_id,
            self.run_check,
            self.enable_agc_check,
            self.send_data_to_pc_check,
            self.run_nco_at_fixed_freq_check,
            self.impulse_check,
            self.frequency,
            self.minimum_frequency,
            self.maximum_frequency,
            self.cic_rate,
            self.cic_bit_shift,
            self.pll_delay,
            self.pll_drive_amplitude,
            self.feedback_delay,
            self.feedback_gain,
            self.resonator_q,
            self.loop_bandwidth,
            self.loop_order,
        ]:
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.update_values)
            else:
                widget.valueChanged.connect(self.update_values)

        # Connect comboboxes separately
        self.input_source_combo.currentIndexChanged.connect(self.update_values)
        self.signal_of_interest_combo.currentIndexChanged.connect(self.update_values)
        self.dac_a_output_combo.currentIndexChanged.connect(self.update_values)
        self.dac_b_output_combo.currentIndexChanged.connect(self.update_values)
        self.pll_datarate_decimation.currentIndexChanged.connect(self.update_values)

        self._setup_styles()

    def _generate_set_all_parameters_string(self, register_values: Dict[str, int]) -> str:
        """Generate SetAllParametersString from register values."""
        smr_driver_id = self.smr_driver_id.value()
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

    def update_values(self) -> None:
        """Update all register values based on current inputs."""
        # Get all input values
        values = calculate_register_values(
            Run=self.run_check.isChecked(),
            Enable_AGC=self.enable_agc_check.isChecked(),
            Send_data_to_pc=self.send_data_to_pc_check.isChecked(),
            Run_NCO_at_fixed_freq=self.run_nco_at_fixed_freq_check.isChecked(),
            Impulse=self.impulse_check.isChecked(),
            Input_source=self.input_source_combo.currentIndex(),
            Signal_of_interest=self.signal_of_interest_combo.currentIndex(),
            DAC_A_output=self.dac_a_output_combo.currentIndex(),
            DAC_B_output=self.dac_b_output_combo.currentIndex(),
            PLL_datarate_decimation=self.pll_datarate_decimation.currentIndex(),
            Frequency=self.frequency.value(),
            Minimum_frequency=self.minimum_frequency.value(),
            Maximum_frequency=self.maximum_frequency.value(),
            CIC_rate=self.cic_rate.value(),
            CIC_bit_shift=self.cic_bit_shift.value(),
            PLL_delay=self.pll_delay.value(),
            PLL_drive_amplitude=self.pll_drive_amplitude.value(),
            Feedback_delay=self.feedback_delay.value(),
            Feedback_gain=self.feedback_gain.value(),
            Resonator_Q=self.resonator_q.value(),
            Loop_bandwidth=self.loop_bandwidth.value(),
            Loop_order=self.loop_order.value(),
        )

        # Update all output labels
        for name, label in self.output_labels.items():
            value = values.get(name, 0)
            label.setText(f"{value}")

        # Update SetAllParametersString
        set_all_string = self._generate_set_all_parameters_string(values)
        self.set_all_parameters_label.setText(set_all_string)

    def get_fpga_parameters_dict(self) -> Dict[str, Any]:
        """Get current FPGA parameters as a dictionary.

        Returns:
            Dictionary of parameter names to values.
        """
        return {
            "smr_driver_id": self.smr_driver_id.value(),
            "Run": self.run_check.isChecked(),
            "Enable_AGC": self.enable_agc_check.isChecked(),
            "Send_data_to_pc": self.send_data_to_pc_check.isChecked(),
            "Run_NCO_at_fixed_freq": self.run_nco_at_fixed_freq_check.isChecked(),
            "Impulse": self.impulse_check.isChecked(),
            "Input_source": self.input_source_combo.currentText(),
            "Signal_of_interest": self.signal_of_interest_combo.currentText(),
            "DAC_A_output": self.dac_a_output_combo.currentText(),
            "DAC_B_output": self.dac_b_output_combo.currentText(),
            "PLL_datarate_decimation": self.pll_datarate_decimation.currentText(),
            "Frequency": self.frequency.value(),
            "Minimum_frequency": self.minimum_frequency.value(),
            "Maximum_frequency": self.maximum_frequency.value(),
            "CIC_rate": self.cic_rate.value(),
            "CIC_bit_shift": self.cic_bit_shift.value(),
            "PLL_delay": self.pll_delay.value(),
            "PLL_drive_amplitude": self.pll_drive_amplitude.value(),
            "Feedback_delay": self.feedback_delay.value(),
            "Feedback_gain": self.feedback_gain.value(),
            "Resonator_Q": self.resonator_q.value(),
            "Loop_bandwidth": self.loop_bandwidth.value(),
            "Loop_order": self.loop_order.value(),
        }

    def on_save_clicked(self) -> None:
        """Handle save button click."""
        settings_type = self.settings_type_input.text().strip()
        if not settings_type:
            print("Error: Settings type is required.")
            return

        substrate_bias = self.substrate_bias_input.value()
        operator = self.operator_combo.currentText()
        fpga_parameters = self.get_fpga_parameters_dict()

        success = write_smr_settings(
            settings_type=settings_type,
            substrate_bias=substrate_bias,
            fpga_parameters=fpga_parameters,
            operator=operator,
        )

        if success:
            print("Settings saved successfully!")
        else:
            print("Error: Failed to save settings.")

    def on_load_clicked(self) -> None:
        """Handle load button click - show dialog with saved settings."""
        settings_list = read_smr_settings()
        if not settings_list:
            print("No saved settings found.")
            return

        dialog = LoadSettingsDialog(settings_list, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_settings = dialog.get_selected_settings()
            if selected_settings:
                self.load_settings_from_dict(selected_settings)

    def load_settings_from_dict(self, settings_dict: Dict[str, str]) -> None:
        """Load settings from a dictionary (from CSV row) into GUI widgets.

        Args:
            settings_dict: Dictionary of setting names to string values.
        """
        # Helper function to convert string to bool
        def str_to_bool(s: str) -> bool:
            return s.lower() in ("true", "1", "yes", "on")

        # Helper function to convert string to int
        def str_to_int(s: str) -> int:
            try:
                return int(float(s))  # Handle "1.0" -> 1
            except (ValueError, TypeError):
                return 0

        # Helper function to convert string to float
        def str_to_float(s: str) -> float:
            try:
                return float(s)
            except (ValueError, TypeError):
                return 0.0

        # Load metadata
        if "settings_type" in settings_dict:
            self.settings_type_input.setText(settings_dict["settings_type"])
        if "substrate_bias" in settings_dict:
            self.substrate_bias_input.setValue(str_to_float(settings_dict["substrate_bias"]))
        if "operator" in settings_dict:
            index = self.operator_combo.findText(settings_dict["operator"])
            if index >= 0:
                self.operator_combo.setCurrentIndex(index)

        # Load FPGA parameters
        if "smr_driver_id" in settings_dict:
            self.smr_driver_id.setValue(str_to_int(settings_dict["smr_driver_id"]))
        if "Run" in settings_dict:
            self.run_check.setChecked(str_to_bool(settings_dict["Run"]))
        if "Enable_AGC" in settings_dict:
            self.enable_agc_check.setChecked(str_to_bool(settings_dict["Enable_AGC"]))
        if "Send_data_to_pc" in settings_dict:
            self.send_data_to_pc_check.setChecked(str_to_bool(settings_dict["Send_data_to_pc"]))
        if "Run_NCO_at_fixed_freq" in settings_dict:
            self.run_nco_at_fixed_freq_check.setChecked(
                str_to_bool(settings_dict["Run_NCO_at_fixed_freq"])
            )
        if "Impulse" in settings_dict:
            self.impulse_check.setChecked(str_to_bool(settings_dict["Impulse"]))

        # Combo boxes - set by text
        if "Input_source" in settings_dict:
            index = self.input_source_combo.findText(settings_dict["Input_source"])
            if index >= 0:
                self.input_source_combo.setCurrentIndex(index)
        if "Signal_of_interest" in settings_dict:
            index = self.signal_of_interest_combo.findText(settings_dict["Signal_of_interest"])
            if index >= 0:
                self.signal_of_interest_combo.setCurrentIndex(index)
        if "DAC_A_output" in settings_dict:
            index = self.dac_a_output_combo.findText(settings_dict["DAC_A_output"])
            if index >= 0:
                self.dac_a_output_combo.setCurrentIndex(index)
        if "DAC_B_output" in settings_dict:
            index = self.dac_b_output_combo.findText(settings_dict["DAC_B_output"])
            if index >= 0:
                self.dac_b_output_combo.setCurrentIndex(index)
        if "PLL_datarate_decimation" in settings_dict:
            # Ensure we treat the value as text, not as an index
            # The combo box items are ['1', '2', '4', '8', '16', '32']
            # We need to match by text value, not by index
            value_str = str(settings_dict["PLL_datarate_decimation"]).strip()
            
            # First try to match the text directly
            index = self.pll_datarate_decimation.findText(value_str)
            if index >= 0:
                self.pll_datarate_decimation.setCurrentIndex(index)
            else:
                # If findText fails, try converting to integer (handles "4.0" -> "4")
                # This prevents accidentally using a numeric value as an index
                try:
                    value_num = int(float(value_str))
                    # Check if this numeric value exists in the combo box items
                    value_str_from_num = str(value_num)
                    index = self.pll_datarate_decimation.findText(value_str_from_num)
                    if index >= 0:
                        self.pll_datarate_decimation.setCurrentIndex(index)
                    else:
                        # Last resort: check if the value is a valid index (0-5)
                        # but only use it if it matches the expected item at that index
                        # This prevents the bug where index 4 (which is "16") is used when value is "4"
                        if 0 <= value_num < self.pll_datarate_decimation.count():
                            item_at_index = self.pll_datarate_decimation.itemText(value_num)
                            if item_at_index == value_str_from_num:
                                # The index matches the value, so it's safe to use
                                self.pll_datarate_decimation.setCurrentIndex(value_num)
                            else:
                                print(f"Warning: PLL_datarate_decimation value '{value_str}' would map to index {value_num} "
                                      f"(item '{item_at_index}'), but expected '{value_str_from_num}'. "
                                      f"This suggests the CSV may have stored an index instead of a value.")
                        else:
                            print(f"Warning: PLL_datarate_decimation value '{value_str}' not found in combo box items.")
                except (ValueError, TypeError):
                    print(f"Warning: Could not parse PLL_datarate_decimation value '{value_str}'.")

        # Numeric values
        if "Frequency" in settings_dict:
            self.frequency.setValue(str_to_float(settings_dict["Frequency"]))
        if "Minimum_frequency" in settings_dict:
            self.minimum_frequency.setValue(str_to_float(settings_dict["Minimum_frequency"]))
        if "Maximum_frequency" in settings_dict:
            self.maximum_frequency.setValue(str_to_float(settings_dict["Maximum_frequency"]))
        if "CIC_rate" in settings_dict:
            self.cic_rate.setValue(str_to_int(settings_dict["CIC_rate"]))
        if "CIC_bit_shift" in settings_dict:
            self.cic_bit_shift.setValue(str_to_int(settings_dict["CIC_bit_shift"]))
        if "PLL_delay" in settings_dict:
            self.pll_delay.setValue(str_to_float(settings_dict["PLL_delay"]))
        if "PLL_drive_amplitude" in settings_dict:
            self.pll_drive_amplitude.setValue(str_to_float(settings_dict["PLL_drive_amplitude"]))
        if "Feedback_delay" in settings_dict:
            self.feedback_delay.setValue(str_to_int(settings_dict["Feedback_delay"]))
        if "Feedback_gain" in settings_dict:
            self.feedback_gain.setValue(str_to_float(settings_dict["Feedback_gain"]))
        if "Resonator_Q" in settings_dict:
            self.resonator_q.setValue(str_to_float(settings_dict["Resonator_Q"]))
        if "Loop_bandwidth" in settings_dict:
            self.loop_bandwidth.setValue(str_to_float(settings_dict["Loop_bandwidth"]))
        if "Loop_order" in settings_dict:
            self.loop_order.setValue(str_to_int(settings_dict["Loop_order"]))

        # Update register values display
        self.update_values()

    def _setup_styles(self) -> None:
        """Apply styles to the widget."""
        self.setStyleSheet(
            """
            QWidget {
                background-color: #f0f0f0;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #ccc;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLabel {
                font-family: 'Segoe UI', Arial;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #999;
                border-radius: 3px;
                background-color: white;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d7;
                border: 2px solid #005a9e;
            }
            QCheckBox::indicator:checked:hover {
                background-color: #005a9e;
                border: 2px solid #004578;
            }
        """
        )


class MainWindow(QMainWindow):
    """Standalone window wrapper for SMRSettingsWidget."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SMR Settings I/O")
        self.setGeometry(100, 100, 950, 1000)
        self.settings_widget = SMRSettingsWidget()
        self.setCentralWidget(self.settings_widget)


def main() -> None:
    """Main entry point for standalone execution."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
