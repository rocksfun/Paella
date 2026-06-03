"""
SMR (Suspended Microchannel Resonator) Control Module.

This module provides a widget for controlling SMR operations.
It can be used standalone or embedded in other applications.
"""

import sys
import os
import traceback
import glob
import numpy as np
import threading
import queue
import time as time_module
import time
import struct
import polars as pl
from collections import deque
from datetime import datetime
from typing import Optional, List
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QSpinBox, QFormLayout, QDialog,
    QCheckBox, QDoubleSpinBox, QLineEdit, QGroupBox, QGridLayout,
    QDialogButtonBox, QMessageBox, QTabWidget, QSplitter, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt, QTimer, QObject, Signal, QThread, Slot, QMetaObject
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False
try:
    import nidaqmx
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False
from helper_functions.FPGA_tcp_manager import FPGACommandQueue
from helper_functions.UDP_data_manager import UDPDataManager, UDPPacket
from helper_functions.UDP_receive_data import parse_udp_data
from helper_functions.frequency_plot import (
    plot_data_preparation_loop, update_extended_freq_bounds, recalculate_extended_bounds,
    update_plot_widget, create_plot_column_widget
)
from helper_functions.diagnostic_plot import DiagnosticPlotWindow
from helper_functions.FPGA_UserParametersToRegisterValues import FPGAParameterWidget, calculate_register_values
from helper_functions.SYSTEM_pull_config_io import (
    load_system_config,
    get_system_name,
    get_operators,
    parse_toml_config,
    get_daq_info,
)
from helper_functions.SMR_sweep_frequencies import SweepWindow, SMRSweepControlWidget
import helper_functions.SMR_sweep_frequencies as smr_sweep_module
from helper_functions.FPGA_relay_reset import reset_fpga_relay
from helper_functions.SMR_settings_io import (
    write_smr_settings,
    read_smr_settings,
    LoadSettingsDialog,
)
from helper_functions.SMR_set_delays import SetDelaysWindow, SetDelaysOptionsDialog
from helper_functions.DATA_realtime_frequency_analysis import PeakDetectionSettings
from helper_functions.META_sample_selection import select_and_copy_sample, _get_local_data_path, _get_nas_sample_path
from helper_functions.META_create_sample import CreateSampleDialog, EditConditionsDialog
from helper_functions.DATA_save_udp import DataSaver
from helper_functions.DATA_posthoc_frequency_analysis import process_experiment, PostHocFrequencyAnalyzer
from helper_functions.UIUX_elements import (
    create_button, get_button_stylesheet, create_status_label, create_status_badge,
    create_connection_indicator, update_connection_indicator,
    create_text_indicator, create_increment_button,
    style_input_field, style_checkbox, Colors
)

# References directory relative to script location
if hasattr(sys, '_MEIPASS'):
    # When running as a bundled executable
    _SCRIPT_DIR = sys._MEIPASS
else:
    # When running from source
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

REFERENCES_DIR = os.path.join(_SCRIPT_DIR, 'references')


class ArrowKeyLineEdit(QLineEdit):
    """QLineEdit that handles arrow key presses for increment/decrement."""
    
    def __init__(self, increment_callback, decrement_callback, parent=None):
        super().__init__(parent)
        self.increment_callback = increment_callback
        self.decrement_callback = decrement_callback
    
    def keyPressEvent(self, event):
        """Handle arrow key presses."""
        if event.key() == Qt.Key.Key_Up:
            if self.increment_callback:
                self.increment_callback()
            event.accept()
        elif event.key() == Qt.Key.Key_Down:
            if self.decrement_callback:
                self.decrement_callback()
            event.accept()
        else:
            super().keyPressEvent(event)


class HHMMTimeAxisItem(pg.AxisItem):
    """AxisItem that formats seconds since epoch as HH:MM time strings."""
    def tickStrings(self, values, scale, spacing):
        """Convert timestamps to HH:MM format."""
        return [datetime.fromtimestamp(value).strftime('%H:%M') for value in values]


class PacketData:
    """Stores packet data with on-demand frequency conversion.""" 
    
    def __init__(self, raw_bytes: bytes, timestamp: float, packet_number: int, frequencies: Optional[List[float]] = None):
        """
        Initialize packet data.
        
        Args:
            raw_bytes: Complete UDP packet bytes (including packet number)
            timestamp: Reception timestamp (kernel timestamp or time.perf_counter() value)
            packet_number: Packet number (first I32 value // 256)
            frequencies: Pre-parsed frequency values (optional, to avoid redundant parsing)
        """
        self.raw_bytes = raw_bytes
        self.timestamp = timestamp
        self.packet_number = packet_number
        self._frequencies = frequencies  # Use pre-parsed frequencies if provided
        self._raw_i32_data = None  # Cached I32 data (without packet number)
    
    @property
    def raw_i32_data(self):
        """Get I32 data without packet number (for file writing)."""
        if self._raw_i32_data is None:
            if len(self.raw_bytes) < 8:  # Need at least 2 I32 values
                self._raw_i32_data = []
            else:
                # Unpack all I32 values, skip first (packet number)
                count = len(self.raw_bytes) // 4
                if count > 1:
                    i32_array = struct.unpack(f'<{count}i', self.raw_bytes[:count*4])
                    self._raw_i32_data = list(i32_array[1:])  # Skip first entry
                else:
                    self._raw_i32_data = []
        return self._raw_i32_data
    
    @property
    def frequencies(self):
        """Get frequency values (pre-converted when packet was created)."""
        # Frequencies are now pre-converted in receive thread, so this is just a getter
        if self._frequencies is None:
            # Fallback: if somehow frequencies weren't pre-converted, convert now
            self._frequencies = parse_udp_data(self.raw_bytes)
            if self._frequencies is None:
                self._frequencies = []
        return self._frequencies
    
    def convert_frequencies(self):
        """
        Convert frequencies from raw bytes and cache the result.
        Called immediately after packet creation in receive thread if frequencies weren't pre-parsed.
        Note: If frequencies were provided in __init__, this is a no-op.
        """
        if self._frequencies is None:
            self._frequencies = parse_udp_data(self.raw_bytes)
            if self._frequencies is None:
                self._frequencies = []


class FPGAConnectionDialog(QDialog):
    """Dialog window for FPGA connection status."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_widget = parent
        self.setWindowTitle("Connecting to FPGA")
        self.setMinimumSize(400, 200)
        self.setModal(True)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)
        
        # Status label - show connection attempt message immediately
        self.status_label = create_status_label("Attempting to connect to FPGA...", "info", font_size="14pt")
        self.status_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.status_label)
        
        # Error label (initially hidden)
        self.error_label = create_status_label("", "error", font_size="12pt")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        main_layout.addWidget(self.error_label)
        
        # Button layout
        button_layout = QHBoxLayout()
        
        # Reset FPGA button (initially hidden, shown on connection failure)
        self.reset_fpga_button = create_button("Reset FPGA", "error", font_size="11pt", padding="8px 16px", min_width="100px")
        self.reset_fpga_button.clicked.connect(self._on_reset_fpga_clicked)
        self.reset_fpga_button.hide()
        button_layout.addWidget(self.reset_fpga_button)
        
        button_layout.addStretch()
        
        # Close button
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        button_layout.addWidget(button_box)
        
        main_layout.addLayout(button_layout)
        
        # Timer for timeout (no countdown display)
        self.timeout_timer = QTimer()
        self.timeout_timer.setSingleShot(True)
        self.timeout_timer.timeout.connect(self._on_timeout)
        self.timeout_seconds = 6  # 6 seconds to be safe (connection timeout is 5 seconds)
        
        # Timer to check connection status
        self.status_check_timer = QTimer()
        self.status_check_timer.timeout.connect(self._check_connection_status)
        self.status_check_timer.start(100)  # Check every 100ms
        
        # Start timeout timer
        self.timeout_timer.start(self.timeout_seconds * 1000)
    
    def _check_connection_status(self):
        """Check if connection is successful."""
        if self.parent_widget is None:
            return
        
        # Sync TCP status with actual connection state (in case it was established elsewhere)
        self.parent_widget._sync_tcp_connection_status()
        
        # Check if both TCP and UDP are connected
        # Primary check: use is_connected() for reliability
        # Secondary check: status string (may lag slightly, so don't require it)
        tcp_connected = self.parent_widget.fpga_command_queue.is_connected()
        udp_connected = self.parent_widget.udp_data_manager.is_connected()
        
        # If TCP is connected, sync the status indicator
        if tcp_connected and self.parent_widget.tcp_connection_status != "connected":
            self.parent_widget.tcp_connection_status = "connected"
            self.parent_widget._update_connection_indicators()
        
        if tcp_connected and udp_connected:
            # Connection successful - close dialog
            self.status_check_timer.stop()
            self.timeout_timer.stop()
            self.accept()
    
    def _on_timeout(self):
        """Handle timeout - show error message with reset instructions."""
        self.status_label.setText("Connection Failed")
        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
        self.error_label.setText(
            "Could not connect to the FPGA.\n\n"
            "Please reset the FPGA and try again."
        )
        self.error_label.show()
        self.status_check_timer.stop()
        # Show Reset FPGA button
        self.reset_fpga_button.show()
    
    def _on_reset_fpga_clicked(self):
        """Handle Reset FPGA button click - close dialog and trigger reset."""
        # Close this dialog
        self.accept()
        
        # Trigger the reset FPGA functionality on the parent widget
        # Skip confirmation since user already confirmed by clicking Reset FPGA from connection failure popup
        if self.parent_widget is not None:
            self.parent_widget.on_reset_fpga_clicked(skip_confirmation=True)
    
    def closeEvent(self, event):
        """Clean up timers when dialog is closed."""
        if hasattr(self, 'timeout_timer'):
            self.timeout_timer.stop()
        if hasattr(self, 'status_check_timer'):
            self.status_check_timer.stop()
        event.accept()


class FPGAResetDialog(QDialog):
    """Dialog window for FPGA relay reset status."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_widget = parent
        self.setWindowTitle("Resetting FPGA Relay")
        self.setMinimumSize(500, 250)
        self.setModal(True)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)
        
        # Status label
        self.status_label = QLabel("Initializing reset sequence...")
        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold;")
        self.status_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.status_label)
        
        # Progress label
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("font-size: 12pt; color: #666666;")
        self.progress_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.progress_label)
        
        # Countdown label (for 10 second wait)
        self.countdown_label = create_status_label("", "info", font_size="12pt")
        self.countdown_label.setAlignment(Qt.AlignCenter)
        self.countdown_label.hide()
        main_layout.addWidget(self.countdown_label)
        
        # Connection status label (for final connection results)
        self.connection_status_label = QLabel("")
        self.connection_status_label.setStyleSheet("font-size: 12pt;")
        self.connection_status_label.setAlignment(Qt.AlignCenter)
        self.connection_status_label.setWordWrap(True)
        self.connection_status_label.hide()
        main_layout.addWidget(self.connection_status_label)
        
        # Error label (initially hidden)
        self.error_label = create_status_label("", "error", font_size="12pt")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        main_layout.addWidget(self.error_label)
        
        # Status update timer
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(100)  # Check every 100ms
        
        # Countdown timer
        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.countdown_seconds = 10
        
        # Thread for running reset
        self.reset_thread = None
        self.reset_success = False
        self.reset_complete = False
        self.status_queue = queue.Queue()
        self.start_time = time_module.time()
        self.current_cycle = 0
        
        # Connection attempt state
        self.countdown_active = False
        self.connection_attempted = False
        
        # Start reset in background thread
        self._start_reset()
    
    def _start_reset(self):
        """Start the reset process in a background thread."""
        def reset_worker():
            """Worker function that runs in thread."""
            try:
                self.reset_success = reset_fpga_relay()
                self.status_queue.put(('complete', self.reset_success))
            except Exception as e:
                self.status_queue.put(('error', str(e)))
                self.status_queue.put(('complete', False))
        
        self.reset_thread = threading.Thread(target=reset_worker, daemon=True)
        self.reset_thread.start()
    
    def _update_status(self):
        """Update status from queue and show progress."""
        # Check if thread is still running
        if self.reset_thread and self.reset_thread.is_alive():
            # Show progress based on elapsed time
            elapsed = time_module.time() - self.start_time
            # Each cycle takes 0.5 seconds (400ms HIGH + 100ms LOW)
            # Total time: 5 cycles * 0.5s = 2.5 seconds
            estimated_cycle = min(int(elapsed / 0.5) + 1, 5)
            
            if estimated_cycle != self.current_cycle:
                self.current_cycle = estimated_cycle
                if self.current_cycle <= 5:
                    self.progress_label.setText(f"Cycle {self.current_cycle}/5 in progress...")
        
        # Check for messages from thread
        try:
            while True:
                msg_type, message = self.status_queue.get_nowait()
                
                if msg_type == 'error':
                    # Show error
                    self.error_label.setText(f"Error: {message}")
                    self.error_label.show()
                    self.status_label.setText("Reset Failed")
                    self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
                    # Stop timer and auto-close after short delay
                    self.status_timer.stop()
                    QTimer.singleShot(2000, self.accept)  # Close after 2 seconds
                elif msg_type == 'complete':
                    # Reset complete
                    self.reset_complete = True
                    self.reset_success = message
                    
                    if message:
                        self.status_label.setText("Reset Complete")
                        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: green;")
                        self.progress_label.setText("FPGA relay reset sequence completed successfully.")
                        
                        # Start countdown before attempting reconnection
                        self._start_countdown()
                    else:
                        self.status_label.setText("Reset Failed")
                        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
                        # Stop timer and auto-close after short delay
                        self.status_timer.stop()
                        QTimer.singleShot(2000, self.accept)  # Close after 2 seconds
                    break
        except queue.Empty:
            pass
    
    def _start_countdown(self):
        """Start 10 second countdown before attempting reconnection."""
        self.countdown_active = True
        self.countdown_seconds = 10
        self.countdown_label.show()
        self.countdown_label.setText(f"FPGA booting: {self.countdown_seconds} seconds remaining")
        self.countdown_timer.start(1000)  # Update every 1 second
    
    def _update_countdown(self):
        """Update countdown timer."""
        if not self.countdown_active:
            return
        
        self.countdown_seconds -= 1
        
        if self.countdown_seconds > 0:
            self.countdown_label.setText(f"FPGA booting: {self.countdown_seconds} seconds remaining")
        else:
            # Countdown complete, attempt reconnection
            self.countdown_timer.stop()
            self.countdown_active = False
            self.countdown_label.hide()
            self._attempt_reconnection()
    
    def _attempt_reconnection(self):
        """Attempt to reconnect TCP and UDP connections."""
        if self.connection_attempted:
            return
        
        self.connection_attempted = True
        self.status_label.setText("Reconnecting to FPGA...")
        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: #2196F3;")
        self.progress_label.setText("Attempting TCP and UDP connections...")
        
        if not self.parent_widget:
            self.connection_status_label.setText("Error: Parent widget not available")
            self.connection_status_label.setStyleSheet("font-size: 12pt; color: red;")
            self.connection_status_label.show()
            QTimer.singleShot(3000, self.accept)  # Close after 3 seconds
            return
        
        # Use a queue to communicate results from thread to main thread
        connection_queue = queue.Queue()
        
        # Attempt connections in a thread to avoid blocking
        import threading
        def connect_thread():
            try:
                # Attempt TCP connection
                self.parent_widget.on_tcp_connect_clicked()
                import time
                time.sleep(0.5)  # Brief wait for TCP connection
                
                # Attempt UDP connection
                self.parent_widget.on_udp_connect_clicked()
                time.sleep(0.5)  # Brief wait for UDP connection
                
                # Check connection status
                tcp_connected = self.parent_widget.fpga_command_queue.is_connected()
                udp_connected = self.parent_widget.udp_data_manager.is_connected()
                
                # Put result in queue for main thread to process
                connection_queue.put(('success', tcp_connected, udp_connected))
            except Exception as e:
                # Put error in queue
                connection_queue.put(('error', str(e)))
        
        thread = threading.Thread(target=connect_thread, daemon=True)
        thread.start()
        
        # Start a timer to check for results from the thread
        self.connection_check_timer = QTimer()
        self.connection_check_timer.timeout.connect(
            lambda: self._check_connection_result(connection_queue, thread)
        )
        self.connection_check_timer.start(100)  # Check every 100ms
    
    def _check_connection_result(self, connection_queue: queue.Queue, thread: threading.Thread):
        """Check for connection results from the background thread."""
        try:
            # Check if thread is still running
            if thread.is_alive():
                # Thread still running, check queue for results
                try:
                    result = connection_queue.get_nowait()
                    # Stop checking timer
                    self.connection_check_timer.stop()
                    
                    if result[0] == 'success':
                        _, tcp_connected, udp_connected = result
                        self._update_connection_status(tcp_connected, udp_connected)
                    else:
                        # Error occurred
                        _, error_msg = result
                        self.status_label.setText("Reconnection Failed")
                        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
                        self.progress_label.setText(f"Error: {error_msg}")
                        self.connection_status_label.setText("Error during reconnection")
                        self.connection_status_label.setStyleSheet("font-size: 12pt; color: red;")
                        self.connection_status_label.show()
                        QTimer.singleShot(3000, self.accept)  # Close after 3 seconds
                except queue.Empty:
                    # No result yet, continue checking
                    pass
            else:
                # Thread finished, check for any remaining results
                try:
                    result = connection_queue.get_nowait()
                    # Stop checking timer
                    self.connection_check_timer.stop()
                    
                    if result[0] == 'success':
                        _, tcp_connected, udp_connected = result
                        self._update_connection_status(tcp_connected, udp_connected)
                    else:
                        # Error occurred
                        _, error_msg = result
                        self.status_label.setText("Reconnection Failed")
                        self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
                        self.progress_label.setText(f"Error: {error_msg}")
                        self.connection_status_label.setText("Error during reconnection")
                        self.connection_status_label.setStyleSheet("font-size: 12pt; color: red;")
                        self.connection_status_label.show()
                        QTimer.singleShot(3000, self.accept)  # Close after 3 seconds
                except queue.Empty:
                    # Thread finished but no result - timeout or error
                    self.connection_check_timer.stop()
                    self.status_label.setText("Reconnection Timeout")
                    self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: orange;")
                    self.progress_label.setText("Connection attempt timed out. Please try manually.")
                    self.connection_status_label.setText("Timeout: Connection attempt took too long")
                    self.connection_status_label.setStyleSheet("font-size: 12pt; color: orange;")
                    self.connection_status_label.show()
                    QTimer.singleShot(3000, self.accept)  # Close after 3 seconds
        except Exception as e:
            # Unexpected error
            self.connection_check_timer.stop()
            self.status_label.setText("Reconnection Error")
            self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
            self.progress_label.setText(f"Unexpected error: {str(e)}")
            self.connection_status_label.setText("Error during reconnection")
            self.connection_status_label.setStyleSheet("font-size: 12pt; color: red;")
            self.connection_status_label.show()
            QTimer.singleShot(3000, self.accept)  # Close after 3 seconds
    
    def _update_connection_status(self, tcp_connected: bool, udp_connected: bool):
        """Update connection status display."""
        self.status_label.setText("Reconnection Complete")
        
        # Build status message
        status_parts = []
        if tcp_connected:
            status_parts.append("TCP: ✓ Connected")
        else:
            status_parts.append("TCP: ✗ Failed")
        
        if udp_connected:
            status_parts.append("UDP: ✓ Connected")
        else:
            status_parts.append("UDP: ✗ Failed")
        
        status_text = "\n".join(status_parts)
        self.connection_status_label.setText(status_text)
        self.connection_status_label.show()
        
        # Set color based on success
        if tcp_connected and udp_connected:
            self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: green;")
            self.connection_status_label.setStyleSheet("font-size: 12pt; color: green;")
            self.progress_label.setText("Both TCP and UDP connections reestablished successfully.")
        elif tcp_connected or udp_connected:
            self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: orange;")
            self.connection_status_label.setStyleSheet("font-size: 12pt; color: orange;")
            self.progress_label.setText("Partial reconnection: Some connections failed.")
        else:
            self.status_label.setStyleSheet("font-size: 14pt; font-weight: bold; color: red;")
            self.connection_status_label.setStyleSheet("font-size: 12pt; color: red;")
            self.progress_label.setText("Reconnection failed: Both TCP and UDP connections failed.")
        
        # Update parent widget's connection status and indicators
        if self.parent_widget:
            # Update TCP connection status
            if tcp_connected:
                self.parent_widget.tcp_connection_status = "connected"
            else:
                self.parent_widget.tcp_connection_status = "failed"
            
            # Update UDP connection status
            if udp_connected:
                self.parent_widget.udp_connection_status = "connected"
            else:
                self.parent_widget.udp_connection_status = "failed"
            
            # Sync TCP connection status (updates indicators)
            self.parent_widget._sync_tcp_connection_status()
            
            # Update connection indicators to reflect new status
            self.parent_widget._update_connection_indicators()
            
            # Update button states
            self.parent_widget._update_push_settings_button_state()
            
            # Start UDP data reception if UDP is connected and not already receiving
            if udp_connected:
                if (self.parent_widget.udp_data_manager.is_connected() and 
                    not self.parent_widget.receiving_udp_data):
                    self.parent_widget.start_udp_data_reception()
        
        # Auto-close after 3 seconds
        QTimer.singleShot(3000, self.accept)
    
    def closeEvent(self, event):
        """Clean up timer when dialog is closed."""
        if hasattr(self, 'status_timer'):
            self.status_timer.stop()
        if hasattr(self, 'countdown_timer'):
            self.countdown_timer.stop()
        if hasattr(self, 'connection_check_timer'):
            self.connection_check_timer.stop()
        event.accept()


class AdvancedFPGAControlsDialog(QDialog):
    """Dialog window for advanced FPGA connection controls."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_widget = parent
        self.setWindowTitle("Advanced FPGA Controls")
        self.setMinimumSize(600, 500)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # TCP Connection section
        tcp_section = self._create_connection_section("TCP", "Initiate TCP FPGA Connection", 
                                                       self._on_tcp_connect_clicked)
        main_layout.addWidget(tcp_section)
        
        # UDP Connection section
        udp_section = self._create_connection_section("UDP", "Initiate UDP FPGA Connection", 
                                                      self._on_udp_connect_clicked, 
                                                      show_close_button=True)
        main_layout.addWidget(udp_section)
        
        main_layout.addStretch()
    
    def _create_connection_section(self, connection_type, button_text, connect_handler, show_close_button=False):
        """Create a connection section widget."""
        section_widget = QWidget()
        section_layout = QVBoxLayout(section_widget)
        section_layout.setContentsMargins(5, 5, 5, 5)
        
        # Button container
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(5)
        
        # Connection button
        connect_button = create_button(button_text, "success", font_size="12pt", padding="10px")
        connect_button.clicked.connect(connect_handler)
        button_layout.addWidget(connect_button)
        
        # Add close button for UDP connections
        if show_close_button:
            close_button = create_button("Close UDP multicast", "error", font_size="12pt", padding="10px")
            close_button.clicked.connect(self._on_udp_close_clicked)
            button_layout.addWidget(close_button)
            self.close_button_udp = close_button
        
        section_layout.addWidget(button_container)
        
        # Response display area
        response_label = QLabel(f"{connection_type} Connection Response:")
        response_label.setStyleSheet("font-weight: bold; font-size: 11pt; padding-top: 10px;")
        section_layout.addWidget(response_label)
        
        response_display = QTextEdit()
        response_display.setReadOnly(True)
        response_display.setMinimumHeight(100)
        response_display.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 5px;
                font-family: 'Courier New', monospace;
            }
        """)
        section_layout.addWidget(response_display)
        
        # Store references
        if connection_type == "TCP":
            self.connect_button = connect_button
            self.response_display = response_display
            # Set parent's reference to this display
            if self.parent_widget:
                self.parent_widget.response_display = response_display
        else:
            self.connect_button_udp = connect_button
            self.response_display_udp = response_display
            # Set parent's reference to this display
            if self.parent_widget:
                self.parent_widget.response_display_udp = response_display
        
        return section_widget
    
    def _on_tcp_connect_clicked(self):
        """Handle TCP FPGA connection button click."""
        if self.parent_widget:
            # Run connection in a thread to avoid Qt threading issues
            import threading
            def connect_thread():
                self.parent_widget.on_tcp_connect_clicked()
                # Wait a bit for connection to establish
                import time
                time.sleep(0.5)
            
            thread = threading.Thread(target=connect_thread, daemon=True)
            thread.start()
            thread.join(timeout=6.0)  # Wait up to 6 seconds for connection
            
            # Sync status after connection attempt
            self.parent_widget._sync_tcp_connection_status()
    
    def _on_udp_connect_clicked(self):
        """Handle UDP FPGA connection button click."""
        if self.parent_widget:
            self.parent_widget.on_udp_connect_clicked()
    
    def _on_udp_close_clicked(self):
        """Handle UDP multicast connection close button click."""
        if self.parent_widget:
            self.parent_widget.on_udp_close_clicked()
    
    def update_displays(self):
        """Update response displays from parent widget (called when dialog is shown)."""
        # Displays are already connected, no need to update
        pass


class RealtimeAnalysisWorker(QThread):
    """Worker thread for running heavy real-time analysis computation off the GUI thread."""
    
    # Signals to communicate results back to GUI thread
    analysis_finished = Signal(bool)  # success
    results_ready = Signal(int, int, object, object, object, object, object, object, float)  # matched_count, unmatched_count, width_times (numpy), widths_ms (numpy), mass_times (numpy), masses_pg (numpy), throughput_times (numpy), throughput_values (numpy), current_throughput
    error_occurred = Signal(str)  # error message
    new_rows_ready = Signal(list)  # new rows for plotter
    
    def __init__(self, analyzer, parent=None):
        super().__init__(parent)
        self.analyzer = analyzer
        self.parent_widget = parent
        self._abort = False
        self._written_pair_times = set()  # Track written pairs in worker
    
    def abort(self):
        """Request worker to stop."""
        self._abort = True
    
    def run(self):
        """Run analysis in background thread."""
        if self._abort or self.analyzer is None:
            return
        
        try:
            # Run heavy computation off GUI thread
            success = self.analyzer.process()
            
            if self._abort:
                return
            
            if success:
                # 1. NEW: Handle CSV writing and collect new rows for plotter
                new_rows = self._handle_csv_writing()
                if new_rows and not self._abort:
                    self.new_rows_ready.emit(new_rows)
                
                # 2. Get results
                matched_count, unmatched_count = self.analyzer.get_peak_counts()
                width_times, widths_ms, mass_times, masses_pg = self.analyzer.get_plot_data()
                throughput_times, throughput_values, current_throughput = self.analyzer.get_throughput_data()
                
                # 3. NEW: Perform plot data filtering in background thread if parent exists
                if self.parent_widget:
                    try:
                        # Use the analyzer's latest buffer time as a true reference for the moving plots
                        # This ensures the X-axis advances even when no peaks are detected
                        ref_time = self.analyzer.latest_buffer_time
                        
                        # Fallback: if latest_buffer_time is not set, use most recent peak time
                        if ref_time <= 0:
                            import numpy as np
                            if len(width_times) > 0:
                                ref_time = np.max(width_times)
                            elif len(throughput_times) > 0:
                                ref_time = np.max(throughput_times)
                        
                        # Peak Width Plot Filtering
                        width_spin = getattr(self.parent_widget, 'peak_width_time_spin', None)
                        width_window = width_spin.value() * 60.0 if width_spin is not None else 1800.0
                        if len(width_times) > 0 and ref_time > 0:
                            mask = width_times >= (ref_time - width_window)
                            width_times = width_times[mask]
                            widths_ms = widths_ms[mask]
                            
                            # Downsample to avoid GUI thread stutter on dense datasets
                            if len(width_times) > 3000:
                                step = len(width_times) // 3000
                                width_times = width_times[::step]
                                widths_ms = widths_ms[::step]
                        
                        # Peak Mass Plot Filtering
                        mass_spin = getattr(self.parent_widget, 'peak_mass_time_spin', None)
                        mass_window = mass_spin.value() * 60.0 if mass_spin is not None else 1800.0
                        if len(mass_times) > 0 and ref_time > 0:
                            mask = mass_times >= (ref_time - mass_window)
                            mass_times = mass_times[mask]
                            masses_pg = masses_pg[mask]
                            
                            # Downsample to avoid GUI thread stutter on dense datasets
                            if len(mass_times) > 3000:
                                step = len(mass_times) // 3000
                                mass_times = mass_times[::step]
                                masses_pg = masses_pg[::step]
                            
                        # Throughput Plot Filtering
                        # Use mass window for throughput as well if available
                        tp_window = mass_spin.value() * 60.0 if mass_spin is not None else 1800.0
                        if len(throughput_times) > 0 and ref_time > 0:
                            mask = throughput_times >= (ref_time - tp_window)
                            throughput_times = throughput_times[mask]
                            throughput_values = throughput_values[mask]
                            
                            if len(throughput_times) > 3000:
                                step = len(throughput_times) // 3000
                                throughput_times = throughput_times[::step]
                                throughput_values = throughput_values[::step]
                    except Exception as e:
                        print(f"Error filtering plot data in worker: {e}")

                if not self._abort:
                    # Emit pre-filtered results to GUI thread
                    self.results_ready.emit(matched_count, unmatched_count, width_times, widths_ms, mass_times, masses_pg, throughput_times, throughput_values, current_throughput)
            
            if not self._abort:
                self.analysis_finished.emit(success)
                
        except Exception as e:
            error_msg = f"Error in real-time analysis worker: {e}"
            print(error_msg)
            import traceback
            traceback.print_exc()
            if not self._abort:
                self.error_occurred.emit(error_msg)
                self.analysis_finished.emit(False)

    def _handle_csv_writing(self):
        """Internal helper to handle CSV writing in background thread."""
        if not self.parent_widget or not self.parent_widget.is_saving:
            return
            
        sample_path = self.parent_widget.selected_sample_path
        exp_string = self.parent_widget.experiment_string
        if not sample_path or not exp_string:
            return
            
        try:
            matched_pairs = self.analyzer.get_matched_pairs_for_csv()
            if not matched_pairs:
                return
                
            csv_filename = f"{exp_string}_uncalibrated_peaks.csv"
            csv_filepath = os.path.join(sample_path, csv_filename)
            file_exists = os.path.exists(csv_filepath)
            
            import csv as csv_module
            with open(csv_filepath, 'a', encoding='utf-8', newline='') as csv_file:
                headers = [
                    'condition', 'peak_time', 'approximate_mass_pg', 'peak_width_ms', 
                    'peak1_delta_hz', 'peak2_delta_hz', 'packet_number', 'relative_time'
                ]
                writer = csv_module.DictWriter(csv_file, fieldnames=headers)
                if not file_exists:
                    writer.writeheader()
                
                new_rows = []
                current_condition = getattr(self.parent_widget, 'last_condition_name', 'N/A')
                for pair in matched_pairs:
                    pair_time = pair.get('peak1_time', pair.get('timestamp', 0))
                    if pair_time not in self._written_pair_times:
                        # Data for CSV (Slim version)
                        csv_row = {
                            'condition': current_condition,
                            'peak_time': (pair.get('peak1_time', 0) + pair.get('peak2_time', 0)) / 2.0,
                            'approximate_mass_pg': pair.get('mass_raw', 0),
                            'peak_width_ms': pair.get('separation_time', 0) * 1000.0,
                            'peak1_delta_hz': pair.get('peak1_deviation', 0),
                            'peak2_delta_hz': pair.get('peak2_deviation', 0),
                            'packet_number': pair.get('packet_number', 0),
                            'relative_time': pair.get('relative_time', 0)
                        }
                        writer.writerow(csv_row)
                        
                        # Full data for in-memory comparison plot list (includes extra features)
                        # This allows the comparison plot to still show baseline_hz etc.
                        full_row = csv_row.copy()
                        # Add extra features for the comparison plot
                        peak1_baseline = pair.get('peak1_baseline', 0)
                        peak2_baseline = pair.get('peak2_baseline', 0)
                        full_row['baseline_hz'] = (peak1_baseline + peak2_baseline) / 2.0
                        # These will be 0.0 as they were removed from real-time analyzer's output
                        # but we include them so the DataFrame has the columns expected by the combo box
                        full_row['node_dev'] = pair.get('node_dev', 0.0)
                        full_row['antinode_difference_raw'] = pair.get('antinode_difference_raw', 0.0)
                        full_row['mass_raw1'] = pair.get('mass_raw1', 0.0)
                        full_row['mass_raw2'] = pair.get('mass_raw2', 0.0)
                        full_row['baseline_noise'] = pair.get('baseline_noise', 0.0)
                        full_row['peak_noise'] = pair.get('peak_noise', 0.0)
                        full_row['baseline_slope'] = pair.get('baseline_slope', 0.0)
                        full_row['height_diff_percent'] = pair.get('height_diff_percent', 0.0)
                        
                        new_rows.append(full_row)
                        self._written_pair_times.add(pair_time)
                
                return new_rows
        except Exception as e:
            print(f"Error in background CSV writing: {e}")
            return []


