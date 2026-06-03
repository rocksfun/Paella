"""
Frequency Plot Helper Functions.

This module provides helper functions and classes for visualizing frequency data
from SMR packets, including plot data preparation, update logic, and extended
frequency bounds management.
"""

import numpy as np
import time as time_module
from collections import deque
from itertools import islice
from typing import Optional, List, Tuple, Callable
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox
from PySide6.QtCore import Qt, QTimer
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False


def prepare_plot_data(packets, max_packets, start_time, data_rate):
    """
    Prepare plot data from packets for visualization.
    
    Args:
        packets: Deque of PacketData objects
        max_packets: Maximum number of packets to include
        start_time: Start time for relative time calculation
        data_rate: Current data rate (frequencies per second)
    
    Returns:
        Tuple of (time_array, freq_array, time_range, freq_range) or None if no data
    """
    if len(packets) == 0:
        return None
    
    num_packets = len(packets)
    packets_to_use = min(num_packets, max_packets)
    
    # Get the last N packets (lock-free snapshot)
    if packets_to_use < num_packets:
        start_idx = num_packets - packets_to_use
        packets_snapshot = list(islice(packets, start_idx, None))
    else:
        packets_snapshot = list(packets)
    
    # Build arrays with interpolated times for each frequency measurement
    # Packet timestamp corresponds to the time of the final (128th) frequency
    # Each frequency is measured sequentially, with time_step = 1 / data_rate between measurements
    time_list = []
    freq_list = []
    
    # Use a reasonable default if data rate not yet calculated (20kHz = 50μs per measurement)
    current_data_rate = data_rate if data_rate > 0 else 20000.0
    time_step = 1.0 / current_data_rate if current_data_rate > 0 else 5e-5  # Default: 50 microseconds
    
    for packet in packets_snapshot:
        # Packet timestamp is the time of the 128th (last) frequency
        packet_timestamp = packet.timestamp
        frequencies = packet.frequencies
        
        if frequencies and len(frequencies) > 0:
            num_frequencies = len(frequencies)
            # For each frequency, calculate its interpolated time
            # Frequency at index i: time = packet_timestamp - ((num_frequencies - 1 - i) * time_step)
            # This ensures the last frequency (index num_frequencies-1) has time = packet_timestamp
            for i, freq in enumerate(frequencies):
                # Calculate time for this frequency measurement
                # i=0 (first): time = packet_timestamp - ((num_frequencies-1) * time_step)
                # i=num_frequencies-1 (last): time = packet_timestamp - 0 = packet_timestamp
                frequency_time = packet_timestamp - ((num_frequencies - 1 - i) * time_step)
                time_elapsed = frequency_time - start_time
                time_list.append(time_elapsed)
                freq_list.append(freq)
        else:
            # Fallback: if no frequencies, use packet timestamp for all (shouldn't happen)
            time_elapsed = packet.timestamp - start_time
            time_list.append(time_elapsed)
            freq_list.append(0.0)  # Placeholder
    
    # Convert to numpy arrays
    if len(time_list) > 0 and len(freq_list) > 0:
        time_array = np.array(time_list, dtype=np.float64)
        freq_array = np.array(freq_list, dtype=np.float64)
        
        # Compute ranges
        time_min = float(np.min(time_array))
        time_max = float(np.max(time_array))
        freq_min = float(np.min(freq_array))
        freq_max = float(np.max(freq_array))
        time_range = (time_min, time_max)
        freq_range = (freq_min, freq_max)
        
        return (time_array, freq_array, time_range, freq_range)
    
    return None


def prepare_plot_data_simple(frequencies_deque, max_frequencies, start_time, data_rate):
    """
    Simplified plot data preparation - just frequencies in order, no timing interpolation.
    Much faster for large datasets. No min/max calculations - pyqtgraph handles auto-ranging.
    
    Args:
        frequencies_deque: Deque storing individual frequency values (not packets)
        max_frequencies: Maximum number of frequencies to display
        start_time: Unused (kept for compatibility)
        data_rate: Unused (kept for compatibility)
    
    Returns:
        Tuple of (time_array, freq_array) or None if no data
    """
    if len(frequencies_deque) == 0:
        return None
    
    num_frequencies = len(frequencies_deque)
    frequencies_to_use = min(num_frequencies, max_frequencies)
    
    # Get last N frequencies (lock-free snapshot)
    if frequencies_to_use < num_frequencies:
        start_idx = num_frequencies - frequencies_to_use
        # Use np.fromiter for much faster conversion from islice than list()
        freq_array = np.fromiter(islice(frequencies_deque, start_idx, None), dtype=np.float64, count=frequencies_to_use)
    else:
        freq_array = np.array(frequencies_deque, dtype=np.float64)
    
    if len(freq_array) == 0:
        return None
    
    # Create simple index-based time array (0, 1, 2, ...) - no interpolation needed
    # This is much faster and still shows the data in order
    time_array = np.arange(len(freq_array), dtype=np.float64)
    
    # Return only arrays - pyqtgraph will calculate ranges automatically
    return (time_array, freq_array)


