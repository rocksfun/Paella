"""
Volume Images Viewer

A GUI application for viewing VolumeImages.bin and Images.bin files.
Allows users to select a frame index and view the associated metadata and images.
Supports both BF+FL mode (VolumeImages.bin) and BF-only mode (Images.bin) files.
"""

import sys
import os
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QSpinBox, QFormLayout,
    QGroupBox, QTextEdit, QSizePolicy, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QPainter

# Defaults for legacy metadata (no width/height tokens)
DEFAULT_BF_DIM = (88, 800)  # (height, width)
DEFAULT_FL_DIM = (88, 800)  # (height, width)
METADATA_SIZE_CANDIDATES = (256, 73)


class ImageDisplayLabel(QLabel):
    """Custom label for displaying images with aspect ratio preservation."""
    
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
        self.setScaledContents(False)
        self._pixmap = None
    
    def set_image(self, image_array, label_text=""):
        """Set the image from a numpy array."""
        if image_array is None or image_array.size == 0:
            self.setText("No image")
            self._pixmap = None
            self.update()
            return
        
        # Get image dimensions
        if len(image_array.shape) == 2:
            height, width = image_array.shape
            bytes_per_line = width
            q_image = QImage(image_array.data, width, height, bytes_per_line, QImage.Format.Format_Grayscale8).copy()
        elif len(image_array.shape) == 3:
            height, width, channels = image_array.shape
            if channels == 3:
                bytes_per_line = width * 3
                q_image = QImage(image_array.data, width, height, bytes_per_line, QImage.Format.Format_RGB888).copy()
            else:
                self.setText("Unsupported image format")
                self._pixmap = None
                self.update()
                return
        else:
            self.setText("Invalid image format")
            self._pixmap = None
            self.update()
            return
        
        # Convert to QPixmap
        pixmap = QPixmap.fromImage(q_image)
        self._pixmap = pixmap
        
        # Update label text
        if label_text:
            self.setText(f"{label_text}\n{width}x{height}")
        else:
            self.setText(f"{width}x{height}")
        
        self.update()
    
    def paintEvent(self, event):
        """Override paintEvent to scale pixmap while maintaining aspect ratio."""
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
        scale = min(scale_x, scale_y)
        
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
        
        painter.end()


