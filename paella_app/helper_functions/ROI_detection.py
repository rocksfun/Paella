"""
ROI Detection Module.

This module provides functions for detecting ROI boundaries using edge detection.
It supports both axis-aligned edge detection and derivative-based corner detection
for angle-tolerant line detection.
"""

import numpy as np


def detect_edges(image_array, overlay_x, overlay_y, overlay_width, overlay_height,
                 edge_threshold=20.0, min_brightness_diff=10.0, verbose=False):
    """
    Detect edges using a two-stage approach:
    1. First detect vertical edges (left and right) by scanning from center outward
    2. Use vertical edges to define a center region
    3. Detect horizontal edges (top and bottom) by scanning from boundaries inward within the center region
    
    Args:
        image_array: numpy array of the image (grayscale or color)
        overlay_x: X coordinate of overlay region top-left corner
        overlay_y: Y coordinate of overlay region top-left corner
        overlay_width: Width of overlay region
        overlay_height: Height of overlay region
        edge_threshold: Minimum intensity drop to consider an edge (default: 20.0)
        min_brightness_diff: Minimum brightness difference between light and dark regions (default: 10.0)
    
    Returns:
        tuple: (top_edge, bottom_edge, left_edge, right_edge) in overlay region coordinates
               Each edge is None if not detected, otherwise it's the coordinate within the overlay region
    """
    if image_array is None or image_array.size == 0:
        return None, None, None, None
    
    # Convert to grayscale if needed
    if len(image_array.shape) == 3:
        if image_array.shape[2] == 3:
            # RGB to grayscale
            gray = np.mean(image_array, axis=2).astype(np.uint8)
        elif image_array.shape[2] == 4:
            # RGBA to grayscale
            gray = np.mean(image_array[:, :, :3], axis=2).astype(np.uint8)
        else:
            return None, None, None, None
    else:
        gray = image_array.copy()
    
    # Get image dimensions
    img_height, img_width = gray.shape[:2]
    
    # Clamp overlay to image bounds
    overlay_x = max(0, min(overlay_x, img_width - 1))
    overlay_y = max(0, min(overlay_y, img_height - 1))
    overlay_right = min(overlay_x + overlay_width, img_width)
    overlay_bottom = min(overlay_y + overlay_height, img_height)
    
    # Extract overlay region
    overlay_region = gray[overlay_y:overlay_bottom, overlay_x:overlay_right]
    if overlay_region.size == 0:
        return None, None, None, None
    
    # Calculate center of overlay region for initial vertical edge detection
    center_x = overlay_region.shape[1] // 2
    
    # STEP 1: Detect vertical edges first (left and right)
    # Detect left edge (scanning from center leftward to left)
    left_edge = None
    for x in range(center_x, 0, -1):  # From center to left (x=0)
        if x >= overlay_region.shape[1] - 1:
            continue
        col = overlay_region[:, x]
        next_col = overlay_region[:, x + 1]
        # Calculate mean intensity drop
        diff = col.astype(np.int16) - next_col.astype(np.int16)
        mean_diff = np.mean(diff)
        brightness_diff = np.mean(col) - np.mean(next_col)
        if mean_diff > edge_threshold and brightness_diff > min_brightness_diff:
            left_edge = x + 1  # Edge is at x+1 where dark region starts
            break  # Use first detected edge
    
    # Detect right edge (scanning from center rightward to right)
    right_edge = None
    for x in range(center_x, overlay_region.shape[1] - 1):  # From center to right
        col = overlay_region[:, x]
        next_col = overlay_region[:, x + 1]
        # Calculate mean intensity drop
        diff = col.astype(np.int16) - next_col.astype(np.int16)
        mean_diff = np.mean(diff)
        brightness_diff = np.mean(col) - np.mean(next_col)
        if mean_diff > edge_threshold and brightness_diff > min_brightness_diff:
            right_edge = x
            break  # Use first detected edge
    
    # STEP 2: Define center region using detected vertical edges
    # If vertical edges are found, use them; otherwise use full overlay width
    if left_edge is not None and right_edge is not None:
        center_region_left = left_edge
        center_region_right = right_edge
    else:
        # Fallback: use full overlay width if vertical edges not found
        center_region_left = 0
        center_region_right = overlay_region.shape[1]
    
    # Extract center region (full height, between vertical edges)
    center_region = overlay_region[:, center_region_left:center_region_right]
    if center_region.size == 0:
        # If center region is invalid, return what we have
        return None, None, left_edge, right_edge
    
    # STEP 3: Detect horizontal edges using outside-in search from top and bottom
    # Search within the center region, scanning from the boundaries inward
    
    # Detect top edge (scanning from top downward - outside-in)
    top_edge = None
    for y in range(0, center_region.shape[0] - 1):  # From top (y=0) downward
        # Use the center region for edge detection
        row = center_region[y, :]
        next_row = center_region[y + 1, :]
        # Calculate mean intensity drop (light to dark)
        diff = row.astype(np.int16) - next_row.astype(np.int16)
        mean_diff = np.mean(diff)
        # Check that we're going from light to dark
        brightness_diff = np.mean(row) - np.mean(next_row)
        if mean_diff > edge_threshold and brightness_diff > min_brightness_diff:
            top_edge = y + 1  # Edge is at y+1 where dark region starts (in overlay coordinates)
            break  # Use first detected edge
    
    # Detect bottom edge (scanning from bottom upward - outside-in)
    bottom_edge = None
    for y in range(center_region.shape[0] - 1, 0, -1):  # From bottom upward
        # Use the center region for edge detection
        row = center_region[y, :]
        prev_row = center_region[y - 1, :]
        # Calculate mean intensity drop (light to dark)
        # When scanning upward, prev_row (above) should be light, row (below) should be dark
        diff = prev_row.astype(np.int16) - row.astype(np.int16)
        mean_diff = np.mean(diff)
        # Check that we're going from light (prev_row) to dark (row)
        brightness_diff = np.mean(prev_row) - np.mean(row)
        if mean_diff > edge_threshold and brightness_diff > min_brightness_diff:
            bottom_edge = y  # Edge is at y (in overlay coordinates)
            break  # Use first detected edge
    
    return top_edge, bottom_edge, left_edge, right_edge