def plot_data_preparation_loop(plot_prep_running, packets, max_packets_getter, 
                                start_time_getter, data_rate_getter, plot_data_setter):
    """
    Continuously prepare plot data in a separate thread.
    This isolates the expensive work (iterating, building arrays) from both
    the receive thread and the main/plot thread, eliminating GIL contention.
    
    Args:
        plot_prep_running: Callable that returns True if loop should continue
        packets: Deque of PacketData objects
        max_packets_getter: Callable that returns current max_packets value
        start_time_getter: Callable that returns start_time
        data_rate_getter: Callable that returns current data_rate
        plot_data_setter: Callable that sets plot_data (takes list of [time_array, freq_array, time_range, freq_range])
    """
    while plot_prep_running():
        try:
            # Sleep briefly to avoid spinning
            # Reduced to 5ms to minimize delay between updates
            time_module.sleep(0.005)  # 5ms - prepares data 200 times per second
            
            # Only prepare if we have packets
            if len(packets) == 0:
                continue
            
            # Get current max packets setting (read once, might be slightly stale)
            try:
                max_packets = max_packets_getter()
            except:
                max_packets = 100  # Fallback
            
            num_packets = len(packets)
            
            if num_packets == 0:
                continue
            
            # Get start_time and data_rate
            try:
                start_time = start_time_getter()
                data_rate = data_rate_getter()
            except:
                continue
            
            if start_time is None:
                continue
            
            # Prepare plot data
            result = prepare_plot_data(packets, max_packets, start_time, data_rate)
            
            if result is not None:
                time_array, freq_array, time_range, freq_range = result
                # Atomic swap: Python list assignment is atomic (no lock needed)
                # This eliminates lock contention that was causing periodic timestamp spikes
                plot_data_setter([time_array, freq_array, time_range, freq_range])
        except Exception:
            # If preparation fails, just continue - plot will use old data
            continue


def plot_data_preparation_loop_optimized(plot_prep_running, frequencies_deque, max_frequencies_getter, 
                                        start_time_getter, data_rate_getter, plot_data_setter):
    """
    Optimized plot data preparation loop with reduced update frequency.
    Uses simplified data structure (frequencies only, no timing interpolation).
    No min/max calculations - pyqtgraph handles auto-ranging automatically.
    
    Args:
        plot_prep_running: Callable that returns True if loop should continue
        frequencies_deque: Deque storing individual frequency values
        max_frequencies_getter: Callable that returns current max_frequencies value
        start_time_getter: Callable that returns start_time (unused, kept for compatibility)
        data_rate_getter: Callable that returns current data_rate (unused, kept for compatibility)
        plot_data_setter: Callable that sets plot_data (takes list of [time_array, freq_array])
    """
    last_update_time = time_module.time()
    update_interval = 0.016  # Update every 16ms (~60 Hz) instead of 50ms (20 Hz)
    
    while plot_prep_running():
        try:
            current_time = time_module.time()
            
            # Only update if enough time has passed
            if current_time - last_update_time < update_interval:
                time_module.sleep(0.01)  # Sleep longer when not updating
                continue
            
            last_update_time = current_time
            
            # Only prepare if we have frequencies
            if len(frequencies_deque) == 0:
                time_module.sleep(0.01)
                continue
            
            # Get current max frequencies setting
            try:
                max_frequencies = max_frequencies_getter()
            except:
                max_frequencies = 12800  # Default: 100 packets * 128 frequencies
            
            # Get start_time and data_rate (for compatibility, not used in simple mode)
            try:
                start_time = start_time_getter()
                data_rate = data_rate_getter()
            except:
                continue
            
            if start_time is None:
                continue
            
            # Prepare plot data using simplified function
            result = prepare_plot_data_simple(frequencies_deque, max_frequencies, start_time, data_rate)
            
            if result is not None:
                time_array, freq_array = result
                # Atomic swap: Python list assignment is atomic (no lock needed)
                plot_data_setter([time_array, freq_array])
        except Exception:
            # If preparation fails, just continue - plot will use old data
            continue