class PostHocAnalysisWorker(QThread):
    """Worker thread for running post-hoc frequency analysis off the GUI thread."""
    
    analysis_finished = Signal(bool, str)  # success, message
    progress = Signal(int, str)  # value (0-100), message
    
    def __init__(self, uncalibrated_csv, parent=None, use_drift_correction=False, data_rate=20000.0, settings=None):
        super().__init__(parent)
        self.uncalibrated_csv = uncalibrated_csv
        self.use_drift_correction = use_drift_correction
        self.data_rate = data_rate
        self.settings = settings
        self.is_stopped = False
        self.analyzer = None # Store reference to analyzer for cancellation
    
    def run(self):
        """Run post-hoc analysis in background thread."""
        try:
            if not os.path.exists(self.uncalibrated_csv):
                self.analysis_finished.emit(False, f"Uncalibrated CSV not found: {self.uncalibrated_csv}")
                return
                
            def progress_callback(value, message):
                if self.is_stopped:
                    if self.analyzer:
                        self.analyzer.is_cancelled = True
                self.progress.emit(value, message)
                
            print(f"Starting post-hoc analysis (R-Parity) for {self.uncalibrated_csv} (Rate: {self.data_rate}Hz, Drift: {self.use_drift_correction})...")
            
            # Create the analyzer first with default settings for now
            # (Settings will be properly applied inside process_experiment)
            self.analyzer = PostHocFrequencyAnalyzer(progress_callback=progress_callback)
            
            # Since process_experiment creates its own analyzer by default, 
            # we pass our pre-created one so we have a reliable reference for cancellation.
            analyzer = process_experiment(
                self.uncalibrated_csv, 
                progress_callback=progress_callback, 
                use_drift_correction=self.use_drift_correction,
                data_rate=self.data_rate,
                settings=self.settings,
                analyzer=self.analyzer
            )
            if analyzer:
                self.analysis_finished.emit(True, f"Post-hoc calibration successful.")
            else:
                self.analysis_finished.emit(False, "Post-hoc calibration failed.")
        except Exception as e:
            error_msg = f"Error in post-hoc analysis worker: {e}"
            print(error_msg)
            traceback.print_exc()
            self.analysis_finished.emit(False, error_msg)
            
    def stop(self):
        """Flag the worker to stop gracefully."""
        self.is_stopped = True
        # If analyzer is currently running, it will check the cancellation flag in its next loop or progress callback
        if self.analyzer:
            self.analyzer.is_cancelled = True