def calculate_roi_from_edges(top_edge, bottom_edge, left_edge, right_edge,
                             overlay_x, overlay_y, roi_width, roi_height,
                             img_width, img_height):
    """
    Calculate ROI position from detected edges.
    
    Args:
        top_edge: Top edge position in overlay coordinates (or None)
        bottom_edge: Bottom edge position in overlay coordinates (or None)
        left_edge: Left edge position in overlay coordinates (or None)
        right_edge: Right edge position in overlay coordinates (or None)
        overlay_x: X coordinate of overlay region in image
        overlay_y: Y coordinate of overlay region in image
        roi_width: Desired ROI width
        roi_height: Desired ROI height
        img_width: Full image width
        img_height: Full image height
    
    Returns:
        tuple: (roi_x, roi_y) in image coordinates, or (None, None) if edges not found
    """
    if top_edge is None or bottom_edge is None or left_edge is None or right_edge is None:
        return None, None
    
    # Calculate corners (in image coordinates)
    top_left = (overlay_x + left_edge, overlay_y + top_edge)
    top_right = (overlay_x + right_edge, overlay_y + top_edge)
    bottom_left = (overlay_x + left_edge, overlay_y + bottom_edge)
    bottom_right = (overlay_x + right_edge, overlay_y + bottom_edge)
    
    # Calculate centroid of the detected rectangle
    centroid_x = (top_left[0] + top_right[0] + bottom_left[0] + bottom_right[0]) / 4.0
    centroid_y = (top_left[1] + top_right[1] + bottom_left[1] + bottom_right[1]) / 4.0
    
    # Calculate ROI location relative to centroid
    # ROI should be centered on the centroid
    detected_roi_x = int(round(centroid_x - roi_width / 2.0))
    detected_roi_y = int(round(centroid_y - roi_height / 2.0))
    
    # Clamp to image bounds
    detected_roi_x = max(0, min(detected_roi_x, img_width - roi_width))
    detected_roi_y = max(0, min(detected_roi_y, img_height - roi_height))
    
    return detected_roi_x, detected_roi_y


