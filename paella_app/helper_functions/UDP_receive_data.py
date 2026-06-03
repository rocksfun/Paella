"""
UDP Data Reception and Parsing Module.

This module provides functionality to receive data from a UDP multicast connection
and parse it into frequency data. Can be used standalone with a GUI or imported
as a module to return parsed data.

Note: UDPReceiverWidget and MainWindow are standalone GUI components for testing/debugging.
For production use, use UDPDataManager from UDP_data_manager.py instead.
"""

import sys
import socket
import select
import struct
import threading
import time
from typing import Optional, Tuple, List, Union
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QGroupBox, QFormLayout
)
from PySide6.QtCore import Qt, QTimer, QObject, Signal
import numpy as np

# Windows-specific imports and setup
_WINDOWS = sys.platform == 'win32'
_WINDOWS_CTYPES_AVAILABLE = False
_windows_perf_freq_value = None
_qpc_offset = 0.0

if _WINDOWS:
    try:
        import ctypes
        _WINDOWS_CTYPES_AVAILABLE = True
        
        # Set Windows timer resolution to 1ms for better select() precision
        try:
            winmm = ctypes.WinDLL('winmm')
            winmm.timeBeginPeriod(1)  # Set global timer resolution to 1ms
        except Exception:
            pass
        
        # Get QueryPerformanceFrequency for QPC timestamp conversion
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            freq = ctypes.c_int64()
            if kernel32.QueryPerformanceFrequency(ctypes.byref(freq)):
                _windows_perf_freq_value = freq.value
        except Exception:
            pass
    except ImportError:
        pass


def _calibrate_qpc_offset():
    """Synchronize Windows hardware timestamp with Python perf_counter."""
    global _qpc_offset
    if _WINDOWS and _WINDOWS_CTYPES_AVAILABLE and _windows_perf_freq_value:
        # Take multiple samples to minimize jitter influence
        diffs = []
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            for _ in range(10):
                t_py = time.perf_counter()
                qpc = ctypes.c_int64()
                kernel32.QueryPerformanceCounter(ctypes.byref(qpc))
                t_win = qpc.value / _windows_perf_freq_value
                diffs.append(t_py - t_win)
            _qpc_offset = sum(diffs) / len(diffs)
        except Exception:
            _qpc_offset = 0.0


# Run calibration once on module load
if _WINDOWS and _WINDOWS_CTYPES_AVAILABLE:
    _calibrate_qpc_offset()


def parse_udp_data(raw_data: bytes) -> Optional[List[float]]:
    """
    Parse UDP data stream into frequency data.
    
    Process:
    1. Unpack bytes to I32 array (Little Endian)
    2. Delete first entry
    3. Apply scaling factor: 12.5e6 / (2^32)
    
    Formula: frequency = I32_value * 12.5e6 / (2^32)
    
    Args:
        raw_data: Raw bytes received from UDP socket
        
    Returns:
        List of parsed frequency values (Hz) or None if parsing fails
    """
    if not raw_data or len(raw_data) < 4:
        return None
    
    try:
        # 1. raw_data is already bytes, no conversion needed
        
        # 2. Unpack bytes to I32 (Little Endian)
        # '<' denotes Little Endian (replaces Swap Bytes + Swap Words), 'i' denotes Signed 32-bit Integer
        count = len(raw_data) // 4
        if count == 0:
            return None
        
        i32_array = struct.unpack(f'<{count}i', raw_data[:count*4])
        
        # 3. Delete first entry and 4. Apply scaling factor
        # Use numpy for faster array operations and reduced memory allocations
        if count > 1:
            # Convert to numpy array (skip first entry) and apply scaling
            scaling_factor = 12.5e6 / (2**32)
            i32_np = np.array(i32_array[1:], dtype=np.int32)
            processed_values = (i32_np * scaling_factor).tolist()
        else:
            processed_values = []
        
        return processed_values
        
    except (struct.error, ValueError, IndexError) as e:
        # Parsing failed
        return None


# Cache for socket timeouts to avoid repeated gettimeout() calls
_socket_timeout_cache = {}

# Cache for SO_TIMESTAMP enablement to avoid repeated setsockopt calls
_socket_timestamp_enabled = set()

