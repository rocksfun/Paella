"""
AUX_image_zip_to_binary.py

Optimized utility script to convert legacy image storage (ZIP files containing PNGs) 
into the modern standard binary format (.bin) used by the Paella system.

Features:
- Parallel PNG decoding using ProcessPoolExecutor
- ZIP handle caching to minimize file I/O overhead
- Progress reporting and batch-based processing
"""

import os
import re
import sys
import zipfile
import numpy as np
import cv2
import argparse
from datetime import datetime
from PIL import Image
from io import BytesIO
from concurrent.futures import ProcessPoolExecutor
import functools

def parse_args():
    parser = argparse.ArgumentParser(description="Convert legacy image ZIPs to Paella binary format.")
    parser.add_argument("input_dir", help="Directory containing legacy ZIP files")
    parser.add_argument("--output_dir", help="Directory for converted .bin files (defaults to input_dir/converted)")
    parser.add_argument("--system_id", help="Filter by specific system ID")
    parser.add_argument("--experiment_id", help="Filter by specific experiment ID")
    parser.add_argument("--workers", type=int, default=os.cpu_count(), help="Number of parallel workers (default: CPU count)")
    parser.add_argument("--batch_size", type=int, default=2000, help="Number of frames to process in each parallel batch")
    return parser.parse_args()

def extract_date_from_experiment_id(experiment_id):
    """Attempts to extract a date from the experiment_id."""
    match_12 = re.search(r'(\d{12})', experiment_id)
    if match_12:
        try:
            return datetime.strptime(match_12.group(1), "%Y%m%d%H%M")
        except ValueError:
            pass
            
    match_8 = re.search(r'(\d{8})', experiment_id)
    if match_8:
        try:
            return datetime.strptime(match_8.group(1), "%Y%m%d")
        except ValueError:
            pass
    return None

