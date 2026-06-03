import os
import argparse
import sys
import numpy as np
import polars as pl
import concurrent.futures
import traceback

try:
    from scipy.optimize import minimize
    from scipy.stats import chi2
    import scipy.signal as signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("Warning: scipy required for calibration. Certain functions may fail.")

import glob

try:
    from sklearn.covariance import MinCovDet
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

from helper_functions.DATA_realtime_frequency_analysis import RealTimeFrequencyAnalyzer, PeakDetectionSettings
from helper_functions.SMR_frequency_processing_functions import (
    read_single_data, get_all_peaks, DataStructure, 
    DetectionSettings, FilterSettings, EstimationSettings, PeakPair
)

class PostHocFrequencyAnalyzer:
    """
    Standalone post-hoc frequency analysis component.
    Processes `_uncalibrated_peaks.csv` to apply Mahalanobis filtering,
    drift correction, sensor calibration, and yield calibrated mass data.
    """
    
    def __init__(self, calibration_bead_diameter=7.008, media_density=1.003, progress_callback=None):
        self.calibration_bead_diameter = calibration_bead_diameter  # microns
        self.media_density = media_density # g/cm^3
        self.bead_density = 1.05 # polystyrene g/cm^3
        self.progress_callback = progress_callback
        self.settings = None # Populated during process_experiment or reprocess_binary_file
        self.assumed_calibration = False
        
        # Calculate expected bead mass
        bead_volume_um3 = (4/3) * np.pi * (self.calibration_bead_diameter / 2) ** 3
        # Volume in cm^3 = um^3 * 1e-12
        bead_volume_cm3 = bead_volume_um3 * 1e-12
        # Mass in pg = volume_cm3 * density(g/cm^3) * 1e12 = volume_um3 * density
        # Buoyant mass = volume_um3 * (bead_density - media_density)
        self.bead_volume_um3 = bead_volume_um3
        self.expected_bead_mass_pg = bead_volume_um3 * (self.bead_density - self.media_density)
        
        # Chunk statistics for metadata reporting
        self.total_chunks = 0
        self.empty_chunks = 0
        self.is_cancelled = False # Flag for graceful termination
        
    def _report_progress(self, value, message=""):
        """Robustly call progress callback, handling both 1 and 2 argument signatures."""
        if self.progress_callback:
            try:
                # Try 2-argument signature (value, message) used by pySMR
                self.progress_callback(value, message)
            except TypeError:
                try:
                    # Fallback to 1-argument signature (value) used by BatchProcessor
                    self.progress_callback(value)
                except Exception as e:
                    print(f"Warning: Progress callback failed: {e}")
                    
    def load_uncalibrated_peaks(self, filepath):
        """Load _uncalibrated_peaks.csv using polars."""
        # Natively multithreaded fast read, casts columns automatically where possible
        self.peaks_df = pl.read_csv(filepath)
        
        # Robust column mapping for backward compatibility
        column_mapping = {
            'approximate_mass_pg': 'uncalibrated_mass_pg',
            'peak_time': 'relative_time'
        }
        for old, new in column_mapping.items():
            if old in self.peaks_df.columns and new not in self.peaks_df.columns:
                print(f"Mapping legacy column {old} -> {new}")
                self.peaks_df = self.peaks_df.rename({old: new})
        
        # Ensure we drop rows where critical columns have missing values (null)
        # Use whichever names are present after mapping
        critical_cols = [c for c in ['relative_time', 'uncalibrated_mass_pg'] if c in self.peaks_df.columns]
        if critical_cols:
            self.peaks_df = self.peaks_df.drop_nulls(subset=critical_cols)
        
        # Fallback for height_diff_percent if missing (for slimmed-down real-time CSVs)
        if 'height_diff_percent' not in self.peaks_df.columns:
            if 'peak1_delta_hz' in self.peaks_df.columns and 'peak2_delta_hz' in self.peaks_df.columns:
                print("Calculating missing height_diff_percent from frequency deltas...")
                self.peaks_df = self.peaks_df.with_columns(
                    ( (pl.col('peak1_delta_hz').abs() - pl.col('peak2_delta_hz').abs()).abs() / 
                      pl.col('peak1_delta_hz').abs() * 100.0 ).alias('height_diff_percent')
                )
            else:
                # Last resort fallback if deltas are missing too
                self.peaks_df = self.peaks_df.with_columns(
                    pl.lit(0.0).alias('height_diff_percent')
                )
        return self.peaks_df
        
    def reprocess_binary_file(self, binary_filepath, experiment_flags_path=None, settings=None):
        """
        Efficiently reprocesses the entire binary data file in chunks.
        Calculates advanced statistics (baseline noise, peak noise, node deviation) 
        for parity with R analysis.
        """
        if not binary_filepath or not os.path.exists(binary_filepath):
            print(f"Warning: Binary file not found. Skipping offline reprocessing.")
            return None
            
        print(f"Reprocessing full binary file: {os.path.basename(binary_filepath)}...")
        
        packet_size_i32 = 130
        scaling_factor = 12.5e6 / (2**32)
        data_rate = getattr(self, 'data_rate', 20000.0)
        time_step = 1.0 / data_rate
        
        # Settings for R-parity analysis
        det_settings = DetectionSettings() # Default R-parity settings (-4.0 Hz)
        filt_settings = FilterSettings()
        est_settings = EstimationSettings()
        
        # Sync with GUI settings if available (self.settings or passed settings)
        actual_settings = settings or self.settings
        if actual_settings:
            det_settings.detection_threshold = actual_settings.detection_threshold
            det_settings.t_doublet_gap_min = actual_settings.doublet_gap_min
            det_settings.t_doublet_gap_max = actual_settings.doublet_gap_max
            det_settings.max_height_diff_percent = actual_settings.max_percent_diff
            det_settings.max_height_diff_abs = getattr(actual_settings, 'max_height_diff', 10.0)
            filt_settings.sg_length = actual_settings.filter_width
            
        seen_peak_indices = set()
        all_detected_chunks = []
        
        try:
            mmap_array = np.memmap(binary_filepath, dtype='>i4', mode='r')
            num_packets = len(mmap_array) // packet_size_i32
            if num_packets == 0:
                print(f"Warning: Binary file {binary_filepath} is empty.")
                return None
                
            # Processing in chunks with overlap
            # 20k packets (~2.5M samples) is a safe memory footprint
            packets_per_chunk = 20000
            overlap_packets = 1000 # 1000 packets overlap to catch doublets spanning chunks
            
            self.total_chunks = 0
            self.empty_chunks = 0
            
            for start_pkt in range(0, num_packets, packets_per_chunk - overlap_packets):
                if self.is_cancelled:
                    print("Reprocessing cancelled by user.")
                    return None
                    
                self.total_chunks += 1
                end_pkt = min(num_packets, start_pkt + packets_per_chunk)
                
                # Extract chunk
                chunk_packets = mmap_array[start_pkt * packet_size_i32 : end_pkt * packet_size_i32].reshape((-1, packet_size_i32))
                num_in_chunk = len(chunk_packets)
                
                if num_in_chunk < 10:
                    continue
                
                # Reconstruct chunk data (Vectorized for performance)
                # Binary format: [timestamp_delta_scaled, packet_number, 128 frequencies]
                timestamp_deltas_sec = chunk_packets[:, 0] / 65536.0
                pnums_arr = chunk_packets[:, 1]
                freqs_i32 = chunk_packets[:, 2:130]
                
                # Each packet has 128 samples. The timestamp in the packet is for the LAST sample.
                # We calculate the time for each of the 128 samples by subtracting offsets.
                k_offsets = (127 - np.arange(128)) * time_step
                
                # Vectorized broadcasting: (N, 1) - (128,) -> (N, 128) -> (N*128,)
                all_times = (timestamp_deltas_sec[:, np.newaxis] - k_offsets).flatten()
                all_freqs = (freqs_i32.astype(np.float64) * scaling_factor).flatten()
                all_pnums = np.repeat(pnums_arr, 128)
                
                # Create DataStructure and detect peaks using R-parity logic
                try:
                    # Pass tuple of arrays directly for memory efficiency
                    data_struct = read_single_data(
                        (all_times, all_freqs), 
                        data_rate=data_rate,
                        apply_filtering=True,
                        filter_settings=filt_settings,
                        estimation_settings=est_settings
                    )
                    
                    # Store packet numbers in data_struct for PeakPair mapping
                    data_struct.packet_numbers = all_pnums
                    data_struct.relative_times = all_times
                    # Detect peaks using R-parity logic
                    peaks_struct = get_all_peaks(
                        data_struct, 
                        detection_settings=det_settings, 
                        estimation_settings=est_settings,
                        suppress_warnings=True # Suppress "no peaks" console noise during full-scan
                    )
                    
                    if len(peaks_struct.peak_pairs) == 0:
                        self.empty_chunks += 1
                    
                    # Process pairs
                    chunk_detected_pairs = []
                    for pair in peaks_struct.peak_pairs:
                        # Use absolute index in binary file as unique ID
                        # (start_pkt * 128) is the global sample offset of this chunk
                        abs_p1_idx = (start_pkt * 128) + pair.peak1_idx
                        
                        if abs_p1_idx not in seen_peak_indices:
                            seen_peak_indices.add(abs_p1_idx)
                            
                            # Calculate height_diff_percent for Mahalanobis
                            h1 = abs(pair.peak1_deviation)
                            h2 = abs(pair.peak2_deviation)
                            h_max = max(h1, h2)
                            h_diff_pct = (abs(h1 - h2) / h_max * 100.0) if h_max > 0 else 0.0
                            
                            # Collect values as a tuple to avoid dict overhead in hot loop
                            chunk_detected_pairs.append((
                                pair.peak1_time,
                                int(all_pnums[pair.peak1_idx]),
                                pair.peak1_deviation,
                                pair.peak2_deviation,
                                h_diff_pct,
                                pair.separation_time * 1000.0,
                                pair.baseline_noise,
                                pair.peak_noise,
                                pair.node_dev,
                                pair.antinode_diff_raw,
                                pair.mass_raw1,
                                pair.mass_raw2,
                                pair.mass_raw, # uncalibrated
                                pair.mass_raw, # placeholder
                                pair.baseline,
                                pair.baseline_slope
                            ))
                    
                    if chunk_detected_pairs:
                        # Define columns for this chunk
                        cols = [
                            'relative_time', 'packet_number', 'peak1_delta_hz', 'peak2_delta_hz',
                            'height_diff_percent', 'peak_width_ms', 'baseline_noise', 'peak_noise',
                            'node_dev', 'antinode_diff_raw', 'mass_raw1', 'mass_raw2',
                            'uncalibrated_mass_pg', 'calibrated_mass_pg', 'baseline_hz', 'baseline_slope_hz_s'
                        ]
                        all_detected_chunks.append(pl.DataFrame(chunk_detected_pairs, schema=cols, orient='row'))
                            
                except Exception as e:
                    print(f"Error processing chunk starting at packet {start_pkt}: {e}")
                    traceback.print_exc()

                # Update progress
                if (end_pkt % 1000 == 0) or (end_pkt == num_packets): # Reduce callback frequency
                    progress_pct = int((end_pkt / num_packets) * 100)
                    self._report_progress(progress_pct, f"Reprocessing binary blocks... ({progress_pct}%)")
                
                # Yield Python GIL to main thread to prevent UI freezing during intense math loop
                import time
                time.sleep(0.001)
            
            print(f"Full reprocessing complete. Detected {len(all_detected_chunks)} chunks with peaks.")
            
            if not all_detected_chunks:
                return None
            
            # Create DataFrame by concatenating all chunks
            new_df = pl.concat(all_detected_chunks)
            total_peaks = new_df.height
            print(f"Total peaks detected: {total_peaks}")
            
            # Map condition strings based on experiment_flags.txt
            if experiment_flags_path and os.path.exists(experiment_flags_path):
                try:
                    flags_df = pl.read_csv(experiment_flags_path, separator='\t')
                    time_col = 'Datetime' if 'Datetime' in flags_df.columns else 'elapsed_time'
                    cond_col = 'Flag ID' if 'Flag ID' in flags_df.columns else 'condition_name'
                    
                    if time_col in flags_df.columns and cond_col in flags_df.columns:
                        flags_df = flags_df.sort(time_col)
                        flag_times = flags_df[time_col].to_numpy()
                        flag_conds = flags_df[cond_col].to_list()
                        
                        # Fallback: If no "Calibration" flag is found, assume it started at t=0
                        if not any("calibration" in str(c).lower() for c in flag_conds):
                            print("No 'Calibration' flag found in experiment flags. Prepending assumed Calibration at t=0.0")
                            # Insert at the beginning of arrays
                            flag_times = np.insert(flag_times, 0, 0.0)
                            flag_conds.insert(0, "Calibration")
                            self.assumed_calibration = True
                        
                        # Add condition column
                        def get_condition(t):
                            if len(flag_times) == 0: return "Assumed Calibration" # Fallback if empty
                            idx = np.searchsorted(flag_times, t, side='right') - 1
                            if idx < 0: return flag_conds[0]
                            return flag_conds[idx]
                        
                        r_times = new_df['relative_time'].to_numpy()
                        new_conditions = [get_condition(t) for t in r_times]
                        new_df = new_df.with_columns(pl.Series('condition', new_conditions))
                    else:
                        new_df = new_df.with_columns(pl.lit("Assumed Calibration").alias('condition'))
                except Exception as e:
                    print(f"Error reading flags: {e}")
                    new_df = new_df.with_columns(pl.lit("Assumed Calibration").alias('condition'))
            else:
                new_df = new_df.with_columns(pl.lit("Assumed Calibration").alias('condition'))
                
            # Reorder columns as requested: relative_time, condition, calibrated_mass_pg, uncalibrated_mass_pg
            final_cols = ['relative_time', 'condition', 'calibrated_mass_pg', 'uncalibrated_mass_pg']
            remaining_cols = [c for c in new_df.columns if c not in final_cols]
            new_df = new_df.select(final_cols + remaining_cols)
            
            self.peaks_df = new_df
            return self.peaks_df
            
        except Exception as e:
            print(f"Critial error in full reprocessing: {e}")
            traceback.print_exc()
            return None

    def reprocess_peaks_from_subsets(self, binary_filepath, experiment_flags_path):
        """Wrapper for backward compatibility, now uses full-file scan."""
        return self.reprocess_binary_file(binary_filepath, experiment_flags_path)

    def filter_beads_mahalanobis(self, bead_condition_substring='Calibration'):
        """
        Filters the beads using Mahalanobis distance.
        Uses height_diff_percent and peak_width_ms as features to find outliers.
        """
        # Identify bead rows
        # We assume condition column contains 'bead' (case-insensitive) for calibration beads
        # Fill nulls with empty string beforehand so str operations do not fail or drop them
        bead_mask = (
            pl.col('condition').fill_null("").str.to_lowercase().str.contains(bead_condition_substring.lower())
        )
        beads_df = self.peaks_df.filter(bead_mask)
        cells_df = self.peaks_df.filter(~bead_mask)
        
        if len(beads_df) < 10:
            print(f"Not enough beads found ({len(beads_df)}) for Mahalanobis filtering.")
            self.filtered_beads_df = beads_df
            self.cells_df = cells_df
            return
            
        # Features for Mahalanobis distance
        # We use peak_width_ms and height_diff_percent
        features = ['peak_width_ms', 'height_diff_percent']
        X = beads_df.select(features).to_numpy()
        
        # Calculate robust covariance and mean (simplified to regular if scikit-learn not available)
        if SKLEARN_AVAILABLE:
            cov_robust = MinCovDet().fit(X)
            mu = cov_robust.location_
            cov = cov_robust.covariance_
        else:
            # Fallback to standard covariance
            mu = np.mean(X, axis=0)
            cov = np.cov(X, rowvar=False)
            
        # Calculate Mahalanobis distances
        try:
            inv_cov = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            # If singular, fallback to diagonal
            inv_cov = np.diag(1.0 / np.var(X, axis=0))
            
        diff = X - mu
        md = np.sum(np.dot(diff, inv_cov) * diff, axis=1)
        
        # Cutoff based on Chi-square distribution (alpha = 0.05 equivalent or similar to R script)
        cutoff = chi2.ppf(0.95, df=len(features))
        
        # Re-attach md array back into polars as a column to filter
        beads_df = beads_df.with_columns(pl.Series("mahalanobis_dist", md))
        self.filtered_beads_df = beads_df.filter(pl.col("mahalanobis_dist") < cutoff)
        
        dropped = len(beads_df) - len(self.filtered_beads_df)
        print(f"Mahalanobis filtering completed. Dropped {dropped} outlier bead peaks.")
        
        self.cells_df = cells_df

    def calibrate_sensors(self, use_drift_correction: bool = True):
        """
        Calibrates the 'approximate_mass_pg' into 'calibrated_mass_pg' 
        by finding the mode of the bead mass distribution and calculating a scale factor.
        We also model an optional simple linear drift in mass to account for media density evaporation/changes.
        """
        if not hasattr(self, 'filtered_beads_df') or len(self.filtered_beads_df) < 5:
            print("Not enough beads for calibration. Applying factor of 1.0.")
            self.peaks_df = self.peaks_df.with_columns(
                pl.col('uncalibrated_mass_pg').alias('calibrated_mass_pg')
            )
            self.calibration_factor = 1.0
            return
            
        bead_times = self.filtered_beads_df['relative_time'].to_numpy()
        
        # Calculate robust mean mass locally using the R-parity mass_raw values if available
        if 'mass_raw1' in self.filtered_beads_df.columns and 'mass_raw2' in self.filtered_beads_df.columns:
            bead_mass1 = self.filtered_beads_df['mass_raw1'].to_numpy()
            bead_mass2 = self.filtered_beads_df['mass_raw2'].to_numpy()
            bead_masses = (bead_mass1 + bead_mass2) / 2.0
        else:
            # Fallback for older CSVs missing R-parity columns
            bead_masses = self.filtered_beads_df['uncalibrated_mass_pg'].to_numpy()
        
        # Ensure time starts at 0 for drifting
        t0 = np.min(bead_times)
        rel_bead_times = bead_times - t0
        
        # Objective function for drift and sensitivity
        def calibration_error(x):
            if use_drift_correction:
                sens, slope = x
            else:
                sens = x[0]
                slope = 0.0
                
            # We want mode of calibrated_masses to match expected_bead_mass_pg
            predicted_masses = (bead_masses * sens) / (1 + slope * rel_bead_times)
            
            # Use median instead of mode for stability in optimization
            current_median = np.median(predicted_masses)
            err = np.abs(current_median - self.expected_bead_mass_pg)
            
            # Penalize highly variable mass
            err += 0.1 * np.std(predicted_masses) 
            return err

        # Initial guess 
        median_raw = np.median(bead_masses)
        if np.abs(median_raw) < 1e-9:
            initial_sens = 1.0
        else:
            initial_sens = self.expected_bead_mass_pg / median_raw
            
        # Clip initial guess to physical bounds [0.5, 2.0]
        initial_sens = np.clip(initial_sens, 0.5, 2.0)
        
        if use_drift_correction:
            # Enforce physical sensitivity bounds [0.5, 2.0]
            # Bounds: (sensitivity [0.5, 2.0], slope is unbounded)
            res = minimize(
                calibration_error, 
                [initial_sens, 0.0], 
                method='L-BFGS-B', 
                bounds=[(0.5, 2.0), (None, None)]
            )
            self.calibration_sens, self.calibration_slope = res.x
        else:
            # Optimize only sensitivity, slope fixed at 0
            # Bounds: (sensitivity [0.5, 2.0])
            res = minimize(
                calibration_error, 
                [initial_sens], 
                method='L-BFGS-B', 
                bounds=[(0.5, 2.0)]
            )
            self.calibration_sens = res.x[0]
            self.calibration_slope = 0.0
            
        # Apply calibration columns using fast native Polars expressions
        if 'mass_raw1' in self.peaks_df.columns and 'mass_raw2' in self.peaks_df.columns:
            expr_mean_raw_mass = (pl.col("mass_raw1") + pl.col("mass_raw2")) / 2.0
        else:
            expr_mean_raw_mass = pl.col("uncalibrated_mass_pg")
            
        expr_calibrate = (
            (expr_mean_raw_mass * self.calibration_sens) / 
            (1 + self.calibration_slope * (pl.col("relative_time") - t0))
        )
        
        self.peaks_df = self.peaks_df.with_columns(
            expr_calibrate.alias("calibrated_mass_pg")
        )
        
        self.filtered_beads_df = self.filtered_beads_df.with_columns(
            expr_calibrate.alias("calibrated_mass_pg")
        )
        
        self.cells_df = self.cells_df.with_columns(
            expr_calibrate.alias("calibrated_mass_pg")
        )
        
        print(f"Calibration successful: Target Mass = {self.expected_bead_mass_pg:.3f} pg")
        print(f"Derived Sensitivity Factor = {self.calibration_sens:.5f}, Drift Slope = {self.calibration_slope:.5e}")

    def save_results(self, output_filepath):
        """Saves calibrated results to CSV."""
        self.peaks_df.write_csv(output_filepath)
        print(f"Saved calibrated peak data to: {output_filepath}")

    def save_meta_file(self, output_filepath, input_files, use_drift_correction=True):
        """Saves calibration metadata to a text file."""
        import datetime
        now = datetime.datetime.now()
        
        # Calculate some stats for the meta file
        total_bead_peaks = 0
        if hasattr(self, 'peaks_df'):
            # Count rows where condition contains 'calibration' (case-insensitive)
            # Use same logic as filter_beads_mahalanobis
            bead_mask = (
                self.peaks_df['condition'].fill_null("").str.to_lowercase().str.contains('calibration')
            )
            total_bead_peaks = self.peaks_df.filter(bead_mask).height
            
        filtered_beads = len(self.filtered_beads_df) if hasattr(self, 'filtered_beads_df') else 0
        sens = self.calibration_sens if hasattr(self, 'calibration_sens') else 1.0
        slope = self.calibration_slope if hasattr(self, 'calibration_slope') else 0.0
        
        with open(output_filepath, 'w') as f:
            f.write(f"Posthoc Analysis Date: {now.strftime('%Y-%m-%d')}\n")
            f.write(f"Posthoc Analysis Time: {now.strftime('%H:%M:%S')}\n")
            
            if self.total_chunks > 0:
                empty_pct = (self.empty_chunks / self.total_chunks) * 100.0
                f.write(f"Data Coverage: {self.empty_chunks}/{self.total_chunks} chunks contained no peaks ({empty_pct:.1f}%)\n")
            f.write("Input Files:\n")
            for file_path in input_files:
                if file_path:
                    f.write(f"  - {os.path.basename(file_path)}\n")
                
            f.write(f"\nBead Peaks Detected (Raw): {total_bead_peaks}\n")
            if self.assumed_calibration:
                f.write(f"Assumed Calibration: True (no 'Calibration' flag found, mapping assumed start of file as Calibration)\n")
            else:
                f.write(f"Assumed Calibration: False\n")
                
            f.write(f"Beads Remaining post-filter: {filtered_beads}\n")
            f.write(f"Bead Diameter Used: {self.calibration_bead_diameter} um\n")
            f.write(f"System Baseline Sensitivity: {sens:.5f} Hz/pg\n")
            
            if use_drift_correction:
                f.write(f"Drift Correction: Enabled\n")
                f.write(f"Calculated Drift Slope: {slope:.5e} (mass shift / sec)\n")
            else:
                f.write(f"Drift Correction: Disabled\n")