# Cache for non-blocking mode to avoid repeated setblocking calls
_socket_nonblocking_enabled = set()


def receive_udp_data(
    udp_socket: socket.socket,
    buffer_size: int = 4096,
    timeout: Optional[float] = None,
    capture_timestamp: bool = False,
    use_kernel_timestamp: bool = True,
    use_nonblocking: bool = False,
    select_timeout: Optional[float] = None
) -> Optional[Tuple[bytes, Tuple[str, int], Optional[float]]]:
    """
    Receive data from a UDP socket with optional timestamp capture.
    
    Uses SO_TIMESTAMP for kernel-level timestamps when available, which provides
    the most accurate timestamp of when the packet actually arrived at the network
    interface, independent of processing delays.
    
    When use_nonblocking=True, the socket is set to non-blocking mode and select()
    is used to efficiently wait for data. This eliminates timeout delays and is
    ideal for high-frequency packet streams (e.g., 6.4ms packet intervals).
    
    Args:
        udp_socket: Open UDP socket to receive from
        buffer_size: Maximum number of bytes to receive
        timeout: Socket timeout in seconds (None for blocking, ignored if use_nonblocking=True)
        capture_timestamp: If True, capture timestamp
        use_kernel_timestamp: If True, attempt to use SO_TIMESTAMP (kernel-level).
                              Falls back to time.perf_counter() if not available.
        use_nonblocking: If True, use non-blocking mode with select() for efficient waiting.
                         This eliminates timeout delays and is recommended for high-frequency streams.
        select_timeout: Timeout for select() call when use_nonblocking=True (None = no timeout).
                        Recommended: 0.001 (1ms) or None for immediate return.
        
    Returns:
        Tuple of (data_bytes, (source_address, source_port), timestamp) or None if timeout/error
        timestamp is None if capture_timestamp is False, otherwise:
        - Kernel timestamp (seconds since epoch) if SO_TIMESTAMP is available
        - time.perf_counter() value as fallback
    """
    if udp_socket is None:
        return None
    
    try:
        socket_id = id(udp_socket)
        
        # Handle non-blocking mode
        if use_nonblocking:
            # Enable non-blocking mode (cache to avoid repeated calls)
            if socket_id not in _socket_nonblocking_enabled:
                udp_socket.setblocking(False)
                _socket_nonblocking_enabled.add(socket_id)
            
            # Use select() to efficiently wait for data without busy-waiting
            # This eliminates timeout delays and provides immediate response when data arrives
            ready, _, _ = select.select([udp_socket], [], [], select_timeout)
            if not ready:
                # No data available, return None immediately (no timeout delay)
                return None
        else:
            # Traditional blocking mode with timeout
            if timeout is not None:
                # Check cache first
                if socket_id not in _socket_timeout_cache or _socket_timeout_cache[socket_id] != timeout:
                    udp_socket.settimeout(timeout)
                    _socket_timeout_cache[socket_id] = timeout
        
        # Enable SO_TIMESTAMP for kernel-level timestamps if requested
        use_kernel_ts = capture_timestamp and use_kernel_timestamp
        if use_kernel_ts and socket_id not in _socket_timestamp_enabled:
            try:
                # SO_TIMESTAMP provides kernel-level timestamp (when packet arrived at interface)
                # This is more accurate than time.perf_counter() after recvfrom() returns
                udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_TIMESTAMP, 1)
                _socket_timestamp_enabled.add(socket_id)
            except (AttributeError, OSError):
                # SO_TIMESTAMP not available on this system, will use perf_counter fallback
                use_kernel_ts = False
        
        # Try to use recvmsg() for kernel timestamps, fall back to recvfrom()
        timestamp = None
        if use_kernel_ts:
            try:
                # Check if recvmsg() is available (Python 3.3+)
                if not hasattr(udp_socket, 'recvmsg'):
                    use_kernel_ts = False
                else:
                    # recvmsg() allows us to receive ancillary data including timestamps
                    # Format: (data, ancdata, flags, addr)
                    # ancdata contains control messages including timestamps
                    data, ancdata, flags, addr = udp_socket.recvmsg(buffer_size)
                    
                    # Extract timestamp from ancillary data
                    if ancdata:
                        for cmsg_level, cmsg_type, cmsg_data in ancdata:
                            # SO_TIMESTAMP returns timeval structure on Linux/macOS
                            # On Windows, it may return QPC (QueryPerformanceCounter) values
                            if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SO_TIMESTAMP:
                                if _WINDOWS and _WINDOWS_CTYPES_AVAILABLE and _windows_perf_freq_value:
                                    # Windows: Extract QPC value from control message
                                    # QPC is a 64-bit integer that needs to be converted to seconds
                                    if len(cmsg_data) >= 8:
                                        try:
                                            # Unpack QPC value (64-bit unsigned integer, little-endian)
                                            perf_counter = struct.unpack('<Q', cmsg_data[:8])[0]
                                            
                                            # Convert to seconds using QueryPerformanceFrequency
                                            if _windows_perf_freq_value is not None and _windows_perf_freq_value > 0:
                                                # Apply the calibrated offset to align with perf_counter domain
                                                raw_timestamp = perf_counter / _windows_perf_freq_value
                                                timestamp = raw_timestamp + _qpc_offset
                                                break
                                        except (struct.error, ValueError):
                                            # QPC extraction failed, will fall back
                                            pass
                                
                                # Linux/macOS: Extract timeval structure
                                # timeval: (seconds, microseconds) as two signed longs
                                if timestamp is None and len(cmsg_data) >= 8:
                                    try:
                                        # Try native byte order first (most common)
                                        # 'll' = two signed longs (works on most 64-bit systems)
                                        # 'ii' = two signed ints (works on 32-bit or if timeval uses ints)
                                        if len(cmsg_data) >= 16:
                                            # 64-bit system, timeval might be 16 bytes (2 x 64-bit)
                                            sec, usec = struct.unpack('=qq', cmsg_data[:16])
                                        else:
                                            # 32-bit or 8-byte timeval (2 x 32-bit)
                                            sec, usec = struct.unpack('=ll', cmsg_data[:8])
                                        # Convert to seconds (float) for consistency with perf_counter
                                        timestamp = sec + usec / 1_000_000.0
                                        break
                                    except (struct.error, ValueError):
                                        # Try alternative format
                                        try:
                                            sec, usec = struct.unpack('=ii', cmsg_data[:8])
                                            timestamp = sec + usec / 1_000_000.0
                                            break
                                        except (struct.error, ValueError):
                                            # Timestamp extraction failed, will fall back
                                            pass
                    
                    # If we didn't get timestamp from kernel, fall back to perf_counter
                    if timestamp is None:
                        timestamp = time.perf_counter()
            except (AttributeError, OSError, struct.error, ValueError):
                # recvmsg() or timestamp extraction failed, fall back to recvfrom()
                use_kernel_ts = False
        
        # Fall back to recvfrom() if kernel timestamps aren't available
        if not use_kernel_ts:
            data, addr = udp_socket.recvfrom(buffer_size)
            
            # Capture timestamp immediately after recvfrom() returns (before any processing)
            if capture_timestamp and len(data) > 0:
                timestamp = time.perf_counter()
        
        if len(data) > 0:
            return (data, addr, timestamp)
        return None
    except socket.timeout:
        # Timeout is normal - no data available yet (only in blocking mode)
        return None
    except BlockingIOError:
        # Non-blocking mode: no data available immediately
        return None
    except socket.error as e:
        # Socket error - return None but don't print (handled by caller)
        return None
    except Exception as e:
        # Unexpected error
        return None


