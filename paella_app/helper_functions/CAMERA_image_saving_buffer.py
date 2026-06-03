"""
Camera Image Saving Buffer Module.

This module provides functionality to save camera images to binary files
in a buffered format with metadata strings and image data.
"""

import os
import struct
import threading
import queue
import numpy as np
from typing import Optional, Tuple


class ImageSavingBuffer:
    """Manages saving camera images to binary files in a buffered format.
    
    Each binary file contains a configurable maximum number of frames (default: 350,000 for BF+FL, 700,000 for BF-only).
    Each frame entry consists of:
    - 256-byte fixed-length metadata string (null-padded, UTF-8)
    - BF image as raw uint8 array (width × height bytes)
    - FL image as raw uint8 array (width × height bytes) [only in BF+FL mode]
    
    Files are named: {experiment_string}_{file_suffix}_{file_number:03d}.bin
    where file_suffix is "VolumeImages" for BF+FL mode or "Images" for BF-only mode.
    """
    
    def __init__(self, experiment_string: str, sample_path: str, file_suffix: str = "VolumeImages", max_frames_per_file: int = 350000):
        """
        Initialize the image saving buffer.
        
        Args:
            experiment_string: Experiment prefix for file naming (e.g., "LC14_202401151430")
            sample_path: Path to the sample directory where files will be saved
            file_suffix: Suffix for file naming (default: "VolumeImages", use "Images" for BF-only mode)
            max_frames_per_file: Maximum number of frames per file (default: 350,000 for BF+FL, use 700,000 for BF-only)
        """
        self.experiment_string = experiment_string
        self.sample_path = sample_path
        self.file_suffix = file_suffix
        self.max_frames_per_file = max_frames_per_file
        self.file_number = 1
        self.frame_count_in_current_file = 0
        
        # Queue for buffering frames before saving.
        # Reduced from 1000 to 50 to prevent massive RAM usage (50 frames ~ 400 MB).
        self.frame_queue = queue.Queue(maxsize=50)
        self.last_queue_full_print_time = 0

        
        # Thread safety for file operations
        self.lock = threading.Lock()
        
        # Current file handle
        self.current_file_handle: Optional[object] = None
        
        # Open first file
        self._open_current_file()
        
        # Background thread for writing frames
        self.write_thread_running = False
        self.write_thread_stop_event = threading.Event()
        self.write_thread: Optional[threading.Thread] = None
        self._start_write_thread()
    
    def _generate_filename(self, file_number: int) -> str:
        """Generate filename for a given file number.
        
        Args:
            file_number: File number (starting at 1)
            
        Returns:
            Filename in format: {experiment_string}_{file_suffix}_{file_number:03d}.bin
        """
        filename = f"{self.experiment_string}_{self.file_suffix}_{file_number:03d}.bin"
        return os.path.join(self.sample_path, filename)
    
    def _open_current_file(self):
        """Open the current file for writing (binary mode)."""
        filename = self._generate_filename(self.file_number)
        
        # Ensure directory exists
        os.makedirs(self.sample_path, exist_ok=True)
        
        # Open file in binary write mode
        self.current_file_handle = open(filename, 'wb')
        self.frame_count_in_current_file = 0
        abs_path = os.path.abspath(filename)
        print(f"Opened image file: {abs_path}")
    
    def _rotate_file(self):
        """Close current file and open next file when capacity reached."""
        if self.current_file_handle:
            self.current_file_handle.close()
            self.current_file_handle = None
        
        self.file_number += 1
        self._open_current_file()
    
    def _write_frame_to_file(self, metadata_str: str, bf_image: np.ndarray, fl_image: Optional[np.ndarray], bf_only_mode: bool = False):
        """
        Write a single frame entry to the current file.
        
        Args:
            metadata_str: Metadata string (will be padded to 256 bytes)
            bf_image: Brightfield image as numpy array (uint8)
            fl_image: Fluorescent image as numpy array (uint8), or None if missing
            bf_only_mode: If True, skip writing FL image data entirely (for BF-only mode)
        """
        # Ensure metadata string is exactly 256 bytes (null-padded) using fast ljust
        metadata_bytes = metadata_str.encode('utf-8')
        metadata_bytes = metadata_bytes.ljust(256, b'\x00')[:256]
        
        with self.lock:
            # Write metadata string (256 bytes)
            self.current_file_handle.write(metadata_bytes)
            
            # Write BF image data directly to avoid copying
            # Fallback to uint8 cast only if not already uint8
            if bf_image.dtype != np.uint8:
                bf_buffer = bf_image.astype(np.uint8)
            else:
                bf_buffer = bf_image
                
            # Convert to C-contiguous array if it isn't already to avoid flatten() copy
            if not bf_buffer.flags.c_contiguous:
                bf_buffer = np.ascontiguousarray(bf_buffer)
                
            # Write direct memory buffer to file (zero copy)
            self.current_file_handle.write(memoryview(bf_buffer))
            
            # Write FL image data
            if bf_only_mode:
                pass
            elif fl_image is not None:
                if fl_image.dtype != np.uint8:
                    fl_buffer = fl_image.astype(np.uint8)
                else:
                    fl_buffer = fl_image
                    
                if not fl_buffer.flags.c_contiguous:
                    fl_buffer = np.ascontiguousarray(fl_buffer)
                    
                self.current_file_handle.write(memoryview(fl_buffer))
            else:
                # If FL image is missing, write zeros with same size as BF
                self.current_file_handle.write(b'\x00' * bf_buffer.size)
            
            # Increment frame count
            self.frame_count_in_current_file += 1
            
            # Check if we need to rotate to next file
            if self.frame_count_in_current_file >= self.max_frames_per_file:
                self._rotate_file()
                
    def _start_write_thread(self):
        """Start background thread for writing frames to file."""
        if self.write_thread is not None and self.write_thread.is_alive():
            return
        
        self.write_thread_running = True
        self.write_thread_stop_event.clear()
        
        def write_loop():
            """Background thread loop for writing frames."""
            while self.write_thread_running:
                try:
                    # Wait for a frame or timeout
                    try:
                        # Wait up to 0.1s for a new frame
                        frame_data = self.frame_queue.get(timeout=0.1)
                        metadata_obj, bf_image, fl_image, bf_only_mode = frame_data
                        
                        # Process metadata
                        if isinstance(metadata_obj, (list, tuple)):
                            # Format metadata string: 14 underscore-delimited values
                            # Format: computer_time, bf_camera_time, bf_frame_number, fl_camera_time, fl_frame_number, 
                            #         trigger_flag, bf_width, bf_height, fl_width, fl_height,
                            #         bf_exposure_us, fl_exposure_us, blue_led_current_a, photodiode_voltage_v
                            # Using joined string is slightly faster than f-string for many items
                            metadata_str = "_".join(str(val) for val in metadata_obj)
                        else:
                            metadata_str = str(metadata_obj)
                            
                        self._write_frame_to_file(metadata_str, bf_image, fl_image, bf_only_mode)
                        self.frame_queue.task_done()
                    except queue.Empty:
                        pass
                    
                    # Check stop event
                    if self.write_thread_stop_event.is_set() and self.frame_queue.empty():
                        break
                        
                except Exception as e:
                    print(f"Error in image write thread: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Ensure file is flushed before thread exits
            self.flush()
        
        self.write_thread = threading.Thread(target=write_loop, daemon=True, name="ImageSaverWriteThread")
        self.write_thread.start()
    
    def add_frame(self, metadata, bf_image: np.ndarray, fl_image: Optional[np.ndarray] = None, bf_only_mode: bool = False):
        """
        Add a frame to the buffer and write to file.
        
        Args:
            metadata: Metadata string OR tuple/list of 14 metadata values
            bf_image: Brightfield image as numpy array
            fl_image: Fluorescent image as numpy array, or None if missing
            bf_only_mode: If True, skip writing FL image data entirely (for BF-only mode)
        """
        try:
            # Put frame data directly into the queue.
            # Avoid copying images here if possible. Since this is buffered and written
            # quickly, the camera driver usually manages buffer rotation. If tearing occurs,
            # we can restore np.copy, but for now we prioritize preventing memory saturation.
            # Using copy() here defeats the purpose of the zero-copy buffer write.
            self.frame_queue.put_nowait((metadata, bf_image, fl_image, bf_only_mode))
        except queue.Full:
            import time
            current_time = time.time()
            if current_time - getattr(self, 'last_queue_full_print_time', 0) > 1.0:
                print("Warning: Image saving buffer queue is full. Dropping frames.")
                self.last_queue_full_print_time = current_time
        except Exception as e:
            print(f"Error buffering frame for save: {e}")
            raise
    
    def is_full(self) -> bool:
        """Check if the internal queue is full (backpressure monitoring)."""
        return self.frame_queue.full()
    
    def flush(self):
        """Flush any buffered data to disk."""
        with self.lock:
            if self.current_file_handle:
                self.current_file_handle.flush()
    
    def close(self):
        """Close the current file."""
        # Stop write thread
        self.write_thread_running = False
        self.write_thread_stop_event.set()
        
        # Wait for write thread to finish (with timeout)
        if self.write_thread is not None and self.write_thread.is_alive():
            self.write_thread.join(timeout=5.0)
            
        # Flush any remaining data to disk
        self.flush()
        
        with self.lock:
            if self.current_file_handle:
                self.current_file_handle.close()
                self.current_file_handle = None
                print(f"Closed image file: {self._generate_filename(self.file_number)}")