def decode_frame_worker(frame_info, png_bytes):
    """
    Worker function to decode PNG bytes and prepare metadata.
    Runs in a separate process.
    """
    # 1. Decode Image
    nparr = np.frombuffer(png_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        try:
            img_pil = Image.open(BytesIO(png_bytes)).convert('L')
            img = np.array(img_pil)
        except Exception:
            # Return a blank frame to maintain sequence if corrupted
            img = np.zeros((100, 100), dtype=np.uint8)

    # 2. Construct Metadata
    # Values: computer_time, bf_camera_time, bf_frame_number, fl_camera_time, fl_frame_number, 
    #         trigger_flag, bf_width, bf_height, fl_width, fl_height,
    #         bf_exposure_us, fl_exposure_us, blue_led_current_a, photodiode_voltage_v
    height, width = img.shape[:2]
    
    # Pack into metadata list
    values = [
        frame_info['computer_time'], # 1: computer_time
        frame_info['computer_time'], # 2: bf_camera_time
        frame_info['frame_index'],   # 3: bf_frame_number
        0,                           # 4: fl_camera_time
        0,                           # 5: fl_frame_number
        1,                           # 6: trigger_flag
        width,                       # 7: bf_width
        height,                      # 8: bf_height
        0,                           # 9: fl_width
        0,                           # 10: fl_height
        0,                           # 11: bf_exposure_us
        0,                           # 12: fl_exposure_us
        0,                           # 13: blue_led_current_a
        0                            # 14: photodiode_voltage_v
    ]
    metadata_str = "_".join(str(val) for val in values)
    
    # Return metadata bytes (256 padded) and raw image bytes
    metadata_bytes = metadata_str.encode('utf-8').ljust(256, b'\x00')[:256]
    return metadata_bytes + img.tobytes()

def process_experiment(experiment_group, all_frames, output_dir, workers, batch_size):
    """Processes an experiment group using parallel decoding."""
    system_id, experiment_id = experiment_group
    print(f"\nProcessing Experiment: {experiment_id} (System: {system_id})")
    
    start_date = extract_date_from_experiment_id(experiment_id)
    if not start_date:
        start_date = datetime.now()
    
    # Sort frames globally
    all_frames.sort(key=lambda x: x['frame_index'])
    total_frames = len(all_frames)
    
    # Pre-calculate computer_time for all frames to avoid passing datetime objects
    for frame in all_frames:
        h = int(frame['hhmmss'][0:2])
        m = int(frame['hhmmss'][2:4])
        s = int(frame['hhmmss'][4:6])
        micro_str = frame['nanos'].ljust(6, '0')[:6]
        micro = int(micro_str)
        try:
            frame_dt = start_date.replace(hour=h, minute=m, second=s, microsecond=micro)
            frame['computer_time'] = frame_dt.timestamp()
        except ValueError:
            frame['computer_time'] = start_date.timestamp() + (h * 3600 + m * 60 + s) + (micro / 1e6)

    # Use a cache for zip handles
    zip_cache = {}
    
    try:
        os.makedirs(output_dir, exist_ok=True)
        
        experiment_string = f"{system_id}_{experiment_id}"
        file_number = 1
        current_frame_count = 0
        max_frames_per_file = 700000
        current_f = None
        
        executor = ProcessPoolExecutor(max_workers=workers)
        
        for i in range(0, total_frames, batch_size):
            batch = all_frames[i : i + batch_size]
            
            # 1. Read PNG bytes from ZIPs (Main thread I/O)
            payloads = []
            for frame in batch:
                zip_path = frame['zip_path']
                if zip_path not in zip_cache:
                    zip_cache[zip_path] = zipfile.ZipFile(zip_path, 'r')
                
                try:
                    png_bytes = zip_cache[zip_path].read(frame['internal_name'])
                except Exception as e:
                    print(f"\nError reading {frame['internal_name']} from {zip_path}: {e}")
                    png_bytes = b'' # Worker will handle empty bytes
                
                payloads.append((frame, png_bytes))
            
            # 2. Decode in parallel (Process Pool CPU)
            # We use map to maintain order
            results = list(executor.map(
                decode_frame_worker, 
                [p[0] for p in payloads], 
                [p[1] for p in payloads]
            ))
            
            # 3. Write to .bin (Main thread I/O)
            for binary_block in results:
                if current_f is None or current_frame_count >= max_frames_per_file:
                    if current_f:
                        current_f.close()
                        file_number += 1
                    
                    bin_filename = f"{experiment_string}_Images_{file_number:03d}.bin"
                    bin_path = os.path.join(output_dir, bin_filename)
                    current_f = open(bin_path, 'wb')
                    current_frame_count = 0
                    print(f"\n  Started writing to: {bin_filename}")

                current_f.write(binary_block)
                current_frame_count += 1
            
            # Progress reporting
            progress = (i + len(batch)) / total_frames * 100
            print(f"\r  Progress: {i + len(batch)}/{total_frames} frames ({progress:.1f}%)", end="", flush=True)

        print("\n  Conversion complete.")

    finally:
        if current_f:
            current_f.close()
        for z in zip_cache.values():
            z.close()
        executor.shutdown()

def main():
    args = parse_args()
    input_dir = os.path.abspath(args.input_dir)
    output_dir = args.output_dir or os.path.join(input_dir, "converted")
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Scanning for ZIP files in: {input_dir}")
    zip_files = [os.path.join(input_dir, f) for f in os.listdir(input_dir) if f.lower().endswith('.zip')]
    
    groups = {}
    for zip_path in zip_files:
        filename = os.path.basename(zip_path)
        match = re.search(r'(.+?)_(.+?)_+Images_(\d+)\.zip$', filename, re.IGNORECASE)
        if match:
            system_id, experiment_id = match.group(1), match.group(2)
            if (args.system_id and system_id != args.system_id) or (args.experiment_id and experiment_id != args.experiment_id):
                continue
            key = (system_id, experiment_id)
            groups.setdefault(key, []).append(zip_path)

    if not groups:
        print("No matching ZIP files found.")
        return

    # For each group, we need to gather all frame metadata BEFORE entering process_experiment
    for group_key, zip_list in groups.items():
        all_frames_in_group = []
        for zip_path in zip_list:
            print(f"  Indexing ZIP: {os.path.basename(zip_path)}")
            with zipfile.ZipFile(zip_path, 'r') as z:
                for name in z.namelist():
                    if name.lower().endswith('.png'):
                        match = re.search(r'(\d{6})\.(\d+)_(\d+)\.png', name, re.IGNORECASE)
                        if match:
                            all_frames_in_group.append({
                                'zip_path': zip_path,
                                'internal_name': name,
                                'hhmmss': match.group(1),
                                'nanos': match.group(2),
                                'frame_index': int(match.group(3))
                            })
        
        process_experiment(group_key, all_frames_in_group, output_dir, args.workers, args.batch_size)

if __name__ == "__main__":
    main()
