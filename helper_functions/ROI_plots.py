"""
ROI Plot Widgets Helper Functions.

This module provides plot widgets for the ROI/Alignment tab, including:
- HistogramWidget: Pixel value histograms for both cameras
- AnglePlotWidget: Detected horizontal line angles over time
- FocusPlotWidget: ROI focus/sharpness values over time

All plots are optimized for performance with OpenGL acceleration.
"""

import numpy as np
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtCore import Qt, QTimer
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


def _configure_pyqtgraph_for_performance():
    """Configure PyQtGraph for optimal performance with OpenGL acceleration."""
    if not PYQTGRAPH_AVAILABLE:
        return
    
    try:
        # Enable OpenGL acceleration for better performance (uses integrated GPU if available)
        # This can provide 2-5x performance improvement if OpenGL drivers are good
        pg.setConfigOption('useOpenGL', True)
        pg.setConfigOption('enableExperimental', True)
    except Exception:
        # If OpenGL is not available, continue without it
        pass


class AnglePlotWidget(QWidget):
    """Widget for displaying detected horizontal line angles."""
    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQTGRAPH_AVAILABLE:
            self.setLayout(QVBoxLayout())
            error_label = QLabel("PyQtGraph not available. Angle plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(error_label)
            return
        
        # Configure PyQtGraph for performance
        _configure_pyqtgraph_for_performance()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create pyqtgraph plot widget with dark theme
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#202124')  # Dark gray background matching frequency plot
        self.plot_widget.setLabel('left', 'Angle', units='degrees', color='white')
        self.plot_widget.setLabel('bottom', 'Time', units='seconds', color='white')
        self.plot_widget.setTitle('Line Angle vs Time', color='white')
        
        # Set white text for axes
        self.plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Set y-axis limits (initial placeholder, auto-range is enabled below)
        self.plot_widget.setYRange(-45, 45)
        self.plot_widget.enableAutoRange(axis='y', enable=True)
        self.plot_widget.enableAutoRange(axis='x', enable=True)
        
        # Show grid
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        layout.addWidget(self.plot_widget)
        
        # Store angle history (last 10 seconds) - separate for each channel
        self.angle_history = {'brightfield': {'time': [], 'mean_angle': []}, 
                              'fluorescent': {'time': [], 'mean_angle': []}}
        self.history_duration = 10.0  # 10 seconds
        
        # Initialize plot data items (reuse instead of clearing/recreating)
        self.bf_line_item = None
        self.fl_line_item = None
        self.zero_line = None
        self.legend_added = False
    
    def update_angles(self, top_line, bottom_line, channel='brightfield'):
        """Update the plot with new detected line angles for the specified channel.
        
        Args:
            top_line: Tuple (x1, y1, x2, y2) representing top edge line, or None
            bottom_line: Tuple (x1, y1, x2, y2) representing bottom edge line, or None
            channel: 'brightfield' or 'fluorescent'
        """
        if not PYQTGRAPH_AVAILABLE:
            return
        
        import time
        current_time = time.time()
        
        # Calculate angles for horizontal lines (relative to perfectly horizontal)
        top_angle = None
        bottom_angle = None
        
        if top_line is not None:
            x1, y1, x2, y2 = top_line
            if x2 != x1:
                # Calculate angle from slope (always relative to horizontal, regardless of line direction)
                # Use absolute value of x-difference to ensure consistent angle calculation
                slope = (y2 - y1) / abs(x2 - x1)
                angle_rad = np.arctan(slope)
                top_angle = np.degrees(angle_rad)
                # Angle is now in range -90 to +90 degrees
                # Positive = tilting down (in image coordinates), Negative = tilting up
        
        if bottom_line is not None:
            x1, y1, x2, y2 = bottom_line
            if x2 != x1:
                # Calculate angle from slope (always relative to horizontal, regardless of line direction)
                # Use absolute value of x-difference to ensure consistent angle calculation
                slope = (y2 - y1) / abs(x2 - x1)
                angle_rad = np.arctan(slope)
                bottom_angle = np.degrees(angle_rad)
                # Angle is now in range -90 to +90 degrees
                # Positive = tilting down (in image coordinates), Negative = tilting up
        
        # Calculate mean angle between top and bottom
        mean_angle = None
        if top_angle is not None and bottom_angle is not None:
            mean_angle = (top_angle + bottom_angle) / 2.0
        elif top_angle is not None:
            mean_angle = top_angle
        elif bottom_angle is not None:
            mean_angle = bottom_angle
        
        # Add to history for this channel
        if mean_angle is not None:
            if channel not in self.angle_history:
                self.angle_history[channel] = {'time': [], 'mean_angle': []}
            
            self.angle_history[channel]['time'].append(current_time)
            self.angle_history[channel]['mean_angle'].append(mean_angle)
            
            # Remove old data (older than history_duration)
            cutoff_time = current_time - self.history_duration
            while (len(self.angle_history[channel]['time']) > 0 and 
                   self.angle_history[channel]['time'][0] < cutoff_time):
                self.angle_history[channel]['time'].pop(0)
                self.angle_history[channel]['mean_angle'].pop(0)
        
        # Plot data for both channels
        all_angles = []
        
        # Plot brightfield channel (white)
        if 'brightfield' in self.angle_history and len(self.angle_history['brightfield']['time']) > 0:
            times = self.angle_history['brightfield']['time']
            relative_times = np.array([(current_time - t) for t in times])
            relative_times = np.array([self.history_duration - t for t in relative_times])
            
            bf_angles = np.array(self.angle_history['brightfield']['mean_angle'])
            if len(bf_angles) > 0:
                all_angles.extend(bf_angles.tolist())
                if self.bf_line_item is None:
                    self.bf_line_item = self.plot_widget.plot(
                        relative_times, bf_angles,
                        pen=pg.mkPen('w', width=2), name='Brightfield'
                    )
                else:
                    self.bf_line_item.setData(relative_times, bf_angles)
        
        # Plot fluorescent channel (green)
        if 'fluorescent' in self.angle_history and len(self.angle_history['fluorescent']['time']) > 0:
            times = self.angle_history['fluorescent']['time']
            relative_times = np.array([(current_time - t) for t in times])
            relative_times = np.array([self.history_duration - t for t in relative_times])
            
            fl_angles = np.array(self.angle_history['fluorescent']['mean_angle'])
            if len(fl_angles) > 0:
                all_angles.extend(fl_angles.tolist())
                if self.fl_line_item is None:
                    self.fl_line_item = self.plot_widget.plot(
                        relative_times, fl_angles,
                        pen=pg.mkPen('g', width=2), name='Fluorescent'
                    )
                else:
                    self.fl_line_item.setData(relative_times, fl_angles)
        
                # Auto-update y-axis range to show all data
                # Only if auto-range is enabled for Y
                view_box = self.plot_widget.getPlotItem().getViewBox()
                if view_box.autoRangeEnabled()[1]:
                    view_box.enableAutoRange(axis='y', enable=True)
        
        # Add zero line (only once)
        if self.zero_line is None:
            self.zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('gray', style=Qt.PenStyle.DashLine, width=1))
            self.plot_widget.addItem(self.zero_line)
        
        # Add legend (only once)
        if not self.legend_added:
            self.plot_widget.addLegend(offset=(10, 10), labelTextColor='white')
            self.legend_added = True
        
        # NOTE: Native auto-range handles X scrolling since data is pre-shifted to [0, 10]
        pass


