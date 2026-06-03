"""
HDF5 Image Viewer

A GUI application for viewing HDF5 image files and their associated metadata.
Supports indexing by frame index or cell_id, and provides crop overlay visualization.
"""

import sys
import os
import h5py
import numpy as np
import polars as pl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QSpinBox, QFormLayout,
    QGroupBox, QTextEdit, QSizePolicy, QMessageBox, QTableView,
    QAbstractButton, QCheckBox, QComboBox, QHeaderView, QLineEdit
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor


class PolarsTableModel(QAbstractTableModel):
    """Table model for displaying a Polars DataFrame in QTableView."""
    
    def __init__(self, df=None):
        super().__init__()
        self._df = df if df is not None else pl.DataFrame()
        self._df_display = self._df # The currently visible (filtered/sorted) data
        
    def update_data(self, df):
        self.beginResetModel()
        self._df = df
        self._df_display = self._df
        self.endResetModel()

    def sort(self, column, order):
        if self._df_display.is_empty():
            return
        
        self.beginResetModel()
        col_name = self._df_display.columns[column]
        descending = (order == Qt.SortOrder.DescendingOrder)
        self._df_display = self._df_display.sort(col_name, descending=descending)
        self.endResetModel()


    def rowCount(self, parent=QModelIndex()):
        return self._df_display.height if not self._df_display.is_empty() else 0

    def columnCount(self, parent=QModelIndex()):
        return self._df_display.width if not self._df_display.is_empty() else 0

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        
        if role == Qt.ItemDataRole.DisplayRole:
            try:
                val = self._df_display[index.row(), index.column()]
                if val is None:
                    return ""
                if isinstance(val, (float, np.float32, np.float64)):
                    return f"{val:.4f}"
                return str(val)
            except:
                return ""
         
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return self._df_display.columns[section]
            else:
                return str(section)
        return None

    def get_row_data(self, row):
        """Returns the row data as a named tuple/dict for the given display row."""
        if 0 <= row < self._df_display.height:
            return self._df_display.row(row, named=True)
        return None

    def find_row_index(self, column_name, value):
        """Returns the display row index for a given column value."""
        if self._df_display.is_empty() or column_name not in self._df_display.columns:
            return -1
        try:
            # row_nr() is the polars equivalent for finding the index
            # We use with_row_index to get the current display position
            matches = self._df_display.with_row_index("display_row").filter(pl.col(column_name) == value)
            if not matches.is_empty():
                return int(matches[0, "display_row"])
        except Exception as e:
            print(f"find_row_index error: {e}")
        return -1


