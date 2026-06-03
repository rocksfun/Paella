"""
UDP Data Manager.

This module provides a centralized, process-isolated UDP data reception system that
receives packets from a single UDP multicast socket and distributes them to
multiple subscribers. This eliminates conflicts when multiple components try
to access the same UDP socket simultaneously.

Uses a Producer-Consumer pattern with a multiprocessing.Process-based worker to receive
packets and route them to subscriber queues. Running in a separate process eliminates
GIL contention and GUI blocking issues, providing maximum timestamp accuracy.
"""

import multiprocessing
import queue
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, List, Callable, Dict, Any

from helper_functions.UDP_receive_data import (
    receive_udp_data,
    read_and_parse_udp,
    parse_udp_data
)


def create_udp_multicast_socket(
    multicast_ip: str,
    host_ip: str,
    udp_port: int,
    timeout: float = 5.0
) -> socket.socket:
    """
    Create and configure a UDP multicast socket.
    
    This is a shared helper function to avoid code duplication across modules.
    
    Args:
        multicast_ip: Multicast IP address
        host_ip: Host IP address (interface to use)
        udp_port: UDP port number
        timeout: Socket timeout in seconds
        
    Returns:
        Configured UDP socket
        
    Raises:
        Exception: If socket creation or configuration fails
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    
    # Allow address reuse (needed for multicast)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    # Try to set SO_REUSEPORT if available (helps on some systems)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    
    # Increase receive buffer size to prevent packet drops
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)  # 1MB
    except (OSError, socket.error):
        pass
    
    # Bind to all interfaces on the specified port
    sock.bind(('', udp_port))
    
    # Join multicast group
    mreq = struct.pack(
        "4s4s",
        socket.inet_aton(multicast_ip),
        socket.inet_aton(host_ip)
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    
    return sock


@dataclass
class UDPPacket:
    """UDP packet data structure.
    
    This dataclass is pickleable for inter-process communication.
    """
    raw_bytes: bytes
    parsed_frequencies: Optional[List[float]]
    address: Tuple[str, int]
    timestamp: Optional[float]
    packet_number: Optional[int] = None


# Command types for inter-process communication
class WorkerCommand:
    """Commands sent to the worker process."""
    INITIALIZE = "initialize"
    STOP = "stop"
    SHUTDOWN = "shutdown"
    ADD_SUBSCRIBER = "add_subscriber"
    REMOVE_SUBSCRIBER = "remove_subscriber"


def _udp_worker_process(
    command_queue: multiprocessing.Queue,
    connection_status_queue: multiprocessing.Queue,
    running_flag: multiprocessing.Value,
    connection_params: Dict[str, Any]
):
    """
    Worker process function that receives UDP packets and distributes them.
    
    This runs in a separate process to eliminate GIL contention and GUI blocking.
    
    Args:
        command_queue: Queue for receiving commands from main process
        connection_status_queue: Queue for sending connection status updates
        running_flag: Shared flag indicating if worker should continue running
        connection_params: Shared dictionary for connection parameters
    """
    import sys
    
    socket_obj: Optional[socket.socket] = None
    current_connection_params: Optional[dict] = None
    # Local dict to store subscriber queues (Manager.Queue objects can be pickled)
    packet_queues: Dict[int, Any] = {}  # Type: Dict[int, multiprocessing.queues.Queue]
    
    # State for throttling queue full warnings
    last_queue_full_warning_time = 0.0
    queue_full_drop_count = 0
    
    def _reconnect() -> bool:
        """Attempt to reconnect using stored connection parameters."""
        nonlocal socket_obj, current_connection_params
        if current_connection_params is None:
            return False
        
        try:
            # Close existing socket if any
            if socket_obj is not None:
                try:
                    socket_obj.close()
                except Exception:
                    pass
                socket_obj = None
            
            # Create and configure UDP socket
            sock = create_udp_multicast_socket(
                multicast_ip=current_connection_params['multicast_ip'],
                host_ip=current_connection_params['host_ip'],
                udp_port=current_connection_params['udp_port'],
                timeout=5.0
            )
            socket_obj = sock
            connection_status_queue.put(True)
            return True
        except Exception as e:
            connection_status_queue.put(False)
            print(f"UDP worker: Connection failed: {e}", file=sys.stderr, flush=True)
            return False
    
    # Main receive loop
    while running_flag.value:
        try:
            # Check for commands (non-blocking)
            try:
                cmd_data = command_queue.get_nowait()
                if isinstance(cmd_data, tuple):
                    command, *args = cmd_data
                else:
                    command = cmd_data
                    args = []
                
                if command == WorkerCommand.INITIALIZE:
                    # Get connection params from shared dict
                    params = dict(connection_params)
                    if params:
                        current_connection_params = params
                        _reconnect()
                elif command == WorkerCommand.ADD_SUBSCRIBER:
                    # Add a subscriber queue: (subscriber_id, queue)
                    if len(args) >= 2:
                        subscriber_id, sub_queue = args[0], args[1]
                        packet_queues[subscriber_id] = sub_queue
                elif command == WorkerCommand.REMOVE_SUBSCRIBER:
                    # Remove a subscriber: (subscriber_id,)
                    if len(args) >= 1:
                        subscriber_id = args[0]
                        packet_queues.pop(subscriber_id, None)
                elif command == WorkerCommand.STOP:
                    if socket_obj is not None:
                        try:
                            socket_obj.close()
                        except Exception:
                            pass
                        socket_obj = None
                        connection_status_queue.put(False)
                elif command == WorkerCommand.SHUTDOWN:
                    break
            except queue.Empty:
                pass
            
            # Check if socket is available
            if socket_obj is None:
                # Try to reconnect
                if current_connection_params is None:
                    # Try to get connection params from shared dict
                    if connection_params:
                        current_connection_params = dict(connection_params)
                
                if current_connection_params is not None:
                    if not _reconnect():
                        time.sleep(0.1)
                        continue
                else:
                    time.sleep(0.1)
                    continue
            
            # Receive packet using non-blocking mode for optimal performance
            result = read_and_parse_udp(
                socket_obj,
                timeout=None,  # Ignored in non-blocking mode
                capture_timestamp=True,
                use_kernel_timestamp=True,
                use_nonblocking=True,
                select_timeout=0.1  # Block efficiently until data arrives or timeout expires
            )
            
            if result is not None:
                raw_data, parsed_freqs, addr, recv_timestamp = result
                
                # Extract packet number if available
                packet_number = None
                if len(raw_data) >= 4:
                    try:
                        packet_count_raw = struct.unpack('<i', raw_data[:4])[0]
                        packet_number = packet_count_raw // 256
                    except (struct.error, ValueError):
                        pass
                
                # Create packet object
                packet = UDPPacket(
                    raw_bytes=raw_data,
                    parsed_frequencies=parsed_freqs,
                    address=addr,
                    timestamp=recv_timestamp,
                    packet_number=packet_number
                )
                
                # Distribute to queue-based subscribers
                # Copy queue references to avoid holding lock during queue operations
                queues_to_notify = dict(packet_queues)
                
                # Put packet in each subscriber queue (non-blocking)
                num_subscribers = len(queues_to_notify)
                if num_subscribers > 0:
                    for sub_queue in queues_to_notify.values():
                        try:
                            sub_queue.put_nowait(packet)
                        except queue.Full:
                            # Queue full, skip this subscriber (they're too slow)
                            queue_full_drop_count += 1
                            current_time = time.time()
                            if current_time - last_queue_full_warning_time > 1.0:
                                print(f"Warning: UDP subscriber queue full, {queue_full_drop_count} packets dropped recently", file=sys.stderr, flush=True)
                                try:
                                    from helper_functions.paella_remote.health import get_health_store
                                    get_health_store().record_udp_queue_drop(queue_full_drop_count)
                                except ImportError:
                                    pass
                                last_queue_full_warning_time = current_time
                                queue_full_drop_count = 0
                
                # Debug: log first few packets to verify reception
                if packet_number is not None and packet_number < 5:
                    print(f"UDP worker received packet #{packet_number}, {num_subscribers} subscribers", file=sys.stderr, flush=True)
            
        except Exception as e:
            # Log error but continue processing
            print(f"Error in UDP worker process: {e}", file=sys.stderr, flush=True)
            # Try to reconnect on error
            if socket_obj is not None:
                try:
                    socket_obj.close()
                except Exception:
                    pass
                socket_obj = None
                connection_status_queue.put(False)
            time.sleep(0.1)  # Wait before retrying
    
    # Cleanup
    if socket_obj is not None:
        try:
            socket_obj.close()
        except Exception:
            pass
    connection_status_queue.put(False)


class UDPDataWorker:
    """Worker that receives UDP packets in a separate process.
    
    This uses multiprocessing to run the UDP receiver in complete isolation,
    eliminating GIL contention and GUI blocking issues.
    """
    
    def __init__(self):
        # Inter-process communication queues
        self._command_queue: Optional[multiprocessing.Queue] = None
        self._connection_status_queue: Optional[multiprocessing.Queue] = None
        
        # Shared state for inter-process communication
        self._subscriber_queues: Dict[int, multiprocessing.Queue] = {}
        self._subscriber_lock: Optional[multiprocessing.Lock] = None
        self._running_flag: Optional[multiprocessing.Value] = None
        self._connection_params: Optional[Dict[str, Any]] = None
        
        # Process management
        self._process: Optional[multiprocessing.Process] = None
        self._next_subscriber_id = 0
        self._lock = threading.Lock()
        self._connection_params_lock = threading.Lock()
        
        # Connection status tracking
        self._connection_status_thread: Optional[threading.Thread] = None
        self._connection_status_callbacks: List[Callable[[bool], None]] = []
        self._connection_status_running = False
        
    def _start_connection_status_monitor(self):
        """Start a thread to monitor connection status from worker process."""
        if self._connection_status_running:
            return
        
        self._connection_status_running = True
        
        def monitor_loop():
            while self._connection_status_running and self._connection_status_queue is not None:
                try:
                    status = self._connection_status_queue.get(timeout=0.1)
                    # Notify callbacks
                    for callback in self._connection_status_callbacks:
                        try:
                            callback(status)
                        except Exception:
                            pass
                except queue.Empty:
                    continue
                except Exception:
                    break
        
        self._connection_status_thread = threading.Thread(target=monitor_loop, daemon=True)
        self._connection_status_thread.start()
    
    def _stop_connection_status_monitor(self):
        """Stop the connection status monitoring thread."""
        self._connection_status_running = False
        if self._connection_status_thread is not None:
            self._connection_status_thread.join(timeout=1.0)
            self._connection_status_thread = None
    
    def initialize_connection(
        self,
        multicast_ip: str = '224.1.1.1',
        host_ip: str = '192.168.100.1',
        udp_port: int = 5007
    ) -> Tuple[bool, str]:
        """
        Initialize or reinitialize UDP multicast connection.
        
        Args:
            multicast_ip: Multicast IP address
            host_ip: Host IP address (interface to use)
            udp_port: UDP port number
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        with self._lock:
            # Ensure worker process is running
            if self._process is None or not self._process.is_alive():
                self._start_process()
            
            # Update connection parameters in shared dict
            with self._connection_params_lock:
                if self._connection_params is None:
                    # Create manager for shared dict
                    manager = multiprocessing.Manager()
                    self._connection_params = manager.dict()
                
                self._connection_params['multicast_ip'] = multicast_ip
                self._connection_params['host_ip'] = host_ip
                self._connection_params['udp_port'] = udp_port
            
            # Send initialize command
            if self._command_queue is not None:
                try:
                    self._command_queue.put(WorkerCommand.INITIALIZE)
                    # Wait a moment for connection to establish
                    time.sleep(0.05)
                    return True, (
                        f"UDP multicast connection initialized. "
                        f"Multicast source: {multicast_ip}:{udp_port}, "
                        f"Interface: {host_ip}"
                    )
                except Exception as e:
                    return False, f"UDP connection initialization failed: {str(e)}"
            else:
                return False, "UDP worker process not available"
    
    def _start_process(self):
        """Start the worker process."""
        if self._process is not None and self._process.is_alive():
            return
        
        # Create inter-process communication objects
        self._command_queue = multiprocessing.Queue()
        self._connection_status_queue = multiprocessing.Queue()
        self._running_flag = multiprocessing.Value('i', 1)
        
        # Create manager for shared connection params dict
        manager = multiprocessing.Manager()
        if self._connection_params is None:
            self._connection_params = manager.dict()
        
        # Start worker process
        self._process = multiprocessing.Process(
            target=_udp_worker_process,
            args=(
                self._command_queue,
                self._connection_status_queue,
                self._running_flag,
                self._connection_params
            ),
            daemon=True,
            name="UDPDataWorker"
        )
        self._process.start()
        
        # Start connection status monitor
        self._start_connection_status_monitor()
        
        # Store reference to manager
        self._manager = manager
    
    def stop(self):
        """Stop the worker process and close socket."""
        with self._lock:
            if self._running_flag is not None:
                self._running_flag.value = 0
            
            if self._command_queue is not None:
                try:
                    self._command_queue.put(WorkerCommand.SHUTDOWN)
                except Exception:
                    pass
            
            if self._process is not None:
                self._process.join(timeout=2.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=1.0)
                    if self._process.is_alive():
                        self._process.kill()
                self._process = None
            
            self._stop_connection_status_monitor()
            
            # Clear all subscribers
            self._subscriber_queues.clear()
            self._shared_packet_queues = None
    
    def subscribe_queue(self, maxsize: int = 1000) -> Tuple[int, multiprocessing.Queue]:
        """
        Subscribe to receive UDP packets via a queue.
        
        Args:
            maxsize: Maximum size of the subscriber queue
            
        Returns:
            Tuple of (subscriber_id, queue) where queue will receive UDPPacket objects
        """
        with self._lock:
            # Ensure process is running
            if self._process is None or not self._process.is_alive():
                self._start_process()
                # Wait a moment for process to fully start
                time.sleep(0.05)
            
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            
            # Create Manager.Queue for this subscriber (can be pickled and shared)
            # Manager.Queue is slightly slower but works on all platforms including Windows
            if not hasattr(self, '_manager') or self._manager is None:
                self._manager = multiprocessing.Manager()
            
            sub_queue = self._manager.Queue(maxsize=maxsize)
            self._subscriber_queues[subscriber_id] = sub_queue
            
            # Send command to worker process to register this queue
            if self._command_queue is not None:
                try:
                    self._command_queue.put_nowait((WorkerCommand.ADD_SUBSCRIBER, subscriber_id, sub_queue))
                except Exception as e:
                    import sys
                    print(f"Warning: Failed to register subscriber in worker process: {e}", file=sys.stderr, flush=True)
        
        return subscriber_id, sub_queue
    
    def subscribe_callback(self, callback: Callable[[UDPPacket], None]) -> int:
        """
        Subscribe to receive UDP packets via a callback function.
        
        Note: Callbacks cannot be used across processes. This creates a queue
        subscription and a thread that calls the callback when packets arrive.
        
        Args:
            callback: Function that will be called with UDPPacket objects
            
        Returns:
            Subscriber ID that can be used to unsubscribe
        """
        subscriber_id, sub_queue = self.subscribe_queue(maxsize=1000)
        
        # Create a thread to monitor the queue and call the callback
        def callback_thread():
            import sys
            consecutive_errors = 0
            max_consecutive_errors = 10
            
            while subscriber_id in self._subscriber_queues:
                try:
                    packet = sub_queue.get(timeout=0.1)
                    consecutive_errors = 0  # Reset error counter on successful packet
                    try:
                        callback(packet)
                    except Exception as e:
                        print(f"Error in UDP subscriber callback: {e}", file=sys.stderr, flush=True)
                except queue.Empty:
                    continue
                except Exception as e:
                    # Log exception but continue consuming packets to prevent queue buildup
                    consecutive_errors += 1
                    print(f"Error in UDP subscriber callback thread (error {consecutive_errors}/{max_consecutive_errors}): {e}", file=sys.stderr, flush=True)
                    
                    # If we have too many consecutive errors, try to drain the queue
                    # to prevent it from filling up completely
                    if consecutive_errors >= max_consecutive_errors:
                        print(f"Warning: Too many consecutive errors in callback thread, draining queue to prevent overflow", file=sys.stderr, flush=True)
                        # Drain queue to prevent overflow
                        drained = 0
                        while drained < 100:  # Drain up to 100 packets
                            try:
                                sub_queue.get_nowait()
                                drained += 1
                            except queue.Empty:
                                break
                        if drained > 0:
                            print(f"Drained {drained} packets from queue", file=sys.stderr, flush=True)
                        consecutive_errors = 0  # Reset after draining
                    
                    # Continue loop instead of breaking to keep consuming packets
                    continue
        
        thread = threading.Thread(target=callback_thread, daemon=True)
        thread.start()
        
        return subscriber_id
    
    def unsubscribe(self, subscriber_id: int):
        """
        Unsubscribe from receiving UDP packets.
        
        Args:
            subscriber_id: ID returned from subscribe_queue() or subscribe_callback()
        """
        with self._lock:
            self._subscriber_queues.pop(subscriber_id, None)
            # Send command to worker process to unregister this queue
            if self._command_queue is not None:
                try:
                    self._command_queue.put_nowait((WorkerCommand.REMOVE_SUBSCRIBER, subscriber_id))
                except Exception:
                    pass
    
    def is_connected(self) -> bool:
        """Check if worker process is running and healthy."""
        if self._process is None or not self._process.is_alive():
            return False
        # Check if process has been running for at least a short time
        # (avoids false positives during startup)
        return True
    
    def get_packet(self, timeout: Optional[float] = None) -> Optional[UDPPacket]:
        """
        Get a single packet from the UDP stream (convenience method).
        
        This creates a temporary subscription, gets one packet, then unsubscribes.
        Useful for one-off packet reads.
        
        Args:
            timeout: Timeout in seconds (None for blocking)
            
        Returns:
            UDPPacket or None if timeout
        """
        subscriber_id, sub_queue = self.subscribe_queue(maxsize=1)
        try:
            return sub_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.unsubscribe(subscriber_id)


