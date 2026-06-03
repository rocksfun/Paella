"""
Diagnostic Plot Helper Functions.

This module provides helper functions and classes for visualizing packet
timestamp delta diagnostic data.
"""

import numpy as np
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtCore import Qt
try:
    import pyqtgraph as pg
    PYQTGRAPH_AVAILABLE = True
except ImportError:
    PYQTGRAPH_AVAILABLE = False


class DiagnosticPlotWindow(QMainWindow):
    """Separate window for displaying timestamp delta diagnostic plot."""
    
    def __init__(self, parent_widget):
        """
        Initialize diagnostic plot window.
        
        Args:
            parent_widget: Parent widget that contains timestamp_deltas deque
        """
        super().__init__()
        self.parent_widget = parent_widget
        self.setWindowTitle("Timestamp Delta Diagnostic Plot")
        self.setGeometry(100, 100, 800, 600)
        
        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Title
        title = QLabel("Timestamp Delta Between Packets")
        title.setStyleSheet("font-size: 16pt; font-weight: bold; padding: 10px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Info label
        info_label = QLabel("Shows timestamp delta (seconds) between consecutive packets for the last 100 packets")
        info_label.setStyleSheet("font-size: 10pt; padding: 5px;")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)
        
        # Statistics labels (for Mean and SD display)
        stats_layout = QHBoxLayout()
        
        self.mean_label = QLabel("Mean: -- s")
        self.mean_label.setStyleSheet("font-size: 11pt; font-weight: bold; padding: 5px; color: #4CAF50;")
        self.mean_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stats_layout.addWidget(self.mean_label)
        
        self.stats_label = QLabel("Standard Deviation: -- s")
        self.stats_label.setStyleSheet("font-size: 11pt; font-weight: bold; padding: 5px; color: #f44336;")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stats_layout.addWidget(self.stats_label)
        
        layout.addLayout(stats_layout)
        
        # Packet count and data rate display
        info_layout = QHBoxLayout()
        
        self.packet_count_label = QLabel("Packet Count: --")
        self.packet_count_label.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 5px;")
        self.packet_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_layout.addWidget(self.packet_count_label)
        
        self.data_rate_label = QLabel("Data Rate: -- Hz")
        self.data_rate_label.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 5px; color: #2196F3;")
        self.data_rate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_layout.addWidget(self.data_rate_label)
        
        layout.addLayout(info_layout)
        
        # Create plot widget
        if not PYQTGRAPH_AVAILABLE:
            error_label = QLabel("PyQtGraph not available. Plot disabled.")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(error_label)
            return
        
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('left', 'Timestamp Delta', units='s')
        self.plot_widget.setLabel('bottom', 'Packet Index')
        self.plot_widget.setTitle('Timestamp Delta vs Packet Index (Last 100 Packets)')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        # Initialize empty plot data item
        self.plot_data_item = self.plot_widget.plot([], [], pen='b', symbol='o', 
                                                     symbolSize=5, symbolBrush='b', 
                                                     symbolPen=None, alpha=0.7)
        
        layout.addWidget(self.plot_widget)
        
        # Update plot with current data
        self.update_plot()
    
    def update_plot(self):
        """Update the diagnostic plot with current timestamp deltas."""
        if not PYQTGRAPH_AVAILABLE:
            return
        
        if self.parent_widget is None:
            return
        
        # Efficiently get deltas without full conversion if possible
        # The deque has maxlen=100, so it's small, but still optimize
        deltas = list(self.parent_widget.timestamp_deltas)
        
        if len(deltas) == 0:
            # No data yet
            self.plot_data_item.setData([], [])
            self.mean_label.setText("Mean: -- s")
            self.stats_label.setText("Standard Deviation: -- s")
            # Update packet count and data rate if available
            if hasattr(self.parent_widget, 'packet_count'):
                self.packet_count_label.setText(f"Packet Count: {self.parent_widget.packet_count}")
            if hasattr(self.parent_widget, 'data_rate'):
                if self.parent_widget.data_rate >= 1000000:
                    display_text = f"{self.parent_widget.data_rate/1e6:.2f} MHz"
                elif self.parent_widget.data_rate >= 1000:
                    display_text = f"{self.parent_widget.data_rate/1e3:.2f} kHz"
                else:
                    display_text = f"{self.parent_widget.data_rate:.1f} Hz"
                self.data_rate_label.setText(f"Data Rate: {display_text}")
            return
        
        # Create packet indices (0 to len(deltas)-1)
        packet_indices = list(range(len(deltas)))
        
        # Convert to numpy arrays for pyqtgraph
        indices_array = np.array(packet_indices)
        deltas_array = np.array(deltas)
        
        # Update packet count and data rate if available
        if hasattr(self.parent_widget, 'packet_count'):
            self.packet_count_label.setText(f"Packet Count: {self.parent_widget.packet_count}")
        if hasattr(self.parent_widget, 'data_rate'):
            if self.parent_widget.data_rate >= 1000000:
                display_text = f"{self.parent_widget.data_rate/1e6:.2f} MHz"
            elif self.parent_widget.data_rate >= 1000:
                display_text = f"{self.parent_widget.data_rate/1e3:.2f} kHz"
            else:
                display_text = f"{self.parent_widget.data_rate:.1f} Hz"
            self.data_rate_label.setText(f"Data Rate: {display_text}")
        
        # Calculate mean and standard deviation for last 100 frames (or available frames)
        if len(deltas_array) > 0:
            mean = float(np.mean(deltas_array))
            # Format mean display
            if mean >= 0.001:
                self.mean_label.setText(f"Mean: {mean*1000:.3f} ms")
            elif mean >= 1e-6:
                self.mean_label.setText(f"Mean: {mean*1e6:.3f} μs")
            else:
                self.mean_label.setText(f"Mean: {mean*1e9:.3f} ns")
            
            # Calculate and format standard deviation
            if len(deltas_array) > 1:
                sd = float(np.std(deltas_array))
                # Format SD display
                if sd >= 0.001:
                    self.stats_label.setText(f"Standard Deviation: {sd*1000:.3f} ms")
                elif sd >= 1e-6:
                    self.stats_label.setText(f"Standard Deviation: {sd*1e6:.3f} μs")
                else:
                    self.stats_label.setText(f"Standard Deviation: {sd*1e9:.3f} ns")
            else:
                self.stats_label.setText("Standard Deviation: -- s")
        else:
            self.mean_label.setText("Mean: -- s")
            self.stats_label.setText("Standard Deviation: -- s")
        
        # Update plot data
        self.plot_data_item.setData(indices_array, deltas_array)
        
        # Auto-scale axes
        if len(indices_array) > 0:
            self.plot_widget.setXRange(0, max(len(indices_array) - 1, 0), padding=0.02)
        if len(deltas_array) > 0:
            delta_min = np.min(deltas_array)
            delta_max = np.max(deltas_array)
            delta_range = delta_max - delta_min
            if delta_range > 0:
                self.plot_widget.setYRange(delta_min - 0.1 * delta_range, 
                                           delta_max + 0.1 * delta_range, padding=0.02)
            else:
                # If all deltas are the same, add small margin
                margin = abs(delta_min) * 0.01 if delta_min != 0 else 1e-6
                self.plot_widget.setYRange(delta_min - margin, delta_max + margin, padding=0.02)
    
    def closeEvent(self, event):
        """Clean up when window is closed."""
        if self.parent_widget is not None:
            self.parent_widget.diagnostic_window = None
        event.accept()