class ImageDisplayLabel(QLabel):
    """Custom label for displaying images with aspect ratio preservation and crop overlays."""
    
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.setText(f"No {title} image")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("""
            background-color: #000000;
            color: #ffffff;
            border: 2px solid #555;
            border-radius: 5px;
        """)
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setScaledContents(False)
        self._pixmap = None
        self._crop_rect = None  # (x0, y0, w, h) in image coordinates
        self._fixed_crop_rect = None  # (x0, y0, w, h)
        self._show_crop = True
        self._show_fixed_crop = False
    
    def set_image(self, image_array, crop_rect=None, fixed_crop_rect=None):
        """Set the image from a numpy array and optional crop rectangles."""
        if image_array is None or image_array.size == 0:
            self.setText(f"No {self.title} image")
            self._pixmap = None
            self._crop_rect = None
            self._fixed_crop_rect = None
            self.update()
            return
        
        # Ensure array is uint8
        if image_array.dtype != np.uint8:
            image_array = (image_array * 255.0).clip(0, 255).astype(np.uint8)
        
        height, width = image_array.shape[:2]
        
        if len(image_array.shape) == 2:
            q_image = QImage(image_array.data, width, height, width, QImage.Format.Format_Grayscale8).copy()
        elif len(image_array.shape) == 3 and image_array.shape[2] == 3:
            q_image = QImage(image_array.data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
        else:
            self.setText("Unsupported image format")
            self._pixmap = None
            self.update()
            return
        
        self._pixmap = QPixmap.fromImage(q_image)
        self._crop_rect = crop_rect
        self._fixed_crop_rect = fixed_crop_rect
        self.update()
    
    def set_show_crop(self, show):
        self._show_crop = show
        self.update()
        
    def set_show_fixed_crop(self, show):
        self._show_fixed_crop = show
        self.update()

    def paintEvent(self, event):
        """Override paintEvent to scale pixmap and draw crop overlays."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        
        if self._pixmap is None:
            super().paintEvent(event)
            return
        
        # Get label size
        label_width = self.width()
        label_height = self.height()
        
        # Get pixmap size
        pixmap_width = self._pixmap.width()
        pixmap_height = self._pixmap.height()
        
        # Calculate scaling factor to fit pixmap in label while maintaining aspect ratio
        scale = min(label_width / pixmap_width, label_height / pixmap_height)
        
        # Calculate scaled dimensions
        scaled_width = int(pixmap_width * scale)
        scaled_height = int(pixmap_height * scale)
        
        # Center the scaled pixmap
        x_offset = (label_width - scaled_width) // 2
        y_offset = (label_height - scaled_height) // 2
        
        # Draw the scaled pixmap
        painter.drawPixmap(x_offset, y_offset, scaled_width, scaled_height, self._pixmap)
        
        # Draw crop rectangle if requested and available
        if self._show_crop and self._crop_rect is not None:
            cx0, cy0, cw, ch = self._crop_rect
            
            # Map crop coordinates to display coordinates
            draw_x = x_offset + int(cx0 * scale)
            draw_y = y_offset + int(cy0 * scale)
            draw_w = int(cw * scale)
            draw_h = int(ch * scale)
            
            pen = QPen(QColor(0, 255, 0))  # Green box
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(draw_x, draw_y, draw_w, draw_h)
            
            # Draw label
            painter.setPen(QColor(0, 255, 0))
            painter.drawText(draw_x, draw_y - 5, f"Meta Crop: {cw}x{ch}")
            
        # Draw fixed crop rectangle if requested and available
        if self._show_fixed_crop and self._fixed_crop_rect is not None:
            cx0, cy0, cw, ch = self._fixed_crop_rect
            
            draw_x = x_offset + int(cx0 * scale)
            draw_y = y_offset + int(cy0 * scale)
            draw_w = int(cw * scale)
            draw_h = int(ch * scale)
            
            pen = QPen(QColor(255, 0, 0))  # Red box
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(draw_x, draw_y, draw_w, draw_h)
            
            # Draw label
            painter.setPen(QColor(255, 0, 0))
            painter.drawText(draw_x, draw_y + draw_h + 15, f"Fixed Crop: {cw}x{ch}")
        
        painter.end()


class HDF5ImageViewer(QMainWindow):
    """Main window for viewing HDF5 image files."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HDF5 Image Viewer")
        self.setGeometry(100, 100, 1600, 1000)
        
        # State
        self.hdf5_path = None
        self.hf = None
        self.df = pl.DataFrame()
        self.bf_path = None
        self.fl_paths = []
        self.current_indices = []  # Managed indices based on selection
        
        # Defaults for cropping (from hdf5_preprocessing.py)
        self.default_crop_w = 32
        self.default_crop_h = 32
        
        self.setup_ui()
    
    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Header: File selection
        header_layout = QHBoxLayout()
        self.file_label = QLabel("No HDF5 file selected")
        self.file_label.setStyleSheet("font-weight: bold; color: #444;")
        open_btn = QPushButton("Open HDF5 File")
        open_btn.clicked.connect(self.open_file)
        header_layout.addWidget(self.file_label, 1)
        header_layout.addWidget(open_btn)
        main_layout.addLayout(header_layout)
        
        # Splitter-like layout: Top for table/controls, Bottom for images
        top_layout = QHBoxLayout()
        
        # Left side: Navigation and selection
        nav_group = QGroupBox("Navigation")
        nav_layout = QVBoxLayout()
        
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Mode:"))
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        self.mode_group = QButtonGroup(self)
        self.index_radio = QRadioButton("Index")
        self.cell_radio = QRadioButton("Cell ID")
        self.cell_radio.setChecked(True)
        self.mode_group.addButton(self.index_radio)
        self.mode_group.addButton(self.cell_radio)
        self.index_radio.toggled.connect(self.update_nav_ui)
        self.cell_radio.toggled.connect(self.update_nav_ui)
        mode_layout.addWidget(self.index_radio)
        mode_layout.addWidget(self.cell_radio)
        nav_layout.addLayout(mode_layout)
        
        # Index widgets
        self.index_widget = QWidget()
        index_layout = QFormLayout(self.index_widget)
        self.index_spin = QSpinBox()
        self.index_spin.setRange(0, 0)
        self.index_spin.valueChanged.connect(self.on_index_changed)
        index_layout.addRow("Frame Index:", self.index_spin)
        self.index_widget.setVisible(False)
        nav_layout.addWidget(self.index_widget)
        
        # Cell ID widgets
        self.cell_widget = QWidget()
        self.cell_widget.setVisible(True)
        cell_layout = QVBoxLayout(self.cell_widget)
        cell_form = QFormLayout()
        
        id_nav_layout = QHBoxLayout()
        self.cell_id_prev_btn = QPushButton("◀")
        self.cell_id_prev_btn.setFixedWidth(30)
        self.cell_id_prev_btn.clicked.connect(self.prev_unique_cell_id)
        
        self.cell_id_input = QLineEdit()
        self.cell_id_input.setPlaceholderText("Cell ID")
        self.cell_id_input.returnPressed.connect(self.search_cell_id)
        
        self.cell_id_next_btn = QPushButton("▶")
        self.cell_id_next_btn.setFixedWidth(30)
        self.cell_id_next_btn.clicked.connect(self.next_unique_cell_id)
        
        id_nav_layout.addWidget(self.cell_id_prev_btn)
        id_nav_layout.addWidget(self.cell_id_input)
        id_nav_layout.addWidget(self.cell_id_next_btn)
        
        cell_form.addRow("Cell ID:", id_nav_layout)
        cell_layout.addLayout(cell_form)
        
        nav_layout.addWidget(self.cell_widget)
        
        # Crop controls
        crop_group = QGroupBox("Crop Options")
        crop_v_layout = QVBoxLayout()
        
        self.show_crop_check = QCheckBox("Show Meta Crop")
        self.show_crop_check.setChecked(True)
        self.show_crop_check.toggled.connect(self.on_crop_toggled)
        crop_v_layout.addWidget(self.show_crop_check)
        
        self.show_fixed_crop_check = QCheckBox("Display Fixed Crop")
        self.show_fixed_crop_check.toggled.connect(self.on_fixed_crop_toggled)
        crop_v_layout.addWidget(self.show_fixed_crop_check)
        
        crop_form = QFormLayout()
        self.crop_w_spin = QSpinBox()
        self.crop_w_spin.setRange(8, 512)
        self.crop_w_spin.setValue(self.default_crop_w)
        self.crop_w_spin.valueChanged.connect(self.refresh_display)
        self.crop_h_spin = QSpinBox()
        self.crop_h_spin.setRange(8, 512)
        self.crop_h_spin.setValue(self.default_crop_h)
        self.crop_h_spin.valueChanged.connect(self.refresh_display)
        crop_form.addRow("Width:", self.crop_w_spin)
        crop_form.addRow("Height:", self.crop_h_spin)
        crop_v_layout.addLayout(crop_form)
        
        crop_group.setLayout(crop_v_layout)
        nav_layout.addWidget(crop_group)
        
        nav_layout.addStretch()
        nav_group.setLayout(nav_layout)
        nav_group.setFixedWidth(250)
        top_layout.addWidget(nav_group)
        
        # Right side: Metadata table
        table_group = QGroupBox("Metadata Table")
        table_layout = QVBoxLayout(table_group)
        
        self.table_view = QTableView()
        self.table_model = PolarsTableModel()
        self.table_view.setModel(self.table_model)
        self.table_view.setSortingEnabled(True)
        self.table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table_view.clicked.connect(self.on_table_click)
        
        # VERY IMPORTANT: Disable automatic column/row resizing for large datasets
        # This prevents the UI from iterating over every single cell to determine size
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table_view.verticalHeader().setDefaultSectionSize(25)
        
        table_layout.addWidget(self.table_view)
        table_group.setLayout(table_layout)
        top_layout.addWidget(table_group, 1)
        
        main_layout.addLayout(top_layout, 1)
        
        # Bottom: Image displays (Horizontal tiling via Scroll Area)
        from PySide6.QtWidgets import QScrollArea
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area_content = QWidget()
        self.tile_layout = QHBoxLayout(self.scroll_area_content)
        self.tile_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.scroll_area.setWidget(self.scroll_area_content)
        
        main_layout.addWidget(self.scroll_area, 2)
        
        self.statusBar().showMessage("Ready")

    def update_nav_ui(self):
        is_index_mode = self.index_radio.isChecked()
        self.index_widget.setVisible(is_index_mode)
        self.cell_widget.setVisible(not is_index_mode)
        self.refresh_display()

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open HDF5", "", "HDF5 Files (*.hdf5 *.h5)")
        if not path:
            return
            
        try:
            if self.hf:
                self.hf.close()
            
            self.hdf5_path = path
            self.hf = h5py.File(path, 'r')
            self.file_label.setText(f"File: {os.path.basename(path)}")
            
            # Find datasets
            self.bf_path = None
            self.fl_paths = []
            def find_cb(name, obj):
                if isinstance(obj, h5py.Dataset):
                    if 'bf' in name.lower() and not self.bf_path: self.bf_path = name
                    if 'fl' in name.lower(): self.fl_paths.append(name)
            self.hf.visititems(find_cb)
            
            # Load metadata - Fallback between /meta/frames and /meta
            frames_ds = None
            if 'meta' in self.hf:
                if 'frames' in self.hf['meta']:
                    frames_ds = self.hf['meta/frames']
                elif isinstance(self.hf['meta'], h5py.Dataset):
                    frames_ds = self.hf['meta']
            
            if frames_ds is not None:
                self.statusBar().showMessage("Reading metadata from disk...")
                QApplication.instance().processEvents()
                
                # Reading the entire block in one shot is usually much faster 
                # than column-by-column due to HDF5's internal storage layout for structured arrays.
                # Use [()] to read the entire dataset as a numpy structured array
                full_data = frames_ds[()]
                
                self.statusBar().showMessage("Converting to dataframe...")
                QApplication.instance().processEvents()
                # Ensure we have a persistent original index for HDF5 access
                self.df = pl.from_numpy(full_data).with_row_index("orig_index")
                
                # Cleanup huge intermediate array immediately
                del full_data
                
                self.table_model.update_data(self.df)
            else:
                QMessageBox.warning(self, "Missing Metadata", "Could not find metadata in 'meta/frames' or '/meta'.")
                self.df = pl.DataFrame()
                self.table_model.update_data(self.df)
            
            num_frames = self.df.height if not self.df.is_empty() else 0
            self.index_spin.setRange(0, max(0, num_frames - 1))
            self.index_spin.setValue(0)
            
            self.statusBar().showMessage(f"Loaded {num_frames} frames.")
            self.refresh_display()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load HDF5:\n{e}")

    def on_index_changed(self):
        if self.index_radio.isChecked():
            self.refresh_display()

    def search_cell_id(self):
        cell_id_str = self.cell_id_input.text().strip()
        if not cell_id_str or self.df.is_empty():
            return
            
        try:
            target_id = int(cell_id_str)
            filtered = self.df.filter(pl.col("cell_id") == target_id)
            if filtered.is_empty():
                self.statusBar().showMessage(f"Cell ID {target_id} not found.")
                return
            
            self.current_indices = filtered["orig_index"].to_list()
            self.refresh_display()
            
            # Select the first matching row in the table
            row_idx = self.table_model.find_row_index("cell_id", target_id)
            if row_idx >= 0:
                self.table_view.selectRow(row_idx)
            
        except ValueError:
            QMessageBox.warning(self, "Invalid ID", "Please enter a numeric Cell ID.")

    def get_unique_cell_ids(self):
        """Returns sorted list of unique cell IDs present in the dataframe."""
        if 'cell_id' not in self.df.columns or self.df.is_empty():
            return []
        return sorted(self.df["cell_id"].unique().to_list())

    def next_unique_cell_id(self):
        unique_ids = self.get_unique_cell_ids()
        if not unique_ids: return
        
        try:
            current_id = int(self.cell_id_input.text())
            # Find first ID greater than current
            next_ids = [i for i in unique_ids if i > current_id]
            if next_ids:
                self.cell_id_input.setText(str(next_ids[0]))
                self.search_cell_id()
            else:
                self.statusBar().showMessage("At last Cell ID.")
        except ValueError:
            # If invalid current, just go to first
            self.cell_id_input.setText(str(unique_ids[0]))
            self.search_cell_id()

    def prev_unique_cell_id(self):
        unique_ids = self.get_unique_cell_ids()
        if not unique_ids: return
        
        try:
            current_id = int(self.cell_id_input.text())
            # Find IDs smaller than current
            prev_ids = [i for i in unique_ids if i < current_id]
            if prev_ids:
                self.cell_id_input.setText(str(prev_ids[-1]))
                self.search_cell_id()
            else:
                self.statusBar().showMessage("At first Cell ID.")
        except ValueError:
            # If invalid current, just go to last
            self.cell_id_input.setText(str(unique_ids[-1]))
            self.search_cell_id()

    def on_table_click(self, index):
        row_data = self.table_model.get_row_data(index.row())
        if not row_data:
            return
            
        if self.index_radio.isChecked():
            if "orig_index" in row_data:
                orig_idx = int(row_data["orig_index"])
                self.index_spin.setValue(orig_idx)
        else:
            # Cell ID mode: find all indices for this cell and show tiled view
            if "cell_id" in row_data:
                cid = row_data["cell_id"]
                self.cell_id_input.setText(str(cid))
                self.search_cell_id()
        
    def on_crop_toggled(self, checked):
        self.refresh_display()
        
    def on_fixed_crop_toggled(self, checked):
        self.refresh_display()

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())

    def refresh_display(self):
        if not self.hf or self.df.is_empty():
            return
            
        indices_to_show = []
        if self.index_radio.isChecked():
            indices_to_show = [self.index_spin.value()]
        else:
            # Sort current_indices descending (highest far left)
            indices_to_show = sorted(self.current_indices, reverse=True)
        
        if not indices_to_show:
            return
            
        try:
            self.clear_layout(self.tile_layout)
            self.tile_layout.setSpacing(0) # Flush tiling between cells
            
            crop_w = self.crop_w_spin.value()
            crop_h = self.crop_h_spin.value()
            show_meta = self.show_crop_check.isChecked()
            show_fixed = self.show_fixed_crop_check.isChecked()

            target_w = 88*2

            for idx in indices_to_show:
                # Load brightfield
                bf_img = self.hf[self.bf_path][idx]
                bh, bw = bf_img.shape[:2]
                bf_disp_h = int(target_w * bh / bw)
                
                meta_row = self.df.row(idx, named=True)
                
                # UI Container for this frame
                frame_vbox = QVBoxLayout()
                frame_vbox.setContentsMargins(0, 5, 0, 5) # Vertical padding only
                frame_vbox.setSpacing(0) # Truly tiled together
                
                index_lbl = QLabel(f"Index: {idx}")
                index_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                index_lbl.setStyleSheet("font-weight: bold; background-color: #333; color: white; border: 1px solid #555;")
                frame_vbox.addWidget(index_lbl)

                # Helper to add a channel label
                def add_channel_view(img, channel_name):
                    h, w = img.shape[:2]
                    disp_h = int(target_w * h / w)
                    
                    lbl = ImageDisplayLabel(channel_name)
                    lbl.setFixedSize(target_w, disp_h)
                    lbl.set_show_crop(show_meta)
                    lbl.set_show_fixed_crop(show_fixed)
                    
                    def get_meta_crop():
                        cx = meta_row.get(f'{channel_name}_cx')
                        cy = meta_row.get(f'{channel_name}_cy')
                        x0 = meta_row.get(f'{channel_name}_crop_x0')
                        y0 = meta_row.get(f'{channel_name}_crop_y0')
                        cw = meta_row.get(f'{channel_name}_box_w')
                        ch = meta_row.get(f'{channel_name}_box_h')
                        if cw is None: cw = self.crop_w_spin.value()
                        if ch is None: ch = self.crop_h_spin.value()
                        
                        if all(v is not None for v in [cx, cy, x0, y0]):
                            return (float(cx) - float(x0) - cw/2, float(cy) - float(y0) - ch/2, cw, ch)
                        return None

                    def get_fixed_crop():
                        # Use channel specific center if available, otherwise fallback
                        cx = meta_row.get(f'{channel_name}_cx') or meta_row.get('bf_cx')
                        cy = meta_row.get(f'{channel_name}_cy') or meta_row.get('bf_cy')
                        x0 = meta_row.get(f'{channel_name}_crop_x0') or meta_row.get('bf_crop_x0')
                        y0 = meta_row.get(f'{channel_name}_crop_y0') or meta_row.get('bf_crop_y0')
                        if all(v is not None for v in [cx, cy, x0, y0]):
                            return (float(cx) - float(x0) - crop_w/2, float(cy) - float(y0) - crop_h/2, crop_w, crop_h)
                        return None

                    lbl.set_image(img, get_meta_crop(), get_fixed_crop())
                    frame_vbox.addWidget(lbl)

                # Add BF
                add_channel_view(bf_img, 'bf')
                
                # Add all FL channels
                for fl_p in self.fl_paths:
                    fl_img = self.hf[fl_p][idx]
                    add_channel_view(fl_img, fl_p.split('/')[-1])
                
                frame_vbox.addStretch()
                
                container_widget = QWidget()
                container_widget.setLayout(frame_vbox)
                container_widget.setFixedWidth(target_w)
                self.tile_layout.addWidget(container_widget)

            # Correctly select the row in the table even if sorted
            if self.index_radio.isChecked():
                orig_idx = indices_to_show[0]
                row_idx = self.table_model.find_row_index("orig_index", orig_idx)
                if row_idx >= 0:
                    self.table_view.selectRow(row_idx)
            else:
                # In Cell ID mode, search_cell_id handles selection to avoid 
                # selecting a random match every refresh.
                pass
                
            self.statusBar().showMessage(f"Displaying {len(indices_to_show)} frames.")
            
        except Exception as e:
            print(f"Error refreshing display: {e}")
            import traceback
            traceback.print_exc()

    def closeEvent(self, event):
        if self.hf:
            self.hf.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = HDF5ImageViewer()
    viewer.show()
    sys.exit(app.exec())
