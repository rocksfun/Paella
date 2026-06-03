"""
Main GUI application that displays three major components tiled:
1. Syringe Control
2. SMR Control
3. Image Control
"""

import sys
import os
import csv
import multiprocessing
from datetime import datetime
from typing import Optional
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSizePolicy, QGridLayout, QDialog, QDialogButtonBox,
    QPushButton, QComboBox, QFormLayout, QGroupBox, QRadioButton, QButtonGroup,
    QListWidget, QListWidgetItem, QTextEdit
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, QTimer
from pyPump import SyringeControlWidget
from pySMR import SMRControlWidget
from pyImage import ImageControlWidget
from helper_functions.SYSTEM_pull_config_io import (
    load_system_config,
    get_reference_paths,
    get_system_name,
    get_operators,
    get_camera_settings,
)
from helper_functions.UIUX_elements import (
    create_button, create_status_label, create_status_badge,
    Colors
)

# References directory relative to script location
if hasattr(sys, '_MEIPASS'):
    # When running as a bundled executable
    _SCRIPT_DIR = sys._MEIPASS
else:
    # When running from source
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

REFERENCES_DIR = os.path.join(_SCRIPT_DIR, 'references')
APP_ICON_PATH = os.path.join(REFERENCES_DIR, 'travera_logo.ico')


