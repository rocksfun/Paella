import os
import sys
import csv
import re
import pandas as pd
from datetime import datetime

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLabel, QPushButton, QSplitter, QMessageBox, QHeaderView, QCheckBox, QDoubleSpinBox,
    QTableWidget, QTableWidgetItem
)
from PySide6.QtCore import Qt, Signal, Slot, QThread
from PySide6.QtGui import QColor
import matplotlib
matplotlib.use('QtAgg')
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
import seaborn as sns

# Adjust path for internal module imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

from helper_functions.META_sample_selection import _get_local_data_path
from helper_functions.SYSTEM_pull_config_io import SYSTEM_CONFIG_PATH
from helper_functions.DATA_posthoc_frequency_analysis import PostHocFrequencyAnalyzer
import polars as pl

class DataScannerThread(QThread):
    """Background thread to scan directories and update cache without freezing UI."""
    finished_scan = Signal(list)  # Emits list of dictionaries
    error_scan = Signal(str)
    progress_update = Signal(str)

    def __init__(self, data_path, cache_path):
        super().__init__()
        self.data_path = data_path
        self.cache_path = cache_path

    def run(self):
        try:
            cached_data = self.load_cache()
            cached_folders = {item['Folder Path'] for item in cached_data}

            new_data = []
            
            if not os.path.exists(self.data_path):
                self.error_scan.emit(f"Data path does not exist: {self.data_path}")
                return

            folders = [f for f in os.listdir(self.data_path) if os.path.isdir(os.path.join(self.data_path, f))]
            
            for folder in folders:
                folder_path = os.path.join(self.data_path, folder)
                if folder_path in cached_folders:
                    continue
                
                self.progress_update.emit(f"Scanning {folder}...")
                
                # Look for uncalibrated peaks csv
                try:
                    csv_files = [f for f in os.listdir(folder_path) if f.endswith('_uncalibrated_peaks.csv')]
                except PermissionError:
                    continue
                
                if not csv_files:
                    continue
                
                # Look for metadata file to get start time and date
                try:
                    metadata_files = [f for f in os.listdir(folder_path) if f.endswith('_metadata.txt')]
                    if metadata_files:
                        with open(os.path.join(folder_path, metadata_files[0]), 'r', encoding='utf-8') as mf:
                            for line in mf:
                                if line.startswith("Experiment_datetime\t"):
                                    dt_str = line.split('\t')[1].strip()
                                    parts = dt_str.split(' ')
                                    if len(parts) >= 2:
                                        run_date = parts[0]
                                        start_time = parts[1]
                                    break
                except Exception as e:
                    print(f"Error reading metadata for {folder}: {e}")
                    
                # Fallback to regex if metadata did not populate them
                if run_date == "Unknown":
                    match = re.match(r'^(\d{6})_(\d{4,6})', folder)
                    if match:
                        date_str = match.group(1)
                        time_str = match.group(2)
                        try:
                            dt_date = datetime.strptime(date_str, "%y%m%d")
                            run_date = dt_date.strftime("%Y-%m-%d")
                            
                            if len(time_str) == 6:
                                dt_time = datetime.strptime(time_str, "%H%M%S")
                                start_time = dt_time.strftime("%H:%M:%S")
                            elif len(time_str) == 4:
                                dt_time = datetime.strptime(time_str, "%H%M")
                                start_time = dt_time.strftime("%H:%M")
                        except ValueError:
                            pass

                csv_file = os.path.join(folder_path, csv_files[0])
                
                # Use PostHocFrequencyAnalyzer to parse the file
                analyzer = PostHocFrequencyAnalyzer()
                try:
                    analyzer.load_uncalibrated_peaks(csv_file)
                    # Force condition evaluation if experiment_flags.txt is present
                    flags_file = csv_file.replace('_uncalibrated_peaks.csv', '_experiment_flags.txt')
                    if os.path.exists(flags_file):
                        # This method in analyzer adds the 'condition' column if applicable
                        pass # PostHocFrequencyAnalyzer loads flags automatically via other methods, or we can just read conditions if already present
                    
                    df = analyzer.peaks_df
                    if 'condition' not in df.columns:
                        # Fallback if no condition column
                        df = df.with_columns(pl.lit("Assumed Calibration").alias('condition'))
                        
                    condition_counts = df.group_by('condition').len()
                    conditions = condition_counts['condition'].to_list()
                    counts = condition_counts['len'].to_list()
                    
                    for cond, count in zip(conditions, counts):
                        entry = {
                            'Folder Path': folder_path,
                            'Folder Name': folder,
                            'Run Date': run_date,
                            'Start Time': start_time,
                            'Condition': cond if cond is not None else "Unknown",
                            'Peaks Count': count,
                            'CSV Path': csv_file
                        }
                        new_data.append(entry)
                except Exception as e:
                    print(f"Error processing {folder}: {e}")
                    continue

            if new_data:
                self.save_cache(new_data)
                
            all_data = cached_data + new_data
            self.finished_scan.emit(all_data)

        except Exception as e:
            self.error_scan.emit(str(e))

    def load_cache(self):
        cache = []
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, mode='r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        cache.append(row)
            except Exception as e:
                print(f"Error reading cache: {e}")
        return cache

    def save_cache(self, new_data):
        fieldnames = ['Folder Path', 'Folder Name', 'Run Date', 'Start Time', 'Condition', 'Peaks Count', 'CSV Path']
        file_exists = os.path.exists(self.cache_path)
        try:
            with open(self.cache_path, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()
                for row in new_data:
                    writer.writerow(row)
        except Exception as e:
            print(f"Error writing to cache: {e}")

class DataReviewViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Paella Data Review Helper")
        self.resize(1200, 800)
        
        self.data_path = _get_local_data_path()
        if not self.data_path:
            QMessageBox.warning(self, "Configuration Error", "Could not determine local data path from system config.")
            self.data_path = ""
            
        config_dir = os.path.dirname(SYSTEM_CONFIG_PATH) if SYSTEM_CONFIG_PATH else os.getcwd()
        self.cache_path = os.path.join(config_dir, "data_review_cache.csv")
        
        self.all_data = []
        
        self.setup_ui()
        
        if self.data_path:
            self.start_scan()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Header layout
        header_layout = QHBoxLayout()
        self.lbl_status = QLabel("Ready")
        btn_refresh = QPushButton("Refresh Scan")
        btn_refresh.clicked.connect(self.start_scan)
        header_layout.addWidget(self.lbl_status)
        header_layout.addStretch()
        header_layout.addWidget(btn_refresh)
        
        main_layout.addLayout(header_layout)
        
        # Splitter for Tree and Plot
        self.splitter = QSplitter(Qt.Horizontal)
        
        # Left Panel: Tree Widget
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Run Date", "Start Time", "Folder", "Condition", "Peaks Count"])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self.on_item_selected)
        
        # Right Panel: Plot Canvas
        self.plot_widget = QWidget()
        plot_layout = QVBoxLayout(self.plot_widget)
        
        # Plot controls
        controls_layout = QHBoxLayout()
        self.cb_log_y = QCheckBox("Log10 Y-Axis")
        self.cb_log_y.setChecked(False)
        self.cb_log_y.toggled.connect(self.replot)
        
        self.cb_auto_y = QCheckBox("Auto Y Limits")
        self.cb_auto_y.setChecked(True)
        self.cb_auto_y.toggled.connect(self.on_auto_y_toggled)
        
        self.cb_norm_width = QCheckBox("Normalize Widths")
        self.cb_norm_width.setChecked(True)
        self.cb_norm_width.toggled.connect(self.replot)
        
        self.lbl_min = QLabel("Min:")
        self.spin_min = QDoubleSpinBox()
        self.spin_min.setRange(0.0, 1000000)
        self.spin_min.setDecimals(4)
        self.spin_min.setValue(0.0)
        self.spin_min.setEnabled(False)
        self.spin_min.valueChanged.connect(self.replot)
        
        self.lbl_max = QLabel("Max:")
        self.spin_max = QDoubleSpinBox()
        self.spin_max.setRange(0.0001, 1000000)
        self.spin_max.setDecimals(4)
        self.spin_max.setValue(100.0)
        self.spin_max.setEnabled(False)
        self.spin_max.valueChanged.connect(self.replot)
        
        controls_layout.addWidget(self.cb_log_y)
        controls_layout.addWidget(self.cb_auto_y)
        controls_layout.addWidget(self.cb_norm_width)
        controls_layout.addWidget(self.lbl_min)
        controls_layout.addWidget(self.spin_min)
        controls_layout.addWidget(self.lbl_max)
        controls_layout.addWidget(self.spin_max)
        controls_layout.addStretch()
        
        plot_layout.addLayout(controls_layout)
        
        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        plot_layout.addWidget(self.canvas, 3)
        
        # Comparison Table
        self.stats_table = QTableWidget()
        self.stats_table.setRowCount(5)
        self.stats_table.setVerticalHeaderLabels(["Mean", "SD", "Q25", "Q50 (Med)", "Q75"])
        self.stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stats_table.verticalHeader().setVisible(True)
        self.stats_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stats_table.setMaximumHeight(200)
        plot_layout.addWidget(self.stats_table, 1)
        
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(self.plot_widget)
        
        # Set initial splitter sizes
        self.splitter.setSizes([500, 700])
        
        main_layout.addWidget(self.splitter)

    def start_scan(self):
        if not self.data_path:
            return
            
        self.lbl_status.setText("Scanning data directories...")
        self.tree.clear()
        
        self.scanner_thread = DataScannerThread(self.data_path, self.cache_path)
        self.scanner_thread.progress_update.connect(self.update_status)
        self.scanner_thread.finished_scan.connect(self.populate_tree)
        self.scanner_thread.error_scan.connect(self.scan_error)
        self.scanner_thread.start()

    @Slot(str)
    def update_status(self, msg):
        self.lbl_status.setText(msg)

    @Slot(str)
    def scan_error(self, err):
        self.lbl_status.setText("Scan Error")
        QMessageBox.critical(self, "Scan Error", f"An error occurred while scanning:\n{err}")

    @Slot(list)
    def populate_tree(self, data_list):
        self.lbl_status.setText("Scan Complete")
        self.all_data = data_list
        
        # Group by Folder
        grouped_data = {}
        for item in data_list:
            folder = item['Folder Name']
            if folder not in grouped_data:
                grouped_data[folder] = []
            grouped_data[folder].append(item)
            
        # Sort by date and time (descending)
        sorted_folders = sorted(grouped_data.keys(), reverse=True)
        
        for folder in sorted_folders:
            items = grouped_data[folder]
            first = items[0]
            
            parent_item = QTreeWidgetItem(self.tree)
            parent_item.setText(0, first['Run Date'])
            parent_item.setText(1, first['Start Time'])
            parent_item.setText(2, folder)
            parent_item.setText(3, "")
            parent_item.setText(4, "")
            
            # Store CSV path in user data of parent for plotting
            parent_item.setData(0, Qt.UserRole, first['CSV Path'])
            
            for sub in items:
                child_item = QTreeWidgetItem(parent_item)
                child_item.setText(0, "")
                child_item.setText(1, "")
                child_item.setText(2, "")
                child_item.setText(3, sub['Condition'])
                child_item.setText(4, str(sub['Peaks Count']))

        self.tree.expandAll()

    def on_item_selected(self):
        selected = self.tree.selectedItems()
        if not selected:
            return
            
        item = selected[0]
        # Get parent if it's a child
        if item.parent():
            item = item.parent()
            
        csv_path = item.data(0, Qt.UserRole)
        if csv_path and os.path.exists(csv_path):
            self.plot_data(csv_path, item.text(2))

    def plot_data(self, csv_path, title):
        self.current_csv_path = csv_path
        self.current_title = title
        self.replot()

    def on_auto_y_toggled(self, checked):
        self.spin_min.setEnabled(not checked)
        self.spin_max.setEnabled(not checked)
        self.replot()

    def replot(self):
        if not hasattr(self, 'current_csv_path') or not self.current_csv_path:
            return
            
        try:
            self.lbl_status.setText(f"Loading {self.current_title}...")
            
            analyzer = PostHocFrequencyAnalyzer()
            analyzer.load_uncalibrated_peaks(self.current_csv_path)
            
            df = analyzer.peaks_df
            if 'condition' not in df.columns:
                df = df.with_columns(pl.lit("Assumed Calibration").alias('condition'))
                
            pd_df = df.to_pandas()
            
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            
            if 'uncalibrated_mass_pg' in pd_df.columns:
                # Explicitly define condition order for both plot and table
                condition_order = pd_df['condition'].unique().tolist()
                
                # Generate pastel palette matching number of conditions
                palette = sns.color_palette("pastel", len(condition_order))
                color_map = {cond: palette[i] for i, cond in enumerate(condition_order)}
                
                # Use 'width' scaling if checked, otherwise 'area' (default)
                # Note: 'density_norm' is the newer name for 'scale' in seaborn >= 0.13.0
                scale_mode = 'width' if self.cb_norm_width.isChecked() else 'area'
                
                try:
                    sns.violinplot(data=pd_df, x='condition', y='uncalibrated_mass_pg', ax=ax, 
                                  inner="quartile", density_norm=scale_mode, width=0.9, 
                                  order=condition_order, palette=palette, hue='condition', legend=False)
                except (TypeError, ValueError):
                    # Fallback for older seaborn versions
                    sns.violinplot(data=pd_df, x='condition', y='uncalibrated_mass_pg', ax=ax, 
                                  inner="quartile", scale=scale_mode, width=0.9, 
                                  order=condition_order, palette=palette, hue='condition')
                
                ax.set_title(f"Uncalibrated Mass by Condition: {self.current_title}")
                ax.set_xlabel("Condition")
                ax.set_ylabel("Uncalibrated Mass (pg)")
                
                # Apply Log scale if checked
                if self.cb_log_y.isChecked():
                    if pd_df['uncalibrated_mass_pg'].min() > 0:
                        ax.set_yscale('log')
                
                import pandas as pd
                # Apply Manual Y limits if auto is unchecked
                if not self.cb_auto_y.isChecked():
                    ax.set_ylim(self.spin_min.value(), self.spin_max.value())
                else:
                    max_mass = pd_df['uncalibrated_mass_pg'].max()
                    y_max = min(100.0, float(max_mass)) if not pd.isna(max_mass) else 100.0
                    
                    if self.cb_log_y.isChecked():
                        min_y = max(0.0001, pd_df['uncalibrated_mass_pg'].min() * 0.5) if not pd.isna(pd_df['uncalibrated_mass_pg'].min()) else 0.0001
                        ax.set_ylim(min_y, max(y_max, min_y * 10))
                    else:
                        ax.set_ylim(0.0, max(y_max, 0.1))
                        
                    self.spin_min.blockSignals(True)
                    self.spin_max.blockSignals(True)
                    if self.cb_log_y.isChecked():
                        self.spin_min.setValue(min_y)
                    else:
                        self.spin_min.setValue(0.0)
                    self.spin_max.setValue(max(y_max, 0.1))
                    self.spin_min.blockSignals(False)
                    self.spin_max.blockSignals(False)
                    
                ax.tick_params(axis='x', rotation=45)
                self.figure.tight_layout()
                
                # Update Statistics Table
                self.update_stats_table(pd_df, condition_order, color_map)
            else:
                ax.text(0.5, 0.5, "uncalibrated_mass_pg column not found", ha='center', va='center')
                self.stats_table.setRowCount(0)
                
            self.canvas.draw()
            self.lbl_status.setText(f"Plot loaded for {self.current_title}")
            
        except Exception as e:
            self.lbl_status.setText("Plotting Error")
            print(f"Plotting error: {e}")
            QMessageBox.warning(self, "Plot Error", f"Could not plot data:\n{e}")

    def update_stats_table(self, df, condition_order, color_map=None):
        try:
            # Calculate summary statistics for each condition
            # Using groupby and agg for efficiency, then reindexing to match order
            stats_df = df.groupby('condition')['uncalibrated_mass_pg'].agg([
                ('mean', 'mean'),
                ('std', 'std'),
                ('q25', lambda x: x.quantile(0.25)),
                ('q50', 'median'),
                ('q75', lambda x: x.quantile(0.75))
            ]).reindex(condition_order).reset_index()
            
            # Row labels (already set in setup_ui but confirmed here)
            metrics = ["Mean", "SD", "Q25", "Q50 (Med)", "Q75"]
            
            self.stats_table.setRowCount(len(metrics))
            self.stats_table.setColumnCount(len(condition_order))
            self.stats_table.setHorizontalHeaderLabels(condition_order)
            self.stats_table.setVerticalHeaderLabels(metrics)
            
            for col_idx, condition in enumerate(condition_order):
                # Find the row in stats_df for this condition
                row_data = stats_df[stats_df['condition'] == condition]
                if row_data.empty:
                    continue
                
                row_data = row_data.iloc[0]
                
                # Get color for background
                qt_color = None
                if color_map and condition in color_map:
                    r, g, b = color_map[condition]
                    # Create a slightly transparent version for the table (alpha=100/255)
                    qt_color = QColor(int(r*255), int(g*255), int(b*255), 120)
                
                # Helper to handle NaN values gracefully
                def fmt(val):
                    return f"{val:.4f}" if pd.notnull(val) else "N/A"
                
                vals = [row_data['mean'], row_data['std'], row_data['q25'], row_data['q50'], row_data['q75']]
                for row_idx, val in enumerate(vals):
                    item = QTableWidgetItem(fmt(val))
                    if qt_color:
                        item.setBackground(qt_color)
                    self.stats_table.setItem(row_idx, col_idx, item)
                
        except Exception as e:
            print(f"Error updating stats table: {e}")
            self.stats_table.setColumnCount(0)

if __name__ == '__main__':
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    window = DataReviewViewer()
    window.show()
    sys.exit(app.exec())
