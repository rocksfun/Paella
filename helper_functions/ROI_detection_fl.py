"""
FL ROI Detection Module for Center-of-Mass-Based Quadrant Detection.

This module provides a function for detecting ROI boundaries in fluorescent images using
a center-of-mass-based quadrant approach. The algorithm binarizes the image, calculates
the center of mass, divides the image into quadrants based on the center of mass, and
uses derivative-based edge detection to identify the four corners of the ROI rectangle.
"""

import numpy as np
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


def detect_fl_roi_center_mass_quadrants(image_array, roi_width, roi_height,
                                        derivative_threshold=None, 
                                        min_change_ratio=0.1, 
                                        smoothing_window=5, 
                                        verbose=False,
                                        manual_threshold=None,
                                        return_debug_info=False):
    """
    Detect ROI from fluorescent image using center-of-mass-based quadrant approach.
    
    The algorithm:
    1. Binarizes the fluorescent image using Otsu's algorithm or manual threshold
    2. Calculates center of mass of the binarized image
    3. Splits image into four quarters divided by center of mass (not image center)
    4. For each quadrant:
       - Computes row sums (sum along columns) and column sums (sum along rows)
       - Computes derivatives of these sums to find areas of largest change
       - Identifies peaks in derivatives to locate edges
       - Identifies one corner (two edges) within each quadrant
    5. Detects left/right edges from column sums and top/bottom edges from binarized image
    6. Combines the four corners to form a rectangle
    7. Calculates centroid, ROI bounds (centered on centroid), and angle
    
    Args:
        image_array: numpy array of the image (grayscale or color)
        roi_width: Desired ROI width (from cameraConfig)
        roi_height: Desired ROI height (from cameraConfig)
        derivative_threshold: Minimum derivative magnitude to consider an edge (None for auto)
        min_change_ratio: Minimum ratio of max derivative to consider an edge (default: 0.1)
        smoothing_window: Window size for smoothing row/column sums before derivative (default: 5)
        verbose: Print debug information (default: False)
        manual_threshold: Manual threshold value for binarization (None for Otsu automatic)
        return_debug_info: If True, return debug information dictionary (default: False)
    
    Returns:
        tuple: ((centroid_x, centroid_y, roi_x, roi_y, roi_width, roi_height, angle_deg), rectangle_vertices) 
               or (None, None) if detection fails
               If return_debug_info=True, returns (result_tuple, rectangle_vertices, debug_info)
        rectangle_vertices: 4 vertices of the detected rectangle (top-left, top-right, bottom-right, bottom-left)
        angle_deg: Angle of horizontal channel relative to perfectly horizontal (degrees)
    """
    if not CV2_AVAILABLE:
        if verbose:
            print("OpenCV not available for FL ROI detection")
        if return_debug_info:
            return None, None, {'error': 'OpenCV not available'}
        return None, None
    
    if image_array is None or image_array.size == 0:
        if verbose:
            print("Empty image array")
        if return_debug_info:
            return None, None, {'error': 'Empty image array'}
        return None, None
    
    # Convert to grayscale if needed
    if len(image_array.shape) == 3:
        if image_array.shape[2] == 3:
            gray = np.mean(image_array, axis=2).astype(np.uint8)
        elif image_array.shape[2] == 4:
            gray = np.mean(image_array[:, :, :3], axis=2).astype(np.uint8)
        else:
            if verbose:
                print(f"Unsupported image shape: {image_array.shape}")
            if return_debug_info:
                return None, None, {'error': f'Unsupported image shape: {image_array.shape}', 'binary_image': None, 'quadrants': []}
            return None, None
    else:
        gray = image_array.copy()
        # Ensure uint8 type
        if gray.dtype != np.uint8:
            # Normalize to 0-255 range if needed
            if gray.max() <= 1.0:
                gray = (gray * 255).astype(np.uint8)
            else:
                gray = gray.astype(np.uint8)
    
    img_height, img_width = gray.shape[:2]
    
    if verbose:
        print(f"Image dimensions: {img_width} x {img_height}")
        print(f"Image dtype: {gray.dtype}, min: {gray.min()}, max: {gray.max()}")
    
    # Apply Gaussian blur to reduce noise before thresholding
    gray_blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    
    # Apply thresholding to binarize (bright objects on dark background)
    if manual_threshold is not None:
        threshold_val = float(manual_threshold)
        threshold_val = max(0.0, min(255.0, threshold_val))
        ret, binary = cv2.threshold(gray_blurred, threshold_val, 255, cv2.THRESH_BINARY)
        if verbose:
            print(f"Manual threshold: {threshold_val}, return value: {ret}")
    else:
        threshold_val, binary = cv2.threshold(gray_blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if verbose:
            print(f"Otsu threshold: {threshold_val}")
    
    if verbose:
        unique_vals = np.unique(binary)
        print(f"Binary image unique values: {unique_vals}")
        print(f"Binary image dtype: {binary.dtype}, min: {binary.min()}, max: {binary.max()}")
    
    # Initialize debug_info early if requested
    debug_info = {}
    if return_debug_info:
        debug_info['binary_image'] = binary.copy()
        debug_info['original_image'] = gray.copy()
        debug_info['threshold_val'] = threshold_val
    
    # Check if binarization was successful
    if threshold_val <= 0 or threshold_val >= 255:
        if verbose:
            print("Binarization failed: invalid threshold value")
        if return_debug_info:
            debug_info['error'] = "Binarization failed: invalid threshold value"
            return None, None, debug_info
        return None, None
    
    # Calculate center of mass of binarized image
    binary_float = binary.astype(np.float64)
    y_coords, x_coords = np.mgrid[0:img_height, 0:img_width]
    total_mass = np.sum(binary_float)
    
    if total_mass == 0:
        if verbose:
            print("No bright pixels found in binarized image")
        if return_debug_info:
            debug_info['error'] = "No bright pixels found in binarized image"
            debug_info['com_x'] = img_width // 2
            debug_info['com_y'] = img_height // 2
            debug_info['quadrants'] = []
            return None, None, debug_info
        return None, None
    
    com_x = np.sum(x_coords * binary_float) / total_mass
    com_y = np.sum(y_coords * binary_float) / total_mass
    
    # Ensure center of mass is within image bounds
    if com_x < 0 or com_x >= img_width or com_y < 0 or com_y >= img_height:
        if verbose:
            print(f"Center of mass ({com_x:.1f}, {com_y:.1f}) outside bounds, using image center")
        com_x = img_width / 2.0
        com_y = img_height / 2.0
    
    com_x = int(com_x)
    com_y = int(com_y)
    
    if verbose:
        print(f"Center of mass: ({com_x}, {com_y})")
    
    # Split image into 4 quadrants using center of mass as divider
    quadrants = [
        {'name': 'top-left', 'x': (0, com_x), 'y': (0, com_y), 'corner_type': 'top-left'},
        {'name': 'top-right', 'x': (com_x, img_width), 'y': (0, com_y), 'corner_type': 'top-right'},
        {'name': 'bottom-left', 'x': (0, com_x), 'y': (com_y, img_height), 'corner_type': 'bottom-left'},
        {'name': 'bottom-right', 'x': (com_x, img_width), 'y': (com_y, img_height), 'corner_type': 'bottom-right'}
    ]
    
    detected_corners = {}
    if return_debug_info:
        debug_info['com_x'] = com_x
        debug_info['com_y'] = com_y
        debug_info['quadrants'] = []
    
    # Try to import scipy for smoothing (optional)
    try:
        from scipy import ndimage
        SCIPY_AVAILABLE = True
    except ImportError:
        SCIPY_AVAILABLE = False
    
    # Calculate row sums and column sums for the entire image
    gray_float = gray.astype(np.float64)
    row_sums_full = np.sum(gray_float, axis=1)
    col_sums_full = np.sum(gray_float, axis=0)
    
    # Smooth the full sums to reduce noise
    if smoothing_window > 1:
        if SCIPY_AVAILABLE:
            row_sums_full = ndimage.uniform_filter1d(row_sums_full, size=smoothing_window, mode='nearest')
            col_sums_full = ndimage.uniform_filter1d(col_sums_full, size=smoothing_window, mode='nearest')
        else:
            def moving_average(data, window):
                padded = np.pad(data, (window//2, window//2), mode='edge')
                return np.convolve(padded, np.ones(window)/window, mode='valid')
            row_sums_full = moving_average(row_sums_full, smoothing_window)
            col_sums_full = moving_average(col_sums_full, smoothing_window)
    
    row_sums_full = row_sums_full.astype(np.float64)
    col_sums_full = col_sums_full.astype(np.float64)
    
    if return_debug_info:
        debug_info['row_sums_full'] = row_sums_full.copy()
        debug_info['col_sums_full'] = col_sums_full.copy()
        if verbose:
            print(f"Debug: row_sums_full dtype={row_sums_full.dtype}, shape={row_sums_full.shape}, "
                  f"min={row_sums_full.min()}, max={row_sums_full.max()}")
            print(f"Debug: col_sums_full dtype={col_sums_full.dtype}, shape={col_sums_full.shape}, "
                  f"min={col_sums_full.min()}, max={col_sums_full.max()}")
    
    def _detect_horizontal_edges_from_binary(binary_img, com_x, com_y, left_edge_col=None, right_edge_col=None, verbose=False):
        """Detect top and bottom edges from binarized image using linear edge detection."""
        if binary_img is None or binary_img.size == 0:
            return None, None
        
        height, width = binary_img.shape[:2]
        com_x_int = int(com_x)
        com_y_int = int(com_y)
        
        # Crop image to region between left and right edges if provided
        crop_x_start = 0
        crop_x_end = width
        x_offset = 0
        
        if left_edge_col is not None and right_edge_col is not None:
            margin = 10
            crop_x_start = max(0, int(left_edge_col) + margin)
            crop_x_end = min(width, int(right_edge_col) + 1 - margin)
            x_offset = crop_x_start
            if crop_x_end > crop_x_start:
                binary_img = binary_img[:, crop_x_start:crop_x_end]
                width = binary_img.shape[1]
                com_x_int = com_x_int - crop_x_start
                if verbose:
                    print(f"Cropped binarized image to columns {crop_x_start} to {crop_x_end} "
                          f"(with {margin}px margin from edges, width: {width}), "
                          f"adjusted com_x to {com_x_int:.1f}")
            elif verbose:
                print(f"Warning: Crop region too small after margin (start: {crop_x_start}, end: {crop_x_end})")
        
        top_region = binary_img[:com_y_int, :]
        bottom_region = binary_img[com_y_int:, :]
        top_edge_line = None
        bottom_edge_line = None
        
        # Detect top edge: scan top-to-bottom
        if top_region.size > 0 and top_region.shape[0] > 0:
            edge_points_top = []
            for col in range(width):
                for row in range(top_region.shape[0]):
                    if top_region[row, col] == 255:
                        if row > 0 and top_region[row - 1, col] == 0:
                            edge_points_top.append((col, row))
                            break
            
            if len(edge_points_top) >= 2:
                x_coords = np.array([p[0] for p in edge_points_top], dtype=np.float64)
                y_coords = np.array([p[1] for p in edge_points_top], dtype=np.float64)
                A = np.vstack([x_coords, np.ones(len(x_coords))]).T
                m, b = np.linalg.lstsq(A, y_coords, rcond=None)[0]
                
                x1_crop, x2_crop = 0, width - 1
                y1 = m * x1_crop + b
                y2 = m * x2_crop + b
                x1 = x1_crop + x_offset
                x2 = x2_crop + x_offset
                
                top_edge_line = (x1, y1, x2, y2)
                
                if verbose:
                    print(f"Top edge detected: line through {len(edge_points_top)} points, "
                          f"y = {m:.3f}*x + {b:.3f}")
        
        # Detect bottom edge: scan bottom-to-top
        if bottom_region.size > 0 and bottom_region.shape[0] > 0:
            edge_points_bottom = []
            for col in range(width):
                for row in range(bottom_region.shape[0]):
                    if bottom_region[row, col] == 0:
                        if row > 0 and bottom_region[row - 1, col] == 255:
                            edge_points_bottom.append((col, row + com_y_int))
                            break
            
            if len(edge_points_bottom) >= 2:
                x_coords = np.array([p[0] for p in edge_points_bottom], dtype=np.float64)
                y_coords = np.array([p[1] for p in edge_points_bottom], dtype=np.float64)
                A = np.vstack([x_coords, np.ones(len(x_coords))]).T
                m, b = np.linalg.lstsq(A, y_coords, rcond=None)[0]
                
                x1_crop, x2_crop = 0, width - 1
                y1 = m * x1_crop + b
                y2 = m * x2_crop + b
                x1 = x1_crop + x_offset
                x2 = x2_crop + x_offset
                
                bottom_edge_line = (x1, y1, x2, y2)
                
                if verbose:
                    print(f"Bottom edge detected: line through {len(edge_points_bottom)} points, "
                          f"y = {m:.3f}*x + {b:.3f}")
        
        return top_edge_line, bottom_edge_line
    
    def find_peak_in_region(derivative_abs, start_idx, end_idx, threshold):
        """Find the peak (local maximum) in a region of the derivative."""
        if start_idx >= end_idx or start_idx < 0 or end_idx > len(derivative_abs):
            return None
        
        region = derivative_abs[start_idx:end_idx]
        if len(region) == 0:
            return None
        
        above_threshold = np.where(region >= threshold)[0]
        if len(above_threshold) == 0:
            return None
        
        peaks = []
        for idx in above_threshold:
            actual_idx = start_idx + idx
            is_peak = True
            if actual_idx > 0:
                if derivative_abs[actual_idx] <= derivative_abs[actual_idx - 1]:
                    is_peak = False
            if actual_idx < len(derivative_abs) - 1:
                if derivative_abs[actual_idx] <= derivative_abs[actual_idx + 1]:
                    is_peak = False
            
            if is_peak:
                peaks.append((actual_idx, derivative_abs[actual_idx]))
        
        if len(peaks) == 0:
            max_idx = np.argmax(region)
            return start_idx + max_idx
        
        peaks.sort(key=lambda x: x[1], reverse=True)
        return peaks[0][0]
    
    # Process each quadrant to find corners
    for quad in quadrants:
        x_start, x_end = quad['x']
        y_start, y_end = quad['y']
        quadrant_region = gray[y_start:y_end, x_start:x_end]
        
        if quadrant_region.size == 0:
            if verbose:
                print(f"Empty quadrant: {quad['name']}")
            continue
        
        quad_height, quad_width = quadrant_region.shape[:2]
        
        if verbose:
            print(f"\nProcessing {quad['name']} quadrant: {quad_width} x {quad_height}")
        
        # Calculate row sums and column sums for this quadrant
        row_sums = np.sum(quadrant_region, axis=1)
        col_sums = np.sum(quadrant_region, axis=0)
        
        # Smooth the sums to reduce noise
        if smoothing_window > 1:
            if SCIPY_AVAILABLE:
                row_sums = ndimage.uniform_filter1d(row_sums, size=smoothing_window, mode='nearest')
                col_sums = ndimage.uniform_filter1d(col_sums, size=smoothing_window, mode='nearest')
            else:
                def moving_average(data, window):
                    padded = np.pad(data, (window//2, window//2), mode='edge')
                    return np.convolve(padded, np.ones(window)/window, mode='valid')
                row_sums = moving_average(row_sums, smoothing_window)
                col_sums = moving_average(col_sums, smoothing_window)
        
        # Compute derivatives
        row_derivative = np.diff(row_sums)
        col_derivative = np.diff(col_sums)
        row_derivative_abs = np.abs(row_derivative)
        col_derivative_abs = np.abs(col_derivative)
        
        # Ensure derivatives are valid
        row_derivative_abs = np.nan_to_num(row_derivative_abs, nan=0.0, posinf=0.0, neginf=0.0)
        col_derivative_abs = np.nan_to_num(col_derivative_abs, nan=0.0, posinf=0.0, neginf=0.0)
        
        # Determine threshold for edge detection
        if derivative_threshold is None:
            if len(row_derivative_abs) > 0:
                max_row_deriv_val = np.max(row_derivative_abs)
                if not np.isfinite(max_row_deriv_val) or max_row_deriv_val > 1e10:
                    if verbose:
                        print(f"  Warning: Invalid max_row_deriv={max_row_deriv_val}, using 0.0")
                    max_row_deriv = 0.0
                else:
                    max_row_deriv = float(max_row_deriv_val)
            else:
                max_row_deriv = 0.0
            if len(col_derivative_abs) > 0:
                max_col_deriv_val = np.max(col_derivative_abs)
                if not np.isfinite(max_col_deriv_val) or max_col_deriv_val > 1e10:
                    if verbose:
                        print(f"  Warning: Invalid max_col_deriv={max_col_deriv_val}, using 0.0")
                    max_col_deriv = 0.0
                else:
                    max_col_deriv = float(max_col_deriv_val)
            else:
                max_col_deriv = 0.0
            row_threshold = max_row_deriv * min_change_ratio
            col_threshold = max_col_deriv * min_change_ratio
        else:
            row_threshold = float(derivative_threshold)
            col_threshold = float(derivative_threshold)
        
        row_threshold = max(0.0, min(row_threshold, 1e10))
        col_threshold = max(0.0, min(col_threshold, 1e10))
        
        if verbose:
            print(f"  Row sums: shape={row_sums.shape}, dtype={row_sums.dtype}, min={row_sums.min()}, max={row_sums.max()}")
            print(f"  Col sums: shape={col_sums.shape}, dtype={col_sums.dtype}, min={col_sums.min()}, max={col_sums.max()}")
            print(f"  Row derivative abs: shape={row_derivative_abs.shape}, dtype={row_derivative_abs.dtype}, min={row_derivative_abs.min()}, max={row_derivative_abs.max()}")
            print(f"  Col derivative abs: shape={col_derivative_abs.shape}, dtype={col_derivative_abs.dtype}, min={col_derivative_abs.min()}, max={col_derivative_abs.max()}")
            print(f"  Max derivatives: row={max_row_deriv:.2f}, col={max_col_deriv:.2f}")
            print(f"  Derivative thresholds: row={row_threshold:.2f}, col={col_threshold:.2f}")
        
        # Store debug info for this quadrant
        quad_debug = {
            'name': quad['name'],
            'region': quadrant_region.copy(),
            'row_sums': row_sums.copy(),
            'col_sums': col_sums.copy(),
            'row_derivative_abs': row_derivative_abs.copy(),
            'col_derivative_abs': col_derivative_abs.copy(),
            'max_row_deriv': float(max_row_deriv) if 'max_row_deriv' in locals() else (float(np.max(row_derivative_abs)) if len(row_derivative_abs) > 0 else 0.0),
            'max_col_deriv': float(max_col_deriv) if 'max_col_deriv' in locals() else (float(np.max(col_derivative_abs)) if len(col_derivative_abs) > 0 else 0.0),
            'row_threshold': float(row_threshold),
            'col_threshold': float(col_threshold),
            'bounds': {'x': (x_start, x_end), 'y': (y_start, y_end)}
        }
        
        # Find edges in this quadrant based on corner type
        edge_row = None
        edge_col = None
        corner_type = quad['corner_type']
        
        search_window_rows = max(1, int(quad_height * 0.2))
        search_window_cols = max(1, int(quad_width * 0.2))
        
        if corner_type == 'top-left':
            edge_row_idx = find_peak_in_region(row_derivative_abs, 0, min(search_window_rows, len(row_derivative_abs)), row_threshold)
            edge_col_idx = find_peak_in_region(col_derivative_abs, 0, min(search_window_cols, len(col_derivative_abs)), col_threshold)
            
            if edge_row_idx is not None:
                edge_row = y_start + edge_row_idx + 1
            if edge_col_idx is not None:
                edge_col = x_start + edge_col_idx + 1
        
        elif corner_type == 'top-right':
            edge_row_idx = find_peak_in_region(row_derivative_abs, 0, min(search_window_rows, len(row_derivative_abs)), row_threshold)
            col_search_start = max(0, len(col_derivative_abs) - search_window_cols)
            edge_col_idx = find_peak_in_region(col_derivative_abs, col_search_start, len(col_derivative_abs), col_threshold)
            
            if edge_row_idx is not None:
                edge_row = y_start + edge_row_idx + 1
            if edge_col_idx is not None:
                edge_col = x_start + edge_col_idx + 1
        
        elif corner_type == 'bottom-left':
            row_search_start = max(0, len(row_derivative_abs) - search_window_rows)
            edge_row_idx = find_peak_in_region(row_derivative_abs, row_search_start, len(row_derivative_abs), row_threshold)
            edge_col_idx = find_peak_in_region(col_derivative_abs, 0, min(search_window_cols, len(col_derivative_abs)), col_threshold)
            
            if edge_row_idx is not None:
                edge_row = y_start + edge_row_idx + 1
            if edge_col_idx is not None:
                edge_col = x_start + edge_col_idx + 1
        
        elif corner_type == 'bottom-right':
            row_search_start = max(0, len(row_derivative_abs) - search_window_rows)
            edge_row_idx = find_peak_in_region(row_derivative_abs, row_search_start, len(row_derivative_abs), row_threshold)
            col_search_start = max(0, len(col_derivative_abs) - search_window_cols)
            edge_col_idx = find_peak_in_region(col_derivative_abs, col_search_start, len(col_derivative_abs), col_threshold)
            
            if edge_row_idx is not None:
                edge_row = y_start + edge_row_idx + 1
            if edge_col_idx is not None:
                edge_col = x_start + edge_col_idx + 1
        
        quad_debug['edge_row'] = edge_row
        quad_debug['edge_col'] = edge_col
        
        if edge_row is not None and edge_col is not None:
            detected_corners[corner_type] = (edge_col, edge_row)
            quad_debug['corner'] = (edge_col, edge_row)
            quad_debug['corner_detected'] = True
            
            if verbose:
                print(f"  Detected {corner_type} corner at ({edge_col}, {edge_row})")
        else:
            quad_debug['corner'] = None
            quad_debug['corner_detected'] = False
            if verbose:
                print(f"  Could not detect {corner_type} corner")
                if edge_row is None:
                    print(f"    Row edge not found (threshold: {row_threshold:.2f})")
                if edge_col is None:
                    print(f"    Col edge not found (threshold: {col_threshold:.2f})")
        
        if return_debug_info:
            debug_info['quadrants'].append(quad_debug)
    
    # Detect left and right edges from column sums derivative
    left_edge_col = None
    right_edge_col = None
    
    if col_sums_full is not None and len(col_sums_full) > 1 and com_x is not None:
        col_derivative = np.diff(col_sums_full)
        col_derivative_abs = np.abs(col_derivative)
        col_derivative_abs = np.nan_to_num(col_derivative_abs, nan=0.0, posinf=0.0, neginf=0.0)
        
        com_x_int = int(com_x)
        
        # Find local maxima in left region
        left_region = col_derivative_abs[:com_x_int]
        if len(left_region) > 0:
            left_peaks = []
            for i in range(1, len(left_region) - 1):
                if left_region[i] > left_region[i-1] and left_region[i] > left_region[i+1]:
                    left_peaks.append((i, left_region[i]))
            if len(left_peaks) > 0:
                left_peaks.sort(key=lambda x: x[1], reverse=True)
                left_edge_col = left_peaks[0][0]
        
        # Find local maxima in right region
        right_region = col_derivative_abs[com_x_int:]
        if len(right_region) > 0:
            right_peaks = []
            for i in range(1, len(right_region) - 1):
                if right_region[i] > right_region[i-1] and right_region[i] > right_region[i+1]:
                    right_peaks.append((com_x_int + i, right_region[i]))
            if len(right_peaks) > 0:
                right_peaks.sort(key=lambda x: x[1], reverse=True)
                right_edge_col = right_peaks[0][0]
    
    # Detect top and bottom edges from binarized image
    top_edge_line = None
    bottom_edge_line = None
    
    if left_edge_col is not None and right_edge_col is not None:
        top_edge_line, bottom_edge_line = _detect_horizontal_edges_from_binary(
            binary, com_x, com_y, left_edge_col, right_edge_col, verbose=verbose
        )
    elif verbose:
        print("Skipping top/bottom edge detection: left or right edge not detected")
    
    if return_debug_info:
        debug_info['top_edge_line'] = top_edge_line
        debug_info['bottom_edge_line'] = bottom_edge_line
        debug_info['left_edge_col'] = left_edge_col
        debug_info['right_edge_col'] = right_edge_col
    
    def line_intersection(line1, line2):
        """Calculate intersection point between two lines."""
        if line1 is None or line2 is None:
            return None
        
        x1_1, y1_1, x2_1, y2_1 = line1
        x1_2, y1_2, x2_2, y2_2 = line2
        
        # Vertical line: x = constant
        if abs(x1_1 - x2_1) < 1e-6:
            vert_x = x1_1
            # Horizontal line: y = constant
            if abs(y1_2 - y2_2) < 1e-6:
                return (vert_x, y1_2)
            # Horizontal line: y = mx + b
            else:
                m2 = (y2_2 - y1_2) / (x2_2 - x1_2) if abs(x2_2 - x1_2) > 1e-6 else 0
                b2 = y1_2 - m2 * x1_2
                y = m2 * vert_x + b2
                return (vert_x, y)
        # Horizontal line: y = constant
        elif abs(y1_2 - y2_2) < 1e-6:
            horiz_y = y1_2
            # Vertical line: y = mx + b
            m1 = (y2_1 - y1_1) / (x2_1 - x1_1) if abs(x2_1 - x1_1) > 1e-6 else 0
            b1 = y1_1 - m1 * x1_1
            x = (horiz_y - b1) / m1 if abs(m1) > 1e-6 else x1_1
            return (x, horiz_y)
        else:
            # Both are slanted lines
            m1 = (y2_1 - y1_1) / (x2_1 - x1_1) if abs(x2_1 - x1_1) > 1e-6 else 0
            b1 = y1_1 - m1 * x1_1
            m2 = (y2_2 - y1_2) / (x2_2 - x1_2) if abs(x2_2 - x1_2) > 1e-6 else 0
            b2 = y1_2 - m2 * x1_2
            
            if abs(m1 - m2) < 1e-6:
                return None  # Parallel lines
            
            x = (b2 - b1) / (m1 - m2)
            y = m1 * x + b1
            return (x, y)
    
    # Calculate corners from intersections
    new_corners = {}
    if left_edge_col is not None and top_edge_line is not None:
        left_line = (left_edge_col, 0, left_edge_col, img_height)
        corner = line_intersection(left_line, top_edge_line)
        if corner is not None:
            new_corners['top-left'] = corner
    
    if right_edge_col is not None and top_edge_line is not None:
        right_line = (right_edge_col, 0, right_edge_col, img_height)
        corner = line_intersection(right_line, top_edge_line)
        if corner is not None:
            new_corners['top-right'] = corner
    
    if left_edge_col is not None and bottom_edge_line is not None:
        left_line = (left_edge_col, 0, left_edge_col, img_height)
        corner = line_intersection(left_line, bottom_edge_line)
        if corner is not None:
            new_corners['bottom-left'] = corner
    
    if right_edge_col is not None and bottom_edge_line is not None:
        right_line = (right_edge_col, 0, right_edge_col, img_height)
        corner = line_intersection(right_line, bottom_edge_line)
        if corner is not None:
            new_corners['bottom-right'] = corner
    
    # Use new corners if we have all 4, otherwise fall back to quadrant-based corners
    if len(new_corners) == 4:
        detected_corners = new_corners
        if verbose:
            print(f"\nUsing edge intersection corners: {new_corners}")
    
    # Check if we have all 4 corners
    required_corners = ['top-left', 'top-right', 'bottom-left', 'bottom-right']
    if not all(corner in detected_corners for corner in required_corners):
        if verbose:
            missing = [c for c in required_corners if c not in detected_corners]
            print(f"\nMissing corners: {missing}")
        if return_debug_info:
            missing = [c for c in required_corners if c not in detected_corners]
            debug_info['error'] = f"Missing corners: {missing}"
            if 'binary_image' not in debug_info:
                debug_info['binary_image'] = None
            if 'quadrants' not in debug_info:
                debug_info['quadrants'] = []
            return None, None, debug_info
        return None, None
    
    # Calculate centroid from 4 corners
    if len(detected_corners) == 4:
        rectangle_vertices = np.array([
            detected_corners['top-left'],
            detected_corners['top-right'],
            detected_corners['bottom-right'],
            detected_corners['bottom-left']
        ], dtype=np.float64)
        centroid_x = np.mean(rectangle_vertices[:, 0])
        centroid_y = np.mean(rectangle_vertices[:, 1])
    else:
        # Fall back to center of mass
        centroid_x = debug_info.get('com_x', img_width / 2.0) if return_debug_info else img_width / 2.0
        centroid_y = debug_info.get('com_y', img_height / 2.0) if return_debug_info else img_height / 2.0
        rectangle_vertices = None
    
    # Calculate ROI bounds: center ROI box on centroid
    # Round to nearest multiple of 4 to align with camera binning
    detected_roi_x = int(round((centroid_x - roi_width / 2.0) / 4.0) * 4.0)
    detected_roi_y = int(round((centroid_y - roi_height / 2.0) / 4.0) * 4.0)
    
    # Clamp to image bounds (after adding offset)
    detected_roi_x = max(0, min(detected_roi_x, img_width - roi_width))
    detected_roi_y = max(0, min(detected_roi_y, img_height - roi_height))
    
    # Calculate angle: average of top edge angle and bottom edge angle
    angle_deg = 0.0
    if rectangle_vertices is not None and len(rectangle_vertices) == 4:
        top_left = rectangle_vertices[0]
        top_right = rectangle_vertices[1]
        bottom_right = rectangle_vertices[2]
        bottom_left = rectangle_vertices[3]
        
        top_dx = top_right[0] - top_left[0]
        top_dy = top_right[1] - top_left[1]
        bottom_dx = bottom_right[0] - bottom_left[0]
        bottom_dy = bottom_right[1] - bottom_left[1]
        
        top_angle_rad = np.arctan(top_dy / abs(top_dx)) if abs(top_dx) > 1e-10 else 0.0
        bottom_angle_rad = np.arctan(bottom_dy / abs(bottom_dx)) if abs(bottom_dx) > 1e-10 else 0.0
        
        mean_angle_rad = (top_angle_rad + bottom_angle_rad) / 2.0
        angle_deg = np.degrees(mean_angle_rad)
        
        if verbose:
            print(f"\nDetected ROI: centroid=({centroid_x:.1f}, {centroid_y:.1f}), "
                  f"rect=({detected_roi_x}, {detected_roi_y}, {roi_width}, {roi_height})")
            print(f"Rectangle vertices:")
            print(f"  Top-left: ({top_left[0]:.1f}, {top_left[1]:.1f})")
            print(f"  Top-right: ({top_right[0]:.1f}, {top_right[1]:.1f})")
            print(f"  Bottom-right: ({bottom_right[0]:.1f}, {bottom_right[1]:.1f})")
            print(f"  Bottom-left: ({bottom_left[0]:.1f}, {bottom_left[1]:.1f})")
            print(f"Angle: {angle_deg:.3f}° (top: {np.degrees(top_angle_rad):.3f}°, bottom: {np.degrees(bottom_angle_rad):.3f}°)")
    elif top_edge_line is not None:
        x1, y1, x2, y2 = top_edge_line
        dx = x2 - x1
        dy = y2 - y1
        angle_rad = np.arctan(dy / abs(dx)) if abs(dx) > 1e-10 else 0.0
        angle_deg = np.degrees(angle_rad)
        if verbose:
            print(f"\nDetected ROI: centroid=({centroid_x:.1f}, {centroid_y:.1f}), "
                  f"rect=({detected_roi_x}, {detected_roi_y}, {roi_width}, {roi_height})")
            print(f"Angle from top edge: {angle_deg:.3f}°")
    
    if return_debug_info:
        debug_info['rectangle_vertices'] = rectangle_vertices
        debug_info['centroid'] = (centroid_x, centroid_y)
        debug_info['angle_deg'] = angle_deg
        return (centroid_x, centroid_y, detected_roi_x, detected_roi_y, roi_width, roi_height, angle_deg), rectangle_vertices, debug_info
    
    return (centroid_x, centroid_y, detected_roi_x, detected_roi_y, roi_width, roi_height, angle_deg), rectangle_vertices
