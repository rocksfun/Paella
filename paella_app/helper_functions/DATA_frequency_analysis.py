"""
Real-time Frequency Analysis Module

Extracted processing logic for real-time peak detection and analysis.
Can be used by both pySMR.py and AUX_frequency_binary_viewer.py.
"""

import numpy as np
import threading
from collections import deque
from typing import List, Tuple, Dict, Any, Optional
from dataclasses import dataclass

try:
    from scipy import signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("Warning: scipy not available. Peak detection will be limited.")

from helper_functions.frequency_analysis import (
    DataStructure,
    estimate_baseline_rolling_median,
    FilterSettings
)


@dataclass
class PeakDetectionSettings:
    """Settings for peak detection."""
    detection_threshold: float = -3.0  # Hz
    doublet_gap_min: float = 0.001  # seconds
    doublet_gap_max: float = 0.015  # seconds
    max_height_diff: float = 10.0  # Hz
    max_percent_diff: float = 10.0  # percent
    baseline_search_width: float = 0.1  # seconds
    filter_width: int = 11  # samples (must be odd)
    use_pg_units: bool = False


class RealTimeFrequencyAnalyzer:
    """
    Real-time frequency analyzer for peak detection and analysis.
    
    Manages a sliding window buffer and performs periodic analysis to detect
    peaks, match them into pairs, and accumulate results over time.
    """
    
    def __init__(
        self,
        data_rate: float = 20000.0,
        buffer_window_seconds: float = 1.0,
        min_samples_for_analysis: int = 1000,
        settings: Optional[PeakDetectionSettings] = None
    ):
        """
        Initialize the real-time frequency analyzer.
        
        Args:
            data_rate: Data rate in Hz
            buffer_window_seconds: Size of sliding window in seconds
            min_samples_for_analysis: Minimum samples required before running analysis
            settings: Peak detection settings (uses defaults if None)
        """
        self.data_rate = data_rate
        self.buffer_window_seconds = buffer_window_seconds
        self.min_samples_for_analysis = min_samples_for_analysis
        self.max_buffer_samples = int(buffer_window_seconds * data_rate)
        
        # Settings
        self.settings = settings or PeakDetectionSettings()
        
        # Thread safety: Lock for buffer access (UDP thread writes, GUI thread reads)
        self.buffer_lock = threading.RLock()
        
        # Analysis buffer: sliding window of (time, frequency) tuples
        self.analysis_buffer: deque = deque()
        
        # Results accumulation (protected by buffer_lock when accessed from multiple threads)
        # Use numpy arrays for efficient storage and operations
        # Store matched pairs as separate arrays for each field
        self.matched_pairs_peak1_time: np.ndarray = np.array([], dtype=np.float64)
        self.matched_pairs_peak2_time: np.ndarray = np.array([], dtype=np.float64)
        self.matched_pairs_separation_time: np.ndarray = np.array([], dtype=np.float64)
        self.matched_pairs_mass_raw: np.ndarray = np.array([], dtype=np.float64)
        self.matched_pairs_height_diff_percent: np.ndarray = np.array([], dtype=np.float64)
        self.matched_pairs_mean_deviation: np.ndarray = np.array([], dtype=np.float64)
        
        # Persistent counters that track total peaks (not affected by 30-minute cleanup)
        self.total_matched_pairs_count: int = 0  # Total accepted matched pairs since initialization/clear
        self.total_individual_peaks_count: int = 0  # Total unmatched peaks since initialization/clear
        
        # Keep list of dicts for backward compatibility and CSV export (only recent data)
        self.all_matched_pairs: List[Dict[str, Any]] = []
        self.all_individual_peaks: List[Dict[str, Any]] = []
        self.processed_pair_times: set = set()
        self.processed_peak_times: set = set()
        
        # Throughput tracking
        self.throughput_window_seconds: float = 15.0  # 15-second windows for throughput calculation
        
        # Current analysis state (protected by buffer_lock when accessed from multiple threads)
        self.detected_individual_peaks: List[Dict[str, float]] = []
        self.matched_peak_pairs: List[Dict[str, Any]] = []
        self.accepted_matched_pairs: List[Dict[str, Any]] = []
        self.filtered_frequencies: Optional[np.ndarray] = None
        self.current_data_structure: Optional[DataStructure] = None
        self.sensitivity_hz_per_pg: float = 1.0
    
    def set_data_rate(self, data_rate: float):
        """Update data rate and recalculate buffer size."""
        self.data_rate = data_rate
        self.max_buffer_samples = int(self.buffer_window_seconds * data_rate)
    
    def update_settings(self, settings: PeakDetectionSettings):
        """Update peak detection settings."""
        self.settings = settings
    
    def add_data_points(self, new_points: List[Tuple[float, float]]):
        """
        Add new data points to the analysis buffer (thread-safe).
        
        Args:
            new_points: List of (time, frequency) tuples
        """
        if not new_points:
            return
        
        with self.buffer_lock:
            # Add new points
            for point in new_points:
                self.analysis_buffer.append(point)
            
            # Trim buffer to maintain window size
            if len(self.analysis_buffer) > 0:
                current_time = self.analysis_buffer[-1][0]
                window_start_time = current_time - self.buffer_window_seconds
                
                # Remove points older than window
                while len(self.analysis_buffer) > 0 and self.analysis_buffer[0][0] < window_start_time:
                    self.analysis_buffer.popleft()
                
                # Also limit by max_samples
                while len(self.analysis_buffer) > self.max_buffer_samples:
                    self.analysis_buffer.popleft()
    
    def process(self) -> bool:
        """
        Run analysis on current buffer (thread-safe).
        
        Creates a snapshot of the buffer to avoid blocking add_data_points.
        
        Returns:
            True if analysis was successful, False otherwise
        """
        # Acquire lock and create snapshot
        with self.buffer_lock:
            if len(self.analysis_buffer) < self.min_samples_for_analysis:
                return False
            
            # Create snapshot of buffer for processing
            # This allows add_data_points to continue while we process
            buffer_list = list(self.analysis_buffer)
        
        # Process snapshot outside of lock to avoid blocking add_data_points
        try:
            # Validate buffer data before processing
            if not buffer_list:
                return False
            
            times = np.array([p[0] for p in buffer_list])
            frequencies = np.array([p[1] for p in buffer_list])
            
            # Validate data arrays
            if len(times) == 0 or len(frequencies) == 0 or len(times) != len(frequencies):
                return False
            
            # Check for invalid values (NaN, Inf)
            if np.any(np.isnan(times)) or np.any(np.isnan(frequencies)):
                return False
            if np.any(np.isinf(times)) or np.any(np.isinf(frequencies)):
                return False
            
            # Apply Savitzky-Golay filter
            try:
                filter_window_samples = self.settings.filter_width
                if filter_window_samples % 2 == 0:
                    filter_window_samples += 1
                filter_window_samples = min(filter_window_samples, len(frequencies) - (len(frequencies) % 2))
                
                if filter_window_samples >= 5 and len(frequencies) >= filter_window_samples:
                    self.filtered_frequencies = signal.savgol_filter(
                        frequencies,
                        window_length=filter_window_samples,
                        polyorder=2
                    )
                else:
                    self.filtered_frequencies = frequencies.copy()
            except Exception:
                self.filtered_frequencies = frequencies.copy()
            
            # Estimate baseline
            filter_settings = FilterSettings()
            baseline_estimate = estimate_baseline_rolling_median(
                self.filtered_frequencies,
                self.data_rate,
                window_size_seconds=filter_settings.quantile_width
            )
            
            # Estimate sensitivity
            baseline_mean = np.mean(baseline_estimate)
            if baseline_mean > 0:
                min_sensitivity_estimate = 0.8794 * (baseline_mean / 1e6) ** 1.5 + 0.0367
                self.sensitivity_hz_per_pg = min_sensitivity_estimate
            else:
                self.sensitivity_hz_per_pg = 1.0
            
            # Create DataStructure
            dat = np.column_stack([times, frequencies])
            dat_hp = np.column_stack([times, baseline_estimate])
            
            self.current_data_structure = DataStructure(
                dat=dat,
                dat_bp=None,
                dat_lp=np.column_stack([times, self.filtered_frequencies]) if self.filtered_frequencies is not None else None,
                dat_hp=dat_hp,
                timestamps=times,
                frequencies=frequencies,
                data_rate=self.data_rate,
                sample_info={},
                baseline_mean=np.mean(baseline_estimate),
                baseline_std=np.std(baseline_estimate),
                baseline_regions=[],
                chunks=[]
            )
            
            # Detect individual peaks
            self._detect_individual_peaks()
            
            # Match peak pairs
            self._match_peak_pairs()
            
            # Filter accepted pairs
            self._filter_accepted_pairs()
            
            # Accumulate results (needs lock for thread-safe access)
            with self.buffer_lock:
                self._accumulate_results()
            
            return True
            
        except Exception as e:
            import traceback
            print(f"Error in real-time analysis processing: {e}")
            traceback.print_exc()
            return False
    
    def _detect_individual_peaks(self):
        """Detect individual peaks in the current buffer."""
        try:
            if not self.current_data_structure:
                self.detected_individual_peaks = []
                return
            
            times = self.current_data_structure.timestamps
            
            if self.filtered_frequencies is not None and len(self.filtered_frequencies) == len(times):
                frequencies_for_detection = self.filtered_frequencies
            else:
                frequencies_for_detection = self.current_data_structure.frequencies
            
            if len(times) == 0 or len(frequencies_for_detection) == 0:
                self.detected_individual_peaks = []
                return
            
            threshold_value = self.settings.detection_threshold
            
            if self.current_data_structure.dat_hp is not None:
                baseline_frequencies = self.current_data_structure.dat_hp[:, 1]
                if len(baseline_frequencies) != len(times):
                    baseline_frequencies = np.full_like(times, np.mean(baseline_frequencies))
            else:
                baseline_frequencies = np.full_like(times, self.current_data_structure.baseline_mean or np.mean(frequencies_for_detection))
            
            threshold_frequencies = baseline_frequencies + threshold_value
            detection_data = frequencies_for_detection - baseline_frequencies
            
            indices_below = np.where(detection_data < threshold_value)[0]
            
            if len(indices_below) == 0:
                self.detected_individual_peaks = []
                return
            
            # Find contiguous regions
            regions = []
            region_start = indices_below[0]
            for i in range(1, len(indices_below)):
                if indices_below[i] - indices_below[i-1] > 1:
                    regions.append((region_start, indices_below[i-1]))
                    region_start = indices_below[i]
            regions.append((region_start, indices_below[-1]))
            
            # Find local minimum in each region
            self.detected_individual_peaks = []
            for start_idx, end_idx in regions:
                region_indices = np.arange(start_idx, end_idx + 1)
                if len(region_indices) > 0:
                    region_detection = detection_data[region_indices]
                    min_idx_in_region = np.argmin(region_detection)
                    absolute_idx = region_indices[min_idx_in_region]
                    
                    peak_dict = {
                        'index': int(absolute_idx),
                        'time': float(times[absolute_idx]),
                        'frequency': float(frequencies_for_detection[absolute_idx]),
                        'deviation': float(detection_data[absolute_idx]),
                        'baseline': float(baseline_frequencies[absolute_idx])
                    }
                    self.detected_individual_peaks.append(peak_dict)
            
        except Exception as e:
            import traceback
            print(f"Error detecting peaks: {e}")
            traceback.print_exc()
            self.detected_individual_peaks = []
    
    def _match_peak_pairs(self):
        """Match individual peaks into pairs."""
        try:
            if len(self.detected_individual_peaks) < 2:
                self.matched_peak_pairs = []
                return
            
            min_gap_samples = int(self.settings.doublet_gap_min * self.data_rate)
            max_gap_samples = int(self.settings.doublet_gap_max * self.data_rate)
            max_height_diff = self.settings.max_height_diff
            
            matched_pairs = []
            used_indices = set()
            
            for i in range(len(self.detected_individual_peaks) - 1):
                if i in used_indices:
                    continue
                
                peak1 = self.detected_individual_peaks[i]
                peak1_idx = peak1['index']
                peak1_time = peak1['time']
                peak1_dev = peak1['deviation']
                
                # Look for next peak
                best_match_idx = None
                
                for j in range(i + 1, len(self.detected_individual_peaks)):
                    if j in used_indices:
                        continue
                    
                    peak2 = self.detected_individual_peaks[j]
                    peak2_idx = peak2['index']
                    peak2_time = peak2['time']
                    peak2_dev = peak2['deviation']
                    
                    time_to_next_samples = peak2_idx - peak1_idx
                    
                    # Check time gap
                    if time_to_next_samples < min_gap_samples or time_to_next_samples > max_gap_samples:
                        continue
                    
                    # Check height difference
                    height_diff = abs(abs(peak1_dev) - abs(peak2_dev))
                    if height_diff > max_height_diff:
                        continue
                    
                    # Check triplet prevention
                    if j + 1 < len(self.detected_individual_peaks):
                        peak3 = self.detected_individual_peaks[j + 1]
                        time_after_next = peak3['time'] - peak2_time
                        time_to_next = peak2_time - peak1_time
                        if time_after_next <= time_to_next:
                            continue
                    
                    # Found a match
                    best_match_idx = j
                    break
                
                if best_match_idx is not None:
                    peak2 = self.detected_individual_peaks[best_match_idx]
                    separation_time = peak2['time'] - peak1_time
                    mean_deviation = (peak1_dev + peak2_dev) / 2.0
                    height_diff_abs = abs(abs(peak1_dev) - abs(peak2_dev))
                    height_diff_percent = (height_diff_abs / abs(peak1_dev)) * 100.0 if abs(peak1_dev) > 0 else 0.0
                    
                    pair_dict = {
                        'peak1': peak1,
                        'peak2': peak2,
                        'peak1_time': peak1_time,
                        'peak2_time': peak2['time'],
                        'peak1_deviation': peak1_dev,
                        'peak2_deviation': peak2['deviation'],
                        'peak1_frequency': peak1['frequency'],
                        'peak2_frequency': peak2['frequency'],
                        'peak1_baseline': peak1['baseline'],
                        'peak2_baseline': peak2['baseline'],
                        'mean_deviation': mean_deviation,
                        'separation_time': separation_time,
                        'height_diff_percent': height_diff_percent,
                        'height_diff_abs': height_diff_abs,
                        'mass_raw': abs(mean_deviation) / self.sensitivity_hz_per_pg if self.sensitivity_hz_per_pg > 0 else 0.0
                    }
                    
                    matched_pairs.append(pair_dict)
                    used_indices.add(i)
                    used_indices.add(best_match_idx)
            
            self.matched_peak_pairs = matched_pairs
            
        except Exception as e:
            import traceback
            print(f"Error matching peaks: {e}")
            traceback.print_exc()
            self.matched_peak_pairs = []
    
    def _filter_accepted_pairs(self):
        """Filter matched pairs based on maximum percentage difference."""
        max_percent_diff = self.settings.max_percent_diff
        self.accepted_matched_pairs = [
            pair for pair in self.matched_peak_pairs
            if pair['height_diff_percent'] <= max_percent_diff
        ]
    
    def _accumulate_results(self):
        """Accumulate new results, avoiding duplicates."""
        import time as time_module
        
        # Accumulate NEW matched pairs (only accepted ones)
        new_pairs = []  # Store new pairs with their timestamps and data
        new_pair_data = []  # Store dict data for CSV export (only recent)
        
        for pair in self.accepted_matched_pairs:
            pair_time = pair['peak1_time']
            if pair_time not in self.processed_pair_times:
                # Append to numpy arrays for efficient storage
                self.matched_pairs_peak1_time = np.append(self.matched_pairs_peak1_time, pair['peak1_time'])
                self.matched_pairs_peak2_time = np.append(self.matched_pairs_peak2_time, pair['peak2_time'])
                self.matched_pairs_separation_time = np.append(self.matched_pairs_separation_time, pair['separation_time'])
                self.matched_pairs_mass_raw = np.append(self.matched_pairs_mass_raw, pair.get('mass_raw', 0.0))
                self.matched_pairs_height_diff_percent = np.append(self.matched_pairs_height_diff_percent, pair['height_diff_percent'])
                self.matched_pairs_mean_deviation = np.append(self.matched_pairs_mean_deviation, pair['mean_deviation'])
                
                # Increment persistent total counter (not affected by cleanup)
                self.total_matched_pairs_count += 1
                
                # Also maintain list of dicts for CSV export (only recent data)
                pair_with_timestamp = pair.copy()
                pair_with_timestamp['timestamp'] = pair_time
                self.all_matched_pairs.append(pair_with_timestamp)
                new_pair_data.append(pair_with_timestamp)
                
                self.processed_pair_times.add(pair_time)
                new_pairs.append(pair_time)
        
        # Clean up old matched pairs (keep only last 30 minutes)
        max_history_age = 1800.0  # 30 minutes in seconds
        if len(self.matched_pairs_peak1_time) > 0:
            most_recent_time = np.max(self.matched_pairs_peak1_time)
            cutoff_time = most_recent_time - max_history_age
            
            # Use numpy boolean indexing for efficient filtering
            mask = self.matched_pairs_peak1_time >= cutoff_time
            
            # Apply mask to all arrays
            self.matched_pairs_peak1_time = self.matched_pairs_peak1_time[mask]
            self.matched_pairs_peak2_time = self.matched_pairs_peak2_time[mask]
            self.matched_pairs_separation_time = self.matched_pairs_separation_time[mask]
            self.matched_pairs_mass_raw = self.matched_pairs_mass_raw[mask]
            self.matched_pairs_height_diff_percent = self.matched_pairs_height_diff_percent[mask]
            self.matched_pairs_mean_deviation = self.matched_pairs_mean_deviation[mask]
            
            # Also clean up processed_pair_times set
            times_to_remove = []
            for pt in self.processed_pair_times:
                if pt < cutoff_time:
                    times_to_remove.append(pt)
            for pt in times_to_remove:
                self.processed_pair_times.remove(pt)
            
            # Clean up all_matched_pairs list (keep only last 30 minutes)
            self.all_matched_pairs = [
                p for p in self.all_matched_pairs
                if p.get('peak1_time', p.get('timestamp', 0)) >= cutoff_time
            ]
        
        # Accumulate individual peaks (only unmatched ones)
        used_indices = set()
        for pair in self.matched_peak_pairs:
            # Find indices of peaks in matched pairs
            for i, peak in enumerate(self.detected_individual_peaks):
                if peak['time'] == pair['peak1_time'] or peak['time'] == pair['peak2_time']:
                    used_indices.add(i)
        
        for i, peak in enumerate(self.detected_individual_peaks):
            if i not in used_indices:
                peak_time = peak['time']
                if peak_time not in self.processed_peak_times:
                    # Increment persistent total counter (not affected by cleanup)
                    self.total_individual_peaks_count += 1
                    
                    peak_with_timestamp = peak.copy()
                    peak_with_timestamp['timestamp'] = peak_time
                    self.all_individual_peaks.append(peak_with_timestamp)
                    self.processed_peak_times.add(peak_time)
        
        # Clean up old individual peaks (keep only last 30 minutes)
        if self.all_individual_peaks:
            # Use most recent peak time or current system time as reference
            if new_pairs:
                most_recent_time = max(new_pairs)
            else:
                most_recent_time = time_module.time()
            individual_peaks_max_age = 1800.0  # 30 minutes
            cutoff_time = most_recent_time - individual_peaks_max_age
            
            # Remove old peaks
            peaks_to_keep = []
            for peak in self.all_individual_peaks:
                peak_time = peak.get('time', peak.get('timestamp', 0))
                if peak_time >= cutoff_time:
                    peaks_to_keep.append(peak)
                else:
                    # Remove from processed set if it's being removed
                    if peak_time in self.processed_peak_times:
                        self.processed_peak_times.remove(peak_time)
            
            self.all_individual_peaks = peaks_to_keep
        
        # Return new pair data for CSV export
        return new_pair_data
    
    def get_peak_counts(self) -> Tuple[int, int]:
        """
        Get current peak counts (thread-safe).
        
        Returns:
            Tuple of (matched_pairs_count, unmatched_peaks_count)
            Returns counts from the last 30 minutes (for visualization)
        """
        with self.buffer_lock:
            # Count accepted matched pairs using numpy arrays (last 30 minutes only)
            if len(self.matched_pairs_peak1_time) > 0:
                # Use numpy boolean indexing for efficient filtering
                mask = self.matched_pairs_height_diff_percent <= self.settings.max_percent_diff
                matched_count = np.sum(mask)
            else:
                matched_count = 0
            
            # Count unmatched peaks (last 30 minutes only)
            unmatched_count = len(self.all_individual_peaks)
        
        return matched_count, unmatched_count
    
    def get_total_peak_counts(self) -> Tuple[int, int]:
        """
        Get total peak counts since analyzer initialization/clear (thread-safe).
        
        These counts are not affected by the 30-minute cleanup and represent
        the cumulative total of all peaks detected.
        
        Returns:
            Tuple of (total_matched_pairs_count, total_unmatched_peaks_count)
        """
        with self.buffer_lock:
            return self.total_matched_pairs_count, self.total_individual_peaks_count
    
    def get_plot_data(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Get time-series plot data for peak width and mass (thread-safe).
        
        Returns:
            Tuple of (width_times, widths_ms, mass_times, masses_pg) as numpy arrays
            Returns empty numpy arrays if no data available
        """
        with self.buffer_lock:
            # Use numpy arrays directly for efficient operations
            if len(self.matched_pairs_peak1_time) == 0:
                return (np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64))
            
            # Filter to only accepted pairs using numpy boolean indexing
            mask = self.matched_pairs_height_diff_percent <= self.settings.max_percent_diff
            
            if not np.any(mask):
                return (np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64))
            
            # Extract filtered data as numpy arrays
            width_times = self.matched_pairs_peak1_time[mask]
            widths_ms = self.matched_pairs_separation_time[mask] * 1000.0  # Convert to ms
            mass_times = self.matched_pairs_peak1_time[mask]
            masses_pg = self.matched_pairs_mass_raw[mask]
        
        return width_times, widths_ms, mass_times, masses_pg
    
    def get_throughput_data(self) -> Tuple[np.ndarray, np.ndarray, float]:
        """
        Get throughput data for plotting (thread-safe).
        
        Returns:
            Tuple of (time_windows, throughput_cells_per_hour, current_throughput)
            - time_windows: numpy array of time window end times (seconds)
            - throughput_cells_per_hour: numpy array of throughput values in cells/hour
            - current_throughput: Current throughput rate in cells/hour (extrapolated from last 15 seconds)
        """
        import time as time_module
        
        with self.buffer_lock:
            # Use numpy arrays directly for efficient operations
            if len(self.matched_pairs_peak1_time) == 0:
                return (np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        0.0)
            
            # Filter to only accepted pairs using numpy boolean indexing
            mask = self.matched_pairs_height_diff_percent <= self.settings.max_percent_diff
            
            if not np.any(mask):
                return (np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        0.0)
            
            # Get pair timestamps as numpy array
            pair_times = self.matched_pairs_peak1_time[mask]
            
            if len(pair_times) == 0:
                return (np.array([], dtype=np.float64),
                        np.array([], dtype=np.float64),
                        0.0)
            
            # Sort by time (numpy arrays are already sorted if appended in order, but sort to be safe)
            sorted_indices = np.argsort(pair_times)
            pair_times_sorted = pair_times[sorted_indices]
            
            # Calculate current throughput from last 15 seconds
            most_recent_time = np.max(pair_times_sorted)
            cutoff_time = most_recent_time - self.throughput_window_seconds
            
            # Count pairs in last 15 seconds using numpy boolean indexing
            pairs_in_last_15s_mask = pair_times_sorted >= cutoff_time
            pairs_in_last_15s = pair_times_sorted[pairs_in_last_15s_mask]
            current_throughput = 0.0
            
            if len(pairs_in_last_15s) > 0:
                # Calculate actual time span (use full 15 seconds or actual span if less)
                actual_time_span = self.throughput_window_seconds
                if len(pairs_in_last_15s) > 1:
                    # Use actual time span if we have multiple pairs
                    time_span = pairs_in_last_15s[-1] - pairs_in_last_15s[0]
                    # Use at least 1 second to avoid division by zero, but prefer actual span
                    actual_time_span = max(time_span, 1.0)
                
                # Extrapolate to cells/hour: (pairs / time_span_seconds) * 3600
                current_throughput = (len(pairs_in_last_15s) / actual_time_span) * 3600.0
            
            # For plotting: generate throughput curve from all accepted pairs in the 30-minute buffer
            # We'll use a sliding window approach to match the indicator's behavior
            if len(pair_times_sorted) > 1:
                # Create time windows for the plot (e.g., every 5 seconds)
                plot_step = 5.0
                start_time = np.min(pair_times_sorted)
                end_time = np.max(pair_times_sorted)
                
                # Generate window centers
                if end_time - start_time > plot_step:
                    time_windows = np.arange(start_time, end_time, plot_step)
                    throughput_values = []
                    
                    for window_center in time_windows:
                        # Count pairs in 15s window around this point
                        w_start = window_center - (self.throughput_window_seconds / 2)
                        w_end = window_center + (self.throughput_window_seconds / 2)
                        
                        mask_w = (pair_times_sorted >= w_start) & (pair_times_sorted <= w_end)
                        count = np.sum(mask_w)
                        
                        # Calculate throughput for this window
                        # (count / window_size) * 3600
                        t_val = (count / self.throughput_window_seconds) * 3600.0
                        throughput_values.append(t_val)
                    
                    throughput_cells_per_hour = np.array(throughput_values, dtype=np.float64)
                else:
                    # Not enough data for a curve, just use the single point
                    time_windows = np.array([most_recent_time], dtype=np.float64)
                    throughput_cells_per_hour = np.array([current_throughput], dtype=np.float64)
            elif len(pair_times_sorted) == 1:
                time_windows = np.array([most_recent_time], dtype=np.float64)
                throughput_cells_per_hour = np.array([current_throughput], dtype=np.float64)
            else:
                time_windows = np.array([], dtype=np.float64)
                throughput_cells_per_hour = np.array([], dtype=np.float64)
        
        return time_windows, throughput_cells_per_hour, current_throughput
    
    def get_matched_pairs_for_csv(self) -> List[Dict[str, Any]]:
        """
        Get matched pairs data for CSV export (thread-safe).
        
        Returns:
            List of dictionaries containing matched pair data
        """
        with self.buffer_lock:
            # Return the list of matched pairs (already filtered to last 30 minutes)
            # Filter to only accepted pairs
            accepted_pairs = [
                p for p in self.all_matched_pairs
                if p.get('height_diff_percent', 100.0) <= self.settings.max_percent_diff
            ]
            return accepted_pairs
    
    def clear_results(self):
        """Clear all accumulated results."""
        with self.buffer_lock:
            # Clear numpy arrays
            self.matched_pairs_peak1_time = np.array([], dtype=np.float64)
            self.matched_pairs_peak2_time = np.array([], dtype=np.float64)
            self.matched_pairs_separation_time = np.array([], dtype=np.float64)
            self.matched_pairs_mass_raw = np.array([], dtype=np.float64)
            self.matched_pairs_height_diff_percent = np.array([], dtype=np.float64)
            self.matched_pairs_mean_deviation = np.array([], dtype=np.float64)
            
            # Reset persistent counters
            self.total_matched_pairs_count = 0
            self.total_individual_peaks_count = 0
            
            # Clear lists and sets
            self.all_matched_pairs.clear()
            self.all_individual_peaks.clear()
            self.processed_pair_times.clear()
            self.processed_peak_times.clear()
