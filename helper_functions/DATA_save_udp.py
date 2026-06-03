"""
UDP Data Saving Module.

This module provides functionality to save UDP packet data to binary files
in the format required for LabVIEW compatibility.
"""

import struct
import threading
import queue
import os
from typing import Optional


class DataSaver:
    """Manages saving UDP packet data to binary files.
    
    Formats packets into arrays of I32 values (timestamp delta, packet number,
    and frequency data) and writes them to big-endian binary files in batches.
    """
    
    def __init__(self, experiment_string: str, sample_path: str):
        """
        Initialize the data saver.
        
        Args:
            experiment_string: Experiment prefix for file naming (e.g., "LC14_202401151430")
            sample_path: Path to the sample directory where files will be saved
        """
        self.experiment_string = experiment_string
        self.sample_path = sample_path
        self.file_path = os.path.join(sample_path, f"{experiment_string}_a00.bin")
        
        # Queue for storing formatted packet data (thread-safe queue)
        # Use a large queue size to prevent blocking
        self.packet_queue = queue.Queue(maxsize=10000)
        
        # File handle
        self.file_handle: Optional[object] = None
        self.file_lock = threading.Lock()
        
        # Batch size for writing
        self.batch_size = 10
        
        # Background thread for writing to file
        self.write_thread: Optional[threading.Thread] = None
        self.write_thread_running = False
        self.write_thread_stop_event = threading.Event()
        
        # State tracking for non-blocking close
        self.is_closing = False
        self.finished_event = threading.Event()
        
        # Open file in append mode (binary)
        try:
            self.file_handle = open(self.file_path, 'ab')
        except Exception as e:
            print(f"Error opening data file {self.file_path}: {e}")
            raise
        
        # Start background write thread
        self._start_write_thread()
    
    def add_packet(self, packet, experiment_start_time: float):
        """
        Add a packet to the save queue (non-blocking).
        
        Args:
            packet: PacketData object containing packet information
            experiment_start_time: Timestamp when experiment started (for delta calculation)
        """
        if self.is_closing:
            return

        try:
            # Format packet into binary data
            formatted_data = self._format_packet(packet, experiment_start_time)
            
            if formatted_data is not None:
                # Add to queue (non-blocking - will raise Full exception if queue is full)
                try:
                    self.packet_queue.put_nowait(formatted_data)
                except queue.Full:
                    # Queue is full - drop packet to avoid blocking
                    print(f"Warning: Data saver queue full, packet dropped")
        except Exception as e:
            print(f"Error adding packet to save queue: {e}")
    
    def _format_packet(self, packet, experiment_start_time: float) -> Optional[bytes]:
        """
        Format packet into variable-length I32 array (big-endian).
        
        Format:
        - Entry 0: Timestamp delta (I32) = (packet_timestamp - experiment_start_time) * 2^16
        - Entry 1: Packet number (I32)
        - Entries 2 to N: All available frequency I32 values from UDP packet
        
        Args:
            packet: PacketData object
            experiment_start_time: Timestamp when experiment started
            
        Returns:
            Bytes containing formatted packet data (big-endian I32 values) or None on error
        """
        try:
            # Validate packet has required data
            if packet is None or not hasattr(packet, 'raw_bytes') or not hasattr(packet, 'timestamp'):
                return None
            
            if len(packet.raw_bytes) < 4:
                return None
            
            # Calculate timestamp delta: (packet_timestamp - experiment_start_time) * 2^16
            # Both timestamps should be in the same format (either both perf_counter or both epoch time)
            # Ensure experiment_start_time is valid
            if experiment_start_time is None:
                print(f"Error: experiment_start_time is None. Cannot calculate timestamp delta.")
                return None
            
            timestamp_delta_seconds = packet.timestamp - experiment_start_time
            
            # Ensure delta is non-negative (packet should arrive after experiment starts)
            if timestamp_delta_seconds < 0:
                # If negative, it might be due to timestamp format mismatch or clock issues
                # Log detailed information for debugging
                print(f"Warning: Negative timestamp delta detected: {timestamp_delta_seconds} seconds")
                print(f"  Packet timestamp: {packet.timestamp}")
                print(f"  Experiment start: {experiment_start_time}")
                print(f"  Difference: {packet.timestamp - experiment_start_time}")
                # Set to 0 as a safe fallback
                timestamp_delta_seconds = 0.0
            
            # Scale by 2^16 and cast to integer
            timestamp_delta_scaled = int(timestamp_delta_seconds * 65536)  # 2^16 = 65536
            
            # Clamp to I32 range to prevent overflow
            # I32 range: -2147483648 to 2147483647
            I32_MIN = -2147483648
            I32_MAX = 2147483647
            timestamp_delta_scaled = max(I32_MIN, min(I32_MAX, timestamp_delta_scaled))
            
            # Cast to I32 (signed 32-bit integer)
            timestamp_delta_i32 = struct.pack('>i', timestamp_delta_scaled)
            
            # Extract packet number
            # First 4 bytes contain packet number (little-endian I32)
            packet_number_raw = struct.unpack('<i', packet.raw_bytes[:4])[0]
            # Divide by 256 and use the quotient as the packet number
            packet_number = packet_number_raw // 256
            
            # Extract all available frequency I32 values from raw_bytes[4:]
            # Skip first 4 bytes which are the packet number header
            frequency_bytes = packet.raw_bytes[4:]
            
            # Calculate number of I32 values in frequency data
            num_frequency_i32 = len(frequency_bytes) // 4
            
            # Build the array: [timestamp_delta, packet_number, ...frequency_i32_values]
            # Unpack frequency data as little-endian I32, then repack as big-endian
            frequency_i32_list = []
            for i in range(num_frequency_i32):
                offset = i * 4
                if offset + 4 <= len(frequency_bytes):
                    # Unpack as little-endian (from UDP packet)
                    freq_value = struct.unpack('<i', frequency_bytes[offset:offset+4])[0]
                    frequency_i32_list.append(freq_value)
            
            # Build complete array: [timestamp_delta, packet_number, ...frequency_i32_values]
            array_length = 2 + len(frequency_i32_list)
            
            if array_length == 2:
                # Only timestamp delta and packet number (no frequency data)
                formatted_data = struct.pack('>2i', timestamp_delta_scaled, packet_number)
            else:
                # Pack all values together as big-endian I32
                all_values = [timestamp_delta_scaled, packet_number] + frequency_i32_list
                formatted_data = struct.pack(f'>{array_length}i', *all_values)
            
            return formatted_data
            
        except Exception as e:
            print(f"Error formatting packet: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _start_write_thread(self):
        """Start background thread for writing packets to file."""
        if self.write_thread is not None and self.write_thread.is_alive():
            return
        
        self.write_thread_running = True
        self.write_thread_stop_event.clear()
        self.finished_event.clear()
        
        def write_loop():
            """Background thread loop for writing packets."""
            packets_to_write = []
            
            # Continue as long as running OR queue is not empty
            while self.write_thread_running or not self.packet_queue.empty():
                try:
                    # Collect packets up to batch_size
                    while len(packets_to_write) < self.batch_size:
                        try:
                            # Wait up to 0.1 seconds for a packet
                            # If we are stopping, use a shorter timeout to drain faster
                            get_timeout = 0.01 if not self.write_thread_running else 0.1
                            packet_data = self.packet_queue.get(timeout=get_timeout)
                            packets_to_write.append(packet_data)
                        except queue.Empty:
                            # Timeout - write what we have if any
                            break
                    
                    # Write batch if we have packets
                    if packets_to_write:
                        self._write_packets_to_file(packets_to_write)
                        packets_to_write = []
                    
                    # Check stop event ONLY if queue is empty
                    if self.write_thread_stop_event.is_set() and self.packet_queue.empty():
                        break
                        
                except Exception as e:
                    print(f"Error in write thread: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Final drain of any remaining packets before stopping
            while not self.packet_queue.empty():
                try:
                    packet_data = self.packet_queue.get_nowait()
                    packets_to_write.append(packet_data)
                    if len(packets_to_write) >= self.batch_size:
                        self._write_packets_to_file(packets_to_write)
                        packets_to_write = []
                except queue.Empty:
                    break
                    
            if packets_to_write:
                self._write_packets_to_file(packets_to_write)

            # --- CRITICAL: Close file handle in background thread ---
            if self.file_handle is not None:
                try:
                    with self.file_lock:
                        self.file_handle.close()
                        print(f"DataSaver: File closed in background thread: {os.path.basename(self.file_path)}")
                except Exception as e:
                    print(f"Error closing file in background: {e}")
                finally:
                    self.file_handle = None
            
            # Signal that we are completely finished
            self.finished_event.set()
        
        self.write_thread = threading.Thread(target=write_loop, daemon=True, name="DataSaverWriteThread")
        self.write_thread.start()
    
    def _write_packets_to_file(self, packets_to_write):
        """Write a list of packet data to file."""
        if self.file_handle is None or not packets_to_write:
            return
        
        try:
            with self.file_lock:
                for packet_data in packets_to_write:
                    self.file_handle.write(packet_data)
                self.file_handle.flush()  # Ensure data is written to disk
        except Exception as e:
            print(f"Error writing batch to file: {e}")
            import traceback
            traceback.print_exc()
    
    def flush(self):
        """Write any remaining packets in queue to file."""
        # Wait for write thread to finish processing queue
        # Give it time to drain the queue
        max_wait_time = 5.0  # Maximum 5 seconds to wait
        wait_interval = 0.1  # Check every 100ms
        waited = 0.0
        
        while waited < max_wait_time:
            if self.packet_queue.empty():
                break
            threading.Event().wait(wait_interval)
            waited += wait_interval
        
        # Final flush to ensure all data is written
        if self.file_handle is not None:
            try:
                with self.file_lock:
                    self.file_handle.flush()
            except Exception as e:
                print(f"Error flushing file: {e}")
    
    def close(self, wait=False):
        """Initiate non-blocking close.
        
        Args:
            wait: If True, blocks until the file is actually closed (old behavior).
                  Returns the finished_event (threading.Event).
        """
        if self.is_closing:
            if wait:
                self.finished_event.wait(timeout=5.0)
            return self.finished_event

        self.is_closing = True
        
        # Stop write thread loop (it will still drain the queue because of the while loop update)
        self.write_thread_running = False
        self.write_thread_stop_event.set()
        
        if wait:
            # Wait for write thread to finish (with timeout)
            if self.write_thread is not None and self.write_thread.is_alive():
                self.finished_event.wait(timeout=5.0)
        
        return self.finished_event