class SampleInfoDialog(QDialog):
    """Dialog displaying conditions and experiment flags for the selected sample."""
    
    def __init__(self, sample_path: str, experiment_string: str = None, parent=None):
        super().__init__(parent)
        self.sample_path = sample_path
        self.experiment_string = experiment_string
        self.parent_widget = parent
        
        sample_name = os.path.basename(os.path.normpath(sample_path))
        self.setWindowTitle(f"Sample Info: {sample_name}")
        self.setMinimumSize(800, 600)
        
        main_layout = QHBoxLayout(self)
        
        # Left Column: Conditions
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("<b>Conditions in this sample:</b>"))
        
        self.conditions_display = QTextEdit()
        self.conditions_display.setReadOnly(True)
        self.conditions_display.setStyleSheet("background-color: white;")
        left_layout.addWidget(self.conditions_display)
        
        update_conditions_btn = QPushButton("Update conditions")
        update_conditions_btn.clicked.connect(self._on_update_conditions_clicked)
        left_layout.addWidget(update_conditions_btn)
        
        main_layout.addLayout(left_layout, 1)  # 1/4 width
        
        # Right Column: Experimental Flags
        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("<b>Experimental flags:</b>"))
        
        self.flags_table = QTableWidget()
        self.flags_table.setColumnCount(5)
        self.flags_table.setHorizontalHeaderLabels([
            "Time", "Experiment time", "Condition", "Number of peaks", ""
        ])
        # Stretch first 4 columns, fixed width for button column
        header = self.flags_table.horizontalHeader()
        for col in range(4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.flags_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.flags_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.flags_table.setAlternatingRowColors(True)
        self.flags_table.setStyleSheet("""
            QTableWidget {
                alternate-background-color: #f0f0f0;
                background-color: white;
            }
        """)
        
        # Store metadata for condition editing
        self._flags_file = None
        self._cond_col = None
        self._all_conditions = []
        
        right_layout.addWidget(self.flags_table)
        
        main_layout.addLayout(right_layout, 3)  # 3/4 width
        
        self._load_content()
        
    def _load_content(self):
        """Load content from local files."""
        # Load conditions.txt
        conditions_file = os.path.join(self.sample_path, "conditions.txt")
        if os.path.exists(conditions_file):
            try:
                with open(conditions_file, 'r', encoding='utf-8') as f:
                    self.conditions_display.setText(f.read())
            except Exception as e:
                self.conditions_display.setText(f"Error reading conditions.txt:\n{e}")
        else:
            self.conditions_display.setText("conditions.txt not found in local sample directory.")
            
        # Resolve Correct Flags File
        flags_file = self._resolve_flags_file()
        self._flags_file = flags_file
        if not flags_file:
            self._show_flag_error("No matching *_experiment_flags.txt found in sample directory.")
            return
            
        try:
            import polars as pl
            from datetime import datetime, timedelta
            
            flags_df = pl.read_csv(flags_file, separator='\t')
            if flags_df.is_empty():
                self._show_flag_error("Experiment flags file is empty.")
                return
                
            time_col = 'Datetime' if 'Datetime' in flags_df.columns else 'elapsed_time'
            cond_col = 'Flag ID' if 'Flag ID' in flags_df.columns else 'condition_name'
            self._cond_col = cond_col
            
            if time_col not in flags_df.columns or cond_col not in flags_df.columns:
                self._show_flag_error("Invalid experiment flags format.")
                return
                
            flags_df = flags_df.sort(time_col)
            flag_times = flags_df[time_col].to_list()
            flag_conds = flags_df[cond_col].to_list()
            
            # Start time from experiment string
            start_time_dt = None
            if self.experiment_string:
                parts = self.experiment_string.split('_')
                if len(parts) >= 2:
                    time_str = parts[-1]
                    if len(time_str) >= 12 and time_str.isdigit():
                        try:
                            start_time_dt = datetime.strptime(time_str[:12], "%Y%m%d%H%M")
                        except ValueError:
                            pass
                            
            # Load uncalibrated peaks
            # Flags file is .txt but peaks file is .csv — must fix extension
            peaks_file = flags_file.replace('_experiment_flags.txt', '_uncalibrated_peaks.csv')
            if not os.path.exists(peaks_file):
                peaks_file = flags_file.replace('_experimental_flags.txt', '_uncalibrated_peaks.csv')
                
            peaks_df = None
            if os.path.exists(peaks_file):
                try:
                    peaks_df = pl.read_csv(peaks_file)
                    if 'relative_time' not in peaks_df.columns:
                        peaks_df = None
                except Exception as e:
                    print(f"Error reading uncalibrated peaks: {e}")
                    pass
            
            # Collect all unique conditions for dropdown
            # Start with conditions from flags file
            unique_conditions = set(str(c) for c in flag_conds)
            # Add conditions from conditions.txt
            conditions_file = os.path.join(self.sample_path, "conditions.txt")
            if os.path.exists(conditions_file):
                try:
                    with open(conditions_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                unique_conditions.add(line)
                except Exception:
                    pass
            self._all_conditions = sorted(unique_conditions)
                    
            self.flags_table.setRowCount(len(flag_times))
            
            for i in range(len(flag_times)):
                rel_time = float(flag_times[i])
                cond = str(flag_conds[i])
                
                # 1. Absolute Time
                abs_time_str = "Unknown"
                if start_time_dt is not None:
                    row_dt = start_time_dt + timedelta(seconds=rel_time)
                    abs_time_str = row_dt.strftime("%H:%M")
                    
                # 2. Experiment Time
                rel_time_str = f"{rel_time:.1f} s"
                
                # 3. Condition
                cond_str = cond
                
                # 4. Peaks
                peak_count = "N/A"
                if peaks_df is not None:
                    start_t = rel_time
                    end_t = float(flag_times[i+1]) if i + 1 < len(flag_times) else float('inf')
                    count = len(peaks_df.filter(
                        (pl.col('relative_time') >= start_t) & 
                        (pl.col('relative_time') < end_t)
                    ))
                    peak_count = str(count)
                    
                self.flags_table.setItem(i, 0, QTableWidgetItem(abs_time_str))
                self.flags_table.setItem(i, 1, QTableWidgetItem(rel_time_str))
                self.flags_table.setItem(i, 2, QTableWidgetItem(cond_str))
                self.flags_table.setItem(i, 3, QTableWidgetItem(peak_count))
                
                # 5. Change condition button
                change_btn = QPushButton("Change condition")
                change_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #FF9800;
                        color: white;
                        border: none;
                        border-radius: 3px;
                        padding: 4px 8px;
                        font-size: 9pt;
                        font-weight: bold;
                    }
                    QPushButton:hover {
                        background-color: #F57C00;
                    }
                """)
                change_btn.clicked.connect(lambda checked, row=i: self._on_change_condition_clicked(row))
                self.flags_table.setCellWidget(i, 4, change_btn)
                
        except Exception as e:
            self._show_flag_error(f"Error reading flags: {e}")
            import traceback
            traceback.print_exc()

    def _on_change_condition_clicked(self, row):
        """Handle 'Change condition' button click for a specific row."""
        current_condition = self.flags_table.item(row, 2).text() if self.flags_table.item(row, 2) else ""
        
        # Create popup dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Change Condition")
        dialog.setMinimumWidth(350)
        layout = QVBoxLayout(dialog)
        
        # Label
        layout.addWidget(QLabel(f"Current condition: <b>{current_condition}</b>"))
        layout.addWidget(QLabel("Select new condition:"))
        
        # Build condition list with Calibration as a default
        conditions = list(self._all_conditions)
        if "Calibration" not in conditions:
            conditions.insert(0, "Calibration")
        
        # Radio buttons
        from PySide6.QtWidgets import QButtonGroup, QRadioButton
        radio_style = """
            QRadioButton {
                background-color: white;
                color: black;
                padding: 8px 12px;
                border-radius: 4px;
                font-size: 10pt;
                spacing: 8px;
            }
            QRadioButton::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 2px solid #999;
                background-color: #666666;
            }
            QRadioButton::indicator:checked {
                background-color: #64b5f6;
                border: 2px solid #1976d2;
            }
        """
        button_group = QButtonGroup(dialog)
        for cond in conditions:
            radio = QRadioButton(cond)
            radio.setStyleSheet(radio_style)
            if cond == current_condition:
                radio.setChecked(True)
            button_group.addButton(radio)
            layout.addWidget(radio)
        
        # Buttons
        buttons_layout = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        update_btn = QPushButton("Update condition and push to file")
        update_btn.setStyleSheet("""
            QPushButton {
                background-color: #1a73e8;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1557b0;
            }
        """)
        update_btn.clicked.connect(dialog.accept)
        buttons_layout.addWidget(cancel_btn)
        buttons_layout.addWidget(update_btn)
        layout.addLayout(buttons_layout)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = button_group.checkedButton()
            if selected:
                new_condition = selected.text()
                if new_condition != current_condition:
                    # Update the table cell
                    self.flags_table.setItem(row, 2, QTableWidgetItem(new_condition))
                    # Update the file
                    self._update_condition_in_file(row, new_condition)
    
    def _update_condition_in_file(self, row, new_condition):
        """Update the condition for a specific row in the experiment flags file."""
        if not self._flags_file or not self._cond_col:
            QMessageBox.warning(self, "Error", "Flags file or column name not available.")
            return
        
        try:
            import polars as pl
            
            flags_df = pl.read_csv(self._flags_file, separator='\t')
            time_col = 'Datetime' if 'Datetime' in flags_df.columns else 'elapsed_time'
            
            # Sort by time (same order as table display)
            flags_df = flags_df.sort(time_col)
            
            # Update the condition at the specified row index
            if row < 0 or row >= len(flags_df):
                QMessageBox.warning(self, "Error", f"Row index {row} out of range.")
                return
            
            # Create a new column with the updated value at the target row
            cond_values = flags_df[self._cond_col].to_list()
            cond_values[row] = new_condition
            flags_df = flags_df.with_columns(
                pl.Series(name=self._cond_col, values=cond_values)
            )
            
            # Write back to file as TSV
            flags_df.write_csv(self._flags_file, separator='\t')
            
            print(f"Updated condition at row {row} to '{new_condition}' in {os.path.basename(self._flags_file)}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to update flags file:\n{e}")
            import traceback
            traceback.print_exc()

    def _show_flag_error(self, msg):
        """Helper to display error messages inside the flags table."""
        self.flags_table.setRowCount(1)
        self.flags_table.setColumnCount(1)
        self.flags_table.setHorizontalHeaderLabels(["Message"])
        item = QTableWidgetItem(msg)
        self.flags_table.setItem(0, 0, item)
        self.flags_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _resolve_flags_file(self):
        """Resolve the path to the experiment flags file based on prefix or most recent."""
        # 1. Try to use explicit experiment string if provided
        if self.experiment_string:
            # Check both common suffixes (in case of past inconsistencies)
            for suffix in ["_experiment_flags.txt", "_experimental_flags.txt"]:
                path = os.path.join(self.sample_path, f"{self.experiment_string}{suffix}")
                if os.path.exists(path):
                    return path
        
        # 2. Look for any matching files in the directory
        # Prioritize the current creation naming convention: *_experiment_flags.txt
        # Then fallback to the legacy: *_experimental_flags.txt
        for pattern in ["*_experiment_flags.txt", "*_experimental_flags.txt"]:
            matches = glob.glob(os.path.join(self.sample_path, pattern))
            if matches:
                # Return the most recently modified matching file
                return max(matches, key=os.path.getmtime)
        
        return None

    def _on_update_conditions_clicked(self):
        """Fetch conditions.txt from NAS source logic and update local pyPump."""
        try:
            # 1. Resolve source sample directory using nas_sample_path
            from helper_functions.META_sample_selection import _get_nas_sample_path
            nas_path = _get_nas_sample_path()
            if not nas_path:
                QMessageBox.warning(self, "Error", "NAS sample path is not configured. Cannot update conditions.")
                return
                
            sample_name = os.path.basename(os.path.normpath(self.sample_path))
            source_dir = os.path.join(nas_path, sample_name)
            
            # Handle _mc samples - they represent a modified local copy, 
            # but usually want to sync conditions from the original source.
            if not os.path.exists(source_dir) and "_mc" in sample_name:
                original_name = sample_name.split("_mc")[0] # Strip _mc and anything after
                source_dir = os.path.join(nas_path, original_name)
            
            if not os.path.exists(source_dir):
                QMessageBox.warning(self, "Error", f"Could not find source directory on NAS:\n{source_dir}")
                return
                
            source_conditions_file = os.path.join(source_dir, "conditions.txt")
            if not os.path.exists(source_conditions_file):
                QMessageBox.warning(self, "Error", f"conditions.txt not found in source directory:\n{source_conditions_file}")
                return
                
            # 2. Copy the conditions.txt file, overwriting the local one
            local_conditions_file = os.path.join(self.sample_path, "conditions.txt")
            import shutil
            shutil.copy2(source_conditions_file, local_conditions_file)
            
            # 3. Reload the display in this window
            self._load_content()
            
            # 4. Notify pyPump to reload the conditions dropdown
            # Check both standard locations for pump_widget reference
            pump_widget = None
            if hasattr(self.parent_widget, 'pump_widget'):
                pump_widget = self.parent_widget.pump_widget
            elif hasattr(self.parent_widget, 'window') and hasattr(self.parent_widget.window(), 'pump_control_widget'):
                pump_widget = self.parent_widget.window().pump_control_widget
                
            if pump_widget:
                pump_widget.load_conditions_from_sample_folder(self.sample_path)
                
            QMessageBox.information(self, "Success", "conditions.txt updated successfully from the server source.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to update conditions: {e}")


class SMRControlWidget(QWidget):
    """Embeddable widget for SMR (Suspended Microchannel Resonator) control."""
    
    # Signal for GUI mode changes
    gui_mode_changed = Signal(str)  # Emits 'basic' or 'advanced'
    
    # Signals for console logging redirection
    saving_started = Signal(str, str)  # Emits (sample_path, experiment_string)
    saving_stopped = Signal()
    data_saver_finished = Signal()    # Emits when DataSaver background thread is done
    console_log_finished = Signal()  # Emits when console log file should be closed
    # Signal for comparison plot worker
    comparison_results_ready = Signal(list)
    posthoc_progress = Signal(int, str)

    def __init__(self, parent=None, operator: Optional[str] = None):
        super().__init__(parent)
        self.operator = operator  # Store operator from main_gui if provided
        self.fpga_command_queue = FPGACommandQueue()
        self.udp_data_manager = UDPDataManager()
        
        # Initialize an empty list to track matched peaks
        self.matched_peaks_list = []
        self.matched_peaks_df = pl.DataFrame()
        self.comparison_worker = None
        self.comparison_results_ready.connect(self._on_comparison_results_ready)

        # NOTE: Config file is READ-ONLY. This code should never write to it.
        self.config_file = os.path.join(REFERENCES_DIR, 'SMR_config.txt')
        # Config values stored in instance variables
        self.nios_ip = "192.168.100.2"
        self.multicast_ip = "224.1.1.1"
        self.host_ip = "192.168.100.1"
        self.udp_port = 5007
        self.remote_port = 30
        # Packet-based data storage (written by receive thread/process only)
        # Store last 10000 packets, automatically discards older packets
        self.packets = deque(maxlen=10000)
        # Frequency-based data storage for optimized plotting (stores individual frequencies)
        # Store last 128000 frequencies (1000 packets * 128 frequencies per packet)
        self.frequencies = deque(maxlen=128000)
        self.start_time = None
        self.receiving_udp_data = False
        
        # UDP manager subscription
        self.udp_subscriber_id = None
        self.udp_subscriber_queue = None
        
        # Threading mode
        self.udp_receive_thread = None
        
        self.packet_count = 0  # Current packet count from FPGA
        # Diagnostic data storage
        self.timestamp_deltas = deque(maxlen=100)  # Store last 100 timestamp deltas
        self.last_timestamp = None  # Track last packet timestamp for delta calculation
        self.diagnostic_window = None  # Reference to diagnostic window
        
        # Data rate tracking: store (timestamp, num_frequencies) for packets in last 1 second
        self.packet_timestamps = deque()  # Store (timestamp, num_frequencies) tuples
        self.data_rate = 0.0  # Current data rate (frequencies per second)
        
        # Extended frequency bounds tracking (for stable y-axis)
        # Track min/max/mean frequencies from last 5x max_packets worth of data
        # Use percentiles and sticky bounds for stability
        self.extended_freq_min = None  # Lower bound (5th percentile or min)
        self.extended_freq_max = None  # Upper bound (95th percentile or max)
        self.extended_freq_mean = None  # Mean frequency in extended window
        self.extended_freq_window = deque()  # Store frequencies from extended window for recalculation
        self.stable_freq_min = None  # Stable lower bound (expands quickly, contracts slowly)
        self.stable_freq_max = None  # Stable upper bound (expands quickly, contracts slowly)
        self.stable_freq_mean = None  # Stable mean (updates slowly)
        
        # Pre-prepared plot data (prepared in separate thread, swapped atomically)
        # Using list reference for atomic swapping (no lock needed - Python list assignment is atomic)
        # Format: [time_array, freq_array] - simplified format, no ranges needed (pyqtgraph handles auto-ranging)
        self.plot_data = [np.array([], dtype=np.float64), np.array([], dtype=np.float64)]
        self.plot_prep_thread = None  # Thread for preparing plot data
        self.plot_prep_running = False
        self._plot_update_pending = False  # Flag to prevent queuing multiple updates
        
        # SMR settings popup window
        self.smr_settings_window = None
        self.smr_settings_widget = None
        # Quick control values (for auto-push)
        self.quick_run_value = False
        self.quick_pll_delay_value = 0.0
        self.quick_pll_drive_amplitude_value = 0.1
        
        # Advanced FPGA controls dialog
        self.advanced_controls_dialog = None
        
        # Response displays (for advanced controls dialog)
        self.response_display = None
        self.response_display_udp = None

        # Connection status tracking
        self.tcp_connection_status = "disconnected"  # "disconnected", "connected", "failed"
        self.udp_connection_status = "disconnected"  # "disconnected", "connected", "failed"
        
        # Track TCP status signal connection to avoid disconnect warnings
        self._tcp_status_signal_connected = False
        
        # Connect to CommandWorker signal to automatically update TCP connection status
        self._connect_tcp_status_signal()
        
        # Sync initial connection status
        self._sync_tcp_connection_status()
        
        # SMR initialization tracking
        self.smr_initialized = False
        self.set_delays_run = False
        
        # Sample selection tracking
        self.selected_sample_path = None
        self.is_saving = False
        self.is_final_clean = False # Tracking for final cleanup mode
        self.saving_start_time = None  # Epoch time for UI display and experiment flags
        self.experiment_start_time_for_packets = None  # Packet timestamp format for data_saver
        self.experiment_string = None
        
        # Pump widget reference for loading conditions
        self.pump_widget = None
        
        # Data saver for UDP packet saving
        self.data_saver = None
        
        # Timer for updating elapsed saving time
        self.saving_time_timer = QTimer()
        self.saving_time_timer.timeout.connect(self._update_saving_elapsed_time)
        
        # Store references to last results windows
        self.last_sweep_window = None  # Last SweepWindow from Initialize SMR
        self.set_delays_window = None  # Last SetDelaysWindow (already tracked)
        
        # Connection dialog
        self.connection_dialog = None
        
        # DAQ info for substrate bias control
        self.daq_name = None
        self.substrate_bias_address = None
        self._load_daq_info()
        
        # Peak detection settings (initialize with defaults)
        self.peak_detection_settings = PeakDetectionSettings()
        
        # Peak detection settings dialog
        self.peak_detection_settings_window = None
        
        # GUI Mode state
        self.gui_mode = "basic"  # Default to basic
        self.image_saving_enabled_internal = False # Track image saving enable state from pyImage
        self.roi_mode_enabled_internal = False # Track ROI mode state from pyImage
        
        # Real-time analyzer (will be initialized when data rate is known)
        self.realtime_analyzer = None
        self.realtime_analysis_timer = None
        self.realtime_analysis_worker = None  # QThread worker for heavy computation
        self.realtime_analysis_worker_busy = False  # Flag to prevent overlapping analysis
        self.posthoc_worker = None
        self._last_uncalibrated_csv = None
        self.detect_peaks_enabled = False  # Flag to control whether peak detection runs
        
        # Peak tracking for sample and condition
        self.sample_peaks_total = 0  # Total matched peaks for entire sample
        self.condition_peaks_total = 0  # Matched peaks for current condition
        self.last_condition_name = None  # Track condition changes to reset condition peaks
        self.matched_count_at_condition_start = 0  # Track matched_count when condition started
        self.matched_count_at_sample_start = 0     # Track matched_count when sample started
        
        # Calculate offset to convert monotonic time (perf_counter) to absolute wall-clock time (Epoch)
        # This is used to fix the time discrepancy on charts (HH:MM display)
        self._timestamp_offset = time.time() - time.perf_counter()
        
        self.setup_ui()
        # Load config after UI is set up
        self.load_config()
    
    def setup_ui(self):
        """Set up the user interface for SMR control."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Three-column layout
        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(10)
        
        # Left column: Connection buttons
        left_column = QWidget()
        left_column_layout = QVBoxLayout(left_column)
        left_column_layout.setContentsMargins(5, 5, 5, 5)
        
        # Buttons row: Select Sample, Start saving, and Reset FPGA button
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(10)
        
        # Check for permissions
        config = load_system_config()
        sample_creation_perms = []
        if "system" in config and "sample_creation_perms" in config["system"]:
            sample_creation_perms = config["system"]["sample_creation_perms"]
            
        sample_selection_perms = []
        if "system" in config and "sample_selection_perms" in config["system"]:
            sample_selection_perms = config["system"]["sample_selection_perms"]
            
        # Select Sample button (left-justified, only visible if authorized)
        if self.operator in sample_selection_perms:
            select_sample_button = create_button("Select Sample", "primary")
            select_sample_button.clicked.connect(self.on_select_sample_clicked)
            # Initial styling will be set by _update_select_sample_button()
            buttons_row.addWidget(select_sample_button)
            self.select_sample_button = select_sample_button
            
        # Create Sample button (only visible if authorized)
        if self.operator in sample_creation_perms:
            self.create_sample_button = create_button("Create Sample", "success")
            self.create_sample_button.clicked.connect(self.on_create_sample_clicked)
            buttons_row.addWidget(self.create_sample_button)
        
        # Start saving button (left-justified, next to Select Sample)
        start_saving_button = create_button("Start saving", "success")
        start_saving_button.clicked.connect(self.on_start_saving_clicked)
        # Initial styling will be set by _update_start_saving_button()
        buttons_row.addWidget(start_saving_button)
        self.start_saving_button = start_saving_button
        
        # Sample Info button
        sample_info_button = create_button("Sample Info", "primary")
        sample_info_button.clicked.connect(self.on_sample_info_clicked)
        buttons_row.addWidget(sample_info_button)
        self.sample_info_button = sample_info_button
        
        # Add stretch to push Reset FPGA button to the right
        buttons_row.addStretch()
        
        # Reset FPGA button
        reset_fpga_button = create_button("Reset FPGA", "error", font_size="12pt", padding="10px")
        reset_fpga_button.clicked.connect(self.on_reset_fpga_clicked)
        buttons_row.addWidget(reset_fpga_button)
        
        left_column_layout.addLayout(buttons_row)
        
        # FPGA Status section
        self.fpga_status_group = QGroupBox("FPGA status")
        fpga_status_layout = QHBoxLayout()
        fpga_status_layout.setSpacing(10)
        
        # Connection status indicators
        tcp_indicator = self._create_connection_indicator("TCP connection")
        fpga_status_layout.addWidget(tcp_indicator)
        self.tcp_indicator = tcp_indicator
        
        udp_indicator = self._create_connection_indicator("UDP connection")
        fpga_status_layout.addWidget(udp_indicator)
        self.udp_indicator = udp_indicator
        
        # Data rate indicator (moved from right column)
        data_rate_indicator = self._create_text_indicator("Data rate")
        fpga_status_layout.addWidget(data_rate_indicator)
        self.data_rate_indicator = data_rate_indicator
        # Initialize to "-- Hz"
        if hasattr(data_rate_indicator, 'value_label'):
            data_rate_indicator.value_label.setText("-- Hz")
        
        fpga_status_layout.addStretch()
        self.fpga_status_group.setLayout(fpga_status_layout)
        left_column_layout.addWidget(self.fpga_status_group)
        
        # System section
        self.system_group = QGroupBox("System")
        system_layout = QHBoxLayout()
        system_layout.setSpacing(10)
        
        system_name_indicator = self._create_text_indicator("System name")
        system_layout.addWidget(system_name_indicator)
        self.system_name_indicator = system_name_indicator
        
        operator_indicator = self._create_text_indicator("Operator")
        system_layout.addWidget(operator_indicator)
        self.operator_indicator = operator_indicator
        
        chip_name_indicator = self._create_text_indicator("Chip name")
        system_layout.addWidget(chip_name_indicator)
        self.chip_name_indicator = chip_name_indicator
        
        system_layout.addStretch()
        self.system_group.setLayout(system_layout)
        left_column_layout.addWidget(self.system_group)
        
        # Sample section
        sample_group = QGroupBox("Sample")
        sample_layout = QHBoxLayout()
        sample_layout.setSpacing(10)
        
        sample_indicator = self._create_text_indicator("Sample name")
        sample_layout.addWidget(sample_indicator)
        self.sample_indicator = sample_indicator
        
        saving_time_indicator = self._create_text_indicator("Saving time")
        sample_layout.addWidget(saving_time_indicator)
        self.saving_time_indicator = saving_time_indicator
        # Initialize to "N/A"
        if hasattr(saving_time_indicator, 'value_label'):
            saving_time_indicator.value_label.setText("N/A")
        
        sample_peaks_indicator = self._create_text_indicator("Total peaks")
        sample_layout.addWidget(sample_peaks_indicator)
        self.sample_peaks_indicator = sample_peaks_indicator
        # Initialize to "0"
        if hasattr(sample_peaks_indicator, 'value_label'):
            sample_peaks_indicator.value_label.setText("0")
        
        sample_layout.addStretch()
        sample_group.setLayout(sample_layout)
        left_column_layout.addWidget(sample_group)
        
        # Condition section
        condition_group = QGroupBox("Condition")
        condition_layout = QHBoxLayout()
        condition_layout.setSpacing(10)
        
        current_condition_indicator = self._create_text_indicator("Condition name")
        condition_layout.addWidget(current_condition_indicator)
        self.current_condition_indicator = current_condition_indicator
        # Initialize to "N/A"
        if hasattr(current_condition_indicator, 'value_label'):
            current_condition_indicator.value_label.setText("N/A")
        
        condition_peaks_indicator = self._create_text_indicator("# of peaks")
        condition_layout.addWidget(condition_peaks_indicator)
        self.condition_peaks_indicator = condition_peaks_indicator
        # Initialize to "0"
        if hasattr(condition_peaks_indicator, 'value_label'):
            condition_peaks_indicator.value_label.setText("0")
        
        throughput_indicator = self._create_text_indicator("Throughput")
        condition_layout.addWidget(throughput_indicator)
        self.throughput_indicator = throughput_indicator
        # Initialize to "--"
        if hasattr(throughput_indicator, 'value_label'):
            throughput_indicator.value_label.setText("--")
            
        concentration_indicator = self._create_text_indicator("Conc.")
        condition_layout.addWidget(concentration_indicator)
        self.concentration_indicator = concentration_indicator
        # Initialize to "--"
        if hasattr(concentration_indicator, 'value_label'):
            concentration_indicator.value_label.setText("--")
            concentration_indicator.value_label.setStyleSheet("padding: 2px; border-radius: 3px; font-weight: bold;")
        
        condition_layout.addStretch()
        condition_group.setLayout(condition_layout)
        left_column_layout.addWidget(condition_group)

        # Fluidic Status section
        # Module Statuses section
        module_statuses_group = QGroupBox("Module statuses")
        module_statuses_layout = QHBoxLayout()
        module_statuses_layout.setSpacing(10)
        
        # Helper to apply large bold styling to module status indicators
        def style_module_indicator(indicator):
            if hasattr(indicator, 'value_label'):
                indicator.value_label.setStyleSheet(
                    indicator.value_label.styleSheet() + 
                    "font-size: 14pt; font-weight: bold;"
                )
        
        # Fluidic Status
        fluidic_indicator = self._create_text_indicator("Fluidic")
        module_statuses_layout.addWidget(fluidic_indicator)
        self.fluidic_state_indicator = fluidic_indicator # Keep internal name for compatibility
        if hasattr(fluidic_indicator, 'value_label'):
            fluidic_indicator.value_label.setText("Not Initialized")
            fluidic_indicator.value_label.setStyleSheet("background-color: #ffcccc; padding: 2px; border-radius: 3px; font-size: 14pt; font-weight: bold;") 
        
        # SMR Status
        smr_status_indicator = self._create_text_indicator("SMR")
        module_statuses_layout.addWidget(smr_status_indicator)
        self.smr_status_indicator = smr_status_indicator
        style_module_indicator(smr_status_indicator)
        
        # Imaging Status
        imaging_status_indicator = self._create_text_indicator("Imaging")
        module_statuses_layout.addWidget(imaging_status_indicator)
        self.imaging_status_indicator = imaging_status_indicator
        style_module_indicator(imaging_status_indicator)
        
        # Initialize SMR and Imaging displays
        self._update_smr_status_display()
        self.set_imaging_status(False)
        
        module_statuses_layout.addStretch()
        module_statuses_group.setLayout(module_statuses_layout)
        left_column_layout.addWidget(module_statuses_group)
        
        # Bottom Controls row: GUI Mode Toggle and Peak Detection
        bottom_controls_layout = QHBoxLayout()
        
        # GUI Mode Toggle button (bottom left, left-justified)
        self.mode_toggle_button = create_button("Switch to Advanced UI", "warning", font_size="11pt", padding="8px 16px")
        self.mode_toggle_button.clicked.connect(self.toggle_gui_mode)
        bottom_controls_layout.addWidget(self.mode_toggle_button)
        
        bottom_controls_layout.addStretch()
        
        # Detect Peaks checkbox control (right-justified)
        detect_peaks_checkbox = QCheckBox("Detect Peaks")
        detect_peaks_checkbox.setChecked(False)  # Default to disabled
        style_checkbox(detect_peaks_checkbox)
        
        # Style the checkbox: bright green (#00FF00) when enabled, dark gray when disabled (matching Auto Y-Range)
        def update_detect_peaks_style():
            if detect_peaks_checkbox.isChecked():
                detect_peaks_checkbox.setStyleSheet("""
                    QCheckBox {
                        font-size: 11pt;
                        font-weight: bold;
                        padding: 5px;
                    }
                    QCheckBox::indicator {
                        width: 20px;
                        height: 20px;
                        border: 2px solid #00CC00;
                        border-radius: 3px;
                        background-color: #00FF00;
                    }
                    QCheckBox::indicator:checked {
                        background-color: #00FF00;
                        border: 2px solid #00CC00;
                    }
                    QCheckBox::indicator:unchecked {
                        background-color: #00FF00;
                        border: 2px solid #00CC00;
                    }
                """)
            else:
                detect_peaks_checkbox.setStyleSheet("""
                    QCheckBox {
                        font-size: 11pt;
                        font-weight: bold;
                        padding: 5px;
                    }
                    QCheckBox::indicator {
                        width: 20px;
                        height: 20px;
                        border: 2px solid #444444;
                        border-radius: 3px;
                        background-color: #404040;
                    }
                    QCheckBox::indicator:checked {
                        background-color: #404040;
                        border: 2px solid #444444;
                    }
                    QCheckBox::indicator:unchecked {
                        background-color: #404040;
                        border: 2px solid #444444;
                    }
                """)
        
        # Update style initially and when toggled
        update_detect_peaks_style()
        detect_peaks_checkbox.toggled.connect(update_detect_peaks_style)
        detect_peaks_checkbox.toggled.connect(self._on_detect_peaks_toggled)
        bottom_controls_layout.addWidget(detect_peaks_checkbox)
        
        peak_detection_settings_button = create_button("Peak Detection Settings", "warning", font_size="11pt", padding="8px 16px")
        peak_detection_settings_button.clicked.connect(self.on_peak_detection_settings_clicked)
        bottom_controls_layout.addWidget(peak_detection_settings_button)
        left_column_layout.addLayout(bottom_controls_layout)
        self.peak_detection_settings_button = peak_detection_settings_button
        self.detect_peaks_checkbox = detect_peaks_checkbox
        
        # Initialize system information indicators
        self._update_system_info_indicators()
        
        # Initialize Initialize SMR button state
        self._update_initialize_smr_button()
        # Initialize Set Delays button state
        self._update_set_delays_button()
        # Initialize Select Sample and Start saving button states
        self._update_select_sample_button()
        self._update_start_saving_button()
        
        left_column_layout.addStretch()
        
        columns_layout.addWidget(left_column)
        
        # Middle column: SMR Settings and Push Settings button
        middle_column = self._create_settings_column()
        columns_layout.addWidget(middle_column)
        
        # Right column: Plot
        plot_column = self._create_plot_column()
        columns_layout.addWidget(plot_column)
        
        main_layout.addLayout(columns_layout)
        main_layout.addStretch()
        
        self._setup_styles()
        
        # Initialize connection indicators
        self._update_connection_indicators()
        # Initialize Push settings button state based on TCP connection
        self._update_push_settings_button_state()
        
        # Apply initial GUI mode
        self.set_gui_mode(self.gui_mode)
    
    def toggle_gui_mode(self):
        """Toggle between basic and advanced GUI modes."""
        new_mode = "advanced" if self.gui_mode == "basic" else "basic"
        self.set_gui_mode(new_mode)
        self.gui_mode_changed.emit(new_mode)

    def set_gui_mode(self, mode):
        """Set the GUI mode and update UI visibility."""
        self.gui_mode = mode
        is_advanced = (mode == "advanced")
        
        # Update toggle button text
        if hasattr(self, 'mode_toggle_button'):
            self.mode_toggle_button.setText("Switch to Basic UI" if is_advanced else "Switch to Advanced UI")
            
        # Hide/show status sections
        if hasattr(self, 'fpga_status_group'):
            self.fpga_status_group.setVisible(is_advanced)
        if hasattr(self, 'system_group'):
            self.system_group.setVisible(is_advanced)
        
        # Hide/show pySMR specific elements
        if hasattr(self, 'peak_detection_settings_button'):
            self.peak_detection_settings_button.setVisible(is_advanced)
        if hasattr(self, 'detect_peaks_checkbox'):
            self.detect_peaks_checkbox.setVisible(is_advanced)
        if hasattr(self, 'initialize_smr_button'):
            self.initialize_smr_button.setVisible(is_advanced)
        if hasattr(self, 'set_delays_button'):
            self.set_delays_button.setVisible(is_advanced)
        if hasattr(self, 'smr_settings_button'):
            self.smr_settings_button.setVisible(is_advanced)
        
        # SMR Settings button (it's not stored as an instance variable, need to find it or store it)
        # Looking at _create_settings_column, it's the 3rd button in buttons_row
        
        # Bias, Delay controls
        if hasattr(self, 'substrate_bias_control'):
            # The parent container is the gray box
            self.substrate_bias_control.parentWidget().setVisible(is_advanced)
        if hasattr(self, 'quick_pll_delay_control'):
            self.quick_pll_delay_control.parentWidget().setVisible(is_advanced)
            
        if hasattr(self, 'detect_peaks_checkbox'):
            self.detect_peaks_checkbox.setVisible(is_advanced)
        
        # Plot controls - Display last N peaks, Show Diagnostic Plot
        if hasattr(self, 'max_packets'):
            self.max_packets.setVisible(is_advanced)
            
            # Find the parent container (plot column)
            parent = self.max_packets.parent()
            if parent:
                # Hide the label "Display last N packets:"
                for label in parent.findChildren(QLabel):
                    if label.text() == "Display last N packets:":
                        label.setVisible(is_advanced)
                
                # Hide the "Show Diagnostic Plot" button
                for button in parent.findChildren(QPushButton):
                    if button.text() == "Show Diagnostic Plot":
                        button.setVisible(is_advanced)

    def _create_connection_indicator(self, label_text):
        """Create a connection status indicator widget with circular indicator and text."""
        return create_connection_indicator(label_text)
    
    def _update_connection_indicator(self, indicator_widget, status):
        """
        Update connection indicator appearance based on status.
        
        Args:
            indicator_widget: The indicator widget to update
            status: "disconnected" (gray), "connected" (green), or "failed" (red)
        """
        update_connection_indicator(indicator_widget, status)
    
    def _create_text_indicator(self, label_text):
        """Create a text indicator widget with label and value."""
        return create_text_indicator(label_text)
    
    def _update_system_info_indicators(self):
        """Update system information indicators (system name, chip name, operator)."""
        try:
            config = load_system_config()
            
            # Get system name
            system_name = get_system_name(config)
            if hasattr(self, 'system_name_indicator'):
                self.system_name_indicator.value_label.setText(system_name if system_name else "N/A")
            
            # Get chip name (from active_devices file)
            from helper_functions.SMR_settings_io import _get_chip_and_system_name
            chip_name, _ = _get_chip_and_system_name()
            if hasattr(self, 'chip_name_indicator'):
                self.chip_name_indicator.value_label.setText(chip_name if chip_name else "N/A")
            
            # Get operator (from config - show first available or N/A)
            # Favor the operator passed during initialization (e.g. from startup dialog)
            if self.operator:
                operator_text = self.operator
            else:
                operators = get_operators(config)
                operator_text = operators[0] if operators else "N/A"
                
            if hasattr(self, 'operator_indicator'):
                self.operator_indicator.value_label.setText(operator_text)
        except Exception as e:
            print(f"Error updating system info indicators: {e}")
            if hasattr(self, 'system_name_indicator'):
                self.system_name_indicator.value_label.setText("Error")
            if hasattr(self, 'chip_name_indicator'):
                self.chip_name_indicator.value_label.setText("Error")
            if hasattr(self, 'operator_indicator'):
                self.operator_indicator.value_label.setText("Error")
    
    def _update_connection_indicators(self):
        """Update both TCP and UDP connection indicators based on current status."""
        if hasattr(self, 'tcp_indicator'):
            self._update_connection_indicator(self.tcp_indicator, self.tcp_connection_status)
        if hasattr(self, 'udp_indicator'):
            self._update_connection_indicator(self.udp_indicator, self.udp_connection_status)
        
        # Update Push settings button state when connection indicators are updated
        self._update_push_settings_button_state()
    
    def on_reset_fpga_clicked(self, skip_confirmation=False, silent=False):
        """Handle Reset FPGA button click - show confirmation dialog, then reset dialog.
        
        Args:
            skip_confirmation: If True, bypass the confirmation dialog and proceed directly
                              to the reset dialog. Used when called from connection failure popup.
            silent: If True, run the reset in the background without showing any dialogs.
                   Used for automated setup.
        """
        # If silent, skip all dialogs and run in background
        if silent:
            def silent_reset_worker():
                try:
                    # Run reset (this takes ~2.5s)
                    success = reset_fpga_relay()
                    print(f"Silent FPGA reset complete. Success: {success}")
                except Exception as e:
                    print(f"Error in silent FPGA reset: {e}")
            
            # Start background thread
            thread = threading.Thread(target=silent_reset_worker, daemon=True)
            thread.start()
            return

        # Show confirmation dialog unless skipping
        if not skip_confirmation:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Reset FPGA")
            msg_box.setText("This will disconnect and restart the FPGA")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            
            # Add custom buttons
            cancel_button = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            continue_button = msg_box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
            msg_box.setDefaultButton(cancel_button)
            
            # Style the buttons
            cancel_button.setStyleSheet("""
                QPushButton {
                    background-color: #f44336;
                    color: white;
                    font-size: 11pt;
                    font-weight: bold;
                    padding: 8px 16px;
                    border-radius: 5px;
                    min-width: 80px;
                }
                QPushButton:hover {
                    background-color: #da190b;
                }
                QPushButton:pressed {
                    background-color: #c62828;
                }
            """)
            
            continue_button.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    font-size: 11pt;
                    font-weight: bold;
                    padding: 8px 16px;
                    border-radius: 5px;
                    min-width: 80px;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
                QPushButton:pressed {
                    background-color: #3d8b40;
                }
            """)
            
            # Show dialog and check result
            reply = msg_box.exec()
            
            # Only proceed if Continue was clicked
            if msg_box.clickedButton() != continue_button:
                return
        
        # Proceed to reset dialog
        reset_dialog = FPGAResetDialog(self)
        # If automated setup status window exists, ensure it stays on top
        if hasattr(self, '_automated_setup_status_window') and self._automated_setup_status_window:
            # Set parent to status window so dialog appears below it
            reset_dialog.setParent(self._automated_setup_status_window)
            reset_dialog.setWindowFlags(
                Qt.WindowType.Dialog |
                Qt.WindowType.WindowTitleHint |
                Qt.WindowType.WindowCloseButtonHint
            )
        reset_dialog.exec()
        # Ensure status window is raised after dialog closes
        if hasattr(self, '_automated_setup_status_window') and self._automated_setup_status_window:
            self._automated_setup_status_window.raise_()
    
    @Slot(str, str)
    def update_fluidic_state(self, state, message):
        """Update fluidic state indicator."""
        if not hasattr(self, 'fluidic_state_indicator') or not hasattr(self.fluidic_state_indicator, 'value_label'):
            return
            
        self.fluidic_state_indicator.value_label.setText(message)
        
        # Common padding and font for module status values
        base_style = "padding: 2px; border-radius: 3px; font-size: 14pt; font-weight: bold;"
        
        # Set background color based on state
        if state == "NOT_INITIALIZED":
            # Red
            self.fluidic_state_indicator.value_label.setStyleSheet(f"background-color: #ffcccc; {base_style}")
        elif state == "IDLE":
            # Yellow
            self.fluidic_state_indicator.value_label.setStyleSheet(f"background-color: #ffffcc; {base_style}")
        elif state == "CLEANING":
            # Blue
            self.fluidic_state_indicator.value_label.setStyleSheet(f"background-color: #cce5ff; {base_style}")
        elif state == "RUNNING_SAMPLE":
            # Green
            self.fluidic_state_indicator.value_label.setStyleSheet(f"background-color: #ccffcc; {base_style}")
        elif state == "BEADS":
            # Purple/Lavender
            self.fluidic_state_indicator.value_label.setStyleSheet(f"background-color: #f0e6ff; {base_style}")
        else:
            # Default
            self.fluidic_state_indicator.value_label.setStyleSheet(base_style)

    def _update_smr_status_display(self):
        """Update SMR status indicator based on saving state."""
        if not hasattr(self, 'smr_status_indicator') or not hasattr(self.smr_status_indicator, 'value_label'):
            return
            
        is_saving = getattr(self, 'is_saving', False)
        text = "Saving" if is_saving else "Not saving"
        color = "#ccffcc" if is_saving else "#ffcccc" # Green-ish or Red-ish
        
        self.smr_status_indicator.value_label.setText(text)
        self.smr_status_indicator.value_label.setStyleSheet(
            f"background-color: {color}; padding: 2px; border-radius: 3px; font-size: 14pt; font-weight: bold;"
        )

    def set_imaging_status(self, is_saving_enabled):
        """Update Imaging status indicator state (enabled/disabled)."""
        self.image_saving_enabled_internal = is_saving_enabled
        self._update_imaging_status_display()

    def set_roi_mode_status(self, is_enabled):
        """Update internal ROI mode state."""
        self.roi_mode_enabled_internal = is_enabled
        self._update_imaging_status_display()

    def _update_imaging_status_display(self):
        """Refresh Imaging status indicator based on enabling, ROI mode, and condition state."""
        if not hasattr(self, 'imaging_status_indicator') or not hasattr(self.imaging_status_indicator, 'value_label'):
            return
            
        # "Running a condition" is true if last_condition_name is set, not N/A, 
        # and doesn't contain exclusion keywords like "Cleaning", "Idle", "None", or "Calibration"
        exclusion_keywords = ["None", "Cleaning", "Idle", "Calibration"]
        is_running_condition = (
            self.last_condition_name is not None and 
            self.last_condition_name != "N/A" and
            not any(kw in self.last_condition_name for kw in exclusion_keywords)
        )
        
        # New logic per user request:
        # 1. Ready to save: ROI mode enabled AND no condition running.
        # 2. Set Cameras to ROI to save: ROI mode disabled AND condition running.
        # 3. Saving images: ROI mode enabled AND condition running AND saving enabled.
        # 4. Not saving: Default fallback.
        
        if self.roi_mode_enabled_internal and not is_running_condition:
            text = "Ready to save"
            color = "#ccffcc" # Green-ish
        elif not self.roi_mode_enabled_internal and is_running_condition:
            text = "Set Cameras to ROI to save"
            color = "#ffcccc" # Red-ish
        elif self.roi_mode_enabled_internal and is_running_condition and self.image_saving_enabled_internal:
            text = "Saving images"
            color = "#ccffcc" # Green-ish
        else:
            text = "Not saving"
            color = "#ffcccc" # Red-ish
        
        self.imaging_status_indicator.value_label.setText(text)
        self.imaging_status_indicator.value_label.setStyleSheet(
            f"background-color: {color}; padding: 2px; border-radius: 3px; font-size: 14pt; font-weight: bold;"
        )

    def set_pump_widget(self, pump_widget):
        """Set the pump widget reference for loading conditions."""
        self.pump_widget = pump_widget
        
        # Connect signals
        if self.pump_widget:
            try:
                self.pump_widget.fluidic_state_changed.connect(self.update_fluidic_state)
            except Exception as e:
                print(f"Error connecting pump signals: {e}")
    
    def _on_detect_peaks_toggled(self, checked):
        """Handle Detect Peaks checkbox toggle."""
        self.detect_peaks_enabled = checked
        # Removing plot clearing when disabled to ensure persistence during 'clean' or idle states
    
    def enable_peak_detection(self):
        """Enable peak detection (called from pump widget when Run Beads/Cells is pressed)."""
        if hasattr(self, 'detect_peaks_checkbox'):
            self.detect_peaks_checkbox.setChecked(True)
    
    def disable_peak_detection(self):
        """Disable peak detection (called from pump widget when clean is executed)."""
        if hasattr(self, 'detect_peaks_checkbox'):
            self.detect_peaks_checkbox.setChecked(False)
    
    def on_create_sample_clicked(self):
        """Handle Create Sample button click."""
        dialog = CreateSampleDialog(self)
        if dialog.exec():
            data = dialog.get_sample_data()
            self._create_new_sample(data)

    def _create_new_sample(self, data):
        """Create a new sample directory and files."""
        try:
            local_path = _get_local_data_path()
            sample_name = data['name']
            sample_path = os.path.join(local_path, sample_name)
            
            # Create directory
            os.makedirs(sample_path, exist_ok=True)
            
            # Create files
            # ActiveSystems.txt (empty)
            with open(os.path.join(sample_path, "ActiveSystems.txt"), 'w') as f:
                pass
                
            # CLIAtag.txt (FALSE)
            with open(os.path.join(sample_path, "CLIAtag.txt"), 'w') as f:
                f.write("FALSE")
                
            # conditions.txt
            with open(os.path.join(sample_path, "conditions.txt"), 'w') as f:
                f.write('\n'.join(data['conditions']))
                
            # sample_name.txt
            with open(os.path.join(sample_path, "sample_name.txt"), 'w') as f:
                f.write(sample_name)
                
            # Update last_manual_sample.txt in local root
            with open(os.path.join(local_path, "last_manual_sample.txt"), 'w') as f:
                f.write(sample_name)
                
            # Select the new sample
            self.selected_sample_path = sample_path
            
            # Update sample indicator
            if hasattr(self, 'sample_indicator'):
                self.sample_indicator.value_label.setText(sample_name)
                
            # Update button states
            self._update_select_sample_button(sample_name)
            self._update_start_saving_button()
            
            # Reset in-memory matched peaks and written times for new sample
            self.matched_peaks_list = []
            self.matched_peaks_df = pl.DataFrame()
            if hasattr(self, '_written_pair_times'):
                self._written_pair_times.clear()
            
            # Update Create Sample button to Edit conditions
            self.create_sample_button.setText("Edit conditions")
            self.create_sample_button.setStyleSheet(get_button_stylesheet("warning"))
            try:
                self.create_sample_button.clicked.disconnect()
            except:
                pass
            self.create_sample_button.clicked.connect(self.on_edit_conditions_clicked)
            
            # Notify pump widget to load conditions
            if self.pump_widget:
                self.pump_widget.load_conditions_from_sample_folder(sample_path)
                
            QMessageBox.information(self, "Success", f"Sample '{sample_name}' created successfully.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create sample: {e}")

    def on_edit_conditions_clicked(self):
        """Handle Edit conditions button click."""
        if not self.selected_sample_path:
            return
            
        conditions_file = os.path.join(self.selected_sample_path, "conditions.txt")
        if not os.path.exists(conditions_file):
            current_conditions = []
        else:
            with open(conditions_file, 'r') as f:
                current_conditions = [line.strip() for line in f.readlines() if line.strip()]
                
        dialog = EditConditionsDialog(current_conditions, self)
        if dialog.exec():
            new_conditions = dialog.get_new_conditions()
            if new_conditions:
                try:
                    with open(conditions_file, 'a') as f:
                        if current_conditions: # Add newline if file not empty
                            f.write('\n')
                        f.write('\n'.join(new_conditions))
                    
                    # Reload conditions in pump widget
                    if self.pump_widget:
                        self.pump_widget.load_conditions_from_sample_folder(self.selected_sample_path)
                        
                    QMessageBox.information(self, "Success", "Conditions updated successfully.")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to update conditions: {e}")

    def on_select_sample_clicked(self):
        """Handle Select Sample button click - launch sample selection dialog."""
        if self.selected_sample_path is not None:
            # Show confirmation dialog if sample is already selected
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Change Sample")
            
            # Build warning message
            warning_text = "Are you sure you want to change samples?"
            if self.is_saving:
                warning_text += "\n\nThis will stop saving the current data."
            
            msg_box.setText(warning_text)
            msg_box.setIcon(QMessageBox.Icon.Question)
            
            # Add custom buttons
            cancel_button = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            yes_button = msg_box.addButton("Yes", QMessageBox.ButtonRole.AcceptRole)
            msg_box.setDefaultButton(cancel_button)
            
            reply = msg_box.exec()
            
            if msg_box.clickedButton() == cancel_button:
                return
            
            # Stop saving if it's currently active
            if self.is_saving:
                self.stop_saving()
        
        # Launch sample selection dialog
        result = select_and_copy_sample()
        
        if result:
            # Store the selected sample path
            self.selected_sample_path = result
            # Extract folder name from path
            folder_name = os.path.basename(os.path.normpath(result))
            
            # Update sample indicator
            if hasattr(self, 'sample_indicator'):
                self.sample_indicator.value_label.setText(folder_name)
            
            # Load conditions from conditions.txt in the sample folder
            if self.pump_widget is not None:
                self.pump_widget.load_conditions_from_sample_folder(result)
            
            # Update button states
            self._update_select_sample_button(folder_name)
            self._update_start_saving_button()
            
            # Reset in-memory matched peaks and written times for new sample
            self.matched_peaks_list = []
            self.matched_peaks_df = pl.DataFrame()
            if hasattr(self, '_written_pair_times'):
                self._written_pair_times.clear()
            
            # Reset Create Sample button state if it exists
            if hasattr(self, 'create_sample_button'):
                # Check if the newly selected sample is a manually created one (contains '_mc')
                # If so, switch to "Edit conditions", otherwise revert to "Create Sample"
                if "_mc" in folder_name:
                    self.create_sample_button.setText("Edit conditions")
                    self.create_sample_button.setStyleSheet(get_button_stylesheet("warning"))
                    try:
                        self.create_sample_button.clicked.disconnect()
                    except:
                        pass
                    self.create_sample_button.clicked.connect(self.on_edit_conditions_clicked)
                else:
                    self.create_sample_button.setText("Create Sample")
                    self.create_sample_button.setStyleSheet(get_button_stylesheet("success"))
                    try:
                        self.create_sample_button.clicked.disconnect()
                    except:
                        pass
                    self.create_sample_button.clicked.connect(self.on_create_sample_clicked)
        else:
            # User cancelled or operation failed
            if self.selected_sample_path is None:
                # Only update if no sample was previously selected
                pass
    
    def on_start_saving_clicked(self):
        """Handle Start saving / Stop Saving button click."""
        if self.is_saving:
            # Currently saving - stop saving
            self.stop_saving()
        else:
            # Not saving - start saving
            if self.selected_sample_path is None:
                QMessageBox.warning(
                    self,
                    "No Sample Selected",
                    "Please select a sample before starting to save."
                )
                return
            
            self.is_saving = True
            # Reset final clean flag if we're starting a new recording
            self.is_final_clean = False
            # Update UI
            self.start_saving_button.setText("Stop saving")
            self._update_start_saving_button()
            self._update_smr_status_display()
            # Capture timestamp when saving starts using time.time() (epoch time)
            # This is used for UI display and experiment flags, which need epoch time
            # Note: For data_saver, we'll use the first packet's timestamp to match packet timestamp format
            self.saving_start_time = time.time()
            self.experiment_start_time_for_packets = None  # Will be set from first packet timestamp
            
            # Reset peak tracking when starting to save
            self.sample_peaks_total = 0
            self.condition_peaks_total = 0
            self.last_condition_name = None
            self.matched_count_at_condition_start = 0
            
            # Record current total matched count as offset for the NEW sample
            # This allows charts to persist historical data while the "Sample Peaks" counter starts at 0
            if hasattr(self, 'realtime_analyzer') and self.realtime_analyzer is not None:
                self.matched_count_at_sample_start, _ = self.realtime_analyzer.get_total_peak_counts()
            else:
                self.matched_count_at_sample_start = 0
            
            # Initialize CSV writing tracking (used in _write_matched_peaks_to_csv)
            self._written_pair_times = set()
            
            # Update displays
            if hasattr(self, 'sample_peaks_indicator') and hasattr(self.sample_peaks_indicator, 'value_label'):
                self.sample_peaks_indicator.value_label.setText("0")
            if hasattr(self, 'condition_peaks_indicator') and hasattr(self.condition_peaks_indicator, 'value_label'):
                self.condition_peaks_indicator.value_label.setText("0")
            
            # Generate experiment string: [system_name]_[YYYYMMDDHHMM]
            self.experiment_string = self._generate_experiment_string()
            
            # Store uncalibrated CSV path for post-hoc analysis
            csv_filename = f"{self.experiment_string}_uncalibrated_peaks.csv"
            self._last_uncalibrated_csv = os.path.join(self.selected_sample_path, csv_filename)
            
            # Create metadata file
            self._create_metadata_file()
            
            # Create SMR settings CSV file
            self._create_smr_settings_csv()
            
            # Create experiment flags TSV file
            self._create_experiment_flags_file()
            
            # Create DataSaver instance for UDP packet saving
            try:
                self.data_saver = DataSaver(
                    experiment_string=self.experiment_string,
                    sample_path=self.selected_sample_path
                )
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Error Starting Data Saving",
                    f"Failed to initialize data saver:\n{e}"
                )
                self.is_saving = False
                self.saving_start_time = None
                self.experiment_start_time_for_packets = None
                self.experiment_string = None
                self._update_smr_status_display()
                return
            
            self.start_saving_button.setText("Stop Saving")
            self._update_start_saving_button()
            # Start the timer to update elapsed time every second
            self.saving_time_timer.start(1000)  # Update every 1000ms (1 second)
            # Update immediately
            self._update_saving_elapsed_time()
            
            # Emit signal that saving started
            self.saving_started.emit(self.selected_sample_path, self.experiment_string)
    
    def stop_saving(self):
        """Centralized logic to stop saving, close files, and update UI."""
        if not self.is_saving:
            return
            
        self.is_saving = False
        
        # Flush and close data saver (NON-BLOCKING)
        if self.data_saver is not None:
            # Initiate non-blocking close (returns a threading.Event)
            finished_event = self.data_saver.close(wait=False)
            
            # Start a small background listener to emit the Qt signal when done
            def wait_for_saver(event):
                # Wait for up to 10 seconds (abundance of caution)
                is_finished = event.wait(timeout=10.0)
                if not is_finished:
                    print("Warning: DataSaver background close timed out after 10 seconds.")
                # Trigger the signal on the main thread
                # This ensures any connected slots (like closing the log) run safely
                QMetaObject.invokeMethod(self, "_on_data_saver_finished", Qt.QueuedConnection)
            
            # Use a reference to the saver to prevent premature garbage collection if needed,
            # though the DataSaver's own write thread should keep it alive.
            threading.Thread(target=wait_for_saver, args=(finished_event,), daemon=True).start()
            
            self.data_saver = None
        
        # Reset saving-related variables
        self.saving_start_time = None
        self.experiment_start_time_for_packets = None
        self.experiment_string = None
        
        # Update displays
        if hasattr(self, 'start_saving_button'):
            self.start_saving_button.setText("Start saving")
            self._update_start_saving_button()
            
        self._update_smr_status_display()
        
        # Stop the saving timer
        if hasattr(self, 'saving_time_timer'):
            self.saving_time_timer.stop()
            
        # Update elapsed time display to "N/A"
        if hasattr(self, 'saving_time_indicator'):
            self.saving_time_indicator.value_label.setText("N/A")
            
        # Emit signal that saving stopped (UI level)
        self.saving_stopped.emit()
        
    @Slot()
    def _on_data_saver_finished(self):
        """Callback when DataSaver has finished closing in the background."""
        print("SMRControlWidget: DataSaver finished in background.")
        self.data_saver_finished.emit()
        
        # Close console log if posthoc is not running AND we're not in final clean
        # (if posthoc IS running, the log will close when posthoc finishes)
        if (self.posthoc_worker is None or not self.posthoc_worker.isRunning()) and not self.is_final_clean:
            self.console_log_finished.emit()

    def on_sample_info_clicked(self):
        """Display info about the current sample including conditions and flags."""
        if not self.selected_sample_path:
            QMessageBox.warning(self, "No Sample Selected", "Please select a sample first.")
            return

        dialog = SampleInfoDialog(self.selected_sample_path, self.experiment_string, self)
        dialog.exec()

    
    def _update_saving_elapsed_time(self):
        """Update the saving elapsed time display in the status section."""
        if not hasattr(self, 'saving_time_indicator'):
            return
        
        if self.is_saving and self.saving_start_time is not None:
            # Calculate elapsed time
            elapsed_seconds = int(time.time() - self.saving_start_time)
            
            # Format as HH:MM:SS
            hours = elapsed_seconds // 3600
            minutes = (elapsed_seconds % 3600) // 60
            seconds = elapsed_seconds % 60
            
            if hours > 0:
                time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                time_str = f"{minutes:02d}:{seconds:02d}"
            
            self.saving_time_indicator.value_label.setText(time_str)
        else:
            # Not saving - show N/A
            self.saving_time_indicator.value_label.setText("N/A")
    
    def _update_current_condition(self, condition_name: str):
        """Update the current condition display in the status section.
        
        Args:
            condition_name: Name of the condition to display
        """
        # Check if condition has changed - if so, reset condition peaks counter
        if condition_name != self.last_condition_name:
            self.condition_peaks_total = 0
            self.last_condition_name = condition_name
            # Store the total matched_count at condition start (using total count, not filtered)
            if hasattr(self, 'realtime_analyzer') and self.realtime_analyzer is not None:
                total_matched_count, _ = self.realtime_analyzer.get_total_peak_counts()
                self.matched_count_at_condition_start = total_matched_count
            else:
                self.matched_count_at_condition_start = 0
            # Update condition peaks display
            if hasattr(self, 'condition_peaks_indicator') and hasattr(self.condition_peaks_indicator, 'value_label'):
                self.condition_peaks_indicator.value_label.setText("0")
        
        if hasattr(self, 'current_condition_indicator') and hasattr(self.current_condition_indicator, 'value_label'):
            self.current_condition_indicator.value_label.setText(condition_name)
            
        # Update Imaging status display as it depends on condition state
        self._update_imaging_status_display()
    
    def _generate_experiment_string(self) -> str:
        """Generate experiment string in format [system_name]_[YYYYMMDDHHMM].
        
        Returns:
            Experiment string combining system name and timestamp.
        """
        # Get system name from config
        try:
            system_name = get_system_name()
            if not system_name:
                system_name = "Unknown"
        except Exception:
            system_name = "Unknown"
        
        # Get current date and time in YYYYMMDDHHMM format
        now = datetime.now()
        date_time_str = now.strftime("%Y%m%d%H%M")
        
        # Combine: system_name_YYYYMMDDHHMM
        experiment_string = f"{system_name}_{date_time_str}"
        
        return experiment_string
    
    def _create_metadata_file(self):
        """Create metadata file in the selected sample directory.
        
        Creates a TSV-formatted metadata file following the format in
        references/example_metadata.txt.
        """
        if not self.selected_sample_path:
            print("Warning: Cannot create metadata file - no sample path selected")
            return
        
        if not self.experiment_string:
            print("Warning: Cannot create metadata file - no experiment string generated")
            return
        
        try:
            # Get system name and other info
            system_name = get_system_name() or "Unknown"
            
            # Get device/chip name if available
            chip_name = "N/A"
            if hasattr(self, 'chip_name_indicator') and self.chip_name_indicator:
                chip_name = self.chip_name_indicator.value_label.text() or "N/A"
            
            # Get operator if available
            operator = "N/A"
            if hasattr(self, 'operator') and self.operator:
                operator = self.operator
            elif hasattr(self, 'operator_indicator') and self.operator_indicator:
                operator = self.operator_indicator.value_label.text() or "N/A"
            
            # Get current datetime for experiment_datetime
            now = datetime.now()
            experiment_datetime = now.strftime("%Y-%m-%d %H:%M")
            
            # Get sample folder name
            sample_name = os.path.basename(os.path.normpath(self.selected_sample_path))
            
            # Get cic_rate and pll_datarate_decimation from SMR settings widget
            cic_rate = "N/A"
            pll_datarate_decimation = "N/A"
            try:
                widget = self._ensure_smr_settings_widget()
                if widget:
                    cic_rate = str(widget.cic_rate.value())
                    pll_datarate_decimation = widget.pll_datarate_decimation.currentText()
            except Exception as e:
                print(f"Warning: Could not get SMR settings values: {e}")
            
            # Create metadata file path
            metadata_filename = f"{self.experiment_string}_metadata.txt"
            metadata_filepath = os.path.join(self.selected_sample_path, metadata_filename)
            
            # Create TSV content following example format
            metadata_lines = [
                f"Sample_ID\t{sample_name}",
                f"System_ID\t{system_name}",
                f"Device_ID\t{chip_name}",
                f"Control_Software\tPaella",
                f"Control_Software_Version\t0.0.1",
                f"Experiment_datetime\t{experiment_datetime}",
                f"Experiment comments\t",
                f"cic_rate\t{cic_rate}",
                f"pll_datarate_decimation\t{pll_datarate_decimation}",
                f"user\t{operator}",
            ]
            
            # Write metadata file
            with open(metadata_filepath, 'w', encoding='utf-8') as f:
                f.write('\n'.join(metadata_lines) + '\n')
            
            print(f"Created metadata file: {metadata_filepath}")
            
        except Exception as e:
            print(f"Error creating metadata file: {e}")
            import traceback
            traceback.print_exc()
    
    def _create_smr_settings_csv(self):
        """Create SMR settings CSV file in the selected sample directory.
        
        Creates a CSV file with three entries:
        1. Most recent type='sweep' entry from current chip's settings
        2. Most recent type='setDelays' entry from current chip's settings
        3. Current SMR settings with type='current'
        """
        if not self.selected_sample_path:
            print("Warning: Cannot create SMR settings CSV - no sample path selected")
            return
        
        if not self.experiment_string:
            print("Warning: Cannot create SMR settings CSV - no experiment string generated")
            return
        
        try:
            import csv
            import helper_functions.SMR_settings_io as smr_settings_io
            
            # Get chip name
            chip_name = "N/A"
            if hasattr(self, 'chip_name_indicator') and self.chip_name_indicator:
                chip_name = self.chip_name_indicator.value_label.text() or "N/A"
            
            if chip_name == "N/A":
                print("Warning: Cannot create SMR settings CSV - chip name not available")
                return
            
            # Read all settings for this chip
            all_settings = read_smr_settings(chip_name)
            
            # Find most recent sweep entry
            sweep_settings = [
                s for s in all_settings 
                if s.get("settings_type", "").strip().lower() == "sweep"
            ]
            most_recent_sweep = sweep_settings[-1] if sweep_settings else None
            
            # Find most recent setDelays entry
            setdelays_settings = [
                s for s in all_settings 
                if s.get("settings_type", "").strip() == "setDelays"
            ]
            most_recent_setdelays = setdelays_settings[-1] if setdelays_settings else None
            
            # Get current SMR settings from widget
            current_settings = None
            try:
                widget = self._ensure_smr_settings_widget()
                if widget:
                    fpga_params = self._get_fpga_parameters_from_widget(widget)
                    # Get substrate bias
                    substrate_bias = 0.0
                    if hasattr(self, 'substrate_bias_control'):
                        substrate_bias = self.substrate_bias_control.get_value()
                    
                    # Get system name and operator
                    system_name = get_system_name() or "Unknown"
                    operator = "N/A"
                    if hasattr(self, 'operator') and self.operator:
                        operator = self.operator
                    elif hasattr(self, 'operator_indicator') and self.operator_indicator:
                        operator = self.operator_indicator.value_label.text() or "N/A"
                    
                    # Get current date and time
                    now = datetime.now()
                    date_str = now.strftime("%Y-%m-%d")
                    time_str = now.strftime("%H:%M:%S")
                    
                    # Build current settings dictionary
                    current_settings = {
                        "date": date_str,
                        "time": time_str,
                        "chip_name": chip_name,
                        "system_name": system_name,
                        "operator": operator,
                        "settings_type": "current",
                        "substrate_bias": str(substrate_bias),
                    }
                    
                    # Add FPGA parameters
                    for param_name in [
                        "smr_driver_id", "Run", "Enable_AGC", "Send_data_to_pc",
                        "Run_NCO_at_fixed_freq", "Impulse", "Input_source",
                        "Signal_of_interest", "DAC_A_output", "DAC_B_output",
                        "PLL_datarate_decimation", "Frequency", "Minimum_frequency",
                        "Maximum_frequency", "CIC_rate", "CIC_bit_shift",
                        "PLL_delay", "PLL_drive_amplitude", "Feedback_delay",
                        "Feedback_gain", "Resonator_Q", "Loop_bandwidth", "Loop_order"
                    ]:
                        value = fpga_params.get(param_name, "")
                        if isinstance(value, bool):
                            current_settings[param_name] = "True" if value else "False"
                        elif isinstance(value, (int, float)):
                            current_settings[param_name] = str(value)
                        else:
                            current_settings[param_name] = str(value) if value is not None else ""
            except Exception as e:
                print(f"Warning: Could not get current SMR settings: {e}")
            
            # Create CSV file path
            csv_filename = f"{self.experiment_string}_smr_settings.csv"
            csv_filepath = os.path.join(self.selected_sample_path, csv_filename)
            
            # Get column headers
            headers = smr_settings_io._get_all_column_headers()
            
            # Write CSV file
            with open(csv_filepath, 'w', encoding='utf-8', newline='') as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=headers)
                writer.writeheader()
                
                # Write most recent sweep entry if available
                if most_recent_sweep:
                    writer.writerow(most_recent_sweep)
                
                # Write most recent setDelays entry if available
                if most_recent_setdelays:
                    writer.writerow(most_recent_setdelays)
                
                # Write current settings if available
                if current_settings:
                    writer.writerow(current_settings)
            
            print(f"Created SMR settings CSV file: {csv_filepath}")
            
        except Exception as e:
            print(f"Error creating SMR settings CSV file: {e}")
            import traceback
            traceback.print_exc()
    
    def _create_experiment_flags_file(self):
        """Create experiment flags TSV file in the selected sample directory.
        
        Creates a TSV file with two columns: 'Datetime' and 'Flag ID'.
        The file is initialized with just the header row.
        """
        if not self.selected_sample_path:
            print("Warning: Cannot create experiment flags file - no sample path selected")
            return
        
        if not self.experiment_string:
            print("Warning: Cannot create experiment flags file - no experiment string generated")
            return
        
        try:
            # Create TSV file path
            flags_filename = f"{self.experiment_string}_experiment_flags.txt"
            flags_filepath = os.path.join(self.selected_sample_path, flags_filename)
            
            # Create TSV content with header row
            header_line = "Datetime\tFlag ID"
            
            # Write TSV file with just the header
            with open(flags_filepath, 'w', encoding='utf-8') as f:
                f.write(header_line + '\n')
            
            print(f"Created experiment flags file: {flags_filepath}")
            
        except Exception as e:
            print(f"Error creating experiment flags file: {e}")
            import traceback
            traceback.print_exc()
    
    def _append_experiment_flag(self, condition_name: str):
        """Append a line to experiment_flags.txt with elapsed time and condition name.
        
        Args:
            condition_name: Name of the condition (e.g., "Calibration" or condition from dropdown)
        """
        if not self.selected_sample_path:
            print("Warning: Cannot append experiment flag - no sample path selected")
            return
        
        if not self.experiment_string:
            print("Warning: Cannot append experiment flag - no experiment string generated")
            return
        
        if not self.is_saving or self.saving_start_time is None:
            print("Warning: Cannot append experiment flag - not currently saving")
            return
        
        try:
            # Create TSV file path
            flags_filename = f"{self.experiment_string}_experiment_flags.txt"
            flags_filepath = os.path.join(self.selected_sample_path, flags_filename)
            
            # Calculate elapsed time in seconds with 6 decimal places
            elapsed_time = time.time() - self.saving_start_time
            elapsed_time_str = f"{elapsed_time:.6f}"
            
            # Check if file exists, create with header if it doesn't
            file_exists = os.path.exists(flags_filepath)
            
            # Append line to file
            with open(flags_filepath, 'a', encoding='utf-8') as f:
                # Write header if file is new
                if not file_exists:
                    f.write("elapsed_time\tcondition_name\n")
                # Write data line: elapsed_time (tab) condition_name
                f.write(f"{elapsed_time_str}\t{condition_name}\n")
            
            print(f"Appended experiment flag: {elapsed_time_str}\t{condition_name}")
            
        except Exception as e:
            print(f"Error appending experiment flag: {e}")
            import traceback
            traceback.print_exc()
    
    
    def _update_push_settings_button_state(self):
        """Update Push settings to FPGA button enabled state based on TCP connection status."""
        if hasattr(self, 'push_settings_button'):
            # Enable if TCP connection is valid (regardless of Connect to FPGA button state)
            is_tcp_connected = self.fpga_command_queue.is_connected()
            self.push_settings_button.setEnabled(is_tcp_connected)
        # Also update button in SMR settings window if it exists
        if hasattr(self, 'smr_settings_window') and self.smr_settings_window is not None:
            if hasattr(self.smr_settings_window, 'push_settings_button'):
                is_tcp_connected = self.fpga_command_queue.is_connected()
                self.smr_settings_window.push_settings_button.setEnabled(is_tcp_connected)
    
    def _connect_tcp_status_signal(self):
        """Connect to CommandWorker connection_status_changed signal to auto-update status."""
        def _ensure_worker_and_connect():
            """Ensure worker exists and connect to its signal."""
            try:
                self.fpga_command_queue._ensure_worker()
                if self.fpga_command_queue.worker is not None:
                    # Only disconnect if we've previously connected to avoid RuntimeWarning
                    if self._tcp_status_signal_connected:
                        try:
                            self.fpga_command_queue.worker.connection_status_changed.disconnect(
                                self._on_tcp_connection_status_changed
                            )
                        except Exception:
                            pass  # Connection may have been broken
                        self._tcp_status_signal_connected = False
                    
                    # Connect signal to update status automatically
                    self.fpga_command_queue.worker.connection_status_changed.connect(
                        self._on_tcp_connection_status_changed
                    )
                    self._tcp_status_signal_connected = True
            except Exception as e:
                # If connection fails, periodic sync will catch it
                import sys
                print(f"Warning: Could not connect to TCP status signal: {e}", file=sys.stderr, flush=True)
        
        # Use QTimer to ensure this runs in main thread (worker might not exist yet)
        # Try multiple times with delays to ensure worker is ready
        QTimer.singleShot(0, _ensure_worker_and_connect)
        QTimer.singleShot(100, _ensure_worker_and_connect)  # Retry after 100ms
        QTimer.singleShot(500, _ensure_worker_and_connect)  # Retry after 500ms
    
    def _on_tcp_connection_status_changed(self, is_connected: bool):
        """Handle TCP connection status change from CommandWorker signal."""
        if is_connected:
            self.tcp_connection_status = "connected"
        else:
            self.tcp_connection_status = "disconnected"
        
        # Update indicators and buttons
        self._update_connection_indicators()
        self._update_push_settings_button_state()
    
    def _sync_tcp_connection_status(self):
        """Sync tcp_connection_status with actual connection state."""
        is_connected = self.fpga_command_queue.is_connected()
        if is_connected and self.tcp_connection_status != "connected":
            self.tcp_connection_status = "connected"
            self._update_connection_indicators()
            self._update_push_settings_button_state()
        elif not is_connected and self.tcp_connection_status == "connected":
            self.tcp_connection_status = "disconnected"
            self._update_connection_indicators()
            self._update_push_settings_button_state()
    
    def _create_settings_column(self):
        """Create the middle column with SMR settings and push settings button."""
        column_widget = QWidget()
        column_layout = QVBoxLayout(column_widget)
        column_layout.setContentsMargins(5, 5, 5, 5)
        
        # Buttons row: Initialize SMR, SMR Settings, and Push settings to FPGA
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(10)
        
        # Initialize SMR button
        initialize_smr_button = create_button("Initialize SMR", "success", font_size="12pt", padding="10px")
        initialize_smr_button.clicked.connect(self.on_initialize_smr_clicked)
        buttons_row.addWidget(initialize_smr_button, 1)  # Equal stretch factor
        self.initialize_smr_button = initialize_smr_button
        
        # Set Delays button
        set_delays_button = create_button("Set Delays", "blue", font_size="12pt", padding="10px")
        set_delays_button.clicked.connect(self.on_set_delays_clicked)
        # Initial styling will be set by _update_set_delays_button()
        buttons_row.addWidget(set_delays_button, 1)  # Equal stretch factor
        self.set_delays_button = set_delays_button
        
        # SMR Settings button
        smr_settings_button = create_button("SMR settings", "blue", font_size="12pt", padding="10px")
        smr_settings_button.clicked.connect(self.on_smr_settings_clicked)
        buttons_row.addWidget(smr_settings_button, 1)  # Equal stretch factor
        self.smr_settings_button = smr_settings_button
        
        column_layout.addLayout(buttons_row)
        
        # Controls row: Run, Bias, Delay, Drive all on the same line
        controls_row = QWidget()
        controls_row_layout = QHBoxLayout(controls_row)
        controls_row_layout.setContentsMargins(0, 0, 0, 0)
        controls_row_layout.setSpacing(15)
        
        # Run checkbox
        run_checkbox = QCheckBox("Run")
        run_checkbox.setStyleSheet("""
            QCheckBox {
                font-size: 11pt;
                padding: 5px;
            }
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border: 2px solid #999;
                border-radius: 3px;
                background-color: #555555;
            }
            QCheckBox::indicator:checked {
                background-color: #00FF00;
                border: 2px solid #00CC00;
            }
            QCheckBox::indicator:unchecked {
                background-color: #555555;
                border: 2px solid #444444;
            }
        """)
        run_checkbox.toggled.connect(self.on_quick_run_changed)
        controls_row_layout.addWidget(run_checkbox)
        self.quick_run_checkbox = run_checkbox
        
        # Substrate bias control with shortened label - wrapped in gray container
        bias_container = QWidget()
        bias_container.setStyleSheet("background-color: #f0f0f0; border: 1px solid #ccc; border-radius: 5px; padding: 5px;")
        bias_container_layout = QHBoxLayout(bias_container)
        bias_container_layout.setContentsMargins(5, 5, 5, 5)
        bias_container_layout.setSpacing(5)
        bias_label = QLabel("Bias (V):")
        bias_label.setStyleSheet("font-size: 10pt; padding-top: 5px;")
        bias_container_layout.addWidget(bias_label)
        substrate_bias_control = self._create_increment_control(
            min_val=0.0, max_val=5.0, initial_val=3.0, step=0.5, is_int=False, arrow_key_step=0.5
        )
        substrate_bias_control._value_changed_callback = lambda val: self.on_substrate_bias_changed(val)
        bias_container_layout.addWidget(substrate_bias_control)
        controls_row_layout.addWidget(bias_container)
        self.substrate_bias_control = substrate_bias_control
        
        # Set initial substrate bias voltage
        self._set_substrate_bias_voltage(3.0)
        
        # PLL delay control with shortened label - wrapped in gray container
        delay_container = QWidget()
        delay_container.setStyleSheet("background-color: #f0f0f0; border: 1px solid #ccc; border-radius: 5px; padding: 5px;")
        delay_container_layout = QHBoxLayout(delay_container)
        delay_container_layout.setContentsMargins(5, 5, 5, 5)
        delay_container_layout.setSpacing(5)
        delay_label = QLabel("Delay:")
        delay_label.setStyleSheet("font-size: 10pt; padding-top: 5px;")
        delay_container_layout.addWidget(delay_label)
        pll_delay_control = self._create_increment_control(
            min_val=0.0, max_val=1.0, initial_val=0.0, step=0.01, suffix="", is_int=False, arrow_key_step=0.01
        )
        pll_delay_control._value_changed_callback = lambda val: self.on_quick_pll_delay_changed(val)
        delay_container_layout.addWidget(pll_delay_control)
        controls_row_layout.addWidget(delay_container)
        self.quick_pll_delay_control = pll_delay_control
        
        # PLL drive amplitude control with shortened label - wrapped in gray container
        drive_container = QWidget()
        drive_container.setStyleSheet("background-color: #f0f0f0; border: 1px solid #ccc; border-radius: 5px; padding: 5px;")
        drive_container_layout = QHBoxLayout(drive_container)
        drive_container_layout.setContentsMargins(5, 5, 5, 5)
        drive_container_layout.setSpacing(5)
        drive_label = QLabel("Drive:")
        drive_label.setStyleSheet("font-size: 10pt; padding-top: 5px;")
        drive_container_layout.addWidget(drive_label)
        pll_drive_amplitude_control = self._create_increment_control(
            min_val=0.0, max_val=1.0, initial_val=0.1, step=0.01, suffix="", is_int=False, arrow_key_step=0.01
        )
        pll_drive_amplitude_control._value_changed_callback = lambda val: self.on_quick_pll_drive_amplitude_changed(val)
        drive_container_layout.addWidget(pll_drive_amplitude_control)
        controls_row_layout.addWidget(drive_container)
        self.quick_pll_drive_amplitude_control = pll_drive_amplitude_control
        
        controls_row_layout.addStretch()
        column_layout.addWidget(controls_row)
        
        # Real-time Analysis Plots Section
        analysis_plots_group = QGroupBox("Real-time Analysis")
        analysis_plots_layout = QVBoxLayout()
        
        # Create tabbed widget for plots
        plots_tabs = QTabWidget()
        
        if PYQTGRAPH_AVAILABLE:
            # Tab 1: Time vs Peak Width
            width_tab = QWidget()
            width_tab_layout = QHBoxLayout(width_tab)
            width_tab_layout.setContentsMargins(0, 0, 0, 0)
            width_tab_layout.setSpacing(0)
            
            # Plot widget
            width_plot = pg.PlotWidget(axisItems={'bottom': HHMMTimeAxisItem(orientation='bottom')})
            width_plot.setLabel('left', 'Peak Width', units='ms')
            width_plot.setLabel('bottom', 'Time')
            width_plot.setTitle('Time vs Peak Width')
            width_plot.setBackground('#202124')
            width_plot.showGrid(x=True, y=True, alpha=0.3)
            # Enable auto-range on both axes by default
            view_box = width_plot.getPlotItem().getViewBox()
            view_box.enableAutoRange(axis='x', enable=True)
            view_box.enableAutoRange(axis='y', enable=True)
            # Use PlotDataItem for efficient rendering (matches frequency plot style)
            width_plot_item = pg.PlotDataItem(
                [], [],
                pen=None,  # No lines
                symbol='o',
                symbolSize=2,  # Smaller for performance
                symbolBrush='#00FFFF',  # Cyan
                symbolPen=None,
                antialias=False,  # Disable for performance
                connect='pairs'  # No lines between points
            )
            width_plot.addItem(width_plot_item)
            
            # Add plot directly to layout (takes full width)
            width_tab_layout.addWidget(width_plot)
            
            plots_tabs.addTab(width_tab, "Peak Width")
            self.peak_width_plot = width_plot
            self.peak_width_plot_item = width_plot_item
            
            # Controls removed as per simplification request
            self.peak_width_y_min_spin = None
            self.peak_width_y_max_spin = None
            self.peak_width_time_spin = None
            self.peak_width_auto_y = None
            
            # Tab 2: Time vs Approximate Mass
            mass_tab = QWidget()
            mass_tab_layout = QHBoxLayout(mass_tab)
            mass_tab_layout.setContentsMargins(0, 0, 0, 0)
            mass_tab_layout.setSpacing(0)
            
            # Plot widget
            mass_plot = pg.PlotWidget(axisItems={'bottom': HHMMTimeAxisItem(orientation='bottom')})
            mass_plot.setLabel('left', 'Mass', units='pg')
            mass_plot.setLabel('bottom', 'Time')
            mass_plot.setTitle('Time vs Approximate Mass')
            mass_plot.setBackground('#202124')
            mass_plot.showGrid(x=True, y=True, alpha=0.3)
            # Enable auto-range on both axes by default
            view_box = mass_plot.getPlotItem().getViewBox()
            view_box.enableAutoRange(axis='x', enable=True)
            view_box.enableAutoRange(axis='y', enable=True)
            # Use PlotDataItem for efficient rendering (matches frequency plot style)
            mass_plot_item = pg.PlotDataItem(
                [], [],
                pen=None,  # No lines
                symbol='o',
                symbolSize=2,  # Smaller for performance
                symbolBrush='#00FF00',  # Green
                symbolPen=None,
                antialias=False,  # Disable for performance
                connect='pairs'  # No lines between points
            )
            mass_plot.addItem(mass_plot_item)
            
            # Add plot directly to layout (takes full width)
            mass_tab_layout.addWidget(mass_plot)
            
            plots_tabs.addTab(mass_tab, "Mass")
            self.peak_mass_plot = mass_plot
            self.peak_mass_plot_item = mass_plot_item
            
            # Controls removed as per simplification request
            self.peak_mass_y_min_spin = None
            self.peak_mass_y_max_spin = None
            self.peak_mass_time_spin = None
            self.peak_mass_auto_y = None
            
            # Tab 3: Throughput (cells/hour)
            throughput_tab = QWidget()
            throughput_tab_layout = QVBoxLayout(throughput_tab)
            throughput_tab_layout.setContentsMargins(0, 0, 0, 0)
            
            # Create horizontal layout for plot and controls
            throughput_plot_layout = QHBoxLayout()
            throughput_plot_layout.setContentsMargins(0, 0, 0, 0)
            throughput_plot_layout.setSpacing(0)
            
            # Plot widget
            throughput_plot = pg.PlotWidget(axisItems={'bottom': HHMMTimeAxisItem(orientation='bottom')})
            throughput_plot.setLabel('left', 'Throughput', units='cells/hour')
            throughput_plot.setLabel('bottom', 'Time')
            throughput_plot.setTitle('Throughput vs Time (15s windows)')
            throughput_plot.setBackground('#202124')
            throughput_plot.showGrid(x=True, y=True, alpha=0.3)
            # Enable auto-range on both axes by default
            view_box = throughput_plot.getPlotItem().getViewBox()
            view_box.enableAutoRange(axis='x', enable=True)
            view_box.enableAutoRange(axis='y', enable=True)
            throughput_plot_item = pg.PlotDataItem([], [], pen='#FF00FF', symbol='o', symbolSize=5, symbolBrush='#FF00FF')
            throughput_plot.addItem(throughput_plot_item)
            
            # Add plot directly to layout (takes full width)
            throughput_tab_layout.addWidget(throughput_plot)
            
            plots_tabs.addTab(throughput_tab, "Throughput")
            self.throughput_plot = throughput_plot
            self.throughput_plot_item = throughput_plot_item
            
            # Controls removed as per simplification request
            self.throughput_y_min_spin = None
            self.throughput_y_max_spin = None
            self.throughput_time_spin = None
            self.throughput_auto_y = None
            
            # Tab 4: Condition comparison (Violin Plot)
            comparison_tab = QWidget()
            comparison_tab_layout = QHBoxLayout(comparison_tab)
            comparison_tab_layout.setContentsMargins(0, 0, 0, 0)
            comparison_tab_layout.setSpacing(0)
            
            # Create splitter for plot and controls
            comparison_splitter = QSplitter(Qt.Orientation.Horizontal)
            
            # Plot widget
            comparison_plot = pg.PlotWidget()
            comparison_plot.setLabel('left', 'Approximate Mass', units='pg')
            comparison_plot.setLabel('bottom', 'Condition')
            comparison_plot.setTitle('Condition Comparison')
            comparison_plot.setBackground('#202124')
            comparison_plot.showGrid(x=False, y=True, alpha=0.3)
            comparison_splitter.addWidget(comparison_plot)
            
            # Controls panel
            comparison_controls, comparison_y_min_spin, comparison_y_max_spin, comparison_auto_y, comparison_y_axis_combo = self._create_comparison_plot_panel()
            comparison_splitter.addWidget(comparison_controls)
            comparison_splitter.setStretchFactor(0, 3)  # Plot takes 3x space
            comparison_splitter.setStretchFactor(1, 1)   # Controls take 1x space
            
            comparison_tab_layout.addWidget(comparison_splitter)
            
            plots_tabs.addTab(comparison_tab, "Condition comparison")
            self.comparison_plot = comparison_plot
            self.comparison_y_min_spin = comparison_y_min_spin
            self.comparison_y_max_spin = comparison_y_max_spin
            self.comparison_auto_y = comparison_auto_y
            self.comparison_y_axis_combo = comparison_y_axis_combo
            
            # Connect tab change signal to update comparison plot when selected
            plots_tabs.currentChanged.connect(lambda index: self._on_plot_tab_changed(index, plots_tabs))
        else:
            no_plot_label = QLabel("pyqtgraph not available")
            plots_tabs.addTab(no_plot_label, "Plots")
            self.peak_width_plot = None
            self.peak_width_plot_item = None
            self.peak_mass_plot = None
            self.peak_mass_plot_item = None
            self.peak_width_y_min_spin = None
            self.peak_width_y_max_spin = None
            self.peak_width_time_spin = None
            self.peak_width_auto_y = None
            self.peak_mass_y_min_spin = None
            self.peak_mass_y_max_spin = None
            self.peak_mass_time_spin = None
            self.peak_mass_auto_y = None
            self.throughput_plot = None
            self.throughput_plot_item = None
            self.throughput_y_min_spin = None
            self.throughput_y_max_spin = None
            self.throughput_time_spin = None
            self.throughput_auto_y = None
            self.comparison_plot = None
            self.comparison_y_min_spin = None
            self.comparison_y_max_spin = None
            self.comparison_auto_y = None
            self.comparison_y_axis_combo = None
        
        analysis_plots_layout.addWidget(plots_tabs)
        analysis_plots_group.setLayout(analysis_plots_layout)
        column_layout.addWidget(analysis_plots_group)
        
        column_layout.addStretch()
        
        return column_widget
    
    def _create_plot_controls_panel(self, y_min_default, y_max_default, time_window_default_minutes=10):
        """
        Create a control panel for plot settings.
        
        Args:
            y_min_default: Default minimum y value
            y_max_default: Default maximum y value
            time_window_default_minutes: Default time window in minutes
            
        Returns:
            Tuple of (control_widget, y_min_spin, y_max_spin, time_window_spin, auto_y_checkbox)
        """
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        controls_layout.setContentsMargins(10, 10, 10, 10)
        controls_layout.setSpacing(10)
        
        # Title
        title_label = QLabel("Plot Controls")
        title_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        controls_layout.addWidget(title_label)
        
        # Time window control
        time_group = QGroupBox("Time Window")
        time_layout = QFormLayout()
        time_window_spin = QDoubleSpinBox()
        time_window_spin.setMinimum(0.1)
        time_window_spin.setMaximum(1440.0)  # Up to 24 hours
        time_window_spin.setValue(time_window_default_minutes)
        time_window_spin.setSuffix(" min")
        time_window_spin.setDecimals(1)
        time_window_spin.setSingleStep(1.0)
        time_layout.addRow("Display last:", time_window_spin)
        time_group.setLayout(time_layout)
        controls_layout.addWidget(time_group)
        
        # Y-axis bounds control
        y_bounds_group = QGroupBox("Y-Axis Bounds")
        y_bounds_layout = QFormLayout()
        
        # Auto/manual toggle
        auto_y_checkbox = QCheckBox("Auto Y-Range")
        auto_y_checkbox.setChecked(True)
        style_checkbox(auto_y_checkbox)
        
        # Style the checkbox: solid green when checked, dark gray when unchecked
        def update_checkbox_style():
            if auto_y_checkbox.isChecked():
                auto_y_checkbox.setStyleSheet("""
                    QCheckBox {
                        font-weight: bold;
                    }
                    QCheckBox::indicator {
                        width: 18px;
                        height: 18px;
                        border-radius: 3px;
                        border: 2px solid #00CC00;
                        background-color: #00FF00;
                    }
                    QCheckBox::indicator:checked {
                        background-color: #00FF00;
                        border: 2px solid #00CC00;
                    }
                    QCheckBox::indicator:unchecked {
                        background-color: #00FF00;
                        border: 2px solid #00CC00;
                    }
                """)
            else:
                auto_y_checkbox.setStyleSheet("""
                    QCheckBox {
                        font-weight: bold;
                    }
                    QCheckBox::indicator {
                        width: 18px;
                        height: 18px;
                        border-radius: 3px;
                        border: 2px solid #555555;
                        background-color: #404040;
                    }
                    QCheckBox::indicator:checked {
                        background-color: #404040;
                        border: 2px solid #555555;
                    }
                    QCheckBox::indicator:unchecked {
                        background-color: #404040;
                        border: 2px solid #555555;
                    }
                """)
        
        # Update style initially and when toggled
        update_checkbox_style()
        auto_y_checkbox.toggled.connect(update_checkbox_style)
        
        y_bounds_layout.addRow("", auto_y_checkbox)
        
        # Min Y value
        y_min_spin = QDoubleSpinBox()
        y_min_spin.setMinimum(-1e6)
        y_min_spin.setMaximum(1e6)
        y_min_spin.setValue(y_min_default)
        y_min_spin.setDecimals(2)
        y_min_spin.setSingleStep(1.0)
        style_input_field(y_min_spin)
        y_bounds_layout.addRow("Min Y:", y_min_spin)
        
        # Max Y value
        y_max_spin = QDoubleSpinBox()
        style_input_field(y_max_spin)
        y_max_spin.setMinimum(-1e6)
        y_max_spin.setMaximum(1e6)
        y_max_spin.setValue(y_max_default)
        y_max_spin.setDecimals(2)
        y_max_spin.setSingleStep(1.0)
        y_bounds_layout.addRow("Max Y:", y_max_spin)
        
        # Enable/disable manual controls based on auto checkbox
        def update_y_controls_enabled():
            enabled = not auto_y_checkbox.isChecked()
            y_min_spin.setEnabled(enabled)
            y_max_spin.setEnabled(enabled)
        
        auto_y_checkbox.toggled.connect(update_y_controls_enabled)
        update_y_controls_enabled()  # Initial state
        
        y_bounds_group.setLayout(y_bounds_layout)
        controls_layout.addWidget(y_bounds_group)
        
        controls_layout.addStretch()
        
        return controls_widget, y_min_spin, y_max_spin, time_window_spin, auto_y_checkbox
    
    def _create_comparison_plot_panel(self):
        """
        Create a control panel for the comparison plot.
        
        Returns:
            Tuple of (control_widget, y_min_spin, y_max_spin, auto_y_checkbox, y_axis_combo)
        """
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        controls_layout.setContentsMargins(10, 10, 10, 10)
        controls_layout.setSpacing(10)
        
        # Title
        title_label = QLabel("Comparison Controls")
        title_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        controls_layout.addWidget(title_label)
        
        # Y-Axis Selection
        y_axis_group = QGroupBox("Y-Axis Feature")
        y_axis_layout = QVBoxLayout()
        y_axis_combo = QComboBox()
        y_axis_combo.addItems([
            "approximate_mass_pg",
            "peak_width_ms",
            "baseline_hz",
            "peak1_delta_hz",
            "peak2_delta_hz",
            "height_diff_percent",
            "peak_time"
        ])
        style_input_field(y_axis_combo)
        y_axis_combo.currentIndexChanged.connect(self._update_comparison_plot)
        y_axis_layout.addWidget(y_axis_combo)
        y_axis_group.setLayout(y_axis_layout)
        controls_layout.addWidget(y_axis_group)
        
        # Y-axis bounds control
        y_bounds_group = QGroupBox("Y-Axis Bounds")
        y_bounds_layout = QFormLayout()
        
        # Auto/manual toggle
        auto_y_checkbox = QCheckBox("Auto Y-Range")
        auto_y_checkbox.setChecked(True)
        style_checkbox(auto_y_checkbox)
        
        # Style the checkbox: solid green when checked, dark gray when unchecked
        def update_checkbox_style():
            if auto_y_checkbox.isChecked():
                auto_y_checkbox.setStyleSheet("""
                    QCheckBox { font-weight: bold; }
                    QCheckBox::indicator {
                        width: 18px; height: 18px; border-radius: 3px;
                        border: 2px solid #00CC00; background-color: #00FF00;
                    }
                    QCheckBox::indicator:checked { background-color: #00FF00; border: 2px solid #00CC00; }
                """)
            else:
                auto_y_checkbox.setStyleSheet("""
                    QCheckBox { font-weight: bold; }
                    QCheckBox::indicator {
                        width: 18px; height: 18px; border-radius: 3px;
                        border: 2px solid #555555; background-color: #404040;
                    }
                """)
        
        update_checkbox_style()
        auto_y_checkbox.toggled.connect(update_checkbox_style)
        auto_y_checkbox.toggled.connect(self._update_comparison_plot)
        
        y_bounds_layout.addRow("", auto_y_checkbox)
        
        # Min Y value
        y_min_spin = QDoubleSpinBox()
        y_min_spin.setMinimum(-1e6)
        y_min_spin.setMaximum(1e6)
        y_min_spin.setValue(0.0)
        y_min_spin.setDecimals(2)
        y_min_spin.setSingleStep(1.0)
        style_input_field(y_min_spin)
        y_min_spin.valueChanged.connect(self._update_comparison_plot)
        y_bounds_layout.addRow("Min Y:", y_min_spin)
        
        # Max Y value
        y_max_spin = QDoubleSpinBox()
        style_input_field(y_max_spin)
        y_max_spin.setMinimum(-1e6)
        y_max_spin.setMaximum(1e6)
        y_max_spin.setValue(100.0)
        y_max_spin.setDecimals(2)
        y_max_spin.setSingleStep(1.0)
        y_max_spin.valueChanged.connect(self._update_comparison_plot)
        y_bounds_layout.addRow("Max Y:", y_max_spin)
        
        # Enable/disable manual controls based on auto checkbox
        def update_y_controls_enabled():
            enabled = not auto_y_checkbox.isChecked()
            y_min_spin.setEnabled(enabled)
            y_max_spin.setEnabled(enabled)
        
        auto_y_checkbox.toggled.connect(update_y_controls_enabled)
        update_y_controls_enabled()
        
        y_bounds_group.setLayout(y_bounds_layout)
        controls_layout.addWidget(y_bounds_group)
        
        # Refresh Button
        refresh_btn = create_button("Refresh Plot", "primary")
        refresh_btn.clicked.connect(self._update_comparison_plot)
        controls_layout.addWidget(refresh_btn)
        
        controls_layout.addStretch()
        
        return controls_widget, y_min_spin, y_max_spin, auto_y_checkbox, y_axis_combo

    def _on_plot_tab_changed(self, index, plots_tabs):
        """Handle plot tab changes."""
        # If the comparison tab is selected, update it
        if plots_tabs.tabText(index) == "Condition comparison":
            self._update_comparison_plot()

    def _on_comparison_results_ready(self, results):
        """Handle results from comparison plot worker."""
        if not hasattr(self, 'comparison_plot'):
            return
            
        self.comparison_plot.clear()
        
        if not results:
            return
            
        # Extract metadata from results
        all_results = results
        conditions = []
        for r in all_results:
            if 'condition' in r:
                conditions.append(r['condition'])
        
        # Set x-axis ticks
        x_ticks = []
        for i, condition in enumerate(conditions):
            x_ticks.append((i, str(condition)))
        self.comparison_plot.getPlotItem().getAxis('bottom').setTicks([x_ticks])
        
        # Draw items
        for i, r in enumerate(all_results):
            if 'path_x' in r and 'path_y' in r:
                # Draw violin curve
                violin_curve = pg.PlotCurveItem(r['path_x'], r['path_y'], pen='#00FFFF')
                self.comparison_plot.addItem(violin_curve)
                
                # Fill violin
                x_half = len(r['path_x']) // 2
                x_left = r['path_x'][:x_half]
                x_right = r['path_x'][x_half:][::-1]
                y_range = r['path_y'][:x_half]
                
                fill = pg.FillBetweenItem(
                    pg.PlotCurveItem(x_left, y_range),
                    pg.PlotCurveItem(x_right, y_range),
                    brush=(0, 255, 255, 100)
                )
                self.comparison_plot.addItem(fill)
            
            if 'quartiles' in r:
                for q_idx, (q_val, w) in enumerate(r['quartiles']):
                    is_median = q_idx == 1
                    line = pg.PlotCurveItem([i - w, i + w], [q_val, q_val], pen=pg.mkPen('w', width=1 if not is_median else 2))
                    self.comparison_plot.addItem(line)
            
            if 'mean' in r:
                mean_dot = pg.ScatterPlotItem([i], [r['mean']], size=8, brush='r', pen='w')
                self.comparison_plot.addItem(mean_dot)
        
        # Set Y range
        if self.comparison_auto_y.isChecked():
            self.comparison_plot.enableAutoRange(axis='y', enable=True)
        else:
            y_min = self.comparison_y_min_spin.value()
            y_max = self.comparison_y_max_spin.value()
            if y_max > y_min:
                self.comparison_plot.setYRange(y_min, y_max, padding=0)
        
        # Set X range
        self.comparison_plot.setXRange(-0.5, len(conditions) - 0.5, padding=0.1)
        
        # Update title and label
        y_feature = self.comparison_y_axis_combo.currentText()
        self.comparison_plot.setTitle(f'Condition Comparison: {y_feature}')
        self.comparison_plot.setLabel('left', y_feature.replace('_', ' ').title())

    def _update_comparison_plot(self):
        """Update the condition comparison violin plot using a background thread."""
        if not PYQTGRAPH_AVAILABLE or not hasattr(self, 'comparison_plot'):
            return
            
        if not hasattr(self, 'matched_peaks_list'):
            self.matched_peaks_list = []
            
        if not self.matched_peaks_list:
            self.matched_peaks_df = pl.DataFrame()
            # Try to load from CSV if memory is empty (e.g. after restart)
            if self.selected_sample_path and self.experiment_string:
                csv_filename = f"{self.experiment_string}_uncalibrated_peaks.csv"
                csv_filepath = os.path.join(self.selected_sample_path, csv_filename)
                if os.path.exists(csv_filepath):
                    try:
                        self.matched_peaks_df = pl.read_csv(csv_filepath)
                    except:
                        pass
            
            if self.matched_peaks_df.is_empty():
                self.comparison_plot.clear()
                return
        else:
            # We have in-memory data, build DataFrame from the list
            self.matched_peaks_df = pl.DataFrame(self.matched_peaks_list)

        y_feature = self.comparison_y_axis_combo.currentText()
        if y_feature not in self.matched_peaks_df.columns:
            return
            
        # Create and start worker thread
        def worker_task(df, feature, signal):
            try:
                results = []
                conditions = df['condition'].unique().sort().to_list()
                
                for i, condition in enumerate(conditions):
                    # Filter data
                    data_series = df.filter(pl.col('condition') == condition).select(feature).drop_nulls().to_series()
                    # Convert to numeric if needed
                    try:
                        data = data_series.cast(pl.Float64).to_numpy()
                    except:
                        continue
                        
                    if len(data) == 0:
                        continue
                        
                    res = {'condition': condition}
                    
                    # Calculate violin shape
                    try:
                        from scipy import stats
                        if len(data) > 1 and np.std(data) > 0:
                            kde = stats.gaussian_kde(data)
                            y_range = np.linspace(min(data), max(data), 100)
                            widths = kde(y_range)
                            if np.max(widths) > 0:
                                widths = widths / np.max(widths) * 0.4
                        else:
                            y_range = np.array([data[0]])
                            widths = np.array([0.4])
                    except:
                        counts, bins = np.histogram(data, bins=20)
                        y_range = (bins[:-1] + bins[1:]) / 2
                        widths = counts / np.max(counts) * 0.4 if np.max(counts) > 0 else np.zeros_like(y_range)
                    
                    res['path_x'] = np.concatenate([i - widths, (i + widths)[::-1]])
                    res['path_y'] = np.concatenate([y_range, y_range[::-1]])
                    
                    # Stats
                    q1, q2, q3 = np.percentile(data, [25, 50, 75])
                    res['mean'] = np.mean(data)
                    
                    # Quartile widths
                    res['quartiles'] = []
                    for q in [q1, q2, q3]:
                        try:
                            if len(data) > 1 and np.std(data) > 0:
                                w = kde(q)[0] / np.max(kde(data)) * 0.4
                            else:
                                w = 0.4
                        except:
                            w = np.interp(q, y_range, widths)
                        res['quartiles'].append((q, w))
                        
                    results.append(res)
                
                signal.emit(results)
            except Exception as e:
                print(f"Comparison worker error: {e}")
                signal.emit([])

        # Use a simple thread since we're in a GUI environment and want to avoid complexity
        # for this specific task. 
        import threading
        thread = threading.Thread(
            target=worker_task, 
            args=(self.matched_peaks_df, y_feature, self.comparison_results_ready),
            daemon=True
        )
        thread.start()
    
    def _create_increment_control(self, min_val, max_val, initial_val, step, suffix="", is_int=True, arrow_key_step=None):
        """
        Creates a custom increment control with explicit + and - buttons and manual text entry.
        Returns a container widget with get_value() and set_value() methods.
        Matches the style used in pyPump for minimum peaks and minimum volume controls.
        
        Args:
            min_val: Minimum value
            max_val: Maximum value
            initial_val: Initial value
            step: Step size for button clicks
            suffix: Suffix text to display after value
            is_int: Whether value is integer
            arrow_key_step: Step size for arrow key presses (if None, uses step)
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        
        # Use arrow_key_step if provided, otherwise use step
        arrow_step = arrow_key_step if arrow_key_step is not None else step
        
        # Internal value storage
        current_val = [initial_val]  # Use list to allow modification in nested functions
        
        def increment():
            new_val = min(max_val, current_val[0] + step)
            current_val[0] = new_val
            update_display()
            # Emit value changed signal
            if hasattr(container, '_value_changed_callback'):
                container._value_changed_callback(new_val)
        
        def decrement():
            new_val = max(min_val, current_val[0] - step)
            current_val[0] = new_val
            update_display()
            # Emit value changed signal
            if hasattr(container, '_value_changed_callback'):
                container._value_changed_callback(new_val)
        
        # Value input (editable) - left aligned, with arrow key support
        # Create it first as a placeholder, will be properly initialized after functions are defined
        value_input = None
        
        def increment_arrow():
            """Increment using arrow key step size."""
            new_val = min(max_val, current_val[0] + arrow_step)
            current_val[0] = new_val
            update_display()
            # Emit value changed signal
            if hasattr(container, '_value_changed_callback'):
                container._value_changed_callback(new_val)
        
        def decrement_arrow():
            """Decrement using arrow key step size."""
            new_val = max(min_val, current_val[0] - arrow_step)
            current_val[0] = new_val
            update_display()
            # Emit value changed signal
            if hasattr(container, '_value_changed_callback'):
                container._value_changed_callback(new_val)
        
        def update_display():
            """Update the text input field with the current value."""
            if value_input is not None:
                if is_int:
                    value_input.setText(f"{int(current_val[0])}")
                else:
                    value_input.setText(f"{current_val[0]:.3f}")
        
        def validate_and_set_value(text):
            """Validate input text and update the internal value."""
            try:
                # Remove suffix if present in text
                text_clean = text.replace(suffix, "").strip()
                if is_int:
                    new_val = int(text_clean)
                else:
                    new_val = float(text_clean)
                # Clamp to valid range
                new_val = max(min_val, min(max_val, new_val))
                current_val[0] = new_val
                update_display()
                # Emit value changed signal
                if hasattr(container, '_value_changed_callback'):
                    container._value_changed_callback(new_val)
            except (ValueError, TypeError):
                # If invalid, revert to current value
                update_display()
        
        # Now create the value input with arrow key support
        value_input = ArrowKeyLineEdit(increment_arrow, decrement_arrow)
        value_input.setAlignment(Qt.AlignmentFlag.AlignLeft)
        value_input.setMinimumWidth(60)
        value_input.setMaximumWidth(80)
        value_input.setStyleSheet("border: 1px solid #ccc; border-radius: 3px; padding: 5px; background-color: white; font-size: 11pt;")
        layout.addWidget(value_input)
        
        # Suffix label (if provided)
        if suffix:
            suffix_label = QLabel(suffix)
            suffix_label.setStyleSheet("font-size: 11pt;")
            layout.addWidget(suffix_label)
        
        # Decrease button - use arrow character for better visibility
        dec_btn = create_increment_button("▼")
        dec_btn.setMinimumWidth(40)
        dec_btn.setMaximumWidth(40)
        dec_btn.setMinimumHeight(30)
        layout.addWidget(dec_btn)
        
        # Increase button - use arrow character for better visibility
        inc_btn = create_increment_button("▲")
        inc_btn.setMinimumWidth(40)
        inc_btn.setMaximumWidth(40)
        inc_btn.setMinimumHeight(30)
        layout.addWidget(inc_btn)
        
        def get_value():
            return current_val[0]
        
        def set_value(val):
            current_val[0] = max(min_val, min(max_val, val))
            update_display()
        
        # Connect signals
        inc_btn.clicked.connect(increment)
        dec_btn.clicked.connect(decrement)
        value_input.editingFinished.connect(lambda: validate_and_set_value(value_input.text()))
        value_input.returnPressed.connect(lambda: validate_and_set_value(value_input.text()))
        
        # Initial display
        update_display()
        
        # Store value_input reference for external access
        container.value_input = value_input
        
        # Attach getter/setter to container for easy access
        container.get_value = get_value
        container.set_value = set_value
        
        return container
    
    def _create_plot_column(self):
        """Create a plot column widget with time vs frequency plot."""
        # Use helper function to create plot column
        packet_count_label_ref = []
        data_rate_label_ref = []
        max_packets_ref = []
        plot_widget_ref = []
        plot_data_item_ref = []
        noise_label_ref = []
        
        plot_widget = create_plot_column_widget(
            packet_count_label_ref, data_rate_label_ref, max_packets_ref,
            plot_widget_ref, plot_data_item_ref,
            self._on_max_packets_changed, self.show_diagnostic_plot,
            noise_label_ref
        )
        
        # Store references
        self.packet_count_label = packet_count_label_ref[0]
        self.data_rate_label = data_rate_label_ref[0]
        self.max_packets = max_packets_ref[0]
        if plot_widget_ref:
            self.plot_widget = plot_widget_ref[0]
        if plot_data_item_ref:
            self.plot_data_item = plot_data_item_ref[0]
        if noise_label_ref:
            self.noise_label = noise_label_ref[0]
        
        return plot_widget
    
    def _safe_gui_update(self, func):
        """
        Safely execute a GUI update function from any thread.
        Uses QTimer.singleShot to schedule the update in the main thread's event loop.
        """
        QTimer.singleShot(0, func)
    
    def on_tcp_connect_clicked(self):
        """Handle TCP FPGA connection button click. Reads settings from config file."""
        # Reload config to get latest values
        self.load_config()
        
        # Disable button during connection attempt (if dialog exists) - thread-safe
        if self.advanced_controls_dialog is not None:
            dialog = self.advanced_controls_dialog  # Capture reference for lambda
            self._safe_gui_update(lambda: dialog.connect_button.setEnabled(False))
            self._safe_gui_update(lambda: dialog.connect_button.setText("Connecting..."))
            if self.response_display is None:
                # Set response_display reference in main thread
                def set_response_display():
                    self.response_display = dialog.response_display
                    if self.response_display:
                        self.response_display.setText("Attempting connection...")
                self._safe_gui_update(set_response_display)
            else:
                self._safe_gui_update(lambda: self.response_display.setText("Attempting connection..."))
        
        # Initiate connection using command queue
        success, message = self.fpga_command_queue.initialize_connection(
            nios_ip=self.nios_ip,
            multicast_ip=self.multicast_ip,
            host_ip=self.host_ip,
            udp_port=self.udp_port,
            remote_port=self.remote_port
        )
        
        # Re-enable button (if dialog exists) - thread-safe
        if self.advanced_controls_dialog is not None:
            self._safe_gui_update(lambda: self.advanced_controls_dialog.connect_button.setEnabled(True))
            self._safe_gui_update(lambda: self.advanced_controls_dialog.connect_button.setText("Initiate TCP FPGA Connection"))
        
        if success:
            # Update connection status and indicators in a thread-safe way (all in main thread)
            def update_on_success():
                self.tcp_connection_status = "connected"
                # Get initial response by sending a test command (connection already established)
                # The connection response was already received during initialization
                display_message = f"Connection successful!\r\n{message}"
                if self.response_display is not None:
                    self.response_display.setText(display_message)
                self._update_connection_indicators()
                self._update_push_settings_button_state()
            self._safe_gui_update(update_on_success)
        else:
            # Update connection status and indicators in a thread-safe way (all in main thread)
            def update_on_failure():
                self.tcp_connection_status = "failed"
                if self.response_display is not None:
                    self.response_display.setText(f"Connection failed: {message}")
                self._update_connection_indicators()
                self._update_push_settings_button_state()
            self._safe_gui_update(update_on_failure)
        
    def on_udp_connect_clicked(self):
        """Handle UDP FPGA connection button click. Opens read-only UDP multicast connection."""
        # Reload config to get latest values
        self.load_config()
        
        # Disable button during connection attempt (if dialog exists) - thread-safe
        if self.advanced_controls_dialog is not None:
            dialog = self.advanced_controls_dialog  # Capture reference for lambda
            self._safe_gui_update(lambda: dialog.connect_button_udp.setEnabled(False))
            self._safe_gui_update(lambda: dialog.connect_button_udp.setText("Connecting..."))
            if self.response_display_udp is None:
                # Set response_display_udp reference in main thread
                def set_response_display_udp():
                    self.response_display_udp = dialog.response_display_udp
                    if self.response_display_udp:
                        self.response_display_udp.setText("Attempting UDP multicast connection...")
                self._safe_gui_update(set_response_display_udp)
            else:
                self._safe_gui_update(lambda: self.response_display_udp.setText("Attempting UDP multicast connection..."))
        
        try:
            # Initialize UDP connection using UDP manager
            success, message = self.udp_data_manager.initialize_connection(
                multicast_ip=self.multicast_ip,
                host_ip=self.host_ip,
                udp_port=self.udp_port
            )
            
            if success:
                self.udp_connection_status = "connected"
                if self.response_display_udp is not None:
                    self._safe_gui_update(lambda: self.response_display_udp.setText(message))
                
                # Start receiving UDP data and updating plot - thread-safe
                self._safe_gui_update(lambda: self.start_udp_data_reception())
            else:
                self.udp_connection_status = "failed"
                if self.response_display_udp is not None:
                    self._safe_gui_update(lambda: self.response_display_udp.setText(message))
        except Exception as e:
            self.udp_connection_status = "failed"
            message = f"UDP connection failed: {str(e)}"
            if self.response_display_udp is not None:
                self._safe_gui_update(lambda: self.response_display_udp.setText(message))
        
        # Re-enable button (if dialog exists) - thread-safe
        if self.advanced_controls_dialog is not None:
            self._safe_gui_update(lambda: self.advanced_controls_dialog.connect_button_udp.setEnabled(True))
            self._safe_gui_update(lambda: self.advanced_controls_dialog.connect_button_udp.setText("Initiate UDP FPGA Connection"))
        
        # Update connection indicators - thread-safe
        self._safe_gui_update(lambda: self._update_connection_indicators())
    
    def start_udp_data_reception(self):
        """Start receiving UDP data and updating the plot."""
        if not self.udp_data_manager.is_connected():
            return
        
        if self.receiving_udp_data:
            return
        
        self.receiving_udp_data = True
        self.start_time = None  # Will be set on first packet using perf_counter
        self.packets.clear()
        self.frequencies.clear()  # Clear frequencies deque
        self.packet_count = 0
        # Reset diagnostic tracking
        self.timestamp_deltas.clear()
        self.last_timestamp = None
        # Reset data rate tracking
        self.packet_timestamps.clear()
        self.data_rate = 0.0
        # Reset extended frequency bounds tracking
        self.extended_freq_min = None
        self.extended_freq_max = None
        self.extended_freq_mean = None
        self.extended_freq_window.clear()
        self.stable_freq_min = None
        self.stable_freq_max = None
        self.stable_freq_mean = None
        
        # Subscribe to UDP manager
        if self.udp_subscriber_id is None:
            self.udp_subscriber_id, self.udp_subscriber_queue = self.udp_data_manager.subscribe_queue(maxsize=5000)
        
        # Start receiving thread that consumes from UDP manager queue
        self.udp_receive_thread = threading.Thread(target=self._udp_receive_loop, daemon=True)
        self.udp_receive_thread.start()
        
        # Start plot data preparation thread (runs continuously, prepares data when needed)
        # This isolates expensive work from both receive and plot threads
        # Uses optimized loop with simplified data structure (frequencies only, no timing interpolation)
        self.plot_prep_running = True
        if self.plot_prep_thread is None or not self.plot_prep_thread.is_alive():
            from helper_functions.frequency_plot import plot_data_preparation_loop_optimized
            self.plot_prep_thread = threading.Thread(
                target=plot_data_preparation_loop_optimized,
                args=(
                    lambda: self.plot_prep_running,
                    self.frequencies,  # Use frequencies deque instead of packets
                    lambda: self.max_packets.value() * 128,  # Convert packets to frequencies (128 per packet)
                    lambda: self.start_time,
                    lambda: self.data_rate,
                    lambda data: setattr(self, 'plot_data', data)
                ),
                daemon=True
            )
            self.plot_prep_thread.start()
        
        # Set up periodic timer for plot updates
        # Use QTimer.singleShot for async updates to avoid blocking receive thread
        # Plot update just swaps pre-prepared arrays (very fast, lock-free, minimal GIL time)
        # Increased to 16ms (~60fps) for silky smooth visualization
        self.plot_update_timer = QTimer()
        self.plot_update_timer.timeout.connect(self._schedule_plot_update)
        self.plot_update_timer.start(16)  # Schedule update every 16ms (~60fps)
        
        # Periodic timer for diagnostic window updates (if open)
        # Reduced to 50ms to avoid periodic delays that match spike pattern
        self.diagnostic_update_timer = QTimer()
        self.diagnostic_update_timer.timeout.connect(self._update_diagnostic_plot)
        self.diagnostic_update_timer.start(50)  # Update every 50ms (reduced from 150ms)
        
        # Timer to update data rate display
        self.data_rate_update_timer = QTimer()
        self.data_rate_update_timer.timeout.connect(self._update_data_rate)
        self.data_rate_update_timer.start(100)  # Update every 100ms
        
        # Timer to update noise indicator
        self.noise_update_timer = QTimer()
        self.noise_update_timer.timeout.connect(self._update_noise_indicator)
        self.noise_update_timer.start(100)  # Update every 100ms
        
        # Periodic TCP connection status sync (in case connection is established elsewhere)
        self.tcp_status_sync_timer = QTimer()
        self.tcp_status_sync_timer.timeout.connect(self._sync_tcp_connection_status)
        self.tcp_status_sync_timer.start(500)  # Check every 500ms
        
        # Initialize real-time analyzer when data rate is known
        if self.data_rate > 0:
            self._initialize_realtime_analyzer()
    
    def _udp_receive_loop(self):
        """Main loop for receiving UDP data from manager queue and updating plot.
        
        Optimized to batch process packets for better performance and to prevent queue overflow.
        """
        batch_size = 50  # Process up to 50 packets per iteration
        freq_bounds_update_interval = 10  # Update frequency bounds every N packets
        
        while self.receiving_udp_data and self.udp_subscriber_queue is not None:
            try:
                # Batch process: drain all available packets up to batch_size
                packets_to_process = []
                for _ in range(batch_size):
                    try:
                        udp_packet = self.udp_subscriber_queue.get_nowait()
                        if udp_packet is None:
                            break
                        packets_to_process.append(udp_packet)
                    except queue.Empty:
                        break  # No more packets available
                
                # If no packets, sleep briefly to avoid busy-waiting
                if not packets_to_process:
                    time.sleep(0.001)  # 1ms sleep to avoid CPU spinning
                    continue
                
                # Process all packets in batch
                packets_processed = 0
                for udp_packet in packets_to_process:
                    # Process packet from UDP manager
                    if udp_packet.timestamp is not None and len(udp_packet.raw_bytes) >= 4:
                        # Extract packet number (already extracted in manager, but verify)
                        packet_number = udp_packet.packet_number
                        if packet_number is None:
                            try:
                                packet_count_raw = struct.unpack('<i', udp_packet.raw_bytes[:4])[0]
                                packet_number = packet_count_raw // 256
                            except (struct.error, ValueError):
                                continue
                        
                        # Use timestamp from packet
                        recv_timestamp = udp_packet.timestamp
                        
                        # Normalize timestamp to Epoch domain (wall-clock time)
                        # If the timestamp is very small, it's likely monotonic (seconds since boot)
                        # whereas Epoch time is currently > 1.7e9.
                        if recv_timestamp is not None and recv_timestamp < 1e9:
                            recv_timestamp += self._timestamp_offset
                        
                        # Use timestamp captured immediately after recvfrom() (or from kernel)
                        if self.start_time is None:
                            self.start_time = recv_timestamp
                        
                        # Calculate timestamp delta for diagnostic plot
                        if self.last_timestamp is not None:
                            delta = recv_timestamp - self.last_timestamp
                            self.timestamp_deltas.append(delta)
                        self.last_timestamp = recv_timestamp
                        
                        # Create and store packet data with pre-parsed frequencies
                        # UDP manager already parsed frequencies, so use them directly
                        packet = PacketData(
                            raw_bytes=udp_packet.raw_bytes,
                            timestamp=recv_timestamp,
                            packet_number=packet_number,
                            frequencies=udp_packet.parsed_frequencies
                        )
                        
                        # Append all frequencies to the frequencies deque for optimized plotting
                        if packet.frequencies:
                            for freq in packet.frequencies:
                                self.frequencies.append(freq)
                            
                            # Feed data to real-time analyzer
                            if self.realtime_analyzer is not None:
                                try:
                                    # Calculate timestamps for each frequency
                                    # Protect against division by zero or invalid data rate
                                    if self.data_rate > 0:
                                        time_step = 1.0 / self.data_rate
                                    else:
                                        time_step = 5e-5  # Default: 20kHz
                                    
                                    num_freqs = len(packet.frequencies)
                                    if num_freqs > 0:
                                        data_points = []
                                        # Calculate relative time if saving
                                        start_ref = self.experiment_start_time_for_packets if self.experiment_start_time_for_packets else recv_timestamp
                                        
                                        for i, freq in enumerate(packet.frequencies):
                                            # Packet timestamp corresponds to the last frequency
                                            freq_time = recv_timestamp - ((num_freqs - 1 - i) * time_step)
                                            rel_time = freq_time - start_ref
                                            data_points.append((freq_time, freq, packet_number, rel_time))
                                        self.realtime_analyzer.add_data_points(data_points)
                                except Exception as e:
                                    # Handle errors gracefully - don't crash on bad data
                                    print(f"Error adding data points to real-time analyzer: {e}")
                        
                        # Save packet to file if saving is active
                        if self.is_saving and self.data_saver is not None:
                            # Set experiment start time from first packet if not already set
                            # This uses packet timestamp format to match packet.timestamp format
                            if self.experiment_start_time_for_packets is None:
                                self.experiment_start_time_for_packets = recv_timestamp
                            
                            try:
                                self.data_saver.add_packet(packet, self.experiment_start_time_for_packets)
                            except Exception as e:
                                print(f"Error saving packet: {e}")
                        
                        # Track packet for data rate calculation
                        num_frequencies = len(packet.frequencies) if packet.frequencies else 0
                        self.packet_timestamps.append((packet.timestamp, num_frequencies))
                        
                        # Remove packets older than 1 second (keep 1-second window for data rate calculation)
                        # Do this once per batch, not per packet, for efficiency
                        if packets_processed == 0:  # Only clean up once per batch
                            current_time = packet.timestamp
                            while self.packet_timestamps and (current_time - self.packet_timestamps[0][0]) > 1.0:
                                self.packet_timestamps.popleft()
                        
                        # Update extended frequency bounds for stable y-axis (throttled)
                        # Only update every N packets to reduce computational overhead
                        if packet.frequencies and (packets_processed % freq_bounds_update_interval == 0):
                            max_packets_val = self.max_packets.value() if hasattr(self, 'max_packets') else 50
                            result = update_extended_freq_bounds(
                                packet.frequencies, max_packets_val, self.extended_freq_window,
                                self.extended_freq_min, self.extended_freq_max, self.extended_freq_mean,
                                self.stable_freq_min, self.stable_freq_max, self.stable_freq_mean
                            )
                            (self.extended_freq_min, self.extended_freq_max, self.extended_freq_mean,
                             self.stable_freq_min, self.stable_freq_max, self.stable_freq_mean) = result
                        
                        self.packets.append(packet)
                        packets_processed += 1
                        
                        # Defer GUI updates to reduce processing in receive loop
                        # Update packet count label if changed (throttled, non-blocking)
                        # Only update every 10 packets to avoid accumulating QTimer callbacks
                        if packet_number != self.packet_count:
                            self.packet_count = packet_number
                            # Throttle updates to avoid accumulating timers
                            if packet_number % 10 == 0:
                                # Update diagnostic window if it exists
                                if hasattr(self, 'diagnostic_window') and self.diagnostic_window is not None:
                                    QTimer.singleShot(0, lambda pc=self.packet_count: self.diagnostic_window.packet_count_label.setText(f"Packet Count: {pc}"))
                
                # Don't rebuild plot data every packet - that would be inefficient
                # Plot data will be rebuilt in plot update thread when needed
                # This keeps receive thread fast and timestamp-accurate
                
            except Exception as e:
                if self.receiving_udp_data:
                    import sys
                    print(f"Error in UDP receive loop: {e}", file=sys.stderr, flush=True)
                break
    
    def _on_max_packets_changed(self):
        """Handle change in max packets setting - trigger plot update."""
        # With simplified plotting, no need to recalculate extended bounds
        # pyqtgraph handles auto-ranging automatically
        # Just trigger plot update
        self.update_plot()
    
    def _schedule_plot_update(self):
        """
        Schedule plot update asynchronously using QTimer.singleShot.
        This prevents blocking the timer callback and allows receive thread more CPU time.
        """
        if not self._plot_update_pending:
            self._plot_update_pending = True
            # Schedule update in next event loop iteration (non-blocking)
            QTimer.singleShot(0, self._do_plot_update)
    
    def _do_plot_update(self):
        """Actually perform the plot update (called asynchronously)."""
        self._plot_update_pending = False
        self.update_plot()
    
    def _update_data_rate(self):
        """Calculate and update data rate display based on packets received in the last 1 second."""
        # Always use current time as reference to ensure data rate winds down when packets stop
        # Using time.perf_counter() ensures old packets age out even when Run = FALSE
        current_time = time.perf_counter()
        
        # Filter packets within last 1 second
        packets_in_window = [
            (ts, num_freq) for ts, num_freq in self.packet_timestamps
            if (current_time - ts) <= 1.0
        ]
        
        # Calculate total frequencies received in the 1-second window
        total_frequencies = sum(num_freq for _, num_freq in packets_in_window)
        
        # Calculate data rate: frequencies per second
        if len(packets_in_window) > 0:
            self.data_rate = total_frequencies / 1.0
        else:
            # No packets in window - display 0
            self.data_rate = 0.0
        
        # Update analyzer data rate if it exists
        if self.realtime_analyzer is not None:
            self.realtime_analyzer.set_data_rate(self.data_rate)
        elif self.data_rate > 0 and self.realtime_analyzer is None:
            # Initialize analyzer if data rate is now known
            self._initialize_realtime_analyzer()
        
        # Update display
        if self.data_rate >= 1000000:
            # Display in MHz
            display_text = f"{self.data_rate/1e6:.2f} MHz"
        elif self.data_rate >= 1000:
            # Display in kHz
            display_text = f"{self.data_rate/1e3:.2f} kHz"
        elif self.data_rate > 0:
            # Display in Hz
            display_text = f"{self.data_rate:.1f} Hz"
        else:
            # No packets received - display 0 Hz
            display_text = "0 Hz"
        
        if hasattr(self, 'data_rate_indicator') and hasattr(self.data_rate_indicator, 'value_label'):
            self.data_rate_indicator.value_label.setText(display_text)
        # Update diagnostic window if it exists
        if hasattr(self, 'diagnostic_window') and self.diagnostic_window is not None:
            self.diagnostic_window.data_rate_label.setText(f"Data Rate: {display_text}")
    
    def _update_diagnostic_plot(self):
        """Update diagnostic plot if window is open."""
        if self.diagnostic_window is not None:
            self.diagnostic_window.update_plot()
    
    def _update_noise_indicator(self):
        """Calculate and update noise indicator (RMSE of last packet) in mHz."""
        if not hasattr(self, 'noise_label'):
            return
        
        if not self.receiving_udp_data or len(self.packets) == 0:
            self.noise_label.setText("Noise: -- mHz")
            return
        
        try:
            # Get the last packet
            last_packet = self.packets[-1]
            frequencies = last_packet.frequencies
            
            if frequencies is None or len(frequencies) == 0:
                self.noise_label.setText("Noise: -- mHz")
                return
            
            # Convert to numpy array for easier calculation
            freq_array = np.array(frequencies)
            
            # Calculate mean frequency
            mean_freq = np.mean(freq_array)
            
            # Calculate RMSE (Root Mean Square Error)
            # RMSE = sqrt(mean((frequencies - mean)^2))
            squared_errors = (freq_array - mean_freq) ** 2
            rmse = np.sqrt(np.mean(squared_errors))
            
            # Convert to mHz (millihertz)
            rmse_mhz = rmse * 1000.0
            
            # Update display
            if rmse_mhz >= 1000.0:
                # Display in Hz with 2 decimal places if >= 1 Hz
                self.noise_label.setText(f"Noise: {rmse_mhz/1000.0:.2f} Hz")
            else:
                # Display in mHz rounded to nearest integer
                self.noise_label.setText(f"Noise: {int(round(rmse_mhz))} mHz")
                
        except Exception as e:
            # On error, show error state
            self.noise_label.setText("Noise: Error")
    
    def get_current_noise_mhz(self):
        """Get current noise value in mHz.
        
        Returns:
            Noise value in mHz (float), or None if noise cannot be calculated.
        """
        if not self.receiving_udp_data or len(self.packets) == 0:
            return None
        
        try:
            # Get the last packet
            last_packet = self.packets[-1]
            frequencies = last_packet.frequencies
            
            if frequencies is None or len(frequencies) == 0:
                return None
            
            # Convert to numpy array for easier calculation
            freq_array = np.array(frequencies)
            
            # Calculate mean frequency
            mean_freq = np.mean(freq_array)
            
            # Calculate RMSE (Root Mean Square Error)
            # RMSE = sqrt(mean((frequencies - mean)^2))
            squared_errors = (freq_array - mean_freq) ** 2
            rmse = np.sqrt(np.mean(squared_errors))
            
            # Convert to mHz (millihertz)
            rmse_mhz = rmse * 1000.0
            
            return rmse_mhz
                
        except Exception as e:
            return None
    
    def _initialize_realtime_analyzer(self):
        """Initialize the real-time frequency analyzer."""
        if self.data_rate <= 0:
            return
        
        from helper_functions.DATA_realtime_frequency_analysis import RealTimeFrequencyAnalyzer
        
        # Create analyzer with current settings
        self.realtime_analyzer = RealTimeFrequencyAnalyzer(
            data_rate=self.data_rate,
            buffer_window_seconds=5.0,  # Balanced window: provides safety margin without excessive CPU load
            min_samples_for_analysis=1000,
            settings=self.peak_detection_settings
        )
        
        # Create worker thread for heavy computation
        if self.realtime_analysis_worker is None:
            self.realtime_analysis_worker = RealtimeAnalysisWorker(self.realtime_analyzer, self)
            self.realtime_analysis_worker.results_ready.connect(self._on_analysis_results_ready)
            self.realtime_analysis_worker.analysis_finished.connect(self._on_analysis_finished)
            self.realtime_analysis_worker.error_occurred.connect(self._on_analysis_error)
        # Connect new rows ready signal for comparison plot
        if hasattr(self.realtime_analysis_worker, 'new_rows_ready'):
            self.realtime_analysis_worker.new_rows_ready.connect(self._on_new_analysis_rows_ready)
            
        # Start periodic analysis timer (750ms interval)
        if self.realtime_analysis_timer is None:
            self.realtime_analysis_timer = QTimer()
            self.realtime_analysis_timer.timeout.connect(self._update_realtime_analysis)
            self.realtime_analysis_timer.start(1000)  # Running consistently at 1Hz

    
    def _update_realtime_analysis(self):
        """Periodically update real-time analysis: schedule processing in worker thread."""
        # Handle case when analyzer or worker is not initialized
        if self.realtime_analyzer is None:
            return
        
        # Stop analysis if peak detection is disabled
        if not self.detect_peaks_enabled:
            return
        
        # Stop analysis if not receiving data (FPGA stopped sending packets)
        if not self.receiving_udp_data:
            # Removing plot clearing when data stops to ensure persistence during idle states
            return
        
        # Skip if worker is already busy (prevents queueing multiple analyses)
        if self.realtime_analysis_worker_busy:
            return
        
        # Ensure worker exists
        if self.realtime_analysis_worker is None:
            return
        
        # Update data rate if it has changed (fast operation, safe on GUI thread)
        try:
            if abs(self.realtime_analyzer.data_rate - self.data_rate) > 0.1:
                self.realtime_analyzer.set_data_rate(self.data_rate)
        except Exception as e:
            print(f"Error updating analyzer data rate: {e}")
            return
        
        # Check if worker thread is still running from previous analysis
        if self.realtime_analysis_worker.isRunning():
            return
        
        # Start worker thread for heavy computation
        try:
            self.realtime_analysis_worker_busy = True
            self.realtime_analysis_worker.start()
        except Exception as e:
            print(f"Error starting analysis worker: {e}")
            self.realtime_analysis_worker_busy = False
    
    def _on_new_analysis_rows_ready(self, new_rows):
        """Handle new formatted rows from analysis worker for the comparison plot."""
        if not hasattr(self, 'matched_peaks_list'):
            self.matched_peaks_list = []
            
        self.matched_peaks_list.extend(new_rows)
        
        # Limit the size of the in-memory list to avoid memory leaks for long runs
        # Keep last 50,000 peaks
        if len(self.matched_peaks_list) > 50000:
            self.matched_peaks_list = self.matched_peaks_list[-50000:]
            
    def _on_analysis_results_ready(self, matched_count, unmatched_count, width_times, widths_ms, mass_times, masses_pg, throughput_times, throughput_values, current_throughput):
        """Handle analysis results from worker thread (runs on GUI thread)."""
        try:
            # Update sample peaks (total matched peaks for entire sample)
            # Use total peak count with an offset to track peaks for the CURRENT sample only
            # The offset is set in on_start_saving_clicked()
            if self.realtime_analyzer is not None:
                total_matched_count, _ = self.realtime_analyzer.get_total_peak_counts()
                self.sample_peaks_total = max(0, total_matched_count - self.matched_count_at_sample_start)
            else:
                self.sample_peaks_total = matched_count  # Fallback to filtered count
            
            if hasattr(self, 'sample_peaks_indicator') and hasattr(self.sample_peaks_indicator, 'value_label'):
                self.sample_peaks_indicator.value_label.setText(str(self.sample_peaks_total))
            
            # Update condition peaks (matched peaks for current condition)
            # Calculate increment since condition started using total counts
            if self.realtime_analyzer is not None:
                total_matched_count, _ = self.realtime_analyzer.get_total_peak_counts()
                condition_peaks = total_matched_count - self.matched_count_at_condition_start
            else:
                # Fallback to filtered count
                condition_peaks = matched_count - self.matched_count_at_condition_start
            
            self.condition_peaks_total = max(0, condition_peaks)  # Ensure non-negative
            if hasattr(self, 'condition_peaks_indicator') and hasattr(self.condition_peaks_indicator, 'value_label'):
                self.condition_peaks_indicator.value_label.setText(str(self.condition_peaks_total))
            
            # Update throughput display in Condition section (format as k/hr if >= 10k/hr)
            if hasattr(self, 'throughput_indicator') and hasattr(self.throughput_indicator, 'value_label'):
                if current_throughput >= 10000:
                    throughput_text = f"{current_throughput/1000:.1f} k/hr"
                else:
                    throughput_text = f"{current_throughput:.1f} cells/hour"
                self.throughput_indicator.value_label.setText(throughput_text)
                
            # Update Concentration
            if hasattr(self, 'concentration_indicator') and hasattr(self.concentration_indicator, 'value_label'):
                volume_drawn = 0.0
                if self.pump_widget and hasattr(self.pump_widget, 'current_volume_drawn'):
                    volume_drawn = self.pump_widget.current_volume_drawn
                
                if volume_drawn > 0.05: # Require at least 50nL to avoid infinity
                    concentration_per_ml = (self.condition_peaks_total / volume_drawn) * 1000.0
                    
                    if concentration_per_ml >= 1000000:
                        conc_text = f"{concentration_per_ml / 1000000:.1f} M/mL"
                    else:
                        conc_text = f"{concentration_per_ml / 1000:.1f} k/mL"
                        
                    # Apply Status Styling based on concentration
                    base_style = "padding: 2px; border-radius: 3px; font-weight: bold; font-size: 14pt;"
                    if 300000 <= concentration_per_ml <= 1200000:
                        color = "#d4edda" # Light Green
                    elif concentration_per_ml > 2000000:
                        color = "#f8d7da" # Light Red
                    else:
                        color = "#fff3cd" # Yellow
                        
                    self.concentration_indicator.value_label.setStyleSheet(f"background-color: {color}; {base_style}")
                    self.concentration_indicator.value_label.setText(conc_text)
                else:
                    self.concentration_indicator.value_label.setStyleSheet("padding: 2px; border-radius: 3px; font-weight: bold;")
                    self.concentration_indicator.value_label.setText("--")
            
            # NOTE: CSV writing and plot data filtering moved to RealtimeAnalysisWorker
            
            # Update plots with pre-filtered data from worker
            if PYQTGRAPH_AVAILABLE and hasattr(self, 'peak_width_plot_item') and hasattr(self, 'peak_mass_plot_item'):
                # Update peak width plot
                if width_times is not None and len(width_times) > 0:
                    self.peak_width_plot_item.setData(width_times, widths_ms)
                    try:
                        view_box = self.peak_width_plot.getPlotItem().getViewBox()
                        auto_x = view_box.autoRangeEnabled()[0]
                        auto_y = self.peak_width_auto_y.isChecked() if (hasattr(self, 'peak_width_auto_y') and self.peak_width_auto_y) else view_box.autoRangeEnabled()[1]
                        
                        x_min, x_max = np.min(width_times), np.max(width_times)
                        
                        # X-Axis: Auto or Live-Edge Scroll
                        if not auto_x:
                            # If user is at the "Live Edge", maintain width and scroll
                            current_x_range = view_box.viewRange()[0]
                            view_width = current_x_range[1] - current_x_range[0]
                            if current_x_range[1] >= x_max - (view_width * 0.05):
                                self.peak_width_plot.setXRange(x_max - view_width, x_max, padding=0)
                        
                        # Y-Axis: Respect manual zoom
                        if not auto_y:
                            view_box.enableAutoRange(axis='y', enable=False)
                            if hasattr(self, 'peak_width_y_min_spin') and self.peak_width_y_min_spin and hasattr(self, 'peak_width_y_max_spin') and self.peak_width_y_max_spin:
                                y_min, y_max = self.peak_width_y_min_spin.value(), self.peak_width_y_max_spin.value()
                                if y_max > y_min:
                                    self.peak_width_plot.setYRange(y_min, y_max, padding=0)
                    except Exception as e:
                        print(f"Error setting peak width plot range: {e}")
                else:
                    self.peak_width_plot_item.setData([], [])
                
                # Update mass plot
                if mass_times is not None and len(mass_times) > 0:
                    self.peak_mass_plot_item.setData(mass_times, masses_pg)
                    try:
                        view_box = self.peak_mass_plot.getPlotItem().getViewBox()
                        auto_x = view_box.autoRangeEnabled()[0]
                        auto_y = self.peak_mass_auto_y.isChecked() if (hasattr(self, 'peak_mass_auto_y') and self.peak_mass_auto_y) else view_box.autoRangeEnabled()[1]
                        
                        x_min, x_max = np.min(mass_times), np.max(mass_times)
                        
                        # X-Axis: Auto or Live-Edge Scroll
                        if not auto_x:
                            # If user is at the "Live Edge", maintain width and scroll
                            current_x_range = view_box.viewRange()[0]
                            view_width = current_x_range[1] - current_x_range[0]
                            if current_x_range[1] >= x_max - (view_width * 0.05):
                                self.peak_mass_plot.setXRange(x_max - view_width, x_max, padding=0)
                            
                        # Y-Axis: Respect manual zoom
                        if not auto_y:
                            view_box.enableAutoRange(axis='y', enable=False)
                            if hasattr(self, 'peak_mass_y_min_spin') and self.peak_mass_y_min_spin and hasattr(self, 'peak_mass_y_max_spin') and self.peak_mass_y_max_spin:
                                y_min, y_max = self.peak_mass_y_min_spin.value(), self.peak_mass_y_max_spin.value()
                                if y_max > y_min:
                                    self.peak_mass_plot.setYRange(y_min, y_max, padding=0)
                    except Exception as e:
                        print(f"Error setting peak mass plot range: {e}")
                else:
                    self.peak_mass_plot_item.setData([], [])
                
                if throughput_times is not None and len(throughput_times) > 0:
                    self.throughput_plot_item.setData(throughput_times, throughput_values)
                    try:
                        view_box = self.throughput_plot.getPlotItem().getViewBox()
                        auto_x = view_box.autoRangeEnabled()[0]
                        auto_y = self.throughput_auto_y.isChecked() if (hasattr(self, 'throughput_auto_y') and self.throughput_auto_y) else view_box.autoRangeEnabled()[1]
                        
                        x_min, x_max = np.min(throughput_times), np.max(throughput_times)
                        
                        # X-Axis: Auto or Live-Edge Scroll
                        if not auto_x:
                            # If user is at the "Live Edge", maintain width and scroll
                            current_x_range = view_box.viewRange()[0]
                            view_width = current_x_range[1] - current_x_range[0]
                            if current_x_range[1] >= x_max - (view_width * 0.05):
                                self.throughput_plot.setXRange(x_max - view_width, x_max, padding=0)
                            
                        # Y-Axis: Respect manual zoom
                        if not auto_y:
                            view_box.enableAutoRange(axis='y', enable=False)
                            if hasattr(self, 'throughput_y_min_spin') and hasattr(self, 'throughput_y_max_spin'):
                                y_min, y_max = self.throughput_y_min_spin.value(), self.throughput_y_max_spin.value()
                                if y_max > y_min:
                                    self.throughput_plot.setYRange(y_min, y_max, padding=0)
                    except Exception as e:
                        print(f"Error setting throughput plot range: {e}")
                else:
                    self.throughput_plot_item.setData([], [])
        except Exception as e:
            print(f"Error updating analysis results in GUI: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_analysis_finished(self, success):
        """Handle analysis completion (runs on GUI thread)."""
        self.realtime_analysis_worker_busy = False
    
    def _on_analysis_error(self, error_msg):
        """Handle analysis error (runs on GUI thread)."""
        print(error_msg)
        self.realtime_analysis_worker_busy = False
    
    def update_plot(self):
        """
        Update the plot with pre-prepared data.
        Plot data is prepared in a separate thread, so this just swaps arrays.
        Lock-free atomic read - no GIL contention with receive thread.
        Uses simplified update function - pyqtgraph handles auto-ranging automatically.
        """
        if not hasattr(self, 'plot_widget') or not hasattr(self, 'plot_data_item'):
            return
        
        from helper_functions.frequency_plot import update_plot_widget_simple
        update_plot_widget_simple(self.plot_widget, self.plot_data_item, self.plot_data)
    
    def pause_udp_data_reception(self):
        """
        Pause UDP data reception temporarily (e.g., during sweep).
        With UDP manager, both components can receive independently, so this
        is mainly for backward compatibility. We can pause our subscription.
        """
        if not self.receiving_udp_data:
            return
        
        # With UDP manager, we can't easily pause without unsubscribing
        # But since both components can receive independently now, pausing
        # is less critical. We'll just drain the queue to prevent processing.
        if self.udp_subscriber_queue is not None:
            import queue
            try:
                while True:
                    self.udp_subscriber_queue.get_nowait()
            except queue.Empty:
                pass
    
    def resume_udp_data_reception(self):
        """
        Resume UDP data reception after being paused.
        With UDP manager, resuming just means continuing to process from queue.
        """
        if not self.receiving_udp_data:
            return
        
        # With UDP manager, resuming is automatic - packets continue to arrive
        # in the queue. No action needed.
    
    def _run_posthoc_analysis(self, csv_path, use_drift_correction=True):
        """Run post-hoc frequency analysis in a separate thread."""
        if self.posthoc_worker is not None and self.posthoc_worker.isRunning():
            print("Post-hoc analysis already in progress. Skipping.")
            return
            
        self.posthoc_worker = PostHocAnalysisWorker(
            csv_path, 
            parent=self, 
            use_drift_correction=False, # Default to False for post-hoc
            data_rate=getattr(self, 'data_rate', 20000.0),
            settings=self.peak_detection_settings
        )
        # Connect signals
        self.posthoc_worker.analysis_finished.connect(self._on_posthoc_analysis_finished)
        self.posthoc_worker.progress.connect(self._on_posthoc_progress)
        self.posthoc_worker.start()
        
    def _on_posthoc_progress(self, value, message):
        """Handle progress updates from post-hoc analysis worker."""
        self.posthoc_progress.emit(value, message)
    
    def _on_posthoc_analysis_finished(self, success, message):
        """Handle post-hoc analysis completion."""
        if success:
            print(f"Post-hoc Analysis: {message}")
            if self.response_display_udp is not None:
                self.response_display_udp.setText(f"Post-hoc analysis complete for {os.path.basename(self._last_uncalibrated_csv)}")
            # Signal 100% completion to progress dialog
            self.posthoc_progress.emit(100, "Analysis complete.")
        else:
            print(f"Post-hoc Analysis Error: {message}")
            if self.response_display_udp is not None:
                self.response_display_udp.setText(f"Post-hoc analysis failed: {message[:50]}...")
            # Signal 100% completion with error so the Finish button is still enabled
            self.posthoc_progress.emit(100, f"Analysis error: {message[:80]}")
        
        # Cleanup: properly wait for QThread to fully stop before releasing reference
        # Dropping the reference without wait() can cause PySide6/PyInstaller deadlocks
        if self.posthoc_worker is not None:
            try:
                self.posthoc_worker.analysis_finished.disconnect(self._on_posthoc_analysis_finished)
                self.posthoc_worker.progress.disconnect(self._on_posthoc_progress)
            except Exception:
                pass
            if self.posthoc_worker.isRunning():
                self.posthoc_worker.wait(2000)  # Wait up to 2s for thread cleanup
            self.posthoc_worker = None
        # Don't clear _last_uncalibrated_csv here so we can potentially retry or reference it
        
        # Close the console log now that post-hoc analysis is complete
        # Skip if we're in final clean mode (let the app shutdown handle it)
        if not self.is_final_clean:
            self.console_log_finished.emit()

    def on_udp_close_clicked(self):
        """Handle UDP multicast connection close button click."""
        # Stop receiving data
        self.receiving_udp_data = False
        
        # Unsubscribe from UDP manager
        if self.udp_subscriber_id is not None:
            self.udp_data_manager.unsubscribe(self.udp_subscriber_id)
            self.udp_subscriber_id = None
            self.udp_subscriber_queue = None
        
        # Stop receive thread
        if self.udp_receive_thread is not None:
            # Thread will check self.receiving_udp_data and exit
            self.udp_receive_thread.join(timeout=1.0)  # Wait up to 1 second
            self.udp_receive_thread = None
        
        # Stop plot data preparation thread
        self.plot_prep_running = False
        if hasattr(self, 'plot_prep_thread') and self.plot_prep_thread is not None:
            self.plot_prep_thread.join(timeout=1.0)  # Wait up to 1 second for thread to finish
        
        # Stop plot update timer
        if hasattr(self, 'plot_update_timer'):
            self.plot_update_timer.stop()
        
        # Stop diagnostic update timer
        if hasattr(self, 'diagnostic_update_timer'):
            self.diagnostic_update_timer.stop()
        
        # Stop data rate update timer
        if hasattr(self, 'data_rate_update_timer') and self.data_rate_update_timer is not None:
            self.data_rate_update_timer.stop()
        
        # Stop noise update timer
        if hasattr(self, 'noise_update_timer') and self.noise_update_timer is not None:
            self.noise_update_timer.stop()
        
        # Stop TCP status sync timer
        if hasattr(self, 'tcp_status_sync_timer') and self.tcp_status_sync_timer is not None:
            self.tcp_status_sync_timer.stop()
        
        # Stop real-time analysis timer
        if self.realtime_analysis_timer is not None:
            self.realtime_analysis_timer.stop()
        
        # Stop and cleanup real-time analysis worker thread
        if self.realtime_analysis_worker is not None:
            self.realtime_analysis_worker.abort()
            if self.realtime_analysis_worker.isRunning():
                self.realtime_analysis_worker.wait(1000)  # Wait up to 1 second
            self.realtime_analysis_worker = None
        self.realtime_analysis_worker_busy = False
        
        # Check if UDP manager is connected
        if not self.udp_data_manager.is_connected():
            if self.response_display_udp is not None:
                self.response_display_udp.setText("No UDP connection to close.")
            return
        
        try:
            # Note: We don't close the UDP manager socket here because other
            # components (like sweep) might still be using it. The manager
            # will handle cleanup when all subscribers are gone.
            # We just unsubscribe and mark as disconnected locally.
            self.udp_connection_status = "disconnected"
            
            message = "UDP multicast connection closed successfully."
            if self.response_display_udp is not None:
                self.response_display_udp.setText(message)
        except Exception as e:
            message = f"Error closing UDP connection: {str(e)}"
            if self.response_display_udp is not None:
                self.response_display_udp.setText(message)
            # Clean up status
            self.udp_connection_status = "disconnected"
        
        # Update connection indicators
        self._update_connection_indicators()
    
    def _load_daq_info(self):
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
    
    def load_config(self):
        """Load configuration from SMR_config.txt (READ-ONLY)."""
        if not os.path.exists(self.config_file):
            # Config file doesn't exist - use default values (config files are read-only)
            return
        
        try:
            # Read SMR config (READ-ONLY - never write to this file)
            with open(self.config_file, mode='r', encoding='utf-8') as file:
                content = file.read()
                config = parse_toml_config(content)
                
                # Load connection settings into instance variables
                if 'connection' in config:
                    conn_config = config['connection']
                    if 'nios_ip' in conn_config:
                        self.nios_ip = str(conn_config['nios_ip'])
                    if 'multicast_ip' in conn_config:
                        self.multicast_ip = str(conn_config['multicast_ip'])
                    if 'host_ip' in conn_config:
                        self.host_ip = str(conn_config['host_ip'])
                    if 'udp_port' in conn_config:
                        self.udp_port = int(conn_config['udp_port'])
                    if 'remote_port' in conn_config:
                        self.remote_port = int(conn_config['remote_port'])
        except Exception as e:
            print(f"Error loading config file: {e}")
    
    def _create_default_config(self):
        """Create a default configuration file.
        
        NOTE: This function is disabled - config files are READ-ONLY.
        If the config file doesn't exist, the application will use default values.
        """
        # Config files are read-only - do not create default config
        # If config file doesn't exist, the application will use default values
        pass
    
    def _setup_styles(self):
        """Apply styles to the widget."""
        self.setStyleSheet("""
            QWidget {
                background-color: #f0f0f0;
            }
            QLabel {
                font-family: 'Segoe UI', Arial;
            }
        """)
    
    def closeEvent(self, event):
        """Clean up when widget is closed."""
        # Stop receiving data
        self.receiving_udp_data = False
        
        # Stop receive thread
        if self.udp_receive_thread is not None:
            if self.udp_receive_thread.is_alive():
                self.udp_receive_thread.join(timeout=2.0)
        
        # Stop plot data preparation thread
        self.plot_prep_running = False
        if hasattr(self, 'plot_prep_thread') and self.plot_prep_thread is not None:
            if self.plot_prep_thread.is_alive():
                self.plot_prep_thread.join(timeout=1.0)  # Wait up to 1 second for thread to finish
        
        # Stop all timers
        if hasattr(self, 'plot_update_timer') and self.plot_update_timer is not None:
            self.plot_update_timer.stop()
        
        if hasattr(self, 'diagnostic_update_timer') and self.diagnostic_update_timer is not None:
            self.diagnostic_update_timer.stop()
        
        if hasattr(self, 'data_rate_update_timer') and self.data_rate_update_timer is not None:
            self.data_rate_update_timer.stop()
        
        if hasattr(self, 'noise_update_timer') and self.noise_update_timer is not None:
            self.noise_update_timer.stop()
        
        if hasattr(self, 'tcp_status_sync_timer') and self.tcp_status_sync_timer is not None:
            self.tcp_status_sync_timer.stop()
        
        # Close diagnostic window if open
        if self.diagnostic_window is not None:
            self.diagnostic_window.close()
        
        # Close SMR settings window if open
        if self.smr_settings_window is not None:
            self.smr_settings_window.close()
        
        # Stop post-hoc analysis if active
        if hasattr(self, 'posthoc_worker') and self.posthoc_worker is not None:
             if self.posthoc_worker.isRunning():
                 print("Stopping background post-hoc analysis...")
                 self.posthoc_worker.stop()
                 self.posthoc_worker.wait(2000) # Wait up to 2s
        
        event.accept()
    
    def show_diagnostic_plot(self):
        """Show diagnostic plot window with timestamp deltas."""
        if self.diagnostic_window is None:
            self.diagnostic_window = DiagnosticPlotWindow(self)
        self.diagnostic_window.show()
        self.diagnostic_window.raise_()
        self.diagnostic_window.activateWindow()

    def _get_most_recent_sweep_frequency(self) -> Optional[float]:
        """Get the most recent resonant frequency from sweep settings for the current chip.
        
        Returns:
            The resonant frequency in Hz if found, None otherwise.
        """
        result = self._get_most_recent_sweep_results()
        return result[0] if result else None
    
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

    def on_initialize_smr_clicked(self):
        """Handle Initialize SMR button click - run SMR sweep frequencies."""
        # Ensure TCP and UDP connections are established
        if not self.fpga_command_queue.is_connected():
            # Show connection dialog
            self.connection_dialog = FPGAConnectionDialog(self)
            self.connection_dialog.show()
            
            # Initialize TCP connection
            import threading
            def connect_thread():
                self.on_tcp_connect_clicked()
                self.on_udp_connect_clicked()
                # Wait a bit for connections to establish
                import time
                time.sleep(0.5)
            
            thread = threading.Thread(target=connect_thread, daemon=True)
            thread.start()
            thread.join(timeout=6.0)  # Wait up to 6 seconds for connection
            
            # Sync status after connection attempt
            self._sync_tcp_connection_status()
            
            # Dialog handles all error display - no separate warning needed
            if not self.fpga_command_queue.is_connected():
                return
        
        if not self.udp_data_manager.is_connected() or not self.receiving_udp_data:
            # Initialize UDP connection if not already connected
            if not self.fpga_command_queue.is_connected():
                # TCP must be connected first
                QMessageBox.warning(
                    self,
                    "Connection Required",
                    "TCP connection must be established before UDP connection."
                )
                return
            self.on_udp_connect_clicked()
            import time
            time.sleep(0.5)  # Brief wait for UDP connection
        
        # Create and show sweep control widget (allows user to adjust parameters before starting)
        try:
            # Check if sweep control widget already exists
            if not hasattr(self, 'sweep_control_widget') or self.sweep_control_widget is None:
                # Create sweep control widget with existing connections
                # Pass the command queue and UDP manager so the widget uses existing connections
                self.sweep_control_widget = SMRSweepControlWidget(
                    tcp_socket=self.fpga_command_queue,  # Pass the command queue
                    udp_socket=self.udp_data_manager,  # Pass the UDP manager (not socket)
                    parent=self,
                    pySMR_widget=self,  # Pass reference to this widget for GUI updates
                    operator=self.operator  # Pass operator from main_gui if available
                )
            else:
                # Update connections if widget already exists (in case connections were reestablished)
                if self.fpga_command_queue.is_connected():
                    self.sweep_control_widget.tcp_command_queue = self.fpga_command_queue
                if self.udp_data_manager.is_connected():
                    self.sweep_control_widget.udp_socket = self.udp_data_manager
            
            # Create a dialog window to contain the control widget
            if not hasattr(self, 'sweep_control_dialog') or self.sweep_control_dialog is None:
                from PySide6.QtWidgets import QDialog, QVBoxLayout
                self.sweep_control_dialog = QDialog(self)
                self.sweep_control_dialog.setWindowTitle("SMR Sweep Controls")
                self.sweep_control_dialog.setMinimumSize(400, 400)  # Increased height to accommodate results section
                dialog_layout = QVBoxLayout(self.sweep_control_dialog)
                dialog_layout.setContentsMargins(10, 10, 10, 10)
                dialog_layout.setSpacing(10)
                
                # Add Results from last sweep section at the top
                self.sweep_results_section = self._create_sweep_results_section()
                dialog_layout.addWidget(self.sweep_results_section)
                
                # Add "View latest results" button (only show if results exist)
                self.view_results_button = QPushButton("View latest results")
                self.view_results_button.setStyleSheet("""
                    QPushButton {
                        background-color: #FF9800;
                        color: white;
                        font-size: 11pt;
                        font-weight: bold;
                        padding: 8px 16px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #F57C00;
                    }
                    QPushButton:pressed {
                        background-color: #E65100;
                    }
                """)
                self.view_results_button.clicked.connect(self._on_view_sweep_results_clicked)
                self.view_results_button.hide()  # Hidden by default
                dialog_layout.addWidget(self.view_results_button)
                
                dialog_layout.addWidget(self.sweep_control_widget)
            else:
                # Dialog exists - ensure results section exists and is at the top
                dialog_layout = self.sweep_control_dialog.layout()
                if not hasattr(self, 'sweep_results_section') or self.sweep_results_section is None:
                    # Results section doesn't exist, add it at the top
                    self.sweep_results_section = self._create_sweep_results_section()
                    dialog_layout.insertWidget(0, self.sweep_results_section)
            
            # Check for previous sweep settings and adjust frequency range and substrate bias if found
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
                self.sweep_control_widget.min_freq_spin.setValue(min_freq)
                self.sweep_control_widget.max_freq_spin.setValue(max_freq)
                
                # Update substrate bias if the widget has that control
                if hasattr(self.sweep_control_widget, 'substrate_bias_spin'):
                    self.sweep_control_widget.substrate_bias_spin.setValue(recent_bias)
                
                print(f"Set sweep frequency range to {min_freq:.0f} - {max_freq:.0f} Hz "
                      f"and substrate bias to {recent_bias:.1f} V "
                      f"based on previous sweep (resonant frequency: {recent_freq:.0f} Hz, Q: {recent_q:.0f})")
            
            # Update sweep results section with most recent data
            if hasattr(self, 'sweep_results_section') and self.sweep_results_section is not None:
                self._update_sweep_results_section(self.sweep_results_section)
            
            # Update "View latest results" button visibility
            # Check if we have a sweep window (either from last run or current widget)
            has_results = False
            if self.last_sweep_window is not None:
                has_results = True
            elif (hasattr(self, 'sweep_control_widget') and 
                  self.sweep_control_widget is not None and 
                  hasattr(self.sweep_control_widget, 'sweep_window') and
                  self.sweep_control_widget.sweep_window is not None):
                # Store reference to current sweep window
                self.last_sweep_window = self.sweep_control_widget.sweep_window
                has_results = True
            
            if hasattr(self, 'view_results_button'):
                self.view_results_button.setVisible(has_results)
            
            # Show the dialog
            self.sweep_control_dialog.show()
            self.sweep_control_dialog.raise_()
            self.sweep_control_dialog.activateWindow()
            
            # Mark as initialized and update buttons
            self.smr_initialized = True
            self._update_initialize_smr_button()
            self._update_set_delays_button()
            
        except Exception as e:
            print(f"Error showing SMR sweep controls: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to show SMR sweep controls: {str(e)}"
            )
    
    def _update_initialize_smr_button(self):
        """Update Initialize SMR button appearance based on initialization status."""
        if hasattr(self, 'initialize_smr_button'):
            if self.smr_initialized:
                # Gray when already initialized
                self.initialize_smr_button.setStyleSheet("""
                    QPushButton {
                        background-color: #CCCCCC;
                        color: #666666;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #BBBBBB;
                    }
                    QPushButton:pressed {
                        background-color: #AAAAAA;
                    }
                """)
            else:
                # Green when not initialized
                self.initialize_smr_button.setStyleSheet("""
                    QPushButton {
                        background-color: #4CAF50;
                        color: white;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #45a049;
                    }
                    QPushButton:pressed {
                        background-color: #3d8b40;
                    }
                """)
    
    def _update_set_delays_button(self):
        """Update Set Delays button appearance based on state."""
        if hasattr(self, 'set_delays_button'):
            if self.set_delays_run:
                # Gray when already run
                self.set_delays_button.setStyleSheet("""
                    QPushButton {
                        background-color: #CCCCCC;
                        color: #666666;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #BBBBBB;
                    }
                    QPushButton:pressed {
                        background-color: #AAAAAA;
                    }
                """)
            elif self.smr_initialized:
                # Blue when SMR is initialized but Set Delays hasn't been run yet (matches SMR Settings button)
                self.set_delays_button.setStyleSheet("""
                    QPushButton {
                        background-color: #2196F3;
                        color: white;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #1976D2;
                    }
                    QPushButton:pressed {
                        background-color: #1565C0;
                    }
                """)
            else:
                # Blue when SMR is not initialized (before Initialize SMR is run) - matches SMR Settings button
                self.set_delays_button.setStyleSheet("""
                    QPushButton {
                        background-color: #2196F3;
                        color: white;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #1976D2;
                    }
                    QPushButton:pressed {
                        background-color: #1565C0;
                    }
                """)
    
    def _update_select_sample_button(self, sample_name=None):
        """Update Select Sample button appearance based on state."""
        if hasattr(self, 'select_sample_button'):
            if self.selected_sample_path is not None:
                # Update text if sample name provided
                if sample_name:
                    self.select_sample_button.setText(f"Sample: {sample_name}")
                
                # Gray when sample is already selected
                self.select_sample_button.setStyleSheet("""
                    QPushButton {
                        background-color: #CCCCCC;
                        color: #666666;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #BBBBBB;
                    }
                    QPushButton:pressed {
                        background-color: #AAAAAA;
                    }
                """)
            else:
                # Reset text
                self.select_sample_button.setText("Select Sample")
                
                # Green when no sample is selected yet
                self.select_sample_button.setStyleSheet("""
                    QPushButton {
                        background-color: #4CAF50;
                        color: white;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #45a049;
                    }
                    QPushButton:pressed {
                        background-color: #3d8b40;
                    }
                """)
    
    def _update_start_saving_button(self):
        """Update Start saving button appearance based on state."""
        if hasattr(self, 'start_saving_button'):
            if self.is_saving:
                # Red when saving (button text is "Stop Saving")
                self.start_saving_button.setStyleSheet("""
                    QPushButton {
                        background-color: #f44336;
                        color: white;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #da190b;
                    }
                    QPushButton:pressed {
                        background-color: #c62828;
                    }
                """)
            elif self.selected_sample_path is not None:
                # Green when sample is selected but not saving yet
                self.start_saving_button.setStyleSheet("""
                    QPushButton {
                        background-color: #4CAF50;
                        color: white;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #45a049;
                    }
                    QPushButton:pressed {
                        background-color: #3d8b40;
                    }
                """)
            else:
                # Gray when no sample is selected
                self.start_saving_button.setStyleSheet("""
                    QPushButton {
                        background-color: #CCCCCC;
                        color: #666666;
                        font-size: 12pt;
                        font-weight: bold;
                        padding: 10px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #BBBBBB;
                    }
                    QPushButton:pressed {
                        background-color: #AAAAAA;
                    }
                """)
    
    def _create_push_settings_button(self):
        """Create and return a Push settings to FPGA button with standard styling."""
        push_settings_button = QPushButton("Push settings to FPGA")
        push_settings_button.clicked.connect(self.on_push_settings_clicked)
        push_settings_button.setEnabled(False)  # Disabled by default
        push_settings_button.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-size: 12pt;
                font-weight: bold;
                padding: 10px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
            QPushButton:pressed {
                background-color: #E65100;
            }
            QPushButton:disabled {
                background-color: #CCCCCC;
                color: #666666;
            }
        """)
        return push_settings_button
    
    def _create_sweep_results_section(self):
        """Create and return a widget showing results from the last sweep."""
        # Create container widget
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(10, 10, 10, 10)
        results_layout.setSpacing(5)
        
        # Title label
        title_label = QLabel("Results from last sweep:")
        title_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #333333;")
        results_layout.addWidget(title_label)
        
        # Create form layout for results
        form_layout = QFormLayout()
        form_layout.setSpacing(8)
        
        # Resonant frequency label
        freq_label = QLabel("N/A")
        freq_label.setStyleSheet("font-size: 11pt; color: #000000;")
        form_layout.addRow("Resonant frequency:", freq_label)
        
        # Q label
        q_label = QLabel("N/A")
        q_label.setStyleSheet("font-size: 11pt; color: #000000;")
        form_layout.addRow("Q:", q_label)
        
        # Substrate bias label
        bias_label = QLabel("N/A")
        bias_label.setStyleSheet("font-size: 11pt; color: #000000;")
        form_layout.addRow("Substrate bias:", bias_label)
        
        results_layout.addLayout(form_layout)
        
        # Store references for updating
        results_widget.freq_label = freq_label
        results_widget.q_label = q_label
        results_widget.bias_label = bias_label
        
        # Update with actual values
        self._update_sweep_results_section(results_widget)
        
        return results_widget
    
    def _update_sweep_results_section(self, results_widget):
        """Update the sweep results section with the most recent sweep data."""
        sweep_results = self._get_most_recent_sweep_results()
        
        if sweep_results is not None:
            frequency, q_value, substrate_bias = sweep_results
            # Format frequency (show in kHz if > 1000, otherwise Hz)
            if frequency >= 1000:
                freq_text = f"{frequency/1000:.3f} kHz"
            else:
                freq_text = f"{frequency:.1f} Hz"
            results_widget.freq_label.setText(freq_text)
            
            # Format Q (show as integer if whole number, otherwise with decimals)
            if q_value == int(q_value):
                q_text = f"{int(q_value)}"
            else:
                q_text = f"{q_value:.2f}"
            results_widget.q_label.setText(q_text)
            
            # Format substrate bias (show with 1 decimal place)
            results_widget.bias_label.setText(f"{substrate_bias:.1f} V")
        else:
            results_widget.freq_label.setText("N/A")
            results_widget.q_label.setText("N/A")
            results_widget.bias_label.setText("N/A")
    
    def _on_view_sweep_results_clicked(self):
        """Handle View latest results button click for Initialize SMR."""
        if self.last_sweep_window is not None:
            self.last_sweep_window.show()
            self.last_sweep_window.raise_()
            self.last_sweep_window.activateWindow()
        elif (hasattr(self, 'sweep_control_widget') and 
              self.sweep_control_widget is not None and 
              hasattr(self.sweep_control_widget, 'sweep_window') and
              self.sweep_control_widget.sweep_window is not None):
            # Use current sweep window and store it
            self.last_sweep_window = self.sweep_control_widget.sweep_window
            self.last_sweep_window.show()
            self.last_sweep_window.raise_()
            self.last_sweep_window.activateWindow()

    def on_set_delays_clicked(self):
        """Handle Set Delays button click - launch delay sweep window."""
        # Ensure TCP and UDP connections are established
        if not self.fpga_command_queue.is_connected():
            # Show connection dialog
            self.connection_dialog = FPGAConnectionDialog(self)
            self.connection_dialog.show()
            
            # Initialize TCP connection
            import threading
            def connect_thread():
                self.on_tcp_connect_clicked()
                self.on_udp_connect_clicked()
                # Wait a bit for connections to establish
                import time
                time.sleep(0.5)
            
            thread = threading.Thread(target=connect_thread, daemon=True)
            thread.start()
            thread.join(timeout=6.0)  # Wait up to 6 seconds for connection
            
            # Sync status after connection attempt
            self._sync_tcp_connection_status()
            
            # Dialog handles all error display - no separate warning needed
            if not self.fpga_command_queue.is_connected():
                return
        
        if not self.udp_data_manager.is_connected():
            # Initialize UDP connection if not already connected
            if not self.fpga_command_queue.is_connected():
                # TCP must be connected first
                QMessageBox.warning(
                    self,
                    "Connection Required",
                    "TCP connection must be established before UDP connection."
                )
                return
            self.on_udp_connect_clicked()
            import time
            time.sleep(0.5)  # Brief wait for UDP connection
        
        # Show Set Delays options dialog
        options_dialog = SetDelaysOptionsDialog(self)
        if options_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        set_bias, selected_settings = options_dialog.get_result()
        if not selected_settings:
            return
        
        # TODO: Use set_bias flag when implementing bias setting functionality
        if set_bias:
            print("Set Bias is enabled (functionality to be implemented)")
        
        # Convert settings dictionary to parameters
        from helper_functions.SMR_set_delays import (
            _convert_settings_dict_to_params,
            _apply_set_delays_overrides,
        )
        from helper_functions.SMR_sweep_frequencies import (
            _map_parameters_to_register_args,
            generate_set_all_parameters_string,
        )
        
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
                future = self.fpga_command_queue.submit_command(
                    command=command, wait_response=True, timeout=1.0
                )
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
        
        # Update SMR settings widget with initial settings (before starting delay sweep)
        try:
            from helper_functions.SMR_set_delays import _convert_params_to_settings_dict
            settings_dict = _convert_params_to_settings_dict(params)
            widget = self._ensure_smr_settings_widget()
            self._load_settings_into_widget(widget, settings_dict)
            # Also sync quick controls
            self._sync_quick_controls_from_widget()
        except Exception as e:
            print(f"Warning: Error updating SMR settings widget with initial settings: {e}")
        
        # Create and show delay sweep window
        if not hasattr(self, 'set_delays_window') or self.set_delays_window is None:
            self.set_delays_window = SetDelaysWindow(
                tcp_queue=self.fpga_command_queue,
                udp_manager=self.udp_data_manager,
                parent=self,
                pySMR_widget=self
            )
        
        # Set automated setup mode flag if in automated setup (window might be reused or just created)
        if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
            self.set_delays_window._automated_setup_mode = True
            if hasattr(self, '_automated_setup_main_window'):
                self.set_delays_window._automated_setup_main_window = self._automated_setup_main_window
        
        # Start the delay sweep
        # Pass both the overridden params and the original settings_params (before overrides)
        # Also pass set_bias flag
        self.set_delays_window.start_delay_sweep(
            params, 
            smr_driver_id, 
            original_settings_params=settings_params,
            set_bias=set_bias
        )
        
        # Show the window
        # If automated setup status window exists, ensure it stays on top
        if hasattr(self, '_automated_setup_status_window') and self._automated_setup_status_window:
            # Set parent to status window so dialog appears below it
            self.set_delays_window.setParent(self._automated_setup_status_window)
            self.set_delays_window.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.WindowTitleHint |
                Qt.WindowType.WindowMinMaxButtonsHint |
                Qt.WindowType.WindowCloseButtonHint
            )
        self.set_delays_window.show()
        self.set_delays_window.raise_()
        self.set_delays_window.activateWindow()
        # Ensure status window is raised after set delays window is shown
        if hasattr(self, '_automated_setup_status_window') and self._automated_setup_status_window:
            QTimer.singleShot(100, lambda: self._automated_setup_status_window.raise_())
        
        # Mark as run and update button
        self.set_delays_run = True
        self._update_set_delays_button()
    
    def run_set_delays_with_settings(self, selected_settings: dict, set_bias: bool = True):
        """Run set delays programmatically with provided settings.
        
        Args:
            selected_settings: Dictionary containing settings to use
            set_bias: Whether to set bias (default True)
        """
        # Ensure TCP and UDP connections are established
        if not self.fpga_command_queue.is_connected():
            # Show connection dialog
            self.connection_dialog = FPGAConnectionDialog(self)
            self.connection_dialog.show()
            
            # Initialize TCP connection
            import threading
            def connect_thread():
                self.on_tcp_connect_clicked()
                self.on_udp_connect_clicked()
                # Wait a bit for connections to establish
                import time
                time.sleep(0.5)
            
            thread = threading.Thread(target=connect_thread, daemon=True)
            thread.start()
            thread.join(timeout=6.0)  # Wait up to 6 seconds for connection
            
            # Sync status after connection attempt
            self._sync_tcp_connection_status()
            
            # Dialog handles all error display - no separate warning needed
            if not self.fpga_command_queue.is_connected():
                return False
        
        if not self.udp_data_manager.is_connected():
            # Initialize UDP connection if not already connected
            if not self.fpga_command_queue.is_connected():
                # TCP must be connected first
                QMessageBox.warning(
                    self,
                    "Connection Required",
                    "TCP connection must be established before UDP connection."
                )
                return False
            self.on_udp_connect_clicked()
            import time
            time.sleep(0.5)  # Brief wait for UDP connection
        
        if not selected_settings:
            return False
        
        # Convert settings dictionary to parameters
        from helper_functions.SMR_set_delays import (
            _convert_settings_dict_to_params,
            _apply_set_delays_overrides,
        )
        from helper_functions.SMR_sweep_frequencies import (
            _map_parameters_to_register_args,
            generate_set_all_parameters_string,
        )
        
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
                future = self.fpga_command_queue.submit_command(
                    command=command, wait_response=True, timeout=1.0
                )
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
        
        # Update SMR settings widget with initial settings (before starting delay sweep)
        try:
            from helper_functions.SMR_set_delays import _convert_params_to_settings_dict
            settings_dict = _convert_params_to_settings_dict(params)
            widget = self._ensure_smr_settings_widget()
            self._load_settings_into_widget(widget, settings_dict)
            # Also sync quick controls
            self._sync_quick_controls_from_widget()
        except Exception as e:
            print(f"Warning: Error updating SMR settings widget with initial settings: {e}")
        
        # Create and show delay sweep window
        if not hasattr(self, 'set_delays_window') or self.set_delays_window is None:
            self.set_delays_window = SetDelaysWindow(
                tcp_queue=self.fpga_command_queue,
                udp_manager=self.udp_data_manager,
                parent=self,
                pySMR_widget=self
            )
        
        # ALWAYS set automated setup mode flag if in automated setup (window might be reused or just created)
        # IMPORTANT: Set this BEFORE start_delay_sweep is called to ensure flag is available throughout the sweep
        # This must run even if window already exists, as it might have been created in manual mode
        if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
            self.set_delays_window._automated_setup_mode = True
            if hasattr(self, '_automated_setup_main_window'):
                self.set_delays_window._automated_setup_main_window = self._automated_setup_main_window
        else:
            # Clear flag on window if we're not in automated setup mode (in case window was reused)
            if hasattr(self, 'set_delays_window') and self.set_delays_window is not None:
                if hasattr(self.set_delays_window, '_automated_setup_mode'):
                    self.set_delays_window._automated_setup_mode = False
        
        # Final verification: ensure flag is set before starting delay sweep
        if hasattr(self, '_automated_setup_mode') and self._automated_setup_mode:
            if not (hasattr(self.set_delays_window, '_automated_setup_mode') and self.set_delays_window._automated_setup_mode):
                self.set_delays_window._automated_setup_mode = True
                if hasattr(self, '_automated_setup_main_window'):
                    self.set_delays_window._automated_setup_main_window = self._automated_setup_main_window
        
        # Start the delay sweep
        # Pass both the overridden params and the original settings_params (before overrides)
        # Also pass set_bias flag
        self.set_delays_window.start_delay_sweep(
            params, 
            smr_driver_id, 
            original_settings_params=settings_params,
            set_bias=set_bias
        )
        
        # Show the window
        # If automated setup status window exists, ensure it stays on top
        if hasattr(self, '_automated_setup_status_window') and self._automated_setup_status_window:
            # Set parent to status window so dialog appears below it
            self.set_delays_window.setParent(self._automated_setup_status_window)
            self.set_delays_window.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.WindowTitleHint |
                Qt.WindowType.WindowMinMaxButtonsHint |
                Qt.WindowType.WindowCloseButtonHint
            )
        self.set_delays_window.show()
        self.set_delays_window.raise_()
        self.set_delays_window.activateWindow()
        # Ensure status window is raised after set delays window is shown
        if hasattr(self, '_automated_setup_status_window') and self._automated_setup_status_window:
            QTimer.singleShot(100, lambda: self._automated_setup_status_window.raise_())
        
        # Mark as run and update button
        self.set_delays_run = True
        self._update_set_delays_button()
        
        return True
    
    def on_smr_settings_clicked(self):
        """Handle SMR settings button click - show popup window with FPGA parameter widget."""
        # Ensure TCP and UDP connections are established
        if not self.fpga_command_queue.is_connected():
            # Show connection dialog
            self.connection_dialog = FPGAConnectionDialog(self)
            self.connection_dialog.show()
            
            # Initialize TCP connection
            import threading
            def connect_thread():
                self.on_tcp_connect_clicked()
                self.on_udp_connect_clicked()
                # Wait a bit for connections to establish
                import time
                time.sleep(0.5)
            
            thread = threading.Thread(target=connect_thread, daemon=True)
            thread.start()
            thread.join(timeout=6.0)  # Wait up to 6 seconds for connection
            
            # Sync status after connection attempt
            self._sync_tcp_connection_status()
            
            # Dialog handles all error display - no separate warning needed
            if not self.fpga_command_queue.is_connected():
                return
        
        if self.smr_settings_window is None:
            # Create popup dialog window
            self.smr_settings_window = QDialog(self)
            self.smr_settings_window.setWindowTitle("SMR Settings")
            self.smr_settings_window.setMinimumSize(1200, 900)  # 100px taller (was 800)

            # Create layout for dialog
            dialog_layout = QVBoxLayout(self.smr_settings_window)
            dialog_layout.setContentsMargins(10, 10, 10, 10)

            # Add Load and Save buttons at the top
            load_save_layout = QHBoxLayout()
            load_save_layout.setSpacing(10)
            
            load_settings_button = QPushButton("Load Settings")
            load_settings_button.clicked.connect(self.on_load_smr_settings_clicked)
            load_settings_button.setStyleSheet("""
                QPushButton {
                    background-color: #2196F3;
                    color: white;
                    font-size: 11pt;
                    font-weight: bold;
                    padding: 8px 16px;
                    border-radius: 5px;
                }
                QPushButton:hover {
                    background-color: #1976D2;
                }
                QPushButton:pressed {
                    background-color: #1565C0;
                }
            """)
            load_save_layout.addWidget(load_settings_button)
            
            save_settings_button = QPushButton("Save Settings")
            save_settings_button.clicked.connect(self.on_save_smr_settings_clicked)
            save_settings_button.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    font-size: 11pt;
                    font-weight: bold;
                    padding: 8px 16px;
                    border-radius: 5px;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
                QPushButton:pressed {
                    background-color: #3d8b40;
                }
            """)
            load_save_layout.addWidget(save_settings_button)
            
            load_save_layout.addStretch()
            dialog_layout.addLayout(load_save_layout)

            # Create FPGA parameter widget
            self.smr_settings_widget = FPGAParameterWidget(self.smr_settings_window)
            dialog_layout.addWidget(self.smr_settings_widget)

            # Add Push settings to FPGA button at the bottom
            push_settings_button = self._create_push_settings_button()
            dialog_layout.addWidget(push_settings_button)

            # Store reference to push button in the window
            self.smr_settings_window.push_settings_button = push_settings_button

            # Sync quick controls with widget values
            self._sync_quick_controls_from_widget()
        
        # Sync quick controls with widget values (in case they changed in popup)
        self._sync_quick_controls_from_widget()
        
        # Update button state based on TCP connection
        if hasattr(self.smr_settings_window, 'push_settings_button'):
            is_tcp_connected = self.fpga_command_queue.is_connected()
            self.smr_settings_window.push_settings_button.setEnabled(is_tcp_connected)
        
        # Show the window
        self.smr_settings_window.show()
        self.smr_settings_window.raise_()
        self.smr_settings_window.activateWindow()
    
    def _get_fpga_parameters_from_widget(self, widget):
        """Get FPGA parameters dictionary from FPGAParameterWidget.
        
        Args:
            widget: FPGAParameterWidget instance
            
        Returns:
            Dictionary of FPGA parameter names to values.
        """
        return {
            "smr_driver_id": widget.smr_driver_id.value(),
            "Run": widget.run_check.isChecked(),
            "Enable_AGC": widget.enable_agc_check.isChecked(),
            "Send_data_to_pc": widget.send_data_to_pc_check.isChecked(),
            "Run_NCO_at_fixed_freq": widget.run_nco_at_fixed_freq_check.isChecked(),
            "Impulse": widget.impulse_check.isChecked(),
            "Input_source": widget.input_source_combo.currentText(),
            "Signal_of_interest": widget.signal_of_interest_combo.currentText(),
            "DAC_A_output": widget.dac_a_output_combo.currentText(),
            "DAC_B_output": widget.dac_b_output_combo.currentText(),
            "PLL_datarate_decimation": widget.pll_datarate_decimation.currentText(),
            "Frequency": widget.frequency.value(),
            "Minimum_frequency": widget.minimum_frequency.value(),
            "Maximum_frequency": widget.maximum_frequency.value(),
            "CIC_rate": widget.cic_rate.value(),
            "CIC_bit_shift": widget.cic_bit_shift.value(),
            "PLL_delay": widget.pll_delay.value(),
            "PLL_drive_amplitude": widget.pll_drive_amplitude.value(),
            "Feedback_delay": widget.feedback_delay.value(),
            "Feedback_gain": widget.feedback_gain.value(),
            "Resonator_Q": widget.resonator_q.value(),
            "Loop_bandwidth": widget.loop_bandwidth.value(),
            "Loop_order": widget.loop_order.value(),
        }
    
    def _load_settings_into_widget(self, widget, settings_dict):
        """Load settings from dictionary into FPGAParameterWidget.
        
        Args:
            widget: FPGAParameterWidget instance
            settings_dict: Dictionary of setting names to string values
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
        
        # Block all signals to avoid triggering updates during bulk update
        # Update all checkboxes
        if "Run" in settings_dict:
            widget.run_check.blockSignals(True)
            widget.run_check.setChecked(str_to_bool(settings_dict["Run"]))
            widget.run_check.blockSignals(False)
        if "Enable_AGC" in settings_dict:
            widget.enable_agc_check.blockSignals(True)
            widget.enable_agc_check.setChecked(str_to_bool(settings_dict["Enable_AGC"]))
            widget.enable_agc_check.blockSignals(False)
        if "Send_data_to_pc" in settings_dict:
            widget.send_data_to_pc_check.blockSignals(True)
            widget.send_data_to_pc_check.setChecked(str_to_bool(settings_dict["Send_data_to_pc"]))
            widget.send_data_to_pc_check.blockSignals(False)
        if "Run_NCO_at_fixed_freq" in settings_dict:
            widget.run_nco_at_fixed_freq_check.blockSignals(True)
            widget.run_nco_at_fixed_freq_check.setChecked(str_to_bool(settings_dict["Run_NCO_at_fixed_freq"]))
            widget.run_nco_at_fixed_freq_check.blockSignals(False)
        if "Impulse" in settings_dict:
            widget.impulse_check.blockSignals(True)
            widget.impulse_check.setChecked(str_to_bool(settings_dict["Impulse"]))
            widget.impulse_check.blockSignals(False)
        
        # Update combo boxes (set by text)
        if "Input_source" in settings_dict:
            index = widget.input_source_combo.findText(settings_dict["Input_source"])
            if index >= 0:
                widget.input_source_combo.blockSignals(True)
                widget.input_source_combo.setCurrentIndex(index)
                widget.input_source_combo.blockSignals(False)
        if "Signal_of_interest" in settings_dict:
            index = widget.signal_of_interest_combo.findText(settings_dict["Signal_of_interest"])
            if index >= 0:
                widget.signal_of_interest_combo.blockSignals(True)
                widget.signal_of_interest_combo.setCurrentIndex(index)
                widget.signal_of_interest_combo.blockSignals(False)
        if "DAC_A_output" in settings_dict:
            index = widget.dac_a_output_combo.findText(settings_dict["DAC_A_output"])
            if index >= 0:
                widget.dac_a_output_combo.blockSignals(True)
                widget.dac_a_output_combo.setCurrentIndex(index)
                widget.dac_a_output_combo.blockSignals(False)
        if "DAC_B_output" in settings_dict:
            index = widget.dac_b_output_combo.findText(settings_dict["DAC_B_output"])
            if index >= 0:
                widget.dac_b_output_combo.blockSignals(True)
                widget.dac_b_output_combo.setCurrentIndex(index)
                widget.dac_b_output_combo.blockSignals(False)
        if "PLL_datarate_decimation" in settings_dict:
            # Ensure we treat the value as text, not as an index
            # The combo box items are ['1', '2', '4', '8', '16', '32']
            # We need to match by text value, not by index
            value_str = str(settings_dict["PLL_datarate_decimation"]).strip()
            
            # First try to match the text directly
            index = widget.pll_datarate_decimation.findText(value_str)
            if index >= 0:
                widget.pll_datarate_decimation.blockSignals(True)
                widget.pll_datarate_decimation.setCurrentIndex(index)
                widget.pll_datarate_decimation.blockSignals(False)
            else:
                # If findText fails, try converting to integer (handles "4.0" -> "4")
                # This prevents accidentally using a numeric value as an index
                try:
                    value_num = int(float(value_str))
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
                                print(f"Warning: PLL_datarate_decimation value '{value_str}' would map to index {value_num} "
                                      f"(item '{item_at_index}'), but expected '{value_str_from_num}'. "
                                      f"This suggests the CSV may have stored an index instead of a value.")
                        else:
                            print(f"Warning: PLL_datarate_decimation value '{value_str}' not found in combo box items.")
                except (ValueError, TypeError):
                    print(f"Warning: Could not parse PLL_datarate_decimation value '{value_str}'.")
        
        # Update all numeric fields
        if "smr_driver_id" in settings_dict:
            widget.smr_driver_id.blockSignals(True)
            widget.smr_driver_id.setValue(str_to_int(settings_dict["smr_driver_id"]))
            widget.smr_driver_id.blockSignals(False)
        if "Frequency" in settings_dict:
            widget.frequency.blockSignals(True)
            widget.frequency.setValue(str_to_float(settings_dict["Frequency"]))
            widget.frequency.blockSignals(False)
        if "Minimum_frequency" in settings_dict:
            widget.minimum_frequency.blockSignals(True)
            widget.minimum_frequency.setValue(str_to_float(settings_dict["Minimum_frequency"]))
            widget.minimum_frequency.blockSignals(False)
        if "Maximum_frequency" in settings_dict:
            widget.maximum_frequency.blockSignals(True)
            widget.maximum_frequency.setValue(str_to_float(settings_dict["Maximum_frequency"]))
            widget.maximum_frequency.blockSignals(False)
        if "CIC_rate" in settings_dict:
            widget.cic_rate.blockSignals(True)
            widget.cic_rate.setValue(str_to_int(settings_dict["CIC_rate"]))
            widget.cic_rate.blockSignals(False)
        if "CIC_bit_shift" in settings_dict:
            widget.cic_bit_shift.blockSignals(True)
            widget.cic_bit_shift.setValue(str_to_int(settings_dict["CIC_bit_shift"]))
            widget.cic_bit_shift.blockSignals(False)
        if "PLL_delay" in settings_dict:
            widget.pll_delay.blockSignals(True)
            widget.pll_delay.setValue(str_to_float(settings_dict["PLL_delay"]))
            widget.pll_delay.blockSignals(False)
        if "PLL_drive_amplitude" in settings_dict:
            widget.pll_drive_amplitude.blockSignals(True)
            widget.pll_drive_amplitude.setValue(str_to_float(settings_dict["PLL_drive_amplitude"]))
            widget.pll_drive_amplitude.blockSignals(False)
        if "Feedback_delay" in settings_dict:
            widget.feedback_delay.blockSignals(True)
            widget.feedback_delay.setValue(str_to_int(settings_dict["Feedback_delay"]))
            widget.feedback_delay.blockSignals(False)
        if "Feedback_gain" in settings_dict:
            widget.feedback_gain.blockSignals(True)
            widget.feedback_gain.setValue(str_to_float(settings_dict["Feedback_gain"]))
            widget.feedback_gain.blockSignals(False)
        if "Resonator_Q" in settings_dict:
            widget.resonator_q.blockSignals(True)
            widget.resonator_q.setValue(str_to_float(settings_dict["Resonator_Q"]))
            widget.resonator_q.blockSignals(False)
        if "Loop_bandwidth" in settings_dict:
            widget.loop_bandwidth.blockSignals(True)
            widget.loop_bandwidth.setValue(str_to_float(settings_dict["Loop_bandwidth"]))
            widget.loop_bandwidth.blockSignals(False)
        if "Loop_order" in settings_dict:
            widget.loop_order.blockSignals(True)
            widget.loop_order.setValue(str_to_int(settings_dict["Loop_order"]))
            widget.loop_order.blockSignals(False)
        
        # Trigger update to recalculate register values
        if hasattr(widget, 'update_values'):
            widget.update_values()
    
    def on_save_smr_settings_clicked(self):
        """Handle Save Settings button click in SMR settings window."""
        if self.smr_settings_widget is None:
            print("Error: SMR settings widget not available.")
            return
        
        # Get substrate bias from quick control if available, otherwise use default
        substrate_bias = 3.0  # Default
        if hasattr(self, 'substrate_bias_control'):
            substrate_bias = self.substrate_bias_control.get_value()
        
        # Get operator from main_gui (stored in self.operator)
        operator = self.operator if hasattr(self, 'operator') and self.operator else None
        
        # Get FPGA parameters from widget
        fpga_parameters = self._get_fpga_parameters_from_widget(self.smr_settings_widget)
        
        # Save settings with settings_type='user'
        success = write_smr_settings(
            settings_type="user",
            substrate_bias=substrate_bias,
            fpga_parameters=fpga_parameters,
            operator=operator,
        )
        
        if success:
            print("Settings saved successfully!")
        else:
            print("Error: Failed to save settings.")
    
    def on_load_smr_settings_clicked(self):
        """Handle Load Settings button click in SMR settings window."""
        if self.smr_settings_widget is None:
            print("Error: SMR settings widget not available.")
            return
        
        # Read all settings from CSV file
        settings_list = read_smr_settings()
        if not settings_list:
            print("No saved settings found.")
            return
        
        # Show dialog to select settings
        dialog = LoadSettingsDialog(settings_list, self.smr_settings_window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_settings = dialog.get_selected_settings()
            if selected_settings:
                # Load settings into widget
                self._load_settings_into_widget(self.smr_settings_widget, selected_settings)
                
                # Sync quick controls with widget values
                self._sync_quick_controls_from_widget()
                
                print("Settings loaded successfully!")
    
    def _sync_quick_controls_from_widget(self):
        """Sync quick controls with current widget values."""
        if self.smr_settings_widget is None:
            return
        
        # Temporarily disconnect signals to avoid triggering auto-push
        if hasattr(self, 'quick_run_checkbox'):
            self.quick_run_checkbox.blockSignals(True)
            self.quick_run_checkbox.setChecked(self.smr_settings_widget.run_check.isChecked())
            self.quick_run_checkbox.blockSignals(False)
        
        if hasattr(self, 'quick_pll_delay_control'):
            # Temporarily remove callback to avoid triggering auto-push
            old_callback = getattr(self.quick_pll_delay_control, '_value_changed_callback', None)
            self.quick_pll_delay_control._value_changed_callback = None
            self.quick_pll_delay_control.set_value(self.smr_settings_widget.pll_delay.value())
            self.quick_pll_delay_control._value_changed_callback = old_callback
        
        if hasattr(self, 'quick_pll_drive_amplitude_control'):
            # Temporarily remove callback to avoid triggering auto-push
            old_callback = getattr(self.quick_pll_drive_amplitude_control, '_value_changed_callback', None)
            self.quick_pll_drive_amplitude_control._value_changed_callback = None
            self.quick_pll_drive_amplitude_control.set_value(self.smr_settings_widget.pll_drive_amplitude.value())
            self.quick_pll_drive_amplitude_control._value_changed_callback = old_callback
    
    def on_push_settings_clicked(self):
        """Handle push settings button click - send parsed string to FPGA via TCP."""
        if not self.fpga_command_queue.is_connected():
            # This shouldn't happen if button is properly disabled, but check anyway
            if hasattr(self, 'push_settings_button'):
                self.push_settings_button.setEnabled(False)
            if hasattr(self, 'smr_settings_window') and self.smr_settings_window is not None:
                if hasattr(self.smr_settings_window, 'push_settings_button'):
                    self.smr_settings_window.push_settings_button.setEnabled(False)
            return
        
        if self.smr_settings_widget is None:
            # Settings window hasn't been opened yet
            if self.response_display is not None:
                self.response_display.setText("Please open SMR settings first.")
            return
        
        try:
            # Get the parsed string from the FPGA parameter widget
            parsed_string = self.smr_settings_widget.set_all_parameters_label.text()
            
            if not parsed_string:
                # No settings to send
                if self.response_display is not None:
                    self.response_display.setText("No settings to send. Please configure SMR settings first.")
                return
            
            # Send each line of the parsed string via TCP command queue
            # Each line should be sent with CRLF line ending
            lines = parsed_string.split('\n')
            commands_sent = 0
            futures = []
            for line in lines:
                if line.strip():  # Skip empty lines
                    # Add CRLF line ending
                    command = line.strip() + '\r\n'
                    # Submit command to queue
                    future = self.fpga_command_queue.submit_command(
                        command=command,
                        wait_response=True,
                        timeout=1.0
                    )
                    futures.append(future)
                    commands_sent += 1
            
            # Wait for all commands to complete and check for errors
            errors = []
            for i, future in enumerate(futures):
                try:
                    success, response = future.result(timeout=2.0)
                    if not success:
                        errors.append(f"Command {i+1} failed")
                except Exception as e:
                    errors.append(f"Command {i+1} error: {str(e)}")
            
            # Update TCP response display to show that settings were sent
            if self.response_display is not None:
                if errors:
                    error_msg = "\r\n".join(errors)
                    self.response_display.setText(
                        f"Settings sent with errors!\r\n"
                        f"Sent {commands_sent} register commands.\r\n"
                        f"Errors: {error_msg}"
                    )
                else:
                    self.response_display.setText(
                        f"Settings sent successfully!\r\n"
                        f"Sent {commands_sent} register commands.\r\n"
                        f"Last sent: {lines[-1] if lines else 'N/A'}"
                    )
            
            # Update Run, PLL delay, and PLL drive amplitude controls to match SMR settings
            self._sync_quick_controls_from_widget()
            
            # Close the SMR settings window after successfully pushing settings (only if no errors)
            if not errors:
                if hasattr(self, 'smr_settings_window') and self.smr_settings_window is not None:
                    self.smr_settings_window.close()
            
        except Exception as e:
            # Update response display with error
            if self.response_display is not None:
                self.response_display.setText(f"Error sending settings: {str(e)}")
            # Connection may be lost
            if not self.fpga_command_queue.is_connected():
                if hasattr(self, 'push_settings_button'):
                    self.push_settings_button.setEnabled(False)
                if hasattr(self, 'smr_settings_window') and self.smr_settings_window is not None:
                    if hasattr(self.smr_settings_window, 'push_settings_button'):
                        self.smr_settings_window.push_settings_button.setEnabled(False)
                self.tcp_connection_status = "disconnected"
                self._update_connection_indicators()
    
    def _ensure_smr_settings_widget(self):
        """Ensure SMR settings widget exists (create if needed, but don't show window)."""
        if self.smr_settings_widget is None:
            # Create widget without showing window
            if self.smr_settings_window is None:
                self.smr_settings_window = QDialog(self)
                self.smr_settings_window.setWindowTitle("SMR Settings")
                self.smr_settings_window.setMinimumSize(1200, 900)
                dialog_layout = QVBoxLayout(self.smr_settings_window)
                dialog_layout.setContentsMargins(10, 10, 10, 10)
                
                # Add Load and Save buttons at the top
                load_save_layout = QHBoxLayout()
                load_save_layout.setSpacing(10)
                
                load_settings_button = QPushButton("Load Settings")
                load_settings_button.clicked.connect(self.on_load_smr_settings_clicked)
                load_settings_button.setStyleSheet("""
                    QPushButton {
                        background-color: #2196F3;
                        color: white;
                        font-size: 11pt;
                        font-weight: bold;
                        padding: 8px 16px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #1976D2;
                    }
                    QPushButton:pressed {
                        background-color: #1565C0;
                    }
                """)
                load_save_layout.addWidget(load_settings_button)
                
                save_settings_button = QPushButton("Save Settings")
                save_settings_button.clicked.connect(self.on_save_smr_settings_clicked)
                save_settings_button.setStyleSheet("""
                    QPushButton {
                        background-color: #4CAF50;
                        color: white;
                        font-size: 11pt;
                        font-weight: bold;
                        padding: 8px 16px;
                        border-radius: 5px;
                    }
                    QPushButton:hover {
                        background-color: #45a049;
                    }
                    QPushButton:pressed {
                        background-color: #3d8b40;
                    }
                """)
                load_save_layout.addWidget(save_settings_button)
                
                load_save_layout.addStretch()
                dialog_layout.addLayout(load_save_layout)
                
                self.smr_settings_widget = FPGAParameterWidget(self.smr_settings_window)
                dialog_layout.addWidget(self.smr_settings_widget)
                
                # Add Push settings to FPGA button at the bottom
                push_settings_button = self._create_push_settings_button()
                dialog_layout.addWidget(push_settings_button)
                
                # Store reference to push button in the window
                self.smr_settings_window.push_settings_button = push_settings_button
            else:
                # Window exists but widget might not
                if not hasattr(self.smr_settings_window, 'layout') or self.smr_settings_window.layout() is None:
                    dialog_layout = QVBoxLayout(self.smr_settings_window)
                    dialog_layout.setContentsMargins(10, 10, 10, 10)
                    
                    # Add Load and Save buttons at the top
                    load_save_layout = QHBoxLayout()
                    load_save_layout.setSpacing(10)
                    
                    load_settings_button = QPushButton("Load Settings")
                    load_settings_button.clicked.connect(self.on_load_smr_settings_clicked)
                    load_settings_button.setStyleSheet("""
                        QPushButton {
                            background-color: #2196F3;
                            color: white;
                            font-size: 11pt;
                            font-weight: bold;
                            padding: 8px 16px;
                            border-radius: 5px;
                        }
                        QPushButton:hover {
                            background-color: #1976D2;
                        }
                        QPushButton:pressed {
                            background-color: #1565C0;
                        }
                    """)
                    load_save_layout.addWidget(load_settings_button)
                    
                    save_settings_button = QPushButton("Save Settings")
                    save_settings_button.clicked.connect(self.on_save_smr_settings_clicked)
                    save_settings_button.setStyleSheet("""
                        QPushButton {
                            background-color: #4CAF50;
                            color: white;
                            font-size: 11pt;
                            font-weight: bold;
                            padding: 8px 16px;
                            border-radius: 5px;
                        }
                        QPushButton:hover {
                            background-color: #45a049;
                        }
                        QPushButton:pressed {
                            background-color: #3d8b40;
                        }
                    """)
                    load_save_layout.addWidget(save_settings_button)
                    
                    load_save_layout.addStretch()
                    dialog_layout.addLayout(load_save_layout)
                    
                    self.smr_settings_widget = FPGAParameterWidget(self.smr_settings_window)
                    dialog_layout.addWidget(self.smr_settings_widget)
                    
                    # Add Push settings to FPGA button at the bottom
                    push_settings_button = self._create_push_settings_button()
                    dialog_layout.addWidget(push_settings_button)
                    
                    # Store reference to push button in the window
                    self.smr_settings_window.push_settings_button = push_settings_button
                else:
                    # Find existing widget
                    layout = self.smr_settings_window.layout()
                    for i in range(layout.count()):
                        item = layout.itemAt(i)
                        if item and item.widget() and isinstance(item.widget(), FPGAParameterWidget):
                            self.smr_settings_widget = item.widget()
                            break
                    if self.smr_settings_widget is None:
                        self.smr_settings_widget = FPGAParameterWidget(self.smr_settings_window)
                        layout.addWidget(self.smr_settings_widget)
                    
                    # Ensure Load/Save buttons exist (check if they're already in the layout)
                    has_load_save_buttons = False
                    for i in range(layout.count()):
                        item = layout.itemAt(i)
                        if item and item.widget() and isinstance(item.widget(), QPushButton):
                            button_text = item.widget().text()
                            if button_text in ("Load Settings", "Save Settings"):
                                has_load_save_buttons = True
                                break
                    
                    if not has_load_save_buttons:
                        # Add Load and Save buttons at the top (insert before widget)
                        load_save_layout = QHBoxLayout()
                        load_save_layout.setSpacing(10)
                        
                        load_settings_button = QPushButton("Load Settings")
                        load_settings_button.clicked.connect(self.on_load_smr_settings_clicked)
                        load_settings_button.setStyleSheet("""
                            QPushButton {
                                background-color: #2196F3;
                                color: white;
                                font-size: 11pt;
                                font-weight: bold;
                                padding: 8px 16px;
                                border-radius: 5px;
                            }
                            QPushButton:hover {
                                background-color: #1976D2;
                            }
                            QPushButton:pressed {
                                background-color: #1565C0;
                            }
                        """)
                        load_save_layout.addWidget(load_settings_button)
                        
                        save_settings_button = QPushButton("Save Settings")
                        save_settings_button.clicked.connect(self.on_save_smr_settings_clicked)
                        save_settings_button.setStyleSheet("""
                            QPushButton {
                                background-color: #4CAF50;
                                color: white;
                                font-size: 11pt;
                                font-weight: bold;
                                padding: 8px 16px;
                                border-radius: 5px;
                            }
                            QPushButton:hover {
                                background-color: #45a049;
                            }
                            QPushButton:pressed {
                                background-color: #3d8b40;
                            }
                        """)
                        load_save_layout.addWidget(save_settings_button)
                        
                        load_save_layout.addStretch()
                        # Insert at position 0 (before widget)
                        layout.insertLayout(0, load_save_layout)
                    
                    # Ensure push button exists
                    if not hasattr(self.smr_settings_window, 'push_settings_button'):
                        push_settings_button = self._create_push_settings_button()
                        layout.addWidget(push_settings_button)
                        self.smr_settings_window.push_settings_button = push_settings_button
            
            # Sync widget values from quick controls if they exist
            if hasattr(self, 'quick_run_checkbox'):
                self.smr_settings_widget.run_check.blockSignals(True)
                self.smr_settings_widget.run_check.setChecked(self.quick_run_checkbox.isChecked())
                self.smr_settings_widget.run_check.blockSignals(False)
            if hasattr(self, 'quick_pll_delay_control'):
                self.smr_settings_widget.pll_delay.blockSignals(True)
                self.smr_settings_widget.pll_delay.setValue(self.quick_pll_delay_control.get_value())
                self.smr_settings_widget.pll_delay.blockSignals(False)
            if hasattr(self, 'quick_pll_drive_amplitude_control'):
                self.smr_settings_widget.pll_drive_amplitude.blockSignals(True)
                self.smr_settings_widget.pll_drive_amplitude.setValue(self.quick_pll_drive_amplitude_control.get_value())
                self.smr_settings_widget.pll_drive_amplitude.blockSignals(False)
            # Update widget to recalculate register values
            self.smr_settings_widget.update_values()
        else:
            # Widget exists, but ensure Load/Save buttons and push button exist
            if self.smr_settings_window is not None:
                layout = self.smr_settings_window.layout()
                if layout is not None:
                    # Check if Load/Save buttons exist
                    has_load_save_buttons = False
                    for i in range(layout.count()):
                        item = layout.itemAt(i)
                        if item and item.layout():
                            # Check if this layout contains Load/Save buttons
                            layout_item = item.layout()
                            for j in range(layout_item.count()):
                                widget_item = layout_item.itemAt(j)
                                if widget_item and widget_item.widget() and isinstance(widget_item.widget(), QPushButton):
                                    button_text = widget_item.widget().text()
                                    if button_text in ("Load Settings", "Save Settings"):
                                        has_load_save_buttons = True
                                        break
                                if has_load_save_buttons:
                                    break
                        if has_load_save_buttons:
                            break
                    
                    if not has_load_save_buttons:
                        # Add Load and Save buttons at the top
                        load_save_layout = QHBoxLayout()
                        load_save_layout.setSpacing(10)
                        
                        load_settings_button = QPushButton("Load Settings")
                        load_settings_button.clicked.connect(self.on_load_smr_settings_clicked)
                        load_settings_button.setStyleSheet("""
                            QPushButton {
                                background-color: #2196F3;
                                color: white;
                                font-size: 11pt;
                                font-weight: bold;
                                padding: 8px 16px;
                                border-radius: 5px;
                            }
                            QPushButton:hover {
                                background-color: #1976D2;
                            }
                            QPushButton:pressed {
                                background-color: #1565C0;
                            }
                        """)
                        load_save_layout.addWidget(load_settings_button)
                        
                        save_settings_button = QPushButton("Save Settings")
                        save_settings_button.clicked.connect(self.on_save_smr_settings_clicked)
                        save_settings_button.setStyleSheet("""
                            QPushButton {
                                background-color: #4CAF50;
                                color: white;
                                font-size: 11pt;
                                font-weight: bold;
                                padding: 8px 16px;
                                border-radius: 5px;
                            }
                            QPushButton:hover {
                                background-color: #45a049;
                            }
                            QPushButton:pressed {
                                background-color: #3d8b40;
                            }
                        """)
                        load_save_layout.addWidget(save_settings_button)
                        
                        load_save_layout.addStretch()
                        layout.insertLayout(0, load_save_layout)
                    
                    # Ensure push button exists
                    if not hasattr(self.smr_settings_window, 'push_settings_button'):
                        push_settings_button = self._create_push_settings_button()
                        layout.addWidget(push_settings_button)
                        self.smr_settings_window.push_settings_button = push_settings_button
        return self.smr_settings_widget
    
    def _push_single_register(self, register_name, setting_constant):
        """Push a single register value to FPGA via TCP."""
        if not self.fpga_command_queue.is_connected():
            return False
        
        try:
            # Ensure widget exists
            widget = self._ensure_smr_settings_widget()
            
            # Get current register values
            register_values = calculate_register_values(
                Run=widget.run_check.isChecked(),
                Enable_AGC=widget.enable_agc_check.isChecked(),
                Send_data_to_pc=widget.send_data_to_pc_check.isChecked(),
                Run_NCO_at_fixed_freq=widget.run_nco_at_fixed_freq_check.isChecked(),
                Impulse=widget.impulse_check.isChecked(),
                Input_source=widget.input_source_combo.currentIndex(),
                Signal_of_interest=widget.signal_of_interest_combo.currentIndex(),
                DAC_A_output=widget.dac_a_output_combo.currentIndex(),
                DAC_B_output=widget.dac_b_output_combo.currentIndex(),
                PLL_datarate_decimation=widget.pll_datarate_decimation.currentIndex(),
                Frequency=widget.frequency.value(),
                Minimum_frequency=widget.minimum_frequency.value(),
                Maximum_frequency=widget.maximum_frequency.value(),
                CIC_rate=widget.cic_rate.value(),
                CIC_bit_shift=widget.cic_bit_shift.value(),
                PLL_delay=widget.pll_delay.value(),
                PLL_drive_amplitude=widget.pll_drive_amplitude.value(),
                Feedback_delay=widget.feedback_delay.value(),
                Feedback_gain=widget.feedback_gain.value(),
                Resonator_Q=widget.resonator_q.value(),
                Loop_bandwidth=widget.loop_bandwidth.value(),
                Loop_order=widget.loop_order.value()
            )
            
            # Get register value
            register_value = register_values.get(register_name, 0)
            
            # Calculate register ID
            smr_driver_id = widget.smr_driver_id.value()
            smr_driver_id_offset = smr_driver_id * (2 ** 8)
            register_id = smr_driver_id_offset + setting_constant
            
            # Send command via queue
            command = f"Pw{register_id},{register_value}\r\n"
            future = self.fpga_command_queue.submit_command(
                command=command,
                wait_response=True,
                timeout=1.0
            )
            
            # Wait for result (non-blocking check with timeout)
            try:
                success, response = future.result(timeout=2.0)
                return success
            except Exception as e:
                # Connection may be lost
                if not self.fpga_command_queue.is_connected():
                    self.tcp_connection_status = "disconnected"
                    if hasattr(self, 'push_settings_button'):
                        self.push_settings_button.setEnabled(False)
                    if self.response_display is not None:
                        self.response_display.setText(f"Connection lost: {str(e)}")
                    self._update_connection_indicators()
                return False
            
        except Exception as e:
            # Silently fail for auto-push (don't spam error messages)
            return False
    
    def on_quick_run_changed(self, checked):
        """Handle Run checkbox change - update widget and push to FPGA."""
        self.quick_run_value = checked
        
        # Update SMR settings widget if it exists
        widget = self._ensure_smr_settings_widget()
        # Block signals to avoid triggering widget's update_values multiple times
        widget.run_check.blockSignals(True)
        widget.run_check.setChecked(checked)
        widget.run_check.blockSignals(False)
        # Trigger update to recalculate register values
        widget.update_values()
        
        # Push to FPGA automatically if connected
        if self.fpga_command_queue.is_connected():
            # smr_driver_mode register (setting constant 0)
            self._push_single_register('smr_driver_mode', 0)
    
    def on_quick_pll_delay_changed(self, value):
        """Handle PLL delay spinbox change - update widget and push to FPGA."""
        self.quick_pll_delay_value = value
        
        # Update SMR settings widget if it exists
        widget = self._ensure_smr_settings_widget()
        # Block signals to avoid triggering widget's update_values multiple times
        widget.pll_delay.blockSignals(True)
        widget.pll_delay.setValue(value)
        widget.pll_delay.blockSignals(False)
        # Trigger update to recalculate register values
        widget.update_values()
        
        # Push to FPGA automatically if connected
        if self.fpga_command_queue.is_connected():
            # delay register (setting constant 5)
            self._push_single_register('delay', 5)
    
    def on_quick_pll_drive_amplitude_changed(self, value):
        """Handle PLL drive amplitude spinbox change - update widget and push to FPGA."""
        self.quick_pll_drive_amplitude_value = value
        
        # Update SMR settings widget if it exists
        widget = self._ensure_smr_settings_widget()
        # Block signals to avoid triggering widget's update_values multiple times
        widget.pll_drive_amplitude.blockSignals(True)
        widget.pll_drive_amplitude.setValue(value)
        widget.pll_drive_amplitude.blockSignals(False)
        # Trigger update to recalculate register values
        widget.update_values()
        
        # Push to FPGA automatically if connected
        if self.fpga_command_queue.is_connected():
            # nco_gain register (setting constant 19)
            self._push_single_register('nco_gain', 19)
    
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
    
    def on_peak_detection_settings_clicked(self):
        """Handle Peak Detection Settings button click - show settings dialog."""
        if self.peak_detection_settings_window is None:
            # Create popup dialog window
            self.peak_detection_settings_window = QDialog(self)
            self.peak_detection_settings_window.setWindowTitle("Peak Detection Settings")
            self.peak_detection_settings_window.setMinimumSize(500, 600)
            
            # Create layout for dialog
            dialog_layout = QVBoxLayout(self.peak_detection_settings_window)
            dialog_layout.setContentsMargins(10, 10, 10, 10)
            
            # Settings form
            settings_group = QGroupBox("Peak Detection Parameters")
            settings_layout = QFormLayout()
            
            # Detection Threshold
            detection_threshold_spin = QDoubleSpinBox()
            detection_threshold_spin.setMinimum(-1000.0)
            detection_threshold_spin.setMaximum(0.0)
            detection_threshold_spin.setValue(self.peak_detection_settings.detection_threshold)
            detection_threshold_spin.setSuffix(" Hz")
            detection_threshold_spin.setDecimals(2)
            settings_layout.addRow("Detection Threshold:", detection_threshold_spin)
            
            # Doublet Gap Min
            doublet_gap_min_spin = QDoubleSpinBox()
            doublet_gap_min_spin.setMinimum(0.0)
            doublet_gap_min_spin.setMaximum(0.1)
            doublet_gap_min_spin.setValue(self.peak_detection_settings.doublet_gap_min)
            doublet_gap_min_spin.setSuffix(" s")
            doublet_gap_min_spin.setDecimals(6)
            settings_layout.addRow("Doublet Gap Min:", doublet_gap_min_spin)
            
            # Doublet Gap Max
            doublet_gap_max_spin = QDoubleSpinBox()
            doublet_gap_max_spin.setMinimum(0.0)
            doublet_gap_max_spin.setMaximum(0.1)
            doublet_gap_max_spin.setValue(self.peak_detection_settings.doublet_gap_max)
            doublet_gap_max_spin.setSuffix(" s")
            doublet_gap_max_spin.setDecimals(6)
            settings_layout.addRow("Doublet Gap Max:", doublet_gap_max_spin)
            
            # Max Height Diff
            max_height_diff_spin = QDoubleSpinBox()
            max_height_diff_spin.setMinimum(0.0)
            max_height_diff_spin.setMaximum(1000.0)
            max_height_diff_spin.setValue(self.peak_detection_settings.max_height_diff)
            max_height_diff_spin.setSuffix(" Hz")
            max_height_diff_spin.setDecimals(2)
            settings_layout.addRow("Max Height Diff (abs):", max_height_diff_spin)
            
            # Max Percent Diff
            max_percent_diff_spin = QDoubleSpinBox()
            max_percent_diff_spin.setMinimum(0.0)
            max_percent_diff_spin.setMaximum(100.0)
            max_percent_diff_spin.setValue(self.peak_detection_settings.max_percent_diff)
            max_percent_diff_spin.setSuffix(" %")
            max_percent_diff_spin.setDecimals(1)
            settings_layout.addRow("Max Percent Diff:", max_percent_diff_spin)
            
            # Baseline Search Width
            baseline_search_width_spin = QDoubleSpinBox()
            baseline_search_width_spin.setMinimum(0.01)
            baseline_search_width_spin.setMaximum(10.0)
            baseline_search_width_spin.setValue(self.peak_detection_settings.baseline_search_width)
            baseline_search_width_spin.setSuffix(" s")
            baseline_search_width_spin.setDecimals(3)
            settings_layout.addRow("Baseline Search Width:", baseline_search_width_spin)
            
            # Filter Width
            filter_width_spin = QSpinBox()
            filter_width_spin.setMinimum(5)
            filter_width_spin.setMaximum(1001)
            filter_width_spin.setValue(self.peak_detection_settings.filter_width)
            filter_width_spin.setSuffix(" samples")
            filter_width_spin.setSingleStep(2)
            def ensure_odd(value):
                if value % 2 == 0:
                    filter_width_spin.blockSignals(True)
                    filter_width_spin.setValue(value + 1)
                    filter_width_spin.blockSignals(False)
            filter_width_spin.valueChanged.connect(ensure_odd)
            settings_layout.addRow("Filter Width (SG):", filter_width_spin)
            
            # Units toggle
            units_toggle = QCheckBox("Display in pg (approximate)")
            units_toggle.setChecked(self.peak_detection_settings.use_pg_units)
            settings_layout.addRow("Units:", units_toggle)
            
            settings_group.setLayout(settings_layout)
            dialog_layout.addWidget(settings_group)
            
            # Store references for access in button handlers
            self.peak_detection_settings_window.detection_threshold_spin = detection_threshold_spin
            self.peak_detection_settings_window.doublet_gap_min_spin = doublet_gap_min_spin
            self.peak_detection_settings_window.doublet_gap_max_spin = doublet_gap_max_spin
            self.peak_detection_settings_window.max_height_diff_spin = max_height_diff_spin
            self.peak_detection_settings_window.max_percent_diff_spin = max_percent_diff_spin
            self.peak_detection_settings_window.baseline_search_width_spin = baseline_search_width_spin
            self.peak_detection_settings_window.filter_width_spin = filter_width_spin
            self.peak_detection_settings_window.units_toggle = units_toggle
            
            # Buttons
            button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            button_box.accepted.connect(self._apply_peak_detection_settings)
            button_box.rejected.connect(self.peak_detection_settings_window.reject)
            dialog_layout.addWidget(button_box)
        
        # Show dialog
        self.peak_detection_settings_window.exec()
    
    def _apply_peak_detection_settings(self):
        """Apply peak detection settings from dialog."""
        if self.peak_detection_settings_window is None:
            return
        
        # Update settings from dialog
        from helper_functions.DATA_realtime_frequency_analysis import PeakDetectionSettings
        self.peak_detection_settings = PeakDetectionSettings(
            detection_threshold=self.peak_detection_settings_window.detection_threshold_spin.value(),
            doublet_gap_min=self.peak_detection_settings_window.doublet_gap_min_spin.value(),
            doublet_gap_max=self.peak_detection_settings_window.doublet_gap_max_spin.value(),
            max_height_diff=self.peak_detection_settings_window.max_height_diff_spin.value(),
            max_percent_diff=self.peak_detection_settings_window.max_percent_diff_spin.value(),
            baseline_search_width=self.peak_detection_settings_window.baseline_search_width_spin.value(),
            filter_width=self.peak_detection_settings_window.filter_width_spin.value(),
            use_pg_units=self.peak_detection_settings_window.units_toggle.isChecked()
        )
        
        # Update analyzer if it exists
        if self.realtime_analyzer is not None:
            self.realtime_analyzer.update_settings(self.peak_detection_settings)
        
        # Close dialog
        self.peak_detection_settings_window.accept()
    
    def on_substrate_bias_changed(self, value: float):
        """Handle substrate bias value change - update DAQ analog output."""
        self._set_substrate_bias_voltage(value)




# Standalone window wrapper for backward compatibility
class MainWindow(QMainWindow):
    """Standalone window wrapper for SMRControlWidget."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SMR Control")
        self.setGeometry(100, 100, 1200, 600)
        self.smr_control = SMRControlWidget()
        self.setCentralWidget(self.smr_control)


def main():
    """Main entry point for standalone execution."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