class VolumeImagesViewer(QMainWindow):
    """Main window for viewing VolumeImages.bin and Images.bin files."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Volume Images Viewer")
        self.setGeometry(100, 100, 1400, 900)
        
        # File state
        self.current_file_path = None
        self.file_handle = None
        self.total_frames = 0
        self.frame_size_cache = {}  # Cache frame sizes for faster navigation
        self.metadata_size = 256
        self.bf_bytes_per_pixel = 1
        self.fl_bytes_per_pixel = 1
        self.ref_bytes_per_pixel = 0
        self.frame_size = 0
        self.is_pyimage_format = False
        self.has_fl_data = True
        self.has_ref_data = False
        self.default_bf_dim = DEFAULT_BF_DIM
        self.default_fl_dim = DEFAULT_FL_DIM
        self.bf_height, self.bf_width = self.default_bf_dim
        self.fl_height, self.fl_width = self.default_fl_dim
        
        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # File selection section
        file_group = QGroupBox("File Selection")
        file_layout = QHBoxLayout()
        self.file_path_label = QLabel("No file selected")
        self.file_path_label.setStyleSheet("font-family: monospace; padding: 5px;")
        open_button = QPushButton("Open File...")
        open_button.clicked.connect(self.open_file)
        file_layout.addWidget(self.file_path_label, 1)
        file_layout.addWidget(open_button)
        file_group.setLayout(file_layout)
        main_layout.addWidget(file_group)
        
        # Frame navigation section
        nav_group = QGroupBox("Frame Navigation")
        nav_layout = QHBoxLayout()
        
        prev_button = QPushButton("◀ Previous")
        prev_button.clicked.connect(self.previous_frame)
        nav_layout.addWidget(prev_button)
        
        self.frame_index_spin = QSpinBox()
        self.frame_index_spin.setMinimum(0)
        self.frame_index_spin.setMaximum(0)
        self.frame_index_spin.setValue(0)
        self.frame_index_spin.valueChanged.connect(self.load_frame)
        nav_layout.addWidget(QLabel("Frame:"))
        nav_layout.addWidget(self.frame_index_spin)
        
        total_label = QLabel("/ 0")
        self.total_frames_label = total_label
        nav_layout.addWidget(total_label)
        
        next_button = QPushButton("Next ▶")
        next_button.clicked.connect(self.next_frame)
        nav_layout.addWidget(next_button)
        
        nav_layout.addStretch()
        nav_group.setLayout(nav_layout)
        main_layout.addWidget(nav_group)
        
        # Images and metadata section
        content_layout = QHBoxLayout()
        
        # Left side: Images
        images_group = QGroupBox("Images")
        images_layout = QVBoxLayout()
        
        # Brightfield image
        bf_label = QLabel("Brightfield")
        bf_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bf_label.setStyleSheet("font-weight: bold; font-size: 12pt;")
        images_layout.addWidget(bf_label)
        self.bf_display = ImageDisplayLabel()
        images_layout.addWidget(self.bf_display, 1)
        
        # Fluorescent image
        fl_label = QLabel("Fluorescent")
        fl_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fl_label.setStyleSheet("font-weight: bold; font-size: 12pt;")
        images_layout.addWidget(fl_label)
        self.fl_display = ImageDisplayLabel()
        images_layout.addWidget(self.fl_display, 1)
        
        images_group.setLayout(images_layout)
        content_layout.addWidget(images_group, 2)
        
        # Right side: Metadata
        metadata_group = QGroupBox("Metadata")
        metadata_layout = QVBoxLayout()
        
        self.metadata_text = QTextEdit()
        self.metadata_text.setReadOnly(True)
        self.metadata_text.setStyleSheet("font-family: monospace; font-size: 10pt;")
        metadata_layout.addWidget(self.metadata_text)
        
        metadata_group.setLayout(metadata_layout)
        content_layout.addWidget(metadata_group, 1)
        
        main_layout.addLayout(content_layout, 1)
        
        # Status bar
        self.statusBar().showMessage("Ready")
    
    def open_file(self):
        """Open a VolumeImages.bin or Images.bin file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Image File",
            "",
            "Binary Files (*.bin);;All Files (*)"
        )
        
        if not file_path:
            return
        
        # Close previous file if open
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None
        
        try:
            # Open file in binary read mode
            self.file_handle = open(file_path, 'rb')
            self.current_file_path = file_path
            self.file_path_label.setText(os.path.basename(file_path))

            # Detect metadata size, capture dims, and data depth from the first entry
            self._detect_file_layout()
            print(f"[INFO] Using metadata size: {self.metadata_size} bytes")
            print(f"[INFO] Using data depth: {self._data_depth_text()}")

            if self.frame_size > 0:
                file_size = os.path.getsize(self.current_file_path)
                remainder = file_size % self.frame_size
                if remainder:
                    QMessageBox.warning(
                        self,
                        "File Size Mismatch",
                        "File size is not a multiple of the entry size.\n"
                        f"File size: {file_size} bytes\n"
                        f"Entry size: {self.frame_size} bytes\n"
                        f"Trailing bytes: {remainder}\n\n"
                        "Frames will be truncated to the last full entry."
                    )
            else:
                QMessageBox.warning(
                    self,
                    "Invalid Entry Size",
                    "Unable to determine a valid entry size for this file."
                )
            
            # Calculate total number of frames
            # Frame sizes are consistent within a file for this viewer
            self.total_frames = self._count_frames()
            
            # Update UI
            self.frame_index_spin.setMaximum(max(0, self.total_frames - 1))
            self.frame_index_spin.setValue(0)
            self.total_frames_label.setText(f"/ {self.total_frames}")
            
            # Load first frame
            if self.total_frames > 0:
                self.load_frame(0)
            
            self.statusBar().showMessage(f"Opened file: {os.path.basename(file_path)} ({self.total_frames} frames)")
        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open file:\n{e}")
            self.file_handle = None
            self.current_file_path = None
    
    def _count_frames(self):
        """Count total number of frames in the file and cache all frame positions."""
        if not self.file_handle or not self.current_file_path:
            return 0

        if self.frame_size <= 0:
            return 0

        file_size = os.path.getsize(self.current_file_path)
        frame_count = file_size // self.frame_size
        remainder = file_size % self.frame_size
        if remainder:
            print(f"[WARN] File size is not an even multiple of frame size; ignoring trailing {remainder} bytes.")

        self.frame_size_cache = {}
        for frame_index in range(frame_count):
            frame_start = frame_index * self.frame_size
            self.frame_size_cache[frame_index] = (
                frame_start,
                self.frame_size,
                self.bf_width,
                self.bf_height,
                self.fl_width,
                self.fl_height,
            )

        print(f"Found {frame_count} frames in file")
        return frame_count

    def _decode_metadata_bytes(self, meta_bytes):
        return self._decode_meta_ascii(meta_bytes)

    def _decode_meta_ascii(self, meta_bytes):
        end = len(meta_bytes)
        for i, byte_val in enumerate(meta_bytes):
            if byte_val == 0 or byte_val < 32 or byte_val > 126:
                end = i
                break
        return meta_bytes[:end].decode("ascii", errors="replace").strip()

    def _legacy_metadata_tokens(self, meta_bytes):
        meta_str = self._decode_meta_ascii(meta_bytes)
        return meta_str.split("_") if meta_str else []

    def _parse_ref_index_from_metadata(self, meta_bytes):
        tokens = self._legacy_metadata_tokens(meta_bytes)
        if len(tokens) < 5:
            return ""
        return tokens[4].strip()

    def _to_uint8(self, image_array):
        if image_array is None:
            return None
        if image_array.dtype == np.uint8:
            return image_array
        if image_array.dtype == np.uint16:
            max_val = int(image_array.max()) if image_array.size else 0
            if max_val <= 0:
                return image_array.astype(np.uint8)
            if max_val <= 4095:
                shift = 4
            elif max_val <= 16383:
                shift = 6
            else:
                shift = 8
            return (image_array >> shift).astype(np.uint8)
        return np.clip(image_array, 0, 255).astype(np.uint8)

    def _data_depth_text(self):
        parts = []
        if self.bf_bytes_per_pixel:
            parts.append(f"BF {self.bf_bytes_per_pixel * 8}-bit")
        if self.fl_width > 0 and self.fl_height > 0 and self.fl_bytes_per_pixel:
            parts.append(f"FL {self.fl_bytes_per_pixel * 8}-bit")
        if self.has_ref_data and self.ref_bytes_per_pixel:
            parts.append(f"REF {self.ref_bytes_per_pixel * 8}-bit")
        if not parts:
            return "Unknown"
        summary = ", ".join(parts)
        if any(
            bpp > 1
            for bpp in (self.bf_bytes_per_pixel, self.fl_bytes_per_pixel, self.ref_bytes_per_pixel)
            if bpp
        ):
            summary += " (displayed as 8-bit)"
        return summary

    def _parse_dims_from_parts(self, parts):
        return self._parse_dims_from_tokens_generic(parts)

    def _parse_legacy_dims_from_tokens(self, tokens):
        return self._parse_dims_from_tokens_generic(tokens)

    def _parse_dims_from_tokens_generic(self, tokens):
        def _token_to_int(token):
            digits = "".join(ch for ch in token if ch.isdigit())
            if not digits:
                return None
            try:
                return int(digits)
            except Exception:
                return None

        if len(tokens) >= 8:
            try:
                bf_width = _token_to_int(tokens[6])
                bf_height = _token_to_int(tokens[7])
                if bf_width is None or bf_height is None:
                    return None
                if bf_width <= 0 or bf_height <= 0:
                    return None
                if len(tokens) >= 10:
                    fl_width = _token_to_int(tokens[8])
                    fl_height = _token_to_int(tokens[9])
                    if fl_width is None or fl_height is None:
                        return None
                else:
                    fl_width, fl_height = bf_width, bf_height
                if fl_width < 0 or fl_height < 0:
                    return None
                return bf_width, bf_height, fl_width, fl_height
            except Exception:
                return None
        return None

    def _detect_file_layout(self):
        if not self.file_handle or not self.current_file_path:
            return self.metadata_size

        file_size = os.path.getsize(self.current_file_path)
        file_name = os.path.basename(self.current_file_path).lower()
        candidates = []

        for meta_size in METADATA_SIZE_CANDIDATES:
            try:
                self.file_handle.seek(0)
                meta = self.file_handle.read(meta_size)
                if len(meta) < meta_size:
                    continue

                if meta_size == 73:
                    parts = self._legacy_metadata_tokens(meta)
                    dims = self._parse_legacy_dims_from_tokens(parts)
                else:
                    meta_str = self._decode_metadata_bytes(meta)
                    parts = meta_str.split('_') if meta_str else []
                    dims = self._parse_dims_from_tokens_generic(parts)
                if dims is not None:
                    bf_w, bf_h, fl_w, fl_h = dims
                else:
                    bf_h, bf_w = self.default_bf_dim
                    fl_h, fl_w = self.default_fl_dim
                    bf_w, bf_h, fl_w, fl_h = int(bf_w), int(bf_h), int(fl_w), int(fl_h)

                is_pyimage = (meta_size == 256 and len(parts) >= 10)
                if meta_size == 256 and not is_pyimage:
                    continue

                if meta_size == 256:
                    fl_candidates = [(fl_w, fl_h)]
                    layout_candidates = [
                        {"bf_bpp": 1, "fl_bpp": 1, "ref_bpp": 0, "label": "bf_fl"},
                    ]
                else:
                    fl_candidates = [(fl_w, fl_h), (0, 0)]
                    layout_candidates = [
                        {"bf_bpp": 1, "fl_bpp": 1, "ref_bpp": 0, "label": "bf_fl"},
                        {"bf_bpp": 1, "fl_bpp": 2, "ref_bpp": 2, "label": "bf_fl_ref16"},
                    ]

                for fl_w_c, fl_h_c in fl_candidates:
                    for layout in layout_candidates:
                        if fl_w_c == 0 and fl_h_c == 0 and layout["fl_bpp"] != 0:
                            continue
                        entry_size = meta_size + (
                            (bf_w * bf_h * layout["bf_bpp"]) +
                            (fl_w_c * fl_h_c * layout["fl_bpp"]) +
                            (fl_w_c * fl_h_c * layout["ref_bpp"])
                        )
                        if entry_size <= meta_size:
                            continue
                        if file_size < entry_size:
                            continue
                        remainder = file_size % entry_size

                        score = 0
                        if meta_size == 256 and is_pyimage:
                            score += 4
                        if meta_size == 73 and (len(parts) >= 8 and not is_pyimage):
                            score += 3
                        if dims is not None:
                            score += 1
                        if remainder == 0:
                            score += 2
                        if layout["fl_bpp"] == 1 and layout["ref_bpp"] == 0 and (
                            "stream8" in file_name or "8bit" in file_name
                        ):
                            score += 1
                        if layout["ref_bpp"] == 2 and (
                            "stream16" in file_name or "16bit" in file_name
                        ):
                            score += 1
                        if fl_w_c == 0 and fl_h_c == 0:
                            if "images" in file_name and "volume" not in file_name:
                                score += 1
                        else:
                            if "volume" in file_name:
                                score += 1

                        candidates.append(
                            {
                                "meta_size": meta_size,
                                "bf_w": bf_w,
                                "bf_h": bf_h,
                                "fl_w": fl_w_c,
                                "fl_h": fl_h_c,
                                "bf_bpp": layout["bf_bpp"],
                                "fl_bpp": layout["fl_bpp"],
                                "ref_bpp": layout["ref_bpp"],
                                "entry_size": entry_size,
                                "is_pyimage": is_pyimage,
                                "remainder": remainder,
                                "score": score,
                            }
                        )
            except Exception:
                continue

        if not candidates:
            self.metadata_size = METADATA_SIZE_CANDIDATES[0]
            self.bf_bytes_per_pixel = 1
            self.fl_bytes_per_pixel = 1
            self.ref_bytes_per_pixel = 0
            self.bf_height, self.bf_width = self.default_bf_dim
            self.fl_height, self.fl_width = self.default_fl_dim
            self.has_fl_data = True
            self.has_ref_data = False
            self.is_pyimage_format = (self.metadata_size == 256)
            self.frame_size = self.metadata_size + (
                (self.bf_width * self.bf_height * self.bf_bytes_per_pixel) +
                (self.fl_width * self.fl_height * self.fl_bytes_per_pixel)
            )
            return self.metadata_size

        best = max(
            candidates,
            key=lambda item: (
                item["score"],
                item["remainder"] == 0,
                item["meta_size"],
            ),
        )
        self.metadata_size = best["meta_size"]
        self.bf_bytes_per_pixel = best["bf_bpp"]
        self.fl_bytes_per_pixel = best["fl_bpp"]
        self.ref_bytes_per_pixel = best["ref_bpp"]
        self.bf_width = best["bf_w"]
        self.bf_height = best["bf_h"]
        self.fl_width = best["fl_w"]
        self.fl_height = best["fl_h"]
        self.has_fl_data = (self.fl_width > 0 and self.fl_height > 0)
        self.has_ref_data = (self.ref_bytes_per_pixel > 0)
        self.is_pyimage_format = (self.metadata_size == 256 and best["is_pyimage"])
        self.frame_size = best["entry_size"]
        return self.metadata_size
    
    def _get_frame_position(self, frame_index):
        """Get file position and size for a given frame index."""
        # Frame positions should already be cached from _count_frames
        if frame_index in self.frame_size_cache:
            return self.frame_size_cache[frame_index]
        
        # If not in cache, return None (shouldn't happen if counting worked)
        return None
    
    def load_frame(self, frame_index):
        """Load and display a frame at the given index."""
        if not self.file_handle or frame_index < 0 or frame_index >= self.total_frames:
            return
        
        try:
            # Get frame position
            frame_info = self._get_frame_position(frame_index)
            if frame_info is None:
                self.statusBar().showMessage(f"Error: Could not locate frame {frame_index}")
                return
            
            frame_start, frame_size, bf_width, bf_height, fl_width, fl_height = frame_info
            
            # Seek to frame start
            self.file_handle.seek(frame_start)
            
            # Read metadata
            metadata_bytes = self.file_handle.read(self.metadata_size)
            if len(metadata_bytes) < self.metadata_size:
                self.statusBar().showMessage(f"Error: Could not read metadata for frame {frame_index}")
                return
            
            # Parse metadata
            metadata_str = self._decode_metadata_bytes(metadata_bytes)
            parts = metadata_str.split('_') if metadata_str else []
            
            is_pyimage = self.metadata_size == 256 and len(parts) >= 10

            if is_pyimage:
                # Extract metadata values (always present)
                computer_time = parts[0]
                bf_camera_time = parts[1]
                bf_frame_number = parts[2]
                fl_camera_time = parts[3]
                fl_frame_number = parts[4]
                trigger_flag = parts[5]

                # Extract new fields if present (format version >= 14 fields)
                bf_exposure_us = float(parts[10]) if len(parts) > 10 else None
                fl_exposure_us = float(parts[11]) if len(parts) > 11 else None
                blue_led_current_a = float(parts[12]) if len(parts) > 12 else None
                photodiode_voltage_v = float(parts[13]) if len(parts) > 13 else None
            else:
                # Legacy metadata (73 bytes): timestamp_packet_frame1_frame2_refIndex_loopIndex_...
                legacy_tokens = self._legacy_metadata_tokens(metadata_bytes)
                timestamp = legacy_tokens[0] if len(legacy_tokens) > 0 else ""
                packet = legacy_tokens[1] if len(legacy_tokens) > 1 else ""
                frame1 = legacy_tokens[2] if len(legacy_tokens) > 2 else ""
                frame2 = legacy_tokens[3] if len(legacy_tokens) > 3 else ""
                ref_index = self._parse_ref_index_from_metadata(metadata_bytes)
                loop_index = legacy_tokens[5] if len(legacy_tokens) > 5 else ""
                computer_time = timestamp
                bf_camera_time = packet
                bf_frame_number = frame1
                fl_camera_time = frame2
                fl_frame_number = ref_index
                trigger_flag = loop_index
                bf_exposure_us = None
                fl_exposure_us = None
                blue_led_current_a = None
                photodiode_voltage_v = None
            
            # Read BF image
            bf_size = bf_width * bf_height * self.bf_bytes_per_pixel
            bf_data = self.file_handle.read(bf_size)
            if len(bf_data) < bf_size:
                self.statusBar().showMessage(f"Error: Could not read BF image for frame {frame_index}")
                return
            
            # Convert BF to numpy array
            bf_dtype = np.uint8 if self.bf_bytes_per_pixel == 1 else np.dtype("<u2")
            bf_array_raw = np.frombuffer(bf_data, dtype=bf_dtype).reshape((bf_height, bf_width))
            bf_array = self._to_uint8(bf_array_raw)
            
            # Read FL image (only if dimensions are non-zero)
            fl_size = fl_width * fl_height * self.fl_bytes_per_pixel
            if fl_size > 0:
                # BF+FL mode: read FL image data
                fl_data = self.file_handle.read(fl_size)
                if len(fl_data) < fl_size:
                    self.statusBar().showMessage(f"Error: Could not read FL image for frame {frame_index}")
                    return
                fl_dtype = np.uint8 if self.fl_bytes_per_pixel == 1 else np.dtype("<u2")
                fl_array_raw = np.frombuffer(fl_data, dtype=fl_dtype).reshape((fl_height, fl_width))
                fl_array = self._to_uint8(fl_array_raw)
            else:
                # BF-only mode: no FL image data in file
                fl_array = None

            # Read REF image if present (legacy BF8 + FL16 + REF16)
            if self.has_ref_data and fl_width > 0 and fl_height > 0:
                ref_size = fl_width * fl_height * self.ref_bytes_per_pixel
                ref_data = self.file_handle.read(ref_size)
                if len(ref_data) < ref_size:
                    self.statusBar().showMessage(f"Error: Could not read REF image for frame {frame_index}")
                    return
            
            # Display images
            self.bf_display.set_image(bf_array, "Brightfield")
            if fl_array is not None:
                self.fl_display.set_image(fl_array, "Fluorescent")
            else:
                # BF-only mode: show "No image" for FL display
                self.fl_display.set_image(None, "Fluorescent (BF-only mode)")
            
            # Display metadata
            mode_text = "BF-only mode" if (fl_width == 0 and fl_height == 0) else "BF+FL mode"
            data_depth_text = self._data_depth_text()
            ref_data_line = "REF Data: Present (not displayed)" if self.has_ref_data else "REF Data: None"
            metadata_text = f"""Frame Index: {frame_index}
Mode: {mode_text}
Data Depth: {data_depth_text}
{ref_data_line}

Computer Timestamp: {computer_time}
BF Camera Timestamp: {bf_camera_time}
BF Frame Number: {bf_frame_number}
FL Camera Timestamp: {fl_camera_time}
FL Frame Number: {fl_frame_number}
Trigger Flag: {trigger_flag}

BF Image Dimensions: {bf_width} × {bf_height}
FL Image Dimensions: {fl_width} × {fl_height} {"(no FL data in file)" if (fl_width == 0 and fl_height == 0) else ""}"""
            
            # Add new metadata fields if present
            if bf_exposure_us is not None:
                metadata_text += f"\n\nBF Exposure: {bf_exposure_us:.1f} µs"
            if fl_exposure_us is not None:
                metadata_text += f"\nFL Exposure: {fl_exposure_us:.1f} µs"
            if blue_led_current_a is not None:
                metadata_text += f"\nBlue LED Current: {blue_led_current_a:.3f} A"
            if photodiode_voltage_v is not None:
                metadata_text += f"\nPhotodiode Voltage: {photodiode_voltage_v:.3f} V"
            
            # Rebuild metadata text to support legacy 73-byte headers
            if is_pyimage:
                metadata_text = f"""Frame Index: {frame_index}
Mode: {mode_text}
Metadata Size: {self.metadata_size} bytes
Data Depth: {data_depth_text}
{ref_data_line}

Computer Timestamp: {computer_time}
BF Camera Timestamp: {bf_camera_time}
BF Frame Number: {bf_frame_number}
FL Camera Timestamp: {fl_camera_time}
FL Frame Number: {fl_frame_number}
Trigger Flag: {trigger_flag}

BF Image Dimensions: {bf_width} x {bf_height}
FL Image Dimensions: {fl_width} x {fl_height} {"(no FL data in file)" if (fl_width == 0 and fl_height == 0) else ""}"""

                if bf_exposure_us is not None:
                    metadata_text += f"\n\nBF Exposure: {bf_exposure_us:.1f} us"
                if fl_exposure_us is not None:
                    metadata_text += f"\nFL Exposure: {fl_exposure_us:.1f} us"
                if blue_led_current_a is not None:
                    metadata_text += f"\nBlue LED Current: {blue_led_current_a:.3f} A"
                if photodiode_voltage_v is not None:
                    metadata_text += f"\nPhotodiode Voltage: {photodiode_voltage_v:.3f} V"
            else:
                metadata_text = f"""Frame Index: {frame_index}
Mode: {mode_text}
Metadata Size: {self.metadata_size} bytes
Data Depth: {data_depth_text}
{ref_data_line}

Timestamp: {timestamp}
Packet: {packet}
Frame1: {frame1}
Frame2: {frame2}
Ref Index: {ref_index}
Loop Index: {loop_index}

BF Image Dimensions: {bf_width} x {bf_height}
FL Image Dimensions: {fl_width} x {fl_height} {"(no FL data in file)" if (fl_width == 0 and fl_height == 0) else ""}"""

            metadata_text += f"""

File Position: {frame_start}
Frame Size: {frame_size} bytes"""
            
            self.metadata_text.setPlainText(metadata_text)
            self.statusBar().showMessage(f"Loaded frame {frame_index + 1} of {self.total_frames}")
        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load frame {frame_index}:\n{e}")
            import traceback
            traceback.print_exc()
    
    def next_frame(self):
        """Load next frame."""
        current = self.frame_index_spin.value()
        if current < self.total_frames - 1:
            self.frame_index_spin.setValue(current + 1)
    
    def previous_frame(self):
        """Load previous frame."""
        current = self.frame_index_spin.value()
        if current > 0:
            self.frame_index_spin.setValue(current - 1)
    
    def closeEvent(self, event):
        """Handle window close event."""
        if self.file_handle:
            self.file_handle.close()
        event.accept()


def main():
    """Main entry point for the viewer application."""
    app = QApplication(sys.argv)
    
    viewer = VolumeImagesViewer()
    viewer.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