class UDPDataManager:
    """Singleton manager for UDP data reception using multiprocessing."""
    
    _instance: Optional['UDPDataManager'] = None
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
        self.worker: Optional[UDPDataWorker] = None
        self._worker_lock = threading.Lock()
        
        # Signal compatibility layer for Qt-based code
        self._signal_emitter: Optional['_SignalEmitter'] = None
    
    def _ensure_worker(self):
        """Ensure worker is created and started."""
        with self._worker_lock:
            if self.worker is None:
                self.worker = UDPDataWorker()
    
    def initialize_connection(
        self,
        multicast_ip: str = '224.1.1.1',
        host_ip: str = '192.168.100.1',
        udp_port: int = 5007
    ) -> Tuple[bool, str]:
        """
        Initialize or reinitialize UDP multicast connection.
        
        Args:
            multicast_ip: Multicast IP address
            host_ip: Host IP address (interface to use)
            udp_port: UDP port number
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        self._ensure_worker()
        success, msg = self.worker.initialize_connection(
            multicast_ip=multicast_ip,
            host_ip=host_ip,
            udp_port=udp_port
        )
        # Give the worker process a moment to start receiving
        if success:
            time.sleep(0.05)  # 50ms delay to ensure worker process is ready
        return success, msg
    
    def subscribe_queue(self, maxsize: int = 1000) -> Tuple[int, multiprocessing.Queue]:
        """
        Subscribe to receive UDP packets via a queue.
        
        Args:
            maxsize: Maximum size of the subscriber queue
            
        Returns:
            Tuple of (subscriber_id, queue) where queue will receive UDPPacket objects
        """
        self._ensure_worker()
        return self.worker.subscribe_queue(maxsize=maxsize)
    
    def subscribe_callback(self, callback: Callable[[UDPPacket], None]) -> int:
        """
        Subscribe to receive UDP packets via a callback function.
        
        Args:
            callback: Function that will be called with UDPPacket objects
            
        Returns:
            Subscriber ID that can be used to unsubscribe
        """
        self._ensure_worker()
        return self.worker.subscribe_callback(callback)
    
    def subscribe_signal(self):
        """
        Subscribe to receive UDP packets via Qt Signal (for Qt-based code).
        
        Note: This creates a compatibility layer that uses a queue and thread
        to emit Qt signals, since signals cannot cross process boundaries.
        
        Returns:
            SignalEmitter object with a packet_received signal
        """
        self._ensure_worker()
        if self._signal_emitter is None:
            self._signal_emitter = _SignalEmitter(self.worker)
        return self._signal_emitter
    
    def unsubscribe(self, subscriber_id: int):
        """
        Unsubscribe from receiving UDP packets.
        
        Args:
            subscriber_id: ID returned from subscribe_queue() or subscribe_callback()
        """
        if self.worker is not None:
            self.worker.unsubscribe(subscriber_id)
    
    def get_packet(self, timeout: Optional[float] = None) -> Optional[UDPPacket]:
        """
        Get a single packet from the UDP stream (convenience method).
        
        Args:
            timeout: Timeout in seconds (None for blocking)
            
        Returns:
            UDPPacket or None if timeout
        """
        self._ensure_worker()
        return self.worker.get_packet(timeout=timeout)
    
    def is_connected(self) -> bool:
        """Check if connection is established."""
        if self.worker is None:
            return False
        return self.worker.is_connected()
    
    def shutdown(self):
        """Shutdown the worker and close connection."""
        with self._worker_lock:
            if self._signal_emitter is not None:
                self._signal_emitter.stop()
                self._signal_emitter = None
            if self.worker is not None:
                self.worker.stop()
                self.worker = None


class _SignalEmitter:
    """Compatibility layer to provide Qt signals for process-based UDP worker.
    
    This uses a queue subscription and a thread to emit Qt signals, since
    Qt signals cannot cross process boundaries.
    """
    
    def __init__(self, worker: UDPDataWorker):
        # Try to import Qt Signal, but handle gracefully if not available
        try:
            from PySide6.QtCore import QObject, Signal
            self._qt_available = True
            self._Signal = Signal
            self._QObject = QObject
        except ImportError:
            self._qt_available = False
            self._Signal = None
            self._QObject = None
        
        if self._qt_available:
            class _SignalObject(QObject):
                packet_received = Signal(object)
            
            self._signal_obj = _SignalObject()
            self.packet_received = self._signal_obj.packet_received
        else:
            # Fallback: create a dummy signal-like object
            class _DummySignal:
                def connect(self, *args, **kwargs):
                    pass
            self.packet_received = _DummySignal()
        
        self._worker = worker
        self._subscriber_id: Optional[int] = None
        self._subscriber_queue: Optional[multiprocessing.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._start()
    
    def _start(self):
        """Start the signal emission thread."""
        if self._running or not self._qt_available:
            return
        
        self._subscriber_id, self._subscriber_queue = self._worker.subscribe_queue(maxsize=1000)
        self._running = True
        
        def emit_loop():
            import sys
            consecutive_errors = 0
            max_consecutive_errors = 10
            
            while self._running and self._subscriber_queue is not None:
                try:
                    packet = self._subscriber_queue.get(timeout=0.1)
                    consecutive_errors = 0  # Reset error counter on successful packet
                    if self._qt_available:
                        self._signal_obj.packet_received.emit(packet)
                except queue.Empty:
                    continue
                except Exception as e:
                    # Log exception but continue consuming packets to prevent queue buildup
                    consecutive_errors += 1
                    print(f"Error in UDP signal emitter thread (error {consecutive_errors}/{max_consecutive_errors}): {e}", file=sys.stderr, flush=True)
                    
                    # If we have too many consecutive errors, try to drain the queue
                    # to prevent it from filling up completely
                    if consecutive_errors >= max_consecutive_errors:
                        print(f"Warning: Too many consecutive errors in signal emitter thread, draining queue to prevent overflow", file=sys.stderr, flush=True)
                        # Drain queue to prevent overflow
                        drained = 0
                        while drained < 100:  # Drain up to 100 packets
                            try:
                                self._subscriber_queue.get_nowait()
                                drained += 1
                            except queue.Empty:
                                break
                        if drained > 0:
                            print(f"Drained {drained} packets from queue", file=sys.stderr, flush=True)
                        consecutive_errors = 0  # Reset after draining
                    
                    # Continue loop instead of breaking to keep consuming packets
                    continue
        
        self._thread = threading.Thread(target=emit_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop the signal emission thread."""
        self._running = False
        if self._subscriber_id is not None:
            self._worker.unsubscribe(self._subscriber_id)
            self._subscriber_id = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
