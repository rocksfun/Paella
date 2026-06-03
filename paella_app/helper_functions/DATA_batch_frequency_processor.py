import os
import sys
import time
import datetime
import traceback
import numpy as np
import polars as pl
from pathlib import Path
from collections import defaultdict
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox, QGroupBox, QDialog, QProgressBar,
    QListWidgetItem, QSpinBox, QCheckBox
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QRunnable, QThreadPool

# Add parent directory to path if running as script (for imports)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# Import Paella modules
try:
    from helper_functions.DATA_realtime_frequency_analysis import RealTimeFrequencyAnalyzer, PeakDetectionSettings
except ImportError:
    try:
        from DATA_realtime_frequency_analysis import RealTimeFrequencyAnalyzer, PeakDetectionSettings
    except ImportError as e:
        print(f"Error importing RealTimeFrequencyAnalyzer: {e}")
        pass

try:
    from helper_functions.DATA_posthoc_frequency_analysis import PostHocFrequencyAnalyzer
except ImportError:
    try:
        from DATA_posthoc_frequency_analysis import PostHocFrequencyAnalyzer
    except ImportError:
        pass

try:
    from helper_functions.AUX_frequency_binary_viewer import read_frequency_binary_file
except ImportError:
    try:
        from AUX_frequency_binary_viewer import read_frequency_binary_file
    except ImportError:
        pass

class FileIndexer:
    """Handles scanning sample folders and identifying relevant files grouped by system."""
    
    def __init__(self):
        # indexing[sample_folder][system_name][experiment_prefix] = {"a00_bin": [...], "peaks_csv": [...], ...}
        self.indexed_data = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
            "a00_bins": [],
            "metadata_txts": [],
            "flags_txts": [],
            "peaks_csvs": []
        })))
        self.folder_map = {} # sample_name -> folder_path
    
    def scan_folders(self, folder_paths):
        """Scans a list of folders and indexes the files."""
        self.indexed_data.clear()
        
        for folder_path in folder_paths:
            folder_name = os.path.basename(os.path.normpath(folder_path))
            self.folder_map[folder_name] = folder_path
            
            # Walk directory looking for specific file types
            for root, _, files in os.walk(folder_path):
                for file_name in files:
                    if file_name.startswith('.'):
                        continue  # Skip hidden files
                    
                    full_path = os.path.join(root, file_name)
                    
                    # Extract identifying info from prefix: "System_Timestamp_..."
                    parts = file_name.split('_')
                    if len(parts) >= 2:
                        system_name = parts[0]
                        timestamp = parts[1]
                        # Clean off extensions just in case the file name is short
                        timestamp = timestamp.split('.')[0] 
                        prefix = f"{system_name}_{timestamp}"
                    else:
                        continue # File doesn't match standard Paella naming convention
                    
                    # Sort files into categories
                    target_dict = self.indexed_data[folder_name][system_name][prefix]
                    
                    if file_name.endswith('_a00.bin'):
                        target_dict["a00_bins"].append(full_path)
                    elif file_name.endswith('_metadata.txt'):
                        target_dict["metadata_txts"].append(full_path)
                    elif file_name.endswith('_experiment_flags.txt'):
                        target_dict["flags_txts"].append(full_path)
                    elif file_name.endswith('_uncalibrated_peaks.csv'):
                        target_dict["peaks_csvs"].append(full_path)
                        
    def get_summary_data(self):
        """
        Returns a flattened list of dictionaries for table display.
        Each dictionary represents a system within a sample folder.
        """
        summary = []
        
        for sample_name, systems in self.indexed_data.items():
            for system_name, prefixes in systems.items():
                
                total_a00 = 0
                total_csvs = 0
                
                for prefix, file_dict in prefixes.items():
                    total_a00 += len(file_dict["a00_bins"])
                    total_csvs += len(file_dict["peaks_csvs"])
                
                # Only add if we found relevant files for this system
                if total_a00 > 0 or total_csvs > 0:
                    # Check for calibrated output file
                    sample_path = self.folder_map.get(sample_name, "")
                    if total_a00 == 1:
                        # Find the prefix for the single bin file
                        single_prefix = next((p for p, fd in prefixes.items() if len(fd["a00_bins"]) == 1), list(prefixes.keys())[0])
                        cal_file = os.path.join(sample_path, f"{single_prefix}_calibrated_peaks.csv")
                    elif total_a00 == 0 and total_csvs == 1:
                        # Find the prefix for the single CSV file
                        single_prefix = next((p for p, fd in prefixes.items() if len(fd["peaks_csvs"]) == 1), list(prefixes.keys())[0])
                        cal_file = os.path.join(sample_path, f"{single_prefix}_calibrated_peaks.csv")
                    else:
                        cal_file = os.path.join(sample_path, f"{sample_name}_{system_name}_combined_calibrated_peaks.csv")
                    
                    is_calibrated = os.path.exists(cal_file)
                    
                    summary.append({
                        "Sample Name": sample_name,
                        "System Name": system_name,
                        "Bin Files": total_a00,
                        "CSV Files": total_csvs,
                        "Calibrated": is_calibrated
                    })
                    
        return summary


