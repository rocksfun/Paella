"""
Frequency Analysis Module

Real-time analysis of frequency data from SMR (Suspended Microchannel Resonator) measurements.
Matches R code processing pipeline: readSingleData → getAllPeaks → calibrateSensors.

This module detects particle transits (two near-symmetric negative frequency deviations),
processes complete baseline-to-baseline chunks, and calculates peak parameters.
"""

import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
import time
try:
    from scipy import signal
    from scipy.interpolate import interp1d
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("Warning: scipy not available. Peak detection will be limited.")

try:
    import h5py
    H5PY_AVAILABLE = True
except ImportError:
    H5PY_AVAILABLE = False
    print("Warning: h5py not available. HDF5 output will be disabled.")

import warnings


@dataclass
class DetectionSettings:
    """Settings for peak detection matching R defaults."""
    detection_threshold: float = -4.0  # Threshold for peak detection (Hz) - R default: -4
    node_dev_threshold: float = 10.0  # Node deviation threshold (pg) - R default: 10. Remove doublet peaks and peaks with excessive negative node deviation signal
    # R default: tDoubletGap = c(5,250)/20e3 = c(0.00025, 0.0125) seconds
    t_doublet_gap_min: float = 0.00025  # Minimum time between doublet peaks (seconds) - R default: 5/20e3
    t_doublet_gap_max: float = 0.0125  # Maximum time between doublet peaks (seconds) - R default: 250/20e3
    max_height_diff_percent: float = 15.0  # Maximum height difference between doublet peaks (percentage) - default: 15%
    bandwidth: float = 50.0  # Detection bandwidth (Hz) - R default: 50


@dataclass
class EstimationSettings:
    """Settings for peak parameter estimation matching R defaults."""
    baseline_search_width_side_points: float = 0.1  # Baseline search width (seconds) - R default: 0.1
    num_baseline_chunks: int = 15  # Number of chunks for baseline estimation - R default: 15
    tip_side_point_fraction: float = 0.15  # Fraction of peak width for tip region - R default: 0.15
    bandwidth: float = 2500.0  # Estimation bandwidth (Hz) - R default: 2500


@dataclass
class FilterSettings:
    """Settings for data filtering matching R defaults."""
    sg_order: int = 2  # Savitzky-Golay filter order
    sg_length: int = 11  # Savitzky-Golay filter length (must be odd, calculated from bandwidth)
    quantile_downsampling_factor: int = 20  # Downsampling for quantile filter - R default: 20
    quantile_width: float = 0.25  # Quantile filter width (seconds) - R default: 0.25
    quantile_prob: float = 0.97  # Quantile probability - R default: 0.97 (not median!)


@dataclass
class DataStructure:
    """Data structure matching R's Data object from readSingleData()."""
    dat: np.ndarray  # Raw frequency data with timestamps: shape (N, 2) where [:, 0] = time, [:, 1] = frequency
    dat_bp: Optional[np.ndarray] = None  # Bandpass filtered data (if applicable)
    dat_lp: Optional[np.ndarray] = None  # Lowpass filtered data (if applicable)
    dat_hp: Optional[np.ndarray] = None  # High-pass quantile filter output (baseline drift estimate)
    timestamps: np.ndarray = field(default_factory=lambda: np.array([]))  # Timestamp array
    frequencies: np.ndarray = field(default_factory=lambda: np.array([]))  # Frequency array
    data_rate: float = 20000.0  # Data rate in Hz
    sample_info: Dict[str, Any] = field(default_factory=dict)  # Sample metadata
    baseline_mean: Optional[float] = None  # Baseline frequency mean (deprecated, use dat_hp for time-varying baseline)
    baseline_std: Optional[float] = None  # Baseline frequency std dev
    baseline_regions: List[Tuple[int, int]] = field(default_factory=list)  # List of (start_idx, end_idx) baseline regions
    chunks: List[Tuple[int, int]] = field(default_factory=list)  # List of (start_idx, end_idx) complete chunks


@dataclass
class PeakPair:
    """Represents a pair of peaks from a single particle transit."""
    peak1_idx: int  # Index of first peak
    peak2_idx: int  # Index of second peak
    peak1_time: float  # Time of first peak
    peak2_time: float  # Time of second peak
    peak1_deviation: float  # Frequency deviation at first peak (negative)
    peak2_deviation: float  # Frequency deviation at second peak (negative)
    peak1_depth: float  # Peak depth (magnitude of deviation)
    peak2_depth: float  # Peak depth (magnitude of deviation)
    separation_time: float  # Time between peaks
    max_deviation: float  # Maximum deviation magnitude (for mass calculation)
    quality_score: float = 0.0  # Quality metric for peak pair
    # Additional fields from reference implementation
    node_dev: float = 0.0  # Node deviation signal
    antinode_diff_raw: float = 0.0  # Difference between antinodes
    mass_raw: float = 0.0  # Raw mass estimate
    mass_raw1: float = 0.0  # Mass from first antinode
    mass_raw2: float = 0.0  # Mass from second antinode
    baseline_noise: float = 0.0  # Baseline noise (RMS)
    peak_noise: float = 0.0  # Peak noise (RMS)
    baseline: float = 0.0  # Baseline frequency (Hz)
    baseline_slope: float = 0.0  # Baseline slope (Hz/s)
    gap: float = 0.0  # Gap between antinodes (samples)


@dataclass
class PeaksStructure:
    """Peaks structure matching R's Peaks object from getAllPeaks()."""
    peak_pairs: List[PeakPair] = field(default_factory=list)  # List of detected peak pairs
    peak_indices: List[int] = field(default_factory=list)  # All peak indices (flattened)
    peak_times: np.ndarray = field(default_factory=lambda: np.array([]))  # All peak times
    peak_deviations: np.ndarray = field(default_factory=lambda: np.array([]))  # All peak deviations
    chunk_indices: List[Tuple[int, int]] = field(default_factory=list)  # Chunk boundaries for each peak pair


@dataclass
class CalibratedPeaksStructure:
    """Calibrated peaks structure matching R's psPeaks object from calibrateSensors()."""
    peak_pairs: List[PeakPair] = field(default_factory=list)  # Calibrated peak pairs
    calibrated_deviations: np.ndarray = field(default_factory=lambda: np.array([]))  # Calibrated deviation values
    mass_values: Optional[np.ndarray] = None  # Mass values if calibration includes mass conversion
    calibration_factors: Dict[str, float] = field(default_factory=dict)  # Calibration parameters used


