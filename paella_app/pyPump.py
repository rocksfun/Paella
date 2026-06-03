import sys
import os
import csv
import time
import re

# Import pyserial with explicit error handling and verification
try:
    # Try to import pyserial explicitly
    import serial
    import serial.tools.list_ports
    
    # Verify that serial.Serial exists (this is the key class from pyserial)
    if not hasattr(serial, 'Serial'):
        # Diagnostic information
        import sys
        serial_module_path = getattr(serial, '__file__', 'unknown')
        serial_module_name = getattr(serial, '__name__', 'unknown')
        
        error_msg = (
            f"ERROR: The 'serial' module does not have 'Serial' attribute.\n"
            f"Module location: {serial_module_path}\n"
            f"Module name: {serial_module_name}\n\n"
            f"This usually means:\n"
            f"1. The 'pyserial' package is not installed. Install it with: pip install pyserial\n"
            f"2. A local file named 'serial.py' is shadowing the pyserial package.\n"
            f"3. The wrong 'serial' module is being imported.\n\n"
            f"Please check:\n"
            f"- Run 'pip install pyserial' to install the package\n"
            f"- Check for any 'serial.py' files in your project or Python path\n"
            f"- Verify the module path above is correct (should point to pyserial package)"
        )
        print(error_msg)
        raise ImportError(error_msg)
    
    # Additional verification: try to instantiate Serial to ensure it works
    # We'll do a dry-run check without actually opening a port
    _test_serial = serial.Serial
    del _test_serial
    
except ImportError as e:
    error_msg = (
        "Failed to import pyserial package.\n\n"
        "Please install pyserial:\n"
        "  pip install pyserial\n\n"
        f"Original error: {e}"
    )
    print(error_msg)
    raise ImportError(error_msg) from e
except AttributeError as e:
    # This will be caught by the hasattr check above, but just in case
    error_msg = (
        f"AttributeError when accessing serial module: {e}\n\n"
        "This suggests the wrong 'serial' module is being imported.\n"
        "Please ensure 'pyserial' is installed: pip install pyserial"
    )
    print(error_msg)
    raise ImportError(error_msg) from e
from helper_functions.SYSTEM_pull_config_io import (
    load_system_config, 
    get_syringe_pump_settings,
    get_camera_settings,
    get_autoclean_summary_enabled
)
from helper_functions.SMR_set_delays import _send_params_with_run_state
from helper_functions.SMR_sweep_frequencies import _load_smr_parameters, _map_parameters_to_register_args
import nidaqmx
import traceback
from helper_functions.PUMP_kickback import execute_kickback
import decimal
import time
import re
import os
import math
import numpy as np
import threading
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import (
    Qt, QThread, Signal, QRect, QPropertyAnimation, Property,
    QEasingCurve, QObject, QTimer, QMutex, Slot, QMetaObject, Q_ARG
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QFrame, QGridLayout,
    QFormLayout, QGroupBox, QRadioButton, QButtonGroup, QSplitter,
    QSizePolicy, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QCheckBox, QDoubleSpinBox, QSpinBox, QProgressBar,
    QScrollArea, QTextEdit, QMessageBox, QDialog, QDialogButtonBox,
    QAbstractItemView, QFileDialog
)
from PySide6.QtGui import QColor, QPainter, QBrush, QPen, QFont, QDropEvent, QIcon
from PySide6.QtCore import Signal as QtSignal
from helper_functions.UIUX_elements import (
    create_button, create_increment_button, create_status_label,
    create_status_badge, style_input_field, style_checkbox,
    create_progress_bar, create_text_indicator, Colors
)

# --- Constants based on the technical manual ---
SYRINGE_VOLUME_UL = 50.0
MAX_STEPS = 24000
STX = b'\x02'
ETX = b'\x03'
# Pump limits for V command (motor steps/second)
MIN_VELOCITY = 2
MAX_VELOCITY = 5800
DEBOUNCE_INTERVAL_SEC = 1.0 # Minimum seconds between fluidic command button presses
# References directory relative to script location
if hasattr(sys, '_MEIPASS'):
    # When running as a bundled executable
    _SCRIPT_DIR = sys._MEIPASS
else:
    # When running from source
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

REFERENCES_DIR = os.path.join(_SCRIPT_DIR, 'references')
ROUTINE_SUBDIR = os.path.join(REFERENCES_DIR, 'pypump_routines')
APP_ICON_PATH = os.path.join(REFERENCES_DIR, 'travera_logo.ico')


class NumericTableWidgetItem(QTableWidgetItem):
    """
    A custom QTableWidgetItem that sorts numerically instead of lexicographically.
    """
    def __lt__(self, other):
        try:
            return int(self.text()) < int(other.text())
        except (ValueError, TypeError):
            return super().__lt__(other)


class SyringeWidget(QWidget):
    """A custom widget to visualize the syringe fill level with animation."""
    stepsChanged = Signal(int)  # Signal to emit when steps change

    def __init__(self, parent=None):
        super().__init__(parent)
        # Increased height by 50%: from 100 to 150
        self.setMinimumSize(60, 150) 
        self._animated_steps = 0
        self.animation = QPropertyAnimation(self, b"animated_steps")

    def get_animated_steps(self):
        return self._animated_steps

    def set_animated_steps(self, steps):
        self._animated_steps = steps
        self.stepsChanged.emit(steps) # Emit signal for volume readout
        self.update()

    animated_steps = Property(int, get_animated_steps, set_animated_steps)

    def set_value_immediate(self, steps):
        """Sets the syringe value instantly without animation."""
        if self.animation.state() == QPropertyAnimation.State.Running:
            self.animation.stop()
        self.set_animated_steps(max(0, min(MAX_STEPS, steps)))

    def animate_to(self, steps, duration_ms):
        """Animates the syringe fill level to a new value over a given duration."""
        if self.animation.state() == QPropertyAnimation.State.Running:
            self.animation.stop()
        
        self.animation.setDuration(int(duration_ms))
        self.animation.setStartValue(self.get_animated_steps())
        self.animation.setEndValue(max(0, min(MAX_STEPS, steps)))
        self.animation.setEasingCurve(QEasingCurve.Type.Linear)
        self.animation.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        width = self.width()
        height = self.height()
        
        # Reserve space for text at the bottom
        text_height = 40
        
        # Syringe barrel (50% of width)
        barrel_width = width * 0.50
        barrel_x = (width - barrel_width) / 2
        
        # Adjust barrel height to leave room for text at the bottom
        # height - top_margin(10) - text_area(40)
        barrel_height = height - 10 - text_height
        barrel_rect = QRect(int(barrel_x), 10, int(barrel_width), int(barrel_height))
        
        painter.setPen(QColor(150, 150, 150))
        painter.setBrush(QColor(240, 240, 240))
        painter.drawRoundedRect(barrel_rect, 5, 5)

        # Liquid
        fill_percentage = self._animated_steps / MAX_STEPS
        liquid_height = int(barrel_rect.height() * fill_percentage)
        liquid_rect = QRect(
            barrel_rect.left(),
            barrel_rect.bottom() - liquid_height,
            barrel_rect.width(),
            liquid_height
        )
        painter.setBrush(QColor(0, 120, 215, 200))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(liquid_rect)

        # Tick marks and labels
        painter.setPen(QColor(50, 50, 50))
        font = self.font()
        font.setPointSize(8)
        painter.setFont(font)

        label_x = 0 # Initialize scope
        for i in range(6): # 0, 10, 20, 30, 40, 50 uL
            y = barrel_rect.bottom() - int((i / 5) * barrel_rect.height())
            tick_start = barrel_rect.right()
            tick_end = tick_start + 5
            label_x = tick_end + 3
            painter.drawLine(tick_start, y, tick_end, y)
            painter.drawText(label_x, y + 4, f"{i*10}")
        
        # Removed "µL" label at top - volume display at bottom already includes "µL"

        # Draw Current Volume Text
        current_vol = (self._animated_steps / MAX_STEPS) * SYRINGE_VOLUME_UL
        text = f"{current_vol:.1f} µL"
        
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(0, 0, 0))
        
        text_rect = QRect(0, height - text_height, width, text_height)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)


class CommunicationThread(QThread):
    """Handles all serial communication in a separate thread to prevent GUI freezing."""
    response_received = Signal(str)
    error_occurred = Signal(str, bool)
    port_closed = Signal()
    pump_ready = Signal(int)
    command_sent = Signal(int)  # Emits pump_address when a command is actually sent
    
    # NEW: Signal to broadcast parsed status for every response
    # (Address, Is_Ready_Bit, Error_Code, Is_Query)
    status_update = Signal(int, bool, int, bool)

    def __init__(self, port, baudrate):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.serial_port = None
        self.running = False
        # Queue stores tuples: (command_bytes, pump_address, is_query)
        self._command_queue = [] 
        self.sequence_number = 0
        self.waiting_for_response = False
        # Context queue handles multiple pending responses (e.g. when an interrupt is sent while a poll is waiting)
        self._pending_contexts = [] # list of (address, is_query)
        self.last_command_time = 0

    def calculate_checksum(self, data_block):
        checksum = 0
        for byte in data_block:
            checksum ^= byte
        return bytes([checksum])

    def format_command(self, address, command):
        self.sequence_number = (self.sequence_number + 1) % 8
        if self.sequence_number == 0: self.sequence_number = 1
        sequence_byte = bytes([0x30 + self.sequence_number])
        address_byte = str(address).encode('ascii')
        command_bytes = command.encode('ascii')
        data_block = STX + address_byte + sequence_byte + command_bytes + ETX
        checksum = self.calculate_checksum(data_block)
        return data_block + checksum

    def run(self):
        try:
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=1)
            self.running = True
            self.response_received.emit(f"Successfully opened port {self.port}")
        except serial.SerialException as e:
            self.error_occurred.emit(f"Error opening port {self.port}: {e}", True)
            return
        
        pending_context = None # (address, is_query)
        
        while self.running:
            # Check for timeout on waiting for response
            if self.waiting_for_response and (time.time() - self.last_command_time > 2.0):
                self.error_occurred.emit("Timeout waiting for response. Clearing busy flag.", False)
                self.waiting_for_response = False
                self._pending_contexts.clear()

            # Command Selection: Priority to 'T' (Interrupt) commands
            # Also allow 'T' to bypass the waiting_for_response gate
            priority_idx = -1
            for i, cmd in enumerate(self._command_queue):
                if b'T' in cmd[0] and b'T' == cmd[0][3:4]: # STX + addr + seq + 'T'
                    priority_idx = i
                    break
            
            can_send = False
            if priority_idx != -1:
                can_send = True # Priority commands always bypass the gate
            elif self._command_queue and not self.waiting_for_response:
                can_send = True
            
            if can_send:
                # Unpack command data
                if priority_idx != -1:
                    cmd_data = self._command_queue.pop(priority_idx)
                else:
                    cmd_data = self._command_queue.pop(0)

                command_to_send = cmd_data[0]
                pump_address = cmd_data[1]
                is_query = cmd_data[2]
                
                self._pending_contexts.append((pump_address, is_query))
                
                try:
                    self.serial_port.write(command_to_send)
                    self.response_received.emit(f"SENT: {command_to_send.hex(' ').upper()}")
                    self.command_sent.emit(pump_address)
                    self.waiting_for_response = True
                    self.last_command_time = time.time()
                except serial.SerialException as e:
                    self.error_occurred.emit(f"Serial error on write: {e}", True)
                    self.stop()
                    break

            try:
                if self.serial_port.in_waiting > 0:
                    start_byte = self.serial_port.read(1)
                    if start_byte == STX:
                        response_body = self.serial_port.read_until(ETX)
                        if response_body and response_body.endswith(ETX):
                            if self.serial_port.in_waiting == 0:
                                self.msleep(10) 
                            if self.serial_port.in_waiting > 0:
                                checksum_byte = self.serial_port.read(1)
                                full_response = STX + response_body + checksum_byte
                                self.response_received.emit(f"RECV: {full_response.hex(' ').upper()}")
                                
                                # Pass context from the front of the queue to parse_response
                                current_context = self._pending_contexts.pop(0) if self._pending_contexts else None
                                self.parse_response(full_response, current_context)
                                
                                # Only clear waiting bit if no more contexts are pending
                                if not self._pending_contexts:
                                    self.waiting_for_response = False
                            else:
                                self.error_occurred.emit(f"Incomplete message received (missing checksum): {response_body.hex(' ')}", False)
                                self.waiting_for_response = False
                        else:
                            self.error_occurred.emit(f"Incomplete message received: {response_body.hex(' ')}", False)
                            self.waiting_for_response = False
            except serial.SerialException as e:
                self.error_occurred.emit(f"Serial error on read: {e}", True)
                self.stop()
                break
            self.msleep(50)

        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        self.port_closed.emit()

    def parse_response(self, response, context=None):
        """
        Parse response from pump.
        context: tuple (expected_pump_address, is_query)
        """
        try:
            response_address = int(response[1:2].decode('ascii'))
        except (ValueError, IndexError):
            self.error_occurred.emit(f"Could not parse address from response: {response.hex(' ')}", False)
            return

        calculated_checksum = self.calculate_checksum(response[:-1])
        if calculated_checksum != response[-1:]:
            self.error_occurred.emit(f"Checksum error in response: {response.hex(' ')}", True)
            return
        
        expected_address = None
        is_query = False
        if context:
            expected_address, is_query = context
            address = expected_address
        else:
            address = response_address
        
        # --- NEW STATUS BYTE PARSING ---
        status_byte = response[2]
        
        # Bits 0-3: Error Code (must be read first)
        error_code = status_byte & 0x0F
        
        # Bit 5: 1 = Ready, 0 = Busy
        # CRITICAL: Error code 15 ("Pump is busy") overrides the ready bit.
        # The pump can set bit 5 to "ready" even when error_code 15 indicates it's busy.
        # If error_code is 15, the pump is definitely BUSY regardless of bit 5.
        is_ready_bit = bool(status_byte & 0x20)
        is_ready = is_ready_bit and (error_code != 15)
        
        # Emit raw status for RoutineThread (for both ACKs and queries)
        # The is_query flag allows RoutineThread to handle them differently:
        # - ACKs: only check for errors, don't update status (ACKs can incorrectly report Ready)
        # - Queries: update status based on is_ready (these are reliable during polling)
        # Note: is_ready is already corrected for error_code 15 above
        self.status_update.emit(address, is_ready, error_code, is_query)
        # --------------------------------
        
        ready_status_str = "Ready" if is_ready else "Busy"
        
        error_map = {
            1: "Initialization error", 2: "Invalid command", 3: "Invalid operand",
            4: "Invalid command sequence", 6: "EEPROM failure", 7: "Syringe not initialized",
            9: "Syringe overload", 10: "Valve overload", 11: "Syringe move not allowed",
            15: "Pump is busy"
        }
        critical_errors = {1, 2, 3, 4, 6, 7, 9, 10, 11}
        
        error_str = "OK"

        if error_code in error_map:
            error_str = error_map[error_code]
            is_critical = error_code in critical_errors
            # Only emit error occurrence if it's a "real" error, not just "Pump is busy"
            if error_code != 15:
                self.error_occurred.emit(f"PUMP {address} NOTIFICATION: {error_str}", is_critical)
                # Print syringe errors to stdout so they are captured in the console log file
                print(f"SYRINGE ERROR - Pump {address}: {error_str} (error code {error_code})")
        
        response_str = f"INTERPRETED (PUMP {address}): Status: {ready_status_str}, Last Command: {error_str}"
        self.response_received.emit(response_str)

        if is_ready and is_query:
            self.pump_ready.emit(address)

    def send_command(self, address, command):
        """Send a command to the specified pump address."""
        command_bytes = self.format_command(address, command)
        # Identify if this is a query command to handle response gating
        is_query = (command == "Q")
        self._command_queue.append((command_bytes, int(address), is_query))

    def stop(self):
        self.running = False

class RoutineThread(QThread):
    """Executes a routine sequence in a separate thread."""
    update_status = Signal(int, str, QColor) # row, message, color
    routine_finished = Signal(str)
    step_changed = Signal(int, int) # old_step, new_step
    progress_updated = Signal(int)  # progress percentage (0-100)

    def __init__(self, routine_data, main_window_ref):
        super().__init__()
        self.routine_data = routine_data
        self.main_window = main_window_ref
        self._running = True
        self._paused = False
        self._mutex = QMutex()
        
        # Tracking variables for robust synchronization
        self.pending_acks = set()  # Pumps we sent a command to, waiting for immediate ACK
        self.active_pumps = {}     # Pumps that are busy, map address -> status("BUSY"/"READY")
        self.ready_count = {}      # Track consecutive ready responses per pump (for stability check)
        self.error_flag = False
        self.error_message = ""

    # Slot to receive status updates from CommunicationThread
    @Slot(int, bool, int, bool)
    def on_status_update(self, address, is_ready, error_code, is_query):
        if not self._running: return
        
        self._mutex.lock()
        try:
            # 1. Handle Command Acknowledgement (ACK) - only process if this is an ACK response
            if not is_query and address in self.pending_acks:
                self.pending_acks.remove(address)
                # If ACK contains error (e.g. invalid operand), abort immediately
                if error_code != 0 and error_code != 15: # 15 is just busy, usually ignored in error check but handled in logic
                    self.error_flag = True
                    self.error_message = f"Pump {address} rejected command. Error Code: {error_code}"
                
                # CRITICAL FIX: Return immediately after handling ACK.
                # Do NOT update active_pumps status based on the immediate ACK packet.
                # ACK responses can incorrectly report "Ready" before the pump physically starts moving.
                # We must wait for the Polling Phase (Q command) to verify actual status.
                return
            
            # 2. Handle Polling Updates - only process query responses for status updates
            if is_query and address in self.active_pumps:
                # If a runtime error occurred (e.g. stall/overload)
                if error_code != 0 and error_code != 15:
                    self.error_flag = True
                    self.error_message = f"Pump {address} runtime error. Code: {error_code}"
                
                # CRITICAL FIX: Error code 15 explicitly means "Pump is busy" - always treat as busy
                # Also require multiple consecutive ready responses before trusting ready status.
                # The pump can report ready intermittently during motion, so we need a "stable ready" check.
                # Note: is_ready is already corrected in parse_response to account for error_code 15.
                # If error_code == 15, is_ready will be False regardless of bit 5.
                
                if error_code == 15:
                    # Explicitly busy - reset ready counter and ensure status is BUSY
                    if address in self.ready_count:
                        self.ready_count[address] = 0
                    self.active_pumps[address] = "BUSY"
                elif is_ready:
                    # Increment ready counter
                    if address not in self.ready_count:
                        self.ready_count[address] = 0
                    self.ready_count[address] += 1
                    
                    # Require 2 consecutive ready responses before marking as READY
                    # This prevents false positives from intermittent ready reports during motion
                    if self.ready_count[address] >= 2:
                        self.active_pumps[address] = "READY"
                else:
                    # Pump is busy (bit 5 is 0, but error_code is not 15) - reset ready counter
                    if address in self.ready_count:
                        self.ready_count[address] = 0
                    # If it was previously marked READY, reset it back to BUSY
                    if self.active_pumps.get(address) == "READY":
                        self.active_pumps[address] = "BUSY"
        finally:
            self._mutex.unlock()

    def _ul_min_to_velocity(self, rate_ul_min):
        # Velocity uses motor steps (6,000 per stroke), not high-res steps (24,000)
        velocity = int(rate_ul_min * 2)
        return max(MIN_VELOCITY, min(velocity, MAX_VELOCITY))
    
    def _volume_to_steps(self, volume_ul):
        return int((volume_ul / SYRINGE_VOLUME_UL) * MAX_STEPS)

    def run(self):
        if not self.routine_data:
            self.routine_finished.emit("Finished: Routine was empty.")
            return

        # Group actions by step number
        steps = {}
        for i, action in enumerate(self.routine_data):
            action['row'] = i
            step_num = action['step']
            if step_num not in steps:
                steps[step_num] = []
            steps[step_num].append(action)
        
        sorted_step_nums = sorted(steps.keys())
        total_steps = len(sorted_step_nums)
        last_step = -1
        completed_steps = 0

        # Emit initial progress (0%)
        self.progress_updated.emit(0)

        for step_num in sorted_step_nums:
            if not self._running or self.error_flag:
                break
            
            while self._paused:
                self.msleep(200)

            self.step_changed.emit(last_step, step_num)
            last_step = step_num
            
            actions_in_step = steps[step_num]
            
            # Reset state for this step
            self._mutex.lock()
            self.active_pumps = {}
            self.pending_acks = set()
            self.ready_count = {}  # Reset ready counters for new step
            self._mutex.unlock()

            wait_duration_s = 0

            # --- PHASE 1: SEND COMMANDS ---
            for action_details in actions_in_step:
                row = action_details['row']
                action_type = action_details['action']
                
                # Handle Interrupt command - send T command immediately
                if action_type == "Interrupt":
                    syringe_idx = action_details.get('syringe')
                    self.update_status.emit(row, "Running", QColor("#f0e68c")) # Khaki
                    # Send T command directly without R (interrupt is immediate)
                    QMetaObject.invokeMethod(self.main_window, "send_interrupt_command",
                                             Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(int, syringe_idx))
                    # Update status - interrupt completes immediately
                    self.update_status.emit(row, "Completed", QColor("#90EE90")) # Light green
                    # Interrupt doesn't wait for ACK - it's immediate, continue to next action
                    continue
                
                self.update_status.emit(row, "Running", QColor("#f0e68c")) # Khaki
                
                if action_type == "Wait":
                    wait_duration_s = max(wait_duration_s, float(action_details['param1']))
                    continue

                syringe_idx = action_details.get('syringe')
                pump_address = syringe_idx + 1

                # Update state tracking
                self._mutex.lock()
                self.pending_acks.add(pump_address)
                self.active_pumps[pump_address] = "BUSY"
                self.ready_count[pump_address] = 0  # Initialize ready counter
                self._mutex.unlock()

                # Update UI immediately
                QMetaObject.invokeMethod(self.main_window, "update_syringe_status_ui",
                                         Qt.ConnectionType.QueuedConnection,
                                         Q_ARG(int, syringe_idx),
                                         Q_ARG(str, "BUSY"),
                                         Q_ARG(str, f"Busy - {action_type}"))

                # Formulate Command
                command = ""
                if action_type == "Move Valve":
                    command = f"I{int(action_details['param1'])}"
                elif action_type in ["Draw", "Dispense"]:
                    # Validate 0 rate
                    try:
                        rate_val = float(action_details['param3']) if action_details['param3'] else 0.0
                        if rate_val == 0.0:
                            self.error_flag = True
                            self.error_message = 'Cannot draw or dispense when rate is set to 0uL/min.'
                            break
                    except (ValueError, TypeError):
                        pass  # Invalid rate will be caught below
                    
                    # Get port, volume, and rate
                    port = int(action_details['param1']) if action_details['param1'] else 1
                    volume = float(action_details['param2']) if action_details['param2'] else 0.0
                    rate = float(action_details['param3']) if action_details['param3'] else 0.0
                    
                    velocity = self._ul_min_to_velocity(rate)
                    steps_to_move = self._volume_to_steps(volume)
                    action_char = 'P' if action_type == "Draw" else 'D'
                    
                    # Combined command: I{port}V{velocity}{P|D}{steps}
                    command = f"I{port}V{velocity}{action_char}{steps_to_move}"
                    
                    # Update valve button state immediately
                    QMetaObject.invokeMethod(self.main_window, "_update_valve_button_state",
                                             Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(int, syringe_idx),
                                             Q_ARG(int, port))
                    
                    # Trigger Animation
                    QMetaObject.invokeMethod(self.main_window, "update_routine_animation",
                                             Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(int, syringe_idx),
                                             Q_ARG(str, action_type),
                                             Q_ARG(float, volume),
                                             Q_ARG(float, rate))
                elif action_type == "Home":
                    # Home is special; it triggers a sequence on the main window side.
                    # We still track it as a BUSY pump.
                    QMetaObject.invokeMethod(self.main_window, "home_pump",
                                             Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(int, syringe_idx))
                    # home_pump sends a command, so we wait for its ACK just like others
                    command = None
                elif action_type == "Empty Syringe":
                    # Empty Syringe is special; it triggers a sequence on the main window side.
                    # We still track it as a BUSY pump.
                    QMetaObject.invokeMethod(self.main_window, "empty_syringe",
                                             Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(int, syringe_idx))
                    # empty_syringe sends a command, so we wait for its ACK just like others
                    command = None
                elif action_type == "Prime Reagent":
                    # Prime Reagent is special; it triggers a sequence on the main window side.
                    # We still track it as a BUSY pump.
                    # Get port from param1
                    port = int(action_details['param1']) if action_details['param1'] else None
                    if port:
                        # Store port for prime sequence and set valve position
                        QMetaObject.invokeMethod(self.main_window, "_set_prime_port",
                                                 Qt.ConnectionType.QueuedConnection,
                                                 Q_ARG(int, syringe_idx),
                                                 Q_ARG(int, port))
                        QMetaObject.invokeMethod(self.main_window, "_update_valve_button_state",
                                                 Qt.ConnectionType.QueuedConnection,
                                                 Q_ARG(int, syringe_idx),
                                                 Q_ARG(int, port))
                        QMetaObject.invokeMethod(self.main_window, "prime_reagent",
                                                 Qt.ConnectionType.QueuedConnection,
                                                 Q_ARG(int, syringe_idx))
                    # prime_reagent sends a command, so we wait for its ACK just like others
                    command = None 

                if command:
                    QMetaObject.invokeMethod(self.main_window, "send_command_to_pump",
                                             Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(int, syringe_idx),
                                             Q_ARG(str, command))

                # --- WAIT FOR ACKNOWLEDGEMENT ---
                # Spin-wait until CommunicationThread confirms the command was received (ACKed)
                # or rejected (Error).
                ack_timeout = 0
                ack_received = False
                while ack_timeout < 10000: # 10s timeout for serial ACK (increased for robust Final Clean)
                    self.msleep(10)
                    ack_timeout += 10
                    
                    self._mutex.lock()
                    if self.error_flag: 
                        self._mutex.unlock()
                        break
                    if pump_address not in self.pending_acks:
                        ack_received = True
                        self._mutex.unlock()
                        break
                    self._mutex.unlock()
                
                if self.error_flag: break
                if not ack_received:
                    self.error_flag = True
                    self.error_message = f"Timeout waiting for ACK from Pump {pump_address}"
                    break
            
            if self.error_flag: break

            # --- PHASE 2: WAIT / POLL FOR COMPLETION ---
            
            if wait_duration_s > 0:
                self.sleep(int(wait_duration_s))

            # Poll until all active pumps are READY
            start_poll_time = time.time()
            poll_interval = 0.5
            last_poll = 0
            
            # Initial mechanical delay - give pump time to actually start moving after ACK
            self.msleep(200)

            while True:
                if not self._running: break
                
                self._mutex.lock()
                if self.error_flag:
                    self._mutex.unlock()
                    break
                
                # Check if all pumps are ready
                all_ready = all(status == "READY" for status in self.active_pumps.values())
                
                # Get list of busy pumps to query
                busy_pumps = [addr for addr, status in self.active_pumps.items() if status == "BUSY"]
                self._mutex.unlock()

                if all_ready:
                    break # Step Complete
                
                if time.time() - start_poll_time > 600: # 10 minute timeout per step
                    self.error_flag = True
                    self.error_message = f"Timeout waiting for Step {step_num} completion."
                    break

                # Poll busy pumps periodically
                if time.time() - last_poll > poll_interval:
                    for addr in busy_pumps:
                        QMetaObject.invokeMethod(self.main_window, "query_pump_status",
                                                 Qt.ConnectionType.QueuedConnection,
                                                 Q_ARG(int, addr - 1))
                    last_poll = time.time()
                
                self.msleep(100)

            if self.error_flag: break

            # --- MARK STEP COMPLETE ---
            completed_steps += 1
            # Calculate and emit progress (percentage)
            progress_percent = int((completed_steps / total_steps) * 100) if total_steps > 0 else 0
            self.progress_updated.emit(progress_percent)
            
            for action_details in actions_in_step:
                self.update_status.emit(action_details['row'], "Done", QColor("#90ee90")) # LightGreen

        # Final cleanup
        if self.error_flag:
            self.routine_finished.emit(f"Routine Aborted: {self.error_message}")
            self.progress_updated.emit(0)  # Reset progress on error
        elif not self._running:
            self.routine_finished.emit("Stopped by user.")
            self.progress_updated.emit(0)  # Reset progress on stop
        else:
            self.routine_finished.emit("Routine completed successfully.")
            self.progress_updated.emit(100)  # Set to 100% on success
        
        self.step_changed.emit(last_step, -1)

    def stop(self):
        self._running = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False