class FocusPlotWidget(QWidget):
    """Widget for displaying ROI focus/sharpness values over time."""
    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQTGRAPH_AVAILABLE:
            self.setLayout(QVBoxLayout())
            error_label = QLabel("PyQtGraph not available. Focus plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(error_label)
            return
        
        # Configure PyQtGraph for performance
        _configure_pyqtgraph_for_performance()
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create brightfield plot widget
        self.bf_plot_widget = pg.PlotWidget()
        self.bf_plot_widget.setBackground('#202124')  # Dark gray background matching frequency plot
        self.bf_plot_widget.setLabel('left', 'Focus (Variance of Laplacian)', color='white')
        self.bf_plot_widget.setLabel('bottom', 'Time', units='seconds', color='white')
        self.bf_plot_widget.setTitle('Focus - Brightfield', color='white')
        
        # Set white text for axes
        self.bf_plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.bf_plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.bf_plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.bf_plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Show grid
        self.bf_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        # Set initial ranges (auto-range is enabled to ensure these update immediately)
        self.bf_plot_widget.setXRange(0, 10.0)  # 10 seconds
        self.bf_plot_widget.setYRange(0, 100)  
        self.bf_plot_widget.enableAutoRange(axis='x', enable=True)
        self.bf_plot_widget.enableAutoRange(axis='y', enable=True)
        
        # Create fluorescent plot widget
        self.fl_plot_widget = pg.PlotWidget()
        self.fl_plot_widget.setBackground('#202124')  # Dark gray background matching frequency plot
        self.fl_plot_widget.setLabel('left', 'Focus (Variance of Laplacian)', color='white')
        self.fl_plot_widget.setLabel('bottom', 'Time', units='seconds', color='white')
        self.fl_plot_widget.setTitle('Focus - Fluorescent', color='white')
        
        # Set white text for axes
        self.fl_plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.fl_plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.fl_plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.fl_plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Show grid
        self.fl_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        # Set initial ranges (auto-range is enabled to ensure these update immediately)
        self.fl_plot_widget.setXRange(0, 10.0)  # 10 seconds
        self.fl_plot_widget.setYRange(0, 100)
        self.fl_plot_widget.enableAutoRange(axis='x', enable=True)
        self.fl_plot_widget.enableAutoRange(axis='y', enable=True)
        
        # Link x-axes for synchronized time scrolling
        self.fl_plot_widget.setXLink(self.bf_plot_widget)
        
        # Add both plots to layout
        layout.addWidget(self.bf_plot_widget, 1)
        layout.addWidget(self.fl_plot_widget, 1)
        
        # Store focus history (last 10 seconds) - separate for each channel
        self.focus_history = {'brightfield': {'values': [], 'time': []},
                              'fluorescent': {'values': [], 'time': []}}
        self.history_duration = 10.0  # 10 seconds
        self.max_focus_value = {'brightfield': 0.0, 'fluorescent': 0.0}  # Track maximum observed focus value per channel
        
        # Initialize plot data items (reuse instead of clearing/recreating)
        self.bf_focus_line_item = None
        self.fl_focus_line_item = None
        self.bf_legend_added = False
        self.fl_legend_added = False
    
    def update_focus(self, focus_value, channel='brightfield'):
        """Update the plot with a new focus value for the specified channel.
        
        Args:
            focus_value: Focus value to add
            channel: 'brightfield' or 'fluorescent'
        """
        if not PYQTGRAPH_AVAILABLE:
            return
        
        if focus_value is None:
            return
        
        import time
        current_time = time.time()
        
        # Add to history for this channel
        if channel not in self.focus_history:
            self.focus_history[channel] = {'values': [], 'time': []}
        
        self.focus_history[channel]['time'].append(current_time)
        self.focus_history[channel]['values'].append(focus_value)
        
        # Update maximum focus value for this channel
        if focus_value > self.max_focus_value[channel]:
            self.max_focus_value[channel] = focus_value
        
        # Remove old data (older than history_duration)
        cutoff_time = current_time - self.history_duration
        while (len(self.focus_history[channel]['time']) > 0 and 
               self.focus_history[channel]['time'][0] < cutoff_time):
            self.focus_history[channel]['time'].pop(0)
            self.focus_history[channel]['values'].pop(0)
        
        # Plot brightfield channel (white) on left plot
        if 'brightfield' in self.focus_history and len(self.focus_history['brightfield']['time']) > 0:
            times = self.focus_history['brightfield']['time']
            relative_times = np.array([(current_time - t) for t in times])
            relative_times = np.array([self.history_duration - t for t in relative_times])
            
            bf_focus_values = np.array(self.focus_history['brightfield']['values'])
            if len(bf_focus_values) > 0:
                if self.bf_focus_line_item is None:
                    self.bf_focus_line_item = self.bf_plot_widget.plot(
                        relative_times, bf_focus_values,
                        pen=pg.mkPen('w', width=2), name='Brightfield'
                    )
                else:
                    self.bf_focus_line_item.setData(relative_times, bf_focus_values)
                
                # Auto-update y-axis range to show all brightfield data from last 10 seconds
                # Only if auto-range is enabled for Y
                bf_view_box = self.bf_plot_widget.getPlotItem().getViewBox()
                if bf_view_box.autoRangeEnabled()[1]:
                    # Let PyQtGraph handle Auto-Range natively
                    bf_view_box.enableAutoRange(axis='y', enable=True)
                else:
                    # Manual mode (user interacted) - do not override
                    pass
        
        # Plot fluorescent channel (green) on right plot
        if 'fluorescent' in self.focus_history and len(self.focus_history['fluorescent']['time']) > 0:
            times = self.focus_history['fluorescent']['time']
            relative_times = np.array([(current_time - t) for t in times])
            relative_times = np.array([self.history_duration - t for t in relative_times])
            
            fl_focus_values = np.array(self.focus_history['fluorescent']['values'])
            if len(fl_focus_values) > 0:
                if self.fl_focus_line_item is None:
                    self.fl_focus_line_item = self.fl_plot_widget.plot(
                        relative_times, fl_focus_values,
                        pen=pg.mkPen('g', width=2)
                    )
                else:
                    self.fl_focus_line_item.setData(relative_times, fl_focus_values)
                
                # Auto-update y-axis range to show all fluorescent data from last 10 seconds
                # Only if auto-range is enabled for Y
                fl_view_box = self.fl_plot_widget.getPlotItem().getViewBox()
                if fl_view_box.autoRangeEnabled()[1]:
                    # Let PyQtGraph handle Auto-Range natively
                    fl_view_box.enableAutoRange(axis='y', enable=True)
                else:
                    # Manual mode (user interacted) - do not override
                    pass
        
        # NOTE: Removed setXRange(0, history_duration) to allow user zoom/pan to persist.
        # Since data is pre-shifted to [0, 10], maintaining view at the right-edge 
        # effectively implements "scrolling zoom".
        pass
        
        # Add legend only for brightfield plot (fluorescent plot doesn't need legend)
        if not self.bf_legend_added:
            self.bf_plot_widget.addLegend(offset=(10, 10), labelTextColor='white')
            self.bf_legend_added = True


class HistogramWidget(QWidget):
    """Widget for displaying pixel value histograms for both cameras."""
    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQTGRAPH_AVAILABLE:
            self.setLayout(QVBoxLayout())
            error_label = QLabel("PyQtGraph not available. Histogram display disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(error_label)
            return
        
        # Configure PyQtGraph for performance
        _configure_pyqtgraph_for_performance()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create pyqtgraph plot widget with dark theme
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#202124')  # Dark gray background matching frequency plot
        self.plot_widget.setLabel('left', 'Frequency', color='white')
        self.plot_widget.setLabel('bottom', 'Pixel Value', color='white')
        self.plot_widget.setTitle('Pixel Value Histogram', color='white')
        
        # Set white text for axes
        self.plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Show grid
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        # Set initial ranges
        # Set initial ranges
        self.plot_widget.setXRange(0, 255)
        self.plot_widget.setYRange(0, 1)
        self.plot_widget.enableAutoRange(axis='x', enable=True)
        self.plot_widget.enableAutoRange(axis='y', enable=True)
        
        layout.addWidget(self.plot_widget)
        
        # Initialize plot data items (reuse instead of clearing/recreating)
        self.brightfield_line_item = None
        self.fluorescent_line_item = None
        self.legend_added = False
        
        # Mode value indicators (vertical lines)
        self.bf_mode_line = None
        self.fl_mode_line = None
        self.bf_mode_value = None
        self.fl_mode_value = None
        
        # Throttling: only calculate histogram every N frames per channel
        self.frame_counters = {'brightfield': 0, 'fluorescent': 0}
        self.update_interval = 15  # Calculate histogram every 15 frames (reduces memory pressure)
        
        # Timer for periodic updates (fallback)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._process_pending_updates)
        self.update_timer.setSingleShot(False)
        self.update_timer.start(250)  # Update at most every 250ms
        
        # Store only the most recent pending data (not accumulating)
        self.pending_brightfield_data = None
        self.pending_fluorescent_data = None
    
    def update_histogram(self, image_array, channel, roi_enabled=False, overlay_width=0, overlay_height=0):
        """Update histogram for the specified channel (throttled to prevent memory issues).
        
        Args:
            image_array: The image array to compute histogram from
            channel: Channel name ('brightfield' or 'fluorescent')
            roi_enabled: If True, use complete image (ROI mode). If False, use overlay region (full image mode).
            overlay_width: Width of overlay region in pixels (used when roi_enabled=False)
            overlay_height: Height of overlay region in pixels (used when roi_enabled=False)
        """
        if not PYQTGRAPH_AVAILABLE or image_array is None or image_array.size == 0:
            return
        
        if channel not in self.frame_counters:
            return
        
        # Throttle: only calculate histogram every N frames per channel to reduce memory pressure
        self.frame_counters[channel] += 1
        if self.frame_counters[channel] < self.update_interval:
            return
        
        self.frame_counters[channel] = 0
        
        # Extract the appropriate region based on mode
        region_2d = None  # Store 2D region for Otsu thresholding
        try:
            # In full image mode (roi_enabled=False), use only overlay region
            # In ROI mode (roi_enabled=True), use complete image
            if not roi_enabled and overlay_width > 0 and overlay_height > 0:
                # Extract overlay region (centered in image)
                img_height, img_width = image_array.shape[:2]
                
                # Ensure overlay dimensions don't exceed image dimensions
                effective_overlay_width = min(overlay_width, img_width)
                effective_overlay_height = min(overlay_height, img_height)
                
                # Calculate centered overlay position
                overlay_x = (img_width - effective_overlay_width) // 2
                overlay_y = (img_height - effective_overlay_height) // 2
                
                # Clamp overlay to image bounds
                overlay_x = max(0, min(overlay_x, img_width - 1))
                overlay_y = max(0, min(overlay_y, img_height - 1))
                overlay_right = min(overlay_x + effective_overlay_width, img_width)
                overlay_bottom = min(overlay_y + effective_overlay_height, img_height)
                
                # Verify we have a valid region
                if overlay_right > overlay_x and overlay_bottom > overlay_y:
                    # Extract overlay region
                    if len(image_array.shape) == 2:
                        # Grayscale
                        region = image_array[overlay_y:overlay_bottom, overlay_x:overlay_right]
                        region_2d = region
                    elif len(image_array.shape) == 3:
                        # Color
                        region = image_array[overlay_y:overlay_bottom, overlay_x:overlay_right, :]
                        region_2d = np.mean(region, axis=2).astype(np.uint8)
                    else:
                        return
                    
                    # Verify the extracted region is valid and actually a subset of the original image
                    # The region should be smaller in at least one dimension (or both)
                    if region.size == 0:
                        # Empty region, fallback to complete image
                        region = image_array
                        if len(image_array.shape) == 2:
                            region_2d = image_array
                        elif len(image_array.shape) == 3:
                            region_2d = np.mean(image_array, axis=2).astype(np.uint8)
                    elif region.shape[0] == img_height and region.shape[1] == img_width:
                        # Extracted region is same size as original - extraction didn't work, use complete image
                        region = image_array
                        if len(image_array.shape) == 2:
                            region_2d = image_array
                        elif len(image_array.shape) == 3:
                            region_2d = np.mean(image_array, axis=2).astype(np.uint8)
                    # Otherwise, use the extracted overlay region (it's smaller than the original)
                else:
                    # Invalid overlay coordinates, fallback to complete image
                    region = image_array
                    if len(image_array.shape) == 2:
                        region_2d = image_array
                    elif len(image_array.shape) == 3:
                        region_2d = np.mean(image_array, axis=2).astype(np.uint8)
            else:
                # ROI mode: use complete image
                region = image_array
                if len(image_array.shape) == 2:
                    region_2d = image_array
                elif len(image_array.shape) == 3:
                    region_2d = np.mean(image_array, axis=2).astype(np.uint8)
            
            # For large images, downsample before processing to reduce memory usage
            if region.size > 500000:
                # Calculate downsampling factor to get ~200k pixels
                downsample_factor = int(np.sqrt(region.size / 200000))
                if len(region.shape) == 2:
                    # Grayscale
                    sampled = region[::downsample_factor, ::downsample_factor]
                    pixels = sampled.ravel()
                elif len(region.shape) == 3:
                    # Color - downsample then convert to grayscale
                    sampled = region[::downsample_factor, ::downsample_factor, :]
                    pixels = np.mean(sampled, axis=2).ravel().astype(np.uint8)
                else:
                    return
            else:
                # Small image - process normally
                if len(region.shape) == 2:
                    # Grayscale
                    pixels = region.ravel()
                elif len(region.shape) == 3:
                    # Color - convert to grayscale
                    pixels = np.mean(region, axis=2).ravel().astype(np.uint8)
                else:
                    return
        except Exception:
            return
        
        # Calculate histogram (creates small arrays: 256 elements each)
        # For 8-bit images, histogram counts are integers, bins are 0-255
        hist, bins = np.histogram(pixels, bins=256, range=(0, 256))
        
        # Calculate mode value
        # For FL channel: mode of pixels above Otsu's threshold
        # For BF channel: mode of all pixels
        if channel == 'fluorescent':
            # Use Otsu's threshold for FL channel
            if CV2_AVAILABLE and region_2d is not None:
                try:
                    threshold_val, _ = cv2.threshold(
                        region_2d.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                    )
                    # Filter pixels above threshold
                    above_threshold_pixels = pixels[pixels > threshold_val]
                    if above_threshold_pixels.size > 0:
                        counts = np.bincount(above_threshold_pixels.astype(np.int32))
                        if counts.size > 0:
                            mode_value = float(np.argmax(counts))
                        else:
                            mode_value = None
                    else:
                        mode_value = None
                except Exception:
                    # Fallback to non-zero pixels on error
                    non_zero_pixels = pixels[pixels > 0]
                    if non_zero_pixels.size > 0:
                        counts = np.bincount(non_zero_pixels.astype(np.int32))
                        if counts.size > 0:
                            mode_value = float(np.argmax(counts))
                        else:
                            mode_value = None
                    else:
                        mode_value = None
            else:
                # Fallback to non-zero pixels if OpenCV not available
                non_zero_pixels = pixels[pixels > 0]
                if non_zero_pixels.size > 0:
                    counts = np.bincount(non_zero_pixels.astype(np.int32))
                    if counts.size > 0:
                        mode_value = float(np.argmax(counts))
                    else:
                        mode_value = None
                else:
                    mode_value = None
        else:
            # BF channel: mode of all pixels
            counts = np.bincount(pixels.astype(np.int32))
            if counts.size > 0:
                mode_value = float(np.argmax(counts))
            else:
                mode_value = None
        
        # Store mode value
        if channel == 'brightfield':
            self.bf_mode_value = mode_value
        elif channel == 'fluorescent':
            self.fl_mode_value = mode_value
        
        # Normalize to 0-1 for display
        # Since we're normalizing, we can use float16 (2 bytes) instead of float32 (4 bytes)
        # This saves 512 bytes per histogram (256 elements * 2 bytes saved)
        # Normalize first in float64 to avoid overflow, then convert to float16
        if hist.max() > 0:
            hist = (hist.astype(np.float64) / hist.max()).astype(np.float16)
        else:
            hist = hist.astype(np.float16)
        
        # Calculate bin centers (0.5, 1.5, 2.5, ..., 254.5)
        # These are small integers + 0.5, so float16 is sufficient
        bin_centers = ((bins[:-1] + bins[1:]) / 2.0).astype(np.float16)
        
        # Store pending data (replace old data, don't accumulate)
        # Make explicit copies to avoid keeping references to large arrays
        if channel == 'brightfield':
            self.pending_brightfield_data = (bin_centers.copy(), hist.copy())
        elif channel == 'fluorescent':
            self.pending_fluorescent_data = (bin_centers.copy(), hist.copy())
        
        # Clear local references to help GC
        del pixels, hist, bins, bin_centers
    
    def _process_pending_updates(self):
        """Process pending histogram updates (called by timer)."""
        if not PYQTGRAPH_AVAILABLE:
            return
        
        updated = False
        
        # Update brightfield if pending
        if self.pending_brightfield_data is not None:
            bin_centers, hist = self.pending_brightfield_data
            # Convert to numpy arrays if needed
            bin_centers = np.array(bin_centers)
            hist = np.array(hist)
            
            if self.brightfield_line_item is None:
                self.brightfield_line_item = self.plot_widget.plot(
                    bin_centers, hist, 
                    pen=pg.mkPen('w', width=1.5), name='Brightfield'
                )
            else:
                # Update existing line data (more efficient than clearing/recreating)
                self.brightfield_line_item.setData(bin_centers, hist)
            # Clear reference to free memory
            self.pending_brightfield_data = None
            updated = True
        
        # Update fluorescent if pending
        if self.pending_fluorescent_data is not None:
            bin_centers, hist = self.pending_fluorescent_data
            # Convert to numpy arrays if needed
            bin_centers = np.array(bin_centers)
            hist = np.array(hist)
            neon_green = '#39FF14'
            
            if self.fluorescent_line_item is None:
                self.fluorescent_line_item = self.plot_widget.plot(
                    bin_centers, hist, 
                    pen=pg.mkPen(neon_green, width=1.5), name='Fluorescent'
                )
            else:
                # Update existing line data (more efficient than clearing/recreating)
                self.fluorescent_line_item.setData(bin_centers, hist)
            # Clear reference to free memory
            self.pending_fluorescent_data = None
            updated = True
        
        if not updated:
            return
        
        # Update y-axis limits based on current data
        try:
                # Only update if user hasn't manually adjusted the Y range
                view_box = self.plot_widget.getPlotItem().getViewBox()
                if max_val > 0 and view_box.autoRangeEnabled()[1]:
                    view_box.enableAutoRange(axis='y', enable=True)
        except Exception:
            pass  # Ignore errors in axis limit calculation
        
        # Update mode indicator lines
        # BF mode line (white dashed)
        if self.bf_mode_value is not None:
            if self.bf_mode_line is None:
                self.bf_mode_line = pg.InfiniteLine(
                    pos=self.bf_mode_value,
                    angle=90,
                    pen=pg.mkPen('w', width=1.5, style=Qt.PenStyle.DashLine),
                    movable=False
                )
                self.plot_widget.addItem(self.bf_mode_line)
            else:
                self.bf_mode_line.setValue(self.bf_mode_value)
        elif self.bf_mode_line is not None:
            # Remove line if mode value is None
            self.plot_widget.removeItem(self.bf_mode_line)
            self.bf_mode_line = None
        
        # FL mode line (green dashed)
        if self.fl_mode_value is not None:
            neon_green = '#39FF14'
            if self.fl_mode_line is None:
                self.fl_mode_line = pg.InfiniteLine(
                    pos=self.fl_mode_value,
                    angle=90,
                    pen=pg.mkPen(neon_green, width=1.5, style=Qt.PenStyle.DashLine),
                    movable=False
                )
                self.plot_widget.addItem(self.fl_mode_line)
            else:
                self.fl_mode_line.setValue(self.fl_mode_value)
        elif self.fl_mode_line is not None:
            # Remove line if mode value is None
            self.plot_widget.removeItem(self.fl_mode_line)
            self.fl_mode_line = None
        
        # Update legend if both lines exist (only once)
        if self.brightfield_line_item is not None and self.fluorescent_line_item is not None:
            if not self.legend_added:
                self.plot_widget.addLegend(offset=(10, 10), labelTextColor='white')
                self.legend_added = True


class AlignmentPlotWidget(QWidget):
    """Widget for displaying detected corners and centerpoints for BF and FL channels."""
    def __init__(self, parent=None, overlay_width=900, overlay_height=160):
        super().__init__(parent)
        if not PYQTGRAPH_AVAILABLE:
            self.setLayout(QVBoxLayout())
            error_label = QLabel("PyQtGraph not available. Alignment plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(error_label)
            return
        
        # Store overlay dimensions
        self.overlay_width = overlay_width
        self.overlay_height = overlay_height
        
        # Configure PyQtGraph for performance
        _configure_pyqtgraph_for_performance()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create pyqtgraph plot widget with dark theme
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#202124')  # Dark gray background
        self.plot_widget.setLabel('left', 'Y Position', units='pixels', color='white')
        self.plot_widget.setLabel('bottom', 'X Position', units='pixels', color='white')
        self.plot_widget.setTitle('Channel Alignment - Detected Corners', color='white')
        
        # Set white text for axes
        self.plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Show grid
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        # Enable equal aspect ratio for proper alignment visualization (x = y scaling)
        self.plot_widget.setAspectLocked(True)
        
        # Set fixed plot limits to match overlay dimensions
        self.plot_widget.setXRange(0, self.overlay_width, padding=0)
        self.plot_widget.setYRange(0, self.overlay_height, padding=0)
        
        layout.addWidget(self.plot_widget)
        
        # Initialize plot data items
        self.bf_corners_item = None
        self.fl_corners_item = None
        self.bf_center_item = None
        self.fl_center_item = None
        self.legend_added = False
        
        # Store overlay offset for coordinate transformation
        self.overlay_x = 0
        self.overlay_y = 0
    
    def update_corners(self, bf_corners=None, fl_corners=None, overlay_x=0, overlay_y=0):
        """Update the plot with detected corners and centerpoints.
        
        Args:
            bf_corners: numpy array of 4 BF corners [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] or None (in full image coordinates)
            fl_corners: numpy array of 4 FL corners [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] or None (in full image coordinates)
            overlay_x: X offset of overlay region in full image (default: 0)
            overlay_y: Y offset of overlay region in full image (default: 0)
        """
        if not PYQTGRAPH_AVAILABLE:
            return
        
        # Store overlay offset for coordinate transformation
        self.overlay_x = overlay_x
        self.overlay_y = overlay_y
        
        # Clear existing items
        if self.bf_corners_item is not None:
            self.plot_widget.removeItem(self.bf_corners_item)
            self.bf_corners_item = None
        if self.fl_corners_item is not None:
            self.plot_widget.removeItem(self.fl_corners_item)
            self.fl_corners_item = None
        if self.bf_center_item is not None:
            self.plot_widget.removeItem(self.bf_center_item)
            self.bf_center_item = None
        if self.fl_center_item is not None:
            self.plot_widget.removeItem(self.fl_center_item)
            self.fl_center_item = None
        
        # Helper function to transform corners from full image coordinates to overlay-relative coordinates
        def transform_to_overlay_coords(corners):
            if corners is None or len(corners) != 4:
                return None
            transformed = corners.copy()
            transformed[:, 0] -= overlay_x  # Transform X coordinates
            transformed[:, 1] -= overlay_y  # Transform Y coordinates
            return transformed
        
        # Transform corners to overlay-relative coordinates
        bf_corners_overlay = transform_to_overlay_coords(bf_corners)
        fl_corners_overlay = transform_to_overlay_coords(fl_corners)
        
        # Plot BF corners (white dots)
        if bf_corners_overlay is not None:
            bf_x = bf_corners_overlay[:, 0]
            bf_y = bf_corners_overlay[:, 1]
            self.bf_corners_item = self.plot_widget.plot(
                bf_x, bf_y,
                pen=None, symbol='o', symbolSize=8, symbolBrush='white', symbolPen=pg.mkPen('white', width=1),
                name='BF Corners'
            )
            
            # Calculate and plot BF centerpoint (+ symbol)
            bf_center_x = np.mean(bf_x)
            bf_center_y = np.mean(bf_y)
            self.bf_center_item = self.plot_widget.plot(
                [bf_center_x], [bf_center_y],
                pen=None, symbol='+', symbolSize=15, symbolBrush='white', symbolPen=pg.mkPen('white', width=2),
                name='BF Center'
            )
        
        # Plot FL corners (green dots)
        if fl_corners_overlay is not None:
            fl_x = fl_corners_overlay[:, 0]
            fl_y = fl_corners_overlay[:, 1]
            self.fl_corners_item = self.plot_widget.plot(
                fl_x, fl_y,
                pen=None, symbol='o', symbolSize=8, symbolBrush='green', symbolPen=pg.mkPen('green', width=1),
                name='FL Corners'
            )
            
            # Calculate and plot FL centerpoint (+ symbol)
            fl_center_x = np.mean(fl_x)
            fl_center_y = np.mean(fl_y)
            self.fl_center_item = self.plot_widget.plot(
                [fl_center_x], [fl_center_y],
                pen=None, symbol='+', symbolSize=15, symbolBrush='green', symbolPen=pg.mkPen('green', width=2),
                name='FL Center'
            )
        
        # Keep fixed plot limits matching overlay dimensions (already set in __init__)
        # The plot maintains equal aspect ratio (x = y scaling) via setAspectLocked(True)
        
        # Add legend (only once)
        if not self.legend_added:
            self.plot_widget.addLegend(offset=(10, 10), labelTextColor='white')
            self.legend_added = True


class BlueLEDCurrentPlotWidget(QWidget):
    """Widget for displaying Blue LED current over time."""
    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQTGRAPH_AVAILABLE:
            self.setLayout(QVBoxLayout())
            error_label = QLabel("PyQtGraph not available. Blue LED current plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(error_label)
            return
        
        # Configure PyQtGraph for performance
        _configure_pyqtgraph_for_performance()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create pyqtgraph plot widget with dark theme
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#202124')  # Dark gray background matching frequency plot
        self.plot_widget.setLabel('left', 'Current', units='A', color='white')
        self.plot_widget.setLabel('bottom', 'Time', units='seconds', color='white')
        self.plot_widget.setTitle('Blue LED Current vs Time', color='white')
        
        # Set white text for axes
        self.plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Show grid
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        layout.addWidget(self.plot_widget)
        
        # Store current history (last 10 seconds)
        self.current_history = {'time': [], 'current': []}
        self.history_duration = 10.0  # 10 seconds
        
        # Initialize plot data item (reuse instead of clearing/recreating)
        self.line_item = None
        
        # Initialize x-axis to show last 10 seconds
        self.plot_widget.setXRange(0, self.history_duration)
    
    def update_current(self, current_value):
        """Update the plot with a new current value.
        
        Args:
            current_value: Current value in Amperes to add
        """
        if not PYQTGRAPH_AVAILABLE:
            return
        
        if current_value is None:
            return
        
        import time
        current_time = time.time()
        
        # Add to history
        self.current_history['time'].append(current_time)
        self.current_history['current'].append(current_value)
        
        # Remove old data (older than history_duration)
        cutoff_time = current_time - self.history_duration
        while (len(self.current_history['time']) > 0 and 
               self.current_history['time'][0] < cutoff_time):
            self.current_history['time'].pop(0)
            self.current_history['current'].pop(0)
        
        # Plot data
        if len(self.current_history['time']) > 0:
            times = self.current_history['time']
            relative_times = np.array([(current_time - t) for t in times])
            relative_times = np.array([self.history_duration - t for t in relative_times])
            
            currents = np.array(self.current_history['current'])
            if len(currents) > 0:
                if self.line_item is None:
                    self.line_item = self.plot_widget.plot(
                        relative_times, currents,
                        pen=pg.mkPen('cyan', width=2), name='Blue LED Current'
                    )
                else:
                    self.line_item.setData(relative_times, currents)
                
                # Auto-update y-axis bounds based on observed data
                # Only if auto-range is enabled for Y
                view_box = self.plot_widget.getPlotItem().getViewBox()
                if view_box.autoRangeEnabled()[1]:
                    view_box.enableAutoRange(axis='y', enable=True)
        
        # NOTE: Native auto-range handles X scrolling
        pass


class PhotodiodePlotWidget(QWidget):
    """Widget for displaying photodiode voltage over time."""
    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQTGRAPH_AVAILABLE:
            self.setLayout(QVBoxLayout())
            error_label = QLabel("PyQtGraph not available. Photodiode plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(error_label)
            return
        
        # Configure PyQtGraph for performance
        _configure_pyqtgraph_for_performance()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create pyqtgraph plot widget with dark theme
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#202124')  # Dark gray background matching frequency plot
        self.plot_widget.setLabel('left', 'Voltage', units='V', color='white')
        self.plot_widget.setLabel('bottom', 'Time', units='seconds', color='white')
        self.plot_widget.setTitle('Photodiode vs Time', color='white')
        
        # Set white text for axes
        self.plot_widget.getAxis('left').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('white'))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen('white'))
        
        # Show grid
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        layout.addWidget(self.plot_widget)
        
        # Store voltage history (last 10 seconds)
        self.voltage_history = {'time': [], 'voltage': []}
        self.history_duration = 10.0  # 10 seconds
        
        # Initialize plot data item (reuse instead of clearing/recreating)
        self.line_item = None
        
        # Initialize x-axis to show last 10 seconds
        self.plot_widget.setXRange(0, self.history_duration)
        self.plot_widget.enableAutoRange(axis='x', enable=True)
        self.plot_widget.enableAutoRange(axis='y', enable=True)
    
    def update_voltage(self, voltage_value):
        """Update the plot with a new voltage value.
        
        Args:
            voltage_value: Voltage value in Volts to add
        """
        if not PYQTGRAPH_AVAILABLE:
            return
        
        if voltage_value is None:
            return
        
        import time
        current_time = time.time()
        
        # Add to history
        self.voltage_history['time'].append(current_time)
        self.voltage_history['voltage'].append(voltage_value)
        
        # Remove old data (older than history_duration)
        cutoff_time = current_time - self.history_duration
        while (len(self.voltage_history['time']) > 0 and 
               self.voltage_history['time'][0] < cutoff_time):
            self.voltage_history['time'].pop(0)
            self.voltage_history['voltage'].pop(0)
        
        # Plot data
        if len(self.voltage_history['time']) > 0:
            times = self.voltage_history['time']
            relative_times = np.array([(current_time - t) for t in times])
            relative_times = np.array([self.history_duration - t for t in relative_times])
            
            voltages = np.array(self.voltage_history['voltage'])
            if len(voltages) > 0:
                if self.line_item is None:
                    self.line_item = self.plot_widget.plot(
                        relative_times, voltages,
                        pen=pg.mkPen('yellow', width=2), name='Photodiode'
                    )
                else:
                    self.line_item.setData(relative_times, voltages)
                
                # Auto-update y-axis bounds based on observed data
                # Only if auto-range is enabled for Y
                view_box = self.plot_widget.getPlotItem().getViewBox()
                if view_box.autoRangeEnabled()[1]:
                    view_box.enableAutoRange(axis='y', enable=True)
        
        # NOTE: Native auto-range handles X scrolling
        pass

