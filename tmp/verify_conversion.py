import os
import zipfile
import numpy as np
from PIL import Image
import subprocess
import shutil

def create_mock_data():
    test_dir = "/Volumes/Users/ncalistri/paella_git/Paella/tmp/test_zip_conversion"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir)
    
    # Create a small 100x100 fake PNG
    img_data = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
    img = Image.fromarray(img_data)
    
    png_path = os.path.join(test_dir, "123005.123456_0001.png")
    img.save(png_path)
    
    # Create ZIP file
    # Pattern: [system_id]_[experiment_ID]__images_[index].zip
    zip_path = os.path.join(test_dir, "SMR1_202401151430__images_01.zip")
    with zipfile.ZipFile(zip_path, 'w') as z:
        z.write(png_path, os.path.basename(png_path))
        
    print(f"Created mock ZIP: {zip_path}")
    return test_dir

def run_conversion(test_dir):
    script_path = "/Volumes/Users/ncalistri/paella_git/Paella/helper_functions/AUX_image_zip_to_binary.py"
    cmd = [
        "python3", script_path,
        test_dir,
        "--output_dir", os.path.join(test_dir, "converted")
    ]
    print(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print("Errors:")
        print(result.stderr)
    return result.returncode == 0

def verify_output(test_dir):
    output_dir = os.path.join(test_dir, "converted")
    expected_bin = "SMR1_202401151430_Images_001.bin"
    bin_path = os.path.join(output_dir, expected_bin)
    
    if os.path.exists(bin_path):
        size = os.path.getsize(bin_path)
        # 256 bytes metadata + 100*100 bytes image = 10256 bytes
        expected_size = 256 + (100 * 100)
        print(f"Verification: Found binary at {bin_path}")
        print(f"Size: {size} bytes (Expected: {expected_size})")
        
        if size == expected_size:
            print("SUCCESS: Conversion verified.")
            return True
        else:
            print("FAILURE: Size mismatch.")
            return False
    else:
        print(f"FAILURE: Output binary {expected_bin} not found.")
        return False

if __name__ == "__main__":
    test_dir = create_mock_data()
    if run_conversion(test_dir):
        verify_output(test_dir)
    else:
        print("Conversion script failed.")