def _fast_running_quantile(data: np.ndarray, window_size: int, quantile: float) -> np.ndarray:
    """
    Fast running quantile calculation using optimized approach.
    Uses pandas rolling quantile if available (much faster), otherwise optimized manual calculation.
    """
    if window_size >= len(data):
        # Window larger than data, return single quantile
        return np.full(len(data), np.quantile(data, quantile))
    
    # Try using polars rolling quantile (much faster)
    try:
        import polars as pl
        series = pl.Series(data)
        rolling_quantile = series.rolling_quantile(quantile=quantile, window_size=window_size, center=True, min_periods=1)
        return rolling_quantile.to_numpy()
    except ImportError:
        # Fallback: optimized manual calculation
        pass
    
    # Optimized manual calculation using stride tricks where possible
    # For very large datasets, use a more efficient approach
    if len(data) > 100000:
        # Use a step-based approach to reduce computation
        step = max(1, window_size // 10)  # Process every Nth point, then interpolate
        indices = np.arange(0, len(data), step)
        quantile_values = np.zeros(len(indices))
        
        half_window = window_size // 2
        for idx, i in enumerate(indices):
            start = max(0, i - half_window)
            end = min(len(data), i + half_window + 1)
            window_data = data[start:end]
            quantile_values[idx] = np.quantile(window_data, quantile)
        
        # Interpolate to full resolution
        if len(indices) > 1:
            return np.interp(np.arange(len(data)), indices, quantile_values)
        else:
            return np.full(len(data), quantile_values[0])
    else:
        # For smaller datasets, calculate directly but efficiently
        quantile_values = np.zeros(len(data))
        half_window = window_size // 2
        
        for i in range(len(data)):
            start = max(0, i - half_window)
            end = min(len(data), i + half_window + 1)
            window_data = data[start:end]
            quantile_values[i] = np.quantile(window_data, quantile)
        
        return quantile_values


def estimate_baseline_rolling_median(
    data: np.ndarray,
    data_rate: float,
    window_size_seconds: float = 0.25
) -> np.ndarray:
    """
    Estimate baseline using rolling median - a simple and robust method.
    
    Args:
        data: Raw frequency data
        data_rate: Data sampling rate (Hz)
        window_size_seconds: Window size for rolling median in seconds (default: 0.25s)
            
    Returns:
        Estimated baseline array (same length as input)
    """
    if len(data) < 10:
        # Not enough data - return mean
        return np.full_like(data, np.mean(data))
    
    # Calculate window size in samples
    window_size_samples = int(window_size_seconds * data_rate)
    window_size_samples = max(5, window_size_samples)  # Minimum 5 samples
    window_size_samples = min(window_size_samples, len(data) // 2)  # Maximum half the data
    
    if window_size_samples % 2 == 0:
        window_size_samples += 1  # Make odd for better behavior
    
    # Use polars rolling median if available (much faster), otherwise scipy
    try:
        import polars as pl
        series = pl.Series(data)
        baseline = series.rolling_median(window_size=window_size_samples, center=True, min_periods=1).to_numpy()
        return baseline
    except ImportError:
        # Fallback: use scipy.ndimage median filter
        try:
            from scipy.ndimage import median_filter
            baseline = median_filter(data, size=window_size_samples, mode='nearest')
            return baseline
        except ImportError:
            # Final fallback: manual rolling median (slow but works)
            baseline = np.zeros_like(data)
            half_window = window_size_samples // 2
            for i in range(len(data)):
                start_idx = max(0, i - half_window)
                end_idx = min(len(data), i + half_window + 1)
                baseline[i] = np.median(data[start_idx:end_idx])
            return baseline


def filter_data(
    data: np.ndarray,
    data_rate: float,
    filter_settings: Optional[FilterSettings] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Apply bandpass filter to data.
    
    Low-pass: Savitzky-Golay filter
    High-pass: Rolling median baseline removal
    
    Args:
        data: Raw frequency data
        data_rate: Data sampling rate (Hz)
        filter_settings: Filter settings (uses defaults if None)
            
    Returns:
        Tuple of (bandpass filtered data, baseline estimate)
    """
    if filter_settings is None:
        filter_settings = FilterSettings()
    
    # Estimate baseline using rolling median
    baseline_estimate = estimate_baseline_rolling_median(
        data, 
        data_rate, 
        window_size_seconds=filter_settings.quantile_width
    )
    
    # Low-pass filter: Savitzky-Golay (for smoothing)
    if len(data) < filter_settings.sg_length:
        dat_lp = data.copy()
    elif SCIPY_AVAILABLE:
        try:
            sg_length = filter_settings.sg_length
            if sg_length % 2 == 0:
                sg_length += 1
            dat_lp = signal.savgol_filter(
                data,
                window_length=min(sg_length, len(data) - (len(data) % 2)),
                polyorder=filter_settings.sg_order
            )
        except (ValueError, TypeError):
            dat_lp = data.copy()
    else:
        dat_lp = data.copy()
    
    # Bandpass = lowpass - baseline
    dat_bp = dat_lp - baseline_estimate
    
    return dat_bp, baseline_estimate


def read_single_data(
    frequency_data: List[Tuple[float, float]],
    data_rate: float = 20000.0,
    baseline_window_size: int = 50,
    baseline_threshold_std: float = 2.0,
    apply_filtering: bool = False,
    filter_settings: Optional[FilterSettings] = None,
    estimation_settings: Optional[EstimationSettings] = None,
    sample_info: Optional[Dict[str, Any]] = None
) -> DataStructure:
    """
    Equivalent to R's readSingleData() function.
    
    Reads frequency data and creates Data structure with baseline detection and chunk identification.
    
    Args:
        frequency_data: List of (time, frequency) tuples
        data_rate: Data rate in Hz
        baseline_window_size: Number of samples for baseline estimation
        baseline_threshold_std: Number of std devs for baseline detection
        apply_filtering: Whether to apply bandpass/lowpass filtering
        filter_settings: Filter settings (uses defaults if None)
        estimation_settings: Estimation settings for low-pass filter (uses defaults if None)
        sample_info: Optional sample metadata dictionary
        
    Returns:
        DataStructure object containing processed data
    """
    if not frequency_data:
        raise ValueError("frequency_data cannot be empty")
    
    # Convert to numpy arrays
    times = np.array([d[0] for d in frequency_data], dtype=np.float64)
    frequencies = np.array([d[1] for d in frequency_data], dtype=np.float64)
    
    # Create dat array: shape (N, 2) where [:, 0] = time, [:, 1] = frequency
    dat = np.column_stack([times, frequencies])
    
    # Apply filtering if requested
    dat_bp = None
    dat_lp = None
    if apply_filtering and SCIPY_AVAILABLE:
        if filter_settings is None:
            filter_settings = FilterSettings()
        if estimation_settings is None:
            estimation_settings = EstimationSettings()
        
        # Bandpass filter using filter_data()
        dat_hp = None
        try:
            filtered_freqs_bp, baseline_drift = filter_data(frequencies, data_rate, filter_settings)
            dat_bp = np.column_stack([times, filtered_freqs_bp])
            dat_hp = np.column_stack([times, baseline_drift])  # Store baseline drift estimate
        except Exception as e:
            warnings.warn(f"Bandpass filtering failed: {e}")
            dat_bp = None
        
        # Lowpass filter using Savitzky-Golay (for estimation)
        try:
            # Calculate SG length from estimation bandwidth
            sg_length = max(5, int(estimation_settings.bandwidth * data_rate / 10))
            if sg_length % 2 == 0:
                sg_length += 1
            if len(frequencies) >= sg_length:
                filtered_freqs_lp = signal.savgol_filter(
                    frequencies,
                    window_length=min(sg_length, len(frequencies) - (len(frequencies) % 2)),
                    polyorder=2
                )
                dat_lp = np.column_stack([times, filtered_freqs_lp])
            else:
                dat_lp = dat  # Not enough data for filtering
        except Exception as e:
            warnings.warn(f"Lowpass filtering failed: {e}")
    
    # Detect baseline regions
    baseline_mean, baseline_std, baseline_regions, chunks = _detect_baseline_and_chunks(
        frequencies, times, baseline_window_size, baseline_threshold_std
    )
    
    # Create Data structure
    data = DataStructure(
        dat=dat,
        dat_bp=dat_bp,
        dat_lp=dat_lp,
        dat_hp=dat_hp if apply_filtering else None,
        timestamps=times,
        frequencies=frequencies,
        data_rate=data_rate,
        sample_info=sample_info or {},
        baseline_mean=baseline_mean,
        baseline_std=baseline_std,
        baseline_regions=baseline_regions,
        chunks=chunks
    )
    
    return data


def _detect_baseline_and_chunks(
    frequencies: np.ndarray,
    times: np.ndarray,
    window_size: int,
    threshold_std: float
) -> Tuple[float, float, List[Tuple[int, int]], List[Tuple[int, int]]]:
    """
    Detect baseline regions and identify complete chunks (baseline → peak → baseline).
    
    Args:
        frequencies: Frequency array
        times: Time array
        window_size: Window size for baseline estimation
        threshold_std: Number of std devs for baseline threshold
        
    Returns:
        Tuple of (baseline_mean, baseline_std, baseline_regions, chunks)
    """
    n_samples = len(frequencies)
    if n_samples < window_size:
        # Not enough data - use all data as baseline
        baseline_mean = np.mean(frequencies)
        baseline_std = np.std(frequencies)
        baseline_regions = [(0, n_samples - 1)]
        chunks = []
        return baseline_mean, baseline_std, baseline_regions, chunks
    
    # Calculate rolling statistics for baseline estimation
    # Use a simple approach: sliding window mean and std
    baseline_mask = np.ones(n_samples, dtype=bool)
    
    # Estimate baseline from initial window
    initial_baseline_mean = np.mean(frequencies[:window_size])
    initial_baseline_std = np.std(frequencies[:window_size])
    
    # Refine baseline estimate using rolling window
    baseline_means = []
    baseline_stds = []
    
    for i in range(n_samples):
        start_idx = max(0, i - window_size // 2)
        end_idx = min(n_samples, i + window_size // 2)
        window_freqs = frequencies[start_idx:end_idx]
        baseline_means.append(np.mean(window_freqs))
        baseline_stds.append(np.std(window_freqs))
    
    baseline_means = np.array(baseline_means)
    baseline_stds = np.array(baseline_stds)
    
    # Overall baseline statistics
    baseline_mean = np.mean(baseline_means)
    baseline_std = np.mean(baseline_stds)
    
    # Detect baseline regions: frequency within threshold_std * std_dev of mean
    threshold = threshold_std * baseline_std
    baseline_mask = np.abs(frequencies - baseline_mean) <= threshold
    
    # Find contiguous baseline regions
    baseline_regions = []
    in_baseline = False
    start_idx = None
    
    for i, is_baseline in enumerate(baseline_mask):
        if is_baseline and not in_baseline:
            start_idx = i
            in_baseline = True
        elif not is_baseline and in_baseline:
            if start_idx is not None:
                baseline_regions.append((start_idx, i - 1))
            in_baseline = False
            start_idx = None
    
    # Handle case where data ends in baseline
    if in_baseline and start_idx is not None:
        baseline_regions.append((start_idx, n_samples - 1))
    
    # Identify complete chunks: baseline → non-baseline → baseline
    chunks = []
    for i in range(len(baseline_regions) - 1):
        baseline_end = baseline_regions[i][1]
        next_baseline_start = baseline_regions[i + 1][0]
        
        # Check if there's non-baseline data between these baseline regions
        if next_baseline_start > baseline_end + 1:
            # This is a complete chunk
            chunk_start = baseline_end + 1
            chunk_end = next_baseline_start - 1
            chunks.append((chunk_start, chunk_end))
    
    return baseline_mean, baseline_std, baseline_regions, chunks


def _split_doublet_peaks(
    detection_data: np.ndarray,
    peak_starts: List[int],
    peak_ends: List[int],
    peak_heights: List[float],
    peak_centers: List[int],
    t_doublet_gap_min_samples: int
) -> Tuple[List[int], List[int], List[float], List[int]]:
    """
    Split peaks that should be two separate peaks (doublets).
    """
    new_starts = peak_starts.copy()
    new_ends = peak_ends.copy()
    new_heights = peak_heights.copy()
    new_centers = peak_centers.copy()
    
    to_insert = []
    
    for i, height in enumerate(peak_heights):
        start = peak_starts[i]
        end = peak_ends[i]
        peak_region = detection_data[start:end+1]
        
        # Find indices below 0.85 * peak height
        threshold = 0.85 * height
        indices_below = np.where(peak_region < threshold)[0]
        
        if len(indices_below) < 2:
            continue
        
        # Find gaps larger than t_doublet_gap_min
        diffs = np.diff(indices_below)
        gap_locations = np.where(diffs > t_doublet_gap_min_samples)[0]
        
        if len(gap_locations) == 1:
            # Found a gap that suggests two peaks
            new_end_idx = indices_below[gap_locations[0]]
            new_start_idx = indices_below[gap_locations[0] + 1]
            
            to_insert.append((i, start + new_start_idx, start + new_end_idx))
    
    # Insert new peaks (from end to start to preserve indices)
    for i, new_start, new_end in reversed(to_insert):
        if i == len(new_starts) - 1:
            new_starts.append(new_start)
        else:
            new_starts.insert(i + 1, new_start)
        
        if i == 0:
            new_ends.insert(0, new_end)
        else:
            new_ends.insert(i, new_end)
    
    # Recalculate heights and centers
    new_heights = []
    new_centers = []
    for start, end in zip(new_starts, new_ends):
        peak_region = detection_data[start:end+1]
        new_heights.append(np.min(peak_region))
        new_centers.append(start + np.argmin(peak_region))
    
    return new_starts, new_ends, new_heights, new_centers


def _find_doublet_pairs(
    peak_heights: List[float],
    peak_centers: List[int],
    t_doublet_gap_min_samples: int,
    t_doublet_gap_max_samples: int,
    max_height_diff_percent: float
) -> List[int]:
    """
    Find pairs of peaks that form doublets.
    
    Uses percentage-based height difference matching: two peaks can be matched
    if their height difference is <= max_height_diff_percent% of the maximum peak height.
    """
    if len(peak_centers) < 2:
        return []
    
    time_between = np.diff(peak_centers)
    doublet_indices = []
    
    for i in range(len(peak_heights) - 1):
        time_to_next = time_between[i]
        height1 = abs(peak_heights[i])
        height2 = abs(peak_heights[i + 1])
        max_height = max(height1, height2)
        
        # Calculate percentage difference relative to maximum peak height
        if max_height > 0:
            height_diff_percent = abs(height1 - height2) / max_height * 100.0
        else:
            height_diff_percent = 0.0
        
        # Check gap criteria (R uses > and <, not >= and <=)
        if time_to_next <= t_doublet_gap_min_samples:
            continue
        if time_to_next >= t_doublet_gap_max_samples:
            continue
        if height_diff_percent > max_height_diff_percent:
            continue
        
        # Check that next peak is further away
        if i + 1 < len(time_between):
            time_after_next = time_between[i + 1]
            if time_after_next <= time_to_next:
                continue
        
        # All criteria met
        doublet_indices.append(i)
    
    return doublet_indices


def _estimate_baseline(
    data_region: np.ndarray,
    peak1_rel: int,
    peak2_rel: int,
    num_baseline_chunks: int
) -> np.ndarray:
    """Estimate baseline using chunks before and after peaks."""
    chunk_size = len(data_region) // num_baseline_chunks
    
    if chunk_size < 1:
        return np.zeros_like(data_region)
    
    # Calculate chunk standard deviations
    num_chunks = len(data_region) // chunk_size
    chunks = data_region[:num_chunks * chunk_size].reshape(num_chunks, chunk_size)
    chunk_sds = np.std(chunks, axis=1)
    
    # Find baseline chunks
    pre_chunk_idx = np.argmin(chunk_sds[:peak1_rel // chunk_size]) if peak1_rel // chunk_size > 0 else 0
    post_chunk_idx = (peak2_rel // chunk_size + 
                     np.argmin(chunk_sds[peak2_rel // chunk_size:]) 
                     if peak2_rel // chunk_size < len(chunk_sds) else len(chunk_sds) - 1)
    
    # Extract baseline segments
    baseline_indices = np.concatenate([
        np.arange(pre_chunk_idx * chunk_size, (pre_chunk_idx + 1) * chunk_size),
        np.arange(post_chunk_idx * chunk_size, (post_chunk_idx + 1) * chunk_size)
    ])
    
    baseline_indices = baseline_indices[baseline_indices < len(data_region)]
    
    if len(baseline_indices) < 2:
        return np.zeros_like(data_region)
    
    # Fit linear baseline
    t = np.arange(len(data_region))
    baseline_values = data_region[baseline_indices]
    baseline_t = t[baseline_indices]
    
    if len(baseline_values) > 1:
        slope, intercept = np.polyfit(baseline_t, baseline_values, 1)
        baseline = intercept + slope * t
    else:
        baseline = np.full_like(data_region, baseline_values[0] if len(baseline_values) > 0 else 0)
    
    return baseline


def _get_baseline_segments(
    data_region: np.ndarray,
    peak1_rel: int,
    peak2_rel: int,
    num_baseline_chunks: int
) -> np.ndarray:
    """Get baseline segments for noise calculation."""
    chunk_size = len(data_region) // num_baseline_chunks
    
    if chunk_size < 1:
        return np.array([])
    
    num_chunks = len(data_region) // chunk_size
    chunks = data_region[:num_chunks * chunk_size].reshape(num_chunks, chunk_size)
    chunk_sds = np.std(chunks, axis=1)
    
    pre_chunk_idx = np.argmin(chunk_sds[:peak1_rel // chunk_size]) if peak1_rel // chunk_size > 0 else 0
    post_chunk_idx = (peak2_rel // chunk_size + 
                     np.argmin(chunk_sds[peak2_rel // chunk_size:]) 
                     if peak2_rel // chunk_size < len(chunk_sds) else len(chunk_sds) - 1)
    
    baseline_indices = np.concatenate([
        np.arange(pre_chunk_idx * chunk_size, (pre_chunk_idx + 1) * chunk_size),
        np.arange(post_chunk_idx * chunk_size, (post_chunk_idx + 1) * chunk_size)
    ])
    
    baseline_indices = baseline_indices[baseline_indices < len(data_region)]
    
    return data_region[baseline_indices] if len(baseline_indices) > 0 else np.array([])


def _get_baseline_indices(
    data_region: np.ndarray,
    peak1_rel: int,
    peak2_rel: int,
    num_baseline_chunks: int
) -> np.ndarray:
    """Get baseline segment indices for slope calculation."""
    chunk_size = len(data_region) // num_baseline_chunks
    
    if chunk_size < 1:
        return np.array([])
    
    num_chunks = len(data_region) // chunk_size
    chunks = data_region[:num_chunks * chunk_size].reshape(num_chunks, chunk_size)
    chunk_sds = np.std(chunks, axis=1)
    
    pre_chunk_idx = np.argmin(chunk_sds[:peak1_rel // chunk_size]) if peak1_rel // chunk_size > 0 else 0
    post_chunk_idx = (peak2_rel // chunk_size + 
                     np.argmin(chunk_sds[peak2_rel // chunk_size:]) 
                     if peak2_rel // chunk_size < len(chunk_sds) else len(chunk_sds) - 1)
    
    baseline_indices = np.concatenate([
        np.arange(pre_chunk_idx * chunk_size, (pre_chunk_idx + 1) * chunk_size),
        np.arange(post_chunk_idx * chunk_size, (post_chunk_idx + 1) * chunk_size)
    ])
    
    baseline_indices = baseline_indices[baseline_indices < len(data_region)]
    
    return baseline_indices if len(baseline_indices) > 0 else np.array([])


def _estimate_peak_parameters(
    raw_data: np.ndarray,
    estimation_data: np.ndarray,
    detection_data: np.ndarray,
    peak1_center: int,
    peak2_center: int,
    data_rate: float,
    timestamps: np.ndarray,
    estimation_settings: EstimationSettings,
    freq_time: Optional[np.ndarray] = None
) -> Optional[Dict[str, float]]:
    """
    Estimate parameters for a doublet peak.
    Returns a dictionary with peak parameters that can be used to create a PeakPair.
    """
    # Check if we have enough data
    if peak2_center >= len(estimation_data) - data_rate:
        return None
    
    # Convert time-based settings to samples
    baseline_search_width_samples = int(
        round(estimation_settings.baseline_search_width_side_points * data_rate)
    )
    
    # Define region around peaks
    region_start = max(0, peak1_center - baseline_search_width_samples)
    region_end = min(len(estimation_data), peak2_center + baseline_search_width_samples)
    
    data_region = estimation_data[region_start:region_end]
    raw_region = raw_data[region_start:region_end]
    
    if len(data_region) < 10:
        return None
    
    # Relative positions within region
    peak1_rel = baseline_search_width_samples
    peak2_rel = baseline_search_width_samples + (peak2_center - peak1_center)
    
    # Estimate baseline
    baseline = _estimate_baseline(data_region, peak1_rel, peak2_rel, estimation_settings.num_baseline_chunks)
    raw_baseline = _estimate_baseline(raw_region, peak1_rel, peak2_rel, estimation_settings.num_baseline_chunks)
    
    # CRITICAL: Subtract baseline BEFORE extracting antinode regions
    data_region_baseline_subtracted = data_region - baseline
    
    # Calculate tip regions
    tip_points = int(
        estimation_settings.tip_side_point_fraction * (peak2_rel - peak1_rel)
    )
    
    if tip_points < 2:
        return None
    
    # Extract antinode regions from baseline-subtracted data
    antinode1_region = data_region_baseline_subtracted[peak1_rel - tip_points:peak1_rel + tip_points + 1]
    antinode2_region = data_region_baseline_subtracted[peak2_rel - tip_points:peak2_rel + tip_points + 1]
    node_region = data_region_baseline_subtracted[peak1_rel:peak2_rel + 1]
    
    if len(antinode1_region) < 5 or len(antinode2_region) < 5:
        return None
    
    # Fit polynomials to antinodes
    t1 = np.arange(len(antinode1_region))
    t2 = np.arange(len(antinode2_region))
    
    try:
        poly1 = np.polyfit(t1, antinode1_region, min(4, len(t1) - 1))
        poly2 = np.polyfit(t2, antinode2_region, min(4, len(t2) - 1))
        
        fit1 = np.polyval(poly1, t1)
        fit2 = np.polyval(poly2, t2)
        
        tip_rmse = np.sqrt(np.mean(np.concatenate([
            (antinode1_region - fit1)**2,
            (antinode2_region - fit2)**2
        ])))
        
        triplet_height = (np.min(fit1) + np.min(fit2)) / 2
        
        # Calculate gap
        gap = (peak2_rel + np.argmin(fit2) - tip_points) - (peak1_rel + np.argmin(fit1) - tip_points)
        
        # Calculate baseline noise
        baseline_segments = _get_baseline_segments(
            data_region, peak1_rel, peak2_rel, estimation_settings.num_baseline_chunks
        )
        baseline_noise = np.std(baseline_segments - baseline[:len(baseline_segments)]) if len(baseline_segments) > 0 else 0.0
        
        # Calculate time
        if freq_time is not None and peak1_center < len(freq_time):
            time_seconds = freq_time[peak1_center]
        else:
            time_seconds = timestamps[peak1_center] if peak1_center < len(timestamps) else peak1_center / data_rate
        
        # Calculate baseline slope from linear fit
        baseline_segment_indices = _get_baseline_indices(
            data_region, peak1_rel, peak2_rel, estimation_settings.num_baseline_chunks
        )
        if len(baseline_segments) > 1 and len(baseline_segment_indices) == len(baseline_segments):
            try:
                slope, intercept = np.polyfit(baseline_segment_indices, baseline_segments, 1)
                baseline_slope = slope * data_rate  # Convert to Hz/s
            except:
                baseline_slope = 0.0
        else:
            baseline_slope = 0.0
        
        # Calculate mean baseline frequency
        if len(raw_baseline) > max(peak1_rel, peak2_rel):
            mean_baseline = np.mean([raw_baseline[peak1_rel], raw_baseline[peak2_rel]])
        else:
            mean_baseline = np.mean(raw_baseline) if len(raw_baseline) > 0 else 0.0
        
        # Return parameters as dictionary
        return {
            'mass': -triplet_height,  # Convert to positive mass
            'node_dev': np.max(node_region),
            'antinode_diff_raw': np.min(fit2) - np.min(fit1),
            'mass_raw': -triplet_height,
            'mass_raw1': -np.min(fit1),
            'mass_raw2': -np.min(fit2),
            'baseline_noise': baseline_noise,
            'peak_noise': tip_rmse,
            'baseline': mean_baseline,
            'baseline_slope': baseline_slope,
            'gap': gap,
            'time_seconds': time_seconds
        }
        
    except (np.linalg.LinAlgError, ValueError) as e:
        warnings.warn(f"Peak parameter estimation failed: {e}")
        return None


def get_all_peaks(
    data: DataStructure,
    detection_settings: Optional[DetectionSettings] = None,
    estimation_settings: Optional[EstimationSettings] = None,
    chunk_min_baseline_samples: int = 10
) -> PeaksStructure:
    """
    Equivalent to R's getAllPeaks() function.
    
    Detects all peaks in the Data structure using threshold-based detection,
    identifying two near-symmetric negative peaks per particle.
    
    Args:
        data: DataStructure from read_single_data()
        detection_settings: Detection settings (uses defaults if None)
        estimation_settings: Estimation settings (uses defaults if None)
        chunk_min_baseline_samples: Minimum baseline samples before/after peak
        
    Returns:
        PeaksStructure containing detected peaks
    """
    if not SCIPY_AVAILABLE:
        raise RuntimeError("scipy is required for peak detection")
    
    if detection_settings is None:
        detection_settings = DetectionSettings()
    if estimation_settings is None:
        estimation_settings = EstimationSettings()
    
    if data.baseline_mean is None:
        raise ValueError("Data structure must have baseline_mean calculated")
    
    # Use bandpass filtered data for detection if available, otherwise use raw deviations
    if data.dat_bp is not None:
        # Use bandpass filtered data - the bandpass filter removes baseline drift
        # but we still need to ensure it's centered around zero for detection
        bp_frequencies = data.dat_bp[:, 1]  # Extract frequency column
        # The bandpass filter should center around zero, but check and center if needed
        bp_mean = np.mean(bp_frequencies)
        if abs(bp_mean) > 0.1:  # If significantly off-center, subtract mean
            detection_data = bp_frequencies - bp_mean
        else:
            detection_data = bp_frequencies
    else:
        # Calculate frequency deviation: deviation = frequency - baseline_mean
        detection_data = data.frequencies - data.baseline_mean
    
    # Use low-pass filtered data for estimation if available
    if data.dat_lp is not None:
        estimation_data = data.dat_lp[:, 1]  # Extract frequency column
    else:
        estimation_data = data.frequencies
    
    raw_data = data.frequencies
    
    # Convert time-based settings to samples
    t_doublet_gap_min_samples = int(round(detection_settings.t_doublet_gap_min * data.data_rate))
    t_doublet_gap_max_samples = int(round(detection_settings.t_doublet_gap_max * data.data_rate))
    
    # Find indices below threshold
    # The threshold is negative (e.g., -4.0 Hz), so we're looking for negative deviations
    threshold = detection_settings.detection_threshold
    
    indices_below = np.where(detection_data < threshold)[0]
    
    if len(indices_below) == 0:
        # Debug: check what the actual range is
        if len(detection_data) > 0:
            min_val = np.min(detection_data)
            max_val = np.max(detection_data)
            if min_val > threshold:
                # No data below threshold - might need to adjust threshold or check filtering
                warnings.warn(f"No peaks detected: detection_data range [{min_val:.2f}, {max_val:.2f}], threshold={threshold:.2f}")
        return PeaksStructure()
    
    # Find contiguous segments (peaks)
    peak_starts = []
    peak_ends = []
    
    if len(indices_below) > 0:
        # Find start indices (where gap > 1)
        diffs = np.diff(indices_below)
        gap_indices = np.where(diffs > 1)[0]
        peak_starts = [indices_below[0]]
        peak_starts.extend(indices_below[gap_indices + 1])
        
        # Find end indices
        peak_ends = list(indices_below[gap_indices])
        peak_ends.append(indices_below[-1])
    
    if len(peak_starts) == 0:
        return PeaksStructure()
    
    # Calculate peak heights and centers
    peak_heights = []
    peak_centers = []
    
    for start, end in zip(peak_starts, peak_ends):
        peak_region = detection_data[start:end+1]
        peak_height = np.min(peak_region)
        peak_center = start + np.argmin(peak_region)
        peak_heights.append(peak_height)
        peak_centers.append(peak_center)
    
    # Detect doublet peaks (split peaks that should be two)
    peak_starts, peak_ends, peak_heights, peak_centers = _split_doublet_peaks(
        detection_data, peak_starts, peak_ends, peak_heights, peak_centers,
        t_doublet_gap_min_samples
    )
    
    # Find doublet pairs
    doublet_indices = _find_doublet_pairs(
        peak_heights, peak_centers,
        t_doublet_gap_min_samples, t_doublet_gap_max_samples,
        detection_settings.max_height_diff_percent
    )
    
    if len(doublet_indices) == 0:
        return PeaksStructure()
    
    # Estimate peak parameters and create peak pairs
    peak_pairs = []
    all_peak_indices = []
    chunk_indices = []
    
    # Create freq_time array for timing (simplified - use timestamps)
    freq_time = data.timestamps
    
    for idx in doublet_indices:
        if idx + 1 >= len(peak_centers):
            continue
        
        peak1_center = peak_centers[idx]
        peak2_center = peak_centers[idx + 1]
        
        # Estimate peak parameters
        peak_params = _estimate_peak_parameters(
            raw_data,
            estimation_data,
            detection_data,
            peak1_center,
            peak2_center,
            data.data_rate,
            data.timestamps,
            estimation_settings,
            freq_time=freq_time
        )
        
        if peak_params is None:
            continue
        
        # Get peak times
        peak1_time = peak_params['time_seconds']
        peak2_time = peak1_time + (peak2_center - peak1_center) / data.data_rate
        
        # Calculate deviations
        peak1_deviation = detection_data[peak1_center] if peak1_center < len(detection_data) else 0.0
        peak2_deviation = detection_data[peak2_center] if peak2_center < len(detection_data) else 0.0
        
        # Calculate peak depths
        peak1_depth = abs(peak1_deviation)
        peak2_depth = abs(peak2_deviation)
        max_deviation = max(peak1_depth, peak2_depth)
        
        # Quality score based on symmetry
        symmetry_score = 1.0 - abs(peak1_depth - peak2_depth) / max(max_deviation, 1e-10)
        quality_score = symmetry_score
        
        # Find which chunk this peak pair belongs to
        chunk_idx = None
        for i, (chunk_start, chunk_end) in enumerate(data.chunks):
            if chunk_start <= peak1_center <= chunk_end and chunk_start <= peak2_center <= chunk_end:
                chunk_idx = (chunk_start, chunk_end)
                break
        
        peak_pair = PeakPair(
            peak1_idx=peak1_center,
            peak2_idx=peak2_center,
            peak1_time=peak1_time,
            peak2_time=peak2_time,
            peak1_deviation=peak1_deviation,
            peak2_deviation=peak2_deviation,
            peak1_depth=peak1_depth,
            peak2_depth=peak2_depth,
            separation_time=peak2_time - peak1_time,
            max_deviation=max_deviation,
            quality_score=quality_score,
            node_dev=peak_params['node_dev'],
            antinode_diff_raw=peak_params['antinode_diff_raw'],
            mass_raw=peak_params['mass_raw'],
            mass_raw1=peak_params['mass_raw1'],
            mass_raw2=peak_params['mass_raw2'],
            baseline_noise=peak_params['baseline_noise'],
            peak_noise=peak_params['peak_noise'],
            baseline=peak_params['baseline'],
            baseline_slope=peak_params['baseline_slope'],
            gap=peak_params['gap']
        )
        
        peak_pairs.append(peak_pair)
        all_peak_indices.extend([peak1_center, peak2_center])
        if chunk_idx:
            chunk_indices.append(chunk_idx)
    
    # Post-detection filtering (matching R code)
    if len(peak_pairs) > 0:
        threshold_abs = abs(detection_settings.detection_threshold)
        max_antinode_diff = 4.0  # Maximum allowed antinode difference (Hz)
        min_magnitude = threshold_abs  # Minimum peak magnitude = detection threshold
        min_node_dev = detection_settings.node_dev_threshold  # Minimum node deviation
        
        # Filter peaks
        # Note: R code filters out peaks where node_dev < threshold (removes doublets/negative node dev)
        # So we keep peaks where node_dev >= threshold
        filtered_peaks = [
            peak for peak in peak_pairs
            if abs(peak.antinode_diff_raw) <= max_antinode_diff
            and abs(peak.mass_raw) >= min_magnitude  # Use absolute value for mass
            and peak.node_dev >= min_node_dev  # Keep if node_dev >= threshold
        ]
        
        peak_pairs = filtered_peaks
        # Update indices
        all_peak_indices = []
        chunk_indices = []
        for peak_pair in peak_pairs:
            all_peak_indices.extend([peak_pair.peak1_idx, peak_pair.peak2_idx])
            # Find chunk for this peak pair
            for chunk_start, chunk_end in data.chunks:
                if chunk_start <= peak_pair.peak1_idx <= chunk_end and chunk_start <= peak_pair.peak2_idx <= chunk_end:
                    chunk_indices.append((chunk_start, chunk_end))
                    break
    
    # Create PeaksStructure
    if peak_pairs:
        peak_times = np.array([p.peak1_time for p in peak_pairs] + [p.peak2_time for p in peak_pairs])
        peak_deviations = np.array([p.peak1_deviation for p in peak_pairs] + [p.peak2_deviation for p in peak_pairs])
    else:
        peak_times = np.array([])
        peak_deviations = np.array([])
    
    peaks = PeaksStructure(
        peak_pairs=peak_pairs,
        peak_indices=all_peak_indices,
        peak_times=peak_times,
        peak_deviations=peak_deviations,
        chunk_indices=chunk_indices
    )
    
    return peaks


def calibrate_sensors(
    data: DataStructure,
    peaks: PeaksStructure,
    calibration_factors: Optional[Dict[str, float]] = None
) -> CalibratedPeaksStructure:
    """
    Equivalent to R's calibrateSensors() function.
    
    Calibrates detected peaks using calibration parameters.
    
    Args:
        data: DataStructure from read_single_data()
        peaks: PeaksStructure from get_all_peaks()
        calibration_factors: Optional calibration parameters (scaling, offset, etc.)
        
    Returns:
        CalibratedPeaksStructure containing calibrated peaks
    """
    if calibration_factors is None:
        calibration_factors = {}
    
    # Default calibration: identity (no change)
    scale_factor = calibration_factors.get('scale', 1.0)
    offset = calibration_factors.get('offset', 0.0)
    mass_conversion_factor = calibration_factors.get('mass_conversion', None)
    
    # Create calibrated peak pairs
    calibrated_pairs = []
    calibrated_deviations = []
    mass_values = [] if mass_conversion_factor else None
    
    for peak_pair in peaks.peak_pairs:
        # Apply calibration to deviations
        calibrated_dev1 = peak_pair.peak1_deviation * scale_factor + offset
        calibrated_dev2 = peak_pair.peak2_deviation * scale_factor + offset
        
        calibrated_deviations.extend([calibrated_dev1, calibrated_dev2])
        
        # Calculate calibrated max deviation
        calibrated_max_deviation = max(abs(calibrated_dev1), abs(calibrated_dev2))
        
        # Create calibrated peak pair (preserve all fields from original)
        calibrated_pair = PeakPair(
            peak1_idx=peak_pair.peak1_idx,
            peak2_idx=peak_pair.peak2_idx,
            peak1_time=peak_pair.peak1_time,
            peak2_time=peak_pair.peak2_time,
            peak1_deviation=calibrated_dev1,
            peak2_deviation=calibrated_dev2,
            peak1_depth=abs(calibrated_dev1),
            peak2_depth=abs(calibrated_dev2),
            separation_time=peak_pair.separation_time,
            max_deviation=calibrated_max_deviation,
            quality_score=peak_pair.quality_score,
            # Preserve additional fields
            node_dev=peak_pair.node_dev,
            antinode_diff_raw=peak_pair.antinode_diff_raw,
            mass_raw=peak_pair.mass_raw,
            mass_raw1=peak_pair.mass_raw1,
            mass_raw2=peak_pair.mass_raw2,
            baseline_noise=peak_pair.baseline_noise,
            peak_noise=peak_pair.peak_noise,
            baseline=peak_pair.baseline,
            baseline_slope=peak_pair.baseline_slope,
            gap=peak_pair.gap
        )
        calibrated_pairs.append(calibrated_pair)
        
        # Calculate mass if conversion factor provided
        if mass_conversion_factor:
            mass = calibrated_max_deviation * mass_conversion_factor
            mass_values.append(mass)
    
    calibrated_deviations = np.array(calibrated_deviations)
    if mass_values:
        mass_values = np.array(mass_values)
    
    ps_peaks = CalibratedPeaksStructure(
        peak_pairs=calibrated_pairs,
        calibrated_deviations=calibrated_deviations,
        mass_values=mass_values,
        calibration_factors=calibration_factors
    )
    
    return ps_peaks


class FrequencyAnalysisModule:
    """
    Main analysis module that orchestrates the R-equivalent pipeline.
    
    Manages real-time data stream and coordinates: readSingleData → getAllPeaks → calibrateSensors
    """
    
    def __init__(
        self,
        baseline_window_size: int = 50,
        baseline_threshold_std: float = 2.0,
        peak_min_depth: Optional[float] = None,  # Deprecated, use detection_settings
        peak_min_distance: Optional[int] = None,  # Deprecated, use detection_settings
        peak_min_prominence: Optional[float] = None,  # Deprecated, use detection_settings
        peak_expected_separation: Optional[float] = None,  # Deprecated, use detection_settings
        chunk_min_baseline_samples: int = 10,
        data_rate: float = 20000.0,
        calibration_factors: Optional[Dict[str, float]] = None,
        max_buffer_size: int = 10000,
        detection_settings: Optional[DetectionSettings] = None,
        estimation_settings: Optional[EstimationSettings] = None,
        filter_settings: Optional[FilterSettings] = None
    ):
        """
        Initialize the frequency analysis module.
        
        Args:
            baseline_window_size: Number of samples for baseline estimation
            baseline_threshold_std: Number of std devs for baseline detection
            peak_min_depth: Deprecated - use detection_settings.detection_threshold
            peak_min_distance: Deprecated - use detection_settings
            peak_min_prominence: Deprecated - use detection_settings
            peak_expected_separation: Deprecated - use detection_settings
            chunk_min_baseline_samples: Minimum baseline samples before/after peak
            data_rate: Data rate in Hz
            calibration_factors: Calibration parameters dictionary
            max_buffer_size: Maximum number of data points to keep in buffer
            detection_settings: Detection settings (uses defaults if None)
            estimation_settings: Estimation settings (uses defaults if None)
            filter_settings: Filter settings (uses defaults if None)
        """
        self.baseline_window_size = baseline_window_size
        self.baseline_threshold_std = baseline_threshold_std
        self.chunk_min_baseline_samples = chunk_min_baseline_samples
        self.data_rate = data_rate
        self.calibration_factors = calibration_factors or {}
        self.max_buffer_size = max_buffer_size
        
        # Use new settings structure with backward compatibility
        if detection_settings is None:
            detection_settings = DetectionSettings()
            # Backward compatibility: override with old parameters if provided
            if peak_min_depth is not None:
                detection_settings.detection_threshold = -abs(peak_min_depth)  # Convert to negative threshold
        self.detection_settings = detection_settings
        
        if estimation_settings is None:
            estimation_settings = EstimationSettings()
        self.estimation_settings = estimation_settings
        
        if filter_settings is None:
            filter_settings = FilterSettings()
        self.filter_settings = filter_settings
        
        # Store old parameters for backward compatibility (deprecated)
        self.peak_min_depth = abs(detection_settings.detection_threshold) if peak_min_depth is None else peak_min_depth
        self.peak_min_distance = peak_min_distance or 100
        self.peak_min_prominence = peak_min_prominence or 5.0
        self.peak_expected_separation = peak_expected_separation or 0.01
        
        # Data buffer: deque of (time, frequency) tuples
        self.data_buffer = deque(maxlen=max_buffer_size)
        
        # Analysis results
        self.current_data: Optional[DataStructure] = None
        self.current_peaks: Optional[PeaksStructure] = None
        self.current_ps_peaks: Optional[CalibratedPeaksStructure] = None
        
        # Throughput tracking
        self.peak_timestamps = deque()  # Timestamps of detected peaks (for throughput calculation)
        self.throughput_bin_size = 30.0  # 30 seconds
        
    def add_data_point(self, time: float, frequency: float):
        """
        Add a single data point to the analysis buffer.
        
        Args:
            time: Timestamp in seconds
            frequency: Frequency value in Hz
        """
        self.data_buffer.append((time, frequency))
    
    def add_packet(self, frequencies: List[float], packet_timestamp: float, data_rate: Optional[float] = None):
        """
        Add a packet of frequencies to the analysis buffer.
        
        Args:
            frequencies: List of frequency values
            packet_timestamp: Timestamp of the packet (time of last frequency)
            data_rate: Optional data rate override
        """
        if data_rate is not None:
            self.data_rate = data_rate
        
        if not frequencies:
            return
        
        # Calculate time step
        time_step = 1.0 / self.data_rate if self.data_rate > 0 else 5e-5
        
        # Add each frequency with its calculated timestamp
        num_freqs = len(frequencies)
        for i, freq in enumerate(frequencies):
            # Packet timestamp corresponds to the last frequency
            # Calculate time for this frequency: packet_timestamp - ((num_freqs - 1 - i) * time_step)
            freq_time = packet_timestamp - ((num_freqs - 1 - i) * time_step)
            self.add_data_point(freq_time, freq)
    
    def process(self, min_samples: int = 1000) -> bool:
        """
        Run analysis on current buffer.
        
        Args:
            min_samples: Minimum number of samples required for processing
            
        Returns:
            True if processing was successful, False otherwise
        """
        if len(self.data_buffer) < min_samples:
            return False
        
        try:
            # Convert buffer to list for processing
            frequency_data = list(self.data_buffer)
            
            # Step 1: readSingleData()
            self.current_data = read_single_data(
                frequency_data,
                data_rate=self.data_rate,
                baseline_window_size=self.baseline_window_size,
                baseline_threshold_std=self.baseline_threshold_std,
                apply_filtering=True,  # Enable filtering to generate dat_bp and dat_lp
                filter_settings=self.filter_settings,
                estimation_settings=self.estimation_settings,
                sample_info={}
            )
            
            # Step 2: getAllPeaks()
            self.current_peaks = get_all_peaks(
                self.current_data,
                detection_settings=self.detection_settings,
                estimation_settings=self.estimation_settings,
                chunk_min_baseline_samples=self.chunk_min_baseline_samples
            )
            
            # Step 3: calibrateSensors()
            self.current_ps_peaks = calibrate_sensors(
                self.current_data,
                self.current_peaks,
                calibration_factors=self.calibration_factors
            )
            
            # Update peak timestamps for throughput calculation
            for peak_pair in self.current_ps_peaks.peak_pairs:
                # Use peak1_time as the timestamp for the particle transit
                self.peak_timestamps.append(peak_pair.peak1_time)
            
            return True
            
        except Exception as e:
            print(f"Error in frequency analysis processing: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_results(self) -> Tuple[Optional[DataStructure], Optional[PeaksStructure], Optional[CalibratedPeaksStructure]]:
        """
        Retrieve latest analysis results.
        
        Returns:
            Tuple of (Data, Peaks, psPeaks) structures
        """
        return self.current_data, self.current_peaks, self.current_ps_peaks
    
    def calculate_throughput(self, current_time: Optional[float] = None) -> List[Tuple[float, float]]:
        """
        Calculate throughput (peaks/hour) in 30-second bins.
        
        Args:
            current_time: Current time for binning (if None, use latest peak time)
            
        Returns:
            List of (time, throughput) tuples for each bin
        """
        if not self.peak_timestamps:
            return []
        
        # Convert to list
        peak_times = list(self.peak_timestamps)
        
        if not peak_times:
            return []
        
        if current_time is None:
            current_time = max(peak_times)
        
        # Find time range
        min_time = min(peak_times)
        max_time = max(max(peak_times), current_time)
        
        # Create bins
        bins = []
        bin_start = min_time
        bin_size = self.throughput_bin_size
        
        while bin_start < max_time:
            bin_end = bin_start + bin_size
            bin_center = bin_start + bin_size / 2.0
            
            # Count peaks in this bin
            peak_count = sum(1 for t in peak_times if bin_start <= t < bin_end)
            
            # Convert to peaks/hour
            throughput = (peak_count / bin_size) * 3600.0
            
            bins.append((bin_center, throughput))
            bin_start = bin_end
        
        return bins
    
    def save_to_hdf5(
        self,
        file_path: str,
        sample_info: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        analysis_settings: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Save analysis results to HDF5 file matching R code structure.
        
        Args:
            file_path: Path to HDF5 output file
            sample_info: Sample metadata dictionary
            metadata: Experiment metadata dictionary
            analysis_settings: Analysis settings dictionary
            
        Returns:
            True if save was successful, False otherwise
        """
        if not H5PY_AVAILABLE:
            print("Warning: h5py not available. Cannot save HDF5 file.")
            return False
        
        if self.current_data is None or self.current_peaks is None or self.current_ps_peaks is None:
            print("Warning: No analysis results to save.")
            return False
        
        try:
            with h5py.File(file_path, 'w') as f:
                # /Sample group
                sample_group = f.create_group('Sample')
                sample_dict = sample_info or {}
                for key, value in sample_dict.items():
                    if isinstance(value, str):
                        sample_group.attrs[key] = value
                    elif isinstance(value, (int, float)):
                        sample_group.attrs[key] = value
                    elif isinstance(value, (list, tuple)):
                        sample_group.create_dataset(key, data=np.array(value))
                
                # /AnalysisSettings group
                settings_group = f.create_group('AnalysisSettings')
                settings_dict = analysis_settings or {}
                settings_dict.update({
                    'baseline_window_size': self.baseline_window_size,
                    'baseline_threshold_std': self.baseline_threshold_std,
                    'chunk_min_baseline_samples': self.chunk_min_baseline_samples,
                    'data_rate': self.data_rate,
                    'detection_threshold': self.detection_settings.detection_threshold,
                    't_doublet_gap_min': self.detection_settings.t_doublet_gap_min,
                    't_doublet_gap_max': self.detection_settings.t_doublet_gap_max,
                    'max_height_diff_percent': self.detection_settings.max_height_diff_percent,
                    'node_dev_threshold': self.detection_settings.node_dev_threshold
                })
                for key, value in settings_dict.items():
                    if isinstance(value, str):
                        settings_group.attrs[key] = value
                    elif isinstance(value, (int, float)):
                        settings_group.attrs[key] = value
                    elif isinstance(value, (list, tuple)):
                        settings_group.create_dataset(key, data=np.array(value))
                
                # /MetaData group
                metadata_group = f.create_group('MetaData')
                metadata_dict = metadata or {}
                for key, value in metadata_dict.items():
                    if isinstance(value, str):
                        metadata_group.attrs[key] = value
                    elif isinstance(value, (int, float)):
                        metadata_group.attrs[key] = value
                    elif isinstance(value, (list, tuple)):
                        metadata_group.create_dataset(key, data=np.array(value))
                
                # /sumData group (summary data without large arrays)
                sum_data_group = f.create_group('sumData')
                sum_data_group.attrs['baseline_mean'] = self.current_data.baseline_mean or 0.0
                sum_data_group.attrs['baseline_std'] = self.current_data.baseline_std or 0.0
                sum_data_group.attrs['data_rate'] = self.current_data.data_rate
                sum_data_group.attrs['num_samples'] = len(self.current_data.frequencies)
                sum_data_group.create_dataset('timestamps', data=self.current_data.timestamps)
                sum_data_group.create_dataset('frequencies', data=self.current_data.frequencies)
                
                # /Peaks group
                peaks_group = f.create_group('Peaks')
                peaks_group.attrs['num_peak_pairs'] = len(self.current_peaks.peak_pairs)
                
                if self.current_peaks.peak_pairs:
                    # Store peak pair data
                    peak1_indices = [p.peak1_idx for p in self.current_peaks.peak_pairs]
                    peak2_indices = [p.peak2_idx for p in self.current_peaks.peak_pairs]
                    peak1_times = [p.peak1_time for p in self.current_peaks.peak_pairs]
                    peak2_times = [p.peak2_time for p in self.current_peaks.peak_pairs]
                    peak1_deviations = [p.peak1_deviation for p in self.current_peaks.peak_pairs]
                    peak2_deviations = [p.peak2_deviation for p in self.current_peaks.peak_pairs]
                    separation_times = [p.separation_time for p in self.current_peaks.peak_pairs]
                    max_deviations = [p.max_deviation for p in self.current_peaks.peak_pairs]
                    quality_scores = [p.quality_score for p in self.current_peaks.peak_pairs]
                    
                    peaks_group.create_dataset('peak1_indices', data=np.array(peak1_indices))
                    peaks_group.create_dataset('peak2_indices', data=np.array(peak2_indices))
                    peaks_group.create_dataset('peak1_times', data=np.array(peak1_times))
                    peaks_group.create_dataset('peak2_times', data=np.array(peak2_times))
                    peaks_group.create_dataset('peak1_deviations', data=np.array(peak1_deviations))
                    peaks_group.create_dataset('peak2_deviations', data=np.array(peak2_deviations))
                    peaks_group.create_dataset('separation_times', data=np.array(separation_times))
                    peaks_group.create_dataset('max_deviations', data=np.array(max_deviations))
                    peaks_group.create_dataset('quality_scores', data=np.array(quality_scores))
                
                # /psPeaks group (calibrated peaks)
                ps_peaks_group = f.create_group('psPeaks')
                ps_peaks_group.attrs['num_peak_pairs'] = len(self.current_ps_peaks.peak_pairs)
                
                if self.current_ps_peaks.peak_pairs:
                    # Store calibrated peak pair data
                    ps_peak1_indices = [p.peak1_idx for p in self.current_ps_peaks.peak_pairs]
                    ps_peak2_indices = [p.peak2_idx for p in self.current_ps_peaks.peak_pairs]
                    ps_peak1_times = [p.peak1_time for p in self.current_ps_peaks.peak_pairs]
                    ps_peak2_times = [p.peak2_time for p in self.current_ps_peaks.peak_pairs]
                    ps_peak1_deviations = [p.peak1_deviation for p in self.current_ps_peaks.peak_pairs]
                    ps_peak2_deviations = [p.peak2_deviation for p in self.current_ps_peaks.peak_pairs]
                    ps_separation_times = [p.separation_time for p in self.current_ps_peaks.peak_pairs]
                    ps_max_deviations = [p.max_deviation for p in self.current_ps_peaks.peak_pairs]
                    ps_quality_scores = [p.quality_score for p in self.current_ps_peaks.peak_pairs]
                    
                    ps_peaks_group.create_dataset('peak1_indices', data=np.array(ps_peak1_indices))
                    ps_peaks_group.create_dataset('peak2_indices', data=np.array(ps_peak2_indices))
                    ps_peaks_group.create_dataset('peak1_times', data=np.array(ps_peak1_times))
                    ps_peaks_group.create_dataset('peak2_times', data=np.array(ps_peak2_times))
                    ps_peaks_group.create_dataset('peak1_deviations', data=np.array(ps_peak1_deviations))
                    ps_peaks_group.create_dataset('peak2_deviations', data=np.array(ps_peak2_deviations))
                    ps_peaks_group.create_dataset('separation_times', data=np.array(ps_separation_times))
                    ps_peaks_group.create_dataset('max_deviations', data=np.array(ps_max_deviations))
                    ps_peaks_group.create_dataset('quality_scores', data=np.array(ps_quality_scores))
                    
                    # Store calibration factors
                    calib_group = ps_peaks_group.create_group('calibration_factors')
                    for key, value in self.current_ps_peaks.calibration_factors.items():
                        if isinstance(value, (int, float)):
                            calib_group.attrs[key] = value
                    
                    # Store mass values if available
                    if self.current_ps_peaks.mass_values is not None:
                        ps_peaks_group.create_dataset('mass_values', data=self.current_ps_peaks.mass_values)
            
            return True
            
        except Exception as e:
            print(f"Error saving HDF5 file: {e}")
            import traceback
            traceback.print_exc()
            return False