def update_extended_freq_bounds(frequencies, max_packets, extended_freq_window,
                                extended_freq_min, extended_freq_max, extended_freq_mean,
                                stable_freq_min, stable_freq_max, stable_freq_mean):
    """
    Update extended frequency bounds based on new packet frequencies.
    Maintains a window of 5x max_packets worth of frequency data.
    Uses actual min/max (not percentiles) to always show all data.
    
    Args:
        frequencies: List of frequency values from a packet
        max_packets: Current max_packets setting
        extended_freq_window: Deque to store extended window frequencies
        extended_freq_min: Current extended min (will be updated)
        extended_freq_max: Current extended max (will be updated)
        extended_freq_mean: Current extended mean (will be updated)
        stable_freq_min: Current stable min (will be updated)
        stable_freq_max: Current stable max (will be updated)
        stable_freq_mean: Current stable mean (will be updated)
    
    Returns:
        Tuple of updated values: (extended_freq_min, extended_freq_max, extended_freq_mean,
                                  stable_freq_min, stable_freq_max, stable_freq_mean)
    """
    if not frequencies or len(frequencies) == 0:
        return (extended_freq_min, extended_freq_max, extended_freq_mean,
                stable_freq_min, stable_freq_max, stable_freq_mean)
    
    # Extended window is 5x max_packets
    extended_window_size = max_packets * 5
    
    # Add frequencies from this packet to the extended window
    for freq in frequencies:
        extended_freq_window.append(freq)
    
    # Trim window to extended_window_size (keep last N frequencies)
    while len(extended_freq_window) > extended_window_size:
        extended_freq_window.popleft()
    
    # Recalculate statistics from current window
    if len(extended_freq_window) > 0:
        window_array = np.array(extended_freq_window)
        
        # Always use actual min/max to ensure all data is visible
        extended_freq_min = float(np.min(window_array))
        extended_freq_max = float(np.max(window_array))
        extended_freq_mean = float(np.mean(window_array))
        
        # Note: Stable bounds are now managed in update_plot_widget based on
        # the actual min/max of the last n packets (not the extended window).
        # This ensures the plot always shows all data from the last n packets,
        # with slow contraction when the range decreases.
    
    return (extended_freq_min, extended_freq_max, extended_freq_mean,
            stable_freq_min, stable_freq_max, stable_freq_mean)


def recalculate_extended_bounds(max_packets, extended_freq_window,
                                extended_freq_min, extended_freq_max, extended_freq_mean,
                                stable_freq_min, stable_freq_max, stable_freq_mean):
    """
    Recalculate extended bounds with new window size.
    Uses actual min/max to always show all data.
    
    Args:
        max_packets: New max_packets setting
        extended_freq_window: Deque storing extended window frequencies
        extended_freq_min: Current extended min (will be updated)
        extended_freq_max: Current extended max (will be updated)
        extended_freq_mean: Current extended mean (will be updated)
        stable_freq_min: Current stable min (will be updated)
        stable_freq_max: Current stable max (will be updated)
        stable_freq_mean: Current stable mean (will be updated)
    
    Returns:
        Tuple of updated values: (extended_freq_min, extended_freq_max, extended_freq_mean,
                                  stable_freq_min, stable_freq_max, stable_freq_mean)
    """
    if len(extended_freq_window) == 0:
        return (extended_freq_min, extended_freq_max, extended_freq_mean,
                stable_freq_min, stable_freq_max, stable_freq_mean)
    
    extended_window_size = max_packets * 5
    
    # Trim window to new size
    while len(extended_freq_window) > extended_window_size:
        extended_freq_window.popleft()
    
    # Recalculate statistics using actual min/max
    if len(extended_freq_window) > 0:
        window_array = np.array(extended_freq_window)
        
        # Always use actual min/max to ensure all data is visible
        extended_freq_min = float(np.min(window_array))
        extended_freq_max = float(np.max(window_array))
        extended_freq_mean = float(np.mean(window_array))
        
        # Update stable bounds if they haven't been initialized
        if stable_freq_min is None:
            stable_freq_min = extended_freq_min
            stable_freq_max = extended_freq_max
            stable_freq_mean = extended_freq_mean
    
    return (extended_freq_min, extended_freq_max, extended_freq_mean,
            stable_freq_min, stable_freq_max, stable_freq_mean)