def detect_edges_with_derivative_method(image_array, overlay_x, overlay_y, overlay_width, overlay_height,
                            edge_threshold=20.0, min_brightness_diff=10.0,
                                       vertical_line_threshold=None, corner_search_width=20,
                                       center_exclusion_percent=40.0,
                                       left_edge_history=None, right_edge_history=None,
                                       roi_mode=False, verbose=False):
    """
    Detect ROI edges using column/row sum derivatives to find corners.
    
    This method:
    1. Computes column sums and row sums of the overlay region
    2. Finds derivatives of these sums to detect vertical edges (left/right)
    3. For each vertical edge, finds corners by analyzing row sums in a search region
    4. Projects lines between corners to form the ROI rectangle
    
    Args:
        image_array: numpy array of the image (grayscale or color)
        overlay_x: X coordinate of overlay region top-left corner
        overlay_y: Y coordinate of overlay region top-left corner
        overlay_width: Width of overlay region
        overlay_height: Height of overlay region
        edge_threshold: Minimum derivative magnitude to consider an edge (default: 20.0)
        min_brightness_diff: Minimum brightness difference for validation (default: 10.0)
        vertical_line_threshold: Additional threshold for vertical line detection stability.
                                If None, uses edge_threshold (default: None)
        corner_search_width: Width of the search region along vertical edges for corner detection (default: 20)
        center_exclusion_percent: Percentage of center region to exclude from vertical edge detection (default: 40.0)
        left_edge_history: List of previous left edge positions for smoothing (default: None)
        right_edge_history: List of previous right edge positions for smoothing (default: None)
    
    Returns:
        tuple: (top_edge_line, bottom_edge_line, left_edge_line, right_edge_line)
               Each is a tuple (x1, y1, x2, y2) representing a line, or None if not detected
               Lines are in overlay region coordinates
    """
    if verbose:
        print(f"\n=== detect_edges_with_derivative_method called ===")
        print(f"Overlay: x={overlay_x}, y={overlay_y}, w={overlay_width}, h={overlay_height}")
    
    if image_array is None or image_array.size == 0:
        if verbose:
            print("ERROR: image_array is None or empty")
        return None, None, None, None
    
    # Convert to grayscale if needed
    if len(image_array.shape) == 3:
        if image_array.shape[2] == 3:
            gray = np.mean(image_array, axis=2).astype(np.uint8)
        elif image_array.shape[2] == 4:
            gray = np.mean(image_array[:, :, :3], axis=2).astype(np.uint8)
        else:
            return None, None, None, None
    else:
        gray = image_array.copy()
    
    # Get image dimensions
    img_height, img_width = gray.shape[:2]
    
    # Clamp overlay to image bounds
    overlay_x = max(0, min(overlay_x, img_width - 1))
    overlay_y = max(0, min(overlay_y, img_height - 1))
    overlay_right = min(overlay_x + overlay_width, img_width)
    overlay_bottom = min(overlay_y + overlay_height, img_height)
    
    # Extract overlay region
    overlay_region = gray[overlay_y:overlay_bottom, overlay_x:overlay_right]
    if overlay_region.size == 0:
        if verbose:
            print("ERROR: overlay_region is empty after extraction")
        return None, None, None, None
    
    if verbose:
        print(f"Overlay region extracted: {overlay_region.shape[1]}x{overlay_region.shape[0]}")
    
    # Preprocessing: 2x2 binning and min-max normalization
    # Store original dimensions for coordinate translation
    original_height, original_width = overlay_region.shape[:2]
    
    # Apply 2x2 binning
    # Ensure dimensions are even for clean binning
    binned_height = original_height // 2
    binned_width = original_width // 2
    
    if binned_height < 1 or binned_width < 1:
        if verbose:
            print("ERROR: Overlay region too small for 2x2 binning")
        return None, None, None, None
    
    # Crop to even dimensions for clean binning
    cropped_height = binned_height * 2
    cropped_width = binned_width * 2
    overlay_region_cropped = overlay_region[:cropped_height, :cropped_width]
    
    # Perform 2x2 binning by reshaping and taking mean
    # Reshape to (height/2, 2, width/2, 2) then take mean over the 2x2 blocks
    binned_region = overlay_region_cropped.reshape(binned_height, 2, binned_width, 2).mean(axis=(1, 3))
    binned_region = binned_region.astype(np.float32)
    
    if verbose:
        print(f"Applied 2x2 binning: {original_width}x{original_height} -> {binned_width}x{binned_height}")
    
    # Apply min-max normalization
    # NOTE: Normalization can create artificial gradients (e.g., if image has bright center,
    # normalization maps center->255 and edges->0, creating a gradient that gets detected as edges).
    # We'll skip normalization for now since binning already provides noise reduction.
    # If normalization is needed, it should be done with care to avoid creating gradients.
    
    # For now, just ensure values are in uint8 range
    min_val = binned_region.min()
    max_val = binned_region.max()
    
    # Only normalize if the dynamic range is very small (less than 10 gray levels)
    # This helps with very low-contrast images without creating artificial gradients
    if max_val > min_val and (max_val - min_val) < 10:
        # Very low contrast image - apply gentle normalization
        binned_region = (binned_region - min_val) / (max_val - min_val) * 255.0
        if verbose:
            print(f"Applied gentle normalization for low contrast: [{min_val:.1f}, {max_val:.1f}] -> [0, 255]")
    else:
        # Normal dynamic range - keep original values (just ensure uint8 range)
        binned_region = np.clip(binned_region, 0, 255)
        if verbose:
            print(f"No normalization applied (preserving original range: [{min_val:.1f}, {max_val:.1f}])")
    
    # Convert back to uint8 for processing
    overlay_region = binned_region.astype(np.uint8)
    
    # Coordinate translation factor: binned coordinates need to be multiplied by 2
    # and offset by 1 to get center of bin in original coordinates
    # Actually, for edge detection, we'll map binned pixel (x, y) to original pixel (x*2+1, y*2+1)
    # which represents the center of the 2x2 bin
    def translate_binned_to_original(x_bin, y_bin):
        """Translate binned coordinates to original overlay region coordinates.
        Maps to the center of the 2x2 bin."""
        x_orig = x_bin * 2 + 1
        y_orig = y_bin * 2 + 1
        # Clamp to original dimensions
        x_orig = min(x_orig, original_width - 1)
        y_orig = min(y_orig, original_height - 1)
        return x_orig, y_orig
    
    # Compute column sums (sum along vertical axis) - detects vertical edges
    column_sums = np.sum(overlay_region, axis=0)  # Shape: (width,)
    if verbose:
        print(f"Column sums: min={column_sums.min()}, max={column_sums.max()}, mean={column_sums.mean():.1f}")
    
    # Compute row sums (sum along horizontal axis) - detects horizontal edges
    row_sums = np.sum(overlay_region, axis=1)  # Shape: (height,)
    if verbose:
        print(f"Row sums: min={row_sums.min()}, max={row_sums.max()}, mean={row_sums.mean():.1f}")
    
    # Compute derivatives
    # For column sums: derivative detects vertical edges (left/right boundaries)
    # A dark line will cause a drop in column sum, so we look for negative derivatives
    column_derivative = np.diff(column_sums.astype(np.float32))
    # Use negative derivative to find where brightness drops (dark line appears)
    column_derivative_neg = -column_derivative
    
    # For row sums: derivative detects horizontal edges (top/bottom boundaries)
    # A dark line will cause a drop in row sum, so we look for negative derivatives
    row_derivative = np.diff(row_sums.astype(np.float32))
    # Use negative derivative to find where brightness drops (dark line appears)
    row_derivative_neg = -row_derivative
    
    # Find peaks in derivatives (edges)
    # Use a threshold based on the derivative magnitude
    # Normalize threshold by the sum magnitude
    if vertical_line_threshold is None:
        vertical_line_threshold = edge_threshold
    column_threshold = vertical_line_threshold * overlay_region.shape[0] / 10.0  # Scale by height
    row_threshold = edge_threshold * overlay_region.shape[1] / 10.0  # Scale by width
    
    if verbose:
        print(f"Derivative thresholds: column (vertical)={column_threshold:.1f}, row={row_threshold:.1f}")
        print(f"Corner search width: {corner_search_width} pixels")
    
    # Find left and right edges from column derivative
    # Scan from center outward to find the first edge in each direction
    # Exclude the center X% of the image from detection
    left_edge_x = None
    right_edge_x = None
    
    # Calculate center of overlay region
    center_x = len(column_derivative_neg) // 2
    width = len(column_derivative_neg)
    
    # Calculate center exclusion region
    exclusion_half_width = int(width * center_exclusion_percent / 100.0 / 2.0)
    center_exclusion_start = center_x - exclusion_half_width
    center_exclusion_end = center_x + exclusion_half_width
    
    if verbose:
        print(f"Scanning for vertical edges from center x={center_x}, excluding center {center_exclusion_percent}% (x={center_exclusion_start} to {center_exclusion_end})")
    
    # Calculate median from history for smoothing (if available)
    left_median = None
    right_median = None
    if left_edge_history and len(left_edge_history) > 0:
        left_median = np.median(left_edge_history)
        if verbose:
            print(f"Left edge history median: {left_median:.1f} (from {len(left_edge_history)} samples)")
    if right_edge_history and len(right_edge_history) > 0:
        right_median = np.median(right_edge_history)
        if verbose:
            print(f"Right edge history median: {right_median:.1f} (from {len(right_edge_history)} samples)")
    
    # Find left edge candidates (scanning from just outside exclusion region toward left)
    # In ROI mode, skip left edge detection and use center instead
    left_edge_x_binned = None
    left_edge_x = None
    if not roi_mode:
        # Collect all candidates and apply penalty based on distance from median
        left_scan_start = max(center_exclusion_start - 1, 0)  # Start just outside exclusion region (toward left)
        left_candidates = []
        for x in range(left_scan_start, -1, -1):  # From exclusion boundary to left edge (decreasing x)
            if x < len(column_derivative_neg) and column_derivative_neg[x] > column_threshold:
                # Verify this is actually a brightness drop
                if x > 0 and x < len(column_sums) - 1:
                    brightness_diff = column_sums[x] - column_sums[x + 1]
                    if brightness_diff > min_brightness_diff * overlay_region.shape[0]:
                        candidate_x_binned = x + 1  # Edge is at x+1 where dark region starts
                        # Translate to original coordinates for penalty calculation
                        candidate_x_orig, _ = translate_binned_to_original(candidate_x_binned, 0)
                        
                        # Calculate score: combination of derivative strength and brightness difference
                        score = column_derivative_neg[x] * brightness_diff
                        
                        # Apply penalty based on distance from center
                        # Edges closer to center are preferred (ROI should be roughly centered)
                        center_x_orig = original_width / 2.0
                        distance_from_center = abs(candidate_x_orig - center_x_orig)
                        max_distance_from_center = original_width / 2.0  # Maximum distance (at edge)
                        # Center penalty factor: 1.0 at center, decreases as distance increases
                        center_penalty_factor = max(0.1, 1.0 - (distance_from_center / max_distance_from_center) * 0.5)  # Moderate penalty
                        score *= center_penalty_factor
                        
                        # Apply penalty based on distance from median (if history available)
                        if left_median is not None:
                            distance_from_median = abs(candidate_x_orig - left_median)
                            # Penalty: reduce score by distance (scaled by overlay width)
                            # Penalty factor: 1.0 for distance=0, decreases as distance increases
                            max_distance = overlay_region.shape[1]  # Maximum possible distance
                            median_penalty_factor = max(0.1, 1.0 - (distance_from_median / max_distance) * 2.0)  # Penalty up to 2x distance
                            score *= median_penalty_factor
                            if verbose:
                                print(f"  Left candidate at x={candidate_x_orig}: base_score={column_derivative_neg[x] * brightness_diff:.1f}, distance_from_center={distance_from_center:.1f} (penalty={center_penalty_factor:.3f}), distance_from_median={distance_from_median:.1f} (penalty={median_penalty_factor:.3f}), final_score={score:.1f}")
                        else:
                            if verbose:
                                print(f"  Left candidate at x={candidate_x_orig}: base_score={column_derivative_neg[x] * brightness_diff:.1f}, distance_from_center={distance_from_center:.1f} (penalty={center_penalty_factor:.3f}), final_score={score:.1f}")
                        
                        left_candidates.append((candidate_x_binned, candidate_x_orig, score))
        
        # Select best left edge candidate (highest score after penalty)
        if left_candidates:
            # Sort by score (descending) and take the best
            left_candidates.sort(key=lambda c: c[2], reverse=True)
            left_edge_x_binned, left_edge_x, best_score = left_candidates[0]
            if verbose:
                print(f"Left edge selected at binned x={left_edge_x_binned} -> original x={left_edge_x} (score={best_score:.1f}, {len(left_candidates)} candidates)")
    else:
        # ROI mode: use center of overlay region as left edge position
        center_x_orig = original_width / 2.0
        left_edge_x = center_x_orig
        # Convert to binned coordinates for corner detection
        center_x_binned = int(center_x_orig / 2.0)  # Approximate binned coordinate
        left_edge_x_binned = center_x_binned
        if verbose:
            print(f"ROI mode: Using center as left edge at x={left_edge_x:.1f} (binned x={left_edge_x_binned})")
    
    # Find right edge candidates (scanning from just outside exclusion region toward right)
    # Collect all candidates and apply penalty based on distance from median
    right_scan_start = min(center_exclusion_end + 1, len(column_derivative_neg) - 1)  # Start just outside exclusion region (toward right)
    right_candidates = []
    for x in range(right_scan_start, len(column_derivative_neg)):  # From exclusion boundary to right edge (increasing x)
        if column_derivative_neg[x] > column_threshold:
            # Verify this is actually a brightness drop
            if x > 0 and x < len(column_sums) - 1:
                brightness_diff = column_sums[x] - column_sums[x + 1]
                if brightness_diff > min_brightness_diff * overlay_region.shape[0]:
                    candidate_x_binned = x + 1  # Edge is at x+1 where dark region starts
                    # Translate to original coordinates for penalty calculation
                    candidate_x_orig, _ = translate_binned_to_original(candidate_x_binned, 0)
                    
                    # Calculate score: combination of derivative strength and brightness difference
                    score = column_derivative_neg[x] * brightness_diff
                    
                    # Apply penalty based on distance from center
                    # Edges closer to center are preferred (ROI should be roughly centered)
                    center_x_orig = original_width / 2.0
                    distance_from_center = abs(candidate_x_orig - center_x_orig)
                    max_distance_from_center = original_width / 2.0  # Maximum distance (at edge)
                    # Center penalty factor: 1.0 at center, decreases as distance increases
                    center_penalty_factor = max(0.1, 1.0 - (distance_from_center / max_distance_from_center) * 0.5)  # Moderate penalty
                    score *= center_penalty_factor
                    
                    # Apply penalty based on distance from median (if history available)
                    if right_median is not None:
                        distance_from_median = abs(candidate_x_orig - right_median)
                        # Penalty: reduce score by distance (scaled by overlay width)
                        # Penalty factor: 1.0 for distance=0, decreases as distance increases
                        max_distance = overlay_region.shape[1]  # Maximum possible distance
                        median_penalty_factor = max(0.1, 1.0 - (distance_from_median / max_distance) * 2.0)  # Penalty up to 2x distance
                        score *= median_penalty_factor
                        if verbose:
                            print(f"  Right candidate at x={candidate_x_orig}: base_score={column_derivative_neg[x] * brightness_diff:.1f}, distance_from_center={distance_from_center:.1f} (penalty={center_penalty_factor:.3f}), distance_from_median={distance_from_median:.1f} (penalty={median_penalty_factor:.3f}), final_score={score:.1f}")
                    else:
                        if verbose:
                            print(f"  Right candidate at x={candidate_x_orig}: base_score={column_derivative_neg[x] * brightness_diff:.1f}, distance_from_center={distance_from_center:.1f} (penalty={center_penalty_factor:.3f}), final_score={score:.1f}")
                    
                    right_candidates.append((candidate_x_binned, candidate_x_orig, score))
    
    # Select best right edge candidate (highest score after penalty)
    right_edge_x_binned = None
    right_edge_x = None
    if right_candidates:
        # Sort by score (descending) and take the best
        right_candidates.sort(key=lambda c: c[2], reverse=True)
        right_edge_x_binned, right_edge_x, best_score = right_candidates[0]
        if verbose:
            print(f"Right edge selected at binned x={right_edge_x_binned} -> original x={right_edge_x} (score={best_score:.1f}, {len(right_candidates)} candidates)")
    
    # Convert vertical edges to line format (using original coordinates)
    # In ROI mode, don't return a left line (it's the center, not an edge to display)
    left_line = None if (roi_mode or left_edge_x is None) else (left_edge_x, 0, left_edge_x, original_height)
    right_line = None if right_edge_x is None else (right_edge_x, 0, right_edge_x, original_height)
    
    # Find corners along vertical edges using row sum derivatives
    # For each vertical edge, search in a region of width corner_search_width
    top_left_corner = None
    bottom_left_corner = None
    top_right_corner = None
    bottom_right_corner = None
    
    # Helper function to find corners along a vertical edge
    def find_corners_along_vertical_edge(edge_x_binned, is_left_edge):
        """Find top and bottom corners along a vertical edge using row sum derivatives.
        Returns the two strongest corners instead of the two outermost.
        Works in binned coordinates."""
        if edge_x_binned is None:
            return None, None
        
        # Adjust corner_search_width for binned coordinates (divide by 2)
        corner_search_width_binned = max(1, corner_search_width // 2)
        
        # Define search region: corner_search_width_binned pixels centered on the edge
        search_half_width = corner_search_width_binned // 2
        search_x_start = max(0, edge_x_binned - search_half_width)
        search_x_end = min(overlay_region.shape[1], edge_x_binned + search_half_width + 1)
        
        # Extract search region
        search_region = overlay_region[:, search_x_start:search_x_end]
        
        # Calculate row sums within the search region
        search_row_sums = np.sum(search_region, axis=1)  # Shape: (height,)
        
        # Compute derivative (negative derivative to find brightness drops)
        search_row_derivative = np.diff(search_row_sums.astype(np.float32))
        search_row_derivative_neg = -search_row_derivative
        
        # Threshold for corner detection (scale by search width)
        corner_threshold = row_threshold * (search_x_end - search_x_start) / overlay_region.shape[1]
        
        # Find all candidate corners with their strength scores
        # Store as (y_position, strength_score) where strength = derivative * brightness_diff
        top_candidates = []
        bottom_candidates = []
        
        # Scan through all positions to find candidates
        for y in range(len(search_row_derivative_neg)):
            if search_row_derivative_neg[y] > corner_threshold:
                # Verify this is actually a brightness drop
                if y > 0 and y < len(search_row_sums) - 1:
                    brightness_diff = search_row_sums[y] - search_row_sums[y + 1]
                    if brightness_diff > min_brightness_diff * (search_x_end - search_x_start):
                        corner_y = y + 1  # Corner is at y+1 where dark region starts
                        # Calculate strength: combination of derivative magnitude and brightness difference
                        strength = search_row_derivative_neg[y] * brightness_diff
                        
                        # Classify as top or bottom based on position relative to center
                        center_y = len(search_row_derivative_neg) // 2
                        if corner_y < center_y:
                            top_candidates.append((corner_y, strength))
                        else:
                            bottom_candidates.append((corner_y, strength))
        
        # Select the strongest corner from top candidates
        top_corner_y = None
        if top_candidates:
            # Sort by strength (descending) and take the strongest
            top_candidates.sort(key=lambda x: x[1], reverse=True)
            top_corner_y = top_candidates[0][0]
            edge_name = "left" if is_left_edge else "right"
            if verbose:
                print(f"Top {edge_name} corner detected at y={top_corner_y} (strength={top_candidates[0][1]:.1f}, {len(top_candidates)} candidates)")
        
        # Select the strongest corner from bottom candidates
        bottom_corner_y = None
        if bottom_candidates:
            # Sort by strength (descending) and take the strongest
            bottom_candidates.sort(key=lambda x: x[1], reverse=True)
            bottom_corner_y = bottom_candidates[0][0]
            edge_name = "left" if is_left_edge else "right"
            if verbose:
                print(f"Bottom {edge_name} corner detected at y={bottom_corner_y} (strength={bottom_candidates[0][1]:.1f}, {len(bottom_candidates)} candidates)")
        
        return top_corner_y, bottom_corner_y
    
    # Find corners along left edge (using binned coordinates)
    if left_edge_x_binned is not None:
        top_left_corner_binned, bottom_left_corner_binned = find_corners_along_vertical_edge(left_edge_x_binned, is_left_edge=True)
        # Translate corners to original coordinates
        if top_left_corner_binned is not None:
            _, top_left_corner = translate_binned_to_original(left_edge_x_binned, top_left_corner_binned)
        if bottom_left_corner_binned is not None:
            _, bottom_left_corner = translate_binned_to_original(left_edge_x_binned, bottom_left_corner_binned)
    
    # Find corners along right edge (using binned coordinates)
    if right_edge_x_binned is not None:
        top_right_corner_binned, bottom_right_corner_binned = find_corners_along_vertical_edge(right_edge_x_binned, is_left_edge=False)
        # Translate corners to original coordinates
        if top_right_corner_binned is not None:
            _, top_right_corner = translate_binned_to_original(right_edge_x_binned, top_right_corner_binned)
        if bottom_right_corner_binned is not None:
            _, bottom_right_corner = translate_binned_to_original(right_edge_x_binned, bottom_right_corner_binned)
    
    # Construct top and bottom lines from corner points
    top_line = None
    bottom_line = None
    
    # Top line: connect top-left and top-right corners
    # In ROI mode, left_edge_x is the center position
    if top_left_corner is not None and top_right_corner is not None:
        top_line = (left_edge_x, top_left_corner, right_edge_x, top_right_corner)
        # Calculate angle
        dx = right_edge_x - left_edge_x
        dy = top_right_corner - top_left_corner
        if dx != 0:
            angle = np.arctan2(dy, dx) * 180 / np.pi
            if verbose:
                print(f"Top line: ({left_edge_x}, {top_left_corner}) -> ({right_edge_x}, {top_right_corner}), angle: {angle:.3f}°")
        else:
            if verbose:
                print(f"Top line: ({left_edge_x}, {top_left_corner}) -> ({right_edge_x}, {top_right_corner}), angle: 90°")
    elif top_left_corner is not None:
        # Only left corner found, extend horizontally
        top_line = (left_edge_x, top_left_corner, original_width, top_left_corner)
        if verbose:
            print(f"Top line (left corner only): ({left_edge_x}, {top_left_corner}) -> ({original_width}, {top_left_corner})")
    elif top_right_corner is not None:
        # Only right corner found, extend horizontally
        top_line = (0, top_right_corner, right_edge_x, top_right_corner)
        if verbose:
            print(f"Top line (right corner only): (0, {top_right_corner}) -> ({right_edge_x}, {top_right_corner})")
    
    # Bottom line: connect bottom-left and bottom-right corners
    # In ROI mode, left_edge_x is the center position
    if bottom_left_corner is not None and bottom_right_corner is not None:
        bottom_line = (left_edge_x, bottom_left_corner, right_edge_x, bottom_right_corner)
        # Calculate angle
        dx = right_edge_x - left_edge_x
        dy = bottom_right_corner - bottom_left_corner
        if dx != 0:
            angle = np.arctan2(dy, dx) * 180 / np.pi
            if verbose:
                print(f"Bottom line: ({left_edge_x}, {bottom_left_corner}) -> ({right_edge_x}, {bottom_right_corner}), angle: {angle:.3f}°")
        else:
            if verbose:
                print(f"Bottom line: ({left_edge_x}, {bottom_left_corner}) -> ({right_edge_x}, {bottom_right_corner}), angle: 90°")
    elif bottom_left_corner is not None:
        # Only left corner found, extend horizontally
        bottom_line = (left_edge_x, bottom_left_corner, original_width, bottom_left_corner)
        if verbose:
            print(f"Bottom line (left corner only): ({left_edge_x}, {bottom_left_corner}) -> ({original_width}, {bottom_left_corner})")
    elif bottom_right_corner is not None:
        # Only right corner found, extend horizontally
        bottom_line = (0, bottom_right_corner, right_edge_x, bottom_right_corner)
        if verbose:
            print(f"Bottom line (right corner only): (0, {bottom_right_corner}) -> ({right_edge_x}, {bottom_right_corner})")
    
    if verbose:
        print(f"Detected edges: top_line={top_line}, bottom_line={bottom_line}, left={left_edge_x}, right={right_edge_x}")
    
    return top_line, bottom_line, left_line, right_line


def detect_edges_with_lines(image_array, overlay_x, overlay_y, overlay_width, overlay_height,
                            edge_threshold=20.0, min_brightness_diff=10.0,
                            use_line_detection=True,
                            vertical_line_threshold=None, corner_search_width=20,
                            center_exclusion_percent=40.0,
                            left_edge_history=None, right_edge_history=None,
                            roi_mode=False, verbose=False):
    """
    Detect edges using hybrid approach:
    - Vertical edges (left/right): Always use axis-aligned edge detection
    - Horizontal edges (top/bottom): Use derivative-based corner detection if use_line_detection=True, 
      otherwise use axis-aligned edge detection
    
    Args:
        image_array: numpy array of the image (grayscale or color)
        overlay_x: X coordinate of overlay region top-left corner
        overlay_y: Y coordinate of overlay region top-left corner
        overlay_width: Width of overlay region
        overlay_height: Height of overlay region
        edge_threshold: Minimum intensity drop to consider an edge (default: 20.0)
        min_brightness_diff: Minimum brightness difference for edge detection (default: 10.0)
        use_line_detection: If True, use derivative-based corner detection for horizontal edges; 
                          if False, use axis-aligned detection for all edges
        vertical_line_threshold: Additional threshold for vertical line detection stability.
                                 If None, uses edge_threshold (default: None)
        corner_search_width: Width of the search region along vertical edges for corner detection (default: 20)
        center_exclusion_percent: Percentage of center region to exclude from vertical edge detection (default: 40.0)
        left_edge_history: List of previous left edge positions for smoothing (default: None)
        right_edge_history: List of previous right edge positions for smoothing (default: None)
    
    Returns:
        tuple: (top_edge_line, bottom_edge_line, left_edge_line, right_edge_line)
               Each is a tuple (x1, y1, x2, y2) representing a line, or None if not detected
               Lines are in overlay region coordinates
    """
    if verbose:
        print(f"\n=== detect_edges_with_lines called ===")
        print(f"use_line_detection: {use_line_detection}")
        print(f"Overlay: x={overlay_x}, y={overlay_y}, w={overlay_width}, h={overlay_height}")
    
    # Always use axis-aligned detection for vertical edges (left/right)
    # In ROI mode, skip left edge detection
    if not roi_mode:
        if verbose:
            print("Using axis-aligned detection for vertical edges (left/right)...")
        top_edge, bottom_edge, left_edge, right_edge = detect_edges(
            image_array, overlay_x, overlay_y, overlay_width, overlay_height,
            edge_threshold, min_brightness_diff, verbose=verbose
        )
        
        # Convert vertical edges to line format (always axis-aligned)
        left_line = None if left_edge is None else (left_edge, 0, left_edge, overlay_height)
        right_line = None if right_edge is None else (right_edge, 0, right_edge, overlay_height)
    else:
        # ROI mode: only detect right edge, skip left edge
        if verbose:
            print("ROI mode: Using axis-aligned detection for right edge only...")
        # We'll use detect_edges but ignore left_edge result
        top_edge, bottom_edge, _, right_edge = detect_edges(
            image_array, overlay_x, overlay_y, overlay_width, overlay_height,
            edge_threshold, min_brightness_diff, verbose=verbose
        )
        left_line = None  # No left line in ROI mode
        right_line = None if right_edge is None else (right_edge, 0, right_edge, overlay_height)
    
    # For horizontal edges, use derivative method if enabled, otherwise use axis-aligned
    if not use_line_detection:
        # Use axis-aligned detection for horizontal edges too
        if verbose:
            print("Using axis-aligned detection for horizontal edges (top/bottom)")
        top_line = None if top_edge is None else (0, top_edge, overlay_width, top_edge)
        bottom_line = None if bottom_edge is None else (0, bottom_edge, overlay_width, bottom_edge)
        if verbose:
            print(f"Result: top={top_line}, bottom={bottom_line}, left={left_line}, right={right_line}")
        return top_line, bottom_line, left_line, right_line
    
    # Use derivative-based method for all edges (replaces Hough line detection)
    if verbose:
        print("Using derivative-based corner detection method...")
    
    # Use derivative method to detect all edges
    top_line, bottom_line, left_line_deriv, right_line_deriv = detect_edges_with_derivative_method(
        image_array, overlay_x, overlay_y, overlay_width, overlay_height,
        edge_threshold, min_brightness_diff,
        vertical_line_threshold, corner_search_width,
        center_exclusion_percent,
        left_edge_history, right_edge_history,
        roi_mode, verbose=verbose
    )
    
    # Use derivative-detected vertical edges if available, otherwise fall back to axis-aligned
    if left_line_deriv is not None:
        left_line = left_line_deriv
    if right_line_deriv is not None:
        right_line = right_line_deriv
    
    # If horizontal edges weren't detected by derivative method, fall back to axis-aligned
    if top_line is None:
        top_line = None if top_edge is None else (0, top_edge, overlay_width, top_edge)
    if bottom_line is None:
        bottom_line = None if bottom_edge is None else (0, bottom_edge, overlay_width, bottom_edge)
    
    if verbose:
        print(f"Final result: top={top_line}, bottom={bottom_line}, left={left_line}, right={right_line}")
    return top_line, bottom_line, left_line, right_line


def calculate_roi_from_lines(top_line, bottom_line, left_line, right_line,
                             overlay_x, overlay_y, roi_width, roi_height,
                             img_width, img_height):
    """
    Calculate ROI position from detected lines by finding their intersections.
    
    Args:
        top_line: Top edge line as (x1, y1, x2, y2) in overlay coordinates (or None)
        bottom_line: Bottom edge line as (x1, y1, x2, y2) in overlay coordinates (or None)
        left_line: Left edge line as (x1, y1, x2, y2) in overlay coordinates (or None)
        right_line: Right edge line as (x1, y1, x2, y2) in overlay coordinates (or None)
        overlay_x: X coordinate of overlay region in image
        overlay_y: Y coordinate of overlay region in image
        roi_width: Desired ROI width
        roi_height: Desired ROI height
        img_width: Full image width
        img_height: Full image height
    
    Returns:
        tuple: (roi_x, roi_y) in image coordinates, or (None, None) if lines not found
    """
    if top_line is None or bottom_line is None or left_line is None or right_line is None:
        return None, None
    
    def line_intersection(line1, line2):
        """Find intersection point of two lines."""
        x1, y1, x2, y2 = line1
        x3, y3, x4, y4 = line2
        
        # Calculate line equations: ax + by + c = 0
        a1 = y2 - y1
        b1 = x1 - x2
        c1 = x2 * y1 - x1 * y2
        
        a2 = y4 - y3
        b2 = x3 - x4
        c2 = x4 * y3 - x3 * y4
        
        det = a1 * b2 - a2 * b1
        if abs(det) < 1e-10:
            return None  # Lines are parallel
        
        x = (b1 * c2 - b2 * c1) / det
        y = (a2 * c1 - a1 * c2) / det
        return (x, y)
    
    # Find corner intersections
    top_left = line_intersection(top_line, left_line)
    top_right = line_intersection(top_line, right_line)
    bottom_left = line_intersection(bottom_line, left_line)
    bottom_right = line_intersection(bottom_line, right_line)
    
    if None in [top_left, top_right, bottom_left, bottom_right]:
        return None, None
    
    # Convert to image coordinates
    top_left_img = (overlay_x + top_left[0], overlay_y + top_left[1])
    top_right_img = (overlay_x + top_right[0], overlay_y + top_right[1])
    bottom_left_img = (overlay_x + bottom_left[0], overlay_y + bottom_left[1])
    bottom_right_img = (overlay_x + bottom_right[0], overlay_y + bottom_right[1])
    
    # Calculate centroid of the detected quadrilateral
    centroid_x = (top_left_img[0] + top_right_img[0] + bottom_left_img[0] + bottom_right_img[0]) / 4.0
    centroid_y = (top_left_img[1] + top_right_img[1] + bottom_left_img[1] + bottom_right_img[1]) / 4.0
    
    # Calculate ROI location relative to centroid
    detected_roi_x = int(round(centroid_x - roi_width / 2.0))
    detected_roi_y = int(round(centroid_y - roi_height / 2.0))
    
    # Clamp to image bounds
    detected_roi_x = max(0, min(detected_roi_x, img_width - roi_width))
    detected_roi_y = max(0, min(detected_roi_y, img_height - roi_height))
    
    return detected_roi_x, detected_roi_y