def read_and_parse_udp(
    udp_socket: socket.socket,
    buffer_size: int = 4096,
    timeout: Optional[float] = None,
    capture_timestamp: bool = False,
    use_kernel_timestamp: bool = True,
    use_nonblocking: bool = False,
    select_timeout: Optional[float] = None
) -> Optional[Tuple[bytes, Optional[List[float]], Tuple[str, int], Optional[float]]]:
    """
    Read data from UDP socket and parse it into frequency.
    
    Args:
        udp_socket: Open UDP socket to receive from
        buffer_size: Maximum number of bytes to receive
        timeout: Socket timeout in seconds (None for blocking, ignored if use_nonblocking=True)
        capture_timestamp: If True, capture timestamp
        use_kernel_timestamp: If True, attempt to use SO_TIMESTAMP for kernel-level timestamps
        use_nonblocking: If True, use non-blocking mode with select() for efficient waiting.
                         Recommended for high-frequency packet streams.
        select_timeout: Timeout for select() call when use_nonblocking=True (None = no timeout).
                        Recommended: 0.001 (1ms) or None for immediate return.
        
    Returns:
        Tuple of (raw_data, parsed_frequencies, (source_address, source_port), timestamp)
        where parsed_frequencies is a list of frequency values (Hz) or None if parsing fails.
        timestamp is None if capture_timestamp is False, otherwise kernel timestamp or time.perf_counter() value.
        Returns None only if receiving data fails.
    """
    result = receive_udp_data(udp_socket, buffer_size, timeout, capture_timestamp, use_kernel_timestamp, use_nonblocking, select_timeout)
    if result is None:
        return None
    
    raw_data, addr, timestamp = result
    parsed_frequencies = parse_udp_data(raw_data)
    
    # Return raw data even if parsing fails (parsed_frequencies will be None)
    return (raw_data, parsed_frequencies, addr, timestamp)


