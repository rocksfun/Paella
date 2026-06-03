"""
ROI Focus Measurement Module.

This module provides functions for measuring image focus/sharpness using the variance of Laplacian method.
"""

import numpy as np
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


def calculate_roi_focus(image_array, roi_x, roi_y, roi_width, roi_height):
    """
    Calculate focus/sharpness of a ROI region using variance of Laplacian.
    
    The variance of Laplacian is a common metric for image sharpness:
    - Higher values indicate sharper/more focused images
    - Lower values indicate blurrier/less focused images
    
    Args:
        image_array: numpy array of the image (grayscale or color)
        roi_x: X coordinate of ROI top-left corner in image coordinates
        roi_y: Y coordinate of ROI top-left corner in image coordinates
        roi_width: Width of ROI region
        roi_height: Height of ROI region
    
    Returns:
        float: Variance of Laplacian value (focus metric), or None if calculation fails
    """
    if image_array is None or image_array.size == 0:
        return None
    
    # Convert to grayscale if needed
    if len(image_array.shape) == 3:
        if image_array.shape[2] == 3:
            gray = np.mean(image_array, axis=2).astype(np.uint8)
        elif image_array.shape[2] == 4:
            gray = np.mean(image_array[:, :, :3], axis=2).astype(np.uint8)
        else:
            return None
    else:
        gray = image_array.copy()
    
    # Get image dimensions
    img_height, img_width = gray.shape[:2]
    
    # Clamp ROI to image bounds
    roi_x = max(0, min(int(roi_x), img_width - 1))
    roi_y = max(0, min(int(roi_y), img_height - 1))
    roi_right = min(roi_x + int(roi_width), img_width)
    roi_bottom = min(roi_y + int(roi_height), img_height)
    
    # Extract ROI region
    roi_region = gray[roi_y:roi_bottom, roi_x:roi_right]
    
    if roi_region.size == 0:
        return None
    
    # Calculate variance of Laplacian
    if CV2_AVAILABLE:
        # Use OpenCV's Laplacian for better performance
        laplacian = cv2.Laplacian(roi_region, cv2.CV_64F)
        focus_value = laplacian.var()
    else:
        # Fallback: manual Laplacian calculation using numpy
        # Simple Laplacian kernel: [[0, -1, 0], [-1, 4, -1], [0, -1, 0]]
        kernel = np.array([[0, -1, 0],
                          [-1, 4, -1],
                          [0, -1, 0]], dtype=np.float64)
        
        # Pad the image for convolution
        padded = np.pad(roi_region.astype(np.float64), 1, mode='edge')
        
        # Convolve with Laplacian kernel
        laplacian = np.zeros_like(roi_region, dtype=np.float64)
        for i in range(roi_region.shape[0]):
            for j in range(roi_region.shape[1]):
                laplacian[i, j] = np.sum(padded[i:i+3, j:j+3] * kernel)
        
        focus_value = np.var(laplacian)
    
    return float(focus_value)

