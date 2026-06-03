"""
Image Control Module.

This module provides a widget for controlling image acquisition and processing.
It can be used standalone or embedded in other applications.
"""

import sys
import os
import time
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QSizePolicy, QTabWidget, QSpinBox,
    QFormLayout, QLineEdit, QDoubleSpinBox, QGroupBox, QComboBox, QCheckBox,
    QDialog, QDialogButtonBox
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap, QPainter, QFont, QColor
from pypylon import pylon
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False

try:
    import nidaqmx
    NIDAQMX_AVAILABLE = True
except ImportError as e:
    print(f"NI-DAQmx import failed: {e}. Hardware controls will be disabled.")
    NIDAQMX_AVAILABLE = False
except Exception as e:
    print(f"NI-DAQmx unexpected error: {e}. Hardware controls will be disabled.")
    NIDAQMX_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

# References directory relative to script location
if hasattr(sys, '_MEIPASS'):
    # When running as a bundled executable
    _SCRIPT_DIR = sys._MEIPASS
else:
    # When running from source
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

REFERENCES_DIR = os.path.join(_SCRIPT_DIR, 'references')

# Import ROI detection functions
from helper_functions.ROI_detection import (
    detect_edges, calculate_roi_from_edges,
    detect_edges_with_lines, calculate_roi_from_lines
)
from helper_functions.ROI_detection_fl import detect_fl_roi_center_mass_quadrants
from helper_functions.ROI_focus import calculate_roi_focus
from helper_functions.ROI_plots import (
    HistogramWidget, AnglePlotWidget, FocusPlotWidget, AlignmentPlotWidget,
    BlueLEDCurrentPlotWidget, PhotodiodePlotWidget
)
from helper_functions.SYSTEM_pull_config_io import (
    load_system_config,
    get_camera_info,
    get_daq_info,
    get_camera_settings,
)
from helper_functions.CAMERA_image_saving_buffer import ImageSavingBuffer
from helper_functions.UIUX_elements import (
    create_button, create_led_button, create_status_label, create_status_badge,
    style_input_field, style_checkbox, Colors
)


class TriggeredMetadataDialog(QDialog):
    """Dialog to display metadata for the most recent triggered frame."""
    
    def __init__(self, metadata_dict, camera_channels, camera_device_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Triggered Frame Metadata")
        self.setMinimumWidth(500)
        self.setMinimumHeight(300)
        
        layout = QVBoxLayout(self)
        
        # Check if we have any metadata
        if not metadata_dict:
            no_data_label = QLabel("No triggered frames recorded yet.")
            no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_data_label.setStyleSheet("font-size: 12pt; padding: 20px;")
            layout.addWidget(no_data_label)
        else:
            # Create a form layout to display metadata for each camera
            form_layout = QVBoxLayout()
            
            # Display metadata for each camera that has data
            for camera_index, metadata in metadata_dict.items():
                # Get camera channel name
                channel_name = camera_channels.get(camera_index, f"Camera {camera_index}")
                if channel_name == 'brightfield':
                    channel_display = "Brightfield"
                elif channel_name == 'fluorescent':
                    channel_display = "Fluorescent"
                else:
                    channel_display = channel_name
                
                # Get device name if available
                device_name = ""
                if camera_index < len(camera_device_names):
                    device_name = camera_device_names[camera_index]
                
                # Create a group box for this camera
                camera_group = QGroupBox(f"{channel_display} Camera")
                if device_name:
                    camera_group.setTitle(f"{channel_display} Camera ({device_name})")
                camera_layout = QFormLayout()
                camera_group.setLayout(camera_layout)
                
                # Display camera timestamp
                camera_timestamp = metadata.get('camera_timestamp')
                if camera_timestamp is not None:
                    # Convert nanoseconds to seconds for display
                    timestamp_sec = camera_timestamp / 1e9
                    timestamp_label = QLabel(f"{timestamp_sec:.9f} s ({camera_timestamp} ns)")
                else:
                    timestamp_label = QLabel("N/A")
                timestamp_label.setStyleSheet("font-family: monospace;")
                camera_layout.addRow("Camera Timestamp:", timestamp_label)
                
                # Display image number
                image_number = metadata.get('image_number')
                if image_number is not None:
                    image_number_label = QLabel(str(image_number))
                else:
                    image_number_label = QLabel("N/A")
                image_number_label.setStyleSheet("font-family: monospace;")
                camera_layout.addRow("Image Number:", image_number_label)
                
                # Display computer timestamp
                computer_timestamp = metadata.get('computer_timestamp')
                if computer_timestamp is not None:
                    # Format as readable datetime
                    from datetime import datetime
                    dt = datetime.fromtimestamp(computer_timestamp)
                    timestamp_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Include milliseconds
                    computer_label = QLabel(f"{timestamp_str} ({computer_timestamp:.6f} s)")
                else:
                    computer_label = QLabel("N/A")
                computer_label.setStyleSheet("font-family: monospace;")
                camera_layout.addRow("Computer Timestamp:", computer_label)
                
                form_layout.addWidget(camera_group)
            
            layout.addLayout(form_layout)
        
        # Add close button
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)