class DataUpdateSignal(QObject):
    """Signal object for thread-safe GUI updates."""
    data_received = Signal(bytes, object, tuple, object)  # raw_data, parsed_freqs (list), addr, timestamp
    status_update = Signal(str)  # status message


class UDPReceiverWidget(QWidget):
    """Widget for receiving and displaying UDP data with GUI."""
    
    def __init__(self, udp_socket: Optional[socket.socket] = None, parent=None):
        super().__init__(parent)
        self.udp_socket = udp_socket
        self.receiving = False
        self.receive_thread = None
        # Create signal object for thread-safe updates
        self.update_signal = DataUpdateSignal()
        # Store first timestamp to calculate relative times
        self._first_timestamp = None
        self.setup_ui()
        # Connect signals after UI is set up (status_label needs to exist)
        self.update_signal.data_received.connect(self._do_update_display)
        self.update_signal.status_update.connect(self.status_label.setText)
    
    def setup_ui(self):
        """Set up the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        title = QLabel("UDP Data Receiver")
        title.setStyleSheet("font-size: 18pt; font-weight: bold; padding: 10px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Control buttons
        button_layout = QHBoxLayout()
        
        self.start_button = QPushButton("Start Receiving")
        self.start_button.clicked.connect(self.start_receiving)
        self.start_button.setStyleSheet("""
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
        """)
        button_layout.addWidget(self.start_button)
        
        self.stop_button = QPushButton("Stop Receiving")
        self.stop_button.clicked.connect(self.stop_receiving)
        self.stop_button.setEnabled(False)
        self.stop_button.setStyleSheet("""
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
        """)
        button_layout.addWidget(self.stop_button)
        
        self.clear_button = QPushButton("Clear Display")
        self.clear_button.clicked.connect(self.clear_display)
        self.clear_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                font-size: 12pt;
                font-weight: bold;
                padding: 10px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #0b7dda;
            }
        """)
        button_layout.addWidget(self.clear_button)
        
        layout.addLayout(button_layout)
        
        # Connection status display
        status_group = QGroupBox("Connection Status")
        status_layout = QVBoxLayout()
        self.connection_status_display = QTextEdit()
        self.connection_status_display.setReadOnly(True)
        self.connection_status_display.setMaximumHeight(80)
        self.connection_status_display.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 5px;
                font-family: 'Courier New', monospace;
                font-size: 10pt;
            }
        """)
        status_layout.addWidget(self.connection_status_display)
        status_group.setLayout(status_layout)
        layout.addWidget(status_group)
        
        # Data display area - two columns
        display_layout = QHBoxLayout()
        
        # Raw data display
        raw_group = QGroupBox("Raw UDP Data")
        raw_layout = QVBoxLayout()
        self.raw_display = QTextEdit()
        self.raw_display.setReadOnly(True)
        self.raw_display.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 5px;
                font-family: 'Courier New', monospace;
                font-size: 10pt;
            }
        """)
        raw_layout.addWidget(self.raw_display)
        raw_group.setLayout(raw_layout)
        display_layout.addWidget(raw_group, 1)
        
        # Parsed data display
        parsed_group = QGroupBox("Parsed Frequency Data")
        parsed_layout = QVBoxLayout()
        self.parsed_display = QTextEdit()
        self.parsed_display.setReadOnly(True)
        self.parsed_display.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 5px;
                font-family: 'Courier New', monospace;
                font-size: 10pt;
            }
        """)
        parsed_layout.addWidget(self.parsed_display)
        parsed_group.setLayout(parsed_layout)
        display_layout.addWidget(parsed_group, 1)
        
        layout.addLayout(display_layout, 1)
        
        # Status label
        self.status_label = QLabel("Status: Not receiving")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 5px;")
        layout.addWidget(self.status_label)
        
        self._setup_styles()
    
    def start_receiving(self):
        """Start receiving UDP data."""
        if self.udp_socket is None:
            self.status_label.setText("Status: Error - No UDP socket available")
            self.raw_display.append("ERROR: No UDP socket available. Please ensure UDP connection is established.\n")
            return
        
        if self.receiving:
            return
        
        # Verify socket is valid
        if not hasattr(self.udp_socket, 'recvfrom'):
            self.status_label.setText("Status: Error - Invalid socket")
            self.raw_display.append("ERROR: Socket is not a valid UDP socket\n")
            return
        
        self.receiving = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText("Status: Receiving data...")
        self.raw_display.append("Starting to receive UDP data...\n")
        self.raw_display.append(f"Socket info: {self.udp_socket}\n")
        try:
            sock_name = self.udp_socket.getsockname()
            self.raw_display.append(f"Socket bound to: {sock_name}\n")
            sock_timeout = self.udp_socket.gettimeout()
            self.raw_display.append(f"Socket timeout: {sock_timeout} seconds\n")
        except Exception as e:
            self.raw_display.append(f"Error getting socket info: {e}\n")
        self.raw_display.append("Waiting for data on socket...\n")
        self.raw_display.append("(If no data appears, check that data is being sent to the multicast address)\n")
        self.raw_display.append("Starting receive thread...\n")
        
        # Test socket before starting thread
        self.raw_display.append("Testing socket with quick receive...\n")
        try:
            test_result = receive_udp_data(self.udp_socket, timeout=0.1)
            if test_result:
                self.raw_display.append(f"Socket test PASSED - received {len(test_result[0])} bytes!\n")
            else:
                self.raw_display.append("Socket test: No data yet (timeout - this is normal)\n")
        except Exception as e:
            self.raw_display.append(f"Socket test ERROR: {e}\n")
            self.status_label.setText(f"Status: Socket error - {e}")
            self.receiving = False
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            return
        
        # Start receiving in a separate thread
        try:
            self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True, name="UDPReceiveThread")
            self.receive_thread.start()
            self.raw_display.append(f"Thread started: {self.receive_thread.is_alive()}\n")
            # Give thread a moment to start
            import time
            time.sleep(0.2)
            if self.receive_thread.is_alive():
                self.raw_display.append(f"Thread confirmed alive: {self.receive_thread.name}\n")
            else:
                self.raw_display.append("WARNING: Thread died immediately!\n")
                self.status_label.setText("Status: Error - Thread died")
                self.receiving = False
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
        except Exception as e:
            self.raw_display.append(f"ERROR starting thread: {e}\n")
            import traceback
            self.raw_display.append(f"Traceback: {traceback.format_exc()}\n")
            self.status_label.setText(f"Status: Error starting receive thread - {e}")
            self.receiving = False
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
    
    def stop_receiving(self):
        """Stop receiving UDP data."""
        self.receiving = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText("Status: Stopped receiving")
        # Reset timestamp reference when stopping
        self._first_timestamp = None
    
    def _receive_loop(self):
        """Main loop for receiving UDP data (runs in separate thread)."""
        packet_count = 0
        timeout_count = 0
        
        # Confirm loop started - update GUI from thread using QTimer
        # Use a proper function to avoid closure issues
        def update_start_msg():
            try:
                self.raw_display.append("Receive loop started...\n")
                self.status_label.setText("Status: Receive loop active, waiting for data...")
            except Exception as e:
                import sys
                print(f"Error updating GUI: {e}", file=sys.stderr, flush=True)
        
        QTimer.singleShot(0, update_start_msg)
        
        # Also print to console for debugging
        import sys
        print("Receive loop thread started", file=sys.stderr, flush=True)
        
        # Force GUI update immediately using a signal-like approach
        # QTimer might not work immediately from thread, so use a small delay
        import time
        time.sleep(0.05)  # Small delay to let thread start
        
        # Update GUI - try multiple methods
        def force_update():
            self.raw_display.append("Receive loop started...\n")
            self.status_label.setText("Status: Receive loop active, waiting for data...")
        
        QTimer.singleShot(100, force_update)  # 100ms delay to ensure it processes
        
        # Immediate test - try one receive to see if socket works
        try:
            test_result = receive_udp_data(self.udp_socket, timeout=0.5)
            if test_result:
                QTimer.singleShot(0, lambda: self.raw_display.append("Socket test: Data received immediately!\n"))
            else:
                QTimer.singleShot(0, lambda: self.raw_display.append("Socket test: No immediate data (this is normal)\n"))
        except Exception as e:
            QTimer.singleShot(0, lambda err=str(e): self.raw_display.append(f"Socket test error: {err}\n"))
        
        while self.receiving:
            try:
                # Use shorter timeout to minimize delay for high-frequency packet streams
                result = read_and_parse_udp(self.udp_socket, timeout=0.01, capture_timestamp=True)
                
                if result is not None:
                    raw_data, parsed_freqs, addr, timestamp = result
                    packet_count += 1
                    
                    # Update GUI in main thread (even if parsing failed)
                    # Throttle updates: only update GUI every N packets to avoid overload
                    if packet_count == 1 or packet_count % 10 == 0:
                        self._update_display(raw_data, parsed_freqs, addr, timestamp)
                    
                    # Update status immediately for first packet, then every 10
                    if packet_count == 1:
                        self.update_signal.status_update.emit(f"Status: Receiving data! ({packet_count} packet)")
                    elif packet_count % 10 == 0:
                        self.update_signal.status_update.emit(f"Status: Receiving... ({packet_count} packets received)")
                    
                    # Debug: print to console (throttled)
                    if packet_count == 1 or packet_count % 100 == 0:
                        import sys
                        print(f"Received packet #{packet_count}: {len(raw_data)} bytes from {addr}", file=sys.stderr, flush=True)
                else:
                    # Timeout occurred - this is normal if no data is being sent
                    timeout_count += 1
                    # Show timeout messages to confirm loop is running
                    if timeout_count == 1:
                        msg = "Waiting for UDP data... (timeouts are normal if no data is being sent)\n"
                        QTimer.singleShot(0, lambda m=msg: self.raw_display.append(m))
                    elif timeout_count == 10:
                        msg = f"Still waiting... ({timeout_count} timeouts - receive loop is active)\n"
                        QTimer.singleShot(0, lambda m=msg: self.raw_display.append(m))
                    elif timeout_count % 50 == 0:
                        msg = f"Still waiting... ({timeout_count} timeouts)\n"
                        QTimer.singleShot(0, lambda m=msg: self.raw_display.append(m))
                
            except Exception as e:
                if self.receiving:
                    # Only show error if we're still supposed to be receiving
                    error_msg = f"Status: Error - {str(e)}"
                    QTimer.singleShot(0, lambda: self.status_label.setText(error_msg))
                    QTimer.singleShot(0, lambda: self.raw_display.append(f"ERROR: {str(e)}\n"))
                break
    
    def _update_display(self, raw_data: bytes, parsed_freqs: Optional[List[float]], addr: Tuple[str, int], timestamp: Optional[float]):
        """Update the display with new data (called from receive thread)."""
        # Use Qt signal for thread-safe GUI update
        self.update_signal.data_received.emit(raw_data, parsed_freqs, addr, timestamp)
    
    def _do_update_display(self, raw_data: bytes, parsed_freqs: Optional[List[float]], addr: Tuple[str, int], timestamp: Optional[float]):
        """Actually update the display (runs in main thread via signal)."""
        try:
            # Format timestamp
            if timestamp is not None:
                if self._first_timestamp is None:
                    self._first_timestamp = timestamp
                    relative_time = 0.0
                else:
                    relative_time = timestamp - self._first_timestamp
                timestamp_str = f"t={relative_time:.6f}s"
            else:
                timestamp_str = "t=N/A"
            
            # Format raw data with timestamp
            hex_data = ' '.join(f'{b:02X}' for b in raw_data)
            raw_text = f"[{timestamp_str}] [{addr[0]}:{addr[1]}] {hex_data}\n"
            
            # Format parsed frequencies (or show error if parsing failed)
            if parsed_freqs is not None and len(parsed_freqs) > 0:
                # Display all frequencies, one per line
                freq_lines = [f"Frequency {i+1}: {freq:.6f} Hz" for i, freq in enumerate(parsed_freqs)]
                parsed_text = f"[{timestamp_str}]\n" + '\n'.join(freq_lines) + '\n'
            else:
                parsed_text = f"[{timestamp_str}] Parse failed - Raw bytes: {len(raw_data)} bytes\n"
            
            # Append to displays
            self.raw_display.append(raw_text)
            self.parsed_display.append(parsed_text)
            
            # Auto-scroll to bottom
            self.raw_display.verticalScrollBar().setValue(
                self.raw_display.verticalScrollBar().maximum()
            )
            self.parsed_display.verticalScrollBar().setValue(
                self.parsed_display.verticalScrollBar().maximum()
            )
        except Exception as e:
            import sys
            print(f"Error updating GUI display: {e}", file=sys.stderr, flush=True)
    
    def clear_display(self):
        """Clear both display areas."""
        self.raw_display.clear()
        self.parsed_display.clear()
        # Reset timestamp reference when clearing
        self._first_timestamp = None
    
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
    
    def set_udp_socket(self, udp_socket: socket.socket, multicast_ip: str = "", udp_port: int = 0, host_ip: str = ""):
        """Set the UDP socket to receive from."""
        if self.receiving:
            self.stop_receiving()
        self.udp_socket = udp_socket
        
        # Update connection status display
        if udp_socket is not None:
            self.connection_status_display.setText(
                f"UDP multicast connection successful!\r\n"
                f"Multicast source: {multicast_ip}:{udp_port}\r\n"
                f"Interface: {host_ip}\r\n"
                f"Ready to receive data..."
            )
        else:
            self.connection_status_display.setText("No UDP socket available.")


class MainWindow(QMainWindow):
    """Standalone window wrapper for UDPReceiverWidget."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UDP Data Receiver")
        self.setGeometry(100, 100, 1000, 700)
        
        # Create a UDP socket for standalone mode
        # This is a placeholder - in real usage, the socket should be created
        # with proper multicast configuration
        udp_socket = None
        multicast_ip = "224.1.1.1"
        host_ip = "192.168.100.1"
        udp_port = 5007
        error_msg = "UDP connection failed: Unknown error"
        
        try:
            # Use shared socket creation function from UDP_data_manager
            from helper_functions.UDP_data_manager import create_udp_multicast_socket
            
            sock = create_udp_multicast_socket(
                multicast_ip=multicast_ip,
                host_ip=host_ip,
                udp_port=udp_port,
                timeout=1.0
            )
            udp_socket = sock
            
            print(f"UDP socket created successfully:")
            print(f"  Bound to: {sock.getsockname()}")
            print(f"  Multicast group: {multicast_ip}")
            print(f"  Port: {udp_port}")
            print(f"  Interface: {host_ip}")
        except socket.error as e:
            error_msg = f"UDP connection failed (socket error): {str(e)}"
            print(f"Warning: {error_msg}")
            print("Please ensure UDP socket is configured properly.")
        except Exception as e:
            error_msg = f"UDP connection failed: {str(e)}"
            print(f"Warning: {error_msg}")
            print("Please ensure UDP socket is configured properly.")
        
        self.receiver_widget = UDPReceiverWidget(udp_socket)
        # Set the socket with connection info for status display
        if udp_socket is not None:
            self.receiver_widget.set_udp_socket(udp_socket, multicast_ip, udp_port, host_ip)
        else:
            # Show error message in connection status
            self.receiver_widget.connection_status_display.setText(error_msg)
        self.setCentralWidget(self.receiver_widget)


def main():
    """Main entry point for standalone execution."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