class SyringeControlWidget(QWidget):
    """Embeddable widget for syringe pump control."""
    
    # Fluidic States
    FLUIDIC_STATE_NOT_INITIALIZED = "NOT_INITIALIZED"
    FLUIDIC_STATE_IDLE = "IDLE"
    FLUIDIC_STATE_CLEANING = "CLEANING"
    FLUIDIC_STATE_RUNNING_SAMPLE = "RUNNING_SAMPLE"
    FLUIDIC_STATE_BEADS = "BEADS"

    # Signals
    fluidic_state_changed = Signal(str, str) # state, message
    kickback_time_updated = Signal(float) # seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.comm_thread = None
        self.routine_thread = None
        
        # Initialize fluidic state
        self.current_fluidic_state = self.FLUIDIC_STATE_NOT_INITIALIZED
        
        self.initialize_state = 'IDLE'
        self.prime_sequence_port = {0: None, 1: None}  # Store port for prime sequence (None = not running)
        self.prime_is_manual = {0: False, 1: False}  # Track if prime was called from manual control
        
        # Debounce for critical fluidic buttons (Manual Kickback, Run Beads, Run Condition, Clean Now, Prime System, Final Clean)
        self._last_fluidic_press_time = 0
        # NOTE: Config file is READ-ONLY. This code should never write to it.
        self.config_file = os.path.join(REFERENCES_DIR, 'pump_config.txt')

        # Single Unified Timer to poll active/busy pumps
        self.busy_poll_timer = QTimer(self)
        self.busy_poll_timer.timeout.connect(self._poll_busy_status)

        # UI Data Structures
        self.valve_buttons = {0: {}, 1: {}}
        self.nickname_inputs = {0: {}, 1: {}}
        self.port_type_combos = {0: {}, 1: {}}
        self.speed_inputs = {0: {}, 1: {}}
        self.selected_valve = {0: None, 1: None}
        self.volume_inputs = {}
        self.prime_buttons = {0: None, 1: None}
        self.sample_draw_rate_input = None
        self.bead_draw_rate_input = None
        self.sample_bf_draw_rate = "2.0"
        self.sample_bffl_draw_rate = "1.0"
        self.bead_draw_rate_value = "2.0"
        self.camera_mode = "BF+FL"
        self.sample_draw_rate_value = "1.0"  # Current selected rate
        self.syringe_control_frames = {}
        self.syringe_visualizers = {}
        
        # State Data
        self.current_steps = {0: 0, 1: 0}
        # SMR widget reference for accessing sample path
        self.smr_widget = None
        
        # Aspiration monitoring state
        self.aspiration_poll_timer = QTimer(self)
        self.aspiration_poll_timer.timeout.connect(self._poll_aspiration_progress)
        self.aspiration_start_time = None
        self.aspiration_target_volume = None
        self.aspiration_draw_rate = None
        self.aspiration_start_steps = None
        self.is_aspirating = False
        
        # Track if current routine is a clean routine
        self.is_clean_routine = False
        
        # Track prime system sequence
        self.prime_system_sequence = None  # List of routine names to run sequentially
        self.prime_system_completed = False  # Track if Prime System has been completed
        
        # Track final clean sequence
        self.final_clean_sequence = None  # List of routine names to run sequentially
        
        # Kickback state
        self.kickback_timed_enabled = False
        self.config_kickback_timed_enabled = False  # Track user's intended setting
        self.kickback_time_seconds = 300.0
        self.kickback_volume_ul = 5.0
        
        # Public tracking variable for cross-module features (readout by pySMR)
        self.current_volume_drawn = 0.0
        self.kickback_rate_ul_min = 100.0
        self.last_kickback_time = None
        self.kickback_in_progress = False
        self.kickback_executor = None  # Store reference to prevent garbage collection
        self.kickback_timer = QTimer(self)
        self.kickback_timer.timeout.connect(self._check_kickback_timing)
        self.kickback_timer.setInterval(500)  # Check every 500ms
        self.kickback_time_update_timer = QTimer(self)
        self.kickback_time_update_timer.timeout.connect(self._update_kickback_time_indicator)
        self.kickback_time_update_timer.setInterval(100)  # Update indicator every 100ms
        self.kickback_time_update_timer.start()
        
        # Status label to replace statusBar
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("background-color: #e8e8e8; padding: 5px; border: 1px solid #ccc; border-radius: 3px;")
        
        self._setup_styles()
        main_layout = QVBoxLayout(self)
        
        self.connection_group = self._create_connection_group()
        main_layout.addWidget(self.connection_group)

        # ---- Combined Main TabWidget with all tabs ----
        main_tabs = QTabWidget()
        self.tabs = main_tabs  # Store reference for mode switching
        # Set size policy to not expand vertically - only use space needed
        main_tabs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        # Experimental Controls Tab (first/default tab)
        main_tabs.addTab(self._create_experimental_controls_tab(), "Experimental Controls")

        # Manual Control Tab
        manual_control_widget = QWidget()
        manual_controls_layout = QHBoxLayout(manual_control_widget)
        manual_controls_layout.addWidget(self._create_syringe_control_group(0))
        manual_controls_layout.addWidget(self._create_syringe_control_group(1))
        main_tabs.addTab(manual_control_widget, "Manual Control")

        # Routines Tab
        main_tabs.addTab(self._create_routines_tab(), "Routines")

        # Port Settings Tab
        main_tabs.addTab(self._create_port_settings_tab(), "Port Settings")

        # Syringe Settings Tab
        main_tabs.addTab(self._create_syringe_settings_tab(), "Syringe Settings")

        # Communication Log Tab
        main_tabs.addTab(self._create_log_group(), "Communication Log")

        main_layout.addWidget(main_tabs, 0)  # Stretch factor 0 to prevent expansion
        
        # Status Section (Outside of Tabs)
        main_layout.addWidget(self._create_status_section(), 0)  # Stretch factor 0 to prevent expansion
        
        # Add status label at bottom
        main_layout.addWidget(self.status_label, 0)

        # ---- End combined main tabs ----
        
        os.makedirs(ROUTINE_SUBDIR, exist_ok=True)
        self.load_or_create_config()

        self.update_status_message("Disconnected")
        self.update_ui_state(connected=False)
        self._update_routine_ui_state(running=False)
        
        # Emit initial fluidic state
        QTimer.singleShot(100, lambda: self.fluidic_state_changed.emit(self.FLUIDIC_STATE_NOT_INITIALIZED, "Syringe Pumps not Initialized"))

    def set_camera_mode(self, mode):
        """Update the sample draw rate based on the camera mode."""
        self.camera_mode = mode
        
        # Update current sample draw rate
        if mode == "BF only":
            self.sample_draw_rate_value = self.sample_bf_draw_rate
        else:
            self.sample_draw_rate_value = self.sample_bffl_draw_rate
            
        # Update UI if it exists
        if self.sample_draw_rate_input:
            self.sample_draw_rate_input.setText(self.sample_draw_rate_value)
            print(f"Syringe pump sample draw rate updated to {self.sample_draw_rate_value} for mode {mode}")

    def set_gui_mode(self, mode):
        """Set the GUI mode and update UI visibility."""
        is_advanced = (mode == "advanced")
        
        # In basic mode, hide the top connection group
        if hasattr(self, 'connection_group'):
            self.connection_group.setVisible(is_advanced)
        
        # In basic mode, only 'Experimental Controls' (index 0) is visible
        for i in range(1, self.tabs.count()):
            self.tabs.setTabVisible(i, is_advanced)

    def update_status_message(self, message, timeout_ms=0):
        """Update the status label message (replaces statusBar().showMessage())."""
        self.status_label.setText(message)
        if timeout_ms > 0:
            QTimer.singleShot(timeout_ms, lambda: self.status_label.setText(""))

    def _setup_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f0f0f0; }
            QWidget { background-color: #f0f0f0; }
            QFrame { border: 1px solid #ccc; border-radius: 5px; background-color: #f0f0f0; }
            QTabWidget::pane { border-top: 2px solid #C2C7CB; background-color: #f0f0f0; }
            QTabWidget > QWidget { background-color: #f0f0f0; }
            QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit { font-family: 'Segoe UI', Arial; font-size: 10pt; }
            QTabBar::tab { background: #e1e1e1; border: 1px solid #c4c4c4; border-bottom: #c2c7cb; border-top-left-radius: 4px; border-top-right-radius: 4px; min-width: 8ex; padding: 5px 20px; }
            QTabBar::tab:selected, QTabBar::tab:hover { background: #f0f0f0; }
            QPushButton { background-color: #0078d7; color: white; border-radius: 5px; padding: 5px 15px; border: 1px solid #005a9e; }
            QPushButton:hover { background-color: #005a9e; } QPushButton:pressed { background-color: #003e6e; }
            QPushButton:disabled { background-color: #d3d3d3; color: #808080; border: 1px solid #a0a0a0; }
            QLineEdit, QComboBox, QTextEdit, QSpinBox, QDoubleSpinBox { border: 1px solid #ccc; border-radius: 5px; padding: 3px; background-color: white; }
            QLineEdit:disabled { background-color: #eeeeee; }
            QTableWidget::item { padding: 3px; }
            /* Fix for table editing clipping text: Remove padding/border from editor widget */
            QTableWidget QLineEdit { padding: 0px; border: none; background-color: white; }
            QTableWidget QComboBox { padding: 0px; border: none; margin: 0px; }
            QHeaderView::section { background-color: #e8e8e8; padding: 5px; border: 1px solid #ccc; }
            /* CHANGED: Updated ValveButton style to be more compact and rectangular for vertical stacking */
            #ValveButton { min-height: 24px; border-radius: 5px; font-size: 10pt; font-weight: bold; background-color: #e0e0e0; color: #333; border: 2px solid #a0a0a0; margin: 1px; padding: 2px 15px; }
            #ValveButton:checked { background-color: #28a745; color: white; border: 2px solid #1e7e34; }
            #Log { font-family: 'Consolas', monospace; font-size: 9pt; background-color: #2b2b2b; color: #a9b7c6; }
            #SyringeGroup { font-size: 14pt; font-weight: bold; }
            #DangerButton { background-color: #dc3545; border-color: #b02a37; }
            #DangerButton:hover { background-color: #c82333; }
            #SuccessButton { background-color: #28a745; border-color: #1e7e34; }
            #SuccessButton:hover { background-color: #218838; }
            #PrimeButton:disabled { background-color: #505050; color: #999; border: 2px solid #404040; }
            #FilledRadioButton::indicator { width: 20px; height: 20px; border: 2px solid #0078d7; border-radius: 10px; background-color: white; }
            #FilledRadioButton::indicator:checked { background-color: #0078d7; border: 2px solid #005a9e; }
        """)

    def _create_syringe_control_group(self, syringe_index):
        group_frame = QFrame()
        self.syringe_control_frames[syringe_index] = group_frame
        group_layout = QVBoxLayout(group_frame)
        group_layout.setSpacing(2) # Tight spacing between elements
        group_layout.setContentsMargins(5, 2, 5, 2) # Reduced vertical margins (top/bottom from 5 to 2)
        group_layout.setAlignment(Qt.AlignmentFlag.AlignTop) # Align everything to top

        title = QLabel(f"Syringe {syringe_index + 1}")
        title.setObjectName("SyringeGroup")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setContentsMargins(0, 0, 0, 0) # Remove padding from title
        title.setMargin(0) # Remove margin
        group_layout.addWidget(title)
        
        self._create_nickname_panel_data(syringe_index)
        self._create_settings_panel_data(syringe_index)
        
        top_layout = QHBoxLayout()
        top_layout.setSpacing(5)  # Tight horizontal spacing
        top_layout.setContentsMargins(0, 0, 0, 0)  # No margins
        valve_group = self._create_valve_group(syringe_index)
        
        vis_layout = QVBoxLayout()
        vis_layout.setContentsMargins(0, 0, 0, 0)  # Remove padding from layout
        vis_layout.setSpacing(0)  # Remove spacing
        self.syringe_visualizers[syringe_index] = SyringeWidget()
        # Connect volume change signal
        self.syringe_visualizers[syringe_index].stepsChanged.connect(
            lambda steps, idx=syringe_index: self.update_syringe_volume_ui(idx, (steps / MAX_STEPS) * SYRINGE_VOLUME_UL)
        )
        # Removed addStretch() calls to reduce vertical padding
        vis_layout.addWidget(self.syringe_visualizers[syringe_index])

        top_layout.addWidget(valve_group, 3) 
        top_layout.addLayout(vis_layout, 2)
        group_layout.addLayout(top_layout)

        syringe_actions_group = self._create_syringe_group(syringe_index)
        group_layout.addWidget(syringe_actions_group)

        return group_frame

    # Removed _populate_nicknames_tab and _populate_settings_tab, as integration is done above

    def _create_nickname_panel_data(self, syringe_index):
        for i in range(1, 7):
            self.nickname_inputs[syringe_index][i] = QLineEdit(f"Port {i}")
            self.port_type_combos[syringe_index][i] = QComboBox()
            self.port_type_combos[syringe_index][i].addItems(['Device', 'Reagent', 'Waste', 'Empty'])

    def _create_nickname_panel_ui(self, syringe_index):
        panel = QWidget()
        layout = QGridLayout(panel)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        for i in range(1, 7):
            nickname_edit = self.nickname_inputs[syringe_index][i]
            type_combo = self.port_type_combos[syringe_index][i]
            layout.addWidget(QLabel(f"Port {i} Nickname:"), i - 1, 0)
            layout.addWidget(nickname_edit, i - 1, 1)
            layout.addWidget(type_combo, i - 1, 2)
            nickname_edit.textChanged.connect(lambda t, s=syringe_index, p=i: self._update_valve_button_text(s, p))
        return panel

    def _create_settings_panel_data(self, syringe_index):
        self.speed_inputs[syringe_index] = {}
        defaults = {
            'Device': {'Draw': "60.0", 'Dispense': "60.0"},
            'Reagent': {'Draw': "100.0", 'Dispense': ""},
            'Waste': {'Draw': "", 'Dispense': "100.0"},
            'Empty': {'Draw': "0.0", 'Dispense': "0.0"},
        }
        for port_type in defaults:
            self.speed_inputs[syringe_index][port_type] = {
                'Draw': QLineEdit(defaults[port_type]['Draw']),
                'Dispense': QLineEdit(defaults[port_type]['Dispense'])
            }
        self.speed_inputs[syringe_index]['Reagent']['Dispense'].setEnabled(False)
        self.speed_inputs[syringe_index]['Waste']['Draw'].setEnabled(False)
        
        # Connect reagent draw rate changes to update prime button state
        self.speed_inputs[syringe_index]['Reagent']['Draw'].textChanged.connect(
            lambda: self._update_prime_button_state(syringe_index))
            
    def _create_settings_panel_ui(self, syringe_index):
        panel = QWidget()
        layout = QGridLayout(panel)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(QLabel("<b>Port Type</b>"), 0, 0)
        layout.addWidget(QLabel("<b>Draw Rate (µL/min)</b>"), 0, 1)
        layout.addWidget(QLabel("<b>Dispense Rate (µL/min)</b>"), 0, 2)
        port_types = ['Device', 'Reagent', 'Waste']
        for i, port_type in enumerate(port_types, 1):
            layout.addWidget(QLabel(f"{port_type}:"), i, 0)
            layout.addWidget(self.speed_inputs[syringe_index][port_type]['Draw'], i, 1)
            layout.addWidget(self.speed_inputs[syringe_index][port_type]['Dispense'], i, 2)
        return panel

    def _update_valve_button_text(self, syringe_index, port_number):
        if port_number in self.valve_buttons[syringe_index]:
            button = self.valve_buttons[syringe_index][port_number]
            if port_number in self.nickname_inputs[syringe_index]:
                nickname = self.nickname_inputs[syringe_index][port_number].text()
                # Simplified text format for compact buttons
                button.setText(f"{port_number}: {nickname}")

    def _create_port_settings_tab(self):
        """Creates the Port Settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # Port assignments (horizontal split)
        ports_section = QHBoxLayout()
        ports_section.setSpacing(20)
        
        # Left: Syringe 1 port assignments
        syringe1_frame = QFrame()
        syringe1_layout = QVBoxLayout(syringe1_frame)
        syringe1_layout.addWidget(QLabel("<b>Syringe 1 - Port Assignments</b>"))
        syringe1_layout.addWidget(self._create_nickname_panel_ui(0))
        ports_section.addWidget(syringe1_frame, 1)
        
        # Right: Syringe 2 port assignments
        syringe2_frame = QFrame()
        syringe2_layout = QVBoxLayout(syringe2_frame)
        syringe2_layout.addWidget(QLabel("<b>Syringe 2 - Port Assignments</b>"))
        syringe2_layout.addWidget(self._create_nickname_panel_ui(1))
        ports_section.addWidget(syringe2_frame, 1)
        
        main_layout.addLayout(ports_section)
        main_layout.addStretch()
        
        return widget

    def _create_syringe_settings_tab(self):
        """Creates the Syringe Settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # Shared syringe settings (draw/dispense rates)
        settings_frame = QFrame()
        settings_layout = QVBoxLayout(settings_frame)
        settings_layout.addWidget(QLabel("<b>Syringe Settings (Applied to Both Syringes)</b>"))
        settings_layout.addWidget(self._create_shared_settings_panel_ui())
        
        main_layout.addWidget(settings_frame)
        main_layout.addStretch()
        
        return widget

    def _create_shared_settings_panel_ui(self):
        """Creates a single settings panel that applies to both syringes."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        
        # Port Type Settings Grid
        port_grid = QGridLayout()
        port_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        port_grid.addWidget(QLabel("<b>Port Type</b>"), 0, 0)
        port_grid.addWidget(QLabel("<b>Draw Rate (µL/min)</b>"), 0, 1)
        port_grid.addWidget(QLabel("<b>Dispense Rate (µL/min)</b>"), 0, 2)
        port_types = ['Device', 'Reagent', 'Waste', 'Empty']
        for i, port_type in enumerate(port_types, 1):
            port_grid.addWidget(QLabel(f"{port_type}:"), i, 0)
            
            # Use the inputs from syringe 0 as the master, and sync to syringe 1
            draw_input = self.speed_inputs[0][port_type]['Draw']
            dispense_input = self.speed_inputs[0][port_type]['Dispense']
            
            # Create sync function to update syringe 1 when syringe 0 changes
            def make_sync_handler(pt, is_draw):
                def sync_to_syringe1():
                    if is_draw:
                        new_val = self.speed_inputs[0][pt]['Draw'].text()
                        self.speed_inputs[1][pt]['Draw'].setText(new_val)
                    else:
                        new_val = self.speed_inputs[0][pt]['Dispense'].text()
                        self.speed_inputs[1][pt]['Dispense'].setText(new_val)
                return sync_to_syringe1
            
            # Connect to sync syringe 1 when syringe 0 changes
            draw_input.textChanged.connect(make_sync_handler(port_type, True))
            dispense_input.textChanged.connect(make_sync_handler(port_type, False))
            
            port_grid.addWidget(draw_input, i, 1)
            port_grid.addWidget(dispense_input, i, 2)
        
        layout.addLayout(port_grid)
        
        # Add spacing
        layout.addSpacing(20)
        
        # Sample Running Parameters Section
        sample_header = QLabel("<b>Sample Running Parameters</b>")
        layout.addWidget(sample_header)
        
        sample_grid = QGridLayout()
        sample_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        
        # Sample Draw Rate
        sample_grid.addWidget(QLabel("Sample Draw Rate (µL/min):"), 0, 0)
        self.sample_draw_rate_input = QLineEdit(self.sample_draw_rate_value)
        style_input_field(self.sample_draw_rate_input)
        sample_grid.addWidget(self.sample_draw_rate_input, 0, 1)
        
        # Bead Draw Rate
        sample_grid.addWidget(QLabel("Bead Draw Rate (µL/min):"), 1, 0)
        self.bead_draw_rate_input = QLineEdit(self.bead_draw_rate_value)
        style_input_field(self.bead_draw_rate_input)
        sample_grid.addWidget(self.bead_draw_rate_input, 1, 1)
        
        layout.addLayout(sample_grid)
        
        # Add spacing
        layout.addSpacing(20)
        
        # Kickback Settings Section
        kickback_header = QLabel("<b>Kickback Settings</b>")
        layout.addWidget(kickback_header)
        
        kickback_grid = QGridLayout()
        kickback_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        
        # Kickback Volume
        kickback_grid.addWidget(QLabel("Kickback Volume (µL):"), 0, 0)
        self.kickback_volume_input = QLineEdit(str(self.kickback_volume_ul))
        style_input_field(self.kickback_volume_input)
        self.kickback_volume_input.textChanged.connect(self._on_kickback_volume_changed)
        kickback_grid.addWidget(self.kickback_volume_input, 0, 1)
        
        # Kickback Rate
        kickback_grid.addWidget(QLabel("Kickback Rate (µL/min):"), 1, 0)
        self.kickback_rate_input = QLineEdit(str(self.kickback_rate_ul_min))
        style_input_field(self.kickback_rate_input)
        self.kickback_rate_input.textChanged.connect(self._on_kickback_rate_changed)
        kickback_grid.addWidget(self.kickback_rate_input, 1, 1)
        
        layout.addLayout(kickback_grid)
        layout.addStretch()
        
        return panel

    def _create_connection_group(self):
        frame = QFrame()
        layout = QHBoxLayout(frame)
        # Add Fluidic Control label inline with COM Port
        fluidic_label = QLabel("Fluidic Control")
        fluidic_label.setStyleSheet("font-size: 12pt; font-weight: bold; padding-right: 10px;")
        layout.addWidget(fluidic_label)
        layout.addWidget(QLabel("COM Port:"))
        self.com_port_combo = QComboBox()
        style_input_field(self.com_port_combo)
        self.refresh_com_ports()
        layout.addWidget(self.com_port_combo)
        self.refresh_button = create_button("Refresh", "primary")
        self.refresh_button.clicked.connect(self.refresh_com_ports)
        layout.addWidget(self.refresh_button)
        layout.addStretch()
        self.initialize_all_button = create_button("Initialize All", "success")
        self.initialize_all_button.clicked.connect(self.initialize_all_pumps)
        layout.addWidget(self.initialize_all_button)
        self.connect_button = create_button("Connect", "primary")
        self.connect_button.clicked.connect(self.toggle_connection)
        layout.addWidget(self.connect_button)
        return frame

    def _create_valve_group(self, syringe_index):
        frame = QFrame()
        main_vbox = QVBoxLayout(frame)
        main_vbox.setContentsMargins(5, 2, 5, 2) # Reduced vertical margins
        main_vbox.setSpacing(2) # Very tight spacing
        main_vbox.setAlignment(Qt.AlignmentFlag.AlignTop) # Align contents to top
        valve_title = QLabel("Valve Control (6-port)")
        valve_title.setContentsMargins(0, 0, 0, 0)  # Remove padding
        valve_title.setMargin(0)  # Remove margin
        main_vbox.addWidget(valve_title, 0, Qt.AlignmentFlag.AlignCenter)
        
        # CHANGED: Vertical stack layout for valve buttons
        buttons_layout = QVBoxLayout()
        buttons_layout.setSpacing(2) # Compact spacing between buttons
        buttons_layout.setAlignment(Qt.AlignmentFlag.AlignTop) # Ensure buttons stay at top
        
        # CHANGED: Create buttons vertically, Port 6 on top (6 down to 1)
        for i in range(6, 0, -1):
            button = QPushButton()
            button.setObjectName("ValveButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda chk, s=syringe_index, p=i: self.move_valve(s, p))
            self.valve_buttons[syringe_index][i] = button
            self._update_valve_button_text(syringe_index, i)
            buttons_layout.addWidget(button)
            
        main_vbox.addLayout(buttons_layout)
        # REMOVED main_vbox.addStretch() to prevent empty space below buttons
        return frame

    def _create_syringe_group(self, syringe_index):
        frame = QFrame()
        vbox = QVBoxLayout(frame)
        vbox.setSpacing(2)  # Tight spacing
        vbox.setContentsMargins(5, 2, 5, 2)  # Reduced margins
        actions_title = QLabel("Syringe Actions")
        actions_title.setContentsMargins(0, 0, 0, 0)  # Remove padding
        actions_title.setMargin(0)  # Remove margin
        vbox.addWidget(actions_title, 0, Qt.AlignmentFlag.AlignCenter)
        grid = QGridLayout()
        grid.setSpacing(3)  # Tight grid spacing
        grid.addWidget(QLabel("Volume (µL):"), 0, 0, 1, 2)
        vol_input = QLineEdit("10.0")
        style_input_field(vol_input)
        grid.addWidget(vol_input, 0, 2, 1, 2)
        self.volume_inputs[syringe_index] = vol_input
        
        draw_button = create_button("Draw", "success")
        draw_button.clicked.connect(lambda chk, s_idx=syringe_index: self.draw_volume(s_idx))
        grid.addWidget(draw_button, 1, 0, 1, 1)

        disp_button = create_button("Dispense", "success")
        disp_button.clicked.connect(lambda chk, s_idx=syringe_index: self.dispense_volume(s_idx))
        grid.addWidget(disp_button, 1, 1, 1, 1)

        pause_button = create_button("Pause", "warning")
        pause_button.clicked.connect(lambda chk, s_idx=syringe_index: self.pause_pump(s_idx))
        grid.addWidget(pause_button, 1, 2, 1, 1)

        home_button = create_button("Recalibrate", "primary")
        home_button.clicked.connect(lambda chk, s_idx=syringe_index: self.home_pump(s_idx))
        grid.addWidget(home_button, 1, 3, 1, 1)

        empty_button = create_button("Empty Syringe", "error")
        empty_button.clicked.connect(lambda chk, s_idx=syringe_index: self.empty_syringe(s_idx))
        grid.addWidget(empty_button, 2, 0, 1, 4)

        prime_button = create_button("Prime Reagent", "success")
        prime_button.setObjectName("PrimeButton")
        prime_button.clicked.connect(lambda chk, s_idx=syringe_index: self.prime_reagent(s_idx))
        grid.addWidget(prime_button, 3, 0, 1, 4)
        self.prime_buttons[syringe_index] = prime_button
        
        # Update prime button state based on current port
        self._update_prime_button_state(syringe_index)
        
        vbox.addLayout(grid)
        return frame

    def _create_increment_control(self, min_val, max_val, initial_val, step, suffix="", is_int=True):
        """
        Creates a custom increment control with explicit + and - buttons and manual text entry.
        Returns a container widget with get_value() and set_value() methods.
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        
        # Value input (editable) - left aligned
        value_input = QLineEdit()
        value_input.setAlignment(Qt.AlignmentFlag.AlignLeft)
        value_input.setMinimumWidth(100)
        style_input_field(value_input)
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
        
        # Internal value storage
        current_val = [initial_val]  # Use list to allow modification in nested functions
        
        def update_display():
            """Update the text input field with the current value."""
            if is_int:
                value_input.setText(f"{int(current_val[0])}")
            else:
                value_input.setText(f"{current_val[0]:.1f}")
        
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
            except (ValueError, TypeError):
                # If invalid, revert to current value
                update_display()
        
        def increment():
            new_val = min(max_val, current_val[0] + step)
            current_val[0] = new_val
            update_display()
            # Trigger callback if available
            if hasattr(container, '_on_value_changed'):
                container._on_value_changed(new_val)
        
        def decrement():
            new_val = max(min_val, current_val[0] - step)
            current_val[0] = new_val
            update_display()
            # Trigger callback if available
            if hasattr(container, '_on_value_changed'):
                container._on_value_changed(new_val)
        
        def get_value():
            return current_val[0]
        
        def set_value(val):
            current_val[0] = max(min_val, min(max_val, val))
            update_display()
        
        def on_text_changed():
            validate_and_set_value(value_input.text())
            # Trigger callback if available
            if hasattr(container, '_on_value_changed'):
                container._on_value_changed(current_val[0])
        
        # Connect signals
        inc_btn.clicked.connect(increment)
        dec_btn.clicked.connect(decrement)
        value_input.editingFinished.connect(on_text_changed)
        value_input.returnPressed.connect(on_text_changed)
        
        # Initial display
        update_display()
        
        # Attach getter/setter to container for easy access
        container.get_value = get_value
        container.set_value = set_value
        
        return container

    def _create_log_group(self):
        frame = QFrame()
        # Set size policy to not expand - only use space needed
        frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        vbox = QVBoxLayout(frame)
        vbox.setSpacing(5)  # Tight spacing
        vbox.setContentsMargins(5, 2, 5, 2)  # Minimal margins
        vbox.setAlignment(Qt.AlignmentFlag.AlignTop)  # Align to top, don't expand
        
        log_label = QLabel("Communication Log")
        log_label.setContentsMargins(0, 0, 0, 0)  # Remove padding
        log_label.setMargin(0)  # Remove margin
        vbox.addWidget(log_label)
        
        self.log_text = QTextEdit()
        self.log_text.setObjectName("Log")
        self.log_text.setReadOnly(True)
        # Reduce height by ~30% - set maximum height to limit expansion
        self.log_text.setMaximumHeight(250)
        # Set size policy to not expand vertically
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        vbox.addWidget(self.log_text, 0)  # Don't allow expansion
        
        clear_button = create_button("Clear Log", "primary")
        clear_button.clicked.connect(self.log_text.clear)
        vbox.addWidget(clear_button, 0, Qt.AlignmentFlag.AlignRight)
        return frame

    def _create_status_section(self):
        """Creates the Current Status section to be displayed outside tabs."""
        status_frame = QFrame()
        # Set size policy to not expand - only use space needed
        status_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        status_layout = QVBoxLayout(status_frame)
        status_layout.setSpacing(10)
        status_layout.addWidget(QLabel("<b>Current Status</b>"))

        # Grid for Syringe Statuses
        status_grid = QGridLayout()
        status_grid.setColumnStretch(1, 1) # Status column stretch
        status_layout.addLayout(status_grid)

        # Labels storage initialization
        self.status_labels = {
            0: {'status': None, 'port': None, 'volume': None},
            1: {'status': None, 'port': None, 'volume': None}
        }

        # Syringe 1
        status_grid.addWidget(QLabel("S1 Status:"), 0, 0)
        self.status_labels[0]['status'] = QLabel("Not Initialized")
        self.status_labels[0]['status'].setStyleSheet("background-color: red; color: white; padding: 4px; border-radius: 4px;")
        self.status_labels[0]['status'].setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_labels[0]['status'].setAutoFillBackground(True)
        # Reduce horizontal space by setting a maximum width (e.g., 200px) or using size policy
        self.status_labels[0]['status'].setMaximumWidth(200) 
        status_grid.addWidget(self.status_labels[0]['status'], 0, 1)

        status_grid.addWidget(QLabel("S1 Port:"), 0, 2)
        self.status_labels[0]['port'] = QLabel("-")
        status_grid.addWidget(self.status_labels[0]['port'], 0, 3)

        status_grid.addWidget(QLabel("S1 Volume (uL):"), 0, 4)
        self.status_labels[0]['volume'] = QLabel("0.0")
        status_grid.addWidget(self.status_labels[0]['volume'], 0, 5)

        # Syringe 2
        status_grid.addWidget(QLabel("S2 Status:"), 1, 0)
        self.status_labels[1]['status'] = QLabel("Not Initialized")
        self.status_labels[1]['status'].setStyleSheet("background-color: red; color: white; padding: 4px; border-radius: 4px;")
        self.status_labels[1]['status'].setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_labels[1]['status'].setAutoFillBackground(True)
        # Reduce horizontal space by setting a maximum width
        self.status_labels[1]['status'].setMaximumWidth(200)
        status_grid.addWidget(self.status_labels[1]['status'], 1, 1)

        status_grid.addWidget(QLabel("S2 Port:"), 1, 2)
        self.status_labels[1]['port'] = QLabel("-")
        status_grid.addWidget(self.status_labels[1]['port'], 1, 3)

        status_grid.addWidget(QLabel("S2 Volume (uL):"), 1, 4)
        self.status_labels[1]['volume'] = QLabel("0.0")
        status_grid.addWidget(self.status_labels[1]['volume'], 1, 5)

        # System Status Row
        sys_row_layout = QHBoxLayout()
        sys_row_layout.addWidget(QLabel("System status:"))
        
        self.system_status_text = QLabel("Idle")
        sys_row_layout.addWidget(self.system_status_text)
        
        self.status_progress_bar = QProgressBar()
        self.status_progress_bar.setRange(0, 100)
        self.status_progress_bar.setValue(0)
        # Increase height of progress bar for better visibility
        self.status_progress_bar.setMinimumHeight(35)
        self.status_progress_bar.setMaximumHeight(35)
        # Style calculation moved to separate method to handle color change
        self.status_progress_bar.valueChanged.connect(self._update_progress_bar_style)
        self._update_progress_bar_style(0) # Set initial style
        
        sys_row_layout.addWidget(self.status_progress_bar)
        
        status_layout.addLayout(sys_row_layout)
        
        return status_frame

    def _update_progress_bar_style(self, value):
        """Update progress bar style based on value (green when 100%)."""
        base_style = """
            QProgressBar {
                border: 2px solid #ccc;
                border-radius: 5px;
                text-align: center;
                background-color: #f0f0f0;
            }
        """
        
        if value >= 100:
            # Green chunk for completion
            chunk_style = """
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 3px;
            }
            """
        else:
            # Blue chunk for progress
            chunk_style = """
            QProgressBar::chunk {
                background-color: #0078d7;
                border-radius: 3px;
            }
            """
            
        self.status_progress_bar.setStyleSheet(base_style + chunk_style)

    def _create_experimental_controls_tab(self):
        """Creates the Experimental Controls tab with system operation buttons."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(40, 40, 40, 40)

        # Upper section: Horizontal split
        upper_section = QHBoxLayout()
        upper_section.setSpacing(20)

        # Left: Condition Controls
        condition_frame = QFrame()
        condition_layout = QVBoxLayout(condition_frame)
        condition_layout.setSpacing(10)
        condition_layout.addWidget(QLabel("<b>Condition Controls</b>"))
        
        # Run Beads button
        self.run_beads_btn = create_button("Run Beads", "success")
        self.run_beads_btn.setObjectName("SuccessButton")
        self.run_beads_btn.clicked.connect(self._on_run_beads)
        self.run_beads_btn.setMinimumWidth(150)
        condition_layout.addWidget(self.run_beads_btn)
        
        # Run Condition button and Condition selection dropdown (inline)
        condition_button_layout = QHBoxLayout()
        condition_button_layout.setSpacing(10)
        self.run_condition_btn = create_button("Run Condition", "success")
        self.run_condition_btn.setObjectName("SuccessButton")
        self.run_condition_btn.clicked.connect(self._on_run_condition)
        self.run_condition_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        condition_button_layout.addWidget(self.run_condition_btn, 1)  # Stretch factor of 1 (50%)
        
        # Condition selection dropdown
        self.condition_combo = QComboBox()
        self.condition_combo.addItems(["Select condition..."])
        self.condition_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        condition_button_layout.addWidget(self.condition_combo, 1)  # Stretch factor of 1 (50%)
        condition_layout.addLayout(condition_button_layout)
        
        # Clean type: Auto clean / Manual Clean
        clean_mode_group = QButtonGroup(condition_frame)
        auto_clean_radio = QRadioButton("Auto clean")
        manual_clean_radio = QRadioButton("Manual Clean")
        auto_clean_radio.setChecked(True)  # Default to Auto clean
        auto_clean_radio.setObjectName("FilledRadioButton")
        manual_clean_radio.setObjectName("FilledRadioButton")
        clean_mode_group.addButton(auto_clean_radio, 0)
        clean_mode_group.addButton(manual_clean_radio, 1)
        self.clean_mode_group = clean_mode_group
        
        # Sample Volume numeric control (custom increment control)
        sample_volume_layout = QHBoxLayout()
        sample_volume_layout.addStretch()  # Push content to the right
        sample_volume_layout.addWidget(QLabel("Sample Volume (uL):"))
        self.sample_volume_control = self._create_increment_control(
            min_val=0.1, max_val=50.0, initial_val=20.0, step=1, suffix="", is_int=False
        )
        sample_volume_layout.addWidget(self.sample_volume_control)
        condition_layout.addLayout(sample_volume_layout)
        
        # Kickback Controls Header
        kickback_header = QLabel("<b>Kickback controls</b>")
        kickback_header.setStyleSheet("font-size: 12pt; margin-top: 10px; margin-bottom: 5px;")
        condition_layout.addWidget(kickback_header)
        
        # Kickback Controls
        kickback_layout = QHBoxLayout()
        kickback_layout.addStretch()  # Push content to the right
        
        # Timed Kickbacks checkbox
        self.kickback_timed_checkbox = QCheckBox("Timed Kickbacks")
        self.kickback_timed_checkbox.setChecked(self.kickback_timed_enabled)
        # Style checkbox: bright green when enabled, dark gray when disabled
        self.kickback_timed_checkbox.setStyleSheet("""
            QCheckBox {
                font-size: 11pt;
                padding: 5px;
            }
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border: 2px solid #999;
                border-radius: 3px;
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
        self.kickback_timed_checkbox.toggled.connect(self._on_kickback_timed_toggled)
        kickback_layout.addWidget(self.kickback_timed_checkbox)
        
        # Kickback time input
        kickback_layout.addWidget(QLabel("Kickback time (s):"))
        self.kickback_time_control = self._create_increment_control(
            min_val=1.0, max_val=3600.0, initial_val=self.kickback_time_seconds, step=10, suffix="", is_int=False
        )
        # Connect callback for dynamic updates when value changes
        self.kickback_time_control._on_value_changed = lambda val: setattr(self, 'kickback_time_seconds', float(val))
        kickback_layout.addWidget(self.kickback_time_control)
        condition_layout.addLayout(kickback_layout)
        
        # Manual Kickback button with time indicator (next row)
        manual_kickback_layout = QHBoxLayout()
        
        # Manual Kickback button (left side)
        self.manual_kickback_button = QPushButton("Manual Kickback")
        self.manual_kickback_button.setStyleSheet("""
            QPushButton {
                background-color: #FFA500;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 8px 15px;
                border-radius: 5px;
                border: 2px solid #FF8C00;
            }
            QPushButton:hover {
                background-color: #FF8C00;
            }
            QPushButton:pressed {
                background-color: #FF7F00;
            }
            QPushButton:disabled {
                background-color: #888888;
                border: 2px solid #666666;
            }
        """)
        self.manual_kickback_button.clicked.connect(self._on_manual_kickback_clicked)
        manual_kickback_layout.addWidget(self.manual_kickback_button)
        
        # Add stretch to push indicator to the right
        manual_kickback_layout.addStretch()
        
        # Time since last kickback indicator (using pySMR style, right side)
        self.kickback_time_indicator = create_text_indicator("Time since last kickback")
        if hasattr(self.kickback_time_indicator, 'value_label'):
            self.kickback_time_indicator.value_label.setText("--")
        manual_kickback_layout.addWidget(self.kickback_time_indicator)
        condition_layout.addLayout(manual_kickback_layout)
        
        condition_layout.addStretch()
        upper_section.addWidget(condition_frame, 1)

        # Right: Clean Controls
        clean_frame = QFrame()
        clean_layout = QVBoxLayout(clean_frame)
        clean_layout.setSpacing(10)
        clean_layout.addWidget(QLabel("<b>Clean Controls</b>"))
        
        # Clean now button and Cleaning Protocol dropdown (inline)
        clean_button_layout = QHBoxLayout()
        clean_button_layout.setSpacing(10)
        self.clean_between_runs_btn = QPushButton("Clean now")
        self.clean_between_runs_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                font-size: 11pt;
                font-weight: bold;
                padding: 8px 15px;
                border-radius: 5px;
                border: 2px solid #1976D2;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:pressed {
                background-color: #1565C0;
            }
            QPushButton:disabled {
                background-color: #888888;
                border: 2px solid #666666;
            }
        """)
        self.clean_between_runs_btn.clicked.connect(self._on_clean_between_runs)
        self.clean_between_runs_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        clean_button_layout.addWidget(self.clean_between_runs_btn, 1)  # Stretch factor of 1 (50%)
        
        # Cleaning Protocol dropdown
        self.clean_protocol_combo = QComboBox()
        self.clean_protocol_combo.addItems(["Complete Clean", "Rapid Clean", "Media Purge"])
        self.clean_protocol_combo.setCurrentIndex(0)  # Default to Complete Clean
        self.clean_protocol_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        clean_button_layout.addWidget(self.clean_protocol_combo, 1)  # Stretch factor of 1 (50%)
        clean_layout.addLayout(clean_button_layout)
        
        # Autoclean between samples section
        clean_layout.addWidget(QLabel("<b>Auto-clean between samples</b>"))
        
        # Auto clean toggle checkbox and Cleaning Protocol dropdown (inline)
        auto_clean_button_layout = QHBoxLayout()
        auto_clean_button_layout.setSpacing(10)
        self.auto_clean_enabled = False  # Initialize state variable
        self.auto_clean_checkbox = QCheckBox("Auto-clean disabled")
        self.auto_clean_checkbox.setChecked(False)  # Initialize to disabled
        style_checkbox(self.auto_clean_checkbox)
        self.auto_clean_checkbox.toggled.connect(self._on_toggle_auto_clean)
        self.auto_clean_pending = False  # Prevent double-execution of auto clean
        self.auto_clean_checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._update_auto_clean_checkbox_style()  # Set initial style (gray, disabled)
        auto_clean_button_layout.addWidget(self.auto_clean_checkbox, 1)  # Stretch factor of 1 (50%)
        
        # Auto clean Cleaning Protocol dropdown
        self.auto_clean_protocol_combo = QComboBox()
        self.auto_clean_protocol_combo.addItems(["Complete Clean", "Rapid Clean", "Media Purge"])
        self.auto_clean_protocol_combo.setCurrentIndex(0)  # Default to Complete Clean
        self.auto_clean_protocol_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        auto_clean_button_layout.addWidget(self.auto_clean_protocol_combo, 1)  # Stretch factor of 1 (50%)
        clean_layout.addLayout(auto_clean_button_layout)
        
        # Minimum peaks numeric control (custom increment control, step size 500)
        peaks_layout = QHBoxLayout()
        peaks_layout.addStretch()  # Push content to the right
        peaks_layout.addWidget(QLabel("Minimum peaks:"))
        self.minimum_peaks_control = self._create_increment_control(
            min_val=1, max_val=50000, initial_val=8000, step=500, suffix="", is_int=True
        )
        peaks_layout.addWidget(self.minimum_peaks_control)
        clean_layout.addLayout(peaks_layout)
        
        # Minimum volume numeric control (custom increment control, step size 0.5 µL)
        volume_layout = QHBoxLayout()
        volume_layout.addStretch()  # Push content to the right
        volume_layout.addWidget(QLabel("Minimum Volume (uL):"))
        self.minimum_volume_control = self._create_increment_control(
            min_val=0.1, max_val=1000.0, initial_val=8.0, step=0.5, suffix="", is_int=False
        )
        volume_layout.addWidget(self.minimum_volume_control)
        clean_layout.addLayout(volume_layout)
        
        clean_layout.addStretch()
        upper_section.addWidget(clean_frame, 1)

        main_layout.addLayout(upper_section)

        # Bottom: System Maintenance Section
        maintenance_frame = QFrame()
        maintenance_layout = QVBoxLayout(maintenance_frame)
        maintenance_layout.setSpacing(10)
        maintenance_layout.addWidget(QLabel("<b>System Maintenance</b>"))
        
        # Buttons on same line
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(10)
        
        self.prime_system_btn = create_button("Prime system", "success")
        self.prime_system_btn.clicked.connect(self._on_prime_system)
        buttons_layout.addWidget(self.prime_system_btn)
        self._update_prime_system_button()  # Set initial state
        
        self.final_clean_btn = create_button("Final clean", "error")
        self.final_clean_btn.clicked.connect(self._on_final_clean)
        buttons_layout.addWidget(self.final_clean_btn)
        
        maintenance_layout.addLayout(buttons_layout)
        
        main_layout.addWidget(maintenance_frame)

        main_layout.addStretch()
        return widget

    @Slot(int, str, str)
    def update_syringe_status_ui(self, syringe_index, status_type, text):
        """Updates the status label style and text for a given syringe."""
        label = self.status_labels[syringe_index]['status']
        label.setText(text)
        
        color_map = {
            "NOT_INIT": "red",
            "READY": "green",
            "BUSY": "orange"
        }
        color = color_map.get(status_type, "gray")
        label.setStyleSheet(f"background-color: {color}; color: white; padding: 4px; border-radius: 4px;")

    def update_syringe_volume_ui(self, syringe_index, volume_ul):
        """Updates the volume label for a given syringe."""
        self.status_labels[syringe_index]['volume'].setText(f"{volume_ul:.1f}")

    def update_syringe_port_ui(self, syringe_index, port_num):
        """Updates the port label for a given syringe."""
        nickname = self.nickname_inputs[syringe_index][port_num].text()
        self.status_labels[syringe_index]['port'].setText(f"{port_num} ({nickname})")

    def _create_routines_tab(self):
        routine_widget = QWidget()
        main_layout = QHBoxLayout(routine_widget)

        # Left side: Routine table container
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        
        # --- Execution buttons (Moved to top of left column) ---
        exec_box = QFrame()
        exec_layout = QGridLayout(exec_box)
        exec_layout.setContentsMargins(0, 0, 0, 0) # Minimize margins
        exec_layout.addWidget(QLabel("<b>Execution Control</b>"), 0, 0, 1, 4, Qt.AlignmentFlag.AlignCenter)
        
        self.run_routine_btn = create_button("Run Routine", "success")
        self.run_routine_btn.setObjectName("SuccessButton")
        self.run_routine_btn.clicked.connect(self._run_routine)
        
        self.pause_routine_btn = create_button("Pause", "warning")
        self.pause_routine_btn.setCheckable(True)
        self.pause_routine_btn.clicked.connect(self._toggle_pause_routine)

        self.stop_routine_btn = create_button("Stop", "error")
        self.stop_routine_btn.setObjectName("DangerButton")
        self.stop_routine_btn.clicked.connect(self._stop_routine)

        # Arrange buttons horizontally to save vertical space
        exec_layout.addWidget(self.run_routine_btn, 1, 0, 1, 2)
        exec_layout.addWidget(self.pause_routine_btn, 1, 2)
        exec_layout.addWidget(self.stop_routine_btn, 1, 3)
        
        table_layout.addWidget(exec_box)

        # --- Routine Table ---
        self.routine_table = QTableWidget()
        self.routine_table.setColumnCount(7)
        self.routine_table.setHorizontalHeaderLabels(["Step", "Syringe", "Action", "Port", "uL", "uL/min", "Status"])
        self.routine_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.routine_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.routine_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.routine_table.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop) # Disable drag-drop
        self.routine_table.itemChanged.connect(self._on_routine_cell_changed)
        
        # Style selected rows with blue background for better visibility
        self.routine_table.setStyleSheet("""
            QTableWidget::item:selected {
                background-color: #4A90E2;
                color: white;
            }
            QTableWidget::item:selected:active {
                background-color: #357ABD;
                color: white;
            }
        """)

        table_layout.addWidget(QLabel("Routine Sequence"))
        table_layout.addWidget(self.routine_table)
        main_layout.addWidget(table_container, 3)

        # Right side: Controls
        controls_container = QFrame()
        controls_layout = QVBoxLayout(controls_container)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        controls_layout.setSpacing(5) # Reduced spacing
        controls_layout.setContentsMargins(5, 5, 5, 5) # Reduced margins

        # --- Top Section: Add Action and Edit Sequence side-by-side ---
        top_controls_layout = QHBoxLayout()
        top_controls_layout.setContentsMargins(0, 0, 0, 0)

        # Action buttons
        action_box = QFrame()
        action_layout = QVBoxLayout(action_box)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignTop) # Align contents to top
        action_layout.addWidget(QLabel("<b>Add Action</b>"), 0, Qt.AlignmentFlag.AlignCenter)

        add_draw_btn = QPushButton("Draw")
        add_draw_btn.clicked.connect(lambda: self._add_routine_action("Draw"))
        add_dispense_btn = QPushButton("Dispense")
        add_dispense_btn.clicked.connect(lambda: self._add_routine_action("Dispense"))
        add_valve_btn = QPushButton("Move Valve")
        add_valve_btn.clicked.connect(lambda: self._add_routine_action("Move Valve"))
        add_home_btn = QPushButton("Recalibrate")
        add_home_btn.clicked.connect(lambda: self._add_routine_action("Home"))
        add_empty_btn = QPushButton("Empty Syringe")
        add_empty_btn.clicked.connect(lambda: self._add_routine_action("Empty Syringe"))
        add_prime_btn = QPushButton("Prime Reagent")
        add_prime_btn.clicked.connect(lambda: self._add_routine_action("Prime Reagent"))
        add_wait_btn = QPushButton("Wait")
        add_wait_btn.clicked.connect(lambda: self._add_routine_action("Wait"))
        add_interrupt_btn = QPushButton("Interrupt")
        add_interrupt_btn.clicked.connect(lambda: self._add_routine_action("Interrupt"))
        
        action_layout.addWidget(add_draw_btn)
        action_layout.addWidget(add_dispense_btn)
        action_layout.addWidget(add_valve_btn)
        action_layout.addWidget(add_home_btn)
        action_layout.addWidget(add_empty_btn)
        action_layout.addWidget(add_prime_btn)
        action_layout.addWidget(add_wait_btn)
        action_layout.addWidget(add_interrupt_btn)
        
        # Edit buttons
        edit_box = QFrame()
        edit_layout = QVBoxLayout(edit_box)
        edit_layout.setAlignment(Qt.AlignmentFlag.AlignTop) # Align contents to top
        edit_layout.addWidget(QLabel("<b>Edit Sequence</b>"), 0, Qt.AlignmentFlag.AlignCenter)
        
        move_up_btn = create_button("Move Up", "primary")
        move_up_btn.clicked.connect(lambda: self._move_routine_row(-1))
        
        move_down_btn = create_button("Move Down", "primary")
        move_down_btn.clicked.connect(lambda: self._move_routine_row(1))
        
        repeat_selection_btn = create_button("Repeat selection", "primary")
        repeat_selection_btn.clicked.connect(self._repeat_selection)

        delete_row_btn = create_button("Delete Selected", "error")
        delete_row_btn.setObjectName("DangerButton")
        delete_row_btn.clicked.connect(self._delete_routine_row)
        
        clear_routine_btn = create_button("Clear Routine", "error")
        clear_routine_btn.setObjectName("DangerButton")
        clear_routine_btn.clicked.connect(self._clear_routine)
        
        edit_layout.addWidget(move_up_btn)
        edit_layout.addWidget(move_down_btn)
        edit_layout.addWidget(repeat_selection_btn)
        edit_layout.addWidget(delete_row_btn)
        edit_layout.addWidget(clear_routine_btn)

        top_controls_layout.addWidget(action_box)
        top_controls_layout.addWidget(edit_box)
        controls_layout.addLayout(top_controls_layout)

        # File buttons
        file_box = QFrame()
        file_layout = QFormLayout(file_box)
        file_layout.setContentsMargins(20,20,20,20)
        file_layout.addWidget(QLabel("<b>File Management</b>"))
        
        self.routine_load_combo = QComboBox()
        self._refresh_routine_list()
        self.routine_load_combo.activated.connect(self._load_routine)
        file_layout.addRow("Load Routine:", self.routine_load_combo)
        
        self.routine_filename_input = QLineEdit("my_routine")
        style_input_field(self.routine_filename_input)
        file_layout.addRow("Save as:", self.routine_filename_input)

        save_routine_btn = QPushButton("Save Current Routine")
        save_routine_btn.clicked.connect(self._save_routine)
        file_layout.addWidget(save_routine_btn)
        controls_layout.addWidget(file_box)

        main_layout.addWidget(controls_container, 1)
        return routine_widget

    # --- Routine Management Methods ---

    def get_cell_text(self, row, col):
        """Helper to get text from cell whether it's an item or a widget."""
        widget = self.routine_table.cellWidget(row, col)
        if isinstance(widget, QComboBox):
            return widget.currentText()
        item = self.routine_table.item(row, col)
        return item.text() if item else ""

    def _get_row_data(self, row):
        return {
            'step': self.routine_table.item(row, 0).text(),
            'syringe_text': self.get_cell_text(row, 1),
            'action': self.get_cell_text(row, 2),
            'param1': self.get_cell_text(row, 3),  # Port for Draw/Dispense/Move Valve
            'param2': self.get_cell_text(row, 4),  # Volume (uL) for Draw/Dispense/Wait
            'param3': self.get_cell_text(row, 5),  # Rate (uL/min) for Draw/Dispense
            'status': self.get_cell_text(row, 6) 
        }

    def _set_row_data(self, row, data):
        self.routine_table.blockSignals(True)
        
        # Step
        self.routine_table.setItem(row, 0, NumericTableWidgetItem(str(data.get('step', 1))))
        
        # Syringe Combo
        syringe_combo = QComboBox()
        # Allow popup to be wider than the cell
        syringe_combo.setStyleSheet("QComboBox { combobox-popup: 0; }") 
        syringe_combo.view().setMinimumWidth(100)
        
        syringe_combo.addItems(["S1", "S2"])
        s_text = data.get('syringe_text', "S1")
        idx = syringe_combo.findText(s_text)
        if idx >= 0: syringe_combo.setCurrentIndex(idx)
        syringe_combo.currentIndexChanged.connect(lambda: self._on_row_config_changed(syringe_combo))
        self.routine_table.setCellWidget(row, 1, syringe_combo)
        
        # Action Combo
        action_combo = QComboBox()
        # Allow popup to be wider than the cell
        action_combo.setStyleSheet("QComboBox { combobox-popup: 0; }") 
        action_combo.view().setMinimumWidth(200)

        action_combo.addItems(["Draw", "Dispense", "Move Valve", "Home", "Empty Syringe", "Prime Reagent", "Wait", "Interrupt"])
        a_text = data.get('action', "Draw")
        idx = action_combo.findText(a_text)
        if idx >= 0: action_combo.setCurrentIndex(idx)
        action_combo.currentIndexChanged.connect(lambda: self._on_row_config_changed(action_combo))
        self.routine_table.setCellWidget(row, 2, action_combo)
        
        # Param 1 (Dynamic - Port for Draw/Dispense/Move Valve)
        self._update_param1_widget(row, initial_value=data.get('param1', ''))
        
        # Param 2 (Volume in uL for Draw/Dispense/Wait)
        self.routine_table.setItem(row, 4, QTableWidgetItem(str(data.get('param2', ''))))
        
        # Param 3 (Rate in uL/min for Draw/Dispense)
        self._update_param3_widget(row, initial_value=data.get('param3', ''))
        
        # Status
        status_item = QTableWidgetItem(str(data.get('status', 'Pending')))
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.routine_table.setItem(row, 6, status_item)
        
        self.routine_table.blockSignals(False)

    def _on_row_config_changed(self, sender_widget):
        for row in range(self.routine_table.rowCount()):
            if self.routine_table.cellWidget(row, 1) == sender_widget or \
               self.routine_table.cellWidget(row, 2) == sender_widget or \
               self.routine_table.cellWidget(row, 3) == sender_widget:
                # If Action, Syringe, or Port changed, we might need to update widgets
                # _update_param1_widget handles the logic of switching widget types
                current_val = self.get_cell_text(row, 3)
                self._update_param1_widget(row, initial_value=current_val)
                # Also update param3 (rate) if it's Draw/Dispense
                current_rate = self.get_cell_text(row, 5)
                self._update_param3_widget(row, initial_value=current_rate)
                break

    def _update_param1_widget(self, row, initial_value=""):
        action_combo = self.routine_table.cellWidget(row, 2)
        syringe_combo = self.routine_table.cellWidget(row, 1)
        
        if not action_combo or not syringe_combo: return
        
        action = action_combo.currentText()
        syringe_text = syringe_combo.currentText()
        syringe_idx = 0 if "1" in syringe_text else 1
        
        current_val = str(initial_value) if initial_value is not None else self.get_cell_text(row, 3)
        
        if action in ["Move Valve", "Draw", "Dispense", "Prime Reagent"]:
            # Create ComboBox for Ports with Nicknames
            combo = QComboBox()
            # Allow popup to be wider than the cell to show full nicknames
            combo.setStyleSheet("QComboBox { combobox-popup: 0; }") 
            combo.view().setMinimumWidth(250)
            
            for port in range(1, 7):
                nickname = self.nickname_inputs[syringe_idx][port].text()
                combo.addItem(f"{port} ({nickname})")
            
            # Try to set current index
            target_port = ""
            if current_val:
                # Extract number
                match = re.match(r"(\d+)", str(current_val))
                if match:
                    target_port = match.group(1)
            
            if target_port:
                for i in range(combo.count()):
                    if combo.itemText(i).startswith(f"{target_port} "):
                        combo.setCurrentIndex(i)
                        break
            else:
                # Default to port 1 if no current value
                combo.setCurrentIndex(0)
            
            combo.currentIndexChanged.connect(lambda: self._on_row_config_changed(combo))
            self.routine_table.setCellWidget(row, 3, combo)
        elif action == "Wait":
            # Wait needs duration (seconds) in param1
            self.routine_table.removeCellWidget(row, 3)
            if "(" in current_val:
                current_val = "1.0"  # Default duration
            elif not current_val or current_val == "-":
                current_val = "1.0"
            self.routine_table.setItem(row, 3, QTableWidgetItem(str(current_val)))
        elif action == "Prime Reagent":
            # Prime Reagent needs port selection (like Move Valve)
            combo = QComboBox()
            combo.setStyleSheet("QComboBox { combobox-popup: 0; }") 
            combo.view().setMinimumWidth(250)
            
            for port in range(1, 7):
                nickname = self.nickname_inputs[syringe_idx][port].text()
                combo.addItem(f"{port} ({nickname})")
            
            # Try to set current index
            target_port = ""
            if current_val:
                match = re.match(r"(\d+)", str(current_val))
                if match:
                    target_port = match.group(1)
            
            if target_port:
                for i in range(combo.count()):
                    if combo.itemText(i).startswith(f"{target_port} "):
                        combo.setCurrentIndex(i)
                        break
            else:
                # Default to port 1 if no current value
                combo.setCurrentIndex(0)
            
            combo.currentIndexChanged.connect(lambda: self._on_row_config_changed(combo))
            self.routine_table.setCellWidget(row, 3, combo)
        else:
            # Home, Empty Syringe, and Interrupt don't need param1
            self.routine_table.removeCellWidget(row, 3)
            self.routine_table.setItem(row, 3, QTableWidgetItem("-"))
    
    def _update_param3_widget(self, row, initial_value=""):
        """Update the rate (uL/min) column for Draw/Dispense actions."""
        action_combo = self.routine_table.cellWidget(row, 2)
        syringe_combo = self.routine_table.cellWidget(row, 1)
        
        if not action_combo or not syringe_combo: return
        
        action = action_combo.currentText()
        syringe_text = syringe_combo.currentText()
        syringe_idx = 0 if "1" in syringe_text else 1
        
        current_val = str(initial_value) if initial_value is not None else self.get_cell_text(row, 5)
        
        if action in ["Draw", "Dispense"]:
            # Get port to determine rate
            port_text = self.get_cell_text(row, 3)
            port_match = re.match(r"(\d+)", port_text)
            port = int(port_match.group(1)) if port_match else 1
            
            # Get port type to determine default rate
            port_type = self.port_type_combos[syringe_idx][port].currentText()
            action_type = "Draw" if action == "Draw" else "Dispense"
            
            # Get default rate from speed inputs
            default_rate = self.speed_inputs[syringe_idx][port_type][action_type].text()
            if not default_rate or default_rate == "":
                default_rate = "100.0"  # Fallback default
            
            if not current_val or current_val == "-" or current_val == "":
                current_val = default_rate
            
            self.routine_table.setItem(row, 5, QTableWidgetItem(str(current_val)))
        else:
            # Other actions don't need rate
            self.routine_table.setItem(row, 5, QTableWidgetItem("-"))

    def _get_last_valve_positions_from_routine(self):
        """Returns a dict mapping syringe index to last valve position in the routine."""
        last_valve_positions = {0: None, 1: None}
        
        # Scan through all rows in the routine table
        for row in range(self.routine_table.rowCount()):
            try:
                action = self.get_cell_text(row, 2)
                if action == "Move Valve":
                    # Get syringe info
                    s_text = self.get_cell_text(row, 1)
                    if 'Syringe' in s_text or 'S' in s_text:
                        # Extract number regardless of "Syringe 1" or "S1" format
                        match = re.search(r'\d+', s_text)
                        if match:
                            syringe_idx = int(match.group()) - 1
                            if 0 <= syringe_idx <= 1:
                                # Get port number
                                param1_text = self.get_cell_text(row, 3)
                                if param1_text:
                                    match_p = re.match(r"(\d+)", param1_text)
                                    if match_p:
                                        port = int(match_p.group(1))
                                        last_valve_positions[syringe_idx] = port
            except (ValueError, AttributeError, IndexError):
                continue
        
        return last_valve_positions

    def _add_routine_action(self, action_type):
        # Get last valve positions from the routine
        last_valve_positions = self._get_last_valve_positions_from_routine()
        dialog = ActionDialog(action_type, self, last_valve_positions)
        if dialog.exec():
            data = dialog.get_data()
            # Check if "Both" syringes selected (syringe ID = 2)
            syringe_id = data.get('syringe', 0)
            if syringe_id == 2:  # "Both" selected
                # Add two rows - one for S1, one for S2, both with same step number
                # First, determine the step number (before adding any rows)
                step = 1
                if self.routine_table.rowCount() > 0:
                    max_step = 0
                    for row in range(self.routine_table.rowCount()):
                        try:
                            step_val = int(self.routine_table.item(row, 0).text())
                            if step_val > max_step:
                                max_step = step_val
                        except (ValueError, AttributeError):
                            continue
                    step = max_step + (1 if data.get('new_step', False) else 0)
                    if step == 0:
                        step = 1
                
                # Add S1 row
                data_s1 = data.copy()
                data_s1['syringe'] = 0
                data_s1['syringe_text'] = 'S1'
                data_s1['explicit_step'] = step  # Use explicit step to ensure same step number
                data_s1['new_step'] = False  # Don't increment step for second row
                self._add_row_to_routine_table(data_s1)
                
                # Add S2 row with same step (use explicit_step to override step calculation)
                data_s2 = data.copy()
                data_s2['syringe'] = 1
                data_s2['syringe_text'] = 'S2'
                data_s2['explicit_step'] = step  # Use explicit step to ensure same step number
                data_s2['new_step'] = False  # Don't increment step
                self._add_row_to_routine_table(data_s2)
            else:
                self._add_row_to_routine_table(data)
    
    def _add_row_to_routine_table(self, data, at_row=None):
        self.routine_table.blockSignals(True)
        
        if at_row is None:
            at_row = self.routine_table.rowCount()
        
        self.routine_table.insertRow(at_row)

        step = 1
        
        # Check if an explicit step number is provided (e.g. from loading a file)
        if 'explicit_step' in data:
            try:
                step = int(data['explicit_step'])
            except ValueError:
                step = 1
        elif self.routine_table.rowCount() > 1:
            # Find the max step number in the table and add 1
            max_step = 0
            for row in range(self.routine_table.rowCount() -1): # Exclude the new row
                try:
                    step_val = int(self.routine_table.item(row, 0).text())
                    if step_val > max_step:
                        max_step = step_val
                except (ValueError, AttributeError):
                    continue # Skip empty or invalid cells
            step = max_step + (1 if data['new_step'] else 0)
            if step == 0: step = 1

        # Populate the row data using the helper method which handles widgets
        row_data = {
            'step': step,
            'syringe_text': data.get('syringe_text', 'S1'),
            'action': data.get('action', 'Draw'),
            'param1': data.get('param1', ''),
            'param2': data.get('param2', ''),
            'param3': data.get('param3', ''),
            'status': 'Pending'
        }
        self._set_row_data(at_row, row_data)
        
        self.routine_table.blockSignals(False)
        self.routine_table.sortItems(0, Qt.SortOrder.AscendingOrder)

    @Slot(QTableWidgetItem)
    def _on_routine_cell_changed(self, item):
        # Only perform logic if the Step column (0) was changed
        if item.column() == 0:
            self.routine_table.blockSignals(True)
            try:
                new_step = int(item.text())
                if new_step <= 0:
                    raise ValueError("Step must be positive")
                self.routine_table.sortItems(0, Qt.SortOrder.AscendingOrder)
            except ValueError:
                self.show_error("Invalid step number. Please enter a positive integer.")
                # Force re-sort to potentially correct order or handle invalid input gracefully
                self.routine_table.sortItems(0, Qt.SortOrder.AscendingOrder)
            finally:
                self.routine_table.blockSignals(False)
        # Changes to columns 3 and 4 are allowed without immediate action; validation occurs at run time.
            
    def _move_routine_row(self, direction):
        current_row = self.routine_table.currentRow()
        if current_row < 0: return
        new_row = current_row + direction
        if not (0 <= new_row < self.routine_table.rowCount()): return
        
        data_current = self._get_row_data(current_row)
        data_target = self._get_row_data(new_row)
        
        self._set_row_data(current_row, data_target)
        self._set_row_data(new_row, data_current)
            
        self.routine_table.setCurrentCell(new_row, 0)


    def _delete_routine_row(self):
        """Delete all selected rows from the routine table."""
        # Get all selected rows
        selected_indexes = self.routine_table.selectedIndexes()
        if not selected_indexes:
            # Fall back to current row if no selection
            current_row = self.routine_table.currentRow()
            if current_row >= 0:
                self.routine_table.removeRow(current_row)
                self._renumber_all_steps()
            return
        
        # Extract unique row indices from selected cells
        selected_rows = sorted(set(idx.row() for idx in selected_indexes), reverse=True)
        
        if not selected_rows:
            return
        
        # Block signals to prevent sorting during deletion
        self.routine_table.blockSignals(True)
        
        # Delete rows from bottom to top to avoid index shifting issues
        for row_idx in selected_rows:
            if 0 <= row_idx < self.routine_table.rowCount():
                self.routine_table.removeRow(row_idx)
        
        self.routine_table.blockSignals(False)
        
        # Renumber all steps sequentially after deletion
        self._renumber_all_steps()
        
        # Sort by step number to ensure proper order
        self.routine_table.sortItems(0, Qt.SortOrder.AscendingOrder)

    def _clear_routine(self):
        """Clear the entire routine table to a blank slate."""
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setText("Are you sure you want to clear the entire routine?")
        msg_box.setWindowTitle("Clear Routine")
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        msg_box.button(QMessageBox.StandardButton.Ok).setText("OK - clear the routine")
        msg_box.button(QMessageBox.StandardButton.Cancel).setText("Cancel")
        
        reply = msg_box.exec()
        if reply == QMessageBox.StandardButton.Ok:
            self.routine_table.setRowCount(0)

    def _renumber_all_steps(self):
        """Renumber all steps in the routine table sequentially starting from 1, preserving step groupings."""
        self.routine_table.blockSignals(True)
        
        # First, group rows by their current step number
        step_to_rows = {}
        for row in range(self.routine_table.rowCount()):
            step_item = self.routine_table.item(row, 0)
            if step_item:
                try:
                    step_num = int(step_item.text())
                    if step_num not in step_to_rows:
                        step_to_rows[step_num] = []
                    step_to_rows[step_num].append(row)
                except ValueError:
                    continue
        
        # Sort step numbers to maintain order
        sorted_steps = sorted(step_to_rows.keys())
        
        # Assign new sequential step numbers to each group
        new_step = 1
        for old_step in sorted_steps:
            # All rows with this step number get the same new step number
            for row in step_to_rows[old_step]:
                step_item = self.routine_table.item(row, 0)
                if step_item:
                    step_item.setText(str(new_step))
            new_step += 1
        
        self.routine_table.blockSignals(False)

    def _repeat_selection(self):
        """Copy selected rows and append them to the end of the routine, then renumber all steps."""
        # Get all selected rows
        selected_indexes = self.routine_table.selectedIndexes()
        if not selected_indexes:
            self.show_error("Please select one or more steps to repeat.")
            return
        
        # Extract unique row indices from selected cells
        selected_rows = sorted(set(idx.row() for idx in selected_indexes))
        
        if not selected_rows:
            self.show_error("Please select one or more steps to repeat.")
            return
        
        # Extract data from selected rows and group by step number
        step_to_data_map = {}  # Maps original step number to list of row data
        
        for row_idx in selected_rows:
            row_data = self._get_row_data(row_idx)
            step_num = int(row_data.get('step', 1))
            
            if step_num not in step_to_data_map:
                step_to_data_map[step_num] = []
            step_to_data_map[step_num].append(row_data)
        
        # Find the maximum step number currently in the table
        max_step = 0
        for row in range(self.routine_table.rowCount()):
            try:
                step_val = int(self.routine_table.item(row, 0).text())
                if step_val > max_step:
                    max_step = step_val
            except (ValueError, AttributeError):
                continue
        
        # Block signals to prevent sorting during addition
        self.routine_table.blockSignals(True)
        
        # Get unique step numbers from selected rows, sorted
        unique_steps = sorted(step_to_data_map.keys())
        
        # Add new rows at the end with copied data
        # Assign new step numbers that preserve the grouping
        new_step_counter = max_step + 1
        for original_step in unique_steps:
            # All rows with this step number get the same new step number
            for data in step_to_data_map[original_step]:
                # Add row
                at_row = self.routine_table.rowCount()
                self.routine_table.insertRow(at_row)
                
                # Set row data - use same step number for all rows that had the same original step
                row_data_dict = {
                    'step': new_step_counter,  # Same step number for rows from same original step
                    'syringe_text': data.get('syringe_text', 'S1'),
                    'action': data.get('action', 'Draw'),
                    'param1': data.get('param1', ''),
                    'param2': data.get('param2', ''),
                    'param3': data.get('param3', ''),
                    'status': 'Pending'
                }
                # Call _set_row_data which handles widget creation
                # It will block/unblock signals internally, but we'll re-block after
                self._set_row_data(at_row, row_data_dict)
                # Re-block signals for next iteration
                self.routine_table.blockSignals(True)
            # Move to next step number for the next group
            new_step_counter += 1
        
        self.routine_table.blockSignals(False)
        
        # Renumber all steps sequentially
        self._renumber_all_steps()
        
        # Sort by step number to ensure proper order
        self.routine_table.sortItems(0, Qt.SortOrder.AscendingOrder)

    def _refresh_routine_list(self):
        self.routine_load_combo.clear()
        self.routine_load_combo.addItem("Select routine to load...", "")
        try:
            files = [f for f in os.listdir(ROUTINE_SUBDIR) if f.endswith('.csv')]
            for f in sorted(files):
                self.routine_load_combo.addItem(f, os.path.join(ROUTINE_SUBDIR, f))
        except FileNotFoundError:
            pass

    def _save_routine(self):
        filename = self.routine_filename_input.text()
        if not filename:
            self.show_error("Please enter a filename for the routine.")
            return
        
        # Remove .csv extension if present for checking
        base_name = filename
        if base_name.endswith('.csv'):
            base_name = base_name[:-4]
        
        # Check if a locked version of this routine exists
        locked_filename = f"lock.{base_name}.csv"
        locked_path = os.path.join(ROUTINE_SUBDIR, locked_filename)
        if os.path.exists(locked_path):
            self.show_error(f"Cannot save routine '{base_name}': A locked version exists ('{locked_filename}'). Locked routines cannot be overwritten.")
            return
        
        # Ensure filename has .csv extension
        if not filename.endswith('.csv'):
            filename += '.csv'
            
        filepath = os.path.join(ROUTINE_SUBDIR, filename)
        
        try:
            with open(filepath, 'w', newline='') as f:
                writer = csv.writer(f)
                headers = ['step', 'syringe', 'action', 'port', 'uL', 'uL/min']
                writer.writerow(headers)

                for row in range(self.routine_table.rowCount()):
                    step = self.routine_table.item(row, 0).text()
                    syringe_text = self.get_cell_text(row, 1)
                    action = self.get_cell_text(row, 2)
                    port = self.get_cell_text(row, 3)
                    volume = self.get_cell_text(row, 4)
                    rate = self.get_cell_text(row, 5)
                    
                    # Cleanup Syringe text to be just '0' or '1'
                    syringe_idx = '0'
                    if '1' in syringe_text: syringe_idx = '0'
                    elif '2' in syringe_text: syringe_idx = '1'
                    
                    # Extract port number from "1 (Water)" format
                    port_match = re.match(r"(\d+)", port)
                    port_num = port_match.group(1) if port_match else ""
                    
                    row_data = [step, syringe_idx, action, port_num, volume, rate]
                    writer.writerow(row_data)

            self.update_status_message(f"Routine saved to {filename}", 3000)
            self._refresh_routine_list()
        except IOError as e:
            self.show_error(f"Error saving routine: {e}")

    def _get_total_steps_in_sequence(self, sequence_names):
        """
        Calculate the total number of unique steps across a list of routines.
        
        Args:
            sequence_names: List of routine names (e.g., ['Rapid Clean', 'Purge'])
            
        Returns:
            Total count of unique step numbers across all routines found.
        """
        total_steps = 0
        for name in sequence_names:
            path = self.find_routine_file(name)
            if not path:
                continue
            
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    steps = set()
                    for row in reader:
                        if 'step' in row and row['step']:
                            steps.add(row['step'])
                    total_steps += len(steps)
            except Exception as e:
                print(f"Error counting steps in {path}: {e}")
        
        return total_steps

    def find_routine_file(self, routine_name):
        """
        Find a routine file, preferring locked versions over unlocked ones.
        
        Args:
            routine_name: Name of the routine without .csv extension (e.g., "Rapid Clean")
            
        Returns:
            Full filepath to the routine file, or None if not found.
            Prefers "lock.{routine_name}.csv" over "{routine_name}.csv"
        """
        # Remove .csv extension if present
        if routine_name.endswith('.csv'):
            routine_name = routine_name[:-4]
        
        # Check for locked version first
        locked_filename = f"lock.{routine_name}.csv"
        locked_path = os.path.join(ROUTINE_SUBDIR, locked_filename)
        if os.path.exists(locked_path):
            return locked_path
        
        # Fall back to unlocked version
        unlocked_filename = f"{routine_name}.csv"
        unlocked_path = os.path.join(ROUTINE_SUBDIR, unlocked_filename)
        if os.path.exists(unlocked_path):
            return unlocked_path
        
        return None

    def load_routine_from_file(self, filepath):
        """Helper to load a routine from a specific filepath."""
        try:
            with open(filepath, 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None) 
                if not header: return False

                self.routine_table.setRowCount(0)

                for i, row_data in enumerate(reader):
                    # Handle both old format (5 columns) and new format (6 columns)
                    if len(row_data) >= 5:
                        data = {
                            'explicit_step': row_data[0], # Pass the exact step number from file
                            'syringe_text': f"S{int(row_data[1]) + 1}" if row_data[1] else "N/A",
                            'action': row_data[2],
                            'param1': row_data[3] if len(row_data) > 3 else "",  # Port
                            'param2': row_data[4] if len(row_data) > 4 else "",  # Volume (uL)
                            'param3': row_data[5] if len(row_data) > 5 else "",  # Rate (uL/min)
                            'new_step': False # Ignored when explicit_step is present
                        }
                        self._add_row_to_routine_table(data, at_row=i)

            filename = os.path.basename(filepath)
            # Strip "lock." prefix from filename for display in input field
            display_name = os.path.splitext(filename)[0]
            if display_name.startswith("lock."):
                display_name = display_name[5:]  # Remove "lock." prefix
            self.routine_filename_input.setText(display_name)
            self.update_status_message(f"Loaded routine: {filename}", 3000)
            self.routine_table.sortItems(0, Qt.SortOrder.AscendingOrder)
            return True

        except (IOError, IndexError, ValueError) as e:
            self.show_error(f"Error loading or parsing routine file: {e}")
            return False

    def _load_routine(self, index):
        filepath = self.routine_load_combo.itemData(index)
        if not filepath:
            return
        
        self.load_routine_from_file(filepath)
        self.routine_load_combo.setCurrentIndex(0)

    def _run_routine(self):
        """Execute the currently loaded routine sequence."""
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected. Cannot run routine.")
            return

        # Thread Safety Guard: Prevent overlapping routine threads
        if self.routine_thread and self.routine_thread.isRunning():
            self.show_busy_abort_dialog("A fluidic routine is already in progress. Please wait for it to finish.")
            return

        routine_data = []
        for row in range(self.routine_table.rowCount()):
            try:
                # Use get_cell_text to grab data
                action = self.get_cell_text(row, 2)
                if not action: continue
                
                s_text = self.get_cell_text(row, 1)
                # Parse syringe index whether "Syringe 1" or "S1"
                s_idx = None
                if 'Syringe' in s_text or 'S' in s_text:
                     match = re.search(r'\d+', s_text)
                     if match:
                         s_idx = int(match.group()) - 1

                port_text = self.get_cell_text(row, 3)
                volume_text = self.get_cell_text(row, 4)
                rate_text = self.get_cell_text(row, 5)

                data = {
                    'step': int(self.routine_table.item(row, 0).text()),
                    'syringe': s_idx,
                    'action': action
                }
                
                if action == "Move Valve":
                    # Parse "1 (Water)" -> 1
                    match = re.match(r"(\d+)", port_text)
                    val = int(match.group(1)) if match else 1
                    data['param1'] = val
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Home":
                    # Home doesn't need params
                    data['param1'] = None
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Empty Syringe":
                    # Empty Syringe doesn't need params
                    data['param1'] = None
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Prime Reagent":
                    # Prime Reagent needs port (param1)
                    match = re.match(r"(\d+)", port_text)
                    port = int(match.group(1)) if match else 1
                    data['param1'] = port
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Wait":
                    # Wait needs duration (seconds) in param1 (port column repurposed)
                    try:
                        data['param1'] = float(port_text) if port_text and port_text != "-" else 0.0
                    except (ValueError, TypeError):
                        data['param1'] = 0.0
                    data['param2'] = None
                    data['param3'] = None
                else: # Draw, Dispense
                    # param1 is port, param2 is volume (uL), param3 is rate (uL/min)
                    match = re.match(r"(\d+)", port_text)
                    port = int(match.group(1)) if match else 1
                    try:
                        volume = float(volume_text) if volume_text and volume_text != "-" else 0.0
                        rate = float(rate_text) if rate_text and rate_text != "-" else 0.0
                    except (ValueError, TypeError):
                        volume = 0.0
                        rate = 0.0
                    data['param1'] = port
                    data['param2'] = volume
                    data['param3'] = rate
                
                routine_data.append(data)
            except (ValueError, TypeError, IndexError) as e:
                self.show_error(f"Invalid data in routine table at row {row + 1}: {e}")
                return

        if not routine_data:
            self.show_error("Routine is empty. Nothing to run.")
            return

        self._update_routine_ui_state(running=True)
        # Reset progress bar
        self.status_progress_bar.setValue(0)
        
        # Get routine name for status display
        routine_name = self.routine_filename_input.text() or "routine"
        self.system_status_text.setText(f"Executing routine: {routine_name}")
        
        self.routine_thread = RoutineThread(routine_data, self)
        
        # Connect signals
        self.routine_thread.update_status.connect(self._update_routine_row_status)
        self.routine_thread.routine_finished.connect(self._on_routine_finished)
        self.routine_thread.step_changed.connect(self._highlight_step)
        self.routine_thread.progress_updated.connect(self._update_routine_progress)
        
        # Connect Auto Clean dialog if active
        if hasattr(self, 'auto_clean_dialog') and self.auto_clean_dialog:
            self.routine_thread.progress_updated.connect(self.auto_clean_dialog.update_progress)
            self.routine_thread.update_status.connect(lambda row, msg, color: self.auto_clean_dialog.update_status(msg))
        
        # CONNECT STATUS UPDATE SIGNAL FOR ROBUST ACKNOWLEDGEMENT
        # Using DirectConnection so ACKs are processed immediately in the communication thread's context
        # This bypasses the main GUI thread's event loop and avoids timeouts due to UI congestion.
        self.comm_thread.status_update.connect(self.routine_thread.on_status_update, Qt.DirectConnection)
        
        self.routine_thread.start()
        self.update_status_message("Routine started...")

    def _toggle_pause_routine(self, checked):
        if self.routine_thread and self.routine_thread.isRunning():
            if checked:
                self.routine_thread.pause()
                self.pause_routine_btn.setText("Resume")
                self.update_status_message("Routine paused.")
            else:
                self.routine_thread.resume()
                self.pause_routine_btn.setText("Pause")
                self.update_status_message("Routine resumed.")

    def _stop_routine(self):
        if self.routine_thread:
            self.routine_thread.stop()

    def _on_routine_finished(self, message):
        self.update_status_message(message, 5000)
        self._update_routine_ui_state(running=False)
        # Reset system status
        self.system_status_text.setText("Idle")
        # Set progress to 100% if completed successfully, otherwise reset to 0
        if "successfully" in message.lower():
            self.status_progress_bar.setValue(100)
        else:
            self.status_progress_bar.setValue(0)
        
        # If this was a clean routine, update condition indicator to "Idle"
        if self.is_clean_routine:
            if self.smr_widget and hasattr(self.smr_widget, '_update_current_condition'):
                self.smr_widget._update_current_condition("Idle")
            self.is_clean_routine = False
            
            # Update fluidic state to IDLE
            self.current_fluidic_state = self.FLUIDIC_STATE_IDLE
            self.fluidic_state_changed.emit(self.FLUIDIC_STATE_IDLE, "Idle")
        
        self.routine_thread = None
    
    def _on_prime_routine_finished(self, message):
        """Handle completion of a routine in the prime system sequence."""
        # Check if routine completed successfully
        if "Aborted" in message or "Stopped" in message or "Error" in message.lower():
            self.show_error(f"Prime system sequence failed: {message}")
            self.prime_system_sequence = None
            self._on_routine_finished(message)
            return
        
        # Remove completed routine from sequence
        if self.prime_system_sequence and len(self.prime_system_sequence) > 0:
            self.prime_system_sequence.pop(0)
        
        # Continue with next routine or finish
        if self.prime_system_sequence and len(self.prime_system_sequence) > 0:
            # Disconnect this handler before running next routine
            if self.routine_thread:
                try:
                    self.routine_thread.routine_finished.disconnect(self._on_prime_routine_finished)
                except:
                    pass
            # Run next routine in sequence
            self._run_next_prime_routine()
        else:
            # Sequence complete
            self.prime_system_sequence = None
            self.prime_system_completed = True
            self._update_prime_system_button()
            self.update_status_message("Prime system sequence completed successfully.", 5000)
            self._on_routine_finished("Prime system sequence completed successfully.")
    
    @Slot(int)
    def _update_routine_progress(self, percent):
        """Updates the progress bar with routine execution progress."""
        self.status_progress_bar.setValue(percent)
    
    def _on_kickback_timed_toggled(self, checked):
        """Handle Timed Kickbacks checkbox toggle."""
        self.kickback_timed_enabled = checked
        # REMOVED: self.config_kickback_timed_enabled = checked  # Strictly from config now
        if checked:
            # Start timer if not already running and we have a start time
            if self.last_kickback_time is not None:
                if not self.kickback_timer.isActive():
                    # Reconnect signal to ensure it's properly connected
                    try:
                        self.kickback_timer.timeout.disconnect()
                    except:
                        pass
                    self.kickback_timer.timeout.connect(self._check_kickback_timing)
                    self.kickback_timer.start()
        else:
            # Stop timer when disabled
            self.kickback_timer.stop()
    
    def _check_fluidic_debounce(self):
        """Helper to prevent rapid duplicate button presses and overlapping fluidic actions."""
        now = time.time()
        
        # 1. Debounce timer (prevents accidental rapid double-clicks)
        if now - getattr(self, '_last_fluidic_press_time', 0) < DEBOUNCE_INTERVAL_SEC:
            return False
            
        # 2. Routine Busy Check (prevents overlapping routine threads)
        if self.routine_thread and self.routine_thread.isRunning():
            self.show_busy_abort_dialog("A fluidic routine is already in progress. Please wait for it to finish.")
            return False
            
        # 3. Kickback Busy Check
        if getattr(self, 'kickback_in_progress', False):
            self.show_error("A manual kickback is currently in progress. Please wait.")
            return False

        self._last_fluidic_press_time = now
        return True

    def _on_manual_kickback_clicked(self):
        """Handle Manual Kickback button click."""
        if not self._check_fluidic_debounce(): return
        
        # Get current values from inputs
        try:
            volume = float(self.kickback_volume_input.text()) if hasattr(self, 'kickback_volume_input') else self.kickback_volume_ul
            rate = float(self.kickback_rate_input.text()) if hasattr(self, 'kickback_rate_input') else self.kickback_rate_ul_min
        except (ValueError, TypeError):
            self.show_error("Invalid kickback volume or rate. Please check settings.")
            return
        
        self._execute_kickback(volume, rate)
    
    @Slot()
    def _check_kickback_timing(self):
        """Check if it's time for a timed kickback."""
        if not self.kickback_timed_enabled:
            return
        
        # Only trigger kickback if fluidic state is RUNNING_SAMPLE or BEADS
        if self.current_fluidic_state not in [self.FLUIDIC_STATE_RUNNING_SAMPLE, self.FLUIDIC_STATE_BEADS]:
            return
        
        if self.kickback_in_progress:
            return
        
        # BUSY GUARD: Only trigger if no other routine or auto-clean is active/pending
        if (self.routine_thread and self.routine_thread.isRunning()) or getattr(self, 'auto_clean_pending', False):
            return
        
        # Get kickback time from control (dynamically updated)
        try:
            kickback_time = self.kickback_time_control.get_value() if hasattr(self, 'kickback_time_control') else self.kickback_time_seconds
            # Update instance variable when control changes
            self.kickback_time_seconds = kickback_time
        except (AttributeError, ValueError):
            kickback_time = self.kickback_time_seconds
        
        # Check if enough time has passed since last kickback
        if self.last_kickback_time is None:
            return
        
        elapsed = time.time() - self.last_kickback_time
        if elapsed >= kickback_time:
            # Get current values from inputs (dynamically updated)
            try:
                volume = float(self.kickback_volume_input.text()) if hasattr(self, 'kickback_volume_input') else self.kickback_volume_ul
                rate = float(self.kickback_rate_input.text()) if hasattr(self, 'kickback_rate_input') else self.kickback_rate_ul_min
                # Update instance variables when inputs change
                self.kickback_volume_ul = volume
                self.kickback_rate_ul_min = rate
            except (ValueError, TypeError):
                return  # Skip if invalid values
            
            # Execute kickback
            self.log_text.append(f"Automated kickback triggered: {elapsed:.1f}s elapsed (threshold: {kickback_time}s)")
            self._execute_kickback(volume, rate)
    
    def _update_kickback_time_indicator(self):
        """Update the time since last kickback indicator."""
        # Calculate elapsed time
        # Handle the case where last_kickback_time might be transiently invalid
        # but don't flicker to '--' immediately if it's just a state transition race
        elapsed = -1.0
        if self.last_kickback_time is not None:
            elapsed = time.time() - self.last_kickback_time
            
        # Emit signal (this updates pySMR UI)
        self.kickback_time_updated.emit(elapsed)

        # Update local indicator if present (legacy support)
        # Avoid updating if state is inconsistent or during rapid changes to prevent flickering
        if hasattr(self, 'kickback_time_indicator') and hasattr(self.kickback_time_indicator, 'value_label'):
            if elapsed < 0:
                self.kickback_time_indicator.value_label.setText("--")
            else:
                self.kickback_time_indicator.value_label.setText(f"{elapsed:.1f} s")
    
    def _execute_kickback(self, volume_ul, rate_ul_min):
        """Execute kickback sequence."""
        if self.kickback_in_progress:
            return
        
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected to the pump.")
            return
        
        self.kickback_in_progress = True
        self.log_text.append(f"CMD: Starting kickback sequence on Syringe 2 (volume: {volume_ul} µL, rate: {rate_ul_min} µL/min)")
        
        # Execute kickback
        
        # Store executor reference to prevent garbage collection
        executor = execute_kickback(self, syringe_index=1, volume_ul=volume_ul, rate_ul_min=rate_ul_min)
        executor.setParent(self)  # Set parent to keep it alive
        executor.kickback_complete.connect(self._on_kickback_complete)
        self.kickback_executor = executor  # Store reference
    
    def _on_kickback_complete(self, success):
        """Handle kickback completion."""
        self.kickback_in_progress = False
        self.last_kickback_time = time.time()
        
        # Clear executor reference
        self.kickback_executor = None
        
        if success:
            self.log_text.append("Kickback sequence completed successfully.")
            self.update_status_message("Kickback completed successfully.", 3000)
        else:
            self.log_text.append("Kickback sequence failed.")
            self.show_error("Kickback sequence failed.")
    
    def _on_kickback_volume_changed(self, text):
        """Handle kickback volume input change."""
        try:
            self.kickback_volume_ul = float(text) if text else 5.0
        except (ValueError, TypeError):
            pass  # Invalid input, keep previous value
    
    def _on_kickback_rate_changed(self, text):
        """Handle kickback rate input change."""
        try:
            self.kickback_rate_ul_min = float(text) if text else 100.0
        except (ValueError, TypeError):
            pass  # Invalid input, keep previous value
    
    def _on_kickback_time_changed(self):
        """Handle kickback time control change (for dynamic updates)."""
        try:
            if hasattr(self, 'kickback_time_control'):
                new_time = self.kickback_time_control.get_value()
                self.kickback_time_seconds = float(new_time)
        except (AttributeError, ValueError, TypeError):
            pass  # Invalid input, keep previous value
    
    def _update_routine_row_status(self, row, message, color):
        item = self.routine_table.item(row, 6)
        if item:
            item.setText(message)
            item.setBackground(color)

    def _highlight_step(self, old_step, new_step):
        # Clear old highlight
        if old_step != -1:
            for row in range(self.routine_table.rowCount()):
                step_item = self.routine_table.item(row, 0)
                # Check if step_item exists and has text before converting
                if step_item and step_item.text():
                    try:
                        current_step = int(step_item.text())
                    except ValueError:
                        continue

                    if current_step == old_step:
                        for col in range(self.routine_table.columnCount()):
                            # Handle QTableWidgetItem
                            item = self.routine_table.item(row, col)
                            if item:
                                item.setBackground(QColor("white"))
                            
                            # Handle Cell Widgets (ComboBoxes)
                            widget = self.routine_table.cellWidget(row, col)
                            if widget:
                                # Maintain the popup fix while resetting background
                                widget.setStyleSheet("QComboBox { combobox-popup: 0; background-color: white; }")

        # Apply new highlight
        if new_step != -1:
            for row in range(self.routine_table.rowCount()):
                step_item = self.routine_table.item(row, 0)
                if step_item and step_item.text():
                    try:
                        current_step = int(step_item.text())
                    except ValueError:
                        continue

                    if current_step == new_step:
                        for col in range(self.routine_table.columnCount()):
                            # Handle QTableWidgetItem
                            item = self.routine_table.item(row, col)
                            if item:
                                item.setBackground(QColor("#cfe2f3"))
                            
                            # Handle Cell Widgets (ComboBoxes)
                            widget = self.routine_table.cellWidget(row, col)
                            if widget:
                                # Maintain the popup fix while setting background
                                widget.setStyleSheet("QComboBox { combobox-popup: 0; background-color: #cfe2f3; }")
        
        # Update Final Clean progress dialog if active
        if hasattr(self, 'final_clean_dialog') and self.final_clean_dialog.isVisible():
            if hasattr(self, 'final_clean_sequence') and self.final_clean_sequence:
                if hasattr(self, 'total_final_clean_steps') and self.total_final_clean_steps > 0:
                    current_cumulative_step = getattr(self, 'steps_completed_before_current_routine', 0) + max(0, new_step)
                    progress_percent = (current_cumulative_step / self.total_final_clean_steps) * 100
                    progress_percent = min(100.0, max(0.0, progress_percent))
                    
                    # Get current routine name for the status label
                    routine_name = self.final_clean_sequence[0]
                    if new_step != -1:
                        self.final_clean_dialog.update_fluidic_status(progress_percent, f"Running {routine_name}: Step {new_step}")
                    else:
                        self.final_clean_dialog.update_fluidic_status(progress_percent, f"Running {routine_name}...")

    def _update_routine_ui_state(self, running):
        self.run_routine_btn.setEnabled(not running)
        self.pause_routine_btn.setEnabled(running)
        self.stop_routine_btn.setEnabled(running)
        self.routine_load_combo.setEnabled(not running)
        self.routine_table.setEnabled(not running)
        if not running:
            self.pause_routine_btn.setChecked(False)
            self.pause_routine_btn.setText("Pause")

    def refresh_com_ports(self):
        self.com_port_combo.clear()
        available_ports = []
        for port in serial.tools.list_ports.comports():
            self.com_port_combo.addItem(port.device)
            available_ports.append(port.device)
        
        # Set default COM port based on config or highest COM number
        default_port = None
        
        # Try to load from config if available
        if hasattr(self, 'config_file') and os.path.exists(self.config_file):
            try:
                with open(self.config_file, mode='r', encoding='utf-8') as file:
                    content = file.read()
                    config = self._parse_toml_config(content)
                    if 'settings' in config and 'com_port' in config['settings']:
                        preferred_port = config['settings']['com_port']
                        # Handle both string and dict formats (for robustness)
                        if isinstance(preferred_port, dict):
                            preferred_port = preferred_port.get('value', '')
                        elif not isinstance(preferred_port, str):
                            preferred_port = str(preferred_port)
                        if preferred_port and preferred_port.strip() and preferred_port in available_ports:
                            default_port = preferred_port
            except Exception:
                pass  # If config read fails, fall back to highest COM number
        
        # If no config preference or preferred port not available, use highest COM number
        if default_port is None and available_ports:
            com_ports = [p for p in available_ports if p.upper().startswith('COM')]
            if com_ports:
                # Extract numbers and find the highest
                def extract_com_number(port_name):
                    try:
                        # Extract number after 'COM' (e.g., 'COM3' -> 3)
                        match = re.search(r'COM(\d+)', port_name.upper())
                        return int(match.group(1)) if match else 0
                    except:
                        return 0
                
                com_ports.sort(key=extract_com_number, reverse=True)
                default_port = com_ports[0]
            else:
                # If no COM ports, just use first available
                default_port = available_ports[0]
        
        # Set the default selection
        if default_port:
            index = self.com_port_combo.findText(default_port)
            if index >= 0:
                self.com_port_combo.setCurrentIndex(index)
            
    def toggle_connection(self):
        if self.comm_thread and self.comm_thread.isRunning():
            self.comm_thread.stop()
            self.connect_button.setText("Connect")
            self.update_status_message("Disconnecting...")
        else:
            port = self.com_port_combo.currentText()
            if not port:
                self.show_error("No COM port selected.")
                return
            self.comm_thread = CommunicationThread(port, baudrate=9600)
            self.comm_thread.response_received.connect(self.handle_response)
            self.comm_thread.error_occurred.connect(self.handle_error)
            self.comm_thread.port_closed.connect(self.on_port_closed)
            self.comm_thread.pump_ready.connect(self._on_pump_ready)
            self.comm_thread.start()
            self.connect_button.setText("Disconnect")
            self.update_status_message(f"Connecting to {port}...")

    def update_ui_state(self, connected):
        for i in range(2):
            self.syringe_control_frames[i].setEnabled(connected)
        self.initialize_all_button.setEnabled(connected)
        self.com_port_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)
        self.run_routine_btn.setEnabled(connected)

    def on_port_closed(self):
        self.update_ui_state(connected=False)
        self.update_status_message("Disconnected")
        self.log_text.append("Connection closed.")

    @Slot(int, str)
    def send_command_to_pump(self, syringe_index, command_str):
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected to the pump.")
            return
        address = str(syringe_index + 1)
        
        # Parse for status update
        status_text = "Busy"
        cmd = command_str.upper()
        if 'Z' in cmd or 'Y' in cmd or 'W' in cmd:
            status_text = "Busy - Initializing"
        elif re.search(r'I(\d+)V.*P.*I6V4000A0', cmd):
            # Prime Reagent: I{port}V{velocity}P{steps}I6V4000A0 pattern (most specific - check first)
            match = re.search(r'I(\d+)V', cmd)
            if match:
                port = int(match.group(1))
                # Get port nickname
                nickname = ""
                try:
                    if (syringe_index in self.nickname_inputs and 
                        port in self.nickname_inputs[syringe_index]):
                        nickname = self.nickname_inputs[syringe_index][port].text().strip()
                except (AttributeError, KeyError):
                    pass
                
                if nickname:
                    status_text = f"Priming {nickname} (Port {port})"
                else:
                    status_text = f"Priming (Port {port})"
                
                # Update Port UI immediately when command is sent
                self._update_valve_button_state(syringe_index, port)
        elif 'I6V4000A0' in cmd:
            # Empty Syringe: moves to waste port 6 and empties
            status_text = "Emptying Syringe (Waste - Port 6)"
            # Update Port UI immediately when command is sent
            self._update_valve_button_state(syringe_index, 6)
        elif 'I' in cmd and ('P' in cmd or 'D' in cmd):
            # Draw/Dispense: I{port}V{velocity}{P|D}{steps} pattern
            match = re.search(r'I(\d+)', cmd)
            if match:
                port = int(match.group(1))
                # Determine action type
                action_type = "Drawing" if 'P' in cmd else "Dispensing"
                
                # Get port nickname
                nickname = ""
                try:
                    if (syringe_index in self.nickname_inputs and 
                        port in self.nickname_inputs[syringe_index]):
                        nickname = self.nickname_inputs[syringe_index][port].text().strip()
                except (AttributeError, KeyError):
                    pass
                
                if nickname:
                    status_text = f"{action_type} {nickname} (Port {port})"
                else:
                    status_text = f"{action_type} (Port {port})"
                
                # Update Port UI immediately when command is sent (handles routines)
                self._update_valve_button_state(syringe_index, port)
        elif 'I' in cmd:
            # Standalone Move Valve command
            match = re.search(r'I(\d+)', cmd)
            port = match.group(1) if match else "?"
            status_text = f"Busy - Moving to Port {port}"
            
            # Update Port UI immediately when command is sent (handles routines)
            if port.isdigit():
                self._update_valve_button_state(syringe_index, int(port))
                
        elif 'O' in cmd:
             match = re.search(r'O(\d+)', cmd)
             port = match.group(1) if match else "?"
             status_text = f"Busy - Moving to Output {port}"
        elif 'P' in cmd:
            status_text = "Busy - Drawing"
        elif 'D' in cmd:
            status_text = "Busy - Dispensing"
        elif 'A' in cmd:
            status_text = "Busy - Moving Absolute"
        elif 'M' in cmd:
            status_text = "Busy - Waiting"
        
        self.update_syringe_status_ui(syringe_index, "BUSY", status_text)

        # Start the busy poller if not already active to ensure we detect when it finishes
        if not self.busy_poll_timer.isActive():
            self.busy_poll_timer.start(500) # Poll every 500ms

        # The 'R' to execute is now added here, simplifying calls
        self.comm_thread.send_command(address, command_str + "R")
    
    def _poll_busy_status(self):
        """Called by timer to check status of pumps that are marked as busy."""
        # If the routine thread is running, let it handle polling for routine steps
        # to avoid flooding the pump with queries.
        if self.routine_thread and self.routine_thread.isRunning():
            return

        any_busy = False
        for i in range(2):
            label_text = self.status_labels[i]['status'].text()
            if label_text.startswith("Busy"):
                self.query_pump_status(i)
                any_busy = True
        
        if not any_busy:
            self.busy_poll_timer.stop()

    @Slot(int, str, float, float)
    def update_routine_animation(self, syringe_index, action_type, volume_ul, rate_ul_min):
        """Updates the syringe animation state based on routine commands."""
        # Calculate steps directly to avoid UI error popups from background threads
        steps = int((volume_ul / SYRINGE_VOLUME_UL) * MAX_STEPS)
        
        # Calculate duration in ms
        duration_ms = (volume_ul / rate_ul_min) * 60 * 1000 if rate_ul_min > 0 else 0
        
        if action_type == "Draw":
            new_steps = self.current_steps[syringe_index] + steps
            new_steps = min(MAX_STEPS, new_steps) # Clamp to max
        elif action_type == "Dispense":
            new_steps = self.current_steps[syringe_index] - steps
            new_steps = max(0, new_steps) # Clamp to min
        else:
            return

        self.current_steps[syringe_index] = new_steps
        self.syringe_visualizers[syringe_index].animate_to(new_steps, duration_ms)

    @Slot(int)
    @Slot(int)
    def query_pump_status(self, syringe_index):
        """Query the status of a pump to force a response."""
        if not (self.comm_thread and self.comm_thread.isRunning()):
            return
        address = str(syringe_index + 1)
        # Send status query command - "Q" is the query command, "R" is not required for query commands
        self.comm_thread.send_command(address, "Q")
        
    def initialize_all_pumps(self):
        self.log_text.append("Starting initialization sequence...")
        self.update_status_message("Initializing Syringe 1...")
        self.initialize_state = 'INIT_S1_SENT'
        # Send N1 to enable high-res mode, then v5 to set minimum start velocity, then Z to initialize S1 first
        # S2 will be initialized automatically by the state machine once S1 completes
        # Robust initialization using H-factors (enables 6-port valve, high res, and avoids v limits)
        self.send_command_to_pump(0, "Zh30001h20000h11001h20001h21006N1")
        # Polling is now handled automatically by send_command_to_pump setting Busy status

    def pause_pump(self, syringe_index):
        self.log_text.append(f"CMD: Pausing Syringe {syringe_index + 1}...")
        self.send_command_to_pump(syringe_index, "T")
        self.syringe_visualizers[syringe_index].animation.stop()
        self.update_status_message(f"Syringe {syringe_index + 1} paused. Position may be inaccurate.", 5000)

    @Slot(int)
    def send_interrupt_command(self, syringe_index):
        """Send T command to interrupt/stop the syringe pump immediately."""
        if not (self.comm_thread and self.comm_thread.isRunning()):
            return
        address = str(syringe_index + 1)
        # Send T command directly without R (interrupt is immediate, doesn't need execute)
        self.comm_thread.send_command(address, "T")
        self.log_text.append(f"CMD: Interrupt sent to Syringe {syringe_index + 1}")

    @Slot(int)
    def home_pump(self, syringe_index):
        """Recalibrate: Move to waste, empty syringe, and home plunger in one command."""
        self.log_text.append(f"CMD: Starting Recalibrate sequence for Syringe {syringe_index + 1}...")
        self.update_status_message(f"S{syringe_index + 1}: Recalibrating (waste → empty → home)...")
        self._update_valve_button_state(syringe_index, 6)
        
        # Get waste dispense rate for emptying
        rate_str = self.speed_inputs[syringe_index]['Waste']['Dispense'].text()
        velocity = self.rate_to_velocity(rate_str)
        if velocity is None:
            velocity = 4000  # Default velocity if rate is invalid
        
        # Single complex command: move to waste, empty syringe, home plunger
        # Format: I6V{velocity}A0Z
        command = f"I6V{velocity}A0Z"
        self.send_command_to_pump(syringe_index, command)
        
        # Update visualizer - empty then reset
        try:
            current_vol_ul = (self.current_steps[syringe_index] / MAX_STEPS) * SYRINGE_VOLUME_UL
            rate_val = float(rate_str) if rate_str else 2000.0
            if rate_val > 0:
                duration_ms = (current_vol_ul / rate_val) * 60 * 1000
                self.syringe_visualizers[syringe_index].animate_to(0, duration_ms)
        except (ValueError, TypeError):
            self.syringe_visualizers[syringe_index].set_value_immediate(0)
        
        # Final state will be empty and homed
        self.current_steps[syringe_index] = 0
    
    @Slot(int)
    def empty_syringe(self, syringe_index):
        """Empty syringe: Move to waste and empty syringe in one command."""
        self.log_text.append(f"CMD: Starting Empty Syringe sequence for Syringe {syringe_index + 1}...")
        self.update_status_message(f"S{syringe_index + 1}: Emptying syringe (waste → empty)...")
        self._update_valve_button_state(syringe_index, 6)
        
        # Single complex command: move to waste and empty syringe
        # Format: I6V4000A0
        command = "I6V4000A0"
        self.send_command_to_pump(syringe_index, command)
        
        # Update visualizer
        try:
            current_vol_ul = (self.current_steps[syringe_index] / MAX_STEPS) * SYRINGE_VOLUME_UL
            rate_val = 2000.0  # Velocity 4000 = 2000 uL/min
            duration_ms = (current_vol_ul / rate_val) * 60 * 1000
            self.syringe_visualizers[syringe_index].animate_to(0, duration_ms)
        except (ValueError, TypeError):
            self.syringe_visualizers[syringe_index].set_value_immediate(0)
        
        # Final state will be empty
        self.current_steps[syringe_index] = 0
    
    @Slot(int, int)
    def _set_prime_port(self, syringe_index, port):
        """Set the port for prime reagent sequence (used by routines)."""
        self.prime_sequence_port[syringe_index] = port
    
    @Slot(int)
    def prime_reagent(self, syringe_index):
        """Prime reagent sequence: move to reagent port, draw 50uL, move to waste, empty."""
        # Use stored port from prime_sequence_port if available (for routines), otherwise use selected_valve
        port = self.prime_sequence_port[syringe_index] if self.prime_sequence_port[syringe_index] is not None else self.selected_valve[syringe_index]
        if port is None:
            self.show_error(f"Please select a valve port for Syringe {syringe_index + 1} first.")
            return
        
        # Check if port is a reagent port with valid draw rate
        port_type = self.port_type_combos[syringe_index][port].currentText()
        if port_type != 'Reagent':
            self.show_error(f"Cannot prime: Port {port} is not a Reagent port.")
            return
        
        draw_rate_str = self.speed_inputs[syringe_index]['Reagent']['Draw'].text()
        try:
            draw_rate = float(draw_rate_str) if draw_rate_str else 0.0
            if draw_rate <= 0:
                self.show_error(f"Cannot prime: Reagent draw rate is not set or is 0.")
                return
        except (ValueError, TypeError):
            self.show_error(f"Cannot prime: Invalid reagent draw rate.")
            return
        
        # Check if this is manual control (port not pre-set) vs routine (port pre-set)
        is_manual_control = self.prime_sequence_port[syringe_index] is None
        
        # Store port and manual flag for completion tracking
        self.prime_sequence_port[syringe_index] = port
        self.prime_is_manual[syringe_index] = is_manual_control
        
        # Calculate velocity and steps for 50uL draw
        velocity = self.rate_to_velocity(draw_rate_str)
        if velocity is None:
            return
        steps_50ul = self.volume_to_steps("50.0")
        if steps_50ul is None:
            return
        
        self.log_text.append(f"CMD: Starting Prime Reagent sequence for Syringe {syringe_index + 1} at Port {port}...")
        if is_manual_control:
            self.update_status_message(f"S{syringe_index + 1}: Priming reagent (port {port} → waste → empty → port {port})...")
        else:
            self.update_status_message(f"S{syringe_index + 1}: Priming reagent (port {port} → waste → empty)...")
        self._update_valve_button_state(syringe_index, port)
        
        # Single complex command: move to reagent port, draw 50uL, move to waste, empty syringe
        # For manual control, also return valve to reagent port at the end
        if is_manual_control:
            # Format: I{port}V{velocity}P{steps}I6V4000A0I{port}
            command = f"I{port}V{velocity}P{steps_50ul}I6V4000A0I{port}"
        else:
            # Format: I{port}V{velocity}P{steps}I6V4000A0
            command = f"I{port}V{velocity}P{steps_50ul}I6V4000A0"
        self.send_command_to_pump(syringe_index, command)
        
        # Update syringe visualizer - will draw then empty
        # First draw 50uL
        new_steps = self.current_steps[syringe_index] + steps_50ul
        draw_duration_ms = (50.0 / draw_rate) * 60 * 1000
        # Then empty (calculate empty duration based on new volume)
        empty_rate = 2000.0  # Velocity 4000 = 2000 uL/min
        empty_duration_ms = (50.0 / empty_rate) * 60 * 1000
        total_duration_ms = draw_duration_ms + empty_duration_ms
        
        # Animate draw first, then empty
        self.syringe_visualizers[syringe_index].animate_to(new_steps, draw_duration_ms)
        # Schedule empty animation after draw completes
        QTimer.singleShot(int(draw_duration_ms), 
                         lambda: self.syringe_visualizers[syringe_index].animate_to(0, empty_duration_ms))
        
        # Update valve button to waste after draw completes
        QTimer.singleShot(int(draw_duration_ms),
                         lambda: self._update_valve_button_state(syringe_index, 6))
        
        # For manual control, schedule valve return to reagent port after emptying completes
        if is_manual_control:
            QTimer.singleShot(int(total_duration_ms),
                             lambda p=port: self._update_valve_button_state(syringe_index, p))
        
        # Final state will be empty
        self.current_steps[syringe_index] = 0
    
    @Slot(int, int)
    def _update_valve_button_state(self, syringe_index, position):
        self.selected_valve[syringe_index] = position
        for port, button in self.valve_buttons[syringe_index].items():
            button.setChecked(port == position)
        
        # Update Port status label
        self.update_syringe_port_ui(syringe_index, position)
        
        # Update prime button state
        self._update_prime_button_state(syringe_index)
    
    def _update_prime_system_button(self):
        """Update Prime System button appearance based on completion state."""
        if not hasattr(self, 'prime_system_btn') or self.prime_system_btn is None:
            return
        
        if self.prime_system_completed:
            # Gray when already completed
            self.prime_system_btn.setStyleSheet("""
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
            # Default style when not completed
            self.prime_system_btn.setStyleSheet("""
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
    
    def _update_prime_button_state(self, syringe_index):
        """Update Prime Reagent button enabled/disabled state based on current port."""
        if self.prime_buttons[syringe_index] is None:
            return
        
        port = self.selected_valve[syringe_index]
        if port is None:
            self.prime_buttons[syringe_index].setEnabled(False)
            return
        
        # Check if port is a reagent port
        port_type = self.port_type_combos[syringe_index][port].currentText()
        if port_type != 'Reagent':
            self.prime_buttons[syringe_index].setEnabled(False)
            return
        
        # Check if reagent draw rate is valid (> 0)
        draw_rate_str = self.speed_inputs[syringe_index]['Reagent']['Draw'].text()
        try:
            draw_rate = float(draw_rate_str) if draw_rate_str else 0.0
            if draw_rate > 0:
                self.prime_buttons[syringe_index].setEnabled(True)
            else:
                self.prime_buttons[syringe_index].setEnabled(False)
        except (ValueError, TypeError):
            self.prime_buttons[syringe_index].setEnabled(False)

    def move_valve(self, syringe_index, position):
        self.log_text.append(f"CMD: S{syringe_index + 1} moving valve to pos {position}...")
        self._update_valve_button_state(syringe_index, position)
        self.send_command_to_pump(syringe_index, f"I{position}")
    
    def rate_to_velocity(self, rate_ul_min_str):
        try:
            rate_ul_min = float(rate_ul_min_str)
            # Mathematical conversion:
            # 1 Stroke = 50 uL = 6,000 motor steps.
            # 1 uL = 120 motor steps.
            # Velocity (V) is in motor steps per second.
            # V = (Rate_uL/min * 120 steps/uL) / 60 sec/min = Rate * 2.
            # Multiplier of 2 is correct for Hamilton PSD/4 (6,000 motor steps per stroke)
            # regardless of current micro-step resolution (N0/N1).
            velocity = int(rate_ul_min * 2)
            
            # Ensure velocity is within hardware limits [2, 5800]
            # Minimum clamp at 2 allows for the 1.0 uL/min rate (V=2).
            # We avoid conflicts with start velocity (v) by removing 'v' from init.
            return max(2, min(velocity, MAX_VELOCITY))
        except (ValueError, TypeError):
            self.show_error(f"Invalid rate '{rate_ul_min_str}'. Please use a number.")
            return None

    def volume_to_steps(self, volume_ul_str):
        try:
            volume = float(volume_ul_str)
            if not (0 < volume <= SYRINGE_VOLUME_UL):
                self.show_error(f"Volume must be between 0 and {SYRINGE_VOLUME_UL} µL.")
                return None
            return int((volume / SYRINGE_VOLUME_UL) * MAX_STEPS)
        except (ValueError, TypeError):
            self.show_error(f"Invalid volume '{volume_ul_str}'. Please use a number.")
            return None

    def _get_syringe_context(self, syringe_index):
        port = self.selected_valve[syringe_index]
        if port is None:
            self.show_error(f"Please select a valve port for Syringe {syringe_index+1} first.")
            return None, None, None, None
        p_type = self.port_type_combos[syringe_index][port].currentText()
        volume_str = self.volume_inputs[syringe_index].text()
        steps = self.volume_to_steps(volume_str)
        return port, p_type, volume_str, steps

    def draw_volume(self, syringe_index):
        port, p_type, vol_str, steps = self._get_syringe_context(syringe_index)
        if steps is None: return
        if p_type == 'Waste':
            self.show_error(f"Action Forbidden: Cannot draw from Port {port} ('{p_type}').")
            return
        
        if self.current_steps[syringe_index] + steps > MAX_STEPS:
            # Truncate to remaining capacity
            steps = MAX_STEPS - self.current_steps[syringe_index]
            if steps <= 0:
                self.show_error(f"Cannot draw: Syringe {syringe_index + 1} is already at maximum capacity.")
                return
            vol_str = f"{(steps / MAX_STEPS) * SYRINGE_VOLUME_UL:.2f}"
            self.log_text.append(f"WARNING: Draw volume truncated to {vol_str} µL ({steps} steps) to respect syringe limit.")
        
        rate_str = self.speed_inputs[syringe_index][p_type]['Draw'].text()
        
        # Validate 0 rate for all port types
        try:
            rate_val = float(rate_str) if rate_str else 0.0
            if rate_val == 0.0:
                self.show_error('Cannot draw or dispense when rate is set to 0uL/min.')
                return
        except (ValueError, TypeError):
            pass  # Will be caught by rate_to_velocity
        
        velocity = self.rate_to_velocity(rate_str)
        if velocity is None: return
        
        self.log_text.append(f"CMD: S{syringe_index+1} drawing {vol_str} µL from port {port} at {rate_str} uL/min...")
        self.send_command_to_pump(syringe_index, f"V{velocity}P{steps}")
        
        duration_ms = (float(vol_str) / float(rate_str)) * 60 * 1000 if float(rate_str) > 0 else 0
        new_steps = self.current_steps[syringe_index] + steps
        self.syringe_visualizers[syringe_index].animate_to(new_steps, duration_ms)
        self.current_steps[syringe_index] = new_steps

    def dispense_volume(self, syringe_index):
        port, p_type, vol_str, steps = self._get_syringe_context(syringe_index)
        if steps is None: return
        if p_type == 'Reagent':
            self.show_error(f"Action Forbidden: Cannot dispense to Port {port} ('{p_type}').")
            return
        
        if self.current_steps[syringe_index] - steps < 0:
            # Truncate to available volume
            steps = self.current_steps[syringe_index]
            if steps <= 0:
                self.show_error(f"Cannot dispense: Syringe {syringe_index + 1} is already empty.")
                return
            vol_str = f"{(steps / MAX_STEPS) * SYRINGE_VOLUME_UL:.2f}"
            self.log_text.append(f"WARNING: Dispense volume truncated to {vol_str} µL ({steps} steps) to respect syringe limit.")

        rate_str = self.speed_inputs[syringe_index][p_type]['Dispense'].text()
        
        # Validate 0 rate for all port types
        try:
            rate_val = float(rate_str) if rate_str else 0.0
            if rate_val == 0.0:
                self.show_error('Cannot draw or dispense when rate is set to 0uL/min.')
                return
        except (ValueError, TypeError):
            pass  # Will be caught by rate_to_velocity
        
        velocity = self.rate_to_velocity(rate_str)
        if velocity is None: return

        self.log_text.append(f"CMD: S{syringe_index+1} dispensing {vol_str} µL to port {port} at {rate_str} uL/min...")
        self.send_command_to_pump(syringe_index, f"V{velocity}D{steps}")

        duration_ms = (float(vol_str) / float(rate_str)) * 60 * 1000 if float(rate_str) > 0 else 0
        new_steps = self.current_steps[syringe_index] - steps
        self.syringe_visualizers[syringe_index].animate_to(new_steps, duration_ms)
        self.current_steps[syringe_index] = new_steps

    def _on_pump_ready(self, pump_address):
        """Slot to handle when a pump reports it's ready."""
        # Now handled by RoutineThread directly via status_update signal
        pass

    def handle_response(self, response):
        self.log_text.append(response)
        if "Successfully opened port" in response:
            self.update_ui_state(connected=True)
            self.update_status_message(f"Connected to {self.comm_thread.port}")
            return
        
        if "Status: Ready" in response:
            # Parse which pump is ready to update UI status
            match = re.search(r'PUMP (\d+)', response)
            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx <= 1:
                    self.update_syringe_status_ui(idx, "READY", "Ready")

            # --- Global Initialization State Machine ---
            if self.initialize_state == 'INIT_S1_SENT' and "PUMP 1" in response:
                self.log_text.append("Syringe 1 initialized. Initializing Syringe 2...")
                self.update_status_message("Initializing Syringe 2...")
                self.initialize_state = 'INIT_S2_SENT'
                # Robust initialization for second syringe
                self.send_command_to_pump(1, "Zh30001h20000h11001h20001h21006N1")
                # Timer continues running to poll Pump 2
            elif self.initialize_state == 'INIT_S2_SENT' and "PUMP 2" in response:
                self.log_text.append("Syringe 2 initialized. Sequence complete.")
                self.update_status_message("All pumps initialized successfully.", 5000)
                self.initialize_state = 'IDLE'
                
                # Update fluidic state to IDLE
                self.current_fluidic_state = self.FLUIDIC_STATE_IDLE
                self.fluidic_state_changed.emit(self.FLUIDIC_STATE_IDLE, "Idle")

                # Polling stops automatically when status becomes Ready
                for i in range(2):
                    self.current_steps[i] = 0
                    self.syringe_visualizers[i].set_value_immediate(0)
            
            # --- Per-Syringe Command Completion ---
            for s_idx in range(2):
                if f"INTERPRETED (PUMP {s_idx + 1})" in response:
                    # Check for Recalibrate completion (no state tracking needed - single command)
                    # The command I6V{velocity}A0Z completes all steps, so when pump is ready,
                    # we just log completion
                    # Note: We can't easily detect which command completed, so we'll handle
                    # completion in the status update when pump becomes ready
                    
                    # --- Prime Reagent Completion ---
                    if self.prime_sequence_port[s_idx] is not None:
                        # Single complex command completes all steps at once
                        port = self.prime_sequence_port[s_idx]
                        is_manual = self.prime_is_manual[s_idx]
                        self.log_text.append(f"Syringe {s_idx + 1} Prime Reagent complete (Port {port}).")
                        self.update_status_message(f"Syringe {s_idx + 1} Prime Reagent complete.", 5000)
                        self.prime_sequence_port[s_idx] = None
                        self.prime_is_manual[s_idx] = False
                        self.current_steps[s_idx] = 0
                        self.syringe_visualizers[s_idx].set_value_immediate(0)
                        # For manual control, valve returns to reagent port; for routines, stays at waste
                        final_valve_pos = port if is_manual else 6
                        self._update_valve_button_state(s_idx, final_valve_pos)

    def handle_error(self, error_message, is_critical):
        self.log_text.append(f"ERROR: {error_message}")
        self.update_status_message(f"Error: {error_message}", 5000)
        # Reset any sequences on critical error
        if is_critical:
            self.initialize_state = 'IDLE'
            self.busy_poll_timer.stop() # Stop polling
            for i in range(2):
                self.prime_sequence_port[i] = None
                self.prime_is_manual[i] = False
            self.show_error(error_message)

    def show_error(self, message):
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setText(message)
        msg_box.setWindowTitle("Application Error")
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.exec()
    
    def show_busy_abort_dialog(self, message):
        """Specialized error dialog for busy states with an immediate 'Abort' option."""
        msg_box = QMessageBox(self)
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setWindowTitle("System Busy")
        msg_box.setText(message)
        
        # Add buttons
        # Note: We don't use StandardButtons for Abort to ensure custom styling
        ok_button = msg_box.addButton(QMessageBox.StandardButton.Ok)
        abort_button = msg_box.addButton("Abort current routine", QMessageBox.ButtonRole.DestructiveRole)
        
        # Style the abort button to be red and bold
        abort_button.setObjectName("DangerButton")
        # Ensure 'error' style from create_button is applied if possible, 
        # or use forced stylesheet for immediate visual impact
        abort_button.setStyleSheet("""
            QPushButton#DangerButton {
                background-color: #dc3545;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 5px;
            }
            QPushButton#DangerButton:hover {
                background-color: #c82333;
            }
        """)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == abort_button:
            self.log_text.append("User requested immediate abort of current routine from busy dialog.")
            self._stop_routine()
    
    # --- Experimental Controls Handlers ---

    def _on_prime_system(self):
        """Handler for Prime system button."""
        if not self._check_fluidic_debounce(): return
        
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected. Cannot run prime system sequence.")
            return
        
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Prime System")
        msg_box.setText("Prime System Sequence")
        msg_box.setInformativeText("This will prime the SMR for usage - Please check and confirm there is sufficient Water and Bleach, and load a fresh Media vial onto the system before hitting continue")
        continue_btn = msg_box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        if msg_box.clickedButton() == continue_btn:
            # Initialize sequence: Prime Reagents -> Complete Clean -> Media Purge
            self.prime_system_sequence = ['Prime Reagents', 'Complete Clean', 'Media Purge']
            self._run_next_prime_routine()
    
    def run_prime_system_programmatic(self, skip_confirmation=False, sequence_type="full_system_prime"):
        """Run Prime System sequence programmatically.
        
        Args:
            skip_confirmation: If True, skip the confirmation dialog and start immediately.
            sequence_type: Either "full_system_prime" (Prime Reagents -> Complete Clean -> Media Purge)
                          or "prime_reagents_only" (Prime Reagents only).
        
        Returns:
            True if sequence started successfully, False otherwise.
        """
        if not self._check_fluidic_debounce(): return False # Added debounce check
        if not (self.comm_thread and self.comm_thread.isRunning()):
            return False
        
        if not skip_confirmation:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Prime System")
            msg_box.setText("Prime System Sequence")
            msg_box.setInformativeText("This will prime the SMR for usage - Please check and confirm there is sufficient Water and Bleach, and load a fresh Media vial onto the system before hitting continue")
            continue_btn = msg_box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
            cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg_box.exec()
            
            if msg_box.clickedButton() != continue_btn:
                return False
        
        # Initialize sequence based on sequence_type
        if sequence_type == "prime_reagents_only":
            self.prime_system_sequence = ['Prime Reagents']
        else:
            # Default: full_system_prime - Prime Reagents -> Complete Clean -> Media Purge
            self.prime_system_sequence = ['Prime Reagents', 'Complete Clean', 'Media Purge']
        self._run_next_prime_routine()
        return True
    
    def _run_next_prime_routine(self):
        """Run the next routine in the prime system sequence."""
        if not self.prime_system_sequence or len(self.prime_system_sequence) == 0:
            # Sequence complete
            self.prime_system_sequence = None
            self.prime_system_completed = True
            self._update_prime_system_button()
            self.update_status_message("Prime system sequence completed successfully.", 5000)
            return
        
        routine_name = self.prime_system_sequence[0]
        routine_path = self.find_routine_file(routine_name)
        
        if not routine_path:
            self.show_error(f"Routine '{routine_name}' not found in {ROUTINE_SUBDIR}.\nPlease create and save this routine first.")
            self.prime_system_sequence = None
            return
        
        if not self.load_routine_from_file(routine_path):
            self.show_error(f"Failed to load routine '{routine_name}'.")
            self.prime_system_sequence = None
            return
        
        # Update status
        self.system_status_text.setText(f"Running {routine_name}...")
        self.update_status_message(f"Running {routine_name} routine...", 3000)
        
        # Run the routine (this creates routine_thread and connects to _on_routine_finished)
        self._run_routine()
        
        # Disconnect default handler and connect custom handler for prime sequence
        if self.routine_thread:
            try:
                self.routine_thread.routine_finished.disconnect(self._on_routine_finished)
            except:
                pass
            self.routine_thread.routine_finished.connect(self._on_prime_routine_finished)
    
    def _on_clean_between_runs(self):
        """Handler for Clean now button. Loads and executes the routine matching the selected clean protocol."""
        if not self._check_fluidic_debounce():
            return
        clean_protocol = self.clean_protocol_combo.currentText()
        if not clean_protocol:
            self.show_error("Please select a clean protocol.")
            return
        self._execute_clean_protocol(clean_protocol, ack_sample_destroyed=False, use_gui_warning=True)

    def _execute_clean_protocol(
        self,
        clean_protocol: str,
        *,
        ack_sample_destroyed: bool = False,
        use_gui_warning: bool = True,
    ) -> dict:
        """Run a clean routine (Complete / Rapid / Media Purge). Returns result dict for remote API."""
        if not self._check_fluidic_debounce():
            return {"ok": False, "error": "debounce"}
        if not (self.comm_thread and self.comm_thread.isRunning()):
            msg = "Not connected. Cannot run clean routine."
            if use_gui_warning:
                self.show_error(msg)
            return {"ok": False, "error": "not_connected", "message": msg}
        if not clean_protocol:
            return {"ok": False, "error": "missing_protocol"}

        self.kickback_timer.stop()
        self.kickback_timed_enabled = False
        if hasattr(self, 'kickback_timed_checkbox'):
            self.kickback_timed_checkbox.setChecked(False)

        self.current_fluidic_state = self.FLUIDIC_STATE_CLEANING
        self.fluidic_state_changed.emit(self.FLUIDIC_STATE_CLEANING, f"Clean - {clean_protocol}")

        if hasattr(self.smr_widget, 'disable_peak_detection'):
            self.smr_widget.disable_peak_detection()

        condition_running = (
            self.is_aspirating
            or ((self.routine_thread and self.routine_thread.isRunning()) and not self.is_clean_routine)
        )

        if condition_running:
            if not ack_sample_destroyed:
                if use_gui_warning:
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("Warning: Condition Running")
                    msg_box.setText(
                        "Executing clean will backflush bleach into the sample vial - "
                        "replace it now if you want to preserve the sample"
                    )
                    msg_box.setIcon(QMessageBox.Icon.Warning)
                    cancel_button = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                    ok_button = msg_box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
                    msg_box.setDefaultButton(cancel_button)
                    msg_box.exec()
                    if msg_box.clickedButton() != ok_button:
                        return {"ok": False, "error": "cancelled", "message": "condition_running"}
                else:
                    return {
                        "ok": False,
                        "error": "condition_running_requires_ack",
                        "message": "Set ack_sample_destroyed=true to proceed.",
                    }

        routine_path = self.find_routine_file(clean_protocol)
        if not routine_path:
            msg = f"Routine '{clean_protocol}' not found."
            if use_gui_warning:
                self.show_error(msg)
            return {"ok": False, "error": "routine_not_found", "message": msg}

        routine_filename = os.path.basename(routine_path)
        self.log_text.append(f"Loading clean routine: {routine_filename}")
        if not self.load_routine_from_file(routine_path):
            msg = f"Failed to load routine '{routine_filename}'."
            if use_gui_warning:
                self.show_error(msg)
            return {"ok": False, "error": "load_failed", "message": msg}

        self.is_clean_routine = True
        if self.smr_widget and hasattr(self.smr_widget, '_update_current_condition'):
            self.smr_widget._update_current_condition("None - Cleaning")
        if self.smr_widget and self.smr_widget.is_saving:
            self.smr_widget._append_experiment_flag(f"Clean: {clean_protocol}")
        self.update_status_message(f"Running {clean_protocol} routine...", 3000)
        self._run_routine()
        return {"ok": True, "message": f"started {clean_protocol}", "protocol": clean_protocol}

    def run_clean_programmatic(self, protocol_key: str, ack_sample_destroyed: bool = False) -> dict:
        """Remote API: protocol_key is complete_clean | rapid_clean | media_purge."""
        from helper_functions.paella_remote.constants import CLEAN_PROTOCOL_MAP
        clean_protocol = CLEAN_PROTOCOL_MAP.get(protocol_key)
        if not clean_protocol:
            return {"ok": False, "error": "invalid_protocol", "protocol": protocol_key}
        return self._execute_clean_protocol(
            clean_protocol, ack_sample_destroyed=ack_sample_destroyed, use_gui_warning=False
        )
    
    def _on_final_clean(self):
        """Handler for Final clean button."""
        if not self._check_fluidic_debounce(): return # Added debounce check
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected. Cannot run final clean sequence.")
            return
        
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Final Clean")
        msg_box.setText("Final Clean Sequence")
        msg_box.setInformativeText("This will stop all saving and clean the SMR for shutdown. The cleaning process will backflush bleach into the sample vial, destroying any remaining sample.")
        continue_btn = msg_box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        if msg_box.clickedButton() == continue_btn:
            self._start_final_clean_sequence(use_progress_dialog=True)

    def run_final_clean_programmatic(self, skip_confirmation: bool = True) -> dict:
        """Remote API: start Final Clean (destructive). Caller must confirm on dashboard."""
        if not self._check_fluidic_debounce():
            return {"ok": False, "error": "debounce"}
        if not (self.comm_thread and self.comm_thread.isRunning()):
            return {"ok": False, "error": "not_connected"}
        if not skip_confirmation:
            return {"ok": False, "error": "confirmation_required"}
        self._start_final_clean_sequence(use_progress_dialog=True)
        return {"ok": True, "message": "final_clean_started"}

    def _start_final_clean_sequence(self, use_progress_dialog: bool = True):
        # Show progress dialog
        self.final_clean_dialog = FinalCleanProgressDialog(self)
        self.final_clean_dialog.show()
        
        # Connect frequency analysis progress
        if self.smr_widget:
            try:
                self.smr_widget.posthoc_progress.connect(self.final_clean_dialog.update_analysis_status)
            except Exception as e:
                print(f"Error connecting post-hoc progress: {e}")
        
        # Force UI update so the dialog isn't a gray box during hardware teardown
        QApplication.processEvents()
        
        # Define how to start the analysis once saving is actually stopped
        def start_analysis_logic():
            if self.smr_widget and hasattr(self.smr_widget, '_last_uncalibrated_csv') and self.smr_widget._last_uncalibrated_csv:
                self.smr_widget._run_posthoc_analysis(self.smr_widget._last_uncalibrated_csv, use_drift_correction=False)
            else:
                # If no data was saved, immediately report 100%
                self.final_clean_dialog.update_analysis_status(100, "No frequency data to analyze.")
                
        # Stop saving in pySMR if applicable
        if self.smr_widget:
            self.smr_widget.is_final_clean = True
            
            if self.smr_widget.is_saving:
                # Connect to the signal - using a one-off connection
                # We use a lambda or named function and disconnect it immediately
                # to avoid multiple triggers if the user clicks again.
                def on_finished():
                    try:
                        self.smr_widget.data_saver_finished.disconnect(on_finished)
                    except: pass
                    start_analysis_logic()
                    
                self.smr_widget.data_saver_finished.connect(on_finished)
                self._stop_smr_saving()
            else:
                # Not saving, start analysis immediately
                start_analysis_logic()
        else:
            self._stop_smr_saving()
            start_analysis_logic()
        
        # Stop kickback timer when final clean starts
        self.kickback_timer.stop()
        self.kickback_timed_enabled = False
        if hasattr(self, 'kickback_timed_checkbox'):
            self.kickback_timed_checkbox.setChecked(False)
        
        # Disable run and send to FPGA
        self._disable_smr_run()
        
        # Disable peak detection
        if hasattr(self.smr_widget, 'disable_peak_detection'):
            self.smr_widget.disable_peak_detection()
            
        # CRITICAL FIX: Switch cameras to Full Image (free-run) mode and disable image saving.
        # To prevent event loop overload and GIL contention while post-hoc analysis is active,
        # the free-run frame rate is throttled to 5.0 FPS inside pyImage.py when is_final_clean is True.
        if hasattr(self, 'image_widget') and self.image_widget:
            self.log_text.append("Configuring cameras to 5 FPS Full Image mode for Final Clean...")
            if hasattr(self.image_widget, 'toggle_roi'):
                self.image_widget.toggle_roi(False)
            if hasattr(self.image_widget, 'roi_button'):
                self.image_widget.roi_button.blockSignals(True)
                self.image_widget.roi_button.setChecked(False)
                self.image_widget.roi_button.blockSignals(False)
            
            # Explicitly disable image saving to prevent dangling buffers
            if hasattr(self.image_widget, 'toggle_image_saving'):
                self.image_widget.toggle_image_saving(False)
        
        # Initialize sequence: Prime Reagents -> Complete Clean -> Shutdown Media Backflush
        self.final_clean_sequence = ['Prime Reagents', 'Complete Clean', 'Shutdown Media Backflush']
        
        # Calculate total steps for cumulative progress
        self.total_final_clean_steps = self._get_total_steps_in_sequence(self.final_clean_sequence)
        self.steps_completed_before_current_routine = 0
        
        self._run_next_final_clean_routine()

    def set_kickback_volume_programmatic(self, volume_ul: float) -> dict:
        """Remote API: set kickback volume in µL."""
        try:
            volume_ul = float(volume_ul)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_volume"}
        if volume_ul <= 0:
            return {"ok": False, "error": "invalid_volume"}
        self.kickback_volume_ul = volume_ul
        if hasattr(self, 'kickback_volume_input'):
            self.kickback_volume_input.setText(str(volume_ul))
        return {"ok": True, "volume_ul": volume_ul}

    def run_kickback_programmatic(
        self, volume_ul=None, rate_ul_min=None
    ) -> dict:
        """Remote API: run manual kickback on syringe 2."""
        if not self._check_fluidic_debounce():
            return {"ok": False, "error": "debounce"}
        if self.kickback_in_progress:
            return {"ok": False, "error": "kickback_in_progress"}
        try:
            vol = float(volume_ul) if volume_ul is not None else float(
                self.kickback_volume_input.text() if hasattr(self, 'kickback_volume_input')
                else self.kickback_volume_ul
            )
            rate = float(rate_ul_min) if rate_ul_min is not None else float(
                self.kickback_rate_input.text() if hasattr(self, 'kickback_rate_input')
                else self.kickback_rate_ul_min
            )
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_volume_or_rate"}
        self._execute_kickback(vol, rate)
        return {"ok": True, "message": "kickback_started", "volume_ul": vol, "rate_ul_min": rate}

    def pumps_connect_programmatic(self, port=None) -> dict:
        """Remote API: open serial connection to syringe pumps."""
        if self.comm_thread and self.comm_thread.isRunning():
            return {
                "ok": True,
                "message": "already_connected",
                "port": getattr(self.comm_thread, "port", None),
            }
        if port:
            idx = self.com_port_combo.findText(str(port))
            if idx >= 0:
                self.com_port_combo.setCurrentIndex(idx)
        else:
            settings = get_syringe_pump_settings(load_system_config())
            cfg_port = settings.get("com_port")
            if cfg_port:
                idx = self.com_port_combo.findText(cfg_port)
                if idx >= 0:
                    self.com_port_combo.setCurrentIndex(idx)
        selected = self.com_port_combo.currentText()
        if not selected:
            return {"ok": False, "error": "no_com_port"}
        self.toggle_connection()
        return {"ok": True, "message": "connecting", "port": selected}

    def pumps_disconnect_programmatic(self) -> dict:
        """Remote API: close syringe pump serial connection."""
        if self.comm_thread and self.comm_thread.isRunning():
            self.toggle_connection()
            return {"ok": True, "message": "disconnecting"}
        return {"ok": True, "message": "already_disconnected"}

    def pumps_initialize_programmatic(self) -> dict:
        """Remote API: initialize all syringe pumps (requires connection)."""
        if not (self.comm_thread and self.comm_thread.isRunning()):
            return {"ok": False, "error": "not_connected"}
        if self.kickback_in_progress:
            return {"ok": False, "error": "kickback_in_progress"}
        if self.current_fluidic_state == self.FLUIDIC_STATE_CLEANING:
            return {"ok": False, "error": "fluidic_busy"}
        if self.routine_thread and self.routine_thread.isRunning():
            return {"ok": False, "error": "routine_running"}
        self.initialize_all_pumps()
        return {"ok": True, "message": "initializing"}
    
    def _stop_smr_saving(self):
        """Stop saving in pySMR if it is currently saving."""
        if self.smr_widget and hasattr(self.smr_widget, 'stop_saving'):
            self.smr_widget.stop_saving()
            self.log_text.append("Stopped saving in pySMR.")
    
    def _disable_smr_run(self):
        """Disable run command and send to FPGA (Asynchronous to prevent UI freeze)."""
        if not self.smr_widget:
            return
        
        # Check if FPGA command queue is available and connected
        if not hasattr(self.smr_widget, 'fpga_command_queue') or not self.smr_widget.fpga_command_queue.is_connected():
            self.log_text.append("FPGA command queue not available or not connected. Skipping run disable.")
            return
            
        def async_fpga_shutdown():
            try:
                # Load current SMR parameters
                params = _load_smr_parameters(section="default")
                
                # Get smr_driver_id from parameters
                smr_driver_id = int(params.get("smr_driver_id", 0))
                
                # Send run=False to FPGA (this helper function blocks on ACKs, so we run it here)
                success = _send_params_with_run_state(
                    self.smr_widget.fpga_command_queue,
                    params,
                    smr_driver_id,
                    False,  # run=False
                    self.smr_widget
                )
                
                if success:
                    # Update status back on main thread
                    QMetaObject.invokeMethod(self, "_on_fpga_disabled_success", Qt.QueuedConnection)
                else:
                    self.log_text.append("Warning: Failed to disable run command on FPGA (ACK timeout).")
            except Exception as e:
                print(f"Error in async FPGA shutdown thread: {e}")

        # Start the hardware teardown in a separate thread to prevent 60s GUI freeze
        threading.Thread(target=async_fpga_shutdown, daemon=True).start()
    
    @Slot()
    def _on_fpga_disabled_success(self):
        """Handle successful FPGA run-disable from background thread."""
        self.log_text.append("Disabled run command and sent to FPGA.")
        # Update run checkbox in SMR settings if available
        if hasattr(self.smr_widget, 'smr_settings_widget') and self.smr_widget.smr_settings_widget:
            if hasattr(self.smr_widget.smr_settings_widget, 'run_check'):
                self.smr_widget.smr_settings_widget.run_check.blockSignals(True)
                self.smr_widget.smr_settings_widget.run_check.setChecked(False)
                self.smr_widget.smr_settings_widget.run_check.blockSignals(False)
        # Update quick run checkbox if available
        if hasattr(self.smr_widget, 'quick_run_checkbox'):
            self.smr_widget.quick_run_checkbox.blockSignals(True)
            self.smr_widget.quick_run_checkbox.setChecked(False)
            self.smr_widget.quick_run_checkbox.blockSignals(False)
    
    def _run_next_final_clean_routine(self):
        """Run the next routine in the final clean sequence."""
        if not self.final_clean_sequence or len(self.final_clean_sequence) == 0:
            # Sequence complete
            self.final_clean_sequence = None
            self.update_status_message("Final clean sequence completed successfully.", 5000)
            
            # Ensure progress bar shows 100%
            if hasattr(self, 'final_clean_dialog'):
                self.final_clean_dialog.update_fluidic_status(100, "Fluidic Clean Complete")
            return
        
        routine_name = self.final_clean_sequence[0]
        routine_path = self.find_routine_file(routine_name)
        
        if not routine_path:
            self.show_error(f"Routine '{routine_name}' not found in {ROUTINE_SUBDIR}.\nPlease create and save this routine first.")
            self.final_clean_sequence = None
            return
        
        if not self.load_routine_from_file(routine_path):
            self.show_error(f"Failed to load routine '{routine_name}'.")
            self.final_clean_sequence = None
            return
        
        # Update status
        self.system_status_text.setText(f"Running {routine_name}...")
        self.update_status_message(f"Running {routine_name} routine...", 3000)
        
        # Update progress dialog
        if hasattr(self, 'final_clean_dialog'):
            if hasattr(self, 'total_final_clean_steps') and self.total_final_clean_steps > 0:
                progress_percent = (getattr(self, 'steps_completed_before_current_routine', 0) / self.total_final_clean_steps) * 100.0
                progress_percent = min(100.0, max(0.0, progress_percent))
            else:
                progress_percent = 0.0
            self.final_clean_dialog.update_fluidic_status(progress_percent, f"Running {routine_name}...")
        
        # Update fluidic module status in pySMR
        self.current_fluidic_state = self.FLUIDIC_STATE_CLEANING
        self.fluidic_state_changed.emit(self.FLUIDIC_STATE_CLEANING, f"Clean - {routine_name}")
        
        # Run the routine (this creates routine_thread and connects to _on_routine_finished)
        self._run_routine()
        
        # Disconnect default handler and connect custom handler for final clean sequence
        if self.routine_thread:
            try:
                self.routine_thread.routine_finished.disconnect(self._on_routine_finished)
            except:
                pass
            self.routine_thread.routine_finished.connect(self._on_final_clean_routine_finished)
    
    def _on_final_clean_routine_finished(self, message):
        """Handle completion of a routine in the final clean sequence."""
        # Check if routine completed successfully
        if "Aborted" in message or "Stopped" in message or "Error" in message.lower():
            self.show_error(f"Final clean sequence failed: {message}")
            self.final_clean_sequence = None
            self._on_routine_finished(message)
            return
        
        # Update cumulative step count before popping
        unique_steps = set()
        for row in range(self.routine_table.rowCount()):
            step_item = self.routine_table.item(row, 0)
            if step_item and step_item.text():
                unique_steps.add(step_item.text())
        
        if not hasattr(self, 'steps_completed_before_current_routine'):
            self.steps_completed_before_current_routine = 0
        self.steps_completed_before_current_routine += len(unique_steps)

        # Remove completed routine from sequence
        if self.final_clean_sequence and len(self.final_clean_sequence) > 0:
            self.final_clean_sequence.pop(0)
        
        # Continue with next routine or finish
        if self.final_clean_sequence and len(self.final_clean_sequence) > 0:
            # Disconnect this handler before running next routine
            if self.routine_thread:
                try:
                    self.routine_thread.routine_finished.disconnect(self._on_final_clean_routine_finished)
                except:
                    pass
            # Run next routine in sequence
            self._run_next_final_clean_routine()
        else:
            # Sequence complete
            self.final_clean_sequence = None
            self.update_status_message("Final clean sequence completed successfully.", 5000)
            
            # Update progress dialog
            if hasattr(self, 'final_clean_dialog'):
                self.final_clean_dialog.update_fluidic_status(100, "Fluidic cleaning complete.")
            
            self._on_routine_finished("Final clean sequence completed successfully.")
            
            # Show shutdown dialog (only after the progress dialog is finished or closed)
            # We wait for the progress dialog to be "accepted" before showing the final shutdown dialog
            if hasattr(self, 'final_clean_dialog'):
                self.final_clean_dialog.accepted.connect(lambda: QTimer.singleShot(200, self._show_clean_shutdown_dialog))
            else:
                QTimer.singleShot(500, self._show_clean_shutdown_dialog)

    def _show_clean_shutdown_dialog(self):
        """Show the dialog once the final clean sequence is complete."""
        dialog = CleanShutdownDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Reset final clean flag if they decided to keep the software open
            if self.smr_widget:
                self.smr_widget.is_final_clean = False
    
    def _on_run_beads(self):
        """Handler for Run Beads button."""
        if not self._check_fluidic_debounce(): return
        
        # Check if SMR widget is saving data
        if not self.smr_widget or not self.smr_widget.is_saving:
            QMessageBox.warning(
                self,
                "Warning",
                "Frequency data is not being saved. Please start saving before running beads/condition."
            )
            return
        
        # Enable peak detection
        if hasattr(self.smr_widget, 'enable_peak_detection'):
            self.smr_widget.enable_peak_detection()
        
        # Write experiment flag
        self.smr_widget._append_experiment_flag("Calibration")
        
        # Update condition display
        self.smr_widget._update_current_condition("Calibration")
        
        # Execute load_sample routine, then start aspiration
        self._execute_run_beads_sequence()
    
    def _on_run_condition(self):
        """Handler for Run Condition button."""
        if not self._check_fluidic_debounce(): return
        
        selected_condition = self.condition_combo.currentText()
        
        # Check if a condition is selected
        if selected_condition == "Select condition...":
            QMessageBox.warning(
                self,
                "Warning",
                "Please select a condition from the dropdown menu."
            )
            return
        
        # Integrate with Image Widget: Enable saving and check for Full Image mode
        if hasattr(self, 'image_widget') and self.image_widget:
            # Enable Image Saving
            if hasattr(self.image_widget, 'enable_saving_checkbox'):
                self.image_widget.enable_saving_checkbox.setChecked(True)
            
            # Check for Full Image mode (warning if ROI is disabled)
            if hasattr(self.image_widget, 'roi_enabled') and not self.image_widget.roi_enabled:
                QMessageBox.warning(
                    self,
                    "Warning",
                    "Warning: Make sure you go to ROI to save images"
                )
        
        # Check if SMR widget is saving data
        if not self.smr_widget or not self.smr_widget.is_saving:
            QMessageBox.warning(
                self,
                "Warning",
                "Frequency data is not being saved. Please start saving before running beads/condition."
            )
            return
        
        # Enable peak detection
        if hasattr(self.smr_widget, 'enable_peak_detection'):
            self.smr_widget.enable_peak_detection()
        
        # Write experiment flag
        self.smr_widget._append_experiment_flag(selected_condition)
        
        # Update condition display
        self.smr_widget._update_current_condition(selected_condition)
        
        # Execute load_sample routine, then start aspiration
        self._execute_run_condition_sequence(selected_condition)
    
    def _execute_run_beads_sequence(self):
        """Execute the sequence for Run Beads: load_sample routine, then aspirate."""
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected. Cannot run beads.")
            return
        
        # Update fluidic state to BEADS
        self.current_fluidic_state = self.FLUIDIC_STATE_BEADS
        self.fluidic_state_changed.emit(self.FLUIDIC_STATE_BEADS, "Running Beads")
        
        # Deferred Activation: Kickback remains disabled during the loading routine.
        # It will be enabled in _start_aspiration() once the loading routine completes.
        self.kickback_timed_enabled = False
        if hasattr(self, 'kickback_timed_checkbox'):
            self.kickback_timed_checkbox.setChecked(False)
        
        # Load and execute load_sample routine - prioritize lock.load_sample.csv over load_sample.csv
        locked_routine_path = os.path.join(ROUTINE_SUBDIR, "lock.load_sample.csv")
        routine_path = os.path.join(ROUTINE_SUBDIR, "load_sample.csv")
        
        # Check for locked version first, then fall back to regular version
        if os.path.exists(locked_routine_path):
            routine_path = locked_routine_path
        elif not os.path.exists(routine_path):
            self.show_error(f"load_sample routine not found. Checked: {locked_routine_path} and {routine_path}")
            return
        
        if not self.load_routine_from_file(routine_path):
            self.show_error("Failed to load load_sample routine.")
            return
        
        # Get routine data and execute
        routine_data = []
        for row in range(self.routine_table.rowCount()):
            try:
                action = self.get_cell_text(row, 2)
                if not action:
                    continue
                
                s_text = self.get_cell_text(row, 1)
                s_idx = None
                if 'Syringe' in s_text or 'S' in s_text:
                    match = re.search(r'\d+', s_text)
                    if match:
                        s_idx = int(match.group()) - 1
                
                port_text = self.get_cell_text(row, 3)
                volume_text = self.get_cell_text(row, 4)
                rate_text = self.get_cell_text(row, 5)
                
                data = {
                    'step': int(self.routine_table.item(row, 0).text()),
                    'syringe': s_idx,
                    'action': action
                }
                
                if action == "Move Valve":
                    match = re.match(r"(\d+)", port_text)
                    val = int(match.group(1)) if match else 1
                    data['param1'] = val
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Home":
                    data['param1'] = None
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Empty Syringe":
                    data['param1'] = None
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Prime Reagent":
                    match = re.match(r"(\d+)", port_text)
                    val = int(match.group(1)) if match else 1
                    data['param1'] = val
                    data['param2'] = None
                    data['param3'] = None
                elif action in ["Draw", "Dispense"]:
                    match = re.match(r"(\d+)", port_text)
                    data['param1'] = int(match.group(1)) if match else 1
                    data['param2'] = volume_text
                    data['param3'] = rate_text
                elif action == "Wait":
                    data['param1'] = volume_text  # Duration stored in param1
                    data['param2'] = None
                    data['param3'] = None
                
                routine_data.append(data)
            except Exception as e:
                print(f"Error parsing routine row {row}: {e}")
                continue
        
        if not routine_data:
            self.show_error("Routine is empty.")
            return
        
        # Create and start routine thread
        self.routine_thread = RoutineThread(routine_data, self)
        self.routine_thread.update_status.connect(self._update_routine_row_status)
        
        # Determine completion behavior: Auto Clean should return to IDLE, not trigger aspiration
        is_clean_sequence = getattr(self, 'is_clean_routine', False)
        self.routine_thread.routine_finished.connect(lambda msg: self._handle_routine_completion(msg, is_beads=(not is_clean_sequence)))
        self.routine_thread.step_changed.connect(self._highlight_step)
        self.routine_thread.progress_updated.connect(self._update_routine_progress)
        
        # Connect Auto Clean dialog if active (e.g. if a run sequence triggers a popup)
        if hasattr(self, 'auto_clean_dialog') and self.auto_clean_dialog:
            self.routine_thread.progress_updated.connect(self.auto_clean_dialog.update_progress)
            self.routine_thread.update_status.connect(lambda row, msg, color: self.auto_clean_dialog.update_status(msg))
        
        # Connect status update signal
        self.comm_thread.status_update.connect(self.routine_thread.on_status_update)
        
        # Update system status to "Loading Sample"
        self.system_status_text.setText("Loading Sample")
        
        self.routine_thread.start()
        self.update_status_message("Running load_sample routine...", 3000)
    
    def _execute_run_condition_sequence(self, condition_name: str):
        """Execute the sequence for Run Condition: load_sample routine, then aspirate."""
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected. Cannot run condition.")
            return
        
        # Update fluidic state to RUNNING_SAMPLE
        self.current_fluidic_state = self.FLUIDIC_STATE_RUNNING_SAMPLE
        self.fluidic_state_changed.emit(self.FLUIDIC_STATE_RUNNING_SAMPLE, "Running Sample")
        
        # Deferred Activation: Kickback remains disabled during the loading routine.
        # It will be enabled in _start_aspiration() once the loading routine completes.
        self.kickback_timed_enabled = False
        if hasattr(self, 'kickback_timed_checkbox'):
            self.kickback_timed_checkbox.setChecked(False)
        
        # Load and execute load_sample routine - prioritize lock.load_sample.csv over load_sample.csv
        locked_routine_path = os.path.join(ROUTINE_SUBDIR, "lock.load_sample.csv")
        routine_path = os.path.join(ROUTINE_SUBDIR, "load_sample.csv")
        
        # Check for locked version first, then fall back to regular version
        if os.path.exists(locked_routine_path):
            routine_path = locked_routine_path
        elif not os.path.exists(routine_path):
            self.show_error(f"load_sample routine not found. Checked: {locked_routine_path} and {routine_path}")
            return
        
        if not self.load_routine_from_file(routine_path):
            self.show_error("Failed to load load_sample routine.")
            return
        
        # Get routine data and execute (same logic as _execute_run_beads_sequence)
        routine_data = []
        for row in range(self.routine_table.rowCount()):
            try:
                action = self.get_cell_text(row, 2)
                if not action:
                    continue
                
                s_text = self.get_cell_text(row, 1)
                s_idx = None
                if 'Syringe' in s_text or 'S' in s_text:
                    match = re.search(r'\d+', s_text)
                    if match:
                        s_idx = int(match.group()) - 1
                
                port_text = self.get_cell_text(row, 3)
                volume_text = self.get_cell_text(row, 4)
                rate_text = self.get_cell_text(row, 5)
                
                data = {
                    'step': int(self.routine_table.item(row, 0).text()),
                    'syringe': s_idx,
                    'action': action
                }
                
                if action == "Move Valve":
                    match = re.match(r"(\d+)", port_text)
                    val = int(match.group(1)) if match else 1
                    data['param1'] = val
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Home":
                    data['param1'] = None
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Empty Syringe":
                    data['param1'] = None
                    data['param2'] = None
                    data['param3'] = None
                elif action == "Prime Reagent":
                    match = re.match(r"(\d+)", port_text)
                    val = int(match.group(1)) if match else 1
                    data['param1'] = val
                    data['param2'] = None
                    data['param3'] = None
                elif action in ["Draw", "Dispense"]:
                    match = re.match(r"(\d+)", port_text)
                    data['param1'] = int(match.group(1)) if match else 1
                    data['param2'] = volume_text
                    data['param3'] = rate_text
                elif action == "Wait":
                    data['param1'] = volume_text
                    data['param2'] = None
                    data['param3'] = None
                
                routine_data.append(data)
            except Exception as e:
                print(f"Error parsing routine row {row}: {e}")
                continue
        
        if not routine_data:
            self.show_error("Routine is empty.")
            return
        
        # Create and start routine thread
        self.routine_thread = RoutineThread(routine_data, self)
        self.routine_thread.update_status.connect(self._update_routine_row_status)
        self.routine_thread.routine_finished.connect(lambda msg: self._handle_routine_completion(msg, is_beads=False))
        self.routine_thread.step_changed.connect(self._highlight_step)
        self.routine_thread.progress_updated.connect(self._update_routine_progress)
        
        # Connect status update signal
        self.comm_thread.status_update.connect(self.routine_thread.on_status_update)
        
        # Update system status to "Loading Sample"
        self.system_status_text.setText("Loading Sample")
        
        self.routine_thread.start()
        self.update_status_message("Running load_sample routine...", 3000)
    
    def _handle_routine_completion(self, message: str, is_beads: bool):
        """Handle routine completion and start aspiration.
        
        Args:
            message: Completion message from routine thread
            is_beads: True if running beads, False if running condition
        """
        # Check if routine completed successfully
        if "Aborted" in message or "Stopped" in message or "Error" in message.lower():
            self.show_error(f"Routine failed: {message}")
            return
        
        # Start kickback timer if enabled (reset will happen in _start_aspiration)
        if self.kickback_timed_enabled:
            # Reconnect signal to ensure it's properly connected
            try:
                self.kickback_timer.timeout.disconnect()
            except:
                pass
            self.kickback_timer.timeout.connect(self._check_kickback_timing)
            
            if not self.kickback_timer.isActive():
                self.kickback_timer.start()
                self.log_text.append(f"Kickback timer started after load_sample routine (interval: {self.kickback_timer.interval()}ms)")
        
        # Start aspiration
        self._start_aspiration(is_beads)
    
    def _start_aspiration(self, is_beads: bool):
        """Start the aspiration sequence after load_sample routine completes.
        
        Args:
            is_beads: True if running beads (use bead draw rate), False if running condition (use sample draw rate)
        """
        if not (self.comm_thread and self.comm_thread.isRunning()):
            self.show_error("Not connected. Cannot start aspiration.")
            return
        
        # Get sample volume
        try:
            sample_volume = self.sample_volume_control.get_value()
        except Exception as e:
            self.show_error(f"Error getting sample volume: {e}")
            return
        
        # Get draw rate based on type
        try:
            if is_beads:
                draw_rate_str = self.bead_draw_rate_input.text()
            else:
                draw_rate_str = self.sample_draw_rate_input.text()
            
            draw_rate = float(draw_rate_str) if draw_rate_str else 0.0
            if draw_rate <= 0:
                self.show_error("Draw rate must be greater than 0.")
                return
        except (ValueError, TypeError) as e:
            self.show_error(f"Invalid draw rate: {e}")
            return
        
        # Convert volume to steps
        steps = self.volume_to_steps(str(sample_volume))
        if steps is None or steps <= 0:
            self.show_error("Invalid sample volume.")
            return
        
        # Convert rate to velocity
        velocity = self.rate_to_velocity(draw_rate_str)
        if velocity is None:
            self.show_error("Invalid draw rate.")
            return
        
        # Store aspiration state
        self.aspiration_start_time = time.time()
        self.aspiration_target_volume = sample_volume
        self.aspiration_draw_rate = draw_rate
        self.aspiration_start_steps = self.current_steps[0]
        self.current_volume_drawn = 0.0
        self.is_aspirating = True
        
        # Send complex command: I2V{velocity}P{steps}
        # I2 = Move valve to port 2
        # V{velocity} = Set velocity
        # P{steps} = Aspirate (draw) steps
        command = f"I2V{velocity}P{steps}"
        self.send_command_to_pump(0, command)
        
        # ACTIVATE KICKBACK: Now that aspiration has started, enable kickback and reset timer
        if self.config_kickback_timed_enabled:
            self.kickback_timed_enabled = True
            self.last_kickback_time = time.time() # Reset timer to 0
            if hasattr(self, 'kickback_timed_checkbox'):
                self.kickback_timed_checkbox.setChecked(True)
                self.log_text.append("Timed kickback activated for new aspiration.")
        
        # Reset kickback timer here - counting starts when aspiration command is sent
        self.last_kickback_time = time.time()
        
        # Update valve button state to port 2
        self._update_valve_button_state(0, 2)
        
        # Prevent race condition: immediately assume pump is busy after sending the command.
        # This ensures the first poll (500ms later) doesn't falsely detect an "Idle" state 
        # if the serial response to the command is delayed.
        if hasattr(self, 'status_labels') and self.status_labels[0]['status']:
            self.status_labels[0]['status'].setText("Busy (starting)")
        
        # Update current_steps and visualizer (similar to draw_volume)
        duration_ms = (sample_volume / draw_rate) * 60 * 1000 if draw_rate > 0 else 0
        new_steps = self.current_steps[0] + steps
        new_steps = min(MAX_STEPS, new_steps)  # Clamp to max
        self.syringe_visualizers[0].animate_to(new_steps, duration_ms)
        self.current_steps[0] = new_steps
        
        # Start position polling timer (500ms interval)
        self.aspiration_poll_timer.start(500)
        
        # Update system status
        status_text = "Running Beads" if is_beads else "Running Sample"
        self.system_status_text.setText(status_text)
        
        self.log_text.append(f"Started aspiration: {sample_volume} µL at {draw_rate} µL/min")
        self.update_status_message(f"Aspirating {sample_volume} µL...", 3000)
    
    def _poll_aspiration_progress(self):
        """Poll syringe 1 position and update volume display and progress bar."""
        # Use fluidic state as the source of truth
        if self.current_fluidic_state not in [self.FLUIDIC_STATE_RUNNING_SAMPLE, self.FLUIDIC_STATE_BEADS]:
            # If state changed (e.g. to Cleaning), stop monitoring
            self._stop_aspiration_monitoring()
            return
            
        if not self.is_aspirating:
            self.aspiration_poll_timer.stop()
            return
        
        # Query pump status to keep UI updated
        self.query_pump_status(0)
        
        # Calculate current volume based on elapsed time and draw rate
        # Since we don't have a position query command, track locally
        elapsed_time = time.time() - self.aspiration_start_time
        elapsed_minutes = elapsed_time / 60.0
        
        # Calculate volume drawn so far (in µL)
        volume_drawn = elapsed_minutes * self.aspiration_draw_rate
        self.current_volume_drawn = volume_drawn
        
        # Clamp to target volume
        volume_drawn = min(volume_drawn, self.aspiration_target_volume)
        
        # Check if pump is still busy by checking status label
        status_text = self.status_labels[0]['status'].text()
        pump_busy = status_text.startswith("Busy")
        
        # Calculate current steps based on volume_drawn
        volume_steps = self.volume_to_steps(str(volume_drawn))
        current_volume = 0
        if volume_steps is not None:
            current_steps = self.aspiration_start_steps + volume_steps
            current_steps = min(current_steps, MAX_STEPS)  # Clamp to max
            self.current_steps[0] = current_steps
            
            # Convert to volume for display (S1 volume)
            current_volume = (current_steps / MAX_STEPS) * SYRINGE_VOLUME_UL
            
            # Update volume display
            if hasattr(self, 'status_labels') and self.status_labels[0]['volume']:
                self.status_labels[0]['volume'].setText(f"{current_volume:.1f}")
            
            # Update progress bar
            if self.aspiration_target_volume > 0:
                progress_percent = int((volume_drawn / self.aspiration_target_volume) * 100)
                progress_percent = min(progress_percent, 100)  # Clamp to 100%
                if hasattr(self, 'status_progress_bar'):
                    self.status_progress_bar.setValue(progress_percent)
                    
        # Explicit user-requested mathematical condition to transition to IDLE:
        # "S1 volume >= sample volume + volume drawn during sample loading"
        volume_drawn_during_loading = (self.aspiration_start_steps / MAX_STEPS) * SYRINGE_VOLUME_UL
        if current_volume >= (self.aspiration_target_volume + volume_drawn_during_loading):
            # Gather statistics for completion popup
            condition_name = "N/A"
            peaks = 0
            if hasattr(self, 'smr_widget') and self.smr_widget:
                condition_name = getattr(self.smr_widget, 'last_condition_name', 'N/A')
                peaks = getattr(self.smr_widget, 'condition_peaks_total', 0)
            
            images = 0
            if hasattr(self, 'image_widget') and self.image_widget:
                images = len(getattr(self.image_widget, 'saved_image_numbers', []))
            
            volume = self.aspiration_target_volume
                
            self._stop_aspiration_monitoring()
            
            # Show popup message
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Aspiration Complete")
            msg_box.setText(f"Finished aspirating {volume} uL for condition {condition_name}")
            msg_box.setInformativeText(f"Measured {peaks} events and saved {images} images")
            msg_box.setIcon(QMessageBox.Icon.Information)
            msg_box.exec()
            
            return
            
        if not pump_busy:
            # If pump stopped before target (e.g. error or reached max early), also stop monitoring
            self._stop_aspiration_monitoring()
            return
        
        # --- Auto Clean Check ---
        # If auto clean is enabled enabled and we are running a condition
        if hasattr(self, 'auto_clean_enabled') and self.auto_clean_enabled:
            # Check if volume requirement is met
            min_vol = self.minimum_volume_control.get_value()
            
            # Check if peak requirement is met
            min_peaks = self.minimum_peaks_control.get_value()
            current_peaks = 0
            if hasattr(self, 'smr_widget') and self.smr_widget:
                if hasattr(self.smr_widget, 'condition_peaks_total'):
                    current_peaks = self.smr_widget.condition_peaks_total
            
            # If both requirements are met, trigger auto clean
            # Rely on state check at the top of function to ensure we are in valid state
            if volume_drawn >= min_vol and current_peaks >= min_peaks:
                if getattr(self, 'auto_clean_pending', False):
                    # Already triggered, waiting for timer
                    return
                
                self.auto_clean_pending = True
                self.log_text.append(f"Auto Clean triggered: Vol={volume_drawn:.1f}uL, Peaks={current_peaks}")
                
                # Stop pump immediately
                self.pause_pump(0)
                
                # Trigger clean based on selected protocol
                protocol = self.auto_clean_protocol_combo.currentText()
                
                # Gather stats for dialog (with robust null-checks to avoid blocking fluidics)
                smr = getattr(self, 'smr_widget', None)
                img = getattr(self, 'image_widget', None)
                
                condition_name = 'N/A'
                if smr and hasattr(smr, 'last_condition_name'):
                    condition_name = smr.last_condition_name or 'N/A'
                
                images = 0
                if img and hasattr(img, 'total_saved_frames'):
                    images = img.total_saved_frames
                
                # Run cleanup in a separate thread or via timer to allow this method to return
                # This will change state to CLEANING, which will stop this poll loop next time
                QTimer.singleShot(100, lambda: self._execute_auto_clean(protocol, condition_name, current_peaks, volume_drawn, images))
                return

        # Check if aspiration should be complete (based on time)
        expected_duration_minutes = self.aspiration_target_volume / self.aspiration_draw_rate
        expected_duration_seconds = expected_duration_minutes * 60
        
        # Add small buffer (1 second) to account for communication delays
        if elapsed_time >= (expected_duration_seconds + 1.0):
            # Aspiration should be complete, check status one more time
            # If still busy after expected time + buffer, stop monitoring anyway
            self.query_pump_status(0)
            # Will be checked in next poll cycle
    
    def _stop_aspiration_monitoring(self):
        """Stop aspiration monitoring and reset UI."""
        self.is_aspirating = False
        self.aspiration_poll_timer.stop()
        
        # Stop kickback timer when aspiration stops
        self.kickback_timer.stop()
        
        # Automatically disable timed kickback checkbox when idling
        # The configured preference is stored in config_kickback_timed_enabled
        self.kickback_timed_enabled = False
        if hasattr(self, 'kickback_timed_checkbox'):
            self.kickback_timed_checkbox.setChecked(False)
        # self.last_kickback_time = None  <-- REMOVED: Caused flickering to '--'
        
        if hasattr(self, 'status_progress_bar'):
            self.status_progress_bar.setValue(0)
        
        # Reset system status to "Idle"
        self.system_status_text.setText("Idle")
        
        # Update fluidic state to IDLE if we were in RUNNING_SAMPLE or BEADS
        if self.current_fluidic_state in [self.FLUIDIC_STATE_RUNNING_SAMPLE, self.FLUIDIC_STATE_BEADS]:
            self.current_fluidic_state = self.FLUIDIC_STATE_IDLE
            self.fluidic_state_changed.emit(self.FLUIDIC_STATE_IDLE, "Idle")
        
        self.aspiration_start_time = None
        self.aspiration_target_volume = None
        self.aspiration_draw_rate = None
        self.aspiration_start_steps = None
        
        self.update_status_message("Aspiration completed.", 3000)
    
    def set_smr_widget(self, smr_widget):
        """Sets the reference to the SMR control widget."""
        self.smr_widget = smr_widget

    def set_image_widget(self, image_widget):
        """Sets the reference to the image control widget."""
        self.image_widget = image_widget

    def _execute_auto_clean(self, protocol, condition_name, peaks, volume, images):
        """Automatically execute the selected clean protocol (triggered by auto clean logic)."""
        self.auto_clean_pending = False  # Reset pending flag
        
        # Stop any active kickback immediately to prevent command collision
        if self.kickback_executor:
            try:
                self.kickback_executor.stop()
            except Exception as e:
                print(f"Error stopping kickback during auto-clean start: {e}")
        
        # Stop kickback timer when auto clean starts
        self.kickback_timer.stop()
        self.kickback_timed_enabled = False
        if hasattr(self, 'kickback_timed_checkbox'):
            self.kickback_timed_checkbox.setChecked(False)
        # self.last_kickback_time = None # Keep time valid until state changes
        
        # Update fluidic state to CLEANING
        self.current_fluidic_state = self.FLUIDIC_STATE_CLEANING
        self.fluidic_state_changed.emit(self.FLUIDIC_STATE_CLEANING, f"Clean - {protocol}")
        
        # Disable peak detection
        if self.smr_widget and hasattr(self.smr_widget, 'disable_peak_detection'):
            self.smr_widget.disable_peak_detection()
            
        # Find and load the routine
        routine_path = self.find_routine_file(protocol)
        if routine_path:
            if self.load_routine_from_file(routine_path):
                self.is_clean_routine = True
                
                # Update condition indicator
                if self.smr_widget and hasattr(self.smr_widget, '_update_current_condition'):
                    self.smr_widget._update_current_condition("None - Auto Cleaning")
                
                # Log to experiment_flags.txt
                if self.smr_widget and self.smr_widget.is_saving:
                    flag_text = f"Auto Clean Triggered: {protocol}"
                    self.smr_widget._append_experiment_flag(flag_text)
                
                # Launch Auto Clean Progress Dialog if enabled in config
                # SAFETY: Wrap UI logic in try/except so a popup failure doesn't block the fluidic routine
                self.auto_clean_dialog = None
                try:
                    if get_autoclean_summary_enabled():
                        self.auto_clean_dialog = AutoCleanProgressDialog(condition_name, peaks, volume, images, self)
                        self.auto_clean_dialog.show()
                except Exception as e:
                    print(f"Error launching Auto Clean summary popup: {e}")
                
                self.update_status_message(f"Auto Clean: Running {protocol} routine...", 3000)
                self._run_routine()
            else:
                self.show_error(f"Auto Clean failed to load routine: {protocol}")
        else:
            self.show_error(f"Auto Clean routine '{protocol}' not found.")
    
    def load_conditions_from_sample_folder(self, sample_path):
        """
        Load conditions from conditions.txt file in the sample folder.
        
        Args:
            sample_path: Path to the sample folder containing conditions.txt
        """
        if not sample_path or not os.path.exists(sample_path):
            print(f"Warning: Cannot load conditions - sample path does not exist: {sample_path}")
            # Clear dropdown and set default
            if hasattr(self, 'condition_combo'):
                self.condition_combo.clear()
                self.condition_combo.addItems(["Select condition..."])
            return
        
        conditions_file = os.path.join(sample_path, 'conditions.txt')
        
        if not os.path.exists(conditions_file):
            print(f"Warning: conditions.txt not found in sample folder: {sample_path}")
            # Clear dropdown and set default
            if hasattr(self, 'condition_combo'):
                self.condition_combo.clear()
                self.condition_combo.addItems(["Select condition..."])
            return
        
        try:
            conditions = []
            with open(conditions_file, 'r', encoding='utf-8') as f:
                for line in f:
                    # Strip whitespace and skip empty lines
                    condition = line.strip()
                    if condition:
                        conditions.append(condition)
            
            if conditions:
                # Update dropdown with loaded conditions
                if hasattr(self, 'condition_combo'):
                    self.condition_combo.clear()
                    self.condition_combo.addItems(["Select condition..."] + conditions)
                    self.log_text.append(f"Loaded {len(conditions)} conditions from conditions.txt")
                    self.update_status_message(f"Loaded {len(conditions)} conditions", 2000)
            else:
                print("Warning: conditions.txt is empty")
                if hasattr(self, 'condition_combo'):
                    self.condition_combo.clear()
                    self.condition_combo.addItems(["Select condition..."])
        except Exception as e:
            print(f"Error loading conditions.txt: {e}")
            import traceback
            traceback.print_exc()
            # Clear dropdown on error
            if hasattr(self, 'condition_combo'):
                self.condition_combo.clear()
                self.condition_combo.addItems(["Select condition..."])
    
    def _on_toggle_auto_clean(self, checked):
        """Handler for Auto clean toggle checkbox."""
        self.auto_clean_enabled = checked
        self._update_auto_clean_checkbox_style()
        status_text = "Auto-clean enabled" if self.auto_clean_enabled else "Auto clean disabled"
        self.log_text.append(f"Auto clean toggled: {status_text}")
        self.update_status_message(f"Auto clean: {status_text}", 3000)
    
    def _update_auto_clean_checkbox_style(self):
        """Update the auto clean checkbox appearance based on enabled/disabled state."""
        is_checked = self.auto_clean_checkbox.isChecked()
        if is_checked:
            self.auto_clean_checkbox.setText("Auto-clean enabled")
        else:
            self.auto_clean_checkbox.setText("Auto-clean disabled")
        
        # Apply styling matching pySMR Run checkbox
        self.auto_clean_checkbox.setStyleSheet("""
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
                background-color: #4CAF50;
                border: 2px solid #45a049;
            }
            QCheckBox::indicator:unchecked {
                background-color: #555555;
                border: 2px solid #444444;
            }
        """)
        
    def _parse_toml_config(self, content):
        """Parse a simple TOML-style config file into a structured dict."""
        config = {}
        current_section = None
        
        for line in content.split('\n'):
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
            
            # Section header: [syringe1] or [syringe2] or [settings]
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1].strip()
                if current_section not in config:
                    config[current_section] = {}
                continue
            
            # Key-value pairs
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                
                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                
                # Handle nested keys like "port1.nickname" or "device.draw_rate"
                if '.' in key:
                    parts = key.split('.')
                    if current_section:
                        if current_section not in config:
                            config[current_section] = {}
                        # Create nested structure
                        parent_key = parts[0]
                        child_key = parts[1]
                        if parent_key not in config[current_section]:
                            config[current_section][parent_key] = {}
                        config[current_section][parent_key][child_key] = value
                    else:
                        if 'root' not in config:
                            config['root'] = {}
                        parent_key = parts[0]
                        child_key = parts[1]
                        if parent_key not in config['root']:
                            config['root'][parent_key] = {}
                        config['root'][parent_key][child_key] = value
                else:
                    # Simple key-value
                    if current_section:
                        config[current_section][key] = value
                    else:
                        if 'root' not in config:
                            config['root'] = {}
                        config['root'][key] = value
        
        return config

    def _write_toml_config(self, config_data):
        """Write a structured dict to a TOML-style config file."""
        lines = []
        lines.append("# Pump Configuration File")
        lines.append("# TOML-style format")
        lines.append("")
        
        # Write syringe port configurations
        for syringe_num in [1, 2]:
            section = f'syringe{syringe_num}'
            lines.append(f"[{section}]")
            
            for port_num in range(1, 7):
                port_key = f'port{port_num}'
                if section in config_data and port_key in config_data[section]:
                    port_data = config_data[section][port_key]
                    nickname = port_data.get('nickname', f'S{syringe_num} Port {port_num}')
                    port_type = port_data.get('type', 'Device')
                    lines.append(f'  {port_key}.nickname = "{nickname}"')
                    lines.append(f'  {port_key}.type = "{port_type}"')
            
            lines.append("")
        
        # Write syringe settings (draw/dispense rates)
        lines.append("[settings]")
        lines.append("# Draw and dispense rates (µL/min) for each port type")
        lines.append("# Applied to both syringes")
        
        if 'settings' in config_data:
            settings = config_data['settings']
            
            # Write COM_PORT preference
            if 'com_port' in settings:
                com_port = settings['com_port']
                lines.append(f'  com_port = "{com_port}"')
            
            # Write port type rates
            for port_type in ['Device', 'Reagent', 'Waste', 'Empty']:
                port_type_lower = port_type.lower()
                if port_type_lower in settings:
                    draw_rate = settings[port_type_lower].get('draw_rate', '')
                    dispense_rate = settings[port_type_lower].get('dispense_rate', '')
                    lines.append(f'  {port_type_lower}.draw_rate = "{draw_rate}"')
                    lines.append(f'  {port_type_lower}.dispense_rate = "{dispense_rate}"')
        
        lines.append("")
        
        # Write cleaning settings
        if 'clean' in config_data:
            clean = config_data['clean']
            lines.append("[clean]")
            if 'manual_clean_routine' in clean:
                lines.append(f'manual_clean_routine = "{clean["manual_clean_routine"]}"')
            if 'auto_clean_enabled' in clean:
                lines.append(f'auto_clean_enabled = {str(clean["auto_clean_enabled"]).lower()}')
            if 'auto_clean_routine' in clean:
                lines.append(f'auto_clean_routine = "{clean["auto_clean_routine"]}"')
            
            if 'auto_clean' in clean:
                ac = clean['auto_clean']
                if 'minimum_peaks' in ac:
                    lines.append(f'auto_clean.minimum_peaks = "{ac["minimum_peaks"]}"')
                if 'minimum_volume' in ac:
                    lines.append(f'auto_clean.minimum_volume = "{ac["minimum_volume"]}"')
            lines.append("")

        # Write kickback settings
        if 'kickback' in config_data:
            kickback = config_data['kickback']
            lines.append("[kickback]")
            for key, val in kickback.items():
                if isinstance(val, bool):
                    lines.append(f'{key} = {str(val).lower()}')
                else:
                    lines.append(f'{key} = {val}')
            lines.append("")
            
        return '\n'.join(lines)

    def load_or_create_config(self):
        """Load configuration from pump_config.txt (READ-ONLY)."""
        if not os.path.exists(self.config_file):
            # Config file doesn't exist - use default values (config files are read-only)
            return
        try:
            # Read pump config (READ-ONLY - never write to this file)
            with open(self.config_file, mode='r', encoding='utf-8') as file:
                content = file.read()
                config = self._parse_toml_config(content)
                
                # Load port nicknames and types
                for syringe_num in [1, 2]:
                    syringe_idx = syringe_num - 1
                    section = f'syringe{syringe_num}'
                    
                    if section in config:
                        for port_num in range(1, 7):
                            port_key = f'port{port_num}'
                            if port_key in config[section]:
                                port_data = config[section][port_key]
                                if isinstance(port_data, dict):
                                    nickname = port_data.get('nickname', f'S{syringe_num} Port {port_num}')
                                    port_type = port_data.get('type', 'Device')
                                else:
                                    # Handle old format where port_data might be a string
                                    nickname = port_data if isinstance(port_data, str) else f'S{syringe_num} Port {port_num}'
                                    port_type = 'Device'
                                
                                self.nickname_inputs[syringe_idx][port_num].setText(nickname)
                                self.port_type_combos[syringe_idx][port_num].setCurrentText(port_type)
                
                # Load speed settings
                if 'settings' in config:
                    settings = config['settings']
                    for port_type in ['Device', 'Reagent', 'Waste', 'Empty']:
                        port_type_lower = port_type.lower()
                        if port_type_lower in settings:
                            setting_data = settings[port_type_lower]
                            if isinstance(setting_data, dict):
                                draw_rate = setting_data.get('draw_rate', '')
                                dispense_rate = setting_data.get('dispense_rate', '')
                            else:
                                # Handle old format
                                draw_rate = ''
                                dispense_rate = ''
                            
                            # Apply to both syringes
                            for syringe_idx in [0, 1]:
                                if port_type in self.speed_inputs[syringe_idx]:
                                    # Always set the value, even if empty (to clear disabled fields)
                                    self.speed_inputs[syringe_idx][port_type]['Draw'].setText(draw_rate)
                                    self.speed_inputs[syringe_idx][port_type]['Dispense'].setText(dispense_rate)
                    
                    # Load sample and bead draw rates from SYSTEM configuration
                    try:
                        sys_config = load_system_config()
                        pump_settings = get_syringe_pump_settings(sys_config)
                        cam_settings = get_camera_settings(sys_config)
                        
                        self.camera_mode = cam_settings.get("camera_mode", "BF+FL")
                        self.sample_bf_draw_rate = pump_settings.get("sample.bf_draw_rate", "2.0")
                        self.sample_bffl_draw_rate = pump_settings.get("sample.bffl_draw_rate", "1.0")
                        self.bead_draw_rate_value = pump_settings.get("bead.draw_rate", "2.0")
                        
                        # Set current sample draw rate based on mode
                        if self.camera_mode == "BF only":
                            self.sample_draw_rate_value = self.sample_bf_draw_rate
                        else:
                            self.sample_draw_rate_value = self.sample_bffl_draw_rate
                            
                        # Update UI if it exists
                        if self.sample_draw_rate_input:
                            self.sample_draw_rate_input.setText(self.sample_draw_rate_value)
                        if self.bead_draw_rate_input:
                            self.bead_draw_rate_input.setText(self.bead_draw_rate_value)
                            
                    except Exception as e:
                        print(f"Error loading syringe settings from system config: {e}")
                
                    if 'kickback' in config:
                        kickback_settings = config['kickback']
                        # Convert string boolean to actual boolean
                        timed_enabled_str = kickback_settings.get('timed_kickbacks_enabled', 'false')
                        if isinstance(timed_enabled_str, str):
                            self.kickback_timed_enabled = timed_enabled_str.lower() in ('true', '1', 'yes')
                        else:
                            self.kickback_timed_enabled = bool(timed_enabled_str)
                        
                        self.config_kickback_timed_enabled = self.kickback_timed_enabled
                        
                        self.kickback_time_seconds = float(kickback_settings.get('kickback_time_seconds', 300.0))
                        self.kickback_volume_ul = float(kickback_settings.get('kickback_volume_ul', 5.0))
                        self.kickback_rate_ul_min = float(kickback_settings.get('kickback_rate_ul_min', 100.0))
                        
                        # Update UI controls if they exist
                        if hasattr(self, 'kickback_timed_checkbox'):
                            self.kickback_timed_checkbox.setChecked(self.kickback_timed_enabled)
                        if hasattr(self, 'kickback_time_control'):
                            self.kickback_time_control.set_value(self.kickback_time_seconds)
                        if hasattr(self, 'kickback_volume_input'):
                            self.kickback_volume_input.setText(str(self.kickback_volume_ul))
                        if hasattr(self, 'kickback_rate_input'):
                            self.kickback_rate_input.setText(str(self.kickback_rate_ul_min))
                
                # Load cleaning settings
                if 'clean' in config:
                    clean_settings = config['clean']
                    
                    # Manual clean protocol
                    manual_routine = clean_settings.get('manual_clean_routine')
                    if manual_routine and hasattr(self, 'clean_protocol_combo'):
                        self.clean_protocol_combo.setCurrentText(manual_routine)
                        
                    # Auto clean enabled
                    auto_enabled_str = clean_settings.get('auto_clean_enabled', 'false')
                    if isinstance(auto_enabled_str, str):
                        auto_enabled = auto_enabled_str.lower() in ('true', '1', 'yes')
                    else:
                        auto_enabled = bool(auto_enabled_str)
                    
                    if hasattr(self, 'auto_clean_checkbox'):
                        self.auto_clean_checkbox.setChecked(auto_enabled)
                        self.auto_clean_enabled = auto_enabled # Ensure internal state is updated
                    
                    # Auto clean protocol
                    auto_routine = clean_settings.get('auto_clean_routine')
                    if auto_routine and hasattr(self, 'auto_clean_protocol_combo'):
                        self.auto_clean_protocol_combo.setCurrentText(auto_routine)
                        
                    # Auto clean thresholds
                    if 'auto_clean' in clean_settings:
                        auto_clean_params = clean_settings['auto_clean']
                        
                        min_peaks = auto_clean_params.get('minimum_peaks')
                        if min_peaks and hasattr(self, 'minimum_peaks_control'):
                            try:
                                self.minimum_peaks_control.set_value(int(float(min_peaks)))
                            except (ValueError, TypeError):
                                pass
                                
                        min_vol = auto_clean_params.get('minimum_volume')
                        if min_vol and hasattr(self, 'minimum_volume_control'):
                            try:
                                self.minimum_volume_control.set_value(float(min_vol))
                            except (ValueError, TypeError):
                                pass
                
                # Apply COM_PORT preference after loading config
                # refresh_com_ports will handle setting the default based on config or highest COM number
                if hasattr(self, 'com_port_combo'):
                    self.refresh_com_ports()
                                        
        except Exception as e:
            self.show_error(f"Failed to read config file '{self.config_file}':\n{e}")

    def create_default_config(self):
        """Create a default configuration file.
        
        NOTE: This function is disabled - config files are READ-ONLY.
        If the config file doesn't exist, the application will use default values.
        """
        # Config files are read-only - do not create default config
        # If config file doesn't exist, the application will use default values
        pass

    def save_config(self):
        """Save current configuration to file.
        
        NOTE: This function is disabled - config files are READ-ONLY.
        Configuration changes are not persisted to disk.
        """
        # Config files are read-only - do not save configuration
        return

    def closeEvent(self, event):
        self._stop_routine()
        # Config files are read-only - do not save on close
        if self.comm_thread and self.comm_thread.isRunning():
            self.comm_thread.stop()
            self.comm_thread.wait() 
        if self.routine_thread and self.routine_thread.isRunning():
            self.routine_thread.stop()
            self.routine_thread.wait()
        event.accept()

class ActionDialog(QDialog):
    """A dialog for adding a new action to the routine."""
    def __init__(self, action_type, parent=None, last_valve_positions=None):
        super().__init__(parent)
        self.action_type = action_type
        self.last_valve_positions = last_valve_positions or {0: None, 1: None}
        self.setWindowTitle(f"Add '{action_type}' Action")
        self.parent_window = parent

        self.layout = QFormLayout(self)
        self.inputs = {}

        if action_type in ["Draw", "Dispense", "Move Valve", "Home", "Empty Syringe", "Prime Reagent", "Interrupt"]:
            # Create selection buttons for Syringe instead of dropdown
            syringe_container = QWidget()
            syringe_layout = QHBoxLayout(syringe_container)
            syringe_layout.setContentsMargins(0, 0, 0, 0)
            
            self.inputs['syringe'] = QButtonGroup(syringe_container)
            s1_btn = QPushButton("S1")
            s2_btn = QPushButton("S2")
            s1_btn.setCheckable(True)
            s2_btn.setCheckable(True)
            s1_btn.setChecked(True)  # Default to S1
            
            # Add "Both" button for Home, Empty Syringe, Prime Reagent, and Interrupt
            both_btn = None
            if action_type in ["Home", "Empty Syringe", "Prime Reagent", "Interrupt"]:
                both_btn = QPushButton("Both")
                both_btn.setCheckable(True)
            
            # Style buttons: gray when unchecked, blue when checked
            button_style = f"""
                QPushButton {{
                    background-color: {Colors.BG_LIGHT};
                    color: {Colors.TEXT_DARK};
                    border: 2px solid {Colors.BORDER_MEDIUM};
                    border-radius: 5px;
                    padding: 8px 20px;
                    font-weight: bold;
                    min-width: 80px;
                }}
                QPushButton:checked {{
                    background-color: {Colors.PRIMARY_BLUE};
                    color: white;
                    border: 2px solid {Colors.PRIMARY_BLUE_HOVER};
                }}
                QPushButton:hover {{
                    background-color: {Colors.BG_DISABLED};
                }}
                QPushButton:checked:hover {{
                    background-color: {Colors.PRIMARY_BLUE_HOVER};
                }}
            """
            s1_btn.setStyleSheet(button_style)
            s2_btn.setStyleSheet(button_style)
            if both_btn:
                both_btn.setStyleSheet(button_style)
            
            self.inputs['syringe'].addButton(s1_btn, 0)
            self.inputs['syringe'].addButton(s2_btn, 1)
            syringe_layout.addWidget(s1_btn)
            syringe_layout.addWidget(s2_btn)
            if both_btn:
                self.inputs['syringe'].addButton(both_btn, 2)  # Use ID 2 for "Both"
                syringe_layout.addWidget(both_btn)
            syringe_layout.addStretch()
            
            self.layout.addRow("Syringe:", syringe_container)

        if action_type in ["Draw", "Dispense"]:
            # Helper function to get default rate based on syringe, port, and action
            def get_default_rate(syringe_idx, port_num, is_draw):
                """Get the default rate from syringe settings based on port."""
                if not parent:
                    return 100.0
                
                # Get port type for the selected port
                if port_num in parent.port_type_combos[syringe_idx]:
                    port_type = parent.port_type_combos[syringe_idx][port_num].currentText()
                    # Get rate from speed_inputs
                    if port_type in parent.speed_inputs[syringe_idx]:
                        rate_key = 'Draw' if is_draw else 'Dispense'
                        rate_str = parent.speed_inputs[syringe_idx][port_type][rate_key].text()
                        try:
                            rate_val = float(rate_str) if rate_str else None
                            if rate_val is not None:
                                return rate_val
                        except ValueError:
                            pass
                
                # Fallback: find the slowest rate among all port types for this action
                rate_key = 'Draw' if is_draw else 'Dispense'
                slowest_rate = None
                port_types = ['Device', 'Reagent', 'Waste', 'Empty']
                
                for port_type in port_types:
                    if port_type in parent.speed_inputs[syringe_idx]:
                        rate_str = parent.speed_inputs[syringe_idx][port_type][rate_key].text()
                        try:
                            rate_val = float(rate_str) if rate_str else None
                            if rate_val is not None:
                                if slowest_rate is None or rate_val < slowest_rate:
                                    slowest_rate = rate_val
                        except ValueError:
                            continue
                
                # Return slowest rate found, or default to 100.0 if none found
                return slowest_rate if slowest_rate is not None else 100.0
            
            # Get initial syringe and port selection
            initial_syringe_idx = 0
            initial_port = self.last_valve_positions.get(initial_syringe_idx) or 1
            initial_rate = get_default_rate(initial_syringe_idx, initial_port, action_type == "Draw")
            
            # Create port selection buttons (6 side-by-side buttons)
            port_container = QWidget()
            port_layout = QHBoxLayout(port_container)
            port_layout.setContentsMargins(0, 0, 0, 0)
            
            self.inputs['param1'] = QButtonGroup(port_container)
            
            # Style buttons: gray when unchecked, blue when checked, dark gray when disabled
            port_button_style = """
                QPushButton {
                    background-color: #e0e0e0;
                    color: #333;
                    border: 2px solid #a0a0a0;
                    border-radius: 5px;
                    padding: 8px 12px;
                    font-weight: bold;
                    min-width: 80px;
                }
                QPushButton:checked {
                    background-color: #0078d7;
                    color: white;
                    border: 2px solid #005a9e;
                }
                QPushButton:hover {
                    background-color: #d0d0d0;
                }
                QPushButton:checked:hover {
                    background-color: #005a9e;
                }
                QPushButton:disabled {
                    background-color: #505050;
                    color: #999;
                    border: 2px solid #404040;
                }
            """
            
            # Create 6 port buttons
            for port_num in range(1, 7):
                port_btn = QPushButton()
                port_btn.setCheckable(True)
                port_btn.setStyleSheet(port_button_style)
                self.inputs['param1'].addButton(port_btn, port_num)
                port_layout.addWidget(port_btn)
            
            # Set initial port button as checked (only if enabled)
            initial_port_btn = self.inputs['param1'].button(initial_port)
            if initial_port_btn:
                initial_rate_check = get_default_rate(initial_syringe_idx, initial_port, action_type == "Draw")
                if initial_rate_check > 0:
                    initial_port_btn.setChecked(True)
                else:
                    # Find first enabled port
                    for p in range(1, 7):
                        check_rate = get_default_rate(initial_syringe_idx, p, action_type == "Draw")
                        if check_rate > 0:
                            self.inputs['param1'].button(p).setChecked(True)
                            break
            
            # Store update function for use in showEvent
            def update_port_buttons():
                syringe_idx = self.inputs['syringe'].checkedId()
                if syringe_idx < 0:
                    syringe_idx = 0
                is_draw = action_type == "Draw"
                for port_num in range(1, 7):
                    btn = self.inputs['param1'].button(port_num)
                    if btn and parent:
                        nickname = ""
                        if (syringe_idx in parent.nickname_inputs and 
                            port_num in parent.nickname_inputs[syringe_idx]):
                            nickname = parent.nickname_inputs[syringe_idx][port_num].text()
                        btn.setText(f"{port_num}\n{nickname}" if nickname else str(port_num))
                        
                        # Check if rate is valid (> 0) for this port
                        rate = get_default_rate(syringe_idx, port_num, is_draw)
                        was_checked = btn.isChecked()
                        if rate > 0:
                            btn.setEnabled(True)
                        else:
                            btn.setEnabled(False)
                            # Uncheck if currently checked but becoming disabled
                            if was_checked:
                                # Find first enabled port and check it instead
                                for p in range(1, 7):
                                    other_btn = self.inputs['param1'].button(p)
                                    if other_btn and p != port_num:
                                        other_rate = get_default_rate(syringe_idx, p, is_draw)
                                        if other_rate > 0:
                                            other_btn.setChecked(True)
                                            break
                self._update_rate_from_port()
            
            # Store the update function for use in showEvent
            self._update_port_buttons = update_port_buttons
            
            # Connect syringe buttons to update port button labels
            for button in self.inputs['syringe'].buttons():
                button.toggled.connect(update_port_buttons)
            
            # Connect port buttons to update rate
            for port_num in range(1, 7):
                btn = self.inputs['param1'].button(port_num)
                if btn:
                    btn.toggled.connect(self._update_rate_from_port)
            
            # Initial update of port button labels
            update_port_buttons()
            
            self.layout.addRow("Port:", port_container)
            
            # Volume control using custom increment control
            if parent and hasattr(parent, '_create_increment_control'):
                self.inputs['param2'] = parent._create_increment_control(
                    min_val=0.1, max_val=SYRINGE_VOLUME_UL, initial_val=10.0, 
                    step=0.5, suffix="", is_int=False
                )
            else:
                # Fallback to QDoubleSpinBox if parent not available
                self.inputs['param2'] = QDoubleSpinBox()
                self.inputs['param2'].setRange(0.1, SYRINGE_VOLUME_UL)
                self.inputs['param2'].setDecimals(1)
                self.inputs['param2'].setValue(10.0)
                style_input_field(self.inputs['param2'])
            self.layout.addRow("Volume (µL):", self.inputs['param2'])
            
            # Rate control using custom increment control with default from settings
            if parent and hasattr(parent, '_create_increment_control'):
                self.inputs['param3'] = parent._create_increment_control(
                    min_val=1.0, max_val=10000.0, initial_val=initial_rate, 
                    step=10.0, suffix="", is_int=False
                )
            else:
                # Fallback to QDoubleSpinBox if parent not available
                self.inputs['param3'] = QDoubleSpinBox()
                self.inputs['param3'].setRange(1, 10000)
                self.inputs['param3'].setValue(initial_rate)
                style_input_field(self.inputs['param3'])
            self.layout.addRow("Rate (µL/min):", self.inputs['param3'])
            
            # Helper to update rate when port or syringe changes
            def update_rate_helper():
                self._update_rate_from_port()
            
            # Store helper for use in _update_rate_from_port
            self._update_rate_helper = update_rate_helper
            
            # Connect to button toggled signals
            for button in self.inputs['syringe'].buttons():
                button.toggled.connect(update_rate_helper)

        elif action_type == "Move Valve":
            # Create port selection buttons (6 side-by-side buttons)
            port_container = QWidget()
            port_layout = QHBoxLayout(port_container)
            port_layout.setContentsMargins(0, 0, 0, 0)
            
            self.inputs['param1'] = QButtonGroup(port_container)
            
            # Style buttons: gray when unchecked, blue when checked, dark gray when disabled
            port_button_style = """
                QPushButton {
                    background-color: #e0e0e0;
                    color: #333;
                    border: 2px solid #a0a0a0;
                    border-radius: 5px;
                    padding: 8px 12px;
                    font-weight: bold;
                    min-width: 80px;
                }
                QPushButton:checked {
                    background-color: #0078d7;
                    color: white;
                    border: 2px solid #005a9e;
                }
                QPushButton:hover {
                    background-color: #d0d0d0;
                }
                QPushButton:checked:hover {
                    background-color: #005a9e;
                }
                QPushButton:disabled {
                    background-color: #505050;
                    color: #999;
                    border: 2px solid #404040;
                }
            """
            
            # Get initial port from last valve position
            initial_syringe_idx = 0
            initial_port = self.last_valve_positions.get(initial_syringe_idx) or 1
            
            # Create 6 port buttons
            for port_num in range(1, 7):
                port_btn = QPushButton()
                port_btn.setCheckable(True)
                port_btn.setStyleSheet(port_button_style)
                self.inputs['param1'].addButton(port_btn, port_num)
                port_layout.addWidget(port_btn)
            
            # Set initial port button as checked
            initial_port_btn = self.inputs['param1'].button(initial_port)
            if initial_port_btn:
                initial_port_btn.setChecked(True)
            
            # Update button text when syringe changes
            def update_port_buttons():
                syringe_idx = self.inputs['syringe'].checkedId()
                if syringe_idx < 0:
                    syringe_idx = 0
                for port_num in range(1, 7):
                    btn = self.inputs['param1'].button(port_num)
                    if btn and parent:
                        nickname = ""
                        if (syringe_idx in parent.nickname_inputs and 
                            port_num in parent.nickname_inputs[syringe_idx]):
                            nickname = parent.nickname_inputs[syringe_idx][port_num].text()
                        btn.setText(f"{port_num}\n{nickname}" if nickname else str(port_num))
            
            # Store the update function for use in showEvent
            self._update_port_buttons = update_port_buttons
            
            # Connect syringe buttons to update port button labels
            for button in self.inputs['syringe'].buttons():
                button.toggled.connect(update_port_buttons)
            
            # Initial update of port button labels
            update_port_buttons()
            
            self.layout.addRow("To Port:", port_container)

        elif action_type == "Prime Reagent":
            # Create port selection buttons (6 side-by-side buttons) - only reagent ports enabled
            port_container = QWidget()
            port_layout = QHBoxLayout(port_container)
            port_layout.setContentsMargins(0, 0, 0, 0)
            
            self.inputs['param1'] = QButtonGroup(port_container)
            
            # Style buttons: gray when unchecked, blue when checked, dark gray when disabled
            port_button_style = """
                QPushButton {
                    background-color: #e0e0e0;
                    color: #333;
                    border: 2px solid #a0a0a0;
                    border-radius: 5px;
                    padding: 8px 12px;
                    font-weight: bold;
                    min-width: 80px;
                }
                QPushButton:checked {
                    background-color: #0078d7;
                    color: white;
                    border: 2px solid #005a9e;
                }
                QPushButton:hover {
                    background-color: #d0d0d0;
                }
                QPushButton:checked:hover {
                    background-color: #005a9e;
                }
                QPushButton:disabled {
                    background-color: #505050;
                    color: #999;
                    border: 2px solid #404040;
                }
            """
            
            # Get initial port from last valve position
            initial_syringe_idx = 0
            initial_port = self.last_valve_positions.get(initial_syringe_idx) or 1
            
            # Create 6 port buttons
            for port_num in range(1, 7):
                port_btn = QPushButton()
                port_btn.setCheckable(True)
                port_btn.setStyleSheet(port_button_style)
                self.inputs['param1'].addButton(port_btn, port_num)
                port_layout.addWidget(port_btn)
            
            # Update button text when syringe changes
            def update_port_buttons():
                syringe_idx = self.inputs['syringe'].checkedId()
                if syringe_idx < 0:
                    syringe_idx = 0
                
                # Check if "Both" is selected
                is_both = (syringe_idx == 2)
                
                for port_num in range(1, 7):
                    btn = self.inputs['param1'].button(port_num)
                    if btn and parent:
                        if is_both:
                            # For "Both", check if both syringes have same nickname for this port
                            nickname1 = ""
                            nickname2 = ""
                            if (0 in parent.nickname_inputs and 
                                port_num in parent.nickname_inputs[0]):
                                nickname1 = parent.nickname_inputs[0][port_num].text()
                            if (1 in parent.nickname_inputs and 
                                port_num in parent.nickname_inputs[1]):
                                nickname2 = parent.nickname_inputs[1][port_num].text()
                            
                            # Only enable if nicknames match and both are reagent ports with valid rates
                            nicknames_match = (nickname1 == nickname2)
                            port_type1 = parent.port_type_combos[0][port_num].currentText() if port_num in parent.port_type_combos[0] else ""
                            port_type2 = parent.port_type_combos[1][port_num].currentText() if port_num in parent.port_type_combos[1] else ""
                            both_reagent = (port_type1 == 'Reagent' and port_type2 == 'Reagent')
                            
                            draw_rate_str1 = parent.speed_inputs[0]['Reagent']['Draw'].text()
                            draw_rate_str2 = parent.speed_inputs[1]['Reagent']['Draw'].text()
                            try:
                                draw_rate1 = float(draw_rate_str1) if draw_rate_str1 else 0.0
                                draw_rate2 = float(draw_rate_str2) if draw_rate_str2 else 0.0
                                both_valid_rates = (draw_rate1 > 0 and draw_rate2 > 0)
                                
                                if nicknames_match and both_reagent and both_valid_rates:
                                    btn.setEnabled(True)
                                    btn.setText(f"{port_num}\n{nickname1}" if nickname1 else str(port_num))
                                else:
                                    btn.setEnabled(False)
                                    btn.setText(f"{port_num}\n(n/a)" if nickname1 or nickname2 else str(port_num))
                                    if btn.isChecked():
                                        # Find first enabled port
                                        for p in range(1, 7):
                                            other_btn = self.inputs['param1'].button(p)
                                            if other_btn and other_btn.isEnabled():
                                                other_btn.setChecked(True)
                                                break
                            except (ValueError, TypeError):
                                btn.setEnabled(False)
                        else:
                            # Single syringe selection
                            nickname = ""
                            if (syringe_idx in parent.nickname_inputs and 
                                port_num in parent.nickname_inputs[syringe_idx]):
                                nickname = parent.nickname_inputs[syringe_idx][port_num].text()
                            btn.setText(f"{port_num}\n{nickname}" if nickname else str(port_num))
                            
                            # Check if port is reagent with valid draw rate
                            port_type = parent.port_type_combos[syringe_idx][port_num].currentText()
                            draw_rate_str = parent.speed_inputs[syringe_idx]['Reagent']['Draw'].text()
                            try:
                                draw_rate = float(draw_rate_str) if draw_rate_str else 0.0
                                if port_type == 'Reagent' and draw_rate > 0:
                                    btn.setEnabled(True)
                                else:
                                    btn.setEnabled(False)
                                    if btn.isChecked():
                                        # Find first enabled port
                                        for p in range(1, 7):
                                            other_btn = self.inputs['param1'].button(p)
                                            if other_btn and other_btn.isEnabled():
                                                other_btn.setChecked(True)
                                                break
                            except (ValueError, TypeError):
                                btn.setEnabled(False)
            
            # Store the update function for use in showEvent
            self._update_port_buttons = update_port_buttons
            
            # Connect syringe buttons to update port button labels
            for button in self.inputs['syringe'].buttons():
                button.toggled.connect(update_port_buttons)
            
            # Set initial port button as checked (only if enabled)
            initial_port_btn = self.inputs['param1'].button(initial_port)
            if initial_port_btn and parent:
                # Check if "Both" is selected initially (shouldn't be, but handle it)
                syringe_idx = self.inputs['syringe'].checkedId() if 'syringe' in self.inputs else initial_syringe_idx
                is_both = (syringe_idx == 2)
                
                if is_both:
                    # For "Both", check if port is valid for both syringes
                    nickname1 = ""
                    nickname2 = ""
                    if (0 in parent.nickname_inputs and initial_port in parent.nickname_inputs[0]):
                        nickname1 = parent.nickname_inputs[0][initial_port].text()
                    if (1 in parent.nickname_inputs and initial_port in parent.nickname_inputs[1]):
                        nickname2 = parent.nickname_inputs[1][initial_port].text()
                    
                    nicknames_match = (nickname1 == nickname2)
                    port_type1 = parent.port_type_combos[0][initial_port].currentText() if initial_port in parent.port_type_combos[0] else ""
                    port_type2 = parent.port_type_combos[1][initial_port].currentText() if initial_port in parent.port_type_combos[1] else ""
                    both_reagent = (port_type1 == 'Reagent' and port_type2 == 'Reagent')
                    
                    draw_rate_str1 = parent.speed_inputs[0]['Reagent']['Draw'].text()
                    draw_rate_str2 = parent.speed_inputs[1]['Reagent']['Draw'].text()
                    try:
                        draw_rate1 = float(draw_rate_str1) if draw_rate_str1 else 0.0
                        draw_rate2 = float(draw_rate_str2) if draw_rate_str2 else 0.0
                        both_valid_rates = (draw_rate1 > 0 and draw_rate2 > 0)
                        
                        if nicknames_match and both_reagent and both_valid_rates:
                            initial_port_btn.setChecked(True)
                        else:
                            # Find first enabled port
                            for p in range(1, 7):
                                check_btn = self.inputs['param1'].button(p)
                                if check_btn and check_btn.isEnabled():
                                    check_btn.setChecked(True)
                                    break
                    except (ValueError, TypeError):
                        # Find first enabled port
                        for p in range(1, 7):
                            check_btn = self.inputs['param1'].button(p)
                            if check_btn and check_btn.isEnabled():
                                check_btn.setChecked(True)
                                break
                else:
                    # Single syringe selection
                    port_type = parent.port_type_combos[initial_syringe_idx][initial_port].currentText()
                    draw_rate_str = parent.speed_inputs[initial_syringe_idx]['Reagent']['Draw'].text()
                    try:
                        draw_rate = float(draw_rate_str) if draw_rate_str else 0.0
                        if port_type == 'Reagent' and draw_rate > 0:
                            initial_port_btn.setChecked(True)
                        else:
                            # Find first enabled port
                            for p in range(1, 7):
                                check_btn = self.inputs['param1'].button(p)
                                if check_btn:
                                    check_type = parent.port_type_combos[initial_syringe_idx][p].currentText()
                                    check_rate_str = parent.speed_inputs[initial_syringe_idx]['Reagent']['Draw'].text()
                                    try:
                                        check_rate = float(check_rate_str) if check_rate_str else 0.0
                                        if check_type == 'Reagent' and check_rate > 0:
                                            check_btn.setChecked(True)
                                            break
                                    except (ValueError, TypeError):
                                        pass
                    except (ValueError, TypeError):
                        pass
            
            # Initial update of port button labels
            update_port_buttons()
            
            self.layout.addRow("Reagent Port:", port_container)

        elif action_type == "Wait":
            # Duration control using custom increment control
            if parent and hasattr(parent, '_create_increment_control'):
                self.inputs['param1'] = parent._create_increment_control(
                    min_val=0.1, max_val=3600.0, initial_val=5.0, 
                    step=0.5, suffix="", is_int=False
                )
            else:
                # Fallback to QDoubleSpinBox if parent not available
                self.inputs['param1'] = QDoubleSpinBox()
                self.inputs['param1'].setRange(0.1, 3600)
                self.inputs['param1'].setValue(5.0)
            self.layout.addRow("Duration (s):", self.inputs['param1'])

        # Create selection buttons for Stepping instead of dropdown
        step_container = QWidget()
        step_layout = QHBoxLayout(step_container)
        step_layout.setContentsMargins(0, 0, 0, 0)
        
        self.inputs['new_step'] = QButtonGroup(step_container)
        current_step_btn = QPushButton("Add to Current Step")
        new_step_btn = QPushButton("Add as New Step")
        current_step_btn.setCheckable(True)
        new_step_btn.setCheckable(True)
        new_step_btn.setChecked(True)  # Default to "Add as New Step"
        
        # Style buttons: gray when unchecked, blue when checked
        step_button_style = """
            QPushButton {
                background-color: #e0e0e0;
                color: #333;
                border: 2px solid #a0a0a0;
                border-radius: 5px;
                padding: 8px 20px;
                font-weight: bold;
                min-width: 150px;
            }
            QPushButton:checked {
                background-color: #0078d7;
                color: white;
                border: 2px solid #005a9e;
            }
            QPushButton:hover {
                background-color: #d0d0d0;
            }
            QPushButton:checked:hover {
                background-color: #005a9e;
            }
        """
        current_step_btn.setStyleSheet(step_button_style)
        new_step_btn.setStyleSheet(step_button_style)
        
        self.inputs['new_step'].addButton(current_step_btn, 0)
        self.inputs['new_step'].addButton(new_step_btn, 1)
        step_layout.addWidget(current_step_btn)
        step_layout.addWidget(new_step_btn)
        step_layout.addStretch()
        
        self.layout.addRow("Stepping:", step_container)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addWidget(self.button_box)

    def showEvent(self, event):
        """Override showEvent to update port buttons when dialog is shown."""
        super().showEvent(event)
        # Update port button labels when dialog is shown
        if hasattr(self, '_update_port_buttons'):
            self._update_port_buttons()

    def _update_port_nickname_display(self):
        """Updates the port button labels based on selected syringe and port."""
        # This method is now handled by update_port_buttons functions in Draw/Dispense and Move Valve sections
        # Keeping for backward compatibility but it's no longer needed
        pass
    
    def _update_rate_from_port(self):
        """Updates the rate field based on selected syringe, port, and action."""
        if self.action_type not in ["Draw", "Dispense"]:
            return
        if 'param3' not in self.inputs or not self.parent_window:
            return
        
        try:
            # Get selected syringe index
            syringe_idx = self.inputs['syringe'].checkedId()
            if syringe_idx < 0:
                syringe_idx = 0  # Default to S1
            
            # Get selected port number from button group
            if isinstance(self.inputs['param1'], QButtonGroup):
                port_num = self.inputs['param1'].checkedId()
                if port_num < 0:
                    port_num = 1  # Default to port 1
            elif hasattr(self.inputs['param1'], 'get_value'):
                port_num = int(self.inputs['param1'].get_value())
            else:
                port_num = self.inputs['param1'].value()
            
            # Get default rate
            is_draw = self.action_type == "Draw"
            default_rate = self._get_default_rate(syringe_idx, port_num, is_draw)
            
            # Update rate field
            if hasattr(self.inputs['param3'], 'set_value'):
                self.inputs['param3'].set_value(default_rate)
            elif hasattr(self.inputs['param3'], 'setValue'):
                self.inputs['param3'].setValue(default_rate)
        except (AttributeError, ValueError, KeyError, TypeError):
            pass
    
    def _get_default_rate(self, syringe_idx, port_num, is_draw):
        """Helper to get default rate based on syringe, port, and action."""
        if not self.parent_window:
            return 100.0
        
        # Get port type for the selected port
        if port_num in self.parent_window.port_type_combos[syringe_idx]:
            port_type = self.parent_window.port_type_combos[syringe_idx][port_num].currentText()
            # Get rate from speed_inputs
            if port_type in self.parent_window.speed_inputs[syringe_idx]:
                rate_key = 'Draw' if is_draw else 'Dispense'
                rate_str = self.parent_window.speed_inputs[syringe_idx][port_type][rate_key].text()
                try:
                    rate_val = float(rate_str) if rate_str else None
                    if rate_val is not None:
                        return rate_val
                except ValueError:
                    pass
        
        # Fallback default
        return 100.0

    def get_data(self):
        data = {'action': self.action_type}
        # Get new_step from button group (1 = "Add as New Step", 0 = "Add to Current Step")
        data['new_step'] = self.inputs['new_step'].checkedId() == 1

        if 'syringe' in self.inputs:
            syringe_idx = self.inputs['syringe'].checkedId()
            if syringe_idx < 0:
                syringe_idx = 0  # Default to S1 if nothing selected
            data['syringe'] = syringe_idx
            if syringe_idx == 2:  # "Both" selected
                data['syringe_text'] = 'Both'  # Will be split into S1 and S2 in _add_routine_action
            else:
                data['syringe_text'] = f"S{syringe_idx + 1}"

        if 'param1' in self.inputs:
            # Handle button group (for port selection) or other controls
            if isinstance(self.inputs['param1'], QButtonGroup):
                port_id = self.inputs['param1'].checkedId()
                data['param1'] = port_id if port_id >= 0 else 1  # Default to port 1 if none selected
            elif hasattr(self.inputs['param1'], 'get_value'):
                data['param1'] = self.inputs['param1'].get_value()
            else:
                data['param1'] = self.inputs['param1'].value()

        if 'param2' in self.inputs:
            # Use get_value() for custom increment controls, value() for other controls
            if hasattr(self.inputs['param2'], 'get_value'):
                data['param2'] = self.inputs['param2'].get_value()
            else:
                data['param2'] = self.inputs['param2'].value()
        
        if 'param3' in self.inputs:
            # Use get_value() for custom increment controls, value() for other controls
            if hasattr(self.inputs['param3'], 'get_value'):
                data['param3'] = self.inputs['param3'].get_value()
            else:
                data['param3'] = self.inputs['param3'].value()
            
        return data
class FinalCleanProgressDialog(QDialog):
    """Dialog to track the progress of the final clean sequence and frequency analysis."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preparing for Shutdown")
        self.setFixedWidth(450)
        self.setModal(True)
        # Prevent closing with X button until finished
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        if not parent:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(20)
        
        # Header
        header = QLabel("System Shutdown in Progress")
        header.setStyleSheet("font-size: 16pt; font-weight: bold; color: #1a73e8;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)
        
        # Fluidic Progress
        self.fluidic_group = QGroupBox("Fluidic Cleaning Sequence")
        self.fluidic_group.setStyleSheet("font-weight: bold;")
        fluidic_layout = QVBoxLayout(self.fluidic_group)
        self.fluidic_status_label = QLabel("Waiting to start...")
        self.fluidic_status_label.setStyleSheet("font-weight: normal; color: #555;")
        fluidic_layout.addWidget(self.fluidic_status_label)
        
        self._progress_bar_blue_style = """
            QProgressBar {
                border: 1px solid #ccc;
                border-radius: 5px;
                text-align: center;
                height: 25px;
            }
            QProgressBar::chunk {
                background-color: #4285f4;
                border-radius: 4px;
            }
        """
        self._progress_bar_green_style = """
            QProgressBar {
                border: 1px solid #ccc;
                border-radius: 5px;
                text-align: center;
                height: 25px;
            }
            QProgressBar::chunk {
                background-color: #34a853;
                border-radius: 4px;
            }
        """
        
        self.fluidic_progress_bar = QProgressBar()
        self.fluidic_progress_bar.setRange(0, 100)
        self.fluidic_progress_bar.setValue(0)
        self.fluidic_progress_bar.setFormat("%p%")
        self.fluidic_progress_bar.setStyleSheet(self._progress_bar_blue_style)
        fluidic_layout.addWidget(self.fluidic_progress_bar)
        layout.addWidget(self.fluidic_group)
        
        # Frequency Analysis Progress
        self.analysis_group = QGroupBox("Fast Batch Frequency Analysis")
        self.analysis_group.setStyleSheet("font-weight: bold;")
        analysis_layout = QVBoxLayout(self.analysis_group)
        self.analysis_status_label = QLabel("Waiting for data save...")
        self.analysis_status_label.setStyleSheet("font-weight: normal; color: #555;")
        analysis_layout.addWidget(self.analysis_status_label)
        
        self.analysis_progress_bar = QProgressBar()
        self.analysis_progress_bar.setRange(0, 100)
        self.analysis_progress_bar.setValue(0)
        self.analysis_progress_bar.setStyleSheet(self._progress_bar_blue_style)
        analysis_layout.addWidget(self.analysis_progress_bar)
        layout.addWidget(self.analysis_group)
        
        # Instructions / Final Note
        self.note_label = QLabel("Real-time analysis is finishing and spawning post-hoc generation...")
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet("font-style: italic; color: #777; font-size: 9pt;")
        layout.addWidget(self.note_label)
        
        # Close Button (initially disabled)
        self.close_btn = QPushButton("Complete shutdown and close program")
        self.close_btn.setEnabled(False)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: #e0e0e0;
                color: #888;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
            }
        """)
        self.close_btn.clicked.connect(self._on_shutdown_clicked)
        layout.addWidget(self.close_btn)
        
        self.fluidic_done = False
        self.analysis_done = False

    def _on_shutdown_clicked(self):
        """Close the dialog and quit the application."""
        self.accept()
        QApplication.instance().quit()

    def update_fluidic_status(self, progress_percent, message):
        self.fluidic_progress_bar.setValue(int(progress_percent))
        if message:
            self.fluidic_status_label.setText(message)
        if progress_percent >= 100:
            self.fluidic_progress_bar.setStyleSheet(self._progress_bar_green_style)
            self.fluidic_done = True
            self._check_completion()

    @Slot(int, str)
    def update_analysis_status(self, value, message):
        self.analysis_progress_bar.setValue(value)
        if message:
            self.analysis_status_label.setText(message)
        if value >= 100:
            self.analysis_progress_bar.setStyleSheet(self._progress_bar_green_style)
            self.analysis_done = True
            self._check_completion()

    def _check_completion(self):
        if self.fluidic_done and self.analysis_done:
            self.close_btn.setEnabled(True)
            self.close_btn.setStyleSheet("""
                QPushButton {
                    background-color: #34a853;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 10px 20px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #2d9147;
                }
            """)

class AutoCleanProgressDialog(QDialog):
    """Modeless dialog to track Auto Clean progress and summarize the previous run."""
    def __init__(self, sample_name, peaks, volume, images, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Auto Clean in Progress")
        self.setFixedWidth(400)
        # Modeless behavior
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Header
        self.header = QLabel("Auto Clean in Progress")
        self.header.setStyleSheet("font-size: 14pt; font-weight: bold; color: #1a73e8;")
        self.header.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.header)
        
        # Run Summary Box
        summary_group = QGroupBox("Run Summary")
        summary_group.setStyleSheet("font-weight: bold;")
        summary_layout = QFormLayout(summary_group)
        summary_layout.setLabelAlignment(Qt.AlignRight)
        
        # Style for values
        val_style = "font-weight: normal; color: #333;"
        
        sample_label = QLabel(str(sample_name))
        sample_label.setStyleSheet(val_style)
        summary_layout.addRow("Sample:", sample_label)
        
        peaks_label = QLabel(f"{peaks:,}")
        peaks_label.setStyleSheet(val_style)
        summary_layout.addRow("Detected Peaks:", peaks_label)
        
        vol_label = QLabel(f"{volume:.1f} µL")
        vol_label.setStyleSheet(val_style)
        summary_layout.addRow("Volume Aspirated:", vol_label)
        
        img_label = QLabel(str(images))
        img_label.setStyleSheet(val_style)
        summary_layout.addRow("Images Saved:", img_label)
        
        layout.addWidget(summary_group)
        
        # Progress Section
        progress_layout = QVBoxLayout()
        self.status_label = QLabel("Initializing clean routine...")
        self.status_label.setStyleSheet("font-weight: normal; color: #555;")
        progress_layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ccc;
                border-radius: 5px;
                text-align: center;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #4285f4;
                border-radius: 4px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)
        layout.addLayout(progress_layout)
        
        # Close Button (initially hidden/disabled)
        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.setVisible(False)
        self.close_btn.clicked.connect(self.accept)
        layout.addWidget(self.close_btn)

    @Slot(int)
    def update_progress(self, value):
        self.progress_bar.setValue(value)
        if value >= 100:
            self.on_finished()

    @Slot(str)
    def update_status(self, message):
        if message:
            self.status_label.setText(message)

    def on_finished(self):
        """Update UI to reflect completion."""
        self.header.setText("System Ready")
        self.header.setStyleSheet("font-size: 14pt; font-weight: bold; color: #34a853;")
        self.status_label.setText("Cleaning complete. Ready for next sample.")
        self.status_label.setStyleSheet("font-weight: bold; color: #34a853;")
        self.progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #34a853; }")
        
        self.close_btn.setVisible(True)
        self.close_btn.setEnabled(True)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: #34a853;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-weight: bold;
            }
        """)

class CleanShutdownDialog(QDialog):
    """Dialog shown after Final Clean sequence is complete."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Clean - Shutdown Ready")
        self.setModal(True)
        self.setMinimumWidth(450)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)
        
        # Message
        message = QLabel(
            "System is now clean and ready to shutdown. Please replace the sample vial "
            "with a clean vial and replace the 15mL media tube with a fresh tube with clean water"
        )
        message.setWordWrap(True)
        message.setStyleSheet("font-size: 11pt; color: #333;")
        layout.addWidget(message)
        
        # Buttons layout
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(15)
        
        # Finish shutdown button (Red)
        self.shutdown_btn = QPushButton("Finish shutdown")
        self.shutdown_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #d32f2f;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 11pt;
            }}
            QPushButton:hover {{
                background-color: #b71c1c;
            }}
        """)
        self.shutdown_btn.clicked.connect(self._on_shutdown)
        
        # Keep software open button (Dark Gray)
        self.keep_open_btn = QPushButton("Keep software open")
        self.keep_open_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #424242;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 11pt;
            }}
            QPushButton:hover {{
                background-color: #212121;
            }}
        """)
        self.keep_open_btn.clicked.connect(self.accept)
        
        buttons_layout.addWidget(self.shutdown_btn)
        buttons_layout.addWidget(self.keep_open_btn)
        layout.addLayout(buttons_layout)
        
    def _on_shutdown(self):
        """Quit the application."""
        QApplication.instance().quit()

# Standalone window wrapper for backward compatibility
class MainWindow(QMainWindow):
    """Standalone window wrapper for SyringeControlWidget."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hamilton PSD/4 Syringe Pump Control")
        self.setWindowIcon(QIcon(APP_ICON_PATH))
        self.setGeometry(100, 100, 800, 800)
        self.syringe_control = SyringeControlWidget()
        self.setCentralWidget(self.syringe_control)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(APP_ICON_PATH))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