class CameraThread(QThread):
    """Thread for acquiring images from a Basler camera."""
    imageAcquired = Signal(np.ndarray, int, dict)  # image, camera_index, metadata
    
    def __init__(self, camera, camera_index, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.camera_index = camera_index
        self.running = False
        self.frame_times = []
        self.hw_frame_times = []  # Track camera hardware timestamps
        self.max_frame_times = 200  # Increased window for stable FPS (averages ~150ms at 1250Hz)
    
    def run(self):
        """Acquire images continuously while running."""
        self.running = True
        try:
            # Don't start grabbing here if camera is already grabbing
            # (cameras should be started simultaneously before threads start)
            if not self.camera.IsGrabbing():
                self.camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
            
            while self.running:
                grab_result = self.camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                if grab_result.GrabSucceeded():
                    # MUST copy the array! grab_result.Array is a view into PyPylon's internal
                    # memory. If we emit the view across threads via a QueuedConnection, and
                    # then call grab_result.Release() before the GUI thread consumes it, we get a segfault!
                    image_array = grab_result.Array.copy()
                    
                    # Get camera metadata
                    metadata = {}
                    try:
                        metadata['camera_timestamp'] = grab_result.TimeStamp  # Camera timestamp in nanoseconds
                    except Exception:
                        metadata['camera_timestamp'] = None
                    
                    try:
                        # Try ImageNumber first, then BlockID
                        if hasattr(grab_result, 'ImageNumber'):
                            metadata['image_number'] = grab_result.ImageNumber
                        elif hasattr(grab_result, 'BlockID'):
                            metadata['image_number'] = grab_result.BlockID
                        else:
                            metadata['image_number'] = None
                    except Exception:
                        metadata['image_number'] = None
                    
                    # Computer timestamp when frame was received
                    computer_timestamp = time.time()
                    metadata['computer_timestamp'] = computer_timestamp
                    
                    # Track frame time for FPS calculation
                    self.frame_times.append(computer_timestamp)
                    if metadata['camera_timestamp'] is not None:
                        self.hw_frame_times.append(metadata['camera_timestamp'])
                    
                    if len(self.frame_times) > self.max_frame_times:
                        self.frame_times.pop(0)
                    if len(self.hw_frame_times) > self.max_frame_times:
                        self.hw_frame_times.pop(0)
                    
                    self.imageAcquired.emit(image_array, self.camera_index, metadata)
                grab_result.Release()
        except Exception as e:
            print(f"Camera {self.camera_index} error: {e}")
        finally:
            if self.camera.IsGrabbing():
                self.camera.StopGrabbing()
    
    def get_fps(self):
        """Calculate current frame rate using hardware timestamps if available, otherwise computer clock."""
        # Use hardware timestamps for maximum stability (generated on camera)
        if len(self.hw_frame_times) >= 2:
            # Pylon timestamps are in nanoseconds
            time_span_ns = self.hw_frame_times[-1] - self.hw_frame_times[0]
            if time_span_ns > 0:
                return ((len(self.hw_frame_times) - 1) * 1e9) / time_span_ns
        
        # Fallback to computer clock (prone to software jitter)
        if len(self.frame_times) < 2:
            return 0.0
        time_span = self.frame_times[-1] - self.frame_times[0]
        if time_span > 0:
            return (len(self.frame_times) - 1) / time_span
        return 0.0
    
    def stop(self):
        """Stop the acquisition thread."""
        self.running = False
        self.wait()


class ImageDisplayLabel(QLabel):
    """Custom label for displaying camera images."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("No image")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("""
            background-color: #000000;
            color: #ffffff;
            border: 2px solid #ccc;
            border-radius: 5px;
        """)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setScaledContents(False)  # We'll handle scaling manually to maintain aspect ratio
        self._pixmap = None
        self.device_name = ""
        self.image_width = 0
        self.image_height = 0
        self.frame_rate = 0.0
        self.overlay_width = 0  # Overlay rectangle width (in original image pixels)
        self.overlay_height = 0  # Overlay rectangle height (in original image pixels)
        self.roi_enabled = False  # Track if ROI mode is enabled (overlay hidden in ROI mode)
        self._current_image_array = None  # Store current image array for edge detection
        self.detected_roi_x = 0  # Detected ROI position (relative to image)
        self.detected_roi_y = 0
        self.detected_roi_width = 0
        self.detected_roi_height = 0
        self.show_detected_roi = False  # Whether to show detected ROI overlay
        # Store detected edge positions in image coordinates
        # Can be either a single coordinate (axis-aligned) or a line tuple (x1, y1, x2, y2) for angle-tolerant
        self.detected_top_edge = None
        self.detected_bottom_edge = None
        self.detected_left_edge = None
        self.detected_right_edge = None
        self.use_line_detection_display = False  # Track if we're using line detection for display
        # Store FL detected corners/rectangle vertices (for fluorescent channel only)
        self.fl_detected_corners = None  # numpy array of 4 corners: top-left, top-right, bottom-right, bottom-left
        # Store BF detected corners/rectangle vertices (for brightfield channel only)
        self.bf_detected_corners = None  # numpy array of 4 corners: top-left, top-right, bottom-right, bottom-left
        self.target_frame_rate = 0.0  # Added for safety monitoring
        self.overexposed = False  # Track if any pixels are at max value
    
    def set_image(self, image_array, device_name="", frame_rate=0.0, original_width=None, original_height=None, overlay_width=0, overlay_height=0, roi_enabled=False, target_frame_rate=0.0):
        """Set the image from a numpy array with optional metadata."""
        if image_array is None or image_array.size == 0:
            return
        
        # Store metadata
        self.device_name = device_name
        self.frame_rate = frame_rate
        self.target_frame_rate = target_frame_rate
        
        # Detect overexposure (pixels at max value 255)
        # This is fast enough to do on every frame for typical ROI sizes
        try:
            self.overexposed = np.any(image_array == 255)
        except:
            self.overexposed = False
        # Use original dimensions if provided (for ROI mode), otherwise use current image dimensions
        if original_width is not None and original_height is not None:
            self.image_width = original_width
            self.image_height = original_height
        else:
            self.image_height, self.image_width = image_array.shape[:2]
        
        # Store overlay dimensions and ROI state
        self.overlay_width = overlay_width
        self.overlay_height = overlay_height
        self.roi_enabled = roi_enabled
        
        # Store image array only when needed for ROI detection or statistics
        # Check if parent widget (ImageControlWidget) needs the array
        parent_widget = self.parent()
        # Walk up the parent chain to find ImageControlWidget (check by class name to avoid forward reference)
        control_widget = parent_widget
        while control_widget:
            if control_widget.__class__.__name__ == 'ImageControlWidget':
                break
            control_widget = control_widget.parent()
        
        needs_image_array = False
        if control_widget:
            # Check if ROI detection is enabled
            if hasattr(control_widget, 'roi_detection_enabled'):
                needs_image_array = control_widget.roi_detection_enabled
            # Check if statistics timer is active
            if hasattr(control_widget, 'roi_stats_update_timer'):
                if control_widget.roi_stats_update_timer.isActive():
                    needs_image_array = True
        
        if needs_image_array and image_array is not None:
            # Only copy if array is not contiguous or if we need to preserve it
            if image_array.flags['C_CONTIGUOUS'] and image_array.dtype == np.uint8:
                # The CameraThread already provides a safe, independent copy of the numpy array
                self._current_image_array = image_array
            else:
                # Make a copy and ensure it's contiguous
                self._current_image_array = np.ascontiguousarray(image_array.astype(np.uint8))
        else:
            # Clear old array to free memory
            self._current_image_array = None
        
        # Ensure array is contiguous and has correct dtype for QImage
        # Only convert if necessary to avoid unnecessary copies
        if image_array.dtype != np.uint8:
            image_array = image_array.astype(np.uint8)
        elif not image_array.flags['C_CONTIGUOUS']:
            image_array = np.ascontiguousarray(image_array)
        
        # Convert numpy array to QImage
        height, width = image_array.shape[:2]
        
        # Handle different image formats
        # QImage can reference the data directly if it's contiguous, but we need to ensure
        # the data persists, so we create a copy for QImage (required for thread safety)
        if len(image_array.shape) == 2:  # Grayscale
            bytes_per_line = width
            q_image = QImage(image_array.data, width, height, bytes_per_line, QImage.Format.Format_Grayscale8).copy()
        elif len(image_array.shape) == 3:  # Color
            if image_array.shape[2] == 3:  # RGB
                bytes_per_line = width * 3
                q_image = QImage(image_array.data, width, height, bytes_per_line, QImage.Format.Format_RGB888).copy()
            elif image_array.shape[2] == 4:  # RGBA
                bytes_per_line = width * 4
                q_image = QImage(image_array.data, width, height, bytes_per_line, QImage.Format.Format_RGBA8888).copy()
            else:
                return
        else:
            return
        
        # Convert to QPixmap (store original without overlay)
        pixmap = QPixmap.fromImage(q_image)
        
        # Store the pixmap and trigger a repaint
        self._pixmap = pixmap
        self.update()
    
    def paintEvent(self, event):
        """Override paintEvent to scale pixmap while maintaining aspect ratio and draw overlay."""
        if self._pixmap is None:
            super().paintEvent(event)
            return
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Get label size
        label_width = self.width()
        label_height = self.height()
        
        # Get pixmap size
        pixmap_width = self._pixmap.width()
        pixmap_height = self._pixmap.height()
        
        # Calculate scaling factor to fit pixmap in label while maintaining aspect ratio
        scale_x = label_width / pixmap_width
        scale_y = label_height / pixmap_height
        scale = min(scale_x, scale_y)  # Use the smaller scale to ensure it fits
        
        # Calculate scaled dimensions
        scaled_width = int(pixmap_width * scale)
        scaled_height = int(pixmap_height * scale)
        
        # Center the scaled pixmap
        x = (label_width - scaled_width) // 2
        y = (label_height - scaled_height) // 2
        
        # Draw the scaled pixmap
        scaled_pixmap = self._pixmap.scaled(
            scaled_width, scaled_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        painter.drawPixmap(x, y, scaled_pixmap)
        
        # Draw overlay text on top of the scaled image (not rasterized into image)
        # Prepare text
        status_info = f"{self.device_name} | {self.image_width}x{self.image_height} | {self.frame_rate:.1f} fps"
        
        # Check for safety warnings
        warnings = []
        if self.roi_enabled and self.target_frame_rate > 0:
            # Threshold: 95% of target
            if self.frame_rate < 0.95 * self.target_frame_rate:
                warnings.append("Warning - unable to reach target frame rate, reduce frame rate or exposure!")
        
        if self.overexposed:
            warnings.append("Warning - Overexposed pixels detected, reduce exposure!")

        unstable = len(warnings) > 0
        if unstable:
            # ONLY show warning messages when in a warning state
            info_text = "\n".join(warnings)
        else:
            info_text = status_info
        
        # Set font - use larger size if there are warnings
        font_size = 14 if unstable else 10
        font = QFont("Arial", font_size, QFont.Weight.Bold)
        painter.setFont(font)
        
        # Calculate text dimensions and position relative to label area
        font_metrics = painter.fontMetrics()
        text_rect = font_metrics.boundingRect(info_text)
        padding = 5
        
        # Position overlay at bottom left of available label area (not scaled image)
        # This places it in unused space when there's excess vertical space
        bg_x = padding
        bg_y = label_height - text_rect.height() - padding * 2
        bg_width = text_rect.width() + padding * 2
        bg_height = text_rect.height() + padding * 2
        
        # Draw semi-transparent background for text
        if unstable:
            painter.fillRect(bg_x, bg_y, bg_width, bg_height, QColor(200, 0, 0, 180)) # Red background for warning
        else:
            painter.fillRect(bg_x, bg_y, bg_width, bg_height, QColor(0, 0, 0, 180))
        
        # Draw text (adjust y position for proper text baseline)
        painter.setPen(QColor(255, 255, 255))
        text_y = bg_y + padding + font_metrics.ascent()
        painter.drawText(bg_x + padding, text_y, info_text)
        
        # Draw overlay rectangle if dimensions are set and ROI mode is disabled
        if not self.roi_enabled and self.overlay_width > 0 and self.overlay_height > 0:
            # Calculate overlay rectangle size in scaled image coordinates
            # The overlay dimensions are in original image pixels, so scale them
            overlay_scaled_width = int(self.overlay_width * scale)
            overlay_scaled_height = int(self.overlay_height * scale)
            
            # Center the rectangle in the scaled image
            overlay_x = x + (scaled_width - overlay_scaled_width) // 2
            overlay_y = y + (scaled_height - overlay_scaled_height) // 2
            
            # Draw rectangle with semi-transparent border
            painter.setPen(QColor(0, 255, 0, 200))  # Green with transparency
            painter.setBrush(QColor(0, 0, 0, 0))  # Transparent fill
            painter.drawRect(overlay_x, overlay_y, overlay_scaled_width, overlay_scaled_height)
        
        # Draw detected edge lines if enabled
        if self.show_detected_roi:
            painter.setPen(QColor(255, 0, 0, 255))  # Solid red
            painter.setBrush(QColor(0, 0, 0, 0))  # Transparent fill
            
            # Check if we're using line detection (lines are tuples) or axis-aligned (single coordinate)
            if self.use_line_detection_display:
                # Draw lines (angle-tolerant mode)
                if self.detected_top_edge is not None and isinstance(self.detected_top_edge, tuple):
                    x1, y1, x2, y2 = self.detected_top_edge
                    x1_scaled = x + int(x1 * scale)
                    y1_scaled = y + int(y1 * scale)
                    x2_scaled = x + int(x2 * scale)
                    y2_scaled = y + int(y2 * scale)
                    painter.drawLine(x1_scaled, y1_scaled, x2_scaled, y2_scaled)
                
                if self.detected_bottom_edge is not None and isinstance(self.detected_bottom_edge, tuple):
                    x1, y1, x2, y2 = self.detected_bottom_edge
                    x1_scaled = x + int(x1 * scale)
                    y1_scaled = y + int(y1 * scale)
                    x2_scaled = x + int(x2 * scale)
                    y2_scaled = y + int(y2 * scale)
                    painter.drawLine(x1_scaled, y1_scaled, x2_scaled, y2_scaled)
                
                # Skip left edge in ROI mode (it's the center line, not an edge to display)
                if self.detected_left_edge is not None and isinstance(self.detected_left_edge, tuple) and not self.roi_enabled:
                    x1, y1, x2, y2 = self.detected_left_edge
                    x1_scaled = x + int(x1 * scale)
                    y1_scaled = y + int(y1 * scale)
                    x2_scaled = x + int(x2 * scale)
                    y2_scaled = y + int(y2 * scale)
                    painter.drawLine(x1_scaled, y1_scaled, x2_scaled, y2_scaled)
                
                if self.detected_right_edge is not None and isinstance(self.detected_right_edge, tuple):
                    x1, y1, x2, y2 = self.detected_right_edge
                    x1_scaled = x + int(x1 * scale)
                    y1_scaled = y + int(y1 * scale)
                    x2_scaled = x + int(x2 * scale)
                    y2_scaled = y + int(y2 * scale)
                    painter.drawLine(x1_scaled, y1_scaled, x2_scaled, y2_scaled)
            else:
                # Draw axis-aligned edges (single coordinate mode)
                if self.detected_top_edge is not None and not isinstance(self.detected_top_edge, tuple):
                    edge_y_scaled = y + int(self.detected_top_edge * scale)
                    painter.drawLine(x, edge_y_scaled, x + scaled_width, edge_y_scaled)
                
                if self.detected_bottom_edge is not None and not isinstance(self.detected_bottom_edge, tuple):
                    edge_y_scaled = y + int(self.detected_bottom_edge * scale)
                    painter.drawLine(x, edge_y_scaled, x + scaled_width, edge_y_scaled)
                
                if self.detected_left_edge is not None and not isinstance(self.detected_left_edge, tuple):
                    edge_x_scaled = x + int(self.detected_left_edge * scale)
                    painter.drawLine(edge_x_scaled, y, edge_x_scaled, y + scaled_height)
                
                if self.detected_right_edge is not None and not isinstance(self.detected_right_edge, tuple):
                    edge_x_scaled = x + int(self.detected_right_edge * scale)
                    painter.drawLine(edge_x_scaled, y, edge_x_scaled, y + scaled_height)
            
            # Draw FL detected corners overlay (for fluorescent channel)
            if self.fl_detected_corners is not None and len(self.fl_detected_corners) == 4:
                painter.setPen(QColor(255, 0, 255, 255))  # Magenta
                painter.setBrush(QColor(0, 0, 0, 0))  # Transparent fill (no fill)
                
                # Draw rectangle connecting the four corners
                corners_scaled = []
                for corner in self.fl_detected_corners:
                    corner_x_scaled = x + int(corner[0] * scale)
                    corner_y_scaled = y + int(corner[1] * scale)
                    corners_scaled.append((corner_x_scaled, corner_y_scaled))
                
                # Draw lines connecting corners to form rectangle
                for i in range(4):
                    x1, y1 = corners_scaled[i]
                    x2, y2 = corners_scaled[(i + 1) % 4]
                    painter.drawLine(x1, y1, x2, y2)
        
        painter.end()


class ExposureController:
    """Controller for automatic exposure adjustment based on image statistics."""
    
    def __init__(self, target_value=190, max_pixel_limit=254, damp_factor=0.05, downscale_factor=0.75):
        """
        Initialize exposure controller.
        
        :param target_value: Desired mode pixel intensity (0-255).
        :param max_pixel_limit: Threshold to trigger rapid reduction (usually 254 or 255).
        :param damp_factor: Limits exposure increase rate (0.05 = max 5% increase per step).
        :param downscale_factor: Multiplier for rapid reduction when clipping (0.75 = 25% drop).
        """
        self.target_value = target_value
        self.limit = max_pixel_limit
        self.damp = damp_factor
        self.downscale = downscale_factor
        self.deadband = 5  # Allow mode to fluctuate +/- 5 units without changing exposure

    def _calculate_mode(self, image: np.ndarray, use_otsu_threshold: bool = False) -> float:
        """
        Calculate mode pixel value from image array.
        
        Args:
            image: Image array (numpy array)
            use_otsu_threshold: If True, calculate mode of pixels above Otsu's threshold (for FL channel)
            
        Returns:
            Mode pixel value (float)
        """
        if image.size == 0:
            return 0.0
        
        # Flatten image to 1D array
        pixels = image.flatten()
        
        # Apply Otsu's threshold if requested
        if use_otsu_threshold:
            if not CV2_AVAILABLE:
                # Fallback to non-zero pixels if OpenCV not available
                pixels = pixels[pixels > 0]
                if pixels.size == 0:
                    return 0.0
            else:
                # Reshape to 2D for cv2.threshold (needs 2D array)
                # Get original shape if possible, otherwise use square approximation
                img_2d = image
                if len(image.shape) == 1:
                    # If already flattened, reshape to approximate square
                    size = int(np.sqrt(image.size))
                    if size * size != image.size:
                        # Not a perfect square, pad with zeros
                        size = int(np.ceil(np.sqrt(image.size)))
                        padded = np.zeros(size * size, dtype=image.dtype)
                        padded[:image.size] = image
                        img_2d = padded.reshape(size, size)
                    else:
                        img_2d = image.reshape(size, size)
                
                # Calculate Otsu's threshold
                try:
                    threshold_val, _ = cv2.threshold(
                        img_2d.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                    )
                    # Filter pixels above threshold
                    pixels = pixels[pixels > threshold_val]
                    if pixels.size == 0:
                        return 0.0
                except Exception:
                    # Fallback to non-zero pixels on error
                    pixels = pixels[pixels > 0]
                    if pixels.size == 0:
                        return 0.0
        
        # Calculate mode using numpy
        # Use bincount for integer pixel values (0-255)
        counts = np.bincount(pixels.astype(np.int32))
        if counts.size == 0:
            return 0.0
        
        mode_value = np.argmax(counts)
        return float(mode_value)

    def calculate_next_exposure(self, image: np.ndarray, current_exp: float, use_otsu_threshold: bool = False) -> float:
        """
        Calculates the next exposure setting based on image statistics.
        
        Args:
            image: Image array (numpy array)
            current_exp: Current exposure time in microseconds
            use_otsu_threshold: If True, calculate mode of pixels above Otsu's threshold (for FL channel)
            
        Returns:
            New exposure time in microseconds
        """
        # 1. Image Statistics
        # Note: If ROI is needed, slice the image before passing it here.
        img_max = np.max(image)
        img_mode = self._calculate_mode(image, use_otsu_threshold=use_otsu_threshold)

        # 2. Priority 1: Instant Clipping Reduction
        # If any pixel is saturated, drop exposure immediately.
        if img_max >= self.limit:
            return current_exp * self.downscale

        # 3. Priority 3: Stability (Deadband)
        # If mode is within acceptable range, do not change exposure.
        if abs(img_mode - self.target_value) < self.deadband:
            return current_exp

        # 4. Priority 2 & 4: Linear Adjustment with Rate Limiting
        # Avoid division by zero
        if img_mode < 1.0:
            img_mode = 1.0
            
        # Calculate ideal exposure based on linear relationship
        ratio = self.target_value / img_mode
        
        # Dampen the increase to prevent oscillation/overshoot
        # We limit the ratio to a max step size (e.g., cannot increase more than 5% at once)
        if ratio > 1:
            ratio = min(ratio, 1.0 + self.damp)
        else:
            # For decreasing exposure (without clipping), we can allow faster movement 
            # or apply symmetric damping depending on preference. 
            # Here we allow linear drop.
            pass 

        new_exp = current_exp * ratio
        
        return new_exp


class ImageControlWidget(QWidget):
    """Embeddable widget for Image control."""
    image_saving_toggled = Signal(bool)
    roi_mode_toggled = Signal(bool)
    camera_mode_changed = Signal(str)
    ui_callback_signal = Signal(object, str, tuple, dict)
    
    def __init__(self, parent=None, smr_widget=None):
        super().__init__(parent)
        self.smr_widget = smr_widget
        self.cameras = []
        self.camera_threads = []
        self.camera_initialized = False
        self.roi_enabled = False
        self.roi_width = 640
        self.roi_height = 480
        self.overlay_width = 900  # Overlay rectangle width in pixels
        self.overlay_height = 160  # Overlay rectangle height in pixels
        self.full_image_size = None  # Store full image dimensions (height, width)
        self.camera_max_width = {}  # Store max width for each camera
        self.camera_max_height = {}  # Store max height for each camera
        self.camera_original_settings = {}  # Store original camera settings
        self.camera_channels = {}  # Map camera index to channel (brightfield/fluorescent)
        self.camera_settings = {}  # Store camera settings from config
        self.available_cameras = []  # List of available camera device names
        self.camera_serial_map = {}  # Map camera serial number to device index
        # NOTE: Config files are READ-ONLY. This code should never write to them.
        self.config_file = os.path.join(REFERENCES_DIR, 'camera_config.txt')
        from helper_functions.SYSTEM_pull_config_io import SYSTEM_CONFIG_PATH
        self.system_config_file = SYSTEM_CONFIG_PATH
        self.camera_mode = "BF+FL"  # Camera mode: "BF only" or "BF+FL"
        self.condition_running = False  # Track if pyPump is actively running a condition or "BF+FL"
        self.roi_detection_enabled = False  # Track if ROI detection is active
        self.roi_detection_timer = QTimer(self)  # Timer for throttling ROI detection
        self.roi_detection_timer.timeout.connect(self._perform_roi_detection)
        self.roi_detection_timer.setInterval(100)

        # Async ROI detection setup
        self._roi_thread_active = {'brightfield': False, 'fluorescent': False}
        self.ui_callback_signal.connect(self._run_ui_callback, Qt.ConnectionType.QueuedConnection)
        from concurrent.futures import ThreadPoolExecutor
        self.roi_thread_pool = ThreadPoolExecutor(max_workers=2)
  # 100ms interval
        self.last_detection_time = 0  # Track last detection time
        # Edge detection parameters
        self.edge_threshold = 20.0  # Minimum intensity drop to consider an edge
        self.min_brightness_diff = 10.0  # Minimum brightness difference between light and dark regions
        self.use_line_detection = True  # Use derivative-based corner detection for angle tolerance
        # Derivative-based detection parameters
        self.vertical_line_threshold = None  # Additional threshold for vertical line detection stability (None = use edge_threshold)
        self.corner_search_width = 20  # Width of search region along vertical edges for corner detection
        self.center_exclusion_percent = 40.0  # Percentage of center region to exclude from vertical edge detection
        # Vertical edge detection history for smoothing (separate for each channel)
        self.bf_left_edge_history = []  # History of last 10 left edge positions for brightfield (in overlay coordinates)
        self.bf_right_edge_history = []  # History of last 10 right edge positions for brightfield (in overlay coordinates)
        self.fl_left_edge_history = []  # History of last 10 left edge positions for fluorescent (in overlay coordinates)
        self.fl_right_edge_history = []  # History of last 10 right edge positions for fluorescent (in overlay coordinates)
        self.edge_history_size = 10  # Number of detections to remember
        # ROI detection console reporting
        self.report_roi_heuristics = False  # Whether to print ROI detection heuristics to console
        # LED control settings
        self.daq_name = "Dev1"  # DAQ device name
        self.red_led_address = "port0/line1"  # Red LED channel address
        self.blue_led_address = "port0/line2"  # Blue LED channel address
        self.red_led_on_state = True  # True if True turns red LED on, False if False turns red LED on
        self.blue_led_on_state = True  # True if True turns blue LED on, False if False turns blue LED on
        self.red_led_state = False  # Current red LED state
        self.blue_led_state = False  # Current blue LED state
        # Analog input settings for FL driver current measurement
        self.fl_driver_current_address = "ai3"  # Analog input channel for differential voltage reading (physical address)
        self.fl_driver_current_update_timer = QTimer(self)  # Timer for periodic FL driver current reading
        self.fl_driver_current_update_timer.timeout.connect(self._update_fl_driver_current)
        self.fl_driver_current_update_timer.setInterval(100)  # Update every 100ms
        # Photodiode polling timer
        self.photodiode_update_timer = QTimer(self)  # Timer for periodic photodiode reading
        self.photodiode_update_timer.timeout.connect(self._update_photodiode)
        self.photodiode_update_timer.setInterval(100)  # Update every 100ms
        # Track most recent values for metadata saving
        self.last_bf_exposure_us = 0.0  # Most recent BF exposure time in microseconds
        self.last_fl_exposure_us = 0.0  # Most recent FL exposure time in microseconds
        self.last_blue_led_current_a = 0.0  # Most recent blue LED current in Amperes
        self.last_photodiode_voltage_v = 0.0  # Most recent photodiode voltage in Volts
        # ROI statistics update timer
        self.roi_stats_update_timer = QTimer(self)  # Timer for periodic ROI statistics
        self.roi_stats_update_timer.timeout.connect(self._update_roi_statistics)
        self.roi_stats_update_timer.setInterval(100)  # Update every 100ms
        # FL auto exposure settings
        self.fl_auto_exposure_enabled = False  # Track auto exposure state
        self.fl_auto_exposure_timer = QTimer(self)  # Timer for throttling exposure updates (100ms)
        self.fl_auto_exposure_timer.timeout.connect(self._update_fl_auto_exposure)
        self.fl_auto_exposure_timer.setInterval(100)  # Update every 100ms
        self.last_auto_exposure_update_time = 0  # Track last update time
        self.fl_auto_exposure_min = 30.0  # Min exposure from config (default 30 µs)
        self.fl_auto_exposure_max = 250.0  # Max exposure from config (default 250 µs)
        self.fl_auto_exposure_target_value = 190.0  # Target value from config (default 190)
        self.fl_auto_exposure_step_size = 5.0  # Step size for exposure adjustments (5 µs)
        self.last_fl_ae_action = None  # State tracking: 'increase', 'decrease', or None
        # Bit depth tracking for accurate saturation detection and statistics
        self.camera_bit_depths = {'brightfield': 8, 'fluorescent': 8}
        # Camera trigger settings for ROI mode
        self.bf_roi_framerate = 1600.0  # Default BF frame rate in Hz
        self.bffl_roi_framerate = 1250.0  # Default BF+FL frame rate in Hz
        self.roi_framerate = 1250.0  # Current active frame rate
        self.camera_trigger_address = None  # DAQ analog output address for camera trigger
        self.trigger_task = None  # nidaqmx.Task for square wave generation
        # Photodiode settings
        self.photodiode_address = None  # DAQ analog input address for photodiode differential voltage reading
        # Display frame rate settings
        # Load display_framerate from config early so spinbox gets correct default
        self.display_framerate = self._load_display_framerate_from_config()
        self.last_display_update_time = {}  # Per-camera tracking for display throttling
        # Frame difference calculation state variables
        self.previous_brightfield_frame = None  # Store previous brightfield frame
        self.frame_difference_offset = 10.0  # Offset parameter (default 10)
        self.frame_difference_threshold = 1000.0  # Threshold for triggering (default 1000, will be loaded from config)
        self.image_saving_enabled = False  # Enable/disable image saving
        self.frame_number = 0  # Track frame number for filename
        self.current_frame_difference = 0.0  # Current frame difference value for display
        self.last_fluorescent_frame = None  # Store last fluorescent frame for saving when triggered (deprecated - use frame_buffer)
        # Frame buffer for matching frames by image_number
        self.frame_buffer = {}  # Dict: {image_number: {'brightfield': (image, metadata), 'fluorescent': (image, metadata)}}
        self.frame_buffer_max_age = 1250  # Maximum frames to keep in buffer (for matching)
        # Preceding frames buffer for saving 4 non-triggered frames before a trigger
        self.preceding_frames_buffer = []  # List of tuples: (image_number, bf_frame, bf_metadata, fl_frame, fl_metadata)
        self.saved_image_numbers = {}  # Track which image_numbers have been saved to prevent duplicates (using dict for ordered O(1) removals)
        self.total_saved_frames = 0  # Total number of frames saved in current experiment
        # Image saving buffer for binary file writing
        self.image_saving_buffer = None  # ImageSavingBuffer instance
        # Frame difference plot data
        self.frame_diff_plot_data = []  # Store frame difference values for plotting
        self.frame_diff_plot_times = []  # Store timestamps for plotting
        self.frame_diff_max_points = 200  # Maximum number of points to keep in plot
        self.setup_ui()
        # Load config after UI is set up so spin boxes exist
        self.load_config()
        # Set overlay dimensions on display labels after config is loaded
        if hasattr(self, 'camera1_display'):
            self.camera1_display.overlay_width = self.overlay_width
            self.camera1_display.overlay_height = self.overlay_height
        if hasattr(self, 'camera2_display'):
            self.camera2_display.overlay_width = self.overlay_width
            self.camera2_display.overlay_height = self.overlay_height
        # Populate camera dropdowns after UI is set up
        self._populate_camera_dropdowns()
    
    def set_smr_widget(self, smr_widget):
        """Set the SMR widget reference."""
        self.smr_widget = smr_widget
    
    def get_sample_path(self):
        """Get the sample path from SMR widget."""
        if self.smr_widget and hasattr(self.smr_widget, 'selected_sample_path'):
            return self.smr_widget.selected_sample_path
        return None
    
    def get_experiment_string(self):
        """Get the experiment string from SMR widget."""
        if self.smr_widget and hasattr(self.smr_widget, 'experiment_string'):
            return self.smr_widget.experiment_string
        return None
    
    def _calculate_frame_difference(self, frame_1, frame_0, offset):
        """
        Calculate frame difference score between two frames.
        Uses OpenCV for optimized performance if available.
        """
        if CV2_AVAILABLE:
            # OpenCV's absdiff and subtract (with saturation) are very fast
            diff = cv2.absdiff(frame_1, frame_0)
            diff = cv2.subtract(diff, np.array([offset], dtype=np.uint8))
            
            # Use float32 for squaring to avoid overflow then sqrt sum
            diff_f = diff.astype(np.float32)
            score = np.sqrt(np.sum(diff_f * diff_f))
            return float(score)
            
        # Fallback to numpy implementation if OpenCV is not available
        diff = np.abs(frame_1.astype(np.int16) - frame_0.astype(np.int16)) - offset
        diff = np.clip(diff, 0, 255)
        diff_squared = diff.astype(np.int32) ** 2
        score = np.sum(diff_squared)**0.5
        return float(score)
    
    def _save_non_triggered_frame(self, brightfield_image, brightfield_camera_index, fluorescent_image=None, fluorescent_metadata=None, brightfield_metadata=None):
        """
        Save non-triggered images from both cameras to binary file using the saving buffer.
        This is used for the 4 preceding frames before a trigger.
        
        Args:
            brightfield_image: Brightfield image array
            brightfield_camera_index: Index of brightfield camera
            fluorescent_image: Fluorescent image array (matched by image_number, or None if not available)
            fluorescent_metadata: Fluorescent metadata (matched by image_number, or None if not available)
            brightfield_metadata: Brightfield metadata (if provided, otherwise will try to find from frame_buffer)
        """
        # Check if buffer is initialized
        if self.image_saving_buffer is None:
            # Try to initialize if saving is enabled but buffer isn't created
            if self.image_saving_enabled:
                print("Warning: Image saving buffer not initialized - attempting to initialize...")
                self._initialize_image_saving_buffer()
                # If still None after attempt, print more details
                if self.image_saving_buffer is None:
                    print("  Failed to initialize buffer. Check that:")
                    print("    1. SMR saving has been started (to generate experiment_string)")
                    print("    2. A sample has been selected in pySMR")
            return
        
        # Get metadata for brightfield camera
        bf_metadata = brightfield_metadata
        
        # If not provided, try to find from frame_buffer
        if bf_metadata is None:
            # Try to find metadata from frame_buffer by matching image_number from fluorescent_metadata
            if fluorescent_metadata is not None:
                fl_image_number = fluorescent_metadata.get('image_number')
                if fl_image_number is not None and fl_image_number in self.frame_buffer:
                    if 'brightfield' in self.frame_buffer[fl_image_number]:
                        _, bf_metadata = self.frame_buffer[fl_image_number]['brightfield']
        
        # If still not found, try to get from last_triggered_metadata as fallback
        if bf_metadata is None:
            bf_metadata = self.last_triggered_metadata.get(brightfield_camera_index, {})
        
        # Get FL metadata (use provided or try to find)
        if fluorescent_metadata is None:
            # Try to find FL camera index and get its metadata
            for fl_idx, ch in self.camera_channels.items():
                if ch == 'fluorescent':
                    # Try to find from frame_buffer using bf metadata
                    bf_image_number = bf_metadata.get('image_number')
                    if bf_image_number is not None and bf_image_number in self.frame_buffer:
                        if 'fluorescent' in self.frame_buffer[bf_image_number]:
                            _, fluorescent_metadata = self.frame_buffer[bf_image_number]['fluorescent']
                    # Fall back to last_triggered_metadata
                    if fluorescent_metadata is None:
                        fluorescent_metadata = self.last_triggered_metadata.get(fl_idx, {})
                    break
        
        # Extract values for metadata string
        computer_time = bf_metadata.get('computer_timestamp', time.time())
        bf_camera_time = bf_metadata.get('camera_timestamp', 0)
        bf_frame_number = bf_metadata.get('image_number', 0)
        
        fl_camera_time = fluorescent_metadata.get('camera_timestamp', 0) if fluorescent_metadata else 0
        fl_frame_number = fluorescent_metadata.get('image_number', 0) if fluorescent_metadata else 0
        
        # Trigger flag is 0 for non-triggered frames
        trigger_flag = 0
        
        # Get image dimensions
        bf_height, bf_width = brightfield_image.shape[:2]
        
        # Handle FL dimensions based on camera mode
        if self.camera_mode == "BF only":
            # In BF-only mode, set FL dimensions to 0x0
            fl_height, fl_width = 0, 0
        elif fluorescent_image is not None:
            fl_height, fl_width = fluorescent_image.shape[:2]
        else:
            # If FL image is missing in BF+FL mode, use BF dimensions (will be filled with zeros)
            fl_height, fl_width = bf_height, bf_width
        
        # Metadata tuple: 14 values
        # Format: computer_time, bf_camera_time, bf_frame_number, fl_camera_time, fl_frame_number, 
        #         trigger_flag, bf_width, bf_height, fl_width, fl_height,
        #         bf_exposure_us, fl_exposure_us, blue_led_current_a, photodiode_voltage_v
        bf_exposure_us = self.last_bf_exposure_us
        fl_exposure_us = self.last_fl_exposure_us
        blue_led_current_a = self.last_blue_led_current_a
        photodiode_voltage_v = self.last_photodiode_voltage_v
        
        metadata_tuple = (
            computer_time, bf_camera_time, bf_frame_number, fl_camera_time, fl_frame_number, 
            trigger_flag, bf_width, bf_height, fl_width, fl_height,
            f"{bf_exposure_us:.1f}", f"{fl_exposure_us:.1f}", f"{blue_led_current_a:.3f}", f"{photodiode_voltage_v:.3f}"
        )
        
        try:
            # Add frame to buffer (pass bf_only_mode flag)
            bf_only_mode = (self.camera_mode == "BF only")
            self.image_saving_buffer.add_frame(metadata_tuple, brightfield_image, fluorescent_image, bf_only_mode=bf_only_mode)
            
            # Track that this image_number has been saved
            if bf_frame_number is not None and bf_frame_number != 0:
                if bf_frame_number not in self.saved_image_numbers:
                    self.saved_image_numbers[bf_frame_number] = None
                    self.total_saved_frames += 1
                
                # Prevent saved_image_numbers from unbounded growth
                if len(self.saved_image_numbers) > 2000:
                    # Remove the oldest 500 image_numbers (O(1) pop per item instead of sorting)
                    for _ in range(500):
                        self.saved_image_numbers.pop(next(iter(self.saved_image_numbers)))
                        
                self._update_saved_frames_count()
        except Exception as e:
            print(f"Error saving non-triggered frame to buffer: {e}")
            import traceback
            traceback.print_exc()
    
    def _save_triggered_images(self, brightfield_image, brightfield_camera_index, fluorescent_image=None, fluorescent_metadata=None):
        """
        Save triggered images from both cameras to binary file using the saving buffer.
        
        Args:
            brightfield_image: Brightfield image array
            brightfield_camera_index: Index of brightfield camera
            fluorescent_image: Fluorescent image array (matched by image_number, or None if not available)
            fluorescent_metadata: Fluorescent metadata (matched by image_number, or None if not available)
        """
        # Check if buffer is initialized
        if self.image_saving_buffer is None:
            # Try to initialize if saving is enabled but buffer isn't created
            if self.image_saving_enabled:
                print("Warning: Image saving buffer not initialized - attempting to initialize...")
                self._initialize_image_saving_buffer()
                # If still None after attempt, print more details
                if self.image_saving_buffer is None:
                    print("  Failed to initialize buffer. Check that:")
                    print("    1. SMR saving has been started (to generate experiment_string)")
                    print("    2. A sample has been selected in pySMR")
            return
        
        # Get metadata for both cameras
        bf_metadata = self.last_triggered_metadata.get(brightfield_camera_index, {})
        
        # Get FL metadata (use provided or from last_triggered_metadata)
        if fluorescent_metadata is None:
            # Try to find FL camera index and get its metadata
            for fl_idx, ch in self.camera_channels.items():
                if ch == 'fluorescent':
                    fluorescent_metadata = self.last_triggered_metadata.get(fl_idx, {})
                    break
        
        # Extract values for metadata string
        computer_time = bf_metadata.get('computer_timestamp', time.time())
        bf_camera_time = bf_metadata.get('camera_timestamp', 0)
        bf_frame_number = bf_metadata.get('image_number', 0)
        
        fl_camera_time = fluorescent_metadata.get('camera_timestamp', 0) if fluorescent_metadata else 0
        fl_frame_number = fluorescent_metadata.get('image_number', 0) if fluorescent_metadata else 0
        
        # Trigger flag is always 1 since only triggered frames are saved
        trigger_flag = 1
        
        # Get image dimensions
        bf_height, bf_width = brightfield_image.shape[:2]
        
        # Handle FL dimensions based on camera mode
        if self.camera_mode == "BF only":
            # In BF-only mode, set FL dimensions to 0x0
            fl_height, fl_width = 0, 0
        elif fluorescent_image is not None:
            fl_height, fl_width = fluorescent_image.shape[:2]
        else:
            # If FL image is missing in BF+FL mode, use BF dimensions (will be filled with zeros)
            fl_height, fl_width = bf_height, bf_width
        
        # Metadata tuple: 14 values
        # Format: computer_time, bf_camera_time, bf_frame_number, fl_camera_time, fl_frame_number, 
        #         trigger_flag, bf_width, bf_height, fl_width, fl_height,
        #         bf_exposure_us, fl_exposure_us, blue_led_current_a, photodiode_voltage_v
        bf_exposure_us = self.last_bf_exposure_us
        fl_exposure_us = self.last_fl_exposure_us
        blue_led_current_a = self.last_blue_led_current_a
        photodiode_voltage_v = self.last_photodiode_voltage_v
        
        metadata_tuple = (
            computer_time, bf_camera_time, bf_frame_number, fl_camera_time, fl_frame_number, 
            trigger_flag, bf_width, bf_height, fl_width, fl_height,
            f"{bf_exposure_us:.1f}", f"{fl_exposure_us:.1f}", f"{blue_led_current_a:.3f}", f"{photodiode_voltage_v:.3f}"
        )
        
        try:
            # Add frame to buffer (pass bf_only_mode flag)
            bf_only_mode = (self.camera_mode == "BF only")
            self.image_saving_buffer.add_frame(metadata_tuple, brightfield_image, fluorescent_image, bf_only_mode=bf_only_mode)
            
            # Track that this image_number has been saved
            if bf_frame_number is not None and bf_frame_number != 0:
                if bf_frame_number not in self.saved_image_numbers:
                    self.saved_image_numbers[bf_frame_number] = None
                    self.total_saved_frames += 1
                
                # Prevent saved_image_numbers from unbounded growth
                # Optimization: Only cleanup if dict exceeds 5000 entries
                if len(self.saved_image_numbers) > 5000:
                    # Remove the oldest 2000 image_numbers (O(1) pop per item instead of sorting)
                    for _ in range(2000):
                        self.saved_image_numbers.pop(next(iter(self.saved_image_numbers)))
                
                self._update_saved_frames_count()
        except Exception as e:
            print(f"Error saving frame to buffer: {e}")
            import traceback
            traceback.print_exc()
    
    def toggle_image_saving(self, checked):
        """Toggle image saving on/off."""
        self.image_saving_enabled = checked
        self.image_saving_toggled.emit(checked)
        if checked:
            # Initialize buffer when enabling
            self._initialize_image_saving_buffer()
        else:
            # Cleanup buffer when disabling
            self._cleanup_image_saving_buffer()
    
    def toggle_fl_auto_exposure(self, checked):
        """Toggle FL auto exposure on/off."""
        self.fl_auto_exposure_enabled = checked
        if checked:
            # Reset state action tracking for fresh run
            self.last_fl_ae_action = None
            # Start timer if cameras are initialized
            if self.camera_initialized:
                self.fl_auto_exposure_timer.start()
        else:
            # Stop timer when disabling
            if self.fl_auto_exposure_timer.isActive():
                self.fl_auto_exposure_timer.stop()
    
    def _update_saved_frames_count(self):
        """Update the saved frames count display (throttled)."""
        # We set a flag or let a timer update it instead to avoid flooding the UI thread 
        self._frames_count_needs_update = True
    
    def on_frame_diff_offset_changed(self, value):
        """Update frame difference offset parameter."""
        self.frame_difference_offset = float(value)
    
    def on_frame_diff_threshold_changed(self, value):
        """Update frame difference threshold parameter."""
        self.frame_difference_threshold = float(value)
        # Update plot to show new threshold
        self._update_frame_diff_plot()
    
    def _reset_frame_difference_state(self):
        """Reset frame difference calculation state."""
        self.previous_brightfield_frame = None
        self.last_fluorescent_frame = None
        self.frame_number = 0
        self.current_frame_difference = 0.0
        self.frame_diff_plot_data = []
        self.frame_diff_plot_times = []
        self.last_triggered_metadata = {}
        self.frame_buffer = {}  # Clear frame buffer on reset
        self.preceding_frames_buffer = []  # Clear preceding frames buffer on reset
        self.saved_image_numbers.clear()  # Clear saved image numbers tracking on reset
        self.total_saved_frames = 0
        
        # Virtual trigger count tracking for robust synchronization
        self.virtual_trigger_count = {}  # Dict: {camera_index: current_virtual_count}
        self.last_camera_timestamp = {}  # Dict: {camera_index: last_hardware_timestamp_ns}
        
        self._update_saved_frames_count()  # Update display to show 0
    
    def show_triggered_metadata(self):
        """Show dialog with metadata for the most recent triggered frame."""
        dialog = TriggeredMetadataDialog(self.last_triggered_metadata, self.camera_channels, self.camera_device_names, self)
        dialog.exec()
    
    def _initialize_image_saving_buffer(self):
        """Initialize the image saving buffer."""
        # Get experiment string from SMR widget
        experiment_string = self.get_experiment_string()
        if not experiment_string:
            print("Warning: Cannot initialize image saving buffer - experiment_string not available")
            print("  Make sure SMR saving has been started to generate experiment_string")
            self.image_saving_enabled = False
            if hasattr(self, 'enable_saving_checkbox'):
                self.enable_saving_checkbox.setChecked(False)
            return
        
        # Get sample path (same location as frequency binary files)
        sample_path = self.get_sample_path()
        if not sample_path:
            print("Warning: Cannot initialize image saving buffer - sample path not available")
            print("  Make sure a sample has been selected in pySMR")
            self.image_saving_enabled = False
            if hasattr(self, 'enable_saving_checkbox'):
                self.enable_saving_checkbox.setChecked(False)
            return
        
        # Verify the path exists and is accessible
        if not os.path.exists(sample_path):
            print(f"Warning: Sample path does not exist: {sample_path}")
            try:
                os.makedirs(sample_path, exist_ok=True)
                print(f"Created sample path: {sample_path}")
            except Exception as e:
                print(f"Error creating sample path: {e}")
                self.image_saving_enabled = False
                if hasattr(self, 'enable_saving_checkbox'):
                    self.enable_saving_checkbox.setChecked(False)
                return
        
        try:
            # Determine file suffix and max frames based on camera mode
            if self.camera_mode == "BF only":
                file_suffix = "Images"
                max_frames = 700000  # 700k frames for BF-only mode (~50GB)
            else:
                file_suffix = "VolumeImages"
                max_frames = 350000  # 350k frame pairs for BF+FL mode (~50GB)
            
            # Create buffer instance (saves to same location as frequency binary)
            self.image_saving_buffer = ImageSavingBuffer(experiment_string, sample_path, file_suffix=file_suffix, max_frames_per_file=max_frames)
            # Reset saved frames count for new experiment
            self.saved_image_numbers.clear()
            self.total_saved_frames = 0
            self._update_saved_frames_count()
            print(f"Initialized image saving buffer: {experiment_string}")
            print(f"  Saving to: {os.path.abspath(sample_path)}")
            print(f"  Files will be: {experiment_string}_{file_suffix}_###.bin")
            print(f"  Max frames per file: {max_frames:,}")
        except Exception as e:
            print(f"Error initializing image saving buffer: {e}")
            import traceback
            traceback.print_exc()
            self.image_saving_enabled = False
            if hasattr(self, 'enable_saving_checkbox'):
                self.enable_saving_checkbox.setChecked(False)
            self.image_saving_buffer = None
    
    def _cleanup_image_saving_buffer(self):
        """Cleanup and close the image saving buffer."""
        if self.image_saving_buffer is not None:
            try:
                self.image_saving_buffer.flush()
                self.image_saving_buffer.close()
                print("Closed image saving buffer")
            except Exception as e:
                print(f"Error closing image saving buffer: {e}")
            finally:
                self.image_saving_buffer = None
                # Reset saved frames count when buffer is closed
                self.saved_image_numbers.clear()
                self.total_saved_frames = 0
                self._update_saved_frames_count()
    
    def _update_frame_diff_plot(self):
        """Update the frame difference plot with current value and threshold."""
        try:
            current_time = time.time()
            
            # Add current data point
            self.frame_diff_plot_data.append(self.current_frame_difference)
            self.frame_diff_plot_times.append(current_time)
            
            # Limit data to max_points
            if len(self.frame_diff_plot_data) > self.frame_diff_max_points:
                self.frame_diff_plot_data.pop(0)
                self.frame_diff_plot_times.pop(0)
            
            if PYQTGRAPH_AVAILABLE and hasattr(self, 'frame_diff_plot'):
                # Update frame difference curve
                if len(self.frame_diff_plot_times) > 0 and len(self.frame_diff_plot_data) > 0:
                    self.frame_diff_curve.setData(self.frame_diff_plot_times, self.frame_diff_plot_data)
                    
                    # Update threshold line (horizontal line at threshold value)
                    threshold_times = [self.frame_diff_plot_times[0], self.frame_diff_plot_times[-1]]
                    threshold_values = [self.frame_difference_threshold, self.frame_difference_threshold]
                    self.frame_diff_threshold_line.setData(threshold_times, threshold_values)
            else:
                # Update text label
                if hasattr(self, 'frame_diff_label'):
                    self.frame_diff_label.setText(
                        f"Frame Difference: {self.current_frame_difference:.1f} | Threshold: {self.frame_difference_threshold:.1f}"
                    )
        except Exception as e:
            # Guard against errors in plot update
            print(f"Error updating frame difference plot: {e}")
    
    def setup_ui(self):
        """Set up the user interface for image control."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        
        # Control buttons
        control_layout = QHBoxLayout()
        
        # ROI button - needs custom styling for checked state
        self.roi_button = create_button("Switch to ROI", "success", font_size="12pt", padding="8px 20px")
        self.roi_button.setCheckable(True)
        # Add checked state styling
        checked_style = f"""
            QPushButton:checked {{
                background-color: {Colors.PRIMARY_BLUE};
            }}
            QPushButton:checked:hover {{
                background-color: {Colors.PRIMARY_BLUE_HOVER};
            }}
        """
        self.roi_button.setStyleSheet(self.roi_button.styleSheet() + checked_style)
        self.roi_button.toggled.connect(self._update_roi_switch_button)
        self.roi_button.clicked.connect(self.toggle_roi)
        control_layout.addWidget(self.roi_button)
        control_layout.addSpacing(10)
        
        # LED control buttons
        self.red_led_button = create_led_button("Red\nOff", "red", size=45)
        self.red_led_button.clicked.connect(self.toggle_red_led)
        self.red_led_button.setEnabled(NIDAQMX_AVAILABLE)
        control_layout.addWidget(self.red_led_button)
        
        self.blue_led_button = create_led_button("Blue\nOff", "blue", size=45)
        self.blue_led_button.clicked.connect(self.toggle_blue_led)
        self.blue_led_button.setEnabled(NIDAQMX_AVAILABLE)
        control_layout.addWidget(self.blue_led_button)
        
        control_layout.addSpacing(10)
        
        # FL auto exposure checkbox and value label (relocated from Main tab)
        self.fl_auto_exposure_checkbox = QCheckBox("FL Auto exposure")
        self.fl_auto_exposure_checkbox.setStyleSheet("""
            QCheckBox {
                font-size: 11pt;
                font-weight: bold;
                padding: 5px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
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
        self.fl_auto_exposure_checkbox.toggled.connect(self.toggle_fl_auto_exposure)
        control_layout.addWidget(self.fl_auto_exposure_checkbox)
        
        self.fl_auto_exposure_value_label = QLabel("N/A")
        self.fl_auto_exposure_value_label.setStyleSheet("""
            QLabel {
                font-size: 11pt;
                font-weight: bold;
                color: #90ee90;
                background-color: #404040;
                padding: 5px 10px;
                border-radius: 3px;
                min-width: 80px;
            }
        """)
        control_layout.addWidget(self.fl_auto_exposure_value_label)
        
        # Add stretch to push camera mode and init button to the right
        control_layout.addStretch()
        
        # Camera mode selection (BF only or BF+FL) - right justified
        self.camera_mode_label = QLabel("Camera Mode:")
        control_layout.addWidget(self.camera_mode_label)
        self.camera_mode_combo = QComboBox()
        self.camera_mode_combo.addItems(["BF only", "BF+FL"])
        self.camera_mode_combo.setCurrentIndex(1)  # Default to BF+FL
        style_input_field(self.camera_mode_combo)
        self.camera_mode_combo.setStyleSheet("""
            QComboBox {
                padding: 5px 10px;
                font-size: 11pt;
                border: 1px solid #ccc;
                border-radius: 3px;
            }
        """)
        control_layout.addWidget(self.camera_mode_combo)
        self.camera_mode_combo.currentIndexChanged.connect(self._update_camera_mode_ui_visibility)
        
        # Add some spacing
        control_layout.addSpacing(10)
        
        # Initialize Cameras button - right justified, initially green
        self.init_button = create_button("Initialize Cameras", "success", font_size="12pt", padding="8px 20px")
        self.init_button.clicked.connect(self.on_init_button_clicked)
        control_layout.addWidget(self.init_button)
        
        layout.addLayout(control_layout)
        
        # Create horizontal layout: tabs on left, images on right
        main_content_layout = QHBoxLayout()
        
        # Create tab widget (left side)
        self.tabs = QTabWidget()
        
        # Set size policy to allow shrinking below size hint, enforcing 20% width
        # Use Maximum horizontal policy to prevent expansion beyond preferred size
        self.tabs.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        
        # Main tab
        main_tab = self._create_main_tab()
        self.tabs.addTab(main_tab, "Main")
        
        # Image triggering tab
        triggering_tab = self._create_image_triggering_tab()
        self.tabs.addTab(triggering_tab, "Image triggering")
        
        # ROI tab
        roi_tab = self._create_roi_tab()
        self.tabs.addTab(roi_tab, "ROI")
        
        # QC tab (formerly Alignment)
        qc_tab = self._create_qc_tab()
        self.tabs.addTab(qc_tab, "QC")
        
        # Connect tab change to auto-activate ROI overlay when ROI or QC tab is active
        self.tabs.currentChanged.connect(self._on_tab_changed)
        
        # Advanced tab
        advanced_tab = self._create_advanced_tab()
        self.tabs.addTab(advanced_tab, "Advanced")
        
        # Add tabs to left side (20% of width - stretch factor 1)
        main_content_layout.addWidget(self.tabs, 1)
        
        # Create camera displays (right side, vertically stacked, 80% of width - stretch factor 4)
        camera_display_widget = self._create_camera_display_panel()
        main_content_layout.addWidget(camera_display_widget, 4)
        
        layout.addLayout(main_content_layout, 1)
        
        # Status label
        self.status_label = create_status_badge("Status: Not initialized", "gray", padding="5px", border_radius="3px")
        layout.addWidget(self.status_label)
        
        self._setup_styles()

    def set_gui_mode(self, mode):
        """Set the GUI mode and update UI visibility."""
        is_advanced = (mode == "advanced")
        
        # In basic mode, hide the entire tab widget
        self.tabs.setVisible(is_advanced)
        
        # If advanced mode, also ensure individual tabs are visible (in case they were hidden)
        if is_advanced:
            for i in range(1, self.tabs.count()):
                self.tabs.setTabVisible(i, True)
                
        # Update visibility of FL controls based on camera mode
        self._update_camera_mode_ui_visibility()
        
        # Hide camera mode selection and initialization button in basic mode
        if hasattr(self, 'camera_mode_label'):
            self.camera_mode_label.setVisible(is_advanced)
        if hasattr(self, 'camera_mode_combo'):
            self.camera_mode_combo.setVisible(is_advanced)
        if hasattr(self, 'init_button'):
            self.init_button.setVisible(is_advanced)

    def _update_camera_mode_ui_visibility(self):
        """Update visibility of FL-related controls based on camera mode."""
        # Check if we are in BF only mode
        if hasattr(self, 'camera_mode_combo'):
            camera_mode = self.camera_mode_combo.currentText()
        else:
            camera_mode = getattr(self, 'camera_mode', "BF+FL")
            
        is_bf_fl = (camera_mode == "BF+FL")
        
        # Hide/show FL specific controls
        if hasattr(self, 'blue_led_button'):
            self.blue_led_button.setVisible(is_bf_fl)
        if hasattr(self, 'fl_auto_exposure_checkbox'):
            self.fl_auto_exposure_checkbox.setVisible(is_bf_fl)
        if hasattr(self, 'fl_auto_exposure_value_label'):
            self.fl_auto_exposure_value_label.setVisible(is_bf_fl)

        # Update framerate based on mode
        if hasattr(self, 'bf_roi_framerate') and hasattr(self, 'bffl_roi_framerate'):
            if self.camera_mode == "BF only":
                self.roi_framerate = self.bf_roi_framerate
            else:
                self.roi_framerate = self.bffl_roi_framerate
            
            if hasattr(self, 'roi_framerate_spin'):
                # Block signals to avoid triggering on_roi_framerate_changed back to us
                self.roi_framerate_spin.blockSignals(True)
                self.roi_framerate_spin.setValue(int(self.roi_framerate))
                self.roi_framerate_spin.blockSignals(False)
            
            print(f"ROI Framerate updated to {self.roi_framerate} for mode {self.camera_mode}")
    
    def _create_main_tab(self):
        """Create the main tab."""
        main_widget = QWidget()
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        info_label = QLabel("Main controls and settings.")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)
        
        # Display ROI overlay button
        self.detect_roi_button = QPushButton("ROI overlay disabled")
        self.detect_roi_button.setCheckable(True)
        self.detect_roi_button.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 5px;
                padding: 8px 20px;
                font-size: 12pt;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
            QPushButton:checked {
                background-color: #0078d7;
            }
            QPushButton:disabled {
                background-color: #d3d3d3;
                color: #808080;
            }
        """)
        self.detect_roi_button.toggled.connect(self._update_roi_button_text)
        self.detect_roi_button.clicked.connect(self.toggle_roi_detection)
        layout.addWidget(self.detect_roi_button)
        
        # Enable Image Saving checkbox
        self.enable_saving_checkbox = QCheckBox("Enable Image Saving")
        style_checkbox(self.enable_saving_checkbox)
        self.enable_saving_checkbox.setStyleSheet(self.enable_saving_checkbox.styleSheet() + """
            QCheckBox {
                font-size: 12pt;
                font-weight: bold;
                padding: 5px;
            }
        """)
        self.enable_saving_checkbox.toggled.connect(self.toggle_image_saving)
        layout.addWidget(self.enable_saving_checkbox)
        
        layout.addStretch()
        
        layout.addStretch()
        
        # Status section at the bottom
        status_group = QGroupBox("Status")
        status_layout = QFormLayout()
        status_group.setLayout(status_layout)
        
        self.fl_driver_current_value_label = QLabel("N/A")
        self.fl_driver_current_value_label.setStyleSheet("""
            QLabel {
                font-size: 14pt;
                font-weight: bold;
                color: #0078d7;
                padding: 5px;
            }
        """)
        status_layout.addRow("FL Driver Current:", self.fl_driver_current_value_label)
        
        # BF mode intensity and saturated pixels (on same line)
        bf_row = QHBoxLayout()
        bf_intensity_label = QLabel("BF mode intensity:")
        bf_intensity_label.setStyleSheet("color: #ff6b6b; font-weight: bold;")
        self.bf_intensity_value = QLabel("N/A")
        self.bf_intensity_value.setStyleSheet("""
            QLabel {
                font-size: 12pt;
                font-weight: bold;
                color: #ff6b6b;
                background-color: #404040;
                padding: 5px;
                border-radius: 3px;
            }
        """)
        bf_saturated_label = QLabel("BF saturated pixels:")
        bf_saturated_label.setStyleSheet("color: #ff6b6b; font-weight: bold;")
        self.bf_saturated_value = QLabel("N/A")
        self.bf_saturated_value.setStyleSheet("""
            QLabel {
                font-size: 12pt;
                font-weight: bold;
                color: #ff6b6b;
                background-color: #404040;
                padding: 5px;
                border-radius: 3px;
            }
        """)
        bf_row.addWidget(bf_intensity_label)
        bf_row.addWidget(self.bf_intensity_value)
        bf_row.addSpacing(20)
        bf_row.addWidget(bf_saturated_label)
        bf_row.addWidget(self.bf_saturated_value)
        bf_row.addStretch()
        bf_row_widget = QWidget()
        bf_row_widget.setLayout(bf_row)
        status_layout.addRow("", bf_row_widget)
        
        # FL mode intensity and saturated pixels (on same line)
        fl_row = QHBoxLayout()
        fl_intensity_label = QLabel("FL mode intensity:")
        fl_intensity_label.setStyleSheet("color: #90ee90; font-weight: bold;")
        self.fl_intensity_value = QLabel("N/A")
        self.fl_intensity_value.setStyleSheet("""
            QLabel {
                font-size: 12pt;
                font-weight: bold;
                color: #90ee90;
                background-color: #404040;
                padding: 5px;
                border-radius: 3px;
            }
        """)
        fl_saturated_label = QLabel("FL saturated pixels:")
        fl_saturated_label.setStyleSheet("color: #90ee90; font-weight: bold;")
        self.fl_saturated_value = QLabel("N/A")
        self.fl_saturated_value.setStyleSheet("""
            QLabel {
                font-size: 12pt;
                font-weight: bold;
                color: #90ee90;
                background-color: #404040;
                padding: 5px;
                border-radius: 3px;
            }
        """)
        fl_row.addWidget(fl_intensity_label)
        fl_row.addWidget(self.fl_intensity_value)
        fl_row.addSpacing(20)
        fl_row.addWidget(fl_saturated_label)
        fl_row.addWidget(self.fl_saturated_value)
        fl_row.addStretch()
        fl_row_widget = QWidget()
        fl_row_widget.setLayout(fl_row)
        status_layout.addRow("", fl_row_widget)
        
        # Saved frames count indicator
        self.saved_frames_count_label = QLabel("0")
        self.saved_frames_count_label.setStyleSheet("""
            QLabel {
                font-size: 14pt;
                font-weight: bold;
                color: #4CAF50;
                padding: 5px;
            }
        """)
        status_layout.addRow("Saved frames:", self.saved_frames_count_label)
        
        layout.addWidget(status_group)
        
        return main_widget
    
    def _create_image_triggering_tab(self):
        """Create the Image triggering tab with frame difference controls."""
        triggering_widget = QWidget()
        layout = QVBoxLayout(triggering_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Triggered frame metadata button
        self.triggered_metadata_button = create_button("Triggered frame metadata", "primary", font_size="12pt", padding="8px 20px")
        self.triggered_metadata_button.clicked.connect(self.show_triggered_metadata)
        layout.addWidget(self.triggered_metadata_button)
        
        # Offset and Threshold controls on the same line
        controls_layout = QHBoxLayout()
        
        # Offset control
        offset_label = QLabel("Offset:")
        offset_label.setStyleSheet("font-weight: bold;")
        self.frame_diff_offset_spin = QDoubleSpinBox()
        self.frame_diff_offset_spin.setMinimum(0.0)
        self.frame_diff_offset_spin.setMaximum(255.0)
        self.frame_diff_offset_spin.setSingleStep(1)
        self.frame_diff_offset_spin.setDecimals(1)
        self.frame_diff_offset_spin.setValue(self.frame_difference_offset)
        self.frame_diff_offset_spin.valueChanged.connect(self.on_frame_diff_offset_changed)
        controls_layout.addWidget(offset_label)
        controls_layout.addWidget(self.frame_diff_offset_spin)
        
        controls_layout.addSpacing(20)
        
        # Threshold control
        threshold_label = QLabel("Trigger Threshold:")
        threshold_label.setStyleSheet("font-weight: bold;")
        self.frame_diff_threshold_spin = QDoubleSpinBox()
        style_input_field(self.frame_diff_threshold_spin)
        self.frame_diff_threshold_spin.setMinimum(0.0)
        self.frame_diff_threshold_spin.setMaximum(1000000.0)
        self.frame_diff_threshold_spin.setSingleStep(100.0)
        self.frame_diff_threshold_spin.setDecimals(1)
        self.frame_diff_threshold_spin.setValue(self.frame_difference_threshold)
        self.frame_diff_threshold_spin.valueChanged.connect(self.on_frame_diff_threshold_changed)
        controls_layout.addWidget(threshold_label)
        controls_layout.addWidget(self.frame_diff_threshold_spin)
        
        controls_layout.addStretch()
        controls_widget = QWidget()
        controls_widget.setLayout(controls_layout)
        layout.addWidget(controls_widget)
        
        # Frame difference plot widget
        if PYQTGRAPH_AVAILABLE:
            self.frame_diff_plot = pg.PlotWidget(title="Frame Difference")
            self.frame_diff_plot.setBackground('#202124')  # Dark gray background matching frequency plot
            self.frame_diff_plot.setLabel('left', 'Frame Difference')
            self.frame_diff_plot.setLabel('bottom', 'Time')
            self.frame_diff_plot.setMinimumHeight(200)
            # Create curves for frame difference value and threshold line
            self.frame_diff_curve = self.frame_diff_plot.plot([], [], pen='y', name='Frame Difference')
            self.frame_diff_threshold_line = self.frame_diff_plot.plot([], [], pen='r', name='Threshold')
            layout.addWidget(self.frame_diff_plot)
        else:
            # Fallback: use QLabel to display text values
            self.frame_diff_label = QLabel("Frame Difference: N/A | Threshold: N/A")
            self.frame_diff_label.setStyleSheet("""
                QLabel {
                    font-size: 11pt;
                    padding: 5px;
                    background-color: #f0f0f0;
                    border: 1px solid #ccc;
                    border-radius: 3px;
                }
            """)
            layout.addWidget(self.frame_diff_label)
        
        layout.addStretch()
        
        return triggering_widget
    
    def _create_camera_display_panel(self):
        """Create the camera display panel with vertically stacked images."""
        display_widget = QWidget()
        layout = QVBoxLayout(display_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # Brightfield camera display (top)
        brightfield_frame = QFrame()
        brightfield_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        brightfield_frame.setStyleSheet("""
            QFrame {
                border: 2px solid #ccc;
                border-radius: 5px;
                background-color: #000000;
            }
        """)
        brightfield_layout = QVBoxLayout(brightfield_frame)
        brightfield_layout.setContentsMargins(5, 5, 5, 5)
        brightfield_label = QLabel("Brightfield")
        brightfield_label.setStyleSheet("color: white; font-weight: bold; font-size: 10pt;")
        brightfield_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brightfield_layout.addWidget(brightfield_label)
        self.camera1_display = ImageDisplayLabel()
        brightfield_layout.addWidget(self.camera1_display)
        layout.addWidget(brightfield_frame, 1)
        
        # Fluorescent camera display (bottom) - will be shown/hidden based on camera mode
        fluorescent_frame = QFrame()
        fluorescent_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        fluorescent_frame.setStyleSheet("""
            QFrame {
                border: 2px solid #ccc;
                border-radius: 5px;
                background-color: #000000;
            }
        """)
        fluorescent_layout = QVBoxLayout(fluorescent_frame)
        fluorescent_layout.setContentsMargins(5, 5, 5, 5)
        fluorescent_label = QLabel("Fluorescent")
        fluorescent_label.setStyleSheet("color: white; font-weight: bold; font-size: 10pt;")
        fluorescent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fluorescent_layout.addWidget(fluorescent_label)
        self.camera2_display = ImageDisplayLabel()
        fluorescent_layout.addWidget(self.camera2_display)
        layout.addWidget(fluorescent_frame, 1)
        
        # Store reference to fluorescent frame for show/hide
        self.fluorescent_frame = fluorescent_frame
        
        return display_widget
    
    def _create_roi_tab(self):
        """Create the ROI tab with histogram and angle plot display."""
        roi_widget = QWidget()
        layout = QVBoxLayout(roi_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        self.histogram_widget = HistogramWidget()
        self.histogram_widget.setMinimumHeight(150)
        layout.addWidget(self.histogram_widget, 1)  # Stretch factor 1
        
        self.angle_plot_widget = AnglePlotWidget()
        self.angle_plot_widget.setMinimumHeight(150)
        layout.addWidget(self.angle_plot_widget, 1)  # Stretch factor 1 (same as histogram)
        
        self.focus_plot_widget = FocusPlotWidget()
        self.focus_plot_widget.setMinimumHeight(150)
        layout.addWidget(self.focus_plot_widget, 1)  # Stretch factor 1
        
        return roi_widget
    
    def _create_qc_tab(self):
        """Create the QC tab with alignment plot, blue LED current plot, and photodiode plot."""
        qc_widget = QWidget()
        layout = QVBoxLayout(qc_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Pass overlay dimensions to AlignmentPlotWidget (first plot)
        self.alignment_plot_widget = AlignmentPlotWidget(overlay_width=self.overlay_width, overlay_height=self.overlay_height)
        self.alignment_plot_widget.setMinimumHeight(200)
        layout.addWidget(self.alignment_plot_widget, 1)
        
        # Blue LED current plot (second plot)
        self.blue_led_current_plot = BlueLEDCurrentPlotWidget()
        self.blue_led_current_plot.setMinimumHeight(200)
        layout.addWidget(self.blue_led_current_plot, 1)
        
        # Photodiode plot (third plot)
        self.photodiode_plot = PhotodiodePlotWidget()
        self.photodiode_plot.setMinimumHeight(200)
        layout.addWidget(self.photodiode_plot, 1)
        
        return qc_widget
    
    def _create_advanced_tab(self):
        """Create the Advanced tab with ROI settings and camera configurations."""
        advanced_widget = QWidget()
        layout = QVBoxLayout(advanced_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # ROI Settings Section
        roi_group = QGroupBox("ROI Settings")
        roi_layout = QFormLayout()
        roi_group.setLayout(roi_layout)
        
        # ROI Width input
        self.roi_width_spin = QSpinBox()
        self.roi_width_spin.setMinimum(1)
        self.roi_width_spin.setMaximum(10000)
        self.roi_width_spin.setValue(self.roi_width)
        self.roi_width_spin.valueChanged.connect(self.on_roi_width_changed)
        roi_layout.addRow("ROI Width:", self.roi_width_spin)
        
        # ROI Height input
        self.roi_height_spin = QSpinBox()
        self.roi_height_spin.setMinimum(1)
        self.roi_height_spin.setMaximum(10000)
        self.roi_height_spin.setValue(self.roi_height)
        self.roi_height_spin.valueChanged.connect(self.on_roi_height_changed)
        roi_layout.addRow("ROI Height:", self.roi_height_spin)
        
        # ROI Framerate input
        self.roi_framerate_spin = QSpinBox()
        self.roi_framerate_spin.setMinimum(1)
        self.roi_framerate_spin.setMaximum(10000)
        self.roi_framerate_spin.setValue(int(self.roi_framerate))
        self.roi_framerate_spin.setSuffix(" Hz")
        self.roi_framerate_spin.valueChanged.connect(self.on_roi_framerate_changed)
        roi_layout.addRow("ROI Framerate:", self.roi_framerate_spin)
        
        # Display Framerate input
        self.display_framerate_spin = QSpinBox()
        self.display_framerate_spin.setMinimum(1)
        self.display_framerate_spin.setMaximum(100)
        self.display_framerate_spin.setValue(int(self.display_framerate))
        self.display_framerate_spin.setSuffix(" Hz")
        self.display_framerate_spin.valueChanged.connect(self.on_display_framerate_changed)
        roi_layout.addRow("Display Framerate:", self.display_framerate_spin)
        
        layout.addWidget(roi_group)
        
        # Brightfield Camera Settings Section
        brightfield_group = QGroupBox("Brightfield Camera")
        brightfield_layout = QFormLayout()
        brightfield_group.setLayout(brightfield_layout)
        
        # Brightfield camera selection dropdown
        self.brightfield_camera_combo = QComboBox()
        self.brightfield_camera_combo.setPlaceholderText("Select camera...")
        self.brightfield_camera_combo.currentTextChanged.connect(self.on_brightfield_camera_changed)
        brightfield_layout.addRow("Camera:", self.brightfield_camera_combo)
        
        # Brightfield exposure input (in microseconds)
        self.brightfield_exposure_spin = QDoubleSpinBox()
        self.brightfield_exposure_spin.setMinimum(20.0)
        self.brightfield_exposure_spin.setMaximum(5000.0)
        self.brightfield_exposure_spin.setSingleStep(5.0)
        self.brightfield_exposure_spin.setSuffix(" µs")
        self.brightfield_exposure_spin.setDecimals(1)
        self.brightfield_exposure_spin.setValue(30.0)  # Default 30µs
        self.last_bf_exposure_us = 30.0  # Initialize tracking variable
        self.brightfield_exposure_spin.valueChanged.connect(self.on_brightfield_exposure_changed)
        brightfield_layout.addRow("Exposure:", self.brightfield_exposure_spin)
        
        # Brightfield gain input
        self.brightfield_gain_spin = QDoubleSpinBox()
        self.brightfield_gain_spin.setMinimum(0.0)
        self.brightfield_gain_spin.setMaximum(48.0)  # Typical max gain for Basler cameras
        self.brightfield_gain_spin.setSingleStep(0.1)
        self.brightfield_gain_spin.setDecimals(1)
        self.brightfield_gain_spin.setValue(8.0)  # Default 8 for BF
        self.brightfield_gain_spin.valueChanged.connect(self.on_brightfield_gain_changed)
        brightfield_layout.addRow("Gain:", self.brightfield_gain_spin)
        
        layout.addWidget(brightfield_group)
        
        # Fluorescent Camera Settings Section
        fluorescent_group = QGroupBox("Fluorescent Camera")
        fluorescent_layout = QFormLayout()
        fluorescent_group.setLayout(fluorescent_layout)
        
        # Fluorescent camera selection dropdown
        self.fluorescent_camera_combo = QComboBox()
        self.fluorescent_camera_combo.setPlaceholderText("Select camera...")
        self.fluorescent_camera_combo.currentTextChanged.connect(self.on_fluorescent_camera_changed)
        fluorescent_layout.addRow("Camera:", self.fluorescent_camera_combo)
        
        # Fluorescent exposure input (in microseconds)
        self.fluorescent_exposure_spin = QDoubleSpinBox()
        self.fluorescent_exposure_spin.setMinimum(20.0)
        self.fluorescent_exposure_spin.setMaximum(5000.0)
        self.fluorescent_exposure_spin.setSingleStep(5.0)
        self.fluorescent_exposure_spin.setSuffix(" µs")
        self.fluorescent_exposure_spin.setDecimals(1)
        self.fluorescent_exposure_spin.setValue(50.0)  # Default 50µs
        self.last_fl_exposure_us = 50.0  # Initialize tracking variable
        self.fluorescent_exposure_spin.valueChanged.connect(self.on_fluorescent_exposure_changed)
        fluorescent_layout.addRow("Exposure:", self.fluorescent_exposure_spin)
        
        # Fluorescent gain input
        self.fluorescent_gain_spin = QDoubleSpinBox()
        self.fluorescent_gain_spin.setMinimum(0.0)
        self.fluorescent_gain_spin.setMaximum(48.0)  # Typical max gain for Basler cameras
        self.fluorescent_gain_spin.setSingleStep(0.1)
        self.fluorescent_gain_spin.setDecimals(1)
        self.fluorescent_gain_spin.setValue(0.0)  # Default 0 for FL
        self.fluorescent_gain_spin.valueChanged.connect(self.on_fluorescent_gain_changed)
        fluorescent_layout.addRow("Gain:", self.fluorescent_gain_spin)
        
        layout.addWidget(fluorescent_group)
        
        # ROI Detection Settings Section (at the bottom)
        detection_group = QGroupBox("ROI Detection Settings")
        detection_layout = QFormLayout()
        detection_group.setLayout(detection_layout)
        
        # Edge threshold control
        self.edge_threshold_spin = QDoubleSpinBox()
        self.edge_threshold_spin.setMinimum(0.0)
        self.edge_threshold_spin.setMaximum(255.0)
        self.edge_threshold_spin.setSingleStep(1.0)
        self.edge_threshold_spin.setDecimals(1)
        self.edge_threshold_spin.setValue(self.edge_threshold)
        self.edge_threshold_spin.valueChanged.connect(self.on_edge_threshold_changed)
        detection_layout.addRow("Edge Threshold:", self.edge_threshold_spin)
        
        # Minimum brightness difference control
        self.min_brightness_diff_spin = QDoubleSpinBox()
        self.min_brightness_diff_spin.setMinimum(0.0)
        self.min_brightness_diff_spin.setMaximum(255.0)
        self.min_brightness_diff_spin.setSingleStep(1.0)
        self.min_brightness_diff_spin.setDecimals(1)
        self.min_brightness_diff_spin.setValue(self.min_brightness_diff)
        self.min_brightness_diff_spin.valueChanged.connect(self.on_min_brightness_diff_changed)
        detection_layout.addRow("Min Brightness Diff:", self.min_brightness_diff_spin)
        
        # Detection Mode
        self.use_line_detection_check = QComboBox()
        self.use_line_detection_check.addItems(["Axis-Aligned", "Angle-Tolerant (Lines)"])
        self.use_line_detection_check.setCurrentIndex(1 if self.use_line_detection else 0)
        self.use_line_detection_check.currentIndexChanged.connect(self.on_use_line_detection_changed)
        detection_layout.addRow("Detection Mode:", self.use_line_detection_check)
        
        # Vertical line threshold control (for derivative method)
        self.vertical_line_threshold_spin = QDoubleSpinBox()
        self.vertical_line_threshold_spin.setMinimum(0.0)
        self.vertical_line_threshold_spin.setMaximum(255.0)
        self.vertical_line_threshold_spin.setSingleStep(1.0)
        self.vertical_line_threshold_spin.setDecimals(1)
        self.vertical_line_threshold_spin.setSpecialValueText("Auto (use Edge Threshold)")
        if self.vertical_line_threshold is None:
            self.vertical_line_threshold_spin.setValue(0.0)
        else:
            self.vertical_line_threshold_spin.setValue(self.vertical_line_threshold)
        self.vertical_line_threshold_spin.valueChanged.connect(self.on_vertical_line_threshold_changed)
        detection_layout.addRow("Vertical Line Threshold:", self.vertical_line_threshold_spin)
        
        # Corner search width control
        self.corner_search_width_spin = QSpinBox()
        self.corner_search_width_spin.setMinimum(1)
        self.corner_search_width_spin.setMaximum(200)
        self.corner_search_width_spin.setSingleStep(1)
        self.corner_search_width_spin.setValue(self.corner_search_width)
        self.corner_search_width_spin.valueChanged.connect(self.on_corner_search_width_changed)
        detection_layout.addRow("Corner Search Width:", self.corner_search_width_spin)
        
        # Center exclusion percent control
        self.center_exclusion_percent_spin = QDoubleSpinBox()
        self.center_exclusion_percent_spin.setMinimum(0.0)
        self.center_exclusion_percent_spin.setMaximum(90.0)
        self.center_exclusion_percent_spin.setSingleStep(5.0)
        self.center_exclusion_percent_spin.setDecimals(1)
        self.center_exclusion_percent_spin.setSuffix(" %")
        self.center_exclusion_percent_spin.setValue(self.center_exclusion_percent)
        self.center_exclusion_percent_spin.valueChanged.connect(self.on_center_exclusion_percent_changed)
        detection_layout.addRow("Center Exclusion %:", self.center_exclusion_percent_spin)
        
        # Report ROI detection heuristics checkbox
        self.report_roi_heuristics_check = QCheckBox("Report ROI detection heuristics to console")
        self.report_roi_heuristics_check.setChecked(self.report_roi_heuristics)
        self.report_roi_heuristics_check.toggled.connect(self.on_report_roi_heuristics_changed)
        # Style checkbox with blue fill when checked
        self.report_roi_heuristics_check.setStyleSheet("""
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #0078d7;
                border-radius: 3px;
                background-color: white;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d7;
                border-color: #005a9e;
            }
            QCheckBox::indicator:hover {
                border-color: #005a9e;
            }
        """)
        detection_layout.addRow("", self.report_roi_heuristics_check)
        
        layout.addWidget(detection_group)
        
        layout.addStretch()
        
        # Detect Cameras button
        self.detect_cameras_button = create_button("Detect Cameras", "primary")
        self.detect_cameras_button.clicked.connect(self.on_detect_cameras_clicked)
        layout.addWidget(self.detect_cameras_button)
        
        return advanced_widget
    
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
        
    def on_detect_cameras_clicked(self):
        """Handle detect cameras button click - re-enumerates cameras and updates dropdowns."""
        self.status_label.setText("Status: Detecting cameras...")
        QApplication.processEvents()
        
        # Re-populate dropdowns
        self._populate_camera_dropdowns()
        
        self.status_label.setText("Status: Cameras detected and dropdowns updated")
    
    def on_init_button_clicked(self):
        """Handle initialization button click - supports both initial and reinitialization."""
        if self.camera_initialized:
            # Reinitialize: cleanup first, wait, then reinitialize
            self.status_label.setText("Status: Reinitializing cameras...")
            QApplication.processEvents()
            
            # Cleanup current camera sessions
            self.cleanup_cameras()
            
            # Wait 500ms before reinitializing
            QTimer.singleShot(500, self.initialize_cameras)
        else:
            # Normal initialization
            self.initialize_cameras()
    
    def _get_camera_display_name(self, device):
        """Get a display name for the camera (model + serial number)."""
        try:
            model_name = device.GetModelName()
            serial_number = device.GetSerialNumber()
            return f"{model_name} (SN: {serial_number})"
        except Exception:
            try:
                # Fallback to just model name if serial not available
                return device.GetModelName()
            except Exception:
                return "Unknown Camera"
    
    def _get_camera_serial(self, device):
        """Get the serial number of a camera device."""
        try:
            return device.GetSerialNumber()
        except Exception:
            return None
    
    def _get_selected_camera_indices(self):
        """Get the camera indices selected for brightfield and fluorescent channels."""
        brightfield_display_name = self.brightfield_camera_combo.currentText()
        fluorescent_display_name = self.fluorescent_camera_combo.currentText()
        
        # Extract serial numbers from display names (format: "Model Name (SN: XXXXX)")
        brightfield_serial = None
        fluorescent_serial = None
        
        if "(SN:" in brightfield_display_name:
            try:
                brightfield_serial = brightfield_display_name.split("(SN:")[1].split(")")[0].strip()
            except Exception:
                pass
        
        if "(SN:" in fluorescent_display_name:
            try:
                fluorescent_serial = fluorescent_display_name.split("(SN:")[1].split(")")[0].strip()
            except Exception:
                pass
        
        # Get available cameras
        try:
            tl_factory = pylon.TlFactory.GetInstance()
            devices = tl_factory.EnumerateDevices()
            
            brightfield_idx = None
            fluorescent_idx = None
            
            # Find indices by matching serial numbers
            for i, device in enumerate(devices):
                try:
                    serial = self._get_camera_serial(device)
                    if serial and serial == brightfield_serial:
                        brightfield_idx = i
                    if serial and serial == fluorescent_serial:
                        fluorescent_idx = i
                except Exception:
                    pass
            
            return brightfield_idx, fluorescent_idx
        except Exception:
            return None, None
    
    def initialize_cameras(self, camera_mode=None):
        """Initialize and start Basler cameras based on config/dropdown selection and camera mode.
        
        Args:
            camera_mode: Optional camera mode override ("BF only" or "BF+FL"). 
                        If None, uses current combo box selection.
        """
        try:
            if hasattr(self, 'init_button'):
                self.init_button.setEnabled(False)
            if hasattr(self, 'status_label'):
                self.status_label.setText("Status: Initializing cameras...")
            QApplication.processEvents()
            
            # Get camera mode from parameter or combo box
            if camera_mode is not None:
                self.camera_mode = camera_mode
            elif hasattr(self, 'camera_mode_combo'):
                self.camera_mode = self.camera_mode_combo.currentText()
            else:
                self.camera_mode = "BF+FL"  # Default
                
            # Update UI visibility based on selected mode
            self._update_camera_mode_ui_visibility()
            
            # Notify other components of mode change
            self.camera_mode_changed.emit(self.camera_mode)
            
            # Get available cameras
            tl_factory = pylon.TlFactory.GetInstance()
            devices = tl_factory.EnumerateDevices()
            
            if len(devices) < 1:
                self.status_label.setText(f"Status: Error - Found {len(devices)} camera(s), need at least 1")
                self.init_button.setEnabled(True)
                return
            
            # Get selected camera indices
            brightfield_idx, fluorescent_idx = self._get_selected_camera_indices()
            
            # Validate brightfield selection
            if brightfield_idx is None:
                self.status_label.setText("Status: Error - Please select brightfield camera in Advanced tab")
                self.init_button.setEnabled(True)
                return
            
            if brightfield_idx >= len(devices):
                self.status_label.setText("Status: Error - Selected brightfield camera index out of range")
                self.init_button.setEnabled(True)
                return
            
            # Validate fluorescent selection only if BF+FL mode
            if self.camera_mode == "BF+FL":
                if fluorescent_idx is None:
                    self.status_label.setText("Status: Error - Please select fluorescent camera in Advanced tab")
                    self.init_button.setEnabled(True)
                    return
                
                if brightfield_idx == fluorescent_idx:
                    self.status_label.setText("Status: Error - Brightfield and Fluorescent cameras must be different")
                    self.init_button.setEnabled(True)
                    return
                
                if fluorescent_idx >= len(devices):
                    self.status_label.setText("Status: Error - Selected fluorescent camera index out of range")
                    self.init_button.setEnabled(True)
                    return
            
            # Create and open cameras based on mode
            self.cameras = []
            self.camera_device_names = []
            self.camera_max_width = {}
            self.camera_max_height = {}
            self.camera_original_settings = {}
            self.camera_channels = {}
            
            # Camera mapping: index 0 = brightfield, index 1 = fluorescent (if BF+FL mode)
            if self.camera_mode == "BF only":
                camera_indices = [brightfield_idx]
                channels = ['brightfield']
            else:  # BF+FL
                camera_indices = [brightfield_idx, fluorescent_idx]
                channels = ['brightfield', 'fluorescent']
            
            for internal_idx, (device_idx, channel) in enumerate(zip(camera_indices, channels)):
                camera = pylon.InstantCamera(tl_factory.CreateDevice(devices[device_idx]))
                camera.Open()
                # Configure camera for continuous acquisition
                # Increased buffer size for high frame rates (1250 Hz) to prevent buffer overflows
                camera.MaxNumBuffer = 50
                
                # Get and store maximum camera dimensions and set to full image
                try:
                    max_width = int(camera.WidthMax.GetValue())
                    max_height = int(camera.HeightMax.GetValue())
                    self.camera_max_width[internal_idx] = max_width
                    self.camera_max_height[internal_idx] = max_height
                    
                    # Set camera to maximum dimensions (full image mode)
                    camera.OffsetX.SetValue(0)
                    camera.OffsetY.SetValue(0)
                    camera.Width.SetValue(max_width)
                    camera.Height.SetValue(max_height)
                    
                    # Store original settings (which are now max dimensions)
                    self.camera_original_settings[internal_idx] = {
                        'width': max_width,
                        'height': max_height,
                        'offset_x': 0,
                        'offset_y': 0
                    }
                except Exception as e:
                    print(f"Warning: Could not get camera {internal_idx} max dimensions: {e}")
                    # Use defaults if we can't get max dimensions
                    self.camera_max_width[internal_idx] = 1920
                    self.camera_max_height[internal_idx] = 1200
                    self.camera_original_settings[internal_idx] = {
                        'width': 1920,
                        'height': 1200,
                        'offset_x': 0,
                        'offset_y': 0
                    }
                
                self.cameras.append(camera)
                # Get device name (model + serial for uniqueness)
                try:
                    device_info = camera.GetDeviceInfo()
                    model_name = device_info.GetModelName()
                    try:
                        serial_number = device_info.GetSerialNumber()
                        device_name = f"{model_name} (SN: {serial_number})"
                    except Exception:
                        device_name = model_name
                except Exception:
                    device_name = f"Camera {device_idx + 1}"
                self.camera_device_names.append(device_name)
                
                # Map camera to channel
                self.camera_channels[internal_idx] = channel
                
                # Apply camera settings from config
                self._apply_camera_settings(camera, channel)
            
            # Start ALL cameras grabbing simultaneously (before starting threads)
            # This ensures cameras start at the same time for synchronization
            for camera in self.cameras:
                camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
            
            # Small delay to ensure all cameras have started grabbing
            time.sleep(0.01)  # 10ms should be enough
            
            # Start acquisition threads (cameras are already grabbing)
            self.camera_threads = []
            for i, camera in enumerate(self.cameras):
                thread = CameraThread(camera, i, self)
                thread.imageAcquired.connect(self.on_image_acquired)
                thread.start()
                self.camera_threads.append(thread)
            
            # Show/hide fluorescent display based on camera mode
            if hasattr(self, 'fluorescent_frame'):
                if self.camera_mode == "BF only":
                    self.fluorescent_frame.setVisible(False)
                else:  # BF+FL
                    self.fluorescent_frame.setVisible(True)
            
            self.camera_initialized = True
            # Reset frame difference state when cameras are initialized
            self._reset_frame_difference_state()
            mode_text = "BF only" if self.camera_mode == "BF only" else "BF+FL"
            self.status_label.setText(f"Status: Cameras initialized ({mode_text}) - {len(devices)} device(s) found")
            self.init_button.setText("Reinitialize Cameras")
            self.init_button.setEnabled(True)
            self.init_button.setStyleSheet("""
                QPushButton {
                    background-color: #0078d7;
                    color: white;
                    border-radius: 5px;
                    padding: 8px 20px;
                    font-size: 12pt;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #005a9e;
                }
                QPushButton:disabled {
                    background-color: #d3d3d3;
                    color: #808080;
                }
            """)
            self.roi_button.setEnabled(True)
            self.roi_button.setChecked(False)
            self.roi_button.setText("Switch to ROI")
            self.roi_enabled = False
            # Enable detect ROI button
            if hasattr(self, 'detect_roi_button'):
                self.detect_roi_button.setEnabled(True)
            
            # After cameras are initialized: turn red LED on, blue LED off, enable ROI overlay
            if NIDAQMX_AVAILABLE:
                # Turn red LED on
                self.red_led_state = True
                self._set_led_state(self.red_led_address, True, self.red_led_on_state)
                if hasattr(self, 'red_led_button'):
                    self.red_led_button.setChecked(True)
                    self.red_led_button.setText("Red\nOn")
                
                # Turn blue LED off
                self.blue_led_state = False
                self._set_led_state(self.blue_led_address, False, self.blue_led_on_state)
                if hasattr(self, 'blue_led_button'):
                    self.blue_led_button.setChecked(False)
                    self.blue_led_button.setText("Blue\nOff")
            
            # Enable ROI overlay
            if hasattr(self, 'camera1_display'):
                self.camera1_display.show_detected_roi = True
            if hasattr(self, 'camera2_display'):
                self.camera2_display.show_detected_roi = True
            # Also enable the ROI detection button (which controls the overlay display)
            if hasattr(self, 'detect_roi_button'):
                self.detect_roi_button.setChecked(True)
                self.toggle_roi_detection(True)
            
            # Start FL driver current reading timer
            if NIDAQMX_AVAILABLE and hasattr(self, 'fl_driver_current_update_timer'):
                self.fl_driver_current_update_timer.start()
                # Read initial value immediately
                self._update_fl_driver_current()
            
            # Start photodiode reading timer
            if NIDAQMX_AVAILABLE and hasattr(self, 'photodiode_update_timer'):
                self.photodiode_update_timer.start()
                # Read initial value immediately
                self._update_photodiode()
            
            # Start ROI statistics update timer
            if hasattr(self, 'roi_stats_update_timer'):
                self.roi_stats_update_timer.start()
                # Update initial values immediately
                self._update_roi_statistics()
            
            # Start FL auto exposure timer if enabled
            if hasattr(self, 'fl_auto_exposure_timer') and self.fl_auto_exposure_enabled:
                self.fl_auto_exposure_timer.start()
            
        except Exception as e:
            self.status_label.setText(f"Status: Error - {str(e)}")
            self.init_button.setEnabled(True)
            self.init_button.setText("Initialize Cameras")
            self.init_button.setStyleSheet("""
                QPushButton {
                    background-color: #28a745;
                    color: white;
                    border-radius: 5px;
                    padding: 8px 20px;
                    font-size: 12pt;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #218838;
                }
                QPushButton:disabled {
                    background-color: #d3d3d3;
                    color: #808080;
                }
            """)
            self.roi_button.setEnabled(False)
            self.roi_button.setChecked(False)
            self.roi_button.setText("Switch to ROI")
            # Disable detect ROI button
            if hasattr(self, 'detect_roi_button'):
                self.detect_roi_button.setEnabled(False)
                self.detect_roi_button.setChecked(False)
            self.cleanup_cameras()
    
    def on_image_acquired(self, image_array, camera_index, metadata):
        """Handle acquired image from camera thread."""
        # Store full image size on first acquisition (original capture dimensions)
        current_height, current_width = image_array.shape[:2]
        if self.full_image_size is None:
            self.full_image_size = (current_height, current_width)
        
        # Update most recent exposure values from current settings when frame is received
        if 'brightfield' in self.camera_settings and 'exposure' in self.camera_settings['brightfield']:
            self.last_bf_exposure_us = self.camera_settings['brightfield']['exposure']
        elif hasattr(self, 'brightfield_exposure_spin'):
            self.last_bf_exposure_us = self.brightfield_exposure_spin.value()
        
        if 'fluorescent' in self.camera_settings and 'exposure' in self.camera_settings['fluorescent']:
            self.last_fl_exposure_us = self.camera_settings['fluorescent']['exposure']
        elif hasattr(self, 'fluorescent_exposure_spin'):
            self.last_fl_exposure_us = self.fluorescent_exposure_spin.value()
        
        # Update exposure indicator label
        if hasattr(self, 'fl_auto_exposure_value_label'):
            self.fl_auto_exposure_value_label.setText(f"{self.last_fl_exposure_us:.1f} µs")
            
        # Hard drop filter for hardware transition residuals (e.g. leftover free-run frames)
        if getattr(self, 'roi_enabled', False) and hasattr(self, 'roi_width') and hasattr(self, 'roi_height'):
            if current_width != getattr(self, 'roi_width') or current_height != getattr(self, 'roi_height'):
                return  # Silently drop residual unformatted frames without touching memory trackers
        
        # Virtual trigger count tracking for robust synchronization
        # Calculate exactly how many physical trigger pulses have elapsed based on hardware timestamps
        current_timestamp = metadata.get('camera_timestamp')
        if current_timestamp is not None:
            if camera_index not in self.last_camera_timestamp:
                self.last_camera_timestamp[camera_index] = current_timestamp
                # Force the very first frame received by the thread to be count 1
                # This prevents a permanent offset if one thread misses the first DAQ pulse
                self.virtual_trigger_count[camera_index] = 1
            else:
                # Calculate delta in nanoseconds
                delta_ns = current_timestamp - self.last_camera_timestamp[camera_index]
                # Expected period in ns from the ROI framerate (shared by both cameras)
                expected_period_ns = 1e9 / self.roi_framerate
                
                # Number of physical triggers elapsed (round to nearest whole trigger)
                # 1 means regular consecutive frame, >1 means hardware triggers were missed
                triggers_elapsed = max(1, int(round(delta_ns / expected_period_ns)))
                
                self.virtual_trigger_count[camera_index] += triggers_elapsed
                self.last_camera_timestamp[camera_index] = current_timestamp
            
            # Use the virtual trigger count as the image_number for pairing logic
            image_number = self.virtual_trigger_count[camera_index]
            metadata['image_number'] = image_number # Override for metadata saving
        else:
            # Fallback to hardware image number if timestamp is missing
            image_number = metadata.get('image_number')
        
        channel = self.camera_channels.get(camera_index)
        
        if image_number is not None and channel in ['brightfield', 'fluorescent']:
            if image_number not in self.frame_buffer:
                self.frame_buffer[image_number] = {}
            # Store frame and metadata for matching - array is already copied in CameraThread
            self.frame_buffer[image_number][channel] = (image_array, metadata)
            
            # Check if we now have a complete pair (both BF and FL) and add to preceding_frames_buffer
            # In BF-only mode, only check for BF frames (no FL frames expected)
            if self.camera_mode == "BF only":
                # In BF-only mode, add BF frames to buffer without waiting for FL frames
                # But don't add to preceding_frames_buffer (preceding frames not saved in BF-only mode)
                pass
            elif 'brightfield' in self.frame_buffer[image_number] and 'fluorescent' in self.frame_buffer[image_number]:
                # Only add to preceding_frames_buffer if this frame hasn't been saved yet and isn't already in the buffer
                if image_number not in self.saved_image_numbers:
                    # Check if this image_number is already in preceding_frames_buffer
                    already_in_buffer = any(entry[0] == image_number for entry in self.preceding_frames_buffer)
                    
                    if not already_in_buffer:
                        bf_frame, bf_metadata = self.frame_buffer[image_number]['brightfield']
                        fl_frame, fl_metadata = self.frame_buffer[image_number]['fluorescent']
                        
                        # Add to preceding_frames_buffer (FIFO, max 5 entries)
                        # We use 5 instead of 4 to ensure we always have 4 non-triggered frames
                        # even if the triggered frame is in the buffer
                        if len(self.preceding_frames_buffer) >= 5:
                            # Remove oldest entry
                            self.preceding_frames_buffer.pop(0)
                        
                        # Add new entry
                        self.preceding_frames_buffer.append((image_number, bf_frame, bf_metadata, fl_frame, fl_metadata))
            
            # Clean up old frames to prevent memory growth
            # In full-frame mode (ROI disabled), images are 12MB+ each. Keeping 1250 full frames = ~15-30 GB RAM!
            # We dynamically shrink the allowed buffer size if ROI is not enabled to protect OS memory.
            is_full_frame = not getattr(self, 'roi_enabled', False)
            dynamic_max_age = 15 if is_full_frame else self.frame_buffer_max_age
            cleanup_threshold = 5 if is_full_frame else 200

            # Optimization: Only cleanup if buffer exceeds dynamic_max_age by a threshold
            # to avoid sorting/deleting on every single frame.
            buffer_size = len(self.frame_buffer)
            if buffer_size > dynamic_max_age + cleanup_threshold:
                # Calculate how many frames to remove
                frames_to_remove = buffer_size - dynamic_max_age
                
                # Sort only when needed
                sorted_image_numbers = sorted(self.frame_buffer.keys())
                frames_to_delete = sorted_image_numbers[:frames_to_remove]
                
                # Remove from frame_buffer
                for img_num in frames_to_delete:
                    self.frame_buffer.pop(img_num, None)
                
                # Remove from preceding_frames_buffer
                if self.preceding_frames_buffer:
                    frames_to_delete_set = set(frames_to_delete)
                    self.preceding_frames_buffer = [
                        entry for entry in self.preceding_frames_buffer
                        if entry[0] not in frames_to_delete_set
                    ]
        
        # Implement display frame rate throttling (only affects display, not trigger detection)
        current_time = time.time()
        min_display_interval = 1.0 / self.display_framerate
        
        # Initialize last update time for this camera if not present
        if camera_index not in self.last_display_update_time:
            self.last_display_update_time[camera_index] = 0
        
        time_since_last = current_time - self.last_display_update_time[camera_index]
        
        # Determine if we should update display (throttling)
        should_update_display = time_since_last >= min_display_interval
        if should_update_display:
            # Update last display time
            self.last_display_update_time[camera_index] = current_time
        
        # Frame difference calculation (only for brightfield, only in ROI mode)
        # NOTE: This runs for ALL frames, not just displayed ones, to ensure accurate trigger detection
        # Enable frame difference when cameras are initialized and ROI mode is enabled
        if self.camera_initialized and self.roi_enabled and camera_index in self.camera_channels:
            channel = self.camera_channels[camera_index]
            if channel == 'brightfield':
                # Calculate frame difference (with shape validation)
                if self.previous_brightfield_frame is not None:
                    if image_array.shape == self.previous_brightfield_frame.shape:
                        try:
                            self.current_frame_difference = self._calculate_frame_difference(
                                image_array, self.previous_brightfield_frame, self.frame_difference_offset
                            )
                        except Exception as e:
                            print(f"Error in frame difference calculation: {e}")
                            self.current_frame_difference = 0.0
                    else:
                        # Shape mismatch (likely during ROI transition) - skip and reset
                        # print(f"Shape mismatch in frame diff: {image_array.shape} vs {self.previous_brightfield_frame.shape}")
                        self.previous_brightfield_frame = None
                        self.current_frame_difference = 0.0
                
                # Update plot (only if displaying this frame)
                if should_update_display:
                    self._update_frame_diff_plot()
                
                # Check if threshold exceeded and saving is enabled
                if self.image_saving_enabled and self.current_frame_difference > self.frame_difference_threshold:
                    
                    is_bf_only = (getattr(self, 'camera_mode', "BF+FL") == "BF only")
                    trigger_running = getattr(self, 'hardware_trigger_active', False)
                    debounce_elapsed = (time.time() - getattr(self, 'roi_transition_time', 0.0) >= 0.25)
                    
                    # Ensure pyPump is actively running an experiment condition
                    condition_running_now = getattr(self, 'condition_running', False)
                    safe_to_save = False
                    
                    if getattr(self, 'roi_enabled', False) and condition_running_now and debounce_elapsed:
                        if is_bf_only:
                            safe_to_save = True
                        else:
                            # For multi-camera tracking, explicitly require trigger to be running in ROI mode
                            safe_to_save = trigger_running

                    if not safe_to_save:
                        pass  # Skip saving during transition/initialization lag, or when running bead/clean routines
                    elif self.camera_mode == "BF only":
                        # Store metadata for triggered frame
                        self.last_triggered_metadata[camera_index] = metadata.copy()
                        
                        # In BF-only mode, save immediately without waiting for FL frames
                        # Skip preceding frames (not saved in BF-only mode)
                        # Save triggered frame with trigger_flag=1, no FL image
                        self._save_triggered_images(image_array, camera_index, None, None)
                    else:
                        # Store metadata for triggered frame
                        self.last_triggered_metadata[camera_index] = metadata.copy()
                        # BF+FL mode: Find matching fluorescent frame by image_number
                        # CRITICAL: Only save if we have BOTH BF and FL frames with the SAME image_number
                        # Note: We check once without blocking - if FL frame hasn't arrived yet,
                        # it will be handled when the FL frame arrives (see below)
                        matching_fl_frame = None
                        matching_fl_metadata = None
                        
                        if image_number is not None and image_number in self.frame_buffer:
                            if 'fluorescent' in self.frame_buffer[image_number]:
                                matching_fl_frame, matching_fl_metadata = self.frame_buffer[image_number]['fluorescent']
                        
                        # Only save if we have a matching FL frame with the same image_number
                        if matching_fl_metadata is not None and matching_fl_frame is not None:
                            # Verify image_numbers match
                            bf_frame_num = metadata.get('image_number')
                            fl_frame_num = matching_fl_metadata.get('image_number')
                            
                            if bf_frame_num == fl_frame_num:
                                # Store fluorescent metadata
                                for fl_idx, ch in self.camera_channels.items():
                                    if ch == 'fluorescent':
                                        self.last_triggered_metadata[fl_idx] = matching_fl_metadata.copy()
                                        break
                                
                                # First, save the 4 preceding non-triggered frames (if available)
                                # Iterate through preceding_frames_buffer and save each with trigger_flag=0
                                # Exclude the triggered frame itself (bf_frame_num) from the preceding frames
                                # Skip frames that were already saved (e.g., from a previous consecutive trigger)
                                frames_to_save = []
                                for prev_img_num, prev_bf_frame, prev_bf_meta, prev_fl_frame, prev_fl_meta in self.preceding_frames_buffer:
                                    # Skip the triggered frame itself - it will be saved separately with trigger_flag=1
                                    if prev_img_num == bf_frame_num:
                                        continue
                                    # Only save if not already saved (prevents duplicate saves from consecutive triggers)
                                    if prev_img_num not in self.saved_image_numbers:
                                        frames_to_save.append((prev_img_num, prev_bf_frame, prev_bf_meta, prev_fl_frame, prev_fl_meta))
                                
                                # Save all non-triggered, unsaved preceding frames
                                if frames_to_save:
                                    # Find brightfield camera index (same for all frames)
                                    prev_bf_camera_idx = None
                                    for cam_idx, ch in self.camera_channels.items():
                                        if ch == 'brightfield':
                                            prev_bf_camera_idx = cam_idx
                                            break
                                    
                                    if prev_bf_camera_idx is not None:
                                        for p_img_num, p_bf_frame, p_bf_meta, p_fl_frame, p_fl_meta in frames_to_save:
                                            self._save_non_triggered_frame(
                                                p_bf_frame, prev_bf_camera_idx, p_fl_frame, p_fl_meta, p_bf_meta
                                            )
                                
                                # Clear preceding_frames_buffer after saving
                                self.preceding_frames_buffer.clear()
                                
                                # Then save the triggered frame with trigger_flag=1
                                self._save_triggered_images(image_array, camera_index, matching_fl_frame, matching_fl_metadata)
                                # Image numbers don't match - skip (shouldn't happen if buffer is working correctly)
                                pass
                        # If FL frame not found, skip silently - it may arrive later and will be handled then
                
                # Store current frame as previous for next iteration - array is already independent
                try:
                    self.previous_brightfield_frame = image_array
                    self.frame_number += 1
                except Exception as e:
                    # Guard against errors when copying frame
                    print(f"Error copying frame: {e}")
                    self.previous_brightfield_frame = None
            elif channel == 'fluorescent':
                # Skip FL channel handling in BF-only mode
                if self.camera_mode == "BF only":
                    # In BF-only mode, FL frames are not processed for saving
                    pass
                else:
                    # Store last fluorescent frame for backward compatibility (deprecated - use frame_buffer)
                    try:
                        self.last_fluorescent_frame = image_array
                        # Store metadata for fluorescent camera
                        self._last_fluorescent_metadata = metadata
                        
                        # If this fluorescent frame matches a recently triggered brightfield frame,
                        # save both images together
                        if image_number is not None and self.image_saving_enabled:
                            # Check if this image_number was recently triggered (exists in last_triggered_metadata for BF)
                            for bf_idx, bf_ch in self.camera_channels.items():
                                if bf_ch == 'brightfield' and bf_idx in self.last_triggered_metadata:
                                    # Check if the brightfield metadata has the same image_number
                                    bf_metadata = self.last_triggered_metadata[bf_idx]
                                    bf_image_number = bf_metadata.get('image_number')
                                    if bf_image_number == image_number:
                                        # This fluorescent frame matches the triggered brightfield frame
                                        # Store fluorescent metadata
                                        self.last_triggered_metadata[camera_index] = metadata.copy()
                                        
                                        # Retrieve the brightfield frame from buffer and save both
                                        if image_number in self.frame_buffer:
                                            if 'brightfield' in self.frame_buffer[image_number]:
                                                bf_frame, bf_meta = self.frame_buffer[image_number]['brightfield']
                                                # Verify image numbers match
                                                if bf_meta.get('image_number') == image_number:
                                                    # First, save the 4 preceding non-triggered frames (if available)
                                                    # Iterate through preceding_frames_buffer and save each with trigger_flag=0
                                                    # Exclude the triggered frame itself (image_number) from the preceding frames
                                                    # Skip frames that were already saved (e.g., from a previous consecutive trigger)
                                                    frames_to_save = []
                                                    for prev_img_num, prev_bf_frame, prev_bf_meta, prev_fl_frame, prev_fl_meta in self.preceding_frames_buffer:
                                                        # Skip the triggered frame itself - it will be saved separately with trigger_flag=1
                                                        if prev_img_num == image_number:
                                                            continue
                                                        # Only save if not already saved (prevents duplicate saves from consecutive triggers)
                                                        if prev_img_num not in self.saved_image_numbers:
                                                            frames_to_save.append((prev_img_num, prev_bf_frame, prev_bf_meta, prev_fl_frame, prev_fl_meta))
                                                    
                                                    # Save all non-triggered, unsaved preceding frames
                                                    if frames_to_save:
                                                        # Find brightfield camera index (same for all frames)
                                                        prev_bf_camera_idx = None
                                                        for cam_idx, ch in self.camera_channels.items():
                                                            if ch == 'brightfield':
                                                                prev_bf_camera_idx = cam_idx
                                                                break
                                                        
                                                        if prev_bf_camera_idx is not None:
                                                            for prev_img_num, prev_bf_frame, prev_bf_meta, prev_fl_frame, prev_fl_meta in frames_to_save:
                                                                self._save_non_triggered_frame(
                                                                    prev_bf_frame, prev_bf_camera_idx, prev_fl_frame, prev_fl_meta, prev_bf_meta
                                                                )
                                                    
                                                    # Clear preceding_frames_buffer after saving
                                                    self.preceding_frames_buffer.clear()
                                                    
                                                    # Then save the triggered frame with trigger_flag=1
                                                    self._save_triggered_images(
                                                        bf_frame, bf_idx, image_array, metadata
                                                    )
                                        break
                    except Exception as e:
                        # Guard against errors when copying fluorescent frame
                        print(f"Error copying fluorescent frame: {e}")
        
        # Display update (only if throttling allows)
        if not should_update_display:
            return
        
        # Get device name and frame rate
        device_name = ""
        frame_rate = 0.0
        if camera_index < len(self.camera_device_names):
            device_name = self.camera_device_names[camera_index]
        if camera_index < len(self.camera_threads):
            frame_rate = self.camera_threads[camera_index].get_fps()
        
        # Determine display dimensions
        # If ROI is enabled, camera is capturing at ROI size, so show ROI dimensions
        # If ROI is disabled, camera is capturing at full size, so show full dimensions
        if self.roi_enabled:
            # Camera is capturing at ROI dimensions
            display_width = self.roi_width
            display_height = self.roi_height
        else:
            # Camera is capturing at full dimensions
            if camera_index in self.camera_max_width:
                display_width = self.camera_max_width[camera_index]
                display_height = self.camera_max_height[camera_index]
            else:
                display_width = current_width
                display_height = current_height
        
        # Display the image with appropriate dimensions based on channel mapping
        if camera_index in self.camera_channels:
            channel = self.camera_channels[camera_index]
            if channel == 'brightfield':
                self.camera1_display.set_image(image_array, device_name, frame_rate, display_width, display_height, self.overlay_width, self.overlay_height, self.roi_enabled, target_frame_rate=self.roi_framerate)
            elif channel == 'fluorescent':
                self.camera2_display.set_image(image_array, device_name, frame_rate, display_width, display_height, self.overlay_width, self.overlay_height, self.roi_enabled, target_frame_rate=self.roi_framerate)
        
        # Update histogram if available (only on displayed frames)
        if hasattr(self, 'histogram_widget') and camera_index in self.camera_channels:
            channel = self.camera_channels[camera_index]
            self.histogram_widget.update_histogram(
                image_array, channel, 
                roi_enabled=self.roi_enabled,
                overlay_width=self.overlay_width,
                overlay_height=self.overlay_height
            )
    
    def _set_camera_roi(self, camera_index, width, height, offset_x=None, offset_y=None):
        """Set the camera's ROI parameters (hardware ROI).
        
        Args:
            camera_index: Index of the camera
            width: ROI width
            height: ROI height
            offset_x: X offset for ROI (if None, centers horizontally)
            offset_y: Y offset for ROI (if None, centers vertically)
        """
        if camera_index >= len(self.cameras):
            return False
        
        camera = self.cameras[camera_index]
        try:
            # Get max dimensions for this camera
            max_width = self.camera_max_width.get(camera_index, 1920)
            max_height = self.camera_max_height.get(camera_index, 1200)
            
            # Clamp ROI dimensions to camera maximums
            roi_width = min(width, max_width)
            roi_height = min(height, max_height)
            
            # Stop grabbing before changing parameters
            if camera.IsGrabbing():
                camera.StopGrabbing()
            
            # Get camera's offset limits
            # Note: Some cameras may report 0 for max, but still support offset
            # We'll use max_width/max_height as fallback limits
            try:
                offset_x_max = int(camera.OffsetXMax.GetValue())
                offset_y_max = int(camera.OffsetYMax.GetValue())
                # If max is 0, it might mean "no offset" or "property unavailable"
                # Use image dimensions as fallback to allow offset
                if offset_x_max == 0:
                    offset_x_max = max_width - 1
                    self._roi_print(f"  Camera {camera_index} reported offset_x_max=0, using {offset_x_max} as limit")
                if offset_y_max == 0:
                    offset_y_max = max_height - 1
                    self._roi_print(f"  Camera {camera_index} reported offset_y_max=0, using {offset_y_max} as limit")
                self._roi_print(f"  Camera {camera_index} offset limits: X_max={offset_x_max}, Y_max={offset_y_max}")
            except Exception:
                # If we can't get offset limits, use image dimensions as limits
                offset_x_max = max_width - 1
                offset_y_max = max_height - 1
                self._roi_print(f"  Camera {camera_index} offset limits unavailable, using max dimensions: X_max={offset_x_max}, Y_max={offset_y_max}")
            
            # Get width and height increment requirements
            try:
                width_inc = int(camera.Width.GetInc())
            except Exception:
                width_inc = 1
            
            try:
                height_inc = int(camera.Height.GetInc())
            except Exception:
                height_inc = 1
            
            # Get offset increment requirements (many cameras require offsets to be aligned)
            try:
                offset_x_inc = int(camera.OffsetX.GetInc())
            except Exception:
                offset_x_inc = 1
            
            try:
                offset_y_inc = int(camera.OffsetY.GetInc())
            except Exception:
                offset_y_inc = 1
            
            # Align ROI dimensions to required increments
            roi_width = (roi_width // width_inc) * width_inc
            roi_height = (roi_height // height_inc) * height_inc
            
            # Ensure minimum size
            if roi_width < width_inc:
                roi_width = width_inc
            if roi_height < height_inc:
                roi_height = height_inc
            
            # Clamp to maximum dimensions
            roi_width = min(roi_width, max_width)
            roi_height = min(roi_height, max_height)
            
            # Calculate offset, respecting camera limits and increments
            # If offset not explicitly provided, default to horizontally centered
            if offset_x is None:
                offset_x = (max_width - roi_width) // 2
            
            # Clamp to limits and align to hardware increments
            original_offset_x = offset_x
            offset_x = max(0, min(offset_x, offset_x_max))
            # Align to offset increment using round-to-nearest instead of floor division
            # This minimizes the systematic physical positioning bias (e.g. crop box shifting systematically)
            offset_x = int(round(offset_x / offset_x_inc)) * offset_x_inc
            # Re-clamp to ensure rounding didn't push us infinitesimally over max bounds
            offset_x = min(offset_x, offset_x_max - (offset_x_max % offset_x_inc))
            
            if offset_x != original_offset_x:
                self._roi_print(f"  Offset X adjusted from {original_offset_x} to {offset_x} (increment: {offset_x_inc}, max: {offset_x_max})")
            
            # If offset not explicitly provided, default to vertically centered
            if offset_y is None:
                offset_y = (max_height - roi_height) // 2
            
            # Clamp to limits and align to hardware increments
            original_offset_y = offset_y
            offset_y = max(0, min(offset_y, offset_y_max))
            # Align to offset increment using round-to-nearest instead of floor division
            offset_y = int(round(offset_y / offset_y_inc)) * offset_y_inc
            # Re-clamp
            offset_y = min(offset_y, offset_y_max - (offset_y_max % offset_y_inc))
            
            if offset_y != original_offset_y:
                self._roi_print(f"  Offset Y adjusted from {original_offset_y} to {offset_y} (increment: {offset_y_inc}, max: {offset_y_max})")
            
            # Set ROI parameters according to manufacturer instructions:
            # 1. Make sure camera is idle (already stopped grabbing above)
            # 2. Set CenterX and CenterY to false (for ace Classic/U/L cameras)
            # 3. Set Width and Height
            # 4. Set OffsetX and OffsetY
            
            # Disable center mode if available (for ace Classic/U/L cameras)
            try:
                if hasattr(camera, 'CenterX'):
                    camera.CenterX.SetValue(False)
                    self._roi_print(f"  Camera {camera_index}: Set CenterX to False")
            except Exception:
                pass  # Camera may not have CenterX parameter
            
            try:
                if hasattr(camera, 'CenterY'):
                    camera.CenterY.SetValue(False)
                    self._roi_print(f"  Camera {camera_index}: Set CenterY to False")
            except Exception:
                pass  # Camera may not have CenterY parameter
            
            # Set size first (Width and Height)
            camera.Width.SetValue(roi_width)
            camera.Height.SetValue(roi_height)
            self._roi_print(f"  Camera {camera_index}: Set size to {roi_width}x{roi_height}")
            
            # Then set position (OffsetX and OffsetY)
            camera.OffsetX.SetValue(offset_x)
            camera.OffsetY.SetValue(offset_y)
            self._roi_print(f"  Camera {camera_index}: Set offset to ({offset_x}, {offset_y})")
            
            return True
        except Exception as e:
            self._roi_print(f"Error setting camera {camera_index} ROI: {e}")
            return False
    
    def _restore_camera_full_image(self, camera_index):
        """Restore camera to full image capture (maximum dimensions)."""
        if camera_index >= len(self.cameras):
            return False
        
        camera = self.cameras[camera_index]
        try:
            # Get original settings
            if camera_index not in self.camera_original_settings:
                return False
            
            original = self.camera_original_settings[camera_index]
            
            # Stop grabbing before changing parameters
            if camera.IsGrabbing():
                camera.StopGrabbing()
            
            # Restore original settings
            camera.OffsetX.SetValue(original['offset_x'])
            camera.OffsetY.SetValue(original['offset_y'])
            camera.Width.SetValue(original['width'])
            camera.Height.SetValue(original['height'])
            
            return True
        except Exception as e:
            print(f"Error restoring camera {camera_index} to full image: {e}")
            return False
    
    def toggle_roi(self, checked):
        """Toggle between ROI (hardware ROI) and Full Image (maximum camera dimensions) mode."""
        if not self.camera_initialized:
            self.roi_button.setChecked(False)
            self.roi_button.setText("Switch to ROI")
            return
        
        # Stop all cameras first so threads blocked in RetrieveResult unblock immediately
        for camera in self.cameras:
            if camera.IsGrabbing():
                camera.StopGrabbing()

        # Stop all camera threads before changing ROI settings
        for thread in self.camera_threads:
            if thread.isRunning():
                thread.stop()
                # Do NOT wait() here, it blocks the GUI event loop and causes stutters!
                # The thread will exit naturally since we called StopGrabbing()
        
        self.roi_enabled = checked
        self.roi_mode_toggled.emit(checked)
        self.roi_transition_time = time.time()
        
        # Stop camera trigger to save power/prevent desync when switching modes
        self._stop_camera_trigger()
        
        if checked:
            # Turn off ROI detection overlay when switching to ROI mode
            if hasattr(self, 'detect_roi_button') and self.detect_roi_button.isChecked():
                self.detect_roi_button.setChecked(False)
                self.toggle_roi_detection(False)

            # ROI mode: detect ROI and set camera to capture at detected ROI dimensions
            # After enabling ROI, button should say "Switch to Full Image"
            self.roi_button.setText("Switch to Full Image")
            
            # First, ensure we're in full image mode for detection
            # Restore full image for all cameras to get accurate detection
            for i in range(len(self.cameras)):
                self._restore_camera_full_image(i)
            
            # Perform ROI detection to get detected coordinates
            detected_roi_x = None
            detected_roi_y = None
            
            # Use camera's maximum dimensions (full image) for coordinate calculations
            if len(self.cameras) > 0 and 0 in self.camera_max_width:
                img_width = self.camera_max_width[0]
                img_height = self.camera_max_height[0]
            else:
                # Fallback: use default dimensions
                img_width = 1920
                img_height = 1200
            
            # Capture a fresh image directly from the camera for detection
            image = None
            if len(self.cameras) > 0:
                try:
                    camera = self.cameras[0]  # Use brightfield camera (index 0)
                    # Start grabbing temporarily
                    if not camera.IsGrabbing():
                        camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
                    # Grab a single image
                    grab_result = camera.RetrieveResult(500, pylon.TimeoutHandling_Return)
                    if grab_result.GrabSucceeded():
                        # Convert to numpy array
                        converter = pylon.ImageFormatConverter()
                        converter.OutputPixelFormat = pylon.PixelType_RGB8packed
                        converted = converter.Convert(grab_result)
                        img = converted.GetArray()
                        image = img.copy()
                    grab_result.Release()
                    # Stop grabbing
                    if camera.IsGrabbing():
                        camera.StopGrabbing()
                except Exception as e:
                    self._roi_print(f"Error capturing image for ROI detection: {e}")
                    # Fallback to using display image if available
                    if hasattr(self, 'camera1_display') and self.camera1_display._current_image_array is not None:
                        image = self.camera1_display._current_image_array
            
            if image is not None and image.size > 0:
                    # Calculate overlay rectangle position (centered in full image coordinates)
                    overlay_x = (img_width - self.overlay_width) // 2
                    overlay_y = (img_height - self.overlay_height) // 2
                    
                    # Detect edges using line detection or axis-aligned detection
                    if self.use_line_detection:
                        # Use derivative-based corner detection for angle tolerance
                        # Get verbose setting from checkbox state (source of truth)
                        verbose = self.report_roi_heuristics_check.isChecked() if hasattr(self, 'report_roi_heuristics_check') else self.report_roi_heuristics
                        top_line, bottom_line, left_line, right_line = detect_edges_with_lines(
                            image, overlay_x, overlay_y, self.overlay_width, self.overlay_height,
                            self.edge_threshold, self.min_brightness_diff, True,
                            self.vertical_line_threshold, self.corner_search_width,
                            self.center_exclusion_percent,
                            None, None, False, verbose=verbose
                        )
                        
                        # Validate that all 4 lines are detected
                        if (top_line is not None and bottom_line is not None and 
                            left_line is not None and right_line is not None):
                            self._roi_print(f"Detected lines - Top: {top_line}, Bottom: {bottom_line}, Left: {left_line}, Right: {right_line}")
                            detected_roi_x, detected_roi_y = calculate_roi_from_lines(
                                top_line, bottom_line, left_line, right_line,
                                overlay_x, overlay_y, self.roi_width, self.roi_height,
                                img_width, img_height
                            )
                            # Verify calculation succeeded
                            if detected_roi_x is None or detected_roi_y is None:
                                self._roi_print(f"Warning: ROI calculation failed - line intersections may be invalid")
                                self._roi_print(f"  Top: {top_line}, Bottom: {bottom_line}, Left: {left_line}, Right: {right_line}")
                            else:
                                self._roi_print(f"Calculated ROI from lines: ({detected_roi_x}, {detected_roi_y})")
                        else:
                            self._roi_print(f"Warning: Not all 4 lines detected for ROI calculation")
                            self._roi_print(f"  Top: {top_line is not None}, Bottom: {bottom_line is not None}, "
                                  f"Left: {left_line is not None}, Right: {right_line is not None}")
                    else:
                        # Use axis-aligned edge detection
                        # Get verbose setting from checkbox state (source of truth)
                        verbose = self.report_roi_heuristics_check.isChecked() if hasattr(self, 'report_roi_heuristics_check') else self.report_roi_heuristics
                        top_edge, bottom_edge, left_edge, right_edge = detect_edges(
                            image, overlay_x, overlay_y, self.overlay_width, self.overlay_height,
                            self.edge_threshold, self.min_brightness_diff, verbose=verbose
                        )
                        
                        # Validate that all 4 edges are detected
                        if (top_edge is not None and bottom_edge is not None and 
                            left_edge is not None and right_edge is not None):
                            self._roi_print(f"Detected edges - Top: {top_edge}, Bottom: {bottom_edge}, Left: {left_edge}, Right: {right_edge}")
                            self._roi_print(f"  (in overlay coordinates, overlay at ({overlay_x}, {overlay_y}))")
                            detected_roi_x, detected_roi_y = calculate_roi_from_edges(
                                top_edge, bottom_edge, left_edge, right_edge,
                                overlay_x, overlay_y, self.roi_width, self.roi_height,
                                img_width, img_height
                            )
                            # Verify calculation succeeded
                            if detected_roi_x is None or detected_roi_y is None:
                                self._roi_print(f"Warning: ROI calculation failed despite all edges detected")
                                self._roi_print(f"  Top: {top_edge}, Bottom: {bottom_edge}, Left: {left_edge}, Right: {right_edge}")
                            else:
                                self._roi_print(f"Calculated ROI from edges: ({detected_roi_x}, {detected_roi_y})")
                        else:
                            self._roi_print(f"Warning: Not all 4 edges detected for ROI calculation")
                            self._roi_print(f"  Top: {top_edge is not None}, Bottom: {bottom_edge is not None}, "
                                  f"Left: {left_edge is not None}, Right: {right_edge is not None}")
            
            # Set ROI for all cameras using channel-specific detected coordinates
            for i in range(len(self.cameras)):
                # Determine which channel this camera is for
                channel = self.camera_channels.get(i, 'brightfield')
                
                # Get channel-specific ROI coordinates
                channel_roi_x = None
                channel_roi_y = None
                
                if channel == 'fluorescent':
                    # Use FL detected coordinates if available
                    if hasattr(self, 'camera2_display') and self.camera2_display.detected_roi_x is not None and self.camera2_display.detected_roi_y is not None:
                        channel_roi_x = self.camera2_display.detected_roi_x
                        channel_roi_y = self.camera2_display.detected_roi_y
                        self._roi_print(f"Using FL detected ROI coordinates: ({channel_roi_x}, {channel_roi_y})")
                    elif detected_roi_x is not None and detected_roi_y is not None:
                        # Fallback to BF coordinates if FL detection hasn't run yet
                        channel_roi_x = detected_roi_x
                        channel_roi_y = detected_roi_y
                        self._roi_print(f"FL detection not available, using BF coordinates as fallback: ({channel_roi_x}, {channel_roi_y})")
                else:
                    # Brightfield channel: use BF detected coordinates
                    if hasattr(self, 'camera1_display') and self.camera1_display.detected_roi_x is not None and self.camera1_display.detected_roi_y is not None:
                        channel_roi_x = self.camera1_display.detected_roi_x
                        channel_roi_y = self.camera1_display.detected_roi_y
                    elif detected_roi_x is not None and detected_roi_y is not None:
                        channel_roi_x = detected_roi_x
                        channel_roi_y = detected_roi_y
                
                if channel_roi_x is not None and channel_roi_y is not None:
                    # Use channel-specific detected ROI coordinates
                    self._roi_print(f"Setting {channel} camera {i} ROI at detected location: ({channel_roi_x}, {channel_roi_y}) with size ({self.roi_width}, {self.roi_height})")
                    self._roi_print(f"  Full image dimensions: {img_width}x{img_height}")
                    if channel == 'brightfield':
                        self._roi_print(f"  Overlay position: ({overlay_x}, {overlay_y}), size: {self.overlay_width}x{self.overlay_height}")
                    success = self._set_camera_roi(i, self.roi_width, self.roi_height, channel_roi_x, channel_roi_y)
                    # Verify what was actually set
                    try:
                        camera = self.cameras[i]
                        actual_offset_x = int(camera.OffsetX.GetValue())
                        actual_offset_y = int(camera.OffsetY.GetValue())
                        actual_width = int(camera.Width.GetValue())
                        actual_height = int(camera.Height.GetValue())
                        self._roi_print(f"  Camera {i} ({channel}) ROI actually set to: offset=({actual_offset_x}, {actual_offset_y}), size=({actual_width}, {actual_height})")
                    except Exception as e:
                        self._roi_print(f"  Could not read back camera {i} ROI settings: {e}")
                else:
                    # Fallback: center ROI if detection failed
                    self._roi_print(f"Warning: {channel} ROI detection failed, centering ROI instead")
                    success = self._set_camera_roi(i, self.roi_width, self.roi_height)
                
                if not success:
                    self.status_label.setText(f"Status: Error setting ROI for camera {i + 1}")
                    self.roi_button.setChecked(False)
                    self.roi_button.setText("Switch to ROI")
                    self.roi_enabled = False
                    # Restart threads
                    self._restart_camera_threads()
                    return
            
            # Configure cameras for trigger mode on Line 1 (only if not BF only mode)
            if self.camera_mode == "BF only":
                self._configure_all_cameras_trigger(enable=False)
                self._roi_print("BF only mode: hardware trigger disabled (free-run)")
            else:
                if not self._configure_all_cameras_trigger(enable=True):
                    self.status_label.setText("Status: Warning - Could not configure all cameras for trigger mode")
            
            # Synchronize cameras for frame number alignment
            self._synchronize_cameras()
            
            # Restart camera threads (this starts cameras grabbing)
            # This must happen before starting the trigger signal. Any residual frames in the 
            # hardware buffer will be acquired immediately by the threads.
            self._restart_camera_threads()
            
            # Add delay to ensure cameras are fully initialized before starting trigger
            # At 1250 Hz, 100ms gives cameras time to initialize and clear any residual buffers
            time.sleep(0.1)  # 100ms delay
            
            # Reset frame difference state *after* threads have cleared residual hardware buffers
            # and before we start the external trigger for ROI comparison.
            self._reset_frame_difference_state()
            
            # Start DAQ square wave generation for camera trigger (only if not BF only mode)
            # This happens after cameras are ready and buffers are perfectly cleanly flushed
            if self.camera_mode == "BF only":
                self._stop_camera_trigger()
            else:
                if not self._start_camera_trigger():
                    self.status_label.setText("Status: Warning - Could not start camera trigger signal")
            
            # Get max dimensions for status message
            if len(self.cameras) > 0 and 0 in self.camera_max_width:
                max_width = self.camera_max_width[0]
                max_height = self.camera_max_height[0]
                if detected_roi_x is not None and detected_roi_y is not None:
                    self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height} at {detected_roi_x},{detected_roi_y} from {max_width}x{max_height})")
                else:
                    self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height} from {max_width}x{max_height})")
            else:
                self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height})")
        else:
            # Turn on ROI detection overlay when switching to Full Image mode
            if hasattr(self, 'detect_roi_button') and not self.detect_roi_button.isChecked():
                self.detect_roi_button.setChecked(True)
                self.toggle_roi_detection(True)

            # Full Image mode: restore camera to maximum dimensions
            # After disabling ROI, button should say "Switch to ROI"
            self.roi_button.setText("Switch to ROI")
            
            # Stop DAQ square wave generation
            self._stop_camera_trigger()
            
            # Configure cameras for free-run mode (disable trigger)
            self._configure_all_cameras_trigger(enable=False)
            
            # Restore full image for all cameras
            for i in range(len(self.cameras)):
                success = self._restore_camera_full_image(i)
                if not success:
                    self.status_label.setText(f"Status: Error restoring full image for camera {i + 1}")
                    return
            
            # Get max dimensions for status message
            if len(self.cameras) > 0 and 0 in self.camera_max_width:
                max_width = self.camera_max_width[0]
                max_height = self.camera_max_height[0]
                self.status_label.setText(f"Status: Full Image mode ({max_width}x{max_height})")
            else:
                self.status_label.setText("Status: Full Image mode")
            
            # Restart camera threads with new settings (for full image mode)
            self._restart_camera_threads()
            
            # Add short delay to clear any residual buffers from ROI mode, then reset state
            time.sleep(0.05)
            self._reset_frame_difference_state()
    
    def _set_led_state(self, led_address, state, on_state):
        """Set LED state using nidaqmx.
        
        Args:
            led_address: Channel address (e.g., "port0/line1")
            state: Desired LED state (True for on, False for off)
            on_state: True if True turns LED on, False if False turns LED on
        """
        if not NIDAQMX_AVAILABLE:
            return False
        
        try:
            # Determine the actual output value based on on_state configuration
            if on_state:
                output_value = state  # True = on, False = off
            else:
                output_value = not state  # False = on, True = off
            
            # Construct full channel name
            channel_name = f"{self.daq_name}/{led_address}"
            
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(channel_name)
                task.write(output_value)
            return True
        except Exception as e:
            print(f"Error setting LED state: {e}")
            return False
    
    def _update_fl_driver_current(self):
        """Read differential voltage from FL driver current channel and update display."""
        if not NIDAQMX_AVAILABLE:
            if hasattr(self, 'fl_driver_current_value_label'):
                self.fl_driver_current_value_label.setText("N/A")
            return
        
        if not hasattr(self, 'fl_driver_current_value_label'):
            return
        
        try:
            # Construct full channel name for differential input
            # For differential input, use format like "Dev1/ai3" with differential terminal config
            # Note: fl_driver_current_address is the physical hardware address (e.g., "ai3") stored in config as blue_current_monitor_daq_address
            channel_name = f"{self.daq_name}/{self.fl_driver_current_address}"
            
            with nidaqmx.Task() as task:
                # Add analog input channel with differential terminal configuration
                task.ai_channels.add_ai_voltage_chan(
                    channel_name,
                    terminal_config=nidaqmx.constants.TerminalConfiguration.DIFF
                )
                # Read voltage value
                voltage = task.read()
            
            # Calculate current: voltage * 2 (with units A)
            current_amps = voltage * 2.0
            
            # Store most recent value for metadata saving
            self.last_blue_led_current_a = current_amps
            
            # Format to 2 decimal places with units
            self.fl_driver_current_value_label.setText(f"{current_amps:.2f} A")
            
            # Update blue LED current plot if widget exists
            if hasattr(self, 'blue_led_current_plot') and self.blue_led_current_plot is not None:
                self.blue_led_current_plot.update_current(current_amps)
        except Exception as e:
            # On error, display error message or N/A
            self.fl_driver_current_value_label.setText("N/A")
    
    def _update_photodiode(self):
        """Read differential voltage from photodiode channel and update plot."""
        if not NIDAQMX_AVAILABLE:
            return
        
        if not hasattr(self, 'photodiode_plot') or self.photodiode_plot is None:
            return
        
        if self.daq_name is None or self.photodiode_address is None:
            return
        
        try:
            # Construct full channel name for differential input
            # For differential input, use format like "Dev1/aiX" with differential terminal config
            channel_name = f"{self.daq_name}/{self.photodiode_address}"
            
            with nidaqmx.Task() as task:
                # Add analog input channel with differential terminal configuration
                task.ai_channels.add_ai_voltage_chan(
                    channel_name,
                    terminal_config=nidaqmx.constants.TerminalConfiguration.DIFF
                )
                # Read voltage value
                voltage = task.read()
            
            # Store most recent value for metadata saving
            self.last_photodiode_voltage_v = voltage
            
            # Update photodiode plot
            self.photodiode_plot.update_voltage(voltage)
        except Exception:
            # On error, silently fail (plot will just not update)
            pass
    
    def _start_camera_trigger(self):
        """Start continuous square wave generation for camera trigger.
        
        Generates a square wave at roi_framerate Hz with 50% duty cycle,
        oscillating between 0V and 5V on the camera_trigger DAQ channel.
        """
        if not NIDAQMX_AVAILABLE:
            print("Warning: nidaqmx not available, cannot start camera trigger")
            return False
        
        if self.daq_name is None or self.camera_trigger_address is None:
            print("Warning: DAQ info not available, cannot start camera trigger")
            return False
        
        # Stop any existing trigger task first
        self._stop_camera_trigger()
        
        try:
            # Construct full channel name (e.g., "Dev1/ao1")
            channel_name = f"{self.daq_name}/{self.camera_trigger_address}"
            
            # Calculate period and sample rate
            framerate = self.roi_framerate
            
            # USB-6001 OEM has max sample rate of 5 kHz (5000 Hz) for analog output
            max_daq_sample_rate = 5000.0 
            
            # We must ensure that sample_rate / framerate is an INTEGER to get the exact frequency.
            # We want the highest possible number of samples per period that is <= max_daq_sample_rate.
            samples_per_period = int(max_daq_sample_rate / framerate)
            
            # Ensure we have at least 2 samples per period (minimum for a square wave)
            if samples_per_period < 2:
                samples_per_period = 2
                sample_rate = samples_per_period * framerate
                if sample_rate > max_daq_sample_rate:
                    print(f"Warning: Requested frame rate {framerate} Hz exceeds DAQ capabilities. Max: {max_daq_sample_rate/2} Hz")
                    return False
            else:
                # Calculate exactly what sample rate we need to reach the target frequency precisely
                sample_rate = float(samples_per_period * framerate)
                
            # Period in seconds
            period = 1.0 / framerate
            
            # Generate one period of square wave (50% duty cycle)
            # High for first half, low for second half
            half_samples = samples_per_period // 2
            square_wave = np.concatenate([
                np.full(half_samples, 5.0),  # High: 5V
                np.full(samples_per_period - half_samples, 0.0)  # Low: 0V
            ])
            
            # Create DAQ task
            self.trigger_task = nidaqmx.Task()
            self.trigger_task.ao_channels.add_ao_voltage_chan(channel_name, min_val=0.0, max_val=5.0)
            
            # Configure timing for continuous generation
            self.trigger_task.timing.cfg_samp_clk_timing(
                rate=sample_rate,
                sample_mode=nidaqmx.constants.AcquisitionType.CONTINUOUS,
                samps_per_chan=len(square_wave)
            )
            
            # Write initial data
            self.trigger_task.write(square_wave, auto_start=False)
            
            # Configure regeneration to repeat the waveform
            self.trigger_task.out_stream.regen_mode = nidaqmx.constants.RegenerationMode.ALLOW_REGENERATION
            
            # Start the task
            self.trigger_task.start()
            
            self.hardware_trigger_active = True
            
            print(f"Started camera trigger at {framerate} Hz on {channel_name}")
            return True
            
        except Exception as e:
            print(f"Error starting camera trigger: {e}")
            if self.trigger_task is not None:
                try:
                    self.trigger_task.close()
                except:
                    pass
                self.trigger_task = None
            return False
    
    def _stop_camera_trigger(self):
        """Stop and cleanup camera trigger DAQ task."""
        if self.trigger_task is not None:
            try:
                self.trigger_task.stop()
                self.trigger_task.close()
            except Exception as e:
                print(f"Error stopping camera trigger: {e}")
            finally:
                self.trigger_task = None
                self.hardware_trigger_active = False
                
    def _update_camera_setting_safely(self, update_func):
        """Helper to pause DAQ triggers, wait for camera idle, execute update, and resume.
        Prevents 'step' desync caused by rewriting GenICam nodes while triggered.
        """
        was_triggered = False
        # Only pause trigger if we are in ROI mode (external trigger active)
        if self.roi_enabled and self.camera_initialized:
            # Check if trigger task exists and is likely running
            if hasattr(self, 'trigger_task') and self.trigger_task is not None:
                self._stop_camera_trigger()
                was_triggered = True
                time.sleep(0.1)  # 100ms quiescent wait for camera to finish last trigger
                
        # Execute the actual camera node updates
        update_func()
        
        if was_triggered:
            time.sleep(0.05)  # 50ms settling wait after internal camera update
            self._start_camera_trigger()
            # Reset frame difference state as the pause likely created a 'glitch' frame
            self._reset_frame_difference_state()
    
    def _update_trigger_frequency(self):
        """Update the frequency of the running camera trigger task."""
        if self.trigger_task is None or not self.roi_enabled or self.camera_mode == "BF only":
            return False
        
        # Stop and restart with new frequency
        return self._start_camera_trigger()
    
    def _set_camera_node(self, camera, node_name, value, logger_prefix=""):
        """Helper to set a camera node with support for 'Bsl' prefix and alternate names.
        This is ultra-robust, catching all exceptions and falling back to NodeMap access.
        """
        aliases = {
            "TriggerMode": ["BslTriggerMode"],
            "TriggerSource": ["BslTriggerSource"],
            "TriggerActivation": ["BslTriggerActivation"],
            "TriggerSelector": ["BslTriggerSelector"],
            "TriggerOverlap": ["BslTriggerOverlap", "OverlapMode"],
            "AcquisitionFrameRateEnable": ["BslAcquisitionFrameRateEnable"],
            "AcquisitionFrameRate": ["BslAcquisitionFrameRate"],
            "TriggerDelay": ["BslTriggerDelay", "TriggerDelayAbs"],
            "TimerSelector": ["BslTimerSelector"],
            "TimerTriggerSource": ["BslTimerTriggerSource"],
            "TimerTriggerActivation": ["BslTimerTriggerActivation"],
            "TimerDelayAbs": ["TimerDelay", "BslTimerDelayAbs", "BslTimerDelay"],
            "TimerDurationAbs": ["TimerDuration", "BslTimerDurationAbs", "BslTimerDuration"],
        }
        
        try_names = [node_name, f"Bsl{node_name}"]
        if node_name in aliases:
            try_names.extend(aliases[node_name])
            
        for name in try_names:
            try:
                # 1. Try direct attribute access
                if hasattr(camera, name):
                    node = getattr(camera, name)
                    node.SetValue(value)
                    return True
                
                # 2. Try NodeMap access (more robust for some Ace 2 features)
                nodemap = camera.GetNodeMap()
                if nodemap.GetNode(name):
                    nodemap.GetNode(name).SetValue(value)
                    return True
            except:
                continue # Try next name
        return False

    def _get_camera_node_value(self, camera, node_name, default=None):
        """Safely get a node value with alias support."""
        aliases = {
            "TriggerSource": ["BslTriggerSource"],
            "TriggerDelay": ["BslTriggerDelay", "TriggerDelayAbs"],
        }
        try_names = [node_name, f"Bsl{node_name}"]
        if node_name in aliases:
            try_names.extend(aliases[node_name])
            
        for name in try_names:
            try:
                if hasattr(camera, name):
                    return getattr(camera, name).GetValue()
                nodemap = camera.GetNodeMap()
                if nodemap.GetNode(name):
                    return nodemap.GetNode(name).GetValue()
            except:
                continue
        return default

    def _configure_camera_trigger(self, camera_index, enable=True):
        """Configure camera trigger settings for Line 1.
        
        Args:
            camera_index: Index of the camera to configure
            enable: If True, enable trigger on Line 1; if False, disable trigger (free-run)
        
        Returns:
            True if successful, False otherwise
        """
        if camera_index >= len(self.cameras):
            return False
        
        camera = self.cameras[camera_index]
        try:
            if enable:
                # Enable trigger mode
                self._set_camera_node(camera, "TriggerMode", "On", f"Camera {camera_index}")
                
                # Set trigger source to Line1 (external hardware trigger)
                self._set_camera_node(camera, "TriggerSource", "Line1", f"Camera {camera_index}")
                self._set_camera_node(camera, "TriggerActivation", "RisingEdge", f"Camera {camera_index}")
                
                # Disable internal frame rate control when using external trigger
                self._set_camera_node(camera, "AcquisitionFrameRateEnable", False, f"Camera {camera_index}")
                
                # Enable trigger overlap to allow exposure during readout
                # (Crucial for maintaining full frame rate when TriggerDelay is used)
                self._set_camera_node(camera, "TriggerOverlap", "Readout", f"Camera {camera_index}")
                
                # Try to set FrameStart trigger selector for better synchronization
                self._set_camera_node(camera, "TriggerSelector", "FrameStart", f"Camera {camera_index}")
            else:
                # Disable trigger mode (free-run)
                self._set_camera_node(camera, "TriggerMode", "Off", f"Camera {camera_index}")
                
                # Check if this is the final clean sequence
                is_final_clean = False
                if hasattr(self, 'smr_widget') and self.smr_widget:
                    is_final_clean = getattr(self.smr_widget, 'is_final_clean', False)
                
                # In BF only ROI mode, we want to set a target frame rate via camera settings
                # since we're not using the external hardware trigger.
                if self.roi_enabled and self.camera_mode == "BF only":
                    self._set_camera_node(camera, "AcquisitionFrameRateEnable", True, f"Camera {camera_index}")
                    self._set_camera_node(camera, "AcquisitionFrameRate", float(self.roi_framerate), f"Camera {camera_index}")
                elif is_final_clean:
                    # During Final Clean, throttle free-run frame rate to 5.0 Hz to prevent
                    # overloading the GUI event loop and GIL contention during post-hoc analysis.
                    self._set_camera_node(camera, "AcquisitionFrameRateEnable", True, f"Camera {camera_index}")
                    self._set_camera_node(camera, "AcquisitionFrameRate", 5.0, f"Camera {camera_index}")
                else:
                    # In other free-run modes (like full image), disable acquisition frame rate control
                    # to allow the camera to run at its maximum possible speed.
                    self._set_camera_node(camera, "AcquisitionFrameRateEnable", False, f"Camera {camera_index}")
                
                # print(f"Camera {camera_index} configured for free-run mode")
            return True
        except Exception as e:
            print(f"Error configuring camera {camera_index} trigger: {e}")
            return False
    
    def _configure_all_cameras_trigger(self, enable=True):
        """Configure all cameras for trigger mode.
        
        Args:
            enable: If True, enable trigger on Line 1; if False, disable trigger (free-run)
        
        Returns:
            True if all cameras configured successfully, False otherwise
        """
        success = True
        for i in range(len(self.cameras)):
            if not self._configure_camera_trigger(i, enable):
                success = False
        return success
    
    def _synchronize_cameras(self):
        """Synchronize all cameras for frame number and timestamp alignment.
        
        This method ensures that when cameras are triggered by the same line,
        their frame counters and timestamps are synchronized. It should be called
        after configuring triggers and before starting acquisition.
        """
        if len(self.cameras) < 2:
            return  # No need to synchronize single camera
        
        try:
            # Step 1: Ensure all cameras are stopped
            for camera in self.cameras:
                if camera.IsGrabbing():
                    camera.StopGrabbing()
            
            # Step 2: Reset timestamps (if supported by camera model)
            # Basler cameras support TimestampReset to reset the internal clock
            timestamp_reset_supported = False
            for i, camera in enumerate(self.cameras):
                try:
                    # Try to reset timestamp counter (if available)
                    if hasattr(camera, 'TimestampReset'):
                        camera.TimestampReset.Execute()
                        timestamp_reset_supported = True
                        print(f"Camera {i}: Timestamp reset via TimestampReset")
                    elif hasattr(camera, 'TimestampLatch'):
                        # Some cameras use TimestampLatch to synchronize
                        camera.TimestampLatch.Execute()
                        timestamp_reset_supported = True
                        print(f"Camera {i}: Timestamp synchronized via TimestampLatch")
                except Exception as e:
                    # Timestamp reset may not be available on all camera models
                    pass
            
            if timestamp_reset_supported:
                print("Camera timestamps reset for synchronization")
            
            # Step 3: Reset frame counters (if supported by camera model)
            # Some Basler cameras support FrameStart to reset counters
            for i, camera in enumerate(self.cameras):
                try:
                    # Try to reset frame counter (if available)
                    if hasattr(camera, 'FrameStart'):
                        camera.FrameStart.Execute()
                        print(f"Camera {i}: Frame counter reset via FrameStart")
                except Exception as e:
                    # FrameStart may not be available on all camera models
                    pass
            
            # Step 4: Use AcquisitionStart to synchronize camera start
            # This ensures all cameras start at the same time
            acquisition_started = False
            for i, camera in enumerate(self.cameras):
                try:
                    # Use AcquisitionStart to synchronize start
                    if hasattr(camera, 'AcquisitionStart'):
                        camera.AcquisitionStart.Execute()
                        acquisition_started = True
                        print(f"Camera {i}: AcquisitionStart executed")
                except Exception as e:
                    print(f"Camera {i}: AcquisitionStart not available or failed: {e}")
            
            if acquisition_started:
                print("Cameras synchronized using AcquisitionStart")
            else:
                # Fallback: cameras will be synchronized by the trigger signal
                # when they start grabbing, but frame counters may not reset
                print("Note: AcquisitionStart not available - cameras will sync via trigger signal")
        
        except Exception as e:
            print(f"Error synchronizing cameras: {e}")
            # Continue anyway - cameras will still work, just may not be perfectly synchronized
    
    def _update_roi_statistics(self):
        """Update ROI statistics (intensity and saturated pixels) for BF and FL channels."""
        # Update saved frames count if needed
        if getattr(self, '_frames_count_needs_update', False) and hasattr(self, 'saved_frames_count_label'):
            self.saved_frames_count_label.setText(str(self.total_saved_frames))
            self._frames_count_needs_update = False
            
        if not self.camera_initialized:
            return
        
        # Get current images from displays
        bf_image = None
        fl_image = None
        
        if hasattr(self, 'camera1_display') and self.camera1_display._current_image_array is not None:
            bf_image = self.camera1_display._current_image_array
        
        if hasattr(self, 'camera2_display') and self.camera2_display._current_image_array is not None:
            fl_image = self.camera2_display._current_image_array
        
        # Get ROI coordinates separately for each channel - use detected ROI if available, otherwise center
        # Brightfield ROI coordinates
        bf_roi_x = None
        bf_roi_y = None
        bf_roi_width = self.roi_width
        bf_roi_height = self.roi_height
        
        if hasattr(self, 'camera1_display'):
            if self.camera1_display.detected_roi_x is not None and self.camera1_display.detected_roi_y is not None:
                # Use detected ROI coordinates for brightfield
                bf_roi_x = self.camera1_display.detected_roi_x
                bf_roi_y = self.camera1_display.detected_roi_y
            elif self.roi_enabled:
                # In ROI mode, the entire image is the ROI (camera already captures at ROI size)
                if bf_image is not None:
                    img_height, img_width = bf_image.shape[:2]
                    bf_roi_x = 0
                    bf_roi_y = 0
                    bf_roi_width = img_width
                    bf_roi_height = img_height
                else:
                    bf_roi_x = 0
                    bf_roi_y = 0
            else:
                # In full image mode, center the ROI
                if bf_image is not None:
                    img_height, img_width = bf_image.shape[:2]
                    bf_roi_x = (img_width - self.roi_width) // 2
                    bf_roi_y = (img_height - self.roi_height) // 2
        
        # Fluorescent ROI coordinates
        fl_roi_x = None
        fl_roi_y = None
        fl_roi_width = self.roi_width
        fl_roi_height = self.roi_height
        
        if hasattr(self, 'camera2_display'):
            if self.camera2_display.detected_roi_x is not None and self.camera2_display.detected_roi_y is not None:
                # Use detected ROI coordinates for fluorescent (calculated from corner center)
                fl_roi_x = self.camera2_display.detected_roi_x
                fl_roi_y = self.camera2_display.detected_roi_y
                # Use detected ROI dimensions if available
                if self.camera2_display.detected_roi_width > 0:
                    fl_roi_width = self.camera2_display.detected_roi_width
                if self.camera2_display.detected_roi_height > 0:
                    fl_roi_height = self.camera2_display.detected_roi_height
            elif self.roi_enabled:
                # In ROI mode, the entire image is the ROI (camera already captures at ROI size)
                if fl_image is not None:
                    img_height, img_width = fl_image.shape[:2]
                    fl_roi_x = 0
                    fl_roi_y = 0
                    fl_roi_width = img_width
                    fl_roi_height = img_height
                else:
                    fl_roi_x = 0
                    fl_roi_y = 0
            else:
                # In full image mode, center the ROI
                if fl_image is not None:
                    img_height, img_width = fl_image.shape[:2]
                    fl_roi_x = (img_width - self.roi_width) // 2
                    fl_roi_y = (img_height - self.roi_height) // 2
        
        # Update BF statistics
        if bf_image is not None and bf_roi_x is not None and bf_roi_y is not None:
            bf_mode, bf_mean, bf_saturated = self._calculate_roi_statistics(
                bf_image, bf_roi_x, bf_roi_y, bf_roi_width, bf_roi_height, channel='brightfield', use_otsu_threshold=False
            )
            if bf_mode is not None:
                if hasattr(self, 'bf_intensity_value'):
                    self.bf_intensity_value.setText(f"{bf_mode:.1f}")
            if bf_saturated is not None:
                if hasattr(self, 'bf_saturated_value'):
                    self.bf_saturated_value.setText(f"{bf_saturated}")
        else:
            if hasattr(self, 'bf_intensity_value'):
                self.bf_intensity_value.setText("N/A")
            if hasattr(self, 'bf_saturated_value'):
                self.bf_saturated_value.setText("N/A")
        
        # Update FL statistics
        if fl_image is not None and fl_roi_x is not None and fl_roi_y is not None:
            fl_mode, fl_mean, fl_saturated = self._calculate_roi_statistics(
                fl_image, fl_roi_x, fl_roi_y, fl_roi_width, fl_roi_height, channel='fluorescent', use_otsu_threshold=True
            )
            if fl_mode is not None:
                if hasattr(self, 'fl_intensity_value'):
                    self.fl_intensity_value.setText(f"{fl_mode:.1f}")
            if fl_saturated is not None:
                if hasattr(self, 'fl_saturated_value'):
                    self.fl_saturated_value.setText(f"{fl_saturated}")
        else:
            if hasattr(self, 'fl_intensity_value'):
                self.fl_intensity_value.setText("N/A")
            if hasattr(self, 'fl_saturated_value'):
                self.fl_saturated_value.setText("N/A")
    
    def _update_fl_auto_exposure(self):
        """Update FL camera exposure based on step-based algorithm (called every 100ms by timer).
        
        Algorithm:
        1. If mode < target_value: increase exposure by 5µs
        2. Continue until saturated pixels appear
        3. Decrease exposure by 5µs until no saturated pixels
        4. Disable auto exposure
        """
        if not self.fl_auto_exposure_enabled or not self.camera_initialized:
            return
        
        # Get current fluorescent image from display
        fl_image = None
        if hasattr(self, 'camera2_display') and self.camera2_display._current_image_array is not None:
            fl_image = self.camera2_display._current_image_array
        
        if fl_image is None:
            return
        
        # Get FL ROI coordinates (same logic as _update_roi_statistics)
        fl_roi_x = None
        fl_roi_y = None
        fl_roi_width = self.roi_width
        fl_roi_height = self.roi_height
        
        if hasattr(self, 'camera2_display'):
            if self.camera2_display.detected_roi_x is not None and self.camera2_display.detected_roi_y is not None:
                # Use detected ROI coordinates for fluorescent
                fl_roi_x = self.camera2_display.detected_roi_x
                fl_roi_y = self.camera2_display.detected_roi_y
                # Use detected ROI dimensions if available
                if self.camera2_display.detected_roi_width > 0:
                    fl_roi_width = self.camera2_display.detected_roi_width
                if self.camera2_display.detected_roi_height > 0:
                    fl_roi_height = self.camera2_display.detected_roi_height
            elif self.roi_enabled:
                # In ROI mode, the entire image is the ROI (camera already captures at ROI size)
                img_height, img_width = fl_image.shape[:2]
                fl_roi_x = 0
                fl_roi_y = 0
                fl_roi_width = img_width
                fl_roi_height = img_height
            else:
                # In full image mode, center the ROI or use overlay region
                img_height, img_width = fl_image.shape[:2]
                fl_roi_x = (img_width - self.roi_width) // 2
                fl_roi_y = (img_height - self.roi_height) // 2
        
        if fl_roi_x is None or fl_roi_y is None:
            return
        
        # Extract ROI region from image (similar to _calculate_roi_statistics)
        # Convert to grayscale if needed
        if len(fl_image.shape) == 3:
            # For RGB images, convert to grayscale while preserving bit depth
            # (Avoid astype(np.uint8) which causes wrap-around for >8-bit data)
            if fl_image.shape[2] == 3:
                gray = np.mean(fl_image, axis=2).astype(fl_image.dtype)
            elif fl_image.shape[2] == 4:
                gray = np.mean(fl_image[:, :, :3], axis=2).astype(fl_image.dtype)
            else:
                return
        else:
            gray = fl_image.copy()
        
        # Get image dimensions
        img_height, img_width = gray.shape[:2]
        
        # Clamp ROI to image bounds
        fl_roi_x = max(0, min(int(fl_roi_x), img_width - 1))
        fl_roi_y = max(0, min(int(fl_roi_y), img_height - 1))
        roi_right = min(fl_roi_x + int(fl_roi_width), img_width)
        roi_bottom = min(fl_roi_y + int(fl_roi_height), img_height)
        
        # Extract ROI region
        roi_region = gray[fl_roi_y:roi_bottom, fl_roi_x:roi_right]
        
        if roi_region.size == 0:
            return
        
        # Get current exposure value
        current_exposure = self.last_fl_exposure_us
        if current_exposure <= 0:
            return
        
        try:
            # Calculate mode value using Otsu threshold
            mode_value = self._calculate_mode(roi_region, channel='fluorescent', use_otsu_threshold=True)
            
            # Check for saturated pixels using the camera's actual bit depth
            # (12-bit cameras saturate at 4095, but are stored in uint16 with max 65535)
            bit_depth = self.camera_bit_depths.get('fluorescent', 8)
            saturation_threshold = (2 ** bit_depth) - 1
            
            # Count pixels at or above saturation threshold
            saturated_count = int(np.sum(roi_region >= saturation_threshold))
            has_saturated_pixels = saturated_count > 0
            
            new_exposure = current_exposure
            
            # Rule 1: Increase if no saturated pixels and mode is below target
            if not has_saturated_pixels and mode_value < self.fl_auto_exposure_target_value:
                new_exposure = current_exposure + self.fl_auto_exposure_step_size
                new_exposure = min(new_exposure, self.fl_auto_exposure_max)
                self.last_fl_ae_action = 'increase'
                
                # Bounds check: Give up if we hit max and still no saturation
                if new_exposure == current_exposure and current_exposure >= self.fl_auto_exposure_max:
                    self._disable_fl_auto_exposure()
                    return

            # Rule 2: Decrease if saturated pixels exist
            elif has_saturated_pixels:
                new_exposure = current_exposure - self.fl_auto_exposure_step_size
                new_exposure = max(new_exposure, self.fl_auto_exposure_min)
                self.last_fl_ae_action = 'decrease'
                
                # Bounds check: Give up if we hit min and still saturated
                if new_exposure == current_exposure and current_exposure <= self.fl_auto_exposure_min:
                    self._disable_fl_auto_exposure()
                    return

            # Rule 3: Stop if last action was decrease and no saturated pixels
            elif self.last_fl_ae_action == 'decrease' and not has_saturated_pixels:
                self._disable_fl_auto_exposure()
                return

            # Rule 4: Stop if last action was increase, mode is above target, and no saturated pixels
            elif self.last_fl_ae_action == 'increase' and mode_value >= self.fl_auto_exposure_target_value and not has_saturated_pixels:
                self._disable_fl_auto_exposure()
                return
            
            # Update exposure if it changed
            if abs(new_exposure - current_exposure) > 0.1:
                # Update exposure via the existing handler (which will update camera)
                self.on_fluorescent_exposure_changed(new_exposure)
                
        except Exception as e:
            print(f"Error updating FL auto exposure: {e}")
            
    def _disable_fl_auto_exposure(self):
        """Helper to disable FL auto exposure and cleanup UI."""
        self.fl_auto_exposure_enabled = False
        if hasattr(self, 'fl_auto_exposure_checkbox'):
            self.fl_auto_exposure_checkbox.blockSignals(True)
            self.fl_auto_exposure_checkbox.setChecked(False)
            self.fl_auto_exposure_checkbox.blockSignals(False)
        if hasattr(self, 'fl_auto_exposure_timer') and self.fl_auto_exposure_timer.isActive():
            self.fl_auto_exposure_timer.stop()
    
    def _calculate_mode(self, roi_region, channel='brightfield', use_otsu_threshold=False):
        """Calculate mode pixel value from ROI region.
        
        Args:
            roi_region: ROI region array (numpy array)
            channel: 'brightfield' or 'fluorescent' (for bit depth lookup)
            use_otsu_threshold: If True, calculate mode of pixels above Otsu's threshold (for FL channel)
            
        Returns:
            Mode pixel value (float) or 0.0 if calculation fails
        """
        if roi_region.size == 0:
            return 0.0
        
        # Flatten ROI region to 1D array
        pixels = roi_region.flatten()
        
        # Apply Otsu's threshold if requested
        if use_otsu_threshold:
            if not CV2_AVAILABLE:
                # Fallback to non-zero pixels if OpenCV not available
                pixels = pixels[pixels > 0]
                if pixels.size == 0:
                    return 0.0
            else:
                # Calculate Otsu's threshold (needs 2D array)
                try:
                    bit_depth = self.camera_bit_depths.get(channel, 8)
                    if bit_depth > 8:
                        # Scale down to 8-bit for Otsu calculation to avoid wrap-around
                        # (OpenCV otsu typically expects 8-bit)
                        shift = bit_depth - 8
                        scaling_factor = 2 ** shift
                        roi_8bit = (roi_region >> shift).astype(np.uint8)
                        threshold_val_8bit, _ = cv2.threshold(
                            roi_8bit, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                        )
                        # Scale back up
                        threshold_val = (threshold_val_8bit << shift)
                    else:
                        threshold_val, _ = cv2.threshold(
                            roi_region.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                        )
                        
                    # Filter pixels above threshold
                    pixels = pixels[pixels > threshold_val]
                    if pixels.size == 0:
                        return 0.0
                except Exception:
                    # Fallback to non-zero pixels on error
                    pixels = pixels[pixels > 0]
                    if pixels.size == 0:
                        return 0.0
        
        # Calculate mode using numpy bincount for integer pixel values
        # For 12-bit data (0-4095), bincount will create a 4096-sized array
        counts = np.bincount(pixels.astype(np.int32))
        if counts.size == 0:
            return 0.0
        
        mode_value = np.argmax(counts)
        return float(mode_value)



    def _calculate_roi_statistics(self, image_array, roi_x, roi_y, roi_width, roi_height, channel='brightfield', use_otsu_threshold=False):
        """Calculate mode intensity, mean intensity, and saturated pixel count for a ROI region.
        
        Args:
            image_array: numpy array of the image (grayscale or color)
            roi_x: X coordinate of ROI top-left corner in image coordinates
            roi_y: Y coordinate of ROI top-left corner in image coordinates
            roi_width: Width of ROI region
            roi_height: Height of ROI region
            channel: 'brightfield' or 'fluorescent' (used for bit depth lookup)
            use_otsu_threshold: If True, calculate mode of pixels above Otsu's threshold (for FL channel)
        
        Returns:
            tuple: (mode_intensity, mean_intensity, saturated_count) or (None, None, None) if calculation fails
        """
        if image_array is None or image_array.size == 0:
            return None, None, None
        
        # Convert to grayscale if needed
        if len(image_array.shape) == 3:
            # For RGB images, convert to grayscale while preserving bit depth
            # (Avoid astype(np.uint8) which causes wrap-around for >8-bit data)
            if image_array.shape[2] == 3:
                gray = np.mean(image_array, axis=2).astype(image_array.dtype)
            elif image_array.shape[2] == 4:
                gray = np.mean(image_array[:, :, :3], axis=2).astype(image_array.dtype)
            else:
                return None, None, None
        else:
            gray = image_array.copy()
        
        # Get image dimensions
        img_height, img_width = gray.shape[:2]
        
        # Clamp ROI to image bounds
        roi_x = max(0, min(int(roi_x), img_width - 1))
        roi_y = max(0, min(int(roi_y), img_height - 1))
        roi_right = min(roi_x + int(roi_width), img_width)
        roi_bottom = min(roi_y + int(roi_height), img_height)
        
        # Extract ROI region
        roi_region = gray[roi_y:roi_bottom, roi_x:roi_right]
        
        if roi_region.size == 0:
            return None, None, None
        
        # Calculate mode intensity
        mode_intensity = self._calculate_mode(roi_region, use_otsu_threshold=use_otsu_threshold)
        
        # Calculate mean intensity
        mean_intensity = float(np.mean(roi_region))
        
        # Count saturated pixels (pixels at or above camera saturation point)
        bit_depth = self.camera_bit_depths.get(channel, 8)
        saturation_threshold = (2 ** bit_depth) - 1
        saturated_count = int(np.sum(roi_region >= saturation_threshold))
        
        return mode_intensity, mean_intensity, saturated_count
    
    def toggle_red_led(self, checked):
        """Toggle red LED on/off."""
        self.red_led_state = checked
        success = self._set_led_state(self.red_led_address, checked, self.red_led_on_state)
        
        if success:
            if checked:
                self.red_led_button.setText("Red\nOn")
            else:
                self.red_led_button.setText("Red\nOff")
        else:
            # Revert button state on error
            self.red_led_button.setChecked(not checked)
            self.red_led_state = not checked
    
    def toggle_blue_led(self, checked):
        """Toggle blue LED on/off."""
        self.blue_led_state = checked
        success = self._set_led_state(self.blue_led_address, checked, self.blue_led_on_state)
        
        if success:
            if checked:
                self.blue_led_button.setText("Blue\nOn")
            else:
                self.blue_led_button.setText("Blue\nOff")
        else:
            # Revert button state on error
            self.blue_led_button.setChecked(not checked)
            self.blue_led_state = not checked
    
    def set_camera_mode_programmatic(self, mode):
        """Set camera mode programmatically (BF only or BF+FL).
        
        Args:
            mode: "BF only" or "BF+FL"
        """
        self.camera_mode = mode
        if hasattr(self, 'camera_mode_combo'):
            if mode == "BF only":
                self.camera_mode_combo.setCurrentIndex(0)
            else:
                self.camera_mode_combo.setCurrentIndex(1)
        
        # Update UI visibility
        self._update_camera_mode_ui_visibility()
        
        # Notify other components of mode change
        self.camera_mode_changed.emit(mode)
    
    def turn_on_blue_led_programmatic(self):
        """Turn on blue LED programmatically."""
        if not self.blue_led_state:
            self.blue_led_state = True
            success = self._set_led_state(self.blue_led_address, True, self.blue_led_on_state)
            if success and hasattr(self, 'blue_led_button'):
                self.blue_led_button.setChecked(True)
                self.blue_led_button.setText("Blue\nOn")
            return success
        return True
    
    def _restart_camera_threads(self):
        """Restart camera acquisition threads."""
        # Stop all cameras first
        for camera in self.cameras:
            if camera.IsGrabbing():
                camera.StopGrabbing()
        
        # Stop all threads and disconnect signals to prevent memory leaks
        for i, camera in enumerate(self.cameras):
            if i < len(self.camera_threads):
                old_thread = self.camera_threads[i]
                # Disconnect signal to prevent multiple connections
                try:
                    old_thread.imageAcquired.disconnect()
                except (TypeError, RuntimeError):
                    # Signal might not be connected or thread already deleted
                    pass
                # Stop old thread if running
                if old_thread.isRunning():
                    old_thread.stop()
                    # Do NOT wait() here, it blocks the GUI event loop and causes stutters!
        
        # Note: Synchronization is handled by the caller (toggle_roi, on_roi_width_changed, etc.)
        # before calling _restart_camera_threads() to ensure proper sequencing
        
        # Start ALL cameras grabbing simultaneously (critical for synchronization)
        for camera in self.cameras:
            camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        
        # Small delay to ensure all cameras have started grabbing
        time.sleep(0.01)  # 10ms should be enough
        
        # Now start threads (they'll just retrieve results from already-grabbing cameras)
        for i, camera in enumerate(self.cameras):
            # Create and start new thread
            thread = CameraThread(camera, i, self)
            thread.imageAcquired.connect(self.on_image_acquired)
            thread.start()
            if i < len(self.camera_threads):
                self.camera_threads[i] = thread
            else:
                self.camera_threads.append(thread)
    
    def on_edge_threshold_changed(self, value):
        """Handle edge threshold change."""
        self.edge_threshold = float(value)
    
    def on_min_brightness_diff_changed(self, value):
        """Handle minimum brightness difference change."""
        self.min_brightness_diff = float(value)
    
    def on_use_line_detection_changed(self, index):
        """Handle line detection mode change."""
        self.use_line_detection = (index == 1)
    
    def on_vertical_line_threshold_changed(self, value):
        """Handle vertical line threshold change."""
        if value == 0.0:
            self.vertical_line_threshold = None  # Auto mode
        else:
            self.vertical_line_threshold = float(value)
    
    def on_corner_search_width_changed(self, value):
        """Handle corner search width change."""
        self.corner_search_width = int(value)
    
    def on_center_exclusion_percent_changed(self, value):
        """Handle center exclusion percent change."""
        self.center_exclusion_percent = float(value)
    
    def on_report_roi_heuristics_changed(self, checked):
        """Handle report ROI heuristics checkbox change."""
        # Explicitly convert to boolean to ensure correct type
        self.report_roi_heuristics = bool(checked)
        # Ensure checkbox state matches the value
        if hasattr(self, 'report_roi_heuristics_check'):
            self.report_roi_heuristics_check.blockSignals(True)
            self.report_roi_heuristics_check.setChecked(self.report_roi_heuristics)
            self.report_roi_heuristics_check.blockSignals(False)
    
    def _roi_print(self, *args, **kwargs):
        """Conditionally print ROI detection messages based on report_roi_heuristics setting."""
        # Check checkbox state directly as source of truth (if it exists)
        if hasattr(self, 'report_roi_heuristics_check'):
            if self.report_roi_heuristics_check.isChecked():
                print(*args, **kwargs)
        elif hasattr(self, 'report_roi_heuristics') and self.report_roi_heuristics:
            # Fallback to variable if checkbox doesn't exist yet
            print(*args, **kwargs)
    
    def _on_tab_changed(self, index):
        """Handle tab change event."""
        # Get tab name
        tab_name = self.tabs.tabText(index)
        
        # Auto-activate ROI overlay when ROI or QC tab is active
        if tab_name == "ROI" or tab_name == "QC":
            if hasattr(self, 'detect_roi_button') and self.camera_initialized:
                if not self.detect_roi_button.isChecked():
                    self.detect_roi_button.setChecked(True)
                    self.toggle_roi_detection(True)
    
    def _update_roi_button_text(self, checked):
        """Update the ROI overlay button text based on checked state."""
        if hasattr(self, 'detect_roi_button'):
            if checked:
                self.detect_roi_button.setText("ROI overlay enabled")
            else:
                self.detect_roi_button.setText("ROI overlay disabled")
    
    def _update_roi_switch_button(self, checked):
        """Update the ROI switch button text based on checked state."""
        if hasattr(self, 'roi_button'):
            if checked:
                self.roi_button.setText("Switch to Full Image")
            else:
                self.roi_button.setText("Switch to ROI")
    
    def toggle_roi_detection(self, checked):
        """Toggle ROI detection on/off."""
        self.roi_detection_enabled = checked
        if checked:
            # Start detection timer
            self.roi_detection_timer.start()
            # Enable detected ROI display on both displays
            if hasattr(self, 'camera1_display'):
                self.camera1_display.show_detected_roi = True
            if hasattr(self, 'camera2_display'):
                self.camera2_display.show_detected_roi = True
        else:
            # Stop detection timer
            self.roi_detection_timer.stop()
            # Disable detected ROI display on both displays
            if hasattr(self, 'camera1_display'):
                self.camera1_display.show_detected_roi = False
            if hasattr(self, 'camera2_display'):
                self.camera2_display.show_detected_roi = False
    

    def _run_ui_callback(self, obj, method_name, args, kwargs):
        try:
            getattr(obj, method_name)(*args, **kwargs)
        except Exception as e:
            print(f"UI Callback Error: {e}")

    def _safe_ui_call(self, obj, method_name, *args, **kwargs):
        self.ui_callback_signal.emit(obj, method_name, args, kwargs)

    def _perform_roi_detection(self):
        """Perform ROI detection separately on brightfield and fluorescent camera images."""
        if not self.roi_detection_enabled or not self.camera_initialized:
            return
        
        # Only perform detection if enough time has passed (throttling handled by timer, but double-check)
        current_time = time.time() * 1000  # Convert to milliseconds
        if current_time - self.last_detection_time < 100:
            return
        self.last_detection_time = current_time
        
        # Perform detection for brightfield channel
        if not self._roi_thread_active.get('brightfield', False) and hasattr(self, 'camera1_display') and self.camera1_display._current_image_array is not None:
            try:
                image = self.camera1_display._current_image_array
                if image is not None and image.size > 0:
                    self._roi_thread_active['brightfield'] = True
                    img_copy = image.copy()
                    bf_l_hist = list(self.bf_left_edge_history)
                    bf_r_hist = list(self.bf_right_edge_history)
                    self.roi_thread_pool.submit(
                        self._async_roi_worker,
                        img_copy, 'brightfield', self.camera1_display, bf_l_hist, bf_r_hist
                    )
            except Exception as e:
                self._roi_print(f"Error starting BF ROI detection thread: {e}")
                self._roi_thread_active['brightfield'] = False
        
        # Perform detection for fluorescent channel
        if not self._roi_thread_active.get('fluorescent', False) and hasattr(self, 'camera2_display') and self.camera2_display._current_image_array is not None:
            try:
                image = self.camera2_display._current_image_array
                if image is not None and image.size > 0:
                    self._roi_thread_active['fluorescent'] = True
                    img_copy = image.copy()
                    fl_l_hist = list(self.fl_left_edge_history)
                    fl_r_hist = list(self.fl_right_edge_history)
                    self.roi_thread_pool.submit(
                        self._async_roi_worker,
                        img_copy, 'fluorescent', self.camera2_display, fl_l_hist, fl_r_hist
                    )
            except Exception as e:
                self._roi_print(f"Error starting FL ROI detection thread: {e}")
                self._roi_thread_active['fluorescent'] = False

    def _async_roi_worker(self, image, channel, display, l_hist, r_hist):
        try:
            self._perform_roi_detection_for_channel(image, channel, display, l_hist, r_hist)
        finally:
            self._roi_thread_active[channel] = False
    
    def _perform_roi_detection_for_channel(self, image, channel, display, left_edge_history, right_edge_history):
        """Perform ROI detection for a single channel.
        
        Args:
            image: Image array for the channel
            channel: 'brightfield' or 'fluorescent'
            display: ImageDisplayLabel instance for this channel
            left_edge_history: List to store left edge history for this channel
            right_edge_history: List to store right edge history for this channel
        """
        try:
            # Save original image for focus calculation (focus should use original, not inverted)
            original_image = image.copy()
            
            # For fluorescent channel, use the new corner detection approach
            if channel == 'fluorescent':
                # Get image dimensions
                if len(image.shape) == 3:
                    img_height, img_width = image.shape[:2]
                else:
                    img_height, img_width = image.shape[:2]
                
                # Get verbose setting from checkbox state
                verbose = self.report_roi_heuristics_check.isChecked() if hasattr(self, 'report_roi_heuristics_check') else self.report_roi_heuristics
                
                # In ROI mode, operate on entire image (which is already the ROI image)
                # In full image mode, crop to overlay box region
                if self.roi_enabled:
                    # ROI mode: use entire image directly (no cropping needed)
                    detection_image = image
                    overlay_x = 0
                    overlay_y = 0
                else:
                    # Full image mode: crop to overlay box region
                    # Calculate overlay box position (centered within the image)
                    effective_overlay_width = min(self.overlay_width, img_width)
                    effective_overlay_height = min(self.overlay_height, img_height)
                    
                    # Calculate overlay rectangle position (centered within the current image)
                    overlay_x = (img_width - effective_overlay_width) // 2
                    overlay_y = (img_height - effective_overlay_height) // 2
                    
                    # Ensure overlay is within bounds (should already be, but double-check)
                    overlay_x = max(0, min(overlay_x, img_width - 1))
                    overlay_y = max(0, min(overlay_y, img_height - 1))
                    
                    # Crop image to overlay box region
                    crop_x_end = min(overlay_x + effective_overlay_width, img_width)
                    crop_y_end = min(overlay_y + effective_overlay_height, img_height)
                    detection_image = image[overlay_y:crop_y_end, overlay_x:crop_x_end]
                
                # Use new FL ROI detection function
                result = detect_fl_roi_center_mass_quadrants(
                    detection_image,
                    self.roi_width,
                    self.roi_height,
                    derivative_threshold=None,
                    min_change_ratio=0.1,
                    smoothing_window=5,
                    verbose=verbose,
                    manual_threshold=None,
                    return_debug_info=False
                )
                
                if result is not None and len(result) >= 2:
                    result_tuple, rectangle_vertices = result
                    if result_tuple is not None and rectangle_vertices is not None:
                        centroid_x, centroid_y, detected_roi_x, detected_roi_y, roi_width, roi_height, angle_deg = result_tuple
                        
                        # Adjust coordinates based on mode
                        if self.roi_enabled:
                            # ROI mode: coordinates are already relative to ROI image (0,0 origin)
                            # No transformation needed - detection was on entire ROI image
                            detected_roi_x_full = detected_roi_x
                            detected_roi_y_full = detected_roi_y
                            rectangle_vertices_full = rectangle_vertices.copy() if rectangle_vertices is not None else None
                        else:
                            # Full image mode: transform from cropped overlay region to full image
                            detected_roi_x_full = detected_roi_x + overlay_x
                            detected_roi_y_full = detected_roi_y + overlay_y
                            
                            # Adjust corner coordinates to full image coordinate system
                            if rectangle_vertices is not None and len(rectangle_vertices) == 4:
                                rectangle_vertices_full = rectangle_vertices.copy()
                                rectangle_vertices_full[:, 0] += overlay_x  # Adjust x coordinates
                                rectangle_vertices_full[:, 1] += overlay_y  # Adjust y coordinates
                            else:
                                rectangle_vertices_full = None
                        
                        # Store detected ROI coordinates
                        display.detected_roi_x = detected_roi_x_full
                        display.detected_roi_y = detected_roi_y_full
                        display.detected_roi_width = roi_width
                        display.detected_roi_height = roi_height
                        
                        # Store detected corners for overlay display
                        display.fl_detected_corners = rectangle_vertices_full
                        
                        # Calculate top and bottom lines from detected corners for angle plot
                        top_line = None
                        bottom_line = None
                        if rectangle_vertices_full is not None and len(rectangle_vertices_full) == 4:
                            # rectangle_vertices_full: [top-left, top-right, bottom-right, bottom-left]
                            top_left = rectangle_vertices_full[0]
                            top_right = rectangle_vertices_full[1]
                            bottom_left = rectangle_vertices_full[3]
                            bottom_right = rectangle_vertices_full[2]
                            
                            # Create line tuples (x1, y1, x2, y2) for top and bottom edges
                            top_line = (top_left[0], top_left[1], top_right[0], top_right[1])
                            bottom_line = (bottom_left[0], bottom_left[1], bottom_right[0], bottom_right[1])
                        
                        # Update angle plot with detected top and bottom lines for FL channel
                        if hasattr(self, 'angle_plot_widget') and top_line is not None and bottom_line is not None:
                            self._safe_ui_call(self.angle_plot_widget, 'update_angles', top_line, bottom_line, channel='fluorescent')
                        
                        # Update alignment plot with detected corners
                        if hasattr(self, 'alignment_plot_widget'):
                            bf_corners_for_plot = None
                            fl_corners_for_plot = rectangle_vertices_full
                            
                            # Get stored BF corners if available
                            if hasattr(self, 'camera1_display') and self.camera1_display.bf_detected_corners is not None:
                                bf_corners_for_plot = self.camera1_display.bf_detected_corners
                            
                            # Pass overlay offsets for coordinate transformation
                            # overlay_x/y represent the overlay position within the current image
                            self._safe_ui_call(self.alignment_plot_widget, 'update_corners', bf_corners_for_plot, fl_corners_for_plot, overlay_x, overlay_y)
                        
                        # Clear edge-based display fields (not used for FL)
                        display.detected_top_edge = None
                        display.detected_bottom_edge = None
                        display.detected_left_edge = None
                        display.detected_right_edge = None
                        display.use_line_detection_display = False
                        self._safe_ui_call(display, 'update')
                        
                        # Calculate focus value using detected ROI (in full image coordinates)
                        if not self.roi_enabled:
                            # Full image mode: use detected ROI coordinates
                            focus_value = calculate_roi_focus(
                                original_image, detected_roi_x_full, detected_roi_y_full,
                                roi_width, roi_height
                            )
                        else:
                            # ROI mode: use detected ROI coordinates
                            focus_value = calculate_roi_focus(
                                original_image, detected_roi_x_full, detected_roi_y_full,
                                roi_width, roi_height
                            )
                        
                        if focus_value is not None and hasattr(self, 'focus_plot_widget'):
                            self._safe_ui_call(self.focus_plot_widget, 'update_focus', focus_value, channel)
                
                # Return early for fluorescent channel (don't use BF detection logic)
                return
            
            # For brightfield channel, use existing edge detection
            # Invert the image to detect bright objects on dark background
            if len(image.shape) == 3:
                # Color image - invert each channel
                image = 255 - image
            else:
                # Grayscale image
                image = 255 - image
            
            # Get image dimensions (need to convert to grayscale first to get dimensions)
            if len(image.shape) == 3:
                img_height, img_width = image.shape[:2]
            else:
                img_height, img_width = image.shape[:2]
            
            # When in ROI mode, ensure overlay fits within the ROI image
            # Clamp overlay dimensions to image dimensions
            effective_overlay_width = min(self.overlay_width, img_width)
            effective_overlay_height = min(self.overlay_height, img_height)
            
            # Calculate overlay rectangle position (centered within the current image)
            # In ROI mode, this will be centered within the ROI image
            overlay_x = (img_width - effective_overlay_width) // 2
            overlay_y = (img_height - effective_overlay_height) // 2
            
            # Ensure overlay is within bounds (should already be, but double-check)
            overlay_x = max(0, min(overlay_x, img_width - 1))
            overlay_y = max(0, min(overlay_y, img_height - 1))
            
            # Detect edges using line detection or axis-aligned detection
            if self.use_line_detection:
                # Use derivative-based corner detection for angle tolerance
                # Pass edge history for smoothing
                # Get verbose setting from checkbox state (source of truth)
                verbose = self.report_roi_heuristics_check.isChecked() if hasattr(self, 'report_roi_heuristics_check') else self.report_roi_heuristics
                top_line, bottom_line, left_line, right_line = detect_edges_with_lines(
                    image, overlay_x, overlay_y, effective_overlay_width, effective_overlay_height,
                    self.edge_threshold, self.min_brightness_diff, True,
                    self.vertical_line_threshold, self.corner_search_width,
                    self.center_exclusion_percent,
                    left_edge_history, right_edge_history,
                    self.roi_enabled, verbose=verbose
                )
                
                # Update edge history for smoothing (extract x position from vertical lines)
                # Lines are in overlay coordinates
                # In ROI mode, left_line is None, so skip history update
                if left_line is not None and not self.roi_enabled:
                    left_x = left_line[0]  # x1 coordinate (vertical line, x1=x2)
                    left_edge_history.append(left_x)
                    # Keep only last N detections
                    if len(left_edge_history) > self.edge_history_size:
                        left_edge_history.pop(0)
                
                if right_line is not None:
                    right_x = right_line[0]  # x1 coordinate (vertical line, x1=x2)
                    right_edge_history.append(right_x)
                    # Keep only last N detections
                    if len(right_edge_history) > self.edge_history_size:
                        right_edge_history.pop(0)
                
                # Convert lines to image coordinates and store
                def convert_line_to_image_coords(line, offset_x, offset_y):
                    if line is None:
                        return None
                    x1, y1, x2, y2 = line
                    return (offset_x + x1, offset_y + y1, offset_x + x2, offset_y + y2)
                
                # Store detected lines in image coordinates for this channel's display
                display.detected_top_edge = convert_line_to_image_coords(top_line, overlay_x, overlay_y)
                display.detected_bottom_edge = convert_line_to_image_coords(bottom_line, overlay_x, overlay_y)
                display.detected_left_edge = convert_line_to_image_coords(left_line, overlay_x, overlay_y)
                display.detected_right_edge = convert_line_to_image_coords(right_line, overlay_x, overlay_y)
                display.use_line_detection_display = True
                self._safe_ui_call(display, 'update')
                
                # Update angle plot with detected horizontal lines (top and bottom) for this channel
                # Pass lines in overlay coordinates (before conversion to image coordinates)
                # The angle calculation works the same regardless of coordinate system offset
                if hasattr(self, 'angle_plot_widget'):
                    # Debug: print line coordinates and calculated angles
                    if top_line is not None:
                        x1, y1, x2, y2 = top_line
                        if x2 != x1:
                            # Calculate angle from slope (consistent with plot calculation)
                            slope = (y2 - y1) / abs(x2 - x1)
                            angle_rad = np.arctan(slope)
                            angle_deg = np.degrees(angle_rad)
                            self._roi_print(f"{channel.capitalize()} top line: ({x1:.1f}, {y1:.1f}) -> ({x2:.1f}, {y2:.1f}), angle: {angle_deg:.3f}°")
                    if bottom_line is not None:
                        x1, y1, x2, y2 = bottom_line
                        if x2 != x1:
                            # Calculate angle from slope (consistent with plot calculation)
                            slope = (y2 - y1) / abs(x2 - x1)
                            angle_rad = np.arctan(slope)
                            angle_deg = np.degrees(angle_rad)
                            self._roi_print(f"{channel.capitalize()} bottom line: ({x1:.1f}, {y1:.1f}) -> ({x2:.1f}, {y2:.1f}), angle: {angle_deg:.3f}°")
                    self._safe_ui_call(self.angle_plot_widget, 'update_angles', top_line, bottom_line, channel)
                
                # Calculate ROI from line intersections
                # In ROI mode, left_line is None, so we need to handle it differently
                detected_roi_x, detected_roi_y = None, None
                bf_corners = None
                
                if top_line and bottom_line and right_line:
                    # Helper function to find line intersections
                    def line_intersection(line1, line2):
                        """Find intersection point of two lines."""
                        if line1 is None or line2 is None:
                            return None
                        x1, y1, x2, y2 = line1
                        x3, y3, x4, y4 = line2
                        
                        # Calculate line equations: ax + by + c = 0
                        a1 = y2 - y1
                        b1 = x1 - x2
                        c1 = x2 * y1 - x1 * y2
                        
                        a2 = y4 - y3
                        b2 = x3 - x4
                        c2 = x4 * y3 - x3 * y4
                        
                        det = a1 * b2 - a2 * b1
                        if abs(det) < 1e-10:
                            return None  # Lines are parallel
                        
                        x = (b1 * c2 - b2 * c1) / det
                        y = (a2 * c1 - a1 * c2) / det
                        return (x, y)
                    
                    # Extract corners from line intersections for BF channel
                    if channel == 'brightfield':
                        # Use actual left_line or virtual left_line for corner calculation
                        left_line_for_corners = left_line
                        if self.roi_enabled and left_line is None:
                            center_x = effective_overlay_width / 2.0
                            left_line_for_corners = (center_x, 0, center_x, effective_overlay_height)
                        
                        if left_line_for_corners is not None:
                            # Find corner intersections in overlay coordinates
                            top_left_overlay = line_intersection(top_line, left_line_for_corners)
                            top_right_overlay = line_intersection(top_line, right_line)
                            bottom_left_overlay = line_intersection(bottom_line, left_line_for_corners)
                            bottom_right_overlay = line_intersection(bottom_line, right_line)
                            
                            if all(corner is not None for corner in [top_left_overlay, top_right_overlay, bottom_left_overlay, bottom_right_overlay]):
                                # Convert to image coordinates
                                top_left_img = (overlay_x + top_left_overlay[0], overlay_y + top_left_overlay[1])
                                top_right_img = (overlay_x + top_right_overlay[0], overlay_y + top_right_overlay[1])
                                bottom_left_img = (overlay_x + bottom_left_overlay[0], overlay_y + bottom_left_overlay[1])
                                bottom_right_img = (overlay_x + bottom_right_overlay[0], overlay_y + bottom_right_overlay[1])
                                
                                # Store BF corners as numpy array
                                bf_corners = np.array([
                                    top_left_img,
                                    top_right_img,
                                    bottom_right_img,
                                    bottom_left_img
                                ], dtype=np.float64)
                                display.bf_detected_corners = bf_corners
                    
                    # In ROI mode, use center of image as left edge for ROI calculation
                    if self.roi_enabled:
                        # Use center of overlay region as left edge
                        center_x = effective_overlay_width / 2.0
                        # Create a virtual left line at center for ROI calculation
                        virtual_left_line = (center_x, 0, center_x, effective_overlay_height)
                        detected_roi_x, detected_roi_y = calculate_roi_from_lines(
                            top_line, bottom_line, virtual_left_line, right_line,
                            overlay_x, overlay_y, self.roi_width, self.roi_height,
                            img_width, img_height
                        )
                    elif left_line:
                        detected_roi_x, detected_roi_y = calculate_roi_from_lines(
                            top_line, bottom_line, left_line, right_line,
                            overlay_x, overlay_y, self.roi_width, self.roi_height,
                            img_width, img_height
                        )
                
                if detected_roi_x is not None and detected_roi_y is not None:
                    # Store ROI coordinates separately for this channel
                    display.detected_roi_x = detected_roi_x
                    display.detected_roi_y = detected_roi_y
                    display.detected_roi_width = self.roi_width
                    display.detected_roi_height = self.roi_height
                
                # Update alignment plot with detected corners
                if hasattr(self, 'alignment_plot_widget'):
                    bf_corners_for_plot = None
                    fl_corners_for_plot = None
                    
                    # Get BF corners
                    if channel == 'brightfield' and bf_corners is not None:
                        bf_corners_for_plot = bf_corners
                    elif hasattr(self, 'camera1_display') and self.camera1_display.bf_detected_corners is not None:
                        bf_corners_for_plot = self.camera1_display.bf_detected_corners
                    
                    # Get FL corners
                    if channel == 'fluorescent' and hasattr(self, 'camera2_display') and self.camera2_display.fl_detected_corners is not None:
                        fl_corners_for_plot = self.camera2_display.fl_detected_corners
                    elif hasattr(self, 'camera2_display') and self.camera2_display.fl_detected_corners is not None:
                        fl_corners_for_plot = self.camera2_display.fl_detected_corners
                    
                    # Pass overlay offsets for coordinate transformation
                    # overlay_x/y represent the overlay position within the current image
                    self._safe_ui_call(self.alignment_plot_widget, 'update_corners', bf_corners_for_plot, fl_corners_for_plot, overlay_x, overlay_y)
                
                # Calculate focus value for this channel
                # In full image mode, always use overlay region for focus calculation
                # In ROI mode, use detected ROI if available, otherwise use overlay region
                # Focus calculation should always run regardless of edge detection success
                if not self.roi_enabled:
                    # Full image mode: use overlay region for focus
                    focus_value = calculate_roi_focus(
                        original_image, overlay_x, overlay_y,
                        effective_overlay_width, effective_overlay_height
                    )
                elif detected_roi_x is not None and detected_roi_y is not None:
                    # ROI mode: use detected ROI coordinates
                    focus_value = calculate_roi_focus(
                        original_image, detected_roi_x, detected_roi_y,
                        self.roi_width, self.roi_height
                    )
                else:
                    # ROI mode but detection failed: use overlay region as fallback
                    focus_value = calculate_roi_focus(
                        original_image, overlay_x, overlay_y,
                        effective_overlay_width, effective_overlay_height
                    )
                
                if focus_value is not None and hasattr(self, 'focus_plot_widget'):
                    self._safe_ui_call(self.focus_plot_widget, 'update_focus', focus_value, channel)
            else:
                # Use axis-aligned edge detection
                # Get verbose setting from checkbox state (source of truth)
                verbose = self.report_roi_heuristics_check.isChecked() if hasattr(self, 'report_roi_heuristics_check') else self.report_roi_heuristics
                top_edge, bottom_edge, left_edge, right_edge = detect_edges(
                    image, overlay_x, overlay_y, effective_overlay_width, effective_overlay_height,
                    self.edge_threshold, self.min_brightness_diff, verbose=verbose
                )
                
                # Store detected edge positions in image coordinates for this channel's display
                # In ROI mode, don't store left edge (it's the center, not an edge to display)
                display.detected_top_edge = overlay_y + top_edge if top_edge is not None else None
                display.detected_bottom_edge = overlay_y + bottom_edge if bottom_edge is not None else None
                display.detected_left_edge = None if self.roi_enabled else (overlay_x + left_edge if left_edge is not None else None)
                display.detected_right_edge = overlay_x + right_edge if right_edge is not None else None
                display.use_line_detection_display = False
                self._safe_ui_call(display, 'update')
                
                # Calculate ROI from edges
                # In ROI mode, use center of overlay as left edge for calculation
                detected_roi_x, detected_roi_y = None, None
                if top_edge is not None and bottom_edge is not None and right_edge is not None:
                    if self.roi_enabled:
                        # Use center of overlay region as left edge
                        center_x = effective_overlay_width / 2.0
                        left_edge_for_calc = center_x
                    else:
                        left_edge_for_calc = left_edge
                    
                    if left_edge_for_calc is not None:
                        detected_roi_x, detected_roi_y = calculate_roi_from_edges(
                            top_edge, bottom_edge, left_edge_for_calc, right_edge,
                            overlay_x, overlay_y, self.roi_width, self.roi_height,
                            img_width, img_height
                        )
                
                if detected_roi_x is not None and detected_roi_y is not None:
                    # Store ROI coordinates separately for this channel
                    display.detected_roi_x = detected_roi_x
                    display.detected_roi_y = detected_roi_y
                    display.detected_roi_width = self.roi_width
                    display.detected_roi_height = self.roi_height
                
                # Extract BF corners from axis-aligned edges for alignment plot
                if channel == 'brightfield' and top_edge is not None and bottom_edge is not None and right_edge is not None:
                    left_edge_for_corners = left_edge
                    if self.roi_enabled:
                        # Use center of overlay region as left edge
                        center_x = effective_overlay_width / 2.0
                        left_edge_for_corners = center_x
                    
                    if left_edge_for_corners is not None:
                        # Calculate corners from edges (in overlay coordinates)
                        top_left_overlay = (left_edge_for_corners, top_edge)
                        top_right_overlay = (right_edge, top_edge)
                        bottom_left_overlay = (left_edge_for_corners, bottom_edge)
                        bottom_right_overlay = (right_edge, bottom_edge)
                        
                        # Convert to image coordinates
                        top_left_img = (overlay_x + top_left_overlay[0], overlay_y + top_left_overlay[1])
                        top_right_img = (overlay_x + top_right_overlay[0], overlay_y + top_right_overlay[1])
                        bottom_left_img = (overlay_x + bottom_left_overlay[0], overlay_y + bottom_left_overlay[1])
                        bottom_right_img = (overlay_x + bottom_right_overlay[0], overlay_y + bottom_right_overlay[1])
                        
                        # Store BF corners as numpy array
                        bf_corners_axis = np.array([
                            top_left_img,
                            top_right_img,
                            bottom_right_img,
                            bottom_left_img
                        ], dtype=np.float64)
                        display.bf_detected_corners = bf_corners_axis
                
                # Update alignment plot with detected corners
                if hasattr(self, 'alignment_plot_widget'):
                    # Get corners from stored displays (they're updated above)
                    bf_corners_for_plot = None
                    fl_corners_for_plot = None
                    
                    if hasattr(self, 'camera1_display') and self.camera1_display.bf_detected_corners is not None:
                        bf_corners_for_plot = self.camera1_display.bf_detected_corners
                    
                    if hasattr(self, 'camera2_display') and self.camera2_display.fl_detected_corners is not None:
                        fl_corners_for_plot = self.camera2_display.fl_detected_corners
                    
                    # Pass overlay offsets for coordinate transformation
                    # overlay_x/y represent the overlay position within the current image
                    self._safe_ui_call(self.alignment_plot_widget, 'update_corners', bf_corners_for_plot, fl_corners_for_plot, overlay_x, overlay_y)
                
                # Calculate focus value for this channel
                # In full image mode, always use overlay region for focus calculation
                # In ROI mode, use detected ROI if available, otherwise use overlay region
                # Focus calculation should always run regardless of edge detection success
                if not self.roi_enabled:
                    # Full image mode: use overlay region for focus
                    focus_value = calculate_roi_focus(
                        original_image, overlay_x, overlay_y,
                        effective_overlay_width, effective_overlay_height
                    )
                elif detected_roi_x is not None and detected_roi_y is not None:
                    # ROI mode: use detected ROI coordinates
                    focus_value = calculate_roi_focus(
                        original_image, detected_roi_x, detected_roi_y,
                        self.roi_width, self.roi_height
                    )
                else:
                    # ROI mode but detection failed: use overlay region as fallback
                    focus_value = calculate_roi_focus(
                        original_image, overlay_x, overlay_y,
                        effective_overlay_width, effective_overlay_height
                    )
                
                if focus_value is not None and hasattr(self, 'focus_plot_widget'):
                    self._safe_ui_call(self.focus_plot_widget, 'update_focus', focus_value, channel)
        except Exception as e:
            self._roi_print(f"Error in {channel} ROI detection: {e}")
    
    def on_roi_width_changed(self, value):
        """Handle ROI width change."""
        self.roi_width = value
        if self.roi_enabled and self.camera_initialized:
            # Reset frame difference state when ROI size changes
            self._reset_frame_difference_state()
            
            # Apply new ROI settings immediately
            # Stop all camera threads
            for thread in self.camera_threads:
                if thread.isRunning():
                    thread.stop()
                    thread.wait()
            
            # Set new ROI for all cameras
            for i in range(len(self.cameras)):
                self._set_camera_roi(i, self.roi_width, self.roi_height)
            
            # Synchronize cameras after ROI change
            self._synchronize_cameras()
            
            # Restart threads
            self._restart_camera_threads()
            
            # Update status
            if len(self.cameras) > 0 and 0 in self.camera_max_width:
                max_width = self.camera_max_width[0]
                max_height = self.camera_max_height[0]
                self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height} from {max_width}x{max_height})")
            else:
                self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height})")
    
    def on_roi_framerate_changed(self, value):
        """Handle ROI framerate spinbox change."""
        self.roi_framerate = float(value)
        # If ROI is currently enabled, update the running trigger task
        if self.roi_enabled and self.trigger_task is not None:
            self._update_trigger_frequency()
    
    def on_display_framerate_changed(self, value):
        """Handle display framerate spinbox change."""
        self.display_framerate = float(value)
        # Reset last display update times to allow immediate update at new rate
        # This ensures the new frame rate takes effect quickly
        self.last_display_update_time.clear()
    
    def on_roi_height_changed(self, value):
        """Handle ROI height change."""
        self.roi_height = value
        if self.roi_enabled and self.camera_initialized:
            # Reset frame difference state when ROI size changes
            self._reset_frame_difference_state()
            
            # Apply new ROI settings immediately
            # Stop all camera threads
            for thread in self.camera_threads:
                if thread.isRunning():
                    thread.stop()
                    thread.wait()
            
            # Set new ROI for all cameras
            for i in range(len(self.cameras)):
                self._set_camera_roi(i, self.roi_width, self.roi_height)
            
            # Synchronize cameras after ROI change
            self._synchronize_cameras()
            
            # Restart threads
            self._restart_camera_threads()
            
            # Update status
            if len(self.cameras) > 0 and 0 in self.camera_max_width:
                max_width = self.camera_max_width[0]
                max_height = self.camera_max_height[0]
                self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height} from {max_width}x{max_height})")
            else:
                self.status_label.setText(f"Status: ROI mode ({self.roi_width}x{self.roi_height})")
    
    def _parse_toml_config(self, content):
        """Parse a simple TOML-style config file into a structured dict."""
        config = {}
        current_section = None
        
        for line in content.split('\n'):
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
            
            # Section header: [settings] or [roi]
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
                
                # Try to convert to boolean
                if value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                else:
                    # Try to convert to number (int or float)
                    try:
                        # Try integer first
                        value = int(value)
                    except ValueError:
                        try:
                            # Try float if integer fails
                            value = float(value)
                        except ValueError:
                            pass
                
                # Simple key-value
                if current_section:
                    config[current_section][key] = value
                else:
                    if 'root' not in config:
                        config['root'] = {}
                    config['root'][key] = value
        
        return config
    
    def _load_display_framerate_from_config(self):
        """Load display_framerate from config file early, before UI is created.
        
        Returns:
            float: Display framerate from config, or 10.0 as default if not found.
        """
        default_value = 10.0
        if not os.path.exists(self.config_file):
            return default_value
        
        try:
            with open(self.config_file, mode='r', encoding='utf-8') as file:
                content = file.read()
                config = self._parse_toml_config(content)
                
                if 'settings' in config:
                    settings_config = config['settings']
                    if 'display_framerate' in settings_config:
                        return float(settings_config['display_framerate'])
        except Exception:
            # If there's any error, return default
            pass
        
        return default_value
    
    def load_config(self):
        """Load configuration from camera_config.txt (READ-ONLY)."""
        if not os.path.exists(self.config_file):
            # Config file doesn't exist - use default values (config files are read-only)
            return
        
        try:
            # Read camera config (READ-ONLY - never write to this file)
            with open(self.config_file, mode='r', encoding='utf-8') as file:
                content = file.read()
                config = self._parse_toml_config(content)
                
                # Load ROI settings
                if 'roi' in config:
                    roi_config = config['roi']
                    if 'ROI_width' in roi_config:
                        self.roi_width = int(roi_config['ROI_width'])
                        if hasattr(self, 'roi_width_spin'):
                            self.roi_width_spin.setValue(self.roi_width)
                    if 'ROI_height' in roi_config:
                        self.roi_height = int(roi_config['ROI_height'])
                        if hasattr(self, 'roi_height_spin'):
                            self.roi_height_spin.setValue(self.roi_height)
                    if 'overlay_width' in roi_config:
                        self.overlay_width = int(roi_config['overlay_width'])
                    if 'overlay_height' in roi_config:
                        self.overlay_height = int(roi_config['overlay_height'])
                    
                # Update display labels with overlay dimensions
                    if hasattr(self, 'camera1_display'):
                        self.camera1_display.overlay_width = self.overlay_width
                        self.camera1_display.overlay_height = self.overlay_height
                    if hasattr(self, 'camera2_display'):
                        self.camera2_display.overlay_width = self.overlay_width
                        self.camera2_display.overlay_height = self.overlay_height
                
                # Load camera settings from SYSTEM config (NOT camera config anymore)
                try:
                    sys_config = load_system_config()
                    cam_settings = get_camera_settings(sys_config)
                    
                    self.camera_mode = cam_settings.get("camera_mode", "BF+FL")
                    if hasattr(self, 'camera_mode_combo'):
                        self.camera_mode_combo.setCurrentText(self.camera_mode)
                        
                    self.bf_roi_framerate = float(cam_settings.get("bf_roi_framerate", 1600))
                    self.bffl_roi_framerate = float(cam_settings.get("bffl_roi_framerate", 1250))
                    
                    # Set current roi_framerate based on mode
                    if self.camera_mode == "BF only":
                        self.roi_framerate = self.bf_roi_framerate
                    else:
                        self.roi_framerate = self.bffl_roi_framerate
                        
                    if hasattr(self, 'roi_framerate_spin'):
                        self.roi_framerate_spin.setValue(int(self.roi_framerate))
                except Exception as e:
                    print(f"Error loading camera settings from system config: {e}")
                
                # Load remaining settings from CAMERA config
                if 'settings' in config:
                    settings_config = config['settings']
                    if 'display_framerate' in settings_config:
                        self.display_framerate = float(settings_config['display_framerate'])
                        if hasattr(self, 'display_framerate_spin'):
                            self.display_framerate_spin.setValue(int(self.display_framerate))
                    if 'bf_trigger_threshold' in settings_config:
                        self.frame_difference_threshold = float(settings_config['bf_trigger_threshold'])
                        if hasattr(self, 'frame_diff_threshold_spin'):
                            self.frame_diff_threshold_spin.setValue(self.frame_difference_threshold)
                    if 'bf_trigger_offset' in settings_config:
                        self.frame_difference_offset = float(settings_config['bf_trigger_offset'])
                        if hasattr(self, 'frame_diff_offset_spin'):
                            self.frame_diff_offset_spin.setValue(self.frame_difference_offset)
                
                # Load edge detection settings
                if 'edge_detection' in config:
                    edge_config = config['edge_detection']
                    if 'edge_threshold' in edge_config:
                        self.edge_threshold = float(edge_config['edge_threshold'])
                        if hasattr(self, 'edge_threshold_spin'):
                            self.edge_threshold_spin.setValue(self.edge_threshold)
                    if 'min_brightness_diff' in edge_config:
                        self.min_brightness_diff = float(edge_config['min_brightness_diff'])
                        if hasattr(self, 'min_brightness_diff_spin'):
                            self.min_brightness_diff_spin.setValue(self.min_brightness_diff)
                    if 'use_line_detection' in edge_config:
                        self.use_line_detection = bool(edge_config['use_line_detection'])
                        if hasattr(self, 'use_line_detection_check'):
                            self.use_line_detection_check.setCurrentIndex(1 if self.use_line_detection else 0)
                    if 'vertical_line_threshold' in edge_config:
                        val = edge_config['vertical_line_threshold']
                        if val == 'None' or val is None:
                            self.vertical_line_threshold = None
                        else:
                            self.vertical_line_threshold = float(val)
                        if hasattr(self, 'vertical_line_threshold_spin'):
                            if self.vertical_line_threshold is None:
                                self.vertical_line_threshold_spin.setValue(0.0)
                            else:
                                self.vertical_line_threshold_spin.setValue(self.vertical_line_threshold)
                    if 'corner_search_width' in edge_config:
                        self.corner_search_width = int(edge_config['corner_search_width'])
                        if hasattr(self, 'corner_search_width_spin'):
                            self.corner_search_width_spin.setValue(self.corner_search_width)
                    if 'center_exclusion_percent' in edge_config:
                        self.center_exclusion_percent = float(edge_config['center_exclusion_percent'])
                        if hasattr(self, 'center_exclusion_percent_spin'):
                            self.center_exclusion_percent_spin.setValue(self.center_exclusion_percent)
                    if 'report_roi_heuristics' in edge_config:
                        self.report_roi_heuristics = bool(edge_config['report_roi_heuristics'])
                        if hasattr(self, 'report_roi_heuristics_check'):
                            self.report_roi_heuristics_check.setChecked(self.report_roi_heuristics)
                
                # Load camera settings (exposure, gain, etc. from camera_config.txt)
                if 'brightfield' in config:
                    self.camera_settings['brightfield'] = config['brightfield']
                    # Support both 'camera_name' (new) and 'device_name' (old) for backward compatibility
                    if 'camera_name' not in self.camera_settings['brightfield'] and 'device_name' in self.camera_settings['brightfield']:
                        self.camera_settings['brightfield']['camera_name'] = self.camera_settings['brightfield']['device_name']
                    # Ensure camera_serial is stored if available (from camera_config.txt, will be overridden by system_config.txt if present)
                    if 'camera_serial' in config['brightfield']:
                        self.camera_settings['brightfield']['camera_serial'] = config['brightfield']['camera_serial']
                    if hasattr(self, 'brightfield_exposure_spin'):
                        # Exposure is stored in microseconds
                        exposure = config['brightfield'].get('exposure', 10000.0)
                        self.brightfield_exposure_spin.setValue(exposure)
                        self.last_bf_exposure_us = exposure
                    # Gain defaults to 8 for BF if not set
                    gain = config['brightfield'].get('gain', 8.0)
                    self.camera_settings['brightfield']['gain'] = gain
                    if hasattr(self, 'brightfield_gain_spin'):
                        self.brightfield_gain_spin.setValue(gain)
                
                if 'fluorescent' in config:
                    self.camera_settings['fluorescent'] = config['fluorescent']
                    # Support both 'camera_name' (new) and 'device_name' (old) for backward compatibility
                    if 'camera_name' not in self.camera_settings['fluorescent'] and 'device_name' in self.camera_settings['fluorescent']:
                        self.camera_settings['fluorescent']['camera_name'] = self.camera_settings['fluorescent']['device_name']
                    # Ensure camera_serial is stored if available (from camera_config.txt, will be overridden by system_config.txt if present)
                    if 'camera_serial' in config['fluorescent']:
                        self.camera_settings['fluorescent']['camera_serial'] = config['fluorescent']['camera_serial']
                    if hasattr(self, 'fluorescent_exposure_spin'):
                        # Exposure is stored in microseconds
                        exposure = config['fluorescent'].get('exposure', 100000.0)
                        self.fluorescent_exposure_spin.setValue(exposure)
                        self.last_fl_exposure_us = exposure
                        # Update exposure indicator label
                        if hasattr(self, 'fl_auto_exposure_value_label'):
                            self.fl_auto_exposure_value_label.setText(f"{exposure:.1f} µs")
                    # Gain defaults to 0 for FL if not set
                    gain = config['fluorescent'].get('gain', 0.0)
                    self.camera_settings['fluorescent']['gain'] = gain
                    if hasattr(self, 'fluorescent_gain_spin'):
                        self.fluorescent_gain_spin.setValue(gain)
                    # Load auto exposure settings
                    if 'min_exposure' in config['fluorescent']:
                        self.fl_auto_exposure_min = float(config['fluorescent']['min_exposure'])
                    if 'max_exposure' in config['fluorescent']:
                        self.fl_auto_exposure_max = float(config['fluorescent']['max_exposure'])
                    # Support both target_value (new) and target_mean_px (old) for backward compatibility
                    if 'target_value' in config['fluorescent']:
                        self.fl_auto_exposure_target_value = float(config['fluorescent']['target_value'])
                    elif 'target_mean_px' in config['fluorescent']:
                        self.fl_auto_exposure_target_value = float(config['fluorescent']['target_mean_px'])
                
                # Note: LED control settings and flip_x are now loaded from system_config.txt, not camera_config.txt
        except Exception as e:
            print(f"Error loading config file: {e}")
        
        # Load camera names, serial numbers, and DAQ name from system_config.txt
        self._load_system_config()
    
    def _load_system_config(self):
        """Load camera names, serial numbers, flip_x, LED control, and DAQ name from system_config.txt (READ-ONLY)."""
        try:
            # Read system config (READ-ONLY - never write to this file)
            config = load_system_config(self.system_config_file)
            
            # Load camera names and serial numbers from system_config.txt
            camera_info = get_camera_info(config)
            
            if 'brightfield' in camera_info:
                if 'brightfield' not in self.camera_settings:
                    self.camera_settings['brightfield'] = {}
                if 'camera_name' in camera_info['brightfield']:
                    self.camera_settings['brightfield']['camera_name'] = camera_info['brightfield']['camera_name']
                if 'camera_serial' in camera_info['brightfield']:
                    self.camera_settings['brightfield']['camera_serial'] = camera_info['brightfield']['camera_serial']
            
            if 'fluorescent' in camera_info:
                if 'fluorescent' not in self.camera_settings:
                    self.camera_settings['fluorescent'] = {}
                if 'camera_name' in camera_info['fluorescent']:
                    self.camera_settings['fluorescent']['camera_name'] = camera_info['fluorescent']['camera_name']
                if 'camera_serial' in camera_info['fluorescent']:
                    self.camera_settings['fluorescent']['camera_serial'] = camera_info['fluorescent']['camera_serial']
            
            # Load flip_x from system_config.txt for both cameras
            if 'brightfield' in config:
                brightfield_config = config['brightfield']
                if 'brightfield' not in self.camera_settings:
                    self.camera_settings['brightfield'] = {}
                if 'flip_x' in brightfield_config:
                    self.camera_settings['brightfield']['flip_x'] = bool(brightfield_config['flip_x'])
            
            if 'fluorescent' in config:
                fluorescent_config = config['fluorescent']
                if 'fluorescent' not in self.camera_settings:
                    self.camera_settings['fluorescent'] = {}
                if 'flip_x' in fluorescent_config:
                    self.camera_settings['fluorescent']['flip_x'] = bool(fluorescent_config['flip_x'])
            
            # Load LED control settings from system_config.txt
            if 'led_control' in config:
                led_config = config['led_control']
                if 'red_led_address' in led_config:
                    self.red_led_address = str(led_config['red_led_address'])
                if 'blue_led_address' in led_config:
                    self.blue_led_address = str(led_config['blue_led_address'])
                if 'red_led_on_state' in led_config:
                    self.red_led_on_state = bool(led_config['red_led_on_state'])
                if 'blue_led_on_state' in led_config:
                    self.blue_led_on_state = bool(led_config['blue_led_on_state'])
                if 'blue_current_monitor_daq_address' in led_config:
                    self.fl_driver_current_address = str(led_config['blue_current_monitor_daq_address'])
                # Backward compatibility: if old 'on_state' exists but separate ones don't, use it for both
                if 'on_state' in led_config and 'red_led_on_state' not in led_config and 'blue_led_on_state' not in led_config:
                    on_state = bool(led_config['on_state'])
                    self.red_led_on_state = on_state
                    self.blue_led_on_state = on_state
            
            # Load DAQ name, camera trigger address, and photodiode address from system_config.txt
            daq_info = get_daq_info(config)
            if 'daq_name' in daq_info:
                self.daq_name = daq_info['daq_name']
            if 'camera_trigger' in daq_info:
                self.camera_trigger_address = daq_info['camera_trigger']
            if 'photodiode' in daq_info:
                self.photodiode_address = daq_info['photodiode']
        except FileNotFoundError:
            print(f"System config file not found: {self.system_config_file}")
        except Exception as e:
            print(f"Error loading system config file: {e}")
    
    def _create_default_config(self):
        """Create a default configuration file.
        
        NOTE: This function is disabled - config files are READ-ONLY.
        If the config file doesn't exist, the application will use default values.
        """
        # Config files are read-only - do not create default config
        # If config file doesn't exist, the application will use default values
        pass
    
    def _populate_camera_dropdowns(self):
        """Populate camera selection dropdowns with available cameras."""
        try:
            tl_factory = pylon.TlFactory.GetInstance()
            devices = tl_factory.EnumerateDevices()
            
            self.available_cameras = []
            camera_display_names = []
            self.camera_serial_map = {}
            
            for i, device in enumerate(devices):
                try:
                    display_name = self._get_camera_display_name(device)
                    serial = self._get_camera_serial(device)
                    camera_display_names.append(display_name)
                    self.available_cameras.append(display_name)
                    if serial:
                        self.camera_serial_map[serial] = i
                except Exception:
                    pass
            
            # Populate brightfield dropdown
            self.brightfield_camera_combo.clear()
            self.brightfield_camera_combo.addItems(camera_display_names)
            
            # Populate fluorescent dropdown
            self.fluorescent_camera_combo.clear()
            self.fluorescent_camera_combo.addItems(camera_display_names)
            
            # Set selections from config if available
            if 'brightfield' in self.camera_settings:
                # Try to match by serial number first (most reliable)
                brightfield_serial = self.camera_settings['brightfield'].get('camera_serial', '')
                if brightfield_serial and brightfield_serial in self.camera_serial_map:
                    # Find the display name for this serial
                    device_idx = self.camera_serial_map[brightfield_serial]
                    if device_idx < len(devices):
                        display_name = self._get_camera_display_name(devices[device_idx])
                        index = self.brightfield_camera_combo.findText(display_name)
                        if index >= 0:
                            self.brightfield_camera_combo.setCurrentIndex(index)
                else:
                    # Fallback to camera_name/device_name for backward compatibility
                    brightfield_name = self.camera_settings['brightfield'].get('camera_name', '')
                    if not brightfield_name:
                        brightfield_name = self.camera_settings['brightfield'].get('device_name', '')
                    if brightfield_name:
                        # Try exact match first
                        index = self.brightfield_camera_combo.findText(brightfield_name)
                        if index < 0:
                            # Try partial match (model name might match)
                            for i in range(self.brightfield_camera_combo.count()):
                                item_text = self.brightfield_camera_combo.itemText(i)
                                if brightfield_name in item_text or item_text.startswith(brightfield_name):
                                    index = i
                                    break
                        if index >= 0:
                            self.brightfield_camera_combo.setCurrentIndex(index)
            
            if 'fluorescent' in self.camera_settings:
                # Try to match by serial number first (most reliable)
                fluorescent_serial = self.camera_settings['fluorescent'].get('camera_serial', '')
                if fluorescent_serial and fluorescent_serial in self.camera_serial_map:
                    # Find the display name for this serial
                    device_idx = self.camera_serial_map[fluorescent_serial]
                    if device_idx < len(devices):
                        display_name = self._get_camera_display_name(devices[device_idx])
                        index = self.fluorescent_camera_combo.findText(display_name)
                        if index >= 0:
                            self.fluorescent_camera_combo.setCurrentIndex(index)
                else:
                    # Fallback to camera_name/device_name for backward compatibility
                    fluorescent_name = self.camera_settings['fluorescent'].get('camera_name', '')
                    if not fluorescent_name:
                        fluorescent_name = self.camera_settings['fluorescent'].get('device_name', '')
                    if fluorescent_name:
                        # Try exact match first
                        index = self.fluorescent_camera_combo.findText(fluorescent_name)
                        if index < 0:
                            # Try partial match (model name might match)
                            for i in range(self.fluorescent_camera_combo.count()):
                                item_text = self.fluorescent_camera_combo.itemText(i)
                                if fluorescent_name in item_text or item_text.startswith(fluorescent_name):
                                    index = i
                                    break
                        if index >= 0:
                            self.fluorescent_camera_combo.setCurrentIndex(index)
        except Exception as e:
            print(f"Error populating camera dropdowns: {e}")
    
    def _apply_camera_settings(self, camera, channel):
        """Apply camera settings from config (exposure, bit depth)."""
        if not channel or channel not in self.camera_settings:
            return
        
        settings = self.camera_settings[channel]
        
        try:
            # Set exposure time (in microseconds)
            if 'exposure' in settings:
                exposure_us = float(settings['exposure'])
                try:
                    # Set exposure time in microseconds
                    camera.ExposureTime.SetValue(exposure_us)
                except Exception as e:
                    print(f"Warning: Could not set exposure for {channel}: {e}")
            
            # Set bit depth
            if 'bit_depth' in settings:
                bit_depth = int(settings['bit_depth'])
                self.camera_bit_depths[channel] = bit_depth
                try:
                    # Set pixel format based on bit depth
                    if bit_depth == 8:
                        # Try to set to 8-bit format
                        try:
                            camera.PixelFormat.SetValue("Mono8")
                        except:
                            try:
                                camera.PixelFormat.SetValue("RGB8")
                            except:
                                pass
                    elif bit_depth == 12:
                        try:
                            camera.PixelFormat.SetValue("Mono12")
                        except:
                            pass
                    elif bit_depth == 16:
                        try:
                            camera.PixelFormat.SetValue("Mono16")
                        except:
                            try:
                                camera.PixelFormat.SetValue("RGB16")
                            except:
                                pass
                except Exception as e:
                    print(f"Warning: Could not set bit depth for {channel}: {e}")
            
            # Set x-axis flip (ReverseX)
            if 'flip_x' in settings:
                flip_x = bool(settings['flip_x'])
                try:
                    camera.ReverseX.SetValue(flip_x)
                except Exception as e:
                    print(f"Warning: Could not set ReverseX (flip_x) for {channel}: {e}")
            
            # Set gain
            if 'gain' in settings:
                gain = float(settings['gain'])
                try:
                    # Try common gain parameter names for Basler cameras
                    if hasattr(camera, 'Gain'):
                        camera.Gain.SetValue(gain)
                    elif hasattr(camera, 'GainRaw'):
                        camera.GainRaw.SetValue(int(gain))
                    elif hasattr(camera, 'GainSelector'):
                        # Some cameras have gain selector (e.g., 'All')
                        try:
                            camera.GainSelector.SetValue('All')
                            camera.Gain.SetValue(gain)
                        except:
                            pass
                except Exception as e:
                    print(f"Warning: Could not set gain for {channel}: {e}")
        except Exception as e:
            print(f"Error applying camera settings for {channel}: {e}")
    
    def cleanup_cameras(self):
        """Stop and close all cameras, reset frame difference state."""
        # Cleanup image saving buffer
        self._cleanup_image_saving_buffer()
        # Stop threads, disconnect signals, and wait for them to finish
        for thread in self.camera_threads:
            # Disconnect signal to prevent memory leaks from signal connections
            try:
                thread.imageAcquired.disconnect()
            except (TypeError, RuntimeError):
                # Signal might not be connected or thread already deleted
                pass
            if thread.isRunning():
                thread.stop()
                thread.wait(2000)  # Wait up to 2 seconds for thread to finish
        self.camera_threads = []
        
        # Stop FL driver current reading timer
        if hasattr(self, 'fl_driver_current_update_timer'):
            self.fl_driver_current_update_timer.stop()
        
        # Stop photodiode reading timer
        if hasattr(self, 'photodiode_update_timer'):
            self.photodiode_update_timer.stop()
        
        # Stop ROI statistics update timer
        if hasattr(self, 'roi_stats_update_timer'):
            self.roi_stats_update_timer.stop()
        
        # Stop ROI detection timer
        if hasattr(self, 'roi_detection_timer'):
            self.roi_detection_timer.stop()
        
        # Stop FL auto exposure timer
        if hasattr(self, 'fl_auto_exposure_timer'):
            self.fl_auto_exposure_timer.stop()
        
        # Close cameras
        for camera in self.cameras:
            try:
                if camera.IsOpen():
                    camera.Close()
            except Exception:
                pass
        self.cameras = []
        self.camera_device_names = []
        self.camera_initialized = False
        self.roi_enabled = False
        # Stop camera trigger task
        self._stop_camera_trigger()
        if hasattr(self, 'roi_button'):
            self.roi_button.setEnabled(False)
            self.roi_button.setChecked(False)
            self.roi_button.setText("Switch to ROI")
        # Disable detect ROI button
        if hasattr(self, 'detect_roi_button'):
            self.detect_roi_button.setEnabled(False)
            self.detect_roi_button.setChecked(False)
        
        # Reset FL driver current display
        if hasattr(self, 'fl_driver_current_value_label'):
            self.fl_driver_current_value_label.setText("N/A")
        
        # Reset ROI statistics displays
        if hasattr(self, 'bf_intensity_value'):
            self.bf_intensity_value.setText("N/A")
        if hasattr(self, 'bf_saturated_value'):
            self.bf_saturated_value.setText("N/A")
        if hasattr(self, 'fl_intensity_value'):
            self.fl_intensity_value.setText("N/A")
        if hasattr(self, 'fl_saturated_value'):
            self.fl_saturated_value.setText("N/A")
        
        # Re-enable camera mode combo when cameras are cleaned up
        if hasattr(self, 'camera_mode_combo'):
            self.camera_mode_combo.setEnabled(True)
        
        self.full_image_size = None
        
        # Reset frame difference state (this also clears triggered metadata)
        self._reset_frame_difference_state()
        
        # Turn off LEDs
        if hasattr(self, 'red_led_button') and self.red_led_state:
            self.red_led_button.setChecked(False)
            self.toggle_red_led(False)
        if hasattr(self, 'blue_led_button') and self.blue_led_state:
            self.blue_led_button.setChecked(False)
            self.toggle_blue_led(False)
        
        # Reset initialization button
        if hasattr(self, 'init_button'):
            self.init_button.setText("Initialize Cameras")
            self.init_button.setStyleSheet("""
                QPushButton {
                    background-color: #28a745;
                    color: white;
                    border-radius: 5px;
                    padding: 8px 20px;
                    font-size: 12pt;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #218838;
                }
                QPushButton:disabled {
                    background-color: #d3d3d3;
                    color: #808080;
                }
            """)
    
    def on_brightfield_camera_changed(self, camera_display_name):
        """Handle brightfield camera selection change."""
        if 'brightfield' not in self.camera_settings:
            self.camera_settings['brightfield'] = {}
        self.camera_settings['brightfield']['camera_name'] = camera_display_name
        
        # Extract and store serial number for reliable identification
        if camera_display_name and "(SN:" in camera_display_name:
            try:
                serial = camera_display_name.split("(SN:")[1].split(")")[0].strip()
                self.camera_settings['brightfield']['camera_serial'] = serial
            except Exception:
                pass
    
    def on_brightfield_exposure_changed(self, value):
        """Handle brightfield exposure change."""
        if 'brightfield' not in self.camera_settings:
            self.camera_settings['brightfield'] = {}
        self.camera_settings['brightfield']['exposure'] = value
        # Store most recent value for metadata saving
        self.last_bf_exposure_us = value
        
        # Apply to camera if initialized
        if self.camera_initialized:
            def apply_exposure():
                for i, channel in self.camera_channels.items():
                    if channel == 'brightfield' and i < len(self.cameras):
                        try:
                            self.cameras[i].ExposureTime.SetValue(value)
                        except Exception as e:
                            print(f"Warning: Could not update exposure for brightfield camera: {e}")
            
            self._update_camera_setting_safely(apply_exposure)

    
    def on_brightfield_gain_changed(self, value):
        """Handle brightfield gain change."""
        if 'brightfield' not in self.camera_settings:
            self.camera_settings['brightfield'] = {}
        self.camera_settings['brightfield']['gain'] = value
        # Apply to camera if initialized
        if self.camera_initialized:
            for i, channel in self.camera_channels.items():
                if channel == 'brightfield' and i < len(self.cameras):
                    try:
                        camera = self.cameras[i]
                        # Try common gain parameter names for Basler cameras
                        if hasattr(camera, 'Gain'):
                            camera.Gain.SetValue(value)
                        elif hasattr(camera, 'GainRaw'):
                            camera.GainRaw.SetValue(int(value))
                        elif hasattr(camera, 'GainSelector'):
                            try:
                                camera.GainSelector.SetValue('All')
                                camera.Gain.SetValue(value)
                            except:
                                pass
                    except Exception as e:
                        print(f"Warning: Could not update gain for brightfield camera: {e}")
    
    def on_fluorescent_camera_changed(self, camera_display_name):
        """Handle fluorescent camera selection change."""
        if 'fluorescent' not in self.camera_settings:
            self.camera_settings['fluorescent'] = {}
        self.camera_settings['fluorescent']['camera_name'] = camera_display_name
        
        # Extract and store serial number for reliable identification
        if camera_display_name and "(SN:" in camera_display_name:
            try:
                serial = camera_display_name.split("(SN:")[1].split(")")[0].strip()
                self.camera_settings['fluorescent']['camera_serial'] = serial
            except Exception:
                pass
    
    def on_fluorescent_exposure_changed(self, value):
        """Handle fluorescent exposure change."""
        if 'fluorescent' not in self.camera_settings:
            self.camera_settings['fluorescent'] = {}
        self.camera_settings['fluorescent']['exposure'] = value
        # Store most recent value for metadata saving
        self.last_fl_exposure_us = value
        
        # Update exposure indicator label
        if hasattr(self, 'fl_auto_exposure_value_label'):
            self.fl_auto_exposure_value_label.setText(f"{value:.1f} µs")
        
        # Update settings tab spinbox (block signals to avoid recursive updates)
        if hasattr(self, 'fluorescent_exposure_spin'):
            self.fluorescent_exposure_spin.blockSignals(True)
            self.fluorescent_exposure_spin.setValue(value)
            self.fluorescent_exposure_spin.blockSignals(False)
        
        # Apply to camera if initialized
        if self.camera_initialized:
            def apply_exposure():
                for i, channel in self.camera_channels.items():
                    if channel == 'fluorescent' and i < len(self.cameras):
                        try:
                            self.cameras[i].ExposureTime.SetValue(value)
                        except Exception as e:
                            print(f"Warning: Could not update exposure for fluorescent camera: {e}")
            
            self._update_camera_setting_safely(apply_exposure)

    
    def on_fluorescent_gain_changed(self, value):
        """Handle fluorescent gain change."""
        if 'fluorescent' not in self.camera_settings:
            self.camera_settings['fluorescent'] = {}
        self.camera_settings['fluorescent']['gain'] = value
        # Apply to camera if initialized
        if self.camera_initialized:
            for i, channel in self.camera_channels.items():
                if channel == 'fluorescent' and i < len(self.cameras):
                    try:
                        camera = self.cameras[i]
                        # Try common gain parameter names for Basler cameras
                        if hasattr(camera, 'Gain'):
                            camera.Gain.SetValue(value)
                        elif hasattr(camera, 'GainRaw'):
                            camera.GainRaw.SetValue(int(value))
                        elif hasattr(camera, 'GainSelector'):
                            try:
                                camera.GainSelector.SetValue('All')
                                camera.Gain.SetValue(value)
                            except:
                                pass
                    except Exception as e:
                        print(f"Warning: Could not update gain for fluorescent camera: {e}")
    
    def closeEvent(self, event):
        """Clean up cameras when widget is closed."""
        self.cleanup_cameras()
        event.accept()


# Standalone window wrapper for backward compatibility
class MainWindow(QMainWindow):
    """Standalone window wrapper for ImageControlWidget."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image Control")
        self.setGeometry(100, 100, 800, 600)
        self.image_control = ImageControlWidget()
        self.setCentralWidget(self.image_control)


def main():
    """Main entry point for standalone execution."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