class ProgressDialog(QDialog):
    """Dialog to display progress of distinct reprocessing tasks."""
    def __init__(self, parent=None, tasks=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Reprocessing Progress")
        self.resize(700, 500)
        self.setModal(True)
        
        self.progress_bars = {} # dict mapping task_id -> QProgressBar
        self.total_tasks = len(tasks) if tasks else 0
        self.completed_tasks = 0
        
        layout = QVBoxLayout(self)
        
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        
        if tasks:
            for task in tasks:
                task_id = task['id']
                task_desc = task['desc']
                
                item = QListWidgetItem()
                
                widget = QWidget()
                widget_layout = QHBoxLayout(widget)
                widget_layout.setContentsMargins(5, 2, 5, 2)
                
                label = QLabel(task_desc)
                label.setMinimumWidth(350)
                label.setWordWrap(True)
                
                progress_bar = QProgressBar()
                progress_bar.setRange(0, 100)
                progress_bar.setValue(0)
                
                widget_layout.addWidget(label)
                widget_layout.addWidget(progress_bar)
                
                # Sizing trick to ensure widgets fit list items properly
                widget.setLayout(widget_layout)
                item.setSizeHint(widget.sizeHint())
                
                self.list_widget.addItem(item)
                self.list_widget.setItemWidget(item, widget)
                
                self.progress_bars[task_id] = progress_bar
                
        self.global_label = QLabel(f"0 of {self.total_tasks} tasks complete")
        self.global_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
        layout.addWidget(self.global_label)
        
        self.global_progress = QProgressBar()
        self.global_progress.setRange(0, self.total_tasks)
        self.global_progress.setValue(0)
        layout.addWidget(self.global_progress)
        
        # Bottom controls
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_close = QPushButton("Cancel")
        self.btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)
        
    def update_task_progress(self, task_id, percent):
        """Update individual task progress bar."""
        if task_id in self.progress_bars:
            self.progress_bars[task_id].setValue(int(percent))
            
    def mark_task_complete(self, task_id):
        """Mark task 100% and update global counter."""
        if task_id in self.progress_bars:
            self.progress_bars[task_id].setValue(100)
            
        self.completed_tasks += 1
        self.global_label.setText(f"{self.completed_tasks} of {self.total_tasks} tasks complete")
        self.global_progress.setValue(self.completed_tasks)
        
        if self.completed_tasks >= self.total_tasks:
            self.btn_close.setText("Close")
            
    def display_error(self, task_id, err_msg):
        """Visually indicate a task crashed."""
        if task_id in self.progress_bars:
            bar = self.progress_bars[task_id]
            bar.setFormat(f"Error: {err_msg}")
            bar.setStyleSheet("QProgressBar::chunk {background-color: red;}")
        else:
            QMessageBox.critical(self, "Worker Error", err_msg)


class ReprocessSignals(QObject):
    """Defines the signals available from a running worker thread."""
    progress = Signal(str, float) # task_id, percentage
    completed = Signal(str) # task_id
    error = Signal(str, str) # task_id, error message
    all_completed = Signal()