def update_plot_widget(plot_widget, plot_data_item, plot_data, stable_freq_min, 
                       stable_freq_max, stable_freq_mean):
    """
    Update the plot widget with pre-prepared data.
    Plot data is prepared in a separate thread, so this just swaps arrays.
    Lock-free atomic read - no GIL contention with receive thread.
    Uses pyqtgraph's default auto-range for y-axis limits.
    
    Args:
        plot_widget: pyqtgraph PlotWidget instance
        plot_data_item: pyqtgraph plot data item
        plot_data: List containing [time_array, freq_array, time_range, freq_range]
        stable_freq_min: Unused, kept for backward compatibility
        stable_freq_max: Unused, kept for backward compatibility
        stable_freq_mean: Unused, kept for backward compatibility
    
    Returns:
        Tuple of (stable_freq_min, stable_freq_max, stable_freq_mean) for backward compatibility
        or None if pyqtgraph not available
    """
    if not PYQTGRAPH_AVAILABLE:
        return None
    
    try:
        # Atomic read: Python list access is atomic (no lock needed)
        plot_data_snapshot = plot_data
        time_array = plot_data_snapshot[0]
        freq_array = plot_data_snapshot[1]
        time_range = plot_data_snapshot[2]
        freq_range = plot_data_snapshot[3]
        
        if len(time_array) == 0 or len(freq_array) == 0:
            return (stable_freq_min, stable_freq_max, stable_freq_mean)
        
        # Update plot data - pyqtgraph will automatically adjust y-axis range
        # For large datasets, use setData which works efficiently for both ScatterPlotItem and PlotDataItem
        # If it's a PlotDataItem (line plot), setData is much faster than ScatterPlotItem for large datasets
        plot_data_item.setData(time_array, freq_array)
        
        # Set x-axis range manually (time axis)
        time_min, time_max = time_range
        if time_max > time_min:
            time_padding = (time_max - time_min) * 0.02
            plot_widget.setXRange(time_min - time_padding, time_max + time_padding, padding=0.02)
        
        # Y-axis range is handled automatically by pyqtgraph's auto-range
        # Return values for backward compatibility
        return (stable_freq_min, stable_freq_max, stable_freq_mean)
        
    except Exception as e:
        import sys
        print(f"Error updating plot: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return (stable_freq_min, stable_freq_max, stable_freq_mean)


def update_plot_widget_simple(plot_widget, plot_data_item, plot_data):
    """
    Simplified plot update - let pyqtgraph handle all auto-ranging.
    Much faster for large datasets. No min/max calculations needed.
    
    Args:
        plot_widget: pyqtgraph PlotWidget instance
        plot_data_item: pyqtgraph plot data item
        plot_data: List containing [time_array, freq_array]
    """
    if not PYQTGRAPH_AVAILABLE:
        return
    
    try:
        # Atomic read: Python list access is atomic (no lock needed)
        plot_data_snapshot = plot_data
        time_array = plot_data_snapshot[0]
        freq_array = plot_data_snapshot[1]
        
        if len(time_array) == 0 or len(freq_array) == 0:
            return
        
        # No manual downsampling needed - we enable built-in peak downsampling
        # in create_plot_column_widget for the PlotWidget which handles this
        # more efficiently at the rendering level.
        
        # Convert x-axis to thousands (k) for display
        time_array_k = time_array / 1000.0
        
        # Update plot data
        plot_data_item.setData(time_array_k, freq_array)
        
        # No need to manually set ranges - pyqtgraph's auto-range handles it efficiently!
        
    except Exception as e:
        import sys
        print(f"Error updating plot: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()


def create_plot_column_widget(packet_count_label_ref, data_rate_label_ref, max_packets_ref,
                              plot_widget_ref, plot_data_item_ref, on_max_packets_changed_callback,
                              show_diagnostic_plot_callback, noise_label_ref=None):
    """
    Create a plot column widget with time vs frequency plot.
    
    Args:
        packet_count_label_ref: List to store reference to packet_count_label
        data_rate_label_ref: List to store reference to data_rate_label
        max_packets_ref: List to store reference to max_packets spinbox
        plot_widget_ref: List to store reference to plot_widget
        plot_data_item_ref: List to store reference to plot_data_item
        on_max_packets_changed_callback: Callback function for max_packets value change
        show_diagnostic_plot_callback: Callback function for diagnostic plot button
    
    Returns:
        QWidget containing the plot column
    """
    plot_widget = QWidget()
    plot_layout = QVBoxLayout(plot_widget)
    plot_layout.setContentsMargins(5, 5, 5, 5)
    
    # Single horizontal layout for all controls: display N packets, and diagnostic button
    controls_layout = QHBoxLayout()
    
    # Create empty labels for backward compatibility (but don't display them)
    packet_count_label = QLabel("")
    packet_count_label.setVisible(False)
    packet_count_label_ref.append(packet_count_label)
    
    data_rate_label = QLabel("")
    data_rate_label.setVisible(False)
    data_rate_label_ref.append(data_rate_label)
    
    # Control for number of packets to display
    controls_layout.addWidget(QLabel("Display last N packets:"))
    max_packets = QSpinBox()
    max_packets.setRange(1, 1000)
    max_packets.setValue(50)  # Reduced from 100 to 50 for better performance default
    max_packets.valueChanged.connect(on_max_packets_changed_callback)
    max_packets_ref.append(max_packets)
    controls_layout.addWidget(max_packets)
    
    # Spacer
    controls_layout.addSpacing(10)
    
    # Noise indicator (RMSE of last packet)
    if noise_label_ref is not None:
        noise_label = QLabel("Noise: -- mHz")
        noise_label.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 5px; color: #FF9800;")
        noise_label.setFixedWidth(150)  # Fixed width to prevent dynamic resizing
        noise_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        noise_label_ref.append(noise_label)
        controls_layout.addWidget(noise_label)
        
        # Spacer
        controls_layout.addSpacing(10)
    
    # Diagnostic plot button
    diagnostic_button = QPushButton("Show Diagnostic Plot")
    diagnostic_button.clicked.connect(show_diagnostic_plot_callback)
    diagnostic_button.setStyleSheet("""
        QPushButton {
            background-color: #2196F3;
            color: white;
            font-size: 11pt;
            font-weight: bold;
            padding: 8px;
            border-radius: 5px;
        }
        QPushButton:hover {
            background-color: #0b7dda;
        }
        QPushButton:pressed {
            background-color: #0a6bc2;
        }
    """)
    controls_layout.addWidget(diagnostic_button)
    
    # Add stretch to push everything to the left
    controls_layout.addStretch()
    
    plot_layout.addLayout(controls_layout)
    
    # Create pyqtgraph plot widget
    if not PYQTGRAPH_AVAILABLE:
        error_label = QLabel("PyQtGraph not available. Plot disabled.")
        error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plot_layout.addWidget(error_label)
        plot_layout.addStretch()
        return plot_widget
    
    # Enable OpenGL acceleration for better performance (uses integrated GPU if available)
    # This can provide 2-5x performance improvement if OpenGL drivers are good
    try:
        pg.setConfigOption('useOpenGL', True)
        pg.setConfigOption('enableExperimental', True)
    except Exception:
        # If OpenGL is not available, continue without it
        pass
    
    # Create plot widget
    plot_widget_pg = pg.PlotWidget()
    plot_widget_pg.setLabel('left', 'Frequency', units='Hz')
    plot_widget_pg.setLabel('bottom', 'Datapoints', units='k')
    plot_widget_pg.setTitle('Frequency vs Datapoints')
    plot_widget_pg.setBackground('#202124')  # Dark gray background matching time vs mass plot
    plot_widget_pg.showGrid(x=True, y=True, alpha=0.3)
    
    # Enable auto-range on BOTH axes - pyqtgraph handles this efficiently
    view_box = plot_widget_pg.getPlotItem().getViewBox()
    view_box.enableAutoRange(axis='x', enable=True)  # Auto-range x-axis
    view_box.enableAutoRange(axis='y', enable=True)  # Auto-range y-axis
    
    # Performance optimizations for high-speed plotting
    plot_widget_pg.getPlotItem().setClipToView(True)
    # Enable auto peak downsampling - this provides optimal performance
    # while preserving the noise envelope (showing both min and max of each bin)
    plot_widget_pg.getPlotItem().setDownsampling(ds=4, auto=False, mode='peak')
    
    # Use PlotDataItem with points (scatter plot) for efficient rendering
    # PlotDataItem is more efficient than ScatterPlotItem for large datasets
    # Using points without lines provides clear visualization while maintaining performance
    plot_data_item = pg.PlotDataItem(
        [], [],
        pen=None,  # No pen = no lines (faster than setting pen to transparent)
        symbol='o',  # Circle symbol for points
        symbolSize=2,  # Small size for performance
        symbolBrush='#00FFFF',  # Cyan fill for symbols
        symbolPen=None,  # No outline for symbols (faster)
        pxMode=True,  # Ensure pixel mode is explicitly on
        antialias=False,  # Disable anti-aliasing for maximum performance
        connect=None  # No lines between points
    )
    plot_widget_pg.addItem(plot_data_item)
    
    plot_widget_ref.append(plot_widget_pg)
    plot_data_item_ref.append(plot_data_item)
    
    plot_layout.addWidget(plot_widget_pg)
    plot_layout.addStretch()
    
    return plot_widget