class ConsoleLogRedirector:
    """Redirects stdout and stderr to both the console and a file."""
    def __init__(self, original_stdout, original_stderr):
        self.terminal_stdout = original_stdout
        self.terminal_stderr = original_stderr
        self.log_file = None
        self.active = False
        import threading
        self.lock = threading.RLock()

    def start_logging(self, sample_path: str, experiment_string: str):
        """Start logging to a file in the sample directory."""
        with self.lock:
            # Close any existing log file first to prevent resource leaks
            if self.log_file:
                self.stop_logging()
                
            try:
                log_filename = f"{experiment_string}_console_log.txt"
                log_filepath = os.path.join(sample_path, log_filename)
                self.log_file = open(log_filepath, 'a', encoding='utf-8')
                self.active = True
                print(f"--- Console logging started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
            except Exception as e:
                if self.terminal_stderr:
                    self.terminal_stderr.write(f"Error starting console log file: {e}\n")

    def stop_logging(self):
        """Stop logging to file."""
        with self.lock:
            if self.log_file:
                try:
                    print(f"--- Console logging stopped: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
                    self.log_file.flush()
                    self.log_file.close()
                except Exception as e:
                    if self.terminal_stderr:
                        self.terminal_stderr.write(f"Error closing console log file: {e}\n")
                finally:
                    self.log_file = None
                    self.active = False

    def write(self, message):
        """Write message to both terminal and file."""
        with self.lock:
            if self.terminal_stdout:
                self.terminal_stdout.write(message)
            if self.active and self.log_file:
                try:
                    self.log_file.write(message)
                except:
                    pass

    def flush(self):
        """Flush both terminal and file."""
        with self.lock:
            if self.terminal_stdout:
                self.terminal_stdout.flush()
            if self.active and self.log_file:
                try:
                    self.log_file.flush()
                except:
                    pass


# Global redirector instance
console_redirector = None


class StartupDialog(QDialog):
    """Dialog for selecting Manual Control or Automated Setup."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Startup Mode Selection")
        self.setModal(True)
        self.setMinimumWidth(300)
        
        layout = QVBoxLayout(self)
               
        # Load system configuration once
        # NOTE: This file is READ-ONLY. This code should never write to it.
        from helper_functions.SYSTEM_pull_config_io import SYSTEM_CONFIG_PATH
        config_path = SYSTEM_CONFIG_PATH
        
        self.config = None
        if os.path.exists(config_path):
            try:
                self.config = load_system_config(config_path)
            except Exception as e:
                print(f"Error loading system config: {e}")
        
        # Create status indicator label
        if self.config:
            status_label = create_status_label("✓ System configuration file loaded", "success")
        else:
            status_label = create_status_label("✗ WARNING: System configuration file not found", "error")
        
        layout.addWidget(status_label)
        
        # Check chip logging status (only if config file loaded)
        if self.config:
            chip_status_label = self._check_chip_logging(config_path)
            if chip_status_label:
                layout.addWidget(chip_status_label)
        
        # Add Operator dropdown
        operator_layout = QFormLayout()
        self.operator_combo = QComboBox()
        operator_layout.addRow("Operator:", self.operator_combo)
        
        # Populate operators from config
        if self.config:
            try:
                operators = get_operators(self.config)
                if operators:
                    self.operator_combo.addItems(operators)
                else:
                    self.operator_combo.addItem("No operators configured")
            except Exception as e:
                print(f"Error loading operators: {e}")
                self.operator_combo.addItem("Error loading operators")
        else:
            self.operator_combo.addItem("Config file not found or failed to load")
        
        layout.addLayout(operator_layout)

        label = QLabel("Please select a startup mode:")
        layout.addWidget(label)
        
        # Horizontal layout for buttons - directly side by side
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(20)
        
        # Manual Control button - orange
        manual_btn = create_button("Manual Control", "warning", font_size="14pt", padding="15px 30px", min_width="200px", min_height="50px")
        manual_btn.clicked.connect(lambda: self.accept_with_mode("manual"))
        buttons_layout.addWidget(manual_btn)
        
        # Automated Setup button - green
        automated_btn = create_button("Automated set up", "success", font_size="14pt", padding="15px 30px", min_width="200px", min_height="50px")
        automated_btn.clicked.connect(lambda: self.accept_with_mode("automated"))
        buttons_layout.addWidget(automated_btn)
        
        layout.addLayout(buttons_layout)
        
        # Camera Mode toggle centered below both buttons
        camera_mode_group = QGroupBox("Camera Mode")
        camera_mode_layout = QHBoxLayout()
        camera_mode_layout.setSpacing(10)
        
        # Load default camera mode from system config
        default_camera_mode = "BF+FL"
        if self.config:
            try:
                cam_settings = get_camera_settings(self.config)
                default_camera_mode = cam_settings.get("camera_mode", "BF+FL")
                print(f"Startup default camera mode: {default_camera_mode}")
            except Exception as e:
                print(f"Error loading camera settings for startup: {e}")

        self.camera_mode_group = QButtonGroup()
        self.bf_only_radio = QRadioButton("BF only")
        self.bf_fl_radio = QRadioButton("BF + FL")
        
        # Set initial checked state based on config
        if default_camera_mode == "BF only":
            self.bf_only_radio.setChecked(True)
            self.camera_mode = "BF only"
        else:
            self.bf_fl_radio.setChecked(True)
            self.camera_mode = "BF+FL"
        
        self.camera_mode_group.addButton(self.bf_only_radio, 0)
        self.camera_mode_group.addButton(self.bf_fl_radio, 1)
        
        camera_mode_layout.addWidget(self.bf_only_radio)
        camera_mode_layout.addWidget(self.bf_fl_radio)
        camera_mode_group.setLayout(camera_mode_layout)
        
        # Horizontal container for Camera Mode and Fluidic startup sequence side by side
        options_container = QHBoxLayout()
        options_container.setSpacing(30)
        
        # Center the camera mode group
        camera_mode_container = QHBoxLayout()
        camera_mode_container.addStretch()
        camera_mode_container.addWidget(camera_mode_group)
        camera_mode_container.addStretch()
        options_container.addLayout(camera_mode_container)
        
        # Fluidic startup sequence group
        fluidic_startup_group = QGroupBox("Fluidic startup sequence")
        fluidic_startup_layout = QHBoxLayout()
        fluidic_startup_layout.setSpacing(10)
        
        self.fluidic_startup_group = QButtonGroup()
        self.full_system_prime_radio = QRadioButton("Full system prime")
        self.prime_reagents_only_radio = QRadioButton("Prime Reagents only")
        self.full_system_prime_radio.setChecked(True)  # Default to Full system prime
        
        self.fluidic_startup_group.addButton(self.full_system_prime_radio, 0)
        self.fluidic_startup_group.addButton(self.prime_reagents_only_radio, 1)
        
        fluidic_startup_layout.addWidget(self.full_system_prime_radio)
        fluidic_startup_layout.addWidget(self.prime_reagents_only_radio)
        fluidic_startup_group.setLayout(fluidic_startup_layout)
        
        # Center the fluidic startup group
        fluidic_startup_container = QHBoxLayout()
        fluidic_startup_container.addStretch()
        fluidic_startup_container.addWidget(fluidic_startup_group)
        fluidic_startup_container.addStretch()
        options_container.addLayout(fluidic_startup_container)
        
        layout.addLayout(options_container)
        
        self.selected_mode = None
        self.fluidic_startup_sequence = "full_system_prime"  # Default fluidic startup sequence
    
    
    def _check_chip_logging(self, config_path):
        """Check chip logging status from TSV file."""
        try:
            # Read system config (READ-ONLY - never write to this file)
            config = load_system_config(config_path)
            
            # Get system name from config
            system_name = get_system_name(config)
            
            if not system_name:
                return create_status_label("✗ WARNING: System name not found in config", "error")
            
            # Get active_devices_path from references section
            paths = get_reference_paths(config)
            active_devices_path = paths.get("active_devices_path")
            
            if not active_devices_path:
                return create_status_label("✗ WARNING: Active devices path not configured", "error")
            
            if not os.path.exists(active_devices_path):
                return create_status_label("✗ WARNING: Active devices file not found", "error")
            
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
                return create_status_label(f"✗ WARNING: Error reading device file: {str(e)}", "error")
            
            # Create status label based on matches
            if len(matching_rows) == 1:
                # Single match - success
                device_name = matching_rows[0]
                return create_status_label(f"✓ Chip {device_name} is logged for this system", "success")
            elif len(matching_rows) > 1:
                # Multiple matches - warning
                return create_status_label("✗ WARNING: Multiple chips are logged for this system", "error")
            else:
                # No matches - warning
                return create_status_label("✗ WARNING: No chips are logged for this system", "error")
            
        except Exception as e:
            # On error, show error message
            print(f"Error checking chip logging: {e}")
            return create_status_label(f"✗ WARNING: Error checking chip logging: {str(e)}", "error")
    
    def accept_with_mode(self, mode):
        """Accept dialog with the selected mode."""
        self.selected_mode = mode
        self.accept()
    
    def get_mode(self):
        """Get the selected mode."""
        return self.selected_mode
    
    def get_operator(self):
        """Get the selected operator."""
        return self.operator_combo.currentText()
    
    def get_camera_mode(self):
        """Get the selected camera mode."""
        if self.bf_fl_radio.isChecked():
            return "BF+FL"
        else:
            return "BF only"
    
    def get_fluidic_startup_sequence(self):
        """Get the selected fluidic startup sequence."""
        if self.full_system_prime_radio.isChecked():
            return "full_system_prime"
        else:
            return "prime_reagents_only"


class AutomatedSetupResultWindow(QDialog):
    """Window to display the result of the automated setup."""
    
    def __init__(self, noise_mhz, parent=None):
        super().__init__(parent)
        self.noise_mhz = noise_mhz
        self.setWindowTitle("Automated Setup Result")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Result Icon/Title
        title_layout = QHBoxLayout()
        self.icon_label = QLabel()
        self.icon_label.setStyleSheet("font-size: 24pt;")
        
        # Define thresholds (mHz)
        # Success: < 500 mHz (0.5 Hz)
        # Warning: 500 mHz <= noise < 2000 mHz (2 Hz)
        # Failure: >= 2000 mHz or 0
        THRESHOLD_SUCCESS = 500.0
        THRESHOLD_FAILURE = 2000.0 # 2 Hz
        
        if noise_mhz is not None and noise_mhz > 0 and noise_mhz < THRESHOLD_SUCCESS:
            # Success
            self.icon_label.setText("✅") # Or use standard icon
            self.title_label = create_status_label("Success: Automated set up complete", "success", font_size="14pt")
            self.title_label.setWordWrap(True)
            
            # Additional details
            details = QLabel(f"Measured Noise: {noise_mhz:.1f} mHz")
            details.setStyleSheet("color: #666;")
            layout.addWidget(details)
            
        elif noise_mhz is not None and noise_mhz >= THRESHOLD_SUCCESS and noise_mhz < THRESHOLD_FAILURE:
            # Warning (Technically success but high noise)
            self.icon_label.setText("⚠️")
            self.title_label = create_status_label("Success: Automated set up complete", "warning", font_size="14pt")
            self.title_label.setWordWrap(True)
            
            warning_label = QLabel("Warning: Noise is too high, please increase PLL Drive until it is below 500mHz")
            warning_label.setWordWrap(True)
            warning_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
            layout.addWidget(warning_label)
            
            details = QLabel(f"Measured Noise: {noise_mhz:.1f} mHz")
            details.setStyleSheet("color: #666;")
            layout.addWidget(details)
            
        else:
            # Failure
            self.icon_label.setText("❌")
            self.title_label = create_status_label("Failure: Automated set up could not achieve adequate SMR performance, please manually set up this device", "error", font_size="14pt")
            self.title_label.setWordWrap(True)
            
            if noise_mhz is not None and noise_mhz > 0:
                details = QLabel(f"Measured Noise: {noise_mhz:.1f} mHz (Threshold: {THRESHOLD_FAILURE/1000.0:.1f} Hz)")
                details.setStyleSheet("color: #666;")
                layout.addWidget(details)
        
        title_layout.addWidget(self.icon_label)
        title_layout.addWidget(self.title_label)
        title_layout.addStretch()
        layout.addLayout(title_layout)
        
        # OK Button
        button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        button_box.accepted.connect(self.accept)
        layout.addWidget(button_box)


class AutomatedSetupStatusWindow(QDialog):
    """Status window showing progress of automated setup across three modules."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Automated Setup Status")
        self.setModal(False)
        self.setMinimumSize(900, 600)
        # Keep status window on top of other dialogs but not system modal
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(15, 15, 15, 15)
        
        # Title
        title_label = QLabel("Automated Setup Progress")
        title_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        main_layout.addWidget(title_label)
        
        # Three-column layout for modules
        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(15)
        
        # Imaging Module column
        imaging_column = self._create_module_column("Imaging Module (pyImage)", [
            "Initialize cameras",
            "Turn on red LED",
            "Turn on blue LED"
        ])
        columns_layout.addWidget(imaging_column)
        self.imaging_column = imaging_column
        
        # Fluidic Module column
        fluidic_column = self._create_module_column("Fluidic Module (pyPump)", [
            "Connect COM port",
            "Initialize syringes",
            "Prime System"
        ])
        columns_layout.addWidget(fluidic_column)
        self.fluidic_column = fluidic_column
        
        # SMR Module column
        smr_column = self._create_module_column("SMR Module (pySMR)", [
            "Reset FPGA",
            "Initialize TCP/UDP",
            "Initialize SMR sweep",
            "Set Delays",
            "Monitor noise"
        ])
        columns_layout.addWidget(smr_column)
        self.smr_column = smr_column
        
        main_layout.addLayout(columns_layout)
        
        # Store references to task lists and status boxes
        self.imaging_tasks = self._get_task_list(imaging_column)
        self.imaging_status = self._get_status_box(imaging_column)
        self.fluidic_tasks = self._get_task_list(fluidic_column)
        self.fluidic_status = self._get_status_box(fluidic_column)
        self.smr_tasks = self._get_task_list(smr_column)
        self.smr_status = self._get_status_box(smr_column)
    
    def _create_module_column(self, title, task_names):
        """Create a column widget for a module."""
        column_widget = QWidget()
        column_layout = QVBoxLayout(column_widget)
        column_layout.setSpacing(10)
        column_layout.setContentsMargins(10, 10, 10, 10)
        
        # Title
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 12pt; font-weight: bold;")
        column_layout.addWidget(title_label)
        
        # Task list
        task_list = QListWidget()
        task_list.setMaximumHeight(200)
        task_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: #f9f9f9;
            }
            QListWidget::item {
                padding: 5px;
                border-bottom: 1px solid #e0e0e0;
            }
        """)
        for task_name in task_names:
            item = QListWidgetItem(f"○ {task_name}")
            item.setData(Qt.ItemDataRole.UserRole, task_name)
            task_list.addItem(item)
        
        column_layout.addWidget(task_list)
        
        # Status box
        status_box = QTextEdit()
        status_box.setMaximumHeight(100)
        status_box.setReadOnly(True)
        status_box.setStyleSheet("""
            QTextEdit {
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: #ffffff;
                font-size: 10pt;
            }
        """)
        status_box.setPlainText("Waiting to start...")
        column_layout.addWidget(status_box)
        
        # Store references in widget
        column_widget.task_list = task_list
        column_widget.status_box = status_box
        
        return column_widget
    
    def _get_task_list(self, column_widget):
        """Get the task list widget from a column."""
        return column_widget.task_list
    
    def _get_status_box(self, column_widget):
        """Get the status box widget from a column."""
        return column_widget.status_box
    
    def update_task_status(self, module, task_name, status):
        """Update the status of a specific task.
        
        Args:
            module: 'imaging', 'fluidic', or 'smr'
            task_name: Name of the task to update
            status: 'pending', 'in_progress', 'success', or 'error'
        """
        if module == 'imaging':
            task_list = self.imaging_tasks
        elif module == 'fluidic':
            task_list = self.fluidic_tasks
        elif module == 'smr':
            task_list = self.smr_tasks
        else:
            return
        
        # Find and update the task item
        for i in range(task_list.count()):
            item = task_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == task_name:
                if status == 'pending':
                    item.setText(f"○ {task_name}")
                    item.setForeground(Qt.GlobalColor.gray)
                elif status == 'in_progress':
                    item.setText(f"⟳ {task_name}")
                    item.setForeground(Qt.GlobalColor.blue)
                elif status == 'success':
                    item.setText(f"✓ {task_name}")
                    item.setForeground(Qt.GlobalColor.green)
                elif status == 'error':
                    item.setText(f"✗ {task_name}")
                    item.setForeground(Qt.GlobalColor.red)
                break
    
    def update_module_status(self, module, message):
        """Update the status message for a module.
        
        Args:
            module: 'imaging', 'fluidic', or 'smr'
            message: Status message to display
        """
        if module == 'imaging':
            status_box = self.imaging_status
        elif module == 'fluidic':
            status_box = self.fluidic_status
        elif module == 'smr':
            status_box = self.smr_status
        else:
            return
        
        status_box.setPlainText(message)


class MainApplicationWindow(QMainWindow):
    """Main application window that tiles all three control components."""
    def __init__(self, operator: Optional[str] = None):
        super().__init__()
        self.operator = operator  # Store operator from startup dialog
        self.setWindowTitle("Travera Paella - SMR control suite")
        self.setWindowIcon(QIcon(APP_ICON_PATH))
        self.setGeometry(50, 50, 1600, 900)
        
        # Create central widget with grid layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Use grid layout for custom positioning
        # Layout: Image (top-left, 66% width, 65% height) | Syringe (top-right, 34% width, 65% height)
        #         SMR (bottom, 100% width, 35% height)
        main_layout = QGridLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)
        
        # Create component widgets and store references
        self.image_widget = ImageControlWidget()
        self.syringe_widget = SyringeControlWidget()
        self.smr_widget = SMRControlWidget(operator=operator)  # Pass operator to SMR widget
        
        # Connect image widget to SMR widget for sample path access
        self.image_widget.set_smr_widget(self.smr_widget)
        
        # Connect SMR widget to syringe widget for conditions loading
        self.syringe_widget.set_smr_widget(self.smr_widget)
        
        # Connect syringe widget to SMR widget for conditions loading
        # Connect SMR widget to syringe widget for conditions loading
        self.smr_widget.set_pump_widget(self.syringe_widget)
        
        # Connect syringe widget to image widget for controlling image saving
        self.syringe_widget.set_image_widget(self.image_widget)
        
        # Connect GUI mode changes from SMR widget to other widgets
        self.smr_widget.gui_mode_changed.connect(self.image_widget.set_gui_mode)
        self.smr_widget.gui_mode_changed.connect(self.syringe_widget.set_gui_mode)
        
        # Connect module statuses to SMR widget for status indicators
        self.syringe_widget.fluidic_state_changed.connect(self.smr_widget.update_fluidic_state)
        # Pass running condition state to image widget to strictly gate image saving 
        self.syringe_widget.fluidic_state_changed.connect(
            lambda state, desc: setattr(self.image_widget, 'condition_running', state == self.syringe_widget.FLUIDIC_STATE_RUNNING_SAMPLE)
        )
        # Handle imaging status connection (image saving + condition running)
        self.image_widget.image_saving_toggled.connect(self.smr_widget.set_imaging_status)
        self.image_widget.roi_mode_toggled.connect(self.smr_widget.set_roi_mode_status)
        self.image_widget.camera_mode_changed.connect(self.syringe_widget.set_camera_mode)
        
        # Create frame containers for each component
        image_frame = self._create_component_frame(self.image_widget, "#e0ffe0")  # Pale green
        syringe_frame = self._create_component_frame(self.syringe_widget, "#e0f0ff")  # Pale blue
        smr_frame = self._create_component_frame(self.smr_widget, "#ffe0cc")  # Pale orange
        
        # Top row: Image (66% width) and Syringe (34% width), both 65% height
        # Stretch factors: Image gets 2, Syringe gets 1 (2:1 ratio ≈ 66.7%:33.3%)
        main_layout.addWidget(image_frame, 0, 0)  # Row 0, Col 0
        main_layout.addWidget(syringe_frame, 0, 1)  # Row 0, Col 1
        main_layout.setColumnStretch(0, 2)  # Image column gets 2x stretch (66.7%)
        main_layout.setColumnStretch(1, 1)  # Syringe column gets 1x stretch (33.3%)
        main_layout.setRowStretch(0, 13)  # Top row gets 13x stretch (65% height)
        
        # Bottom row: SMR (100% width, 35% height)
        main_layout.addWidget(smr_frame, 1, 0, 1, 2)  # Row 1, spans both columns
        main_layout.setRowStretch(1, 7)  # Bottom row gets 7x stretch (35% height)
        
        # Connect console logging signals
        global console_redirector
        if console_redirector:
            self.smr_widget.saving_started.connect(console_redirector.start_logging)
            self.smr_widget.console_log_finished.connect(console_redirector.stop_logging)
        
        # Connect image saving signals to ensure it stays in sync with SMR saving
        self.smr_widget.saving_started.connect(lambda path, exp: self.image_widget.toggle_image_saving(True))
        self.smr_widget.saving_stopped.connect(lambda: self.image_widget.toggle_image_saving(False))
        
        # Apply global styles
        self._setup_styles()

        # Add a periodic flush timer for the console log redirector
        # Flushes every 2 seconds to avoid blocking GUI on every print
        self.log_flush_timer = QTimer(self)
        self.log_flush_timer.timeout.connect(self._flush_logs)
        self.log_flush_timer.start(2000)
    
    def _flush_logs(self):
        """Periodically flush console logs to disk."""
        global console_redirector
        if console_redirector:
            console_redirector.flush()
    
    def _create_component_frame(self, widget, bg_color="#ffffff"):
        """Create a frame container for a component widget."""
        frame = QFrame()
        frame.setFrameStyle(QFrame.Shape.StyledPanel)
        frame.setStyleSheet(f"""
            QFrame {{
                border: 2px solid #ccc;
                border-radius: 5px;
                background-color: {bg_color};
            }}
        """)
        
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Component widget
        layout.addWidget(widget, 1)
        
        return frame
    
    def _setup_styles(self):
        """Apply global application styles."""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #e0e0e0;
            }
        """)
    
    def closeEvent(self, event):
        """Clean up all child widgets when main window is closed."""
        # Stop console logging if active
        global console_redirector
        if console_redirector:
            console_redirector.stop_logging()

        # Clean up all component widgets
        if hasattr(self, 'image_widget'):
            self.image_widget.closeEvent(event)
        
        if hasattr(self, 'syringe_widget'):
            self.syringe_widget.closeEvent(event)
        
        if hasattr(self, 'smr_widget'):
            self.smr_widget.closeEvent(event)
        
        event.accept()
    
    def perform_automated_setup(self, camera_mode="BF+FL", fluidic_startup_sequence="full_system_prime"):
        """Perform automated setup sequence with 5 stages.
        
        Args:
            camera_mode: Camera mode to use ("BF only" or "BF+FL")
            fluidic_startup_sequence: Either "full_system_prime" or "prime_reagents_only"
        """
        # Create and show status window
        self.status_window = AutomatedSetupStatusWindow(self)
        self.status_window.show()
        self.status_window.raise_()
        self.status_window.activateWindow()
        
        # Store camera mode and fluidic startup sequence
        self.automated_setup_camera_mode = camera_mode
        self.automated_setup_fluidic_sequence = fluidic_startup_sequence
        
        # Track stage completion for parallel execution
        self.stage_completion = {
            'imaging': False,
            'fluidic': False,
            'smr': False
        }
        
        # Start Stage 1
        self._stage1_initial_setup()
    
    def _safe_status_window_update(self, update_func):
        """Safely update status window if it exists."""
        if hasattr(self, 'status_window') and self.status_window is not None:
            try:
                update_func()
            except AttributeError as e:
                # Window may have been closed or method doesn't exist
                print(f"Warning: Status window update failed: {e}")
            except Exception as e:
                # Catch any other errors but log them
                print(f"Error updating status window: {e}")
    
    def _stage1_initial_setup(self):
        """Stage 1: Initial module setup - all modules run in parallel."""
        # Reset completion tracking
        self.stage_completion = {
            'imaging': False,
            'fluidic': False,
            'smr': False
        }
        
        # Start all modules in parallel
        self._stage1_imaging()
        self._stage1_smr()
        self._stage1_fluidic()
    
    def _stage1_imaging(self):
        """Stage 1: Imaging module tasks (sequential within module)."""
        self._safe_status_window_update(lambda: self.status_window.update_module_status('imaging', 'Initializing cameras...'))
        self._safe_status_window_update(lambda: self.status_window.update_task_status('imaging', 'Initialize cameras', 'in_progress'))
        
        # pyImage: Initialize cameras with specified mode
        if hasattr(self, 'image_widget'):
            self.image_widget.set_camera_mode_programmatic(self.automated_setup_camera_mode)
            if hasattr(self, 'syringe_widget'):
                self.syringe_widget.set_camera_mode(self.automated_setup_camera_mode)
            self.image_widget.initialize_cameras(self.automated_setup_camera_mode)
            # Wait a bit for cameras to initialize
            QTimer.singleShot(1000, self._stage1_imaging_after_cameras)
        else:
            self._stage1_imaging_after_cameras()
    
    def _stage1_imaging_after_cameras(self):
        """Continue Stage 1 imaging after cameras initialize."""
        self.status_window.update_task_status('imaging', 'Initialize cameras', 'success')
        self.status_window.update_task_status('imaging', 'Turn on red LED', 'in_progress')
        self.status_window.update_module_status('imaging', 'Turning on red LED...')
        
        # Red LED should be turned on automatically by initialize_cameras
        # But ensure it's on
        if hasattr(self, 'image_widget') and hasattr(self.image_widget, 'red_led_state'):
            if not self.image_widget.red_led_state:
                # Turn on red LED if not already on
                from PySide6.QtCore import QMetaObject, Q_ARG
                QMetaObject.invokeMethod(self.image_widget, "toggle_red_led", 
                                       Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))
        
        QTimer.singleShot(500, self._stage1_imaging_complete)
    
    def _stage1_imaging_complete(self):
        """Stage 1 imaging module complete."""
        self.status_window.update_task_status('imaging', 'Turn on red LED', 'success')
        self.stage_completion['imaging'] = True
        
        # Check if other modules are still running
        if not self.stage_completion['fluidic'] or not self.stage_completion['smr']:
            waiting_for = []
            if not self.stage_completion['fluidic']:
                waiting_for.append('Fluidic')
            if not self.stage_completion['smr']:
                waiting_for.append('SMR')
            self.status_window.update_module_status('imaging', f'Waiting for {", ".join(waiting_for)} module(s) to complete Stage 1...')
        else:
            self.status_window.update_module_status('imaging', 'Stage 1 complete')
        
        self._check_stage1_complete()
    
    def _stage1_smr(self):
        """Stage 1: SMR module tasks (sequential within module)."""
        self._safe_status_window_update(lambda: self.status_window.update_task_status('smr', 'Reset FPGA', 'in_progress'))
        self._safe_status_window_update(lambda: self.status_window.update_module_status('smr', 'Resetting FPGA...'))
        
        if hasattr(self, 'smr_widget'):
            # Call reset FPGA programmatically (skip confirmation)
            # Store reference to status window in smr_widget so dialogs can respect z-order
            if hasattr(self, 'status_window'):
                self.smr_widget._automated_setup_status_window = self.status_window
            # Use silent=True to allow parallel execution (no modal dialog)
            self.smr_widget.on_reset_fpga_clicked(skip_confirmation=True, silent=True)

            # Wait for reset to complete (FPGAResetDialog takes ~3 seconds)
            QTimer.singleShot(4000, self._stage1_smr_after_fpga_reset)
        else:
            QTimer.singleShot(100, self._stage1_smr_after_fpga_reset)
    
    def _stage1_smr_after_fpga_reset(self):
        """Continue Stage 1 SMR after FPGA reset."""
        self.status_window.update_task_status('smr', 'Reset FPGA', 'success')
        self.status_window.update_task_status('smr', 'Initialize TCP/UDP', 'in_progress')
        self.status_window.update_module_status('smr', 'Initializing TCP and UDP connections...')
        
        # Initialize TCP and UDP connections
        if hasattr(self, 'smr_widget'):
            if not self.smr_widget.fpga_command_queue.is_connected():
                self.smr_widget.on_tcp_connect_clicked()
            QTimer.singleShot(1000, self._stage1_smr_after_tcp)
        else:
            QTimer.singleShot(100, self._stage1_smr_after_tcp)
    
    def _stage1_smr_after_tcp(self):
        """Continue Stage 1 SMR after TCP connection."""
        if hasattr(self, 'smr_widget'):
            if not self.smr_widget.udp_data_manager.is_connected():
                self.smr_widget.on_udp_connect_clicked()
            QTimer.singleShot(1000, self._stage1_smr_complete)
        else:
            QTimer.singleShot(100, self._stage1_smr_complete)
    
    def _stage1_smr_complete(self):
        """Stage 1 SMR module complete."""
        self.status_window.update_task_status('smr', 'Initialize TCP/UDP', 'success')
        self.stage_completion['smr'] = True
        
        # Check if other modules are still running
        if not self.stage_completion['imaging'] or not self.stage_completion['fluidic']:
            waiting_for = []
            if not self.stage_completion['imaging']:
                waiting_for.append('Imaging')
            if not self.stage_completion['fluidic']:
                waiting_for.append('Fluidic')
            self.status_window.update_module_status('smr', f'Waiting for {", ".join(waiting_for)} module(s) to complete Stage 1...')
        else:
            self.status_window.update_module_status('smr', 'Stage 1 complete')
        
        self._check_stage1_complete()
    
    def _stage1_fluidic(self):
        """Stage 1: Fluidic module tasks (sequential within module)."""
        self._safe_status_window_update(lambda: self.status_window.update_task_status('fluidic', 'Connect COM port', 'in_progress'))
        self._safe_status_window_update(lambda: self.status_window.update_module_status('fluidic', 'Connecting to COM port...'))
        
        if hasattr(self, 'syringe_widget'):
            if not (self.syringe_widget.comm_thread and self.syringe_widget.comm_thread.isRunning()):
                self.syringe_widget.toggle_connection()
            QTimer.singleShot(1000, self._stage1_fluidic_after_com_connect)
        else:
            QTimer.singleShot(100, self._stage1_fluidic_after_com_connect)
    
    def _stage1_fluidic_after_com_connect(self):
        """Continue Stage 1 fluidic after COM port connection."""
        self.status_window.update_task_status('fluidic', 'Connect COM port', 'success')
        self.status_window.update_task_status('fluidic', 'Initialize syringes', 'in_progress')
        self.status_window.update_module_status('fluidic', 'Initializing syringes...')
        
        # Initialize syringes
        if hasattr(self, 'syringe_widget'):
            self.syringe_widget.initialize_all_pumps()
            # Wait for initialization to complete (polling-based, takes a few seconds)
            QTimer.singleShot(5000, self._stage1_fluidic_complete)
        else:
            QTimer.singleShot(100, self._stage1_fluidic_complete)
    
    def _stage1_fluidic_complete(self):
        """Stage 1 fluidic module complete."""
        self.status_window.update_task_status('fluidic', 'Initialize syringes', 'success')
        self.stage_completion['fluidic'] = True
        
        # Check if other modules are still running
        if not self.stage_completion['imaging'] or not self.stage_completion['smr']:
            waiting_for = []
            if not self.stage_completion['imaging']:
                waiting_for.append('Imaging')
            if not self.stage_completion['smr']:
                waiting_for.append('SMR')
            self.status_window.update_module_status('fluidic', f'Waiting for {", ".join(waiting_for)} module(s) to complete Stage 1...')
        else:
            self.status_window.update_module_status('fluidic', 'Stage 1 complete')
        
        self._check_stage1_complete()
    
    def _check_stage1_complete(self):
        """Check if all modules have completed Stage 1."""
        if all(self.stage_completion.values()):
            # Update all modules to show completion
            self.status_window.update_module_status('imaging', 'Stage 1 complete')
            self.status_window.update_module_status('fluidic', 'Stage 1 complete')
            self.status_window.update_module_status('smr', 'Stage 1 complete')
            # All modules complete, move to Stage 2
            QTimer.singleShot(1000, self._stage2_prime_system)
    
    def _stage2_prime_system(self):
        """Stage 2: Prime System."""
        # Stage 2 only involves Fluidic module, but Imaging and SMR should show waiting status
        self._safe_status_window_update(lambda: self.status_window.update_module_status('imaging', 'Waiting for Fluidic module to finish Prime System...'))
        self._safe_status_window_update(lambda: self.status_window.update_module_status('smr', 'Waiting for Fluidic module to finish Prime System...'))
        
        self._safe_status_window_update(lambda: self.status_window.update_task_status('fluidic', 'Prime System', 'in_progress'))
        
        if hasattr(self, 'syringe_widget'):
            # Get the fluidic startup sequence type from automated setup settings
            sequence_type = getattr(self, 'automated_setup_fluidic_sequence', 'full_system_prime')
            # Run Prime System programmatically (skip confirmation for automated setup)
            success = self.syringe_widget.run_prime_system_programmatic(
                skip_confirmation=True,
                sequence_type=sequence_type
            )
            if success:
                # Set initial status message for first routine
                if sequence_type == "prime_reagents_only":
                    initial_message = 'Prime system step 1: Priming Reagents'
                else:
                    initial_message = 'Prime system step 1: Priming Reagents'
                self._safe_status_window_update(lambda: self.status_window.update_module_status('fluidic', initial_message))
                
                # Monitor for completion (Prime System runs routines sequentially)
                # Check every 2 seconds if sequence is complete
                self._check_prime_system_complete()
            else:
                self._safe_status_window_update(lambda: self.status_window.update_task_status('fluidic', 'Prime System', 'error'))
                self._safe_status_window_update(lambda: self.status_window.update_module_status('fluidic', 'Failed to start Prime System'))
                # Continue anyway
                QTimer.singleShot(2000, self._stage2_complete)
        else:
            QTimer.singleShot(100, self._stage2_complete)
    
    def _check_prime_system_complete(self):
        """Check if Prime System sequence is complete."""
        if hasattr(self, 'syringe_widget'):
            if self.syringe_widget.prime_system_sequence is None:
                # Sequence complete
                self._stage2_complete()
            else:
                # Update status message based on current routine
                if len(self.syringe_widget.prime_system_sequence) > 0:
                    current_routine = self.syringe_widget.prime_system_sequence[0]
                    # Map routine names to step messages
                    step_messages = {
                        'Prime Reagents': 'Prime system step 1: Priming Reagents',
                        'Complete Clean': 'Prime system step 2: Complete Clean',
                        'Media Purge': 'Prime system step 3: Reagent flush'
                    }
                    status_message = step_messages.get(current_routine, f'Running {current_routine}...')
                    self._safe_status_window_update(lambda: self.status_window.update_module_status('fluidic', status_message))
                
                # Still running, check again in 2 seconds
                QTimer.singleShot(2000, self._check_prime_system_complete)
        else:
            self._stage2_complete()
    
    def _stage2_complete(self):
        """Stage 2 complete, move to Stage 3."""
        self.status_window.update_task_status('fluidic', 'Prime System', 'success')
        # Update UI state for Prime System button
        if hasattr(self, 'syringe_widget'):
            self.syringe_widget.prime_system_completed = True
            self.syringe_widget._update_prime_system_button()
        
        self.status_window.update_module_status('fluidic', 'Stage 2 complete. Starting Stage 3...')
        QTimer.singleShot(1000, self._stage3_initialize_smr)
    
    def _stage3_initialize_smr(self):
        """Stage 3: Initialize SMR sweep."""
        # Stage 3 only involves SMR module, but Imaging and Fluidic should show waiting status
        self.status_window.update_module_status('imaging', 'Waiting for SMR module to finish Initialize SMR sweep...')
        self.status_window.update_module_status('fluidic', 'Waiting for SMR module to finish Initialize SMR sweep...')
        
        self.status_window.update_task_status('smr', 'Initialize SMR sweep', 'in_progress')
        self.status_window.update_module_status('smr', 'Running Initialize SMR sweep with narrow settings...')
        
        if hasattr(self, 'smr_widget'):
            # Get most recent sweep results to set narrow sweep range
            sweep_results = self.smr_widget._get_most_recent_sweep_results()
            
            # Create sweep control widget and configure for narrow sweep
            from helper_functions.SMR_sweep_frequencies import SMRSweepControlWidget
            if not hasattr(self.smr_widget, 'sweep_control_widget') or self.smr_widget.sweep_control_widget is None:
                self.smr_widget.sweep_control_widget = SMRSweepControlWidget(
                    tcp_socket=self.smr_widget.fpga_command_queue,
                    udp_socket=self.smr_widget.udp_data_manager,
                    parent=self.smr_widget,
                    pySMR_widget=self.smr_widget,
                    operator=self.operator
                )
            
            # Set narrow sweep settings if previous sweep exists
            if sweep_results is not None:
                recent_freq, recent_q, recent_bias = sweep_results
                min_freq = max(0.0, recent_freq - 50000.0)
                max_freq = min(2e7, recent_freq + 50000.0)
                self.smr_widget.sweep_control_widget.min_freq_spin.setValue(min_freq)
                self.smr_widget.sweep_control_widget.max_freq_spin.setValue(max_freq)
                if hasattr(self.smr_widget.sweep_control_widget, 'substrate_bias_spin'):
                    self.smr_widget.sweep_control_widget.substrate_bias_spin.setValue(recent_bias)
            
            # Mark that we're in automated setup mode
            self.smr_widget.sweep_control_widget._automated_setup_mode = True
            self.smr_widget.sweep_control_widget._automated_setup_main_window = self
            
            # Start the sweep
            self.smr_widget.sweep_control_widget._start_sweep()
            

            
            # Monitor for sweep completion
            self._check_sweep_complete()
        else:
            QTimer.singleShot(100, self._stage3_complete)
    
    def _check_sweep_complete(self):
        """Check if Initialize SMR sweep is complete."""
        # Use a timeout-based approach since sweeps take variable time
        # The sweep will save results when complete, and Set Delays will use the most recent
        # During automated setup, the sweep will automatically proceed to Set Delays when complete
        if not hasattr(self, '_sweep_check_count'):
            self._sweep_check_count = 0
        
        self._sweep_check_count += 1
        
        # Check if sweep window exists and is still running
        sweep_running = False
        sweep_window_closed = False
        if hasattr(self, 'smr_widget') and hasattr(self.smr_widget, 'sweep_control_widget'):
            if self.smr_widget.sweep_control_widget is not None:
                if hasattr(self.smr_widget.sweep_control_widget, 'sweep_window'):
                    sweep_window = self.smr_widget.sweep_control_widget.sweep_window
                    if sweep_window is not None:
                        if sweep_window.isVisible():
                            sweep_running = True
                        else:
                            # Window closed - sweep likely completed and proceeded to set delays
                            sweep_window_closed = True
        
        # If sweep window closed during automated setup, it means we proceeded to Set Delays
        # Don't call _stage3_complete here - it will be called by the sweep window when Set Delays starts
        if sweep_window_closed and hasattr(self, 'smr_widget') and hasattr(self.smr_widget, 'sweep_control_widget'):
            if hasattr(self.smr_widget.sweep_control_widget, '_automated_setup_mode') and self.smr_widget.sweep_control_widget._automated_setup_mode:
                # Sweep completed and automatically proceeded to Set Delays
                # The sweep window will notify us when Set Delays starts
                return
        
        # If sweep is not running or we've checked enough times (90 seconds = ~45 checks at 2s interval)
        if not sweep_running or self._sweep_check_count >= 45:
            self._stage3_complete()
        else:
            # Check again in 2 seconds
            QTimer.singleShot(2000, self._check_sweep_complete)
    
    def _stage3_complete(self):
        """Stage 3 complete, move to Stage 4."""
        self.status_window.update_task_status('smr', 'Initialize SMR sweep', 'success')
        # Update UI state
        if hasattr(self, 'smr_widget'):
            self.smr_widget.smr_initialized = True
            self.smr_widget._update_initialize_smr_button()
        
        # Check if Set Delays was already started automatically
        set_delays_already_started = False
        if (hasattr(self, 'smr_widget') and 
            hasattr(self.smr_widget, 'set_delays_window') and
            self.smr_widget.set_delays_window is not None):
            # Check if set delays window exists and is visible
            if self.smr_widget.set_delays_window.isVisible():
                set_delays_already_started = True
        
        if set_delays_already_started:
            # Set Delays was automatically started, so we're already in Stage 4
            # Still need to turn on blue LED (imaging part of Stage 4)
            # Reset completion tracking for Stage 4
            self.stage_completion = {
                'imaging': False,
                'smr': False
            }
            # Turn on blue LED
            self._stage4_imaging()
            # Update status
            self.status_window.update_module_status('smr', 'Stage 3 complete. Set Delays already started automatically.')
            # Update task status to show it's in progress (blue arrow)
            self.status_window.update_task_status('smr', 'Set Delays', 'in_progress')
        else:
            # Normal flow - proceed to Stage 4
            self.status_window.update_module_status('smr', 'Stage 3 complete. Starting Stage 4...')
            QTimer.singleShot(1000, self._stage4_set_delays)
    
    def _stage4_set_delays(self):
        """Stage 4: Set Delays - imaging and SMR can run in parallel."""
        # Reset completion tracking
        self.stage_completion = {
            'imaging': False,
            'smr': False
        }
        
        # Start both modules in parallel
        self._stage4_imaging()
        self._stage4_smr()
    
    def _stage4_imaging(self):
        """Stage 4: Imaging module - Turn on blue LED."""
        self.status_window.update_task_status('imaging', 'Turn on blue LED', 'in_progress')
        self.status_window.update_module_status('imaging', 'Turning on blue LED...')
        
        if hasattr(self, 'image_widget'):
            self.image_widget.turn_on_blue_led_programmatic()
        
        QTimer.singleShot(500, self._stage4_imaging_complete)
    
    def _stage4_imaging_complete(self):
        """Stage 4 imaging module complete."""
        self.status_window.update_task_status('imaging', 'Turn on blue LED', 'success')
        self.stage_completion['imaging'] = True
        
        # Check if SMR module is still running
        if not self.stage_completion['smr']:
            self.status_window.update_module_status('imaging', 'Waiting for SMR module to finish Set Delays...')
        else:
            self.status_window.update_module_status('imaging', 'Stage 4 complete')
        
        self._check_stage4_complete()
    
    def _stage4_smr(self):
        """Stage 4: SMR module - Set Delays."""
        self.status_window.update_task_status('smr', 'Set Delays', 'in_progress')
        self.status_window.update_module_status('smr', 'Running Set Delays routine...')
        
        # pySMR: Run Set Delays with most recent sweep
        if hasattr(self, 'smr_widget'):
            from helper_functions.SMR_settings_io import read_smr_settings
            
            settings_list = read_smr_settings()
            if settings_list:
                # Filter for sweep settings and get most recent
                sweep_settings = [s for s in settings_list 
                                if s.get("settings_type", "").strip().lower() == "sweep"]
                if sweep_settings:
                    # Sort by date and time
                    def get_datetime_key(settings):
                        date_str = settings.get("date", "0000-00-00")
                        time_str = settings.get("time", "00:00:00")
                        return (date_str, time_str)
                    sweep_settings.sort(key=get_datetime_key, reverse=True)
                    most_recent = sweep_settings[0]
                    
                    # Store reference to status window and main window so set delays window can notify us
                    if hasattr(self, 'status_window'):
                        self.smr_widget._automated_setup_status_window = self.status_window
                    self.smr_widget._automated_setup_main_window = self
                    self.smr_widget._automated_setup_mode = True
                    
                    # Run set delays
                    success = self.smr_widget.run_set_delays_with_settings(
                        selected_settings=most_recent,
                        set_bias=True
                    )
                    if success:
                        # Update UI state
                        self.smr_widget.set_delays_run = True
                        self.smr_widget._update_set_delays_button()
                        

                        
                        # Pass automated setup mode to Set Delays window (backup - should already be set in run_set_delays_with_settings)
                        # Use a small delay to ensure window is fully created
                        def set_automated_flag():
                            if hasattr(self.smr_widget, 'set_delays_window') and self.smr_widget.set_delays_window is not None:
                                self.smr_widget.set_delays_window._automated_setup_mode = True
                                self.smr_widget.set_delays_window._automated_setup_main_window = self
                        
                        QTimer.singleShot(200, set_automated_flag)
                        # Also set immediately if window already exists
                        if hasattr(self.smr_widget, 'set_delays_window') and self.smr_widget.set_delays_window is not None:
                            self.smr_widget.set_delays_window._automated_setup_mode = True
                            self.smr_widget.set_delays_window._automated_setup_main_window = self
                        
                        # Monitor for Set Delays completion instead of fixed timeout
                        self._check_set_delays_complete()
                    else:
                        self.status_window.update_task_status('smr', 'Set Delays', 'error')
                        self.status_window.update_module_status('smr', 'Failed to start Set Delays')
                        QTimer.singleShot(2000, self._stage4_smr_complete)
                else:
                    self.status_window.update_task_status('smr', 'Set Delays', 'error')
                    self.status_window.update_module_status('smr', 'No sweep settings found')
                    QTimer.singleShot(2000, self._stage4_smr_complete)
            else:
                self.status_window.update_task_status('smr', 'Set Delays', 'error')
                self.status_window.update_module_status('smr', 'No settings found')
                QTimer.singleShot(2000, self._stage4_smr_complete)
        else:
            QTimer.singleShot(100, self._stage4_smr_complete)
    
    def _check_set_delays_complete(self):
        """Check if Set Delays is complete."""
        # If already complete, stop checking
        if self.stage_completion.get('smr', False):
            return
            
        if hasattr(self, 'smr_widget') and hasattr(self.smr_widget, 'set_delays_window'):
            set_delays_window = self.smr_widget.set_delays_window
            if set_delays_window is not None:
                # Check if window is still visible (if closed, Set Delays is complete)
                if not set_delays_window.isVisible():
                    # Set Delays window closed - complete
                    self._stage4_smr_complete()
                    return
                # Check if Set Delays has finished (by checking if it's in a finished state)
                # We'll check again in 2 seconds
                QTimer.singleShot(2000, self._check_set_delays_complete)
            else:
                # Window doesn't exist, assume complete
                self._stage4_smr_complete()
        else:
            # No Set Delays window, assume complete
            self._stage4_smr_complete()
    
    def _stage4_smr_complete(self):
        """Stage 4 SMR module complete."""
        # Update status first, even if already marked complete (ensures UI is updated)
        was_already_complete = self.stage_completion.get('smr', False)
        
        if not was_already_complete:
            self.stage_completion['smr'] = True
        
        # Always update the task status to success (in case it was missed on first call)
        self._safe_status_window_update(lambda: self.status_window.update_task_status('smr', 'Set Delays', 'success'))
        
        # Check if Imaging module is still running
        if not self.stage_completion.get('imaging', False):
            self._safe_status_window_update(lambda: self.status_window.update_module_status('smr', 'Waiting for Imaging module to finish...'))
        else:
            self._safe_status_window_update(lambda: self.status_window.update_module_status('smr', 'Stage 4 complete'))
        
        # Check if we can advance (even if already complete, in case imaging completed after SMR)
        self._check_stage4_complete()
    
    def _check_stage4_complete(self):
        """Check if all modules have completed Stage 4."""
        if all(self.stage_completion.values()):
            # Update all modules to show completion
            self.status_window.update_module_status('imaging', 'Stage 4 complete')
            self.status_window.update_module_status('smr', 'Stage 4 complete')
            # All modules complete, move to Stage 5
            QTimer.singleShot(1000, self._stage5_monitor_noise)
    
    def _stage5_monitor_noise(self):
        """Stage 5: Monitor noise and display summary."""
        # Stage 5 only involves SMR module, but Imaging and Fluidic should show waiting status
        self.status_window.update_module_status('imaging', 'Waiting for SMR module to finish noise monitoring...')
        self.status_window.update_module_status('fluidic', 'Waiting for SMR module to finish noise monitoring...')
        
        self.status_window.update_task_status('smr', 'Monitor noise', 'in_progress')
        self.status_window.update_module_status('smr', 'Monitoring noise...')
        
        # Collect noise measurements over a period
        self._noise_samples = []
        self._noise_sample_count = 0
        self._noise_max_samples = 10  # Collect 10 samples
        
        QTimer.singleShot(2000, self._collect_noise_sample)
    
    def _collect_noise_sample(self):
        """Collect a noise sample."""
        if hasattr(self, 'smr_widget'):
            noise_mhz = self.smr_widget.get_current_noise_mhz()
            if noise_mhz is not None:
                self._noise_samples.append(noise_mhz)
        
        self._noise_sample_count += 1
        
        if self._noise_sample_count < self._noise_max_samples:
            # Collect more samples (every 1 second)
            QTimer.singleShot(1000, self._collect_noise_sample)
        else:
            # Calculate average noise
            self._calculate_noise_summary()
    
    def _calculate_noise_summary(self):
        """Calculate noise summary and display message."""
        if not hasattr(self, '_noise_samples') or len(self._noise_samples) == 0:
            # No valid samples
            message = "SMR could not be automatically set up, please continue with manual set up using the 'Initialize SMR' button."
            self._safe_status_window_update(lambda: self.status_window.update_task_status('smr', 'Monitor noise', 'error'))
        else:
            # Calculate average noise
            self._final_avg_noise_mhz = sum(self._noise_samples) / len(self._noise_samples)
            
            # Update status window temporarily
            message = f"Noise monitoring complete. Average: {self._final_avg_noise_mhz:.1f} mHz"
            self._safe_status_window_update(lambda: self.status_window.update_task_status('smr', 'Monitor noise', 'success'))
        
        self._safe_status_window_update(lambda: self.status_window.update_module_status('smr', message))
        
        # Mark automated setup as complete
        QTimer.singleShot(2000, self._automated_setup_complete)
    
    def _automated_setup_complete(self):
        """Automated setup complete."""
        # Guard against duplicate calls - check if we've already shown the result window
        if hasattr(self, '_automated_setup_result_shown') and self._automated_setup_result_shown:
            return
        
        # Mark that we're showing the result window
        self._automated_setup_result_shown = True
        
        # Close status window
        if hasattr(self, 'status_window') and self.status_window:
            self.status_window.close()
            self.status_window = None
            
        # Show result window
        if hasattr(self, '_final_avg_noise_mhz'):
             noise = self._final_avg_noise_mhz
        else:
             noise = 0.0
             
        result_window = AutomatedSetupResultWindow(noise, parent=self)
        result_window.exec()
        



