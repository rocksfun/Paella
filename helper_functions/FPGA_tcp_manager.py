"""
FPGA TCP Command Queue Manager.

This module provides a thread-safe command queue system for sending commands
to the FPGA via TCP. It uses a Producer-Consumer pattern with a QThread-based
worker to serialize all commands and eliminate conflicts when multiple threads
try to access the same TCP socket.
"""

import queue
import socket
import select
import time
import threading
from dataclasses import dataclass
from typing import Optional, Tuple
from concurrent.futures import Future, ThreadPoolExecutor

from PySide6.QtCore import QObject, QThread, Signal

from helper_functions.FPGA_connect import initiate_fpga_connection


@dataclass
class FPGACommand:
    """Command object for FPGA TCP communication."""
    command: str
    wait_response: bool = True
    timeout: float = 1.0
    future: Optional[Future] = None


class CommandWorker(QObject):
    """Worker that processes FPGA commands in a separate thread."""
    
    connection_status_changed = Signal(bool)  # Emits True when connected, False when disconnected
    
    def __init__(self):
        super().__init__()
        self.command_queue = queue.Queue()
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.thread: Optional[QThread] = None
        self._lock = threading.Lock()
        self.connection_params: Optional[dict] = None
        
    def initialize_connection(self, nios_ip: str = '192.168.100.2',
                             multicast_ip: str = '224.1.1.1',
                             host_ip: str = '192.168.100.1',
                             udp_port: int = 5007,
                             remote_port: int = 30) -> Tuple[bool, str]:
        """
        Initialize or reinitialize TCP connection to FPGA.
        
        Args:
            nios_ip: NIOS IP address
            multicast_ip: Multicast IP address (not used for TCP, kept for compatibility)
            host_ip: Host IP address (not used for TCP, kept for compatibility)
            udp_port: UDP port (not used for TCP, kept for compatibility)
            remote_port: Remote port number for TCP connection
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        with self._lock:
            # Store connection parameters for potential reconnection
            self.connection_params = {
                'nios_ip': nios_ip,
                'multicast_ip': multicast_ip,
                'host_ip': host_ip,
                'udp_port': udp_port,
                'remote_port': remote_port
            }
            
            # Close existing socket if any
            if self.socket is not None:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            
            # Establish new connection
            success, sock, response, error_msg = initiate_fpga_connection(
                nios_ip=nios_ip,
                multicast_ip=multicast_ip,
                host_ip=host_ip,
                udp_port=udp_port,
                remote_port=remote_port
            )
            
            if success:
                self.socket = sock
                self.connection_status_changed.emit(True)
                return True, "TCP connection to FPGA successful."
            else:
                self.connection_status_changed.emit(False)
                return False, f"TCP connection failed: {error_msg}"
    
    def _reconnect(self) -> bool:
        """Attempt to reconnect using stored connection parameters."""
        if self.connection_params is None:
            return False
        
        success, _ = self.initialize_connection(**self.connection_params)
        return success
    
    def start(self):
        """Start the worker thread."""
        if self.running:
            return
        
        self.running = True
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.started.connect(self._process_commands)
        self.thread.start()
    
    def stop(self):
        """Stop the worker thread and close socket."""
        self.running = False
        # Put a sentinel value to wake up the queue
        try:
            self.command_queue.put(None, block=False)
        except queue.Full:
            pass
        
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(2000)  # Wait up to 2 seconds
            self.thread = None
        
        with self._lock:
            if self.socket is not None:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            self.connection_status_changed.emit(False)
    
    def _process_commands(self):
        """Main loop that processes commands from the queue."""
        while self.running:
            try:
                # Get command from queue (blocking with timeout to allow checking self.running)
                try:
                    cmd = self.command_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                
                # Sentinel value to exit
                if cmd is None:
                    break
                
                # Process the command
                self._execute_command(cmd)
                
            except Exception as e:
                # Log error but continue processing
                import sys
                print(f"Error in command worker: {e}", file=sys.stderr, flush=True)
                # If command has a future, set exception
                if isinstance(cmd, FPGACommand) and cmd.future is not None:
                    try:
                        cmd.future.set_exception(e)
                    except Exception:
                        pass
    
    def _execute_command(self, cmd: FPGACommand):
        """Execute a single FPGA command."""
        if cmd.future is None:
            # Create a future if one wasn't provided
            cmd.future = Future()
        
        # Check if socket is available
        with self._lock:
            sock = self.socket
        
        if sock is None:
            # Try to reconnect
            if not self._reconnect():
                error_msg = "No TCP connection available and reconnection failed"
                cmd.future.set_exception(ConnectionError(error_msg))
                return
            with self._lock:
                sock = self.socket
        
        # Send command
        try:
            command_bytes = cmd.command.encode('ascii')
            sock.sendall(command_bytes)
        except (socket.error, OSError) as e:
            # Connection lost, try to reconnect
            with self._lock:
                self.socket = None
                self.connection_status_changed.emit(False)
            
            if self._reconnect():
                # Retry once after reconnection
                try:
                    with self._lock:
                        sock = self.socket
                    if sock is not None:
                        command_bytes = cmd.command.encode('ascii')
                        sock.sendall(command_bytes)
                    else:
                        cmd.future.set_exception(ConnectionError("Reconnection failed"))
                        return
                except Exception as retry_e:
                    cmd.future.set_exception(ConnectionError(f"Retry after reconnection failed: {retry_e}"))
                    return
            else:
                cmd.future.set_exception(ConnectionError(f"Connection lost and reconnection failed: {e}"))
                return
        
        # If we don't need to wait for response, set success immediately
        if not cmd.wait_response:
            cmd.future.set_result((True, b''))
            return
        
        # Wait for response with timeout
        response_received = False
        response_bytes = b''
        start_time = time.time()
        timeout = cmd.timeout
        
        while (time.time() - start_time) < timeout:
            try:
                # Use select for non-blocking check
                ready, _, _ = select.select([sock], [], [], 0.1)
                if ready:
                    # Data available, read response
                    with self._lock:
                        sock = self.socket
                    if sock is None:
                        cmd.future.set_exception(ConnectionError("Socket closed while waiting for response"))
                        return
                    
                    recv_bytes = sock.recv(4096)
                    if recv_bytes:
                        response_bytes += recv_bytes
                        response_received = True
                        # For FPGA, we typically expect a response, but don't wait for more
                        # if we got some data (FPGA may send variable length responses)
                        break
            except (socket.error, OSError) as e:
                cmd.future.set_exception(ConnectionError(f"Error receiving response: {e}"))
                return
        
        if response_received:
            cmd.future.set_result((True, response_bytes))
        else:
            # Timeout - no response received
            cmd.future.set_exception(TimeoutError(f"No response received within {timeout} seconds"))
    
    def submit_command(self, command: str, wait_response: bool = True,
                      timeout: float = 1.0) -> Future:
        """
        Submit a command to the queue.
        
        Args:
            command: Command string to send (should include \\r\\n if needed)
            wait_response: Whether to wait for FPGA acknowledgement
            timeout: Timeout in seconds for waiting for response
            
        Returns:
            Future object that will contain (success: bool, response_bytes: bytes) or exception
        """
        future = Future()
        cmd = FPGACommand(
            command=command,
            wait_response=wait_response,
            timeout=timeout,
            future=future
        )
        
        try:
            self.command_queue.put(cmd, block=True, timeout=5.0)
        except queue.Full:
            future.set_exception(RuntimeError("Command queue is full"))
        
        return future
    
    def is_connected(self) -> bool:
        """Check if socket is connected."""
        with self._lock:
            return self.socket is not None


class FPGACommandQueue:
    """Singleton manager for FPGA command queue."""
    
    _instance: Optional['FPGACommandQueue'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self.worker: Optional[CommandWorker] = None
        self._worker_lock = threading.Lock()
    
    def _ensure_worker(self):
        """Ensure worker is created and started."""
        with self._worker_lock:
            if self.worker is None:
                self.worker = CommandWorker()
                self.worker.start()
    
    def initialize_connection(self, nios_ip: str = '192.168.100.2',
                             multicast_ip: str = '224.1.1.1',
                             host_ip: str = '192.168.100.1',
                             udp_port: int = 5007,
                             remote_port: int = 30) -> Tuple[bool, str]:
        """
        Initialize or reinitialize TCP connection to FPGA.
        
        Args:
            nios_ip: NIOS IP address
            multicast_ip: Multicast IP address (not used for TCP, kept for compatibility)
            host_ip: Host IP address (not used for TCP, kept for compatibility)
            udp_port: UDP port (not used for TCP, kept for compatibility)
            remote_port: Remote port number for TCP connection
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        self._ensure_worker()
        return self.worker.initialize_connection(
            nios_ip=nios_ip,
            multicast_ip=multicast_ip,
            host_ip=host_ip,
            udp_port=udp_port,
            remote_port=remote_port
        )
    
    def submit_command(self, command: str, wait_response: bool = True,
                      timeout: float = 1.0) -> Future:
        """
        Submit a command to the queue.
        
        Args:
            command: Command string to send (should include \\r\\n if needed)
            wait_response: Whether to wait for FPGA acknowledgement
            timeout: Timeout in seconds for waiting for response
            
        Returns:
            Future object that will contain (success: bool, response_bytes: bytes) or exception
        """
        self._ensure_worker()
        return self.worker.submit_command(
            command=command,
            wait_response=wait_response,
            timeout=timeout
        )
    
    def is_connected(self) -> bool:
        """Check if connection is established."""
        if self.worker is None:
            return False
        return self.worker.is_connected()
    
    def shutdown(self):
        """Shutdown the worker and close connection."""
        with self._worker_lock:
            if self.worker is not None:
                self.worker.stop()
                self.worker = None