class ReprocessWorker(QRunnable):
    """
    Worker thread that iterates through all tasks in the background.
    Tasks can be of type 'bin' (generate peaks) or 'calibrate' (concat and analyze).
    """
    def __init__(self, tasks, indexer_data, max_threads=4, use_drift_correction=False):
        super().__init__()
        self.tasks = tasks 
        self.indexer_data = indexer_data
        self.max_threads = max_threads
        self.use_drift_correction = use_drift_correction
        self.signals = ReprocessSignals()
        self.is_cancelled = False

    def run(self):
        try:
            import concurrent.futures
            from collections import defaultdict
            
            # Group tasks by combo (Sample_System) to manage dependencies
            combo_to_bins = defaultdict(list)
            combo_to_cal = {}
            for task in self.tasks:
                if task['type'] == 'bin':
                    combo = (task['sample_name'], task['system_name'])
                    combo_to_bins[combo].append(task)
                elif task['type'] == 'calibrate':
                    combo = (task['sample_name'], task['system_name'])
                    combo_to_cal[combo] = task
                    
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                # Maintain futures
                active_futures = {}  # future -> task
                combo_futures = defaultdict(list)
                completed_combos = set()
                
                # Submit all bin tasks
                for combo, bin_tasks in combo_to_bins.items():
                    for bin_task in bin_tasks:
                        future = executor.submit(self._process_bin, bin_task, bin_task['id'])
                        active_futures[future] = bin_task
                        combo_futures[combo].append(future)

                # If there are NO bin tasks but calibration tasks exist, submit them early
                for combo in list(combo_to_cal.keys()):
                    if combo not in combo_to_bins or not combo_to_bins[combo]:
                        cal_task = combo_to_cal[combo]
                        future = executor.submit(self._process_calibrate, cal_task, cal_task['id'])
                        active_futures[future] = cal_task
                        completed_combos.add(combo)

                # Wait loop
                while active_futures and not self.is_cancelled:
                    # Wait for at least one future to complete
                    done, _ = concurrent.futures.wait(
                        active_futures.keys(), 
                        return_when=concurrent.futures.FIRST_COMPLETED,
                        timeout=0.5
                    )
                    
                    if self.is_cancelled:
                        break
                        
                    for future in done:
                        task = active_futures.pop(future)
                        # Check exceptions
                        try:
                            future.result()
                            self.signals.completed.emit(task['id'])
                        except Exception as e:
                            self.signals.error.emit(task['id'], f"Task Failed: {str(e)}\n{traceback.format_exc()}")
                            
                        # If bin task, check if combo is fully complete and submit cal task
                        if task['type'] == 'bin':
                            combo = (task['sample_name'], task['system_name'])
                            if combo not in completed_combos and combo in combo_to_cal:
                                if all(f.done() for f in combo_futures[combo]):
                                    completed_combos.add(combo)
                                    cal_task = combo_to_cal[combo]
                                    cal_future = executor.submit(self._process_calibrate, cal_task, cal_task['id'])
                                    active_futures[cal_future] = cal_task

        except Exception as e:
            self.signals.error.emit("Global", f"Worker crashed: {str(e)}\n{traceback.format_exc()}")
        finally:
            self.signals.all_completed.emit()
            
    def _process_bin(self, task, task_id):
        bin_path = task['filepath']
        try:
            # 1. Setup PostHocFrequencyAnalyzer
            ph_analyzer = PostHocFrequencyAnalyzer()
            
            # Progress bridging
            def progress_callback(pct):
                self.signals.progress.emit(task_id, pct * 0.95) # Save 5% for save/finish
                
            ph_analyzer.progress_callback = progress_callback
            
            # 2. Execute full-binary reprocessing and advanced feature derivation
            # This unified engine handles chunked reading and R-parity statistics.
            dir_name = os.path.dirname(bin_path)
            prefix = os.path.basename(bin_path).replace('_a00.bin', '')
            flags_file = os.path.join(dir_name, f"{prefix}_experiment_flags.txt")
            
            print(f"Batch reprocessing bin: {bin_path}...")
            ph_analyzer.reprocess_binary_file(bin_path, flags_file)
            
            if not hasattr(ph_analyzer, 'peaks_df') or ph_analyzer.peaks_df is None:
                 print(f"Warning: No peaks detected in {bin_path}")
                 self.signals.progress.emit(task_id, 100.0)
                 return
                 
            # 3. Save the final uncalibrated subset data
            csv_filename = os.path.join(dir_name, f"{prefix}_uncalibrated_peaks.csv")
            ph_analyzer.peaks_df.write_csv(csv_filename)
                    
            self.signals.progress.emit(task_id, 100.0)
            
        except Exception as e:
            self.signals.error.emit(task_id, f"Bin error: {str(e)}")

    def _process_calibrate(self, task, task_id):
        sample_name = task['sample_name']
        system_name = task['system_name']
        csv_files = task['csv_files'] # Expected to be populated correctly in main thread
        target_dir = task['dir']
        
        if not csv_files: return
        self.signals.progress.emit(task_id, 5.0)
        
        try:
            dfs = []
            for csv_path in csv_files:
                if self.is_cancelled: return
                if not os.path.exists(csv_path): continue
                
                df = pl.read_csv(csv_path)
                
                # Identify prefix for this CSV and append it
                basename = os.path.basename(csv_path)
                parts = basename.split('_')
                if len(parts) >= 2:
                    prefix = f"{parts[0]}_{parts[1]}"
                else:
                    prefix = os.path.splitext(basename)[0]
                    
                df = df.with_columns(pl.lit(prefix).alias('experiment_prefix'))
                dfs.append(df)
                
            if not dfs: return
            
            self.signals.progress.emit(task_id, 20.0)
            combined_df = pl.concat(dfs)
            
            # Write a temporary concatenated file since PostHocAnalyzer expects a file path
            temp_combined = os.path.join(target_dir, f"{sample_name}_{system_name}_TEMP.csv")
            combined_df.write_csv(temp_combined)
            
            self.signals.progress.emit(task_id, 40.0)
            
            # Init PostHocAnalyzer and load the temporary file
            ph_analyzer = PostHocFrequencyAnalyzer(calibration_bead_diameter=7.008)
            ph_analyzer.load_uncalibrated_peaks(temp_combined)
            
            self.signals.progress.emit(task_id, 60.0)
            
            # Filter mathematically
            ph_analyzer.filter_beads_mahalanobis()
            self.signals.progress.emit(task_id, 75.0)
            
            # Derive drift slopes and offset multipliers
            ph_analyzer.calibrate_sensors(use_drift_correction=self.use_drift_correction)
            self.signals.progress.emit(task_id, 90.0)
            
            # Save final results and delete temp
            if len(csv_files) == 1:
                basename = os.path.basename(csv_files[0])
                parts = basename.split('_')
                if len(parts) >= 2:
                    single_prefix = f"{parts[0]}_{parts[1]}"
                else:
                    single_prefix = os.path.splitext(basename)[0].replace('_uncalibrated_peaks', '')
                final_csv = os.path.join(target_dir, f"{single_prefix}_calibrated_peaks.csv")
                meta_txt = os.path.join(target_dir, f"{single_prefix}_calibration_meta.txt")
            else:
                final_csv = os.path.join(target_dir, f"{sample_name}_{system_name}_combined_calibrated_peaks.csv")
                meta_txt = os.path.join(target_dir, f"{sample_name}_{system_name}_combined_calibration_meta.txt")
                
            ph_analyzer.save_results(final_csv)
            
            if os.path.exists(temp_combined):
                os.remove(temp_combined)
                
            # Create Metadata details
            total_beads_raw = len(combined_df.filter(pl.col('condition').fill_null('').str.to_lowercase().str.contains('calibration')))
            
            filtered_beads = len(ph_analyzer.filtered_beads_df) if hasattr(ph_analyzer, 'filtered_beads_df') else total_beads_raw
            sens = ph_analyzer.calibration_sens if hasattr(ph_analyzer, 'calibration_sens') else 1.0
            with open(meta_txt, 'w') as f:
                f.write(f"Posthoc Analysis Date: {datetime.datetime.now().strftime('%Y-%m-%d')}\n")
                f.write(f"Posthoc Analysis Time: {datetime.datetime.now().strftime('%H:%M:%S')}\n")
                f.write("Input Files Combined:\n")
                for c in csv_files:
                    f.write(f"  - {os.path.basename(c)}\n")
                    
                f.write(f"\nBead Peaks Detected (Raw): {total_beads_raw}\n")
                if getattr(ph_analyzer, 'assumed_calibration', False):
                    f.write(f"Assumed Calibration: True (no 'Calibration' flag found, mapping assumed start of file as Calibration)\n")
                
                f.write(f"Beads Remaining post-filter: {filtered_beads}\n")
                f.write(f"Bead Diameter Used: 7.008 um\n")
                f.write(f"System Baseline Sensitivity: {sens:.5f} Hz/pg\n")
                
                # Drift details
                drift_status = "Enabled" if self.use_drift_correction else "Disabled"
                f.write(f"Drift Correction: {drift_status}\n")
                if self.use_drift_correction:
                    slope = ph_analyzer.calibration_slope if hasattr(ph_analyzer, 'calibration_slope') else 0.0
                    f.write(f"Calculated Drift Slope: {slope:.5e} (mass shift / sec)\n")
            
            self.signals.progress.emit(task_id, 100.0)
            
        except Exception as e:
            self.signals.error.emit(task_id, f"Calibration error: {str(e)}")