def main():
    """Main entry point for the application."""
    # Initialize console redirection
    global console_redirector
    console_redirector = ConsoleLogRedirector(sys.stdout, sys.stderr)
    sys.stdout = console_redirector
    sys.stderr = console_redirector
    
    from PySide6.QtCore import QLockFile
    import tempfile
    
    # Single instance lock using a temporary file
    lock_path = os.path.join(tempfile.gettempdir(), "paella_app.lock")
    lock_file = QLockFile(lock_path)
    
    # Try to acquire the lock for 100ms
    if not lock_file.tryLock(100):
        # Already running - simple check for GUI app
        from PySide6.QtWidgets import QMessageBox
        temp_app = QApplication(sys.argv)
        QMessageBox.critical(
            None, 
            "Application Already Running",
            "An instance of the Paella GUI is already running. \n\n"
            "To prevent hardware conflicts (Serial/FPGA), please close the other instance before starting a new one."
        )
        sys.exit(1)
        
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(APP_ICON_PATH))
    
    # Show startup dialog
    dialog = StartupDialog()
    result = dialog.exec()
    
    # If dialog was rejected (closed with 'x' button), exit immediately
    if result != QDialog.DialogCode.Accepted:
        sys.exit(0)
    
    mode = dialog.get_mode()
    operator = dialog.get_operator()
    camera_mode = dialog.get_camera_mode()
    fluidic_startup_sequence = dialog.get_fluidic_startup_sequence()
    
    # Create main window
    window = MainApplicationWindow(operator=operator)
    
    # Set initial camera mode based on startup selection
    window.image_widget.set_camera_mode_programmatic(camera_mode)
    window.syringe_widget.set_camera_mode(camera_mode)
    
    # Set initial GUI mode based on startup selection
    # Automated Setup -> Basic mode (default)
    # Manual Control -> Advanced mode
    if mode == "manual":
        window.smr_widget.set_gui_mode("advanced")
        window.image_widget.set_gui_mode("advanced")
        window.syringe_widget.set_gui_mode("advanced")
    else:
        # Explicitly set basic for automated (though it's the default)
        window.smr_widget.set_gui_mode("basic")
        window.image_widget.set_gui_mode("basic")
        window.syringe_widget.set_gui_mode("basic")
        
    window.show()
    # Use a timer to maximize window after it's shown - fixes issue on Windows where
    # immediate maximize only maximizes height but not width
    QTimer.singleShot(100, window.showMaximized)
    window.raise_()
    window.activateWindow()
    
    # If automated setup was selected, perform the sequence
    if mode == "automated":
        window.perform_automated_setup(camera_mode=camera_mode, fluidic_startup_sequence=fluidic_startup_sequence)
    
    sys.exit(app.exec())


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()

