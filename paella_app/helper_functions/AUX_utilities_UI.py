import sys
import os
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QPushButton, QLabel, 
    QSpacerItem, QSizePolicy, QGridLayout
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QIcon, QPixmap

# Add parent directory to path if running as script (for imports)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# Import the 4 utility modules
try:
    from helper_functions.AUX_frequency_binary_viewer import FrequencyBinaryViewer
    from helper_functions.DATA_batch_frequency_processor import BatchProcessorWindow
    from helper_functions.AUX_volume_images_viewer import VolumeImagesViewer
    from helper_functions.AUX_hdf5_image_viewer import HDF5ImageViewer
    from helper_functions.AUX_data_review_viewer import DataReviewViewer
except ImportError as e:
    print(f"Error importing utility modules: {e}")
    # Fallback to direct import if executed directly inside helper_functions
    from AUX_frequency_binary_viewer import FrequencyBinaryViewer
    from DATA_batch_frequency_processor import BatchProcessorWindow
    from AUX_volume_images_viewer import VolumeImagesViewer
    from AUX_hdf5_image_viewer import HDF5ImageViewer
    from AUX_data_review_viewer import DataReviewViewer

class UtilitiesUI(QMainWindow):
    """Main launcher window for Paella Utilities."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Paella Utilities")
        self.setMinimumSize(500, 400)
        self.resize(1200, 500)
        
        # Keep strong references to open windows to prevent garbage collection
        self.open_windows = []
        
        # Try to set window icon
        logo_path = os.path.join(_PARENT_DIR, 'references', 'travera_logo.ico')
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
            
        self.setup_ui()
        
    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)
        
        # Header Label
        header = QLabel("Paella Data Utilities")
        header_font = QFont("Arial", 24, QFont.Bold)
        header.setFont(header_font)
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet("color: #333333; margin-bottom: 20px;")
        layout.addWidget(header)
        
        # Grid layout for buttons
        grid = QGridLayout()
        grid.setSpacing(15)
        
        # Define the tools
        tools = [
            ("Frequency Binary Viewer", "View & analyze frequency binary files packet-by-packet.", self.launch_frequency_viewer),
            ("Batch Reprocess Frequency", "Process multiple frequency binaries using the Real-Time Alg.", self.launch_batch_processor),
            ("Binary Image Viewer", "View raw camera binary image archives (.bin).", self.launch_binary_image_viewer),
            ("HDF5 Image Viewer", "View images stored in HDF5 (.h5) archives.", self.launch_hdf5_viewer),
            ("Data Review Helper", "Scan, cache, and plot mass peak distributions across conditions.", self.launch_data_review_viewer)
        ]
        
        # Create stylish buttons
        for i, (title, desc, callback) in enumerate(tools):
            btn = QPushButton()
            btn.setMinimumHeight(100)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            
            # Inner layout for text to support custom alignment/bolding
            btn_layout = QVBoxLayout(btn)
            btn_layout.setContentsMargins(10, 15, 10, 15)
            btn_layout.setSpacing(5)
            
            lbl_title = QLabel(title)
            lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_title.setStyleSheet("font-weight: bold; font-size: 16px; color: #212529; background: transparent;")
            lbl_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            
            lbl_desc = QLabel(desc)
            lbl_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_desc.setStyleSheet("font-weight: normal; font-size: 13px; color: #555555; background: transparent;")
            lbl_desc.setWordWrap(True)
            lbl_desc.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            
            btn_layout.addStretch()
            btn_layout.addWidget(lbl_title)
            btn_layout.addWidget(lbl_desc)
            btn_layout.addStretch()
            
            # CSS styling
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #f8f9fa;
                    border: 2px solid #dee2e6;
                    border-radius: 10px;
                }
                QPushButton:hover {
                    background-color: #e9ecef;
                    border-color: #adb5bd;
                }
                QPushButton:pressed {
                    background-color: #dde2e6;
                    border-color: #6c757d;
                }
            """)
            
            btn.clicked.connect(callback)
            
            row = i // 2
            col = i % 2
            grid.addWidget(btn, row, col)
            
        layout.addLayout(grid)
        
        # Add stretch at the bottom to push everything up nicely
        layout.addSpacerItem(QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding))
        
        # Footer
        footer = QLabel("Travera Paella Toolkit")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(footer)

    def launch_frequency_viewer(self):
        try:
            window = FrequencyBinaryViewer()
            self._show_window(window)
        except Exception as e:
            self._show_error(f"Failed to launch Frequency Binary Viewer:\n{e}")

    def launch_batch_processor(self):
        try:
            window = BatchProcessorWindow()
            self._show_window(window)
        except Exception as e:
            self._show_error(f"Failed to launch Batch Processor:\n{e}")

    def launch_binary_image_viewer(self):
        try:
            window = VolumeImagesViewer()
            self._show_window(window)
        except Exception as e:
            self._show_error(f"Failed to launch Binary Image Viewer:\n{e}")

    def launch_hdf5_viewer(self):
        try:
            window = HDF5ImageViewer()
            self._show_window(window)
        except Exception as e:
            self._show_error(f"Failed to launch HDF5 Viewer:\n{e}")

    def launch_data_review_viewer(self):
        try:
            window = DataReviewViewer()
            self._show_window(window)
        except Exception as e:
            self._show_error(f"Failed to launch Data Review Viewer:\n{e}")

    def _show_window(self, window):
        # Keep reference to prevent GC
        self.open_windows.append(window)
        window.show()
        
        # Clean up closed windows from the list
        # We check periodically, or we can just hook into the destroyed signal
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        window.destroyed.connect(lambda: self._handle_window_closed(window))

    def _handle_window_closed(self, window):
        if window in self.open_windows:
            self.open_windows.remove(window)
            
    def _show_error(self, message):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(self, "Launch Error", message)

def main():
    app = QApplication(sys.argv)
    
    # Try to set application icon
    logo_path = os.path.join(_PARENT_DIR, 'references', 'travera_logo.ico')
    if os.path.exists(logo_path):
        app.setWindowIcon(QIcon(logo_path))
        
    app.setStyle("Fusion")
    
    ui = UtilitiesUI()
    ui.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