class BatchProcessorWindow(QMainWindow):
    """Main GUI window for the Batch Frequency Processor."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Batch Frequency Processor")
        self.resize(800, 600)
        
        self.indexer = FileIndexer()
        self.sample_folders = set() # Storing paths as a set prevents duplicates
        
        self._init_ui()
        
    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # --- Top Section: Folder Selection ---
        folder_group = QGroupBox("Sample Folders")
        folder_layout = QVBoxLayout()
        
        button_layout = QHBoxLayout()
        self.btn_add_folder = QPushButton("Add Folder...")
        self.btn_add_folder.clicked.connect(self.add_folder)
        
        self.btn_clear_folders = QPushButton("Clear Folders")
        self.btn_clear_folders.clicked.connect(self.clear_folders)
        
        button_layout.addWidget(self.btn_add_folder)
        button_layout.addWidget(self.btn_clear_folders)
        button_layout.addStretch()
        folder_layout.addLayout(button_layout)
        
        self.list_folders = QListWidget()
        folder_layout.addWidget(self.list_folders)
        
        self.btn_run_indexer = QPushButton("Refresh File Index")
        self.btn_run_indexer.clicked.connect(self.run_indexer)
        self.btn_run_indexer.setStyleSheet("font-weight: bold; padding: 5px;")
        folder_layout.addWidget(self.btn_run_indexer)
        
        folder_group.setLayout(folder_layout)
        main_layout.addWidget(folder_group, 1)
        
        # --- Bottom Section: Data Table ---
        data_group = QGroupBox("Indexed Data")
        data_layout = QVBoxLayout()
        
        # Selection buttons and options
        selection_layout = QHBoxLayout()
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.clicked.connect(self.select_all)
        self.btn_deselect_all = QPushButton("Deselect All")
        self.btn_deselect_all.clicked.connect(self.deselect_all)
        
        selection_layout.addWidget(self.btn_select_all)
        selection_layout.addWidget(self.btn_deselect_all)
        selection_layout.addStretch()
        
        # Drift Correction Toggle
        self.chk_drift = QCheckBox("Use Drift Correction")
        self.chk_drift.setChecked(False)
        self.chk_drift.setToolTip("Enable to correct for media density / temperature drift over time. Recommended if beads were run at both start and end.")
        
        lbl_threads = QLabel("Max Threads:")
        lbl_threads.setStyleSheet("font-weight: bold;")
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 64)
        default_cores = os.cpu_count()
        self.spin_threads.setValue(max(1, min(default_cores or 4, 16)))
        
        self.btn_reprocess = QPushButton("Reprocess frequency data")
        self.btn_reprocess.setStyleSheet("font-weight: bold; padding: 5px; background-color: #2E8B57; color: white;")
        self.btn_reprocess.clicked.connect(self.start_reprocessing)
        
        selection_layout.addWidget(self.chk_drift)
        selection_layout.addSpacing(10)
        selection_layout.addWidget(lbl_threads)
        selection_layout.addWidget(self.spin_threads)
        selection_layout.addWidget(self.btn_reprocess)
        data_layout.addLayout(selection_layout)
        
        self.table_results = QTableWidget()
        self.table_results.setColumnCount(6)
        self.table_results.setHorizontalHeaderLabels([
            "Process", "Sample Name", "System Name", "# of _a00.bin", "# of uncalibrated_peaks.csv", "Calibrated?"
        ])
        # Make the table fill horizontal space
        header = self.table_results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table_results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_results.setSelectionBehavior(QTableWidget.SelectRows)
        
        data_layout.addWidget(self.table_results)
        data_group.setLayout(data_layout)
        main_layout.addWidget(data_group, 2)
        
        self.statusBar().showMessage("Ready.")
        
    def add_folder(self):
        """Open a dialog to add multiple directory paths."""
        dialog = QFileDialog(self, "Select Sample Folders")
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        
        # This is required on some platforms to allow multiple directory selection
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        
        from PySide6.QtWidgets import QListView, QTreeView
        
        # Override the native behavior by connecting to the internal tree view
        # PyQt6 / PySide6 specific hack to allow multiple directory selection
        file_view = dialog.findChild(QListView, "listView")
        tree_view = dialog.findChild(QTreeView, "treeView")
        
        if file_view: file_view.setSelectionMode(QListView.ExtendedSelection)
        if tree_view: tree_view.setSelectionMode(QTreeView.ExtendedSelection)

        if dialog.exec() == QFileDialog.Accepted:
            selected_folders = dialog.selectedFiles()
            added_any = False
            for folder_path in selected_folders:
                if folder_path and folder_path not in self.sample_folders:
                    self.sample_folders.add(folder_path)
                    self.list_folders.addItem(folder_path)
                    added_any = True
            
            if added_any:
                self.run_indexer()
            else:
                if selected_folders: # Only show message if they actually selected something that was already present
                    QMessageBox.information(self, "Folders Already Added", "The selected folders are already in the list.")
                
    def clear_folders(self):
        """Clear all selected folders and empty the table."""
        self.sample_folders.clear()
        self.list_folders.clear()
        self.indexer.indexed_data.clear()
        self.refresh_table()
        self.statusBar().showMessage("Folders cleared.")
        
    def run_indexer(self):
        """Scan the folders and update the table with identified files."""
        if not self.sample_folders:
            self.statusBar().showMessage("No folders selected to index.")
            return
            
        self.statusBar().showMessage("Scanning folders...")
        QApplication.processEvents()
        
        self.indexer.scan_folders(list(self.sample_folders))
        self.refresh_table()
        
        self.statusBar().showMessage(f"Indexed {len(self.sample_folders)} folder(s) successfully.")
        
    def refresh_table(self):
        """Update the QTableWidget with data from the FileIndexer."""
        self.table_results.setRowCount(0)
        summary_data = self.indexer.get_summary_data()
        
        for row_idx, row_data in enumerate(summary_data):
            self.table_results.insertRow(row_idx)
            
            # Checkbox item: Default to unchecked if already calibrated
            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if row_data.get("Calibrated", False):
                checkbox_item.setCheckState(Qt.Unchecked)
            else:
                checkbox_item.setCheckState(Qt.Checked)
            self.table_results.setItem(row_idx, 0, checkbox_item)
            
            self.table_results.setItem(row_idx, 1, QTableWidgetItem(str(row_data["Sample Name"])))
            self.table_results.setItem(row_idx, 2, QTableWidgetItem(str(row_data["System Name"])))
            
            # Align number columns to center
            bin_item = QTableWidgetItem(str(row_data["Bin Files"]))
            bin_item.setTextAlignment(Qt.AlignCenter)
            self.table_results.setItem(row_idx, 3, bin_item)
            
            csv_item = QTableWidgetItem(str(row_data["CSV Files"]))
            csv_item.setTextAlignment(Qt.AlignCenter)
            self.table_results.setItem(row_idx, 4, csv_item)
            
            # Calibrated Status
            cal_status = "Yes" if row_data["Calibrated"] else "No"
            cal_item = QTableWidgetItem(cal_status)
            cal_item.setTextAlignment(Qt.AlignCenter)
            if row_data["Calibrated"]:
                cal_item.setForeground(Qt.darkGreen)
            else:
                cal_item.setForeground(Qt.red)
            self.table_results.setItem(row_idx, 5, cal_item)

    def select_all(self):
        """Check all rows in the table."""
        for row in range(self.table_results.rowCount()):
            item = self.table_results.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.Checked)
                
    def deselect_all(self):
        """Uncheck all rows in the table."""
        for row in range(self.table_results.rowCount()):
            item = self.table_results.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.Unchecked)

    def start_reprocessing(self):
        """Fetch tasks from checked rows and execute the worker."""
        tasks = []
        summary_data = self.indexer.get_summary_data()
        
        for row in range(self.table_results.rowCount()):
            checkbox = self.table_results.item(row, 0)
            if checkbox and checkbox.checkState() == Qt.Checked:
                sample_name = self.table_results.item(row, 1).text()
                system_name = self.table_results.item(row, 2).text()
                
                # Fetch paths from indexer
                system_dict = self.indexer.indexed_data.get(sample_name, {}).get(system_name, {})
                expected_csvs = []
                target_dir = ""
                
                # Queue up bin tasks
                for prefix, filegroups in system_dict.items():
                    for bin_file in filegroups['a00_bins']:
                        target_dir = os.path.dirname(bin_file)
                        task_id = f"bin_{sample_name}_{system_name}_{prefix}_{os.path.basename(bin_file)}"
                        task_desc = f"Generate Peaks: {os.path.basename(bin_file)}"
                        tasks.append({
                            'id': task_id,
                            'type': 'bin',
                            'desc': task_desc,
                            'filepath': bin_file,
                            'sample_name': sample_name,
                            'system_name': system_name
                        })
                        expected_csv_path = os.path.join(target_dir, f"{prefix}_uncalibrated_peaks.csv")
                        expected_csvs.append(expected_csv_path)
                
                # Queue up final calibration task for this row
                if expected_csvs:
                    task_id = f"cal_{sample_name}_{system_name}"
                    task_desc = f"Calibrate & Merge: {sample_name} - {system_name}"
                    tasks.append({
                        'id': task_id,
                        'type': 'calibrate',
                        'desc': task_desc,
                        'sample_name': sample_name,
                        'system_name': system_name,
                        'csv_files': expected_csvs,
                        'dir': target_dir
                    })
                    
        if not tasks:
            QMessageBox.information(self, "No Tasks", "Please select at least one valid row to process.")
            return
            
        # UI lock
        self.btn_reprocess.setEnabled(False)
        self.btn_run_indexer.setEnabled(False)
        self.table_results.setEnabled(False)
        
        # Show Progress UI
        self.progress_dialog = ProgressDialog(self, tasks)
        self.progress_dialog.rejected.connect(self._cancel_worker)
        self.progress_dialog.show()
        
        # Create Worker and configure signals
        # Create worker
        self.worker = ReprocessWorker(
            tasks, 
            self.indexer.indexed_data, 
            max_threads=self.spin_threads.value(),
            use_drift_correction=self.chk_drift.isChecked()
        )
        self.worker.signals.progress.connect(self.progress_dialog.update_task_progress)
        self.worker.signals.completed.connect(self.progress_dialog.mark_task_complete)
        self.worker.signals.error.connect(self.progress_dialog.display_error)
        self.worker.signals.all_completed.connect(self._on_worker_finished)
        
        QThreadPool.globalInstance().start(self.worker)
        
    def _cancel_worker(self):
        """Flag worker to abort cleanly."""
        if hasattr(self, 'worker') and self.worker:
            self.worker.is_cancelled = True
            
    def _on_worker_finished(self):
        """Unlock UI when done."""
        self.btn_reprocess.setEnabled(True)
        self.btn_run_indexer.setEnabled(True)
        self.table_results.setEnabled(True)
        # Refresh indexing so table CSV counts update to include new freshly generated calibrated/uncalibrated files natively
        self.run_indexer()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BatchProcessorWindow()
    window.show()
    sys.exit(app.exec())
