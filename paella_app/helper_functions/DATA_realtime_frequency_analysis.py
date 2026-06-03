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
import warnings
# Suppress RankWarnings from polyfit in the detection inner loops
# Handle NumPy 1.x vs 2.x attribute locations
try:
    _RANK_WARNING = getattr(np, 'RankWarning', getattr(np.get_include(), 'RankWarning', None))
    if _RANK_WARNING is None:
        import numpy.exceptions
        _RANK_WARNING = numpy.exceptions.RankWarning
except (AttributeError, ImportError):
    _RANK_WARNING = None

if _RANK_WARNING:
    warnings.simplefilter('ignore', _RANK_WARNING)

try:
    from scipy import signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("Warning: scipy not available. Peak detection will be limited.")

from helper_functions.SMR_frequency_processing_functions import (
    DataStructure,
    estimate_baseline_rolling_median,
    FilterSettings
)


@dataclass
class PeakDetectionSettings:
    """Settings for peak detection."""
    detection_threshold: float = -3.0  # Hz
    doublet_gap_min: float = 0.001  # seconds
    doublet_gap_max: float = 0.025  # seconds
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
        settings: Optional[PeakDetectionSettings] = None,
        offline_mode: bool = False
    ):
        """
        Initialize the real-time frequency analyzer.
        
        Args:
            data_rate: Data rate in Hz
            buffer_window_seconds: Size of sliding window in seconds
            min_samples_for_analysis: Minimum samples required before running analysis
            settings: Peak detection settings (uses defaults if None)
            offline_mode: Disables memory-bounds clipping and GUI array allocation for fast batching
        """
        self.offline_mode = offline_mode
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
        
        # Track the latest time in the buffer to provide a reference for throughput/plots
        self.latest_buffer_time: float = 0.0
    
    def set_data_rate(self, data_rate: float):
        """Update data rate and recalculate buffer size."""
        self.data_rate = data_rate
        self.max_buffer_samples = int(self.buffer_window_seconds * data_rate)
    
    def update_settings(self, settings: PeakDetectionSettings):
        """Update peak detection settings."""
        self.settings = settings
    
    def add_data_points(self, new_points: List[Tuple[float, float, int, float]]):
        """
        Add new data points to the analysis buffer (thread-safe).
        
        Args:
            new_points: List of (time, frequency, packet_number, relative_time) tuples
        """
        if not new_points:
            return
        
        with self.buffer_lock:
            # Add new points efficiently
            self.analysis_buffer.extend(new_points)
            
            # Track latest time
            if len(new_points) > 0:
                self.latest_buffer_time = max(self.latest_buffer_time, new_points[-1][0])
                
            # Trim buffer to maintain window size
            if len(self.analysis_buffer) > 0:
                window_start_time = self.latest_buffer_time - self.buffer_window_seconds
                
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
        
        # Validate buffer data before processing
        if not buffer_list:
            return False
            
        # convert list of tuples to numpy array
        buffer_arr = np.array(buffer_list, dtype=np.float64)
        times = buffer_arr[:, 0]
        frequencies = buffer_arr[:, 1]
        
        if buffer_arr.shape[1] > 2:
            packet_numbers = buffer_arr[:, 2].astype(np.int64)
        else:
            packet_numbers = np.zeros(len(buffer_arr), dtype=np.int64)
            
        if buffer_arr.shape[1] > 3:
            relative_times = buffer_arr[:, 3]
        else:
            relative_times = np.zeros(len(buffer_arr), dtype=np.float64)
            
        return self.process_arrays(times, frequencies, packet_numbers, relative_times)
        
    def process_arrays(self, times: np.ndarray, frequencies: np.ndarray, packet_numbers: np.ndarray, relative_times: np.ndarray) -> bool:
        """
        Process pre-extracted numpy arrays directly.
        Returns true if analysis was successful.
        """
        try:
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
                packet_numbers=packet_numbers,
                relative_times=relative_times,
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
            print(f"Error in real-time array processing: {e}")
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
            
            # Find all local minima (peaks on inverted signal) using scipy.signal.find_peaks
            # This is much more robust than the contiguous region approach which merges peaks
            self.detected_individual_peaks = []
            
            if SCIPY_AVAILABLE:
                # Use a more robust min_dist and prominence to prevent 'shattering'
                # (detecting noise jitters as multiple peaks within one physical dip)
                min_dist_samples = max(2, int(0.0015 * self.data_rate)) # 1.5ms min separation
                
                # find_peaks looks for maxima, so invert the dips
                peak_indices, _ = signal.find_peaks(
                    -detection_data,
                    height=-self.settings.detection_threshold, # Threshold (inverted)
                    distance=min_dist_samples,
                    prominence=1.25 # Must stand out by 1.25 Hz from noise floor/neighbors
                )
                
                for idx in peak_indices:
                    # Perform Parabolic Interpolation for higher precision mass and width
                    # Use 3 points: idx-1, idx, idx+1 to find the true sub-sample minimum
                    sub_sample_idx = float(idx)
                    sub_sample_dev = float(detection_data[idx])
                    sub_sample_time = float(times[idx])
                    
                    if 0 < idx < len(detection_data) - 1:
                        y_mid = detection_data[idx]
                        y_left = detection_data[idx-1]
                        y_right = detection_data[idx+1]
                        
                        # Parabolic formula: f(x) = ax^2 + bx + c
                        # Offset to vertex: d = 0.5 * (y_left - y_right) / (y_left - 2*y_mid + y_right)
                        denominator = (y_left - 2*y_mid + y_right)
                        if abs(denominator) > 1e-9:
                            d = 0.5 * (y_left - y_right) / denominator
                            # Clamp d to +/- 1 sample to avoid runaway interpolation on weird shapes
                            d = max(-1.0, min(1.0, d))
                            
                            sub_sample_idx = idx + d
                            # Interpolated peak value
                            sub_sample_dev = y_mid - 0.25 * (y_left - y_right) * d
                            
                            # Interpolate time and relative time
                            if idx + 1 < len(times):
                                time_step = times[idx+1] - times[idx]
                                sub_sample_time = times[idx] + (d * time_step)
                    
                    # Ensure peaks at the absolute leading edge of the timeline have 
                    # enough 'future' padding to prevent median-filter edge reflection warping
                    if not self.offline_mode and len(times) > 0:
                        max_time = times[-1]
                        if sub_sample_time > max_time - 0.5:
                            continue
                    
                    peak_dict = {
                        'index': sub_sample_idx,
                        'time': sub_sample_time,
                        'frequency': float(frequencies_for_detection[idx]), # Keep original sample for frequency metadata
                        'deviation': sub_sample_dev,
                        'baseline': float(baseline_frequencies[idx]),
                        'packet_number': int(self.current_data_structure.packet_numbers[idx]) if (self.current_data_structure and self.current_data_structure.packet_numbers is not None) else 0,
                    }
                    
                    # Interpolate relative time if available
                    if self.current_data_structure and self.current_data_structure.relative_times is not None:
                        rel_times = self.current_data_structure.relative_times
                        d_idx = sub_sample_idx - idx
                        if 0 <= int(sub_sample_idx) < len(rel_times) - 1:
                            rel_step = rel_times[idx+1] - rel_times[idx]
                            peak_dict['relative_time'] = float(rel_times[idx] + (d_idx * rel_step))
                        else:
                            peak_dict['relative_time'] = float(rel_times[idx])
                    else:
                        peak_dict['relative_time'] = 0.0
                        
                    self.detected_individual_peaks.append(peak_dict)
            else:
                # Fallback: Simple contiguous region logic if scipy is somehow missing
                indices_below = np.where(detection_data < threshold_value)[0]
                if len(indices_below) == 0:
                    return
                    
                diffs = np.diff(indices_below)
                breaks = np.where(diffs > 1)[0]
                starts = np.concatenate([[0], breaks + 1])
                ends = np.concatenate([breaks, [len(indices_below) - 1]])
                regions = zip(indices_below[starts], indices_below[ends])
                
                for start_idx, end_idx in regions:
                    region_indices = np.arange(start_idx, end_idx + 1)
                    if len(region_indices) > 0:
                        region_detection = detection_data[region_indices]
                        min_idx_in_region = np.argmin(region_detection)
                        absolute_idx = region_indices[min_idx_in_region]
                        peak_time = float(times[absolute_idx])
                        
                        if not self.offline_mode and len(times) > 0:
                            if peak_time > times[-1] - 0.5:
                                continue
                        
                        peak_dict = {
                            'index': int(absolute_idx),
                            'time': peak_time,
                            'frequency': float(frequencies_for_detection[absolute_idx]),
                            'deviation': float(detection_data[absolute_idx]),
                            'baseline': float(baseline_frequencies[absolute_idx]),
                            'packet_number': int(self.current_data_structure.packet_numbers[absolute_idx]) if self.current_data_structure.packet_numbers is not None else 0,
                            'relative_time': float(self.current_data_structure.relative_times[absolute_idx]) if self.current_data_structure.relative_times is not None else 0.0
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
            
            raw_freqs = self.current_data_structure.frequencies if self.current_data_structure else np.array([])

            
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
                    if time_to_next_samples < min_gap_samples:
                        continue
                    if time_to_next_samples > max_gap_samples:
                        break
                    
                    # Check height difference
                    height_diff = abs(abs(peak1_dev) - abs(peak2_dev))
                    if height_diff > max_height_diff:
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
                        'mass_raw': abs(mean_deviation) / self.sensitivity_hz_per_pg if self.sensitivity_hz_per_pg > 0 else 0.0,
                        'packet_number': peak2['packet_number'],
                        'relative_time': (peak1['relative_time'] + peak2['relative_time']) / 2.0,
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
        
        # Track which pairs were already processed to avoid duplicates across sliding windows
        new_accepted_pairs = []
        
        for pair in self.accepted_matched_pairs:
            pair_time = pair['peak1_time']
            if pair_time not in self.processed_pair_times:
                new_accepted_pairs.append(pair)
                self.processed_pair_times.add(pair_time)
                self.total_matched_pairs_count += 1
                
        # If we have new pairs, append them
        if new_accepted_pairs:
            if not self.offline_mode:
                p1t = np.array([p['peak1_time'] for p in new_accepted_pairs])
                p2t = np.array([p['peak2_time'] for p in new_accepted_pairs])
                sep = np.array([p['separation_time'] for p in new_accepted_pairs])
                mraw = np.array([p.get('mass_raw', 0.0) for p in new_accepted_pairs])
                hdiff = np.array([p['height_diff_percent'] for p in new_accepted_pairs])
                mdev = np.array([p['mean_deviation'] for p in new_accepted_pairs])
                
                self.matched_pairs_peak1_time = np.concatenate([self.matched_pairs_peak1_time, p1t])
                self.matched_pairs_peak2_time = np.concatenate([self.matched_pairs_peak2_time, p2t])
                self.matched_pairs_separation_time = np.concatenate([self.matched_pairs_separation_time, sep])
                self.matched_pairs_mass_raw = np.concatenate([self.matched_pairs_mass_raw, mraw])
                self.matched_pairs_height_diff_percent = np.concatenate([self.matched_pairs_height_diff_percent, hdiff])
                self.matched_pairs_mean_deviation = np.concatenate([self.matched_pairs_mean_deviation, mdev])
            
            # ALWAYS maintain list of dicts for CSV export
            for p in new_accepted_pairs:
                p_copy = p.copy()
                p_copy['timestamp'] = p['peak1_time']
                self.all_matched_pairs.append(p_copy)
        
        # Clean up old matched pairs (keep only last 30 minutes)
        if not self.offline_mode:
            max_history_age = 1800.0  # 30 minutes
            
            # Defensive: Use latest_buffer_time but check if it's realistic (not 0 and not ancient)
            # If we just started, latest_buffer_time might be far from existing data if flow was paused
            ref_time = self.latest_buffer_time
            if ref_time <= 0 and len(self.matched_pairs_peak1_time) > 0:
                ref_time = np.max(self.matched_pairs_peak1_time)
                
            cutoff_time = ref_time - max_history_age
            
            if len(self.matched_pairs_peak1_time) > 0:
                mask = self.matched_pairs_peak1_time >= cutoff_time
                self.matched_pairs_peak1_time = self.matched_pairs_peak1_time[mask]
                self.matched_pairs_peak2_time = self.matched_pairs_peak2_time[mask]
                self.matched_pairs_separation_time = self.matched_pairs_separation_time[mask]
                self.matched_pairs_mass_raw = self.matched_pairs_mass_raw[mask]
                self.matched_pairs_height_diff_percent = self.matched_pairs_height_diff_percent[mask]
                self.matched_pairs_mean_deviation = self.matched_pairs_mean_deviation[mask]
                
                # Clean up processed set
                expired_times = [t for t in self.processed_pair_times if t < cutoff_time]
                for t in expired_times:
                    self.processed_pair_times.remove(t)
                    
                # Clean up history list
                self.all_matched_pairs = [p for p in self.all_matched_pairs if p.get('peak1_time', 0) >= cutoff_time]
        
        # Handle individual unmatched peaks
        used_peak_times = set()
        for pair in self.matched_peak_pairs:
            used_peak_times.add(pair['peak1_time'])
            used_peak_times.add(pair['peak2_time'])
            
        for peak in self.detected_individual_peaks:
            peak_time = peak['time']
            if peak_time not in used_peak_times and peak_time not in self.processed_peak_times:
                self.total_individual_peaks_count += 1
                pk_copy = peak.copy()
                pk_copy['timestamp'] = peak_time
                self.all_individual_peaks.append(pk_copy)
                self.processed_peak_times.add(peak_time)

        # Clean up individual peaks history
        if not self.offline_mode:
            if self.all_individual_peaks:
                self.all_individual_peaks = [p for p in self.all_individual_peaks if p.get('time', 0) >= cutoff_time]
                expired_peaks = [t for t in self.processed_peak_times if t < cutoff_time]
                for t in expired_peaks:
                    self.processed_peak_times.remove(t)
            
        # Return only the results that were JUST added in this call
        # We use peak1_time as a unique identifier for the pair
        new_pair_times = [p['peak1_time'] for p in new_accepted_pairs]
        return [p for p in self.all_matched_pairs if p.get('peak1_time') in new_pair_times]
    
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
        Get throughput data for plotting (optimized and lock-free).
        
        Returns:
            Tuple of (time_windows, throughput_cells_per_hour, current_throughput)
            - time_windows: numpy array of time window end times (seconds)
            - throughput_cells_per_hour: numpy array of throughput values in cells/hour
            - current_throughput: Current throughput rate in cells/hour (extrapolated from last 15 seconds)
        """
        import time as time_module
        import numpy as np
        
        # 1. Quickly snapshot the required data and release the lock immediately
        with self.buffer_lock:
            if len(self.matched_pairs_peak1_time) == 0:
                return (np.array([], dtype=np.float64), np.array([], dtype=np.float64), 0.0)
            
            mask = self.matched_pairs_height_diff_percent <= self.settings.max_percent_diff
            if not np.any(mask):
                return (np.array([], dtype=np.float64), np.array([], dtype=np.float64), 0.0)
            
            pair_times = self.matched_pairs_peak1_time[mask]
            
            if len(pair_times) == 0:
                return (np.array([], dtype=np.float64), np.array([], dtype=np.float64), 0.0)
            
            # Extract just the times, and store latest buffer time
            ref_time_locked = self.latest_buffer_time

        # 2. Lock is now released. Perform computations freely.
        # Numpy arrays are usually appended in order, but sort to ensure monotonic array for searchsorted
        pair_times_sorted = np.sort(pair_times)
        
        ref_time = ref_time_locked if ref_time_locked > 0 else pair_times_sorted[-1]
        cutoff_time = ref_time - self.throughput_window_seconds
        
        # Find index of elements occurring after cutoff for current throughput
        idx_cutoff = np.searchsorted(pair_times_sorted, cutoff_time)
        pairs_in_last_15s_count = len(pair_times_sorted) - idx_cutoff
        
        current_throughput = 0.0
        if pairs_in_last_15s_count > 0:
            actual_time_span = self.throughput_window_seconds
            if pairs_in_last_15s_count > 1:
                time_span = pair_times_sorted[-1] - pair_times_sorted[idx_cutoff]
                actual_time_span = max(time_span, 1.0)
            
            current_throughput = (pairs_in_last_15s_count / actual_time_span) * 3600.0
            
        # For plotting: generate throughput curve
        if len(pair_times_sorted) > 1:
            plot_step = 5.0
            start_time = pair_times_sorted[0]
            end_time = pair_times_sorted[-1]
            
            if end_time - start_time > plot_step:
                time_windows = np.arange(start_time, end_time, plot_step)
                
                # Vectorized window count using searchsorted for O(log N) instead of O(N) mask
                starts = time_windows - (self.throughput_window_seconds / 2)
                ends = time_windows + (self.throughput_window_seconds / 2)
                
                start_indices = np.searchsorted(pair_times_sorted, starts, side='left')
                end_indices = np.searchsorted(pair_times_sorted, ends, side='right')
                
                counts = end_indices - start_indices
                throughput_cells_per_hour = (counts / self.throughput_window_seconds) * 3600.0
                
            else:
                time_windows = np.array([ref_time], dtype=np.float64)
                throughput_cells_per_hour = np.array([current_throughput], dtype=np.float64)
                
        elif len(pair_times_sorted) == 1:
            time_windows = np.array([ref_time], dtype=np.float64)
            throughput_cells_per_hour = np.array([current_throughput], dtype=np.float64)
            
        else:
            time_windows = np.array([ref_time], dtype=np.float64) if ref_time > 0 else np.array([], dtype=np.float64)
            throughput_cells_per_hour = np.array([0.0], dtype=np.float64) if ref_time > 0 else np.array([], dtype=np.float64)
            
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