def process_experiment(input_csv, calibration_bead_diameter=7.008, bead_condition_substring='Calibration', progress_callback=None, use_drift_correction=False, data_rate=20000.0, settings=None, analyzer=None):
    """
    Main entry point for post-hoc analysis. 
    Prioritizes full binary reprocessing if the frequency binary is available.
    """
    def report_progress(value, message):
        if progress_callback:
            if callable(progress_callback):
                try:
                    progress_callback(value, message)
                except TypeError:
                    progress_callback(value) # Fallback for single-arg callbacks
            
    csv_dir = os.path.dirname(os.path.abspath(input_csv))
    csv_basename = os.path.basename(input_csv)
    
    # Identify prefix (e.g. LC05_202603201001)
    parts = csv_basename.split('_')
    if len(parts) >= 2:
        prefix = f"{parts[0]}_{parts[1]}"
    else:
        prefix = os.path.splitext(csv_basename)[0]
        
    output_csv = os.path.join(csv_dir, f"{prefix}_calibrated_peaks.csv")
    
    if analyzer is None:
        analyzer = PostHocFrequencyAnalyzer(
            calibration_bead_diameter=calibration_bead_diameter,
            progress_callback=progress_callback
        )
    analyzer.data_rate = data_rate
    analyzer.settings = settings
    
    # Attempt to locate matching binary file and experiment flags file
    binary_filename = os.path.join(csv_dir, f"{prefix}_a00.bin")
    if not os.path.exists(binary_filename):
        # Check for non-prefixed version in case user renamed things
        binary_filename = input_csv.replace('_uncalibrated_peaks.csv', '_a00.bin')
        
    if not os.path.exists(binary_filename):
        binary_filename = None
        
    experiment_flags_path = os.path.join(csv_dir, f"{prefix}_experiment_flags.txt")
    if not os.path.exists(experiment_flags_path):
        experiment_flags_path = input_csv.replace('_uncalibrated_peaks.csv', '_experiment_flags.txt')
        
    if not os.path.exists(experiment_flags_path):
        experiment_flags_path = None

    if binary_filename:
        report_progress(5, "Reprocessing full binary file (R-parity logic)...")
        analyzer.reprocess_binary_file(binary_filename, experiment_flags_path, settings=settings)
    else:
        report_progress(5, "Loading existing uncalibrated peaks (Binary not found)...")
        print(f"Loading data from {input_csv}...")
        analyzer.load_uncalibrated_peaks(input_csv)
        
    if not hasattr(analyzer, 'peaks_df') or analyzer.peaks_df is None:
        print("Error: No peak data available for analysis.")
        report_progress(100, "Error: No peaks found.")
        return None
        
    report_progress(60, "Filtering beads via Mahalanobis...")
    analyzer.filter_beads_mahalanobis(bead_condition_substring)
    
    report_progress(80, "Calibrating sensors...")
    analyzer.calibrate_sensors(use_drift_correction=use_drift_correction)
    
    report_progress(95, "Saving calibrated results...")
    analyzer.save_results(output_csv)
    
    # Generate meta file
    meta_output = os.path.join(csv_dir, f"{prefix}_calibration_meta.txt")
    analyzer.save_meta_file(meta_output, [input_csv, binary_filename], use_drift_correction=use_drift_correction)
    
    report_progress(100, "Analysis complete.")
    return analyzer

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post-hoc Frequency Analysis for Paella")
    parser.add_argument("input_csv", help="Path to the _uncalibrated_peaks.csv file")
    parser.add_argument("--bead-diameter", type=float, default=7.008, help="Diameter of calibration beads (microns)")
    parser.add_argument("--bead-condition", type=str, default="Calibration", help="Substring identifying beads in condition column")
    parser.add_argument("--data-rate", type=float, default=20000.0, help="Sampling data rate in Hz (default: 20000)")
    parser.add_argument("--no-drift", action="store_false", dest="use_drift", help="Disable drift correction")
    
    args = parser.parse_args()
    process_experiment(
        args.input_csv, 
        args.bead_diameter, 
        args.bead_condition, 
        use_drift_correction=getattr(args, 'use_drift', True),
        data_rate=args.data_rate
    )
