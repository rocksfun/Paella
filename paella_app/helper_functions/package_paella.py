import os
import subprocess
import sys

def package():
    """Package Paella repository into a standalone executable using PyInstaller."""
    print("Starting Paella packaging process...")
    
    # Get the project root directory (one level up from this script in helper_functions/)
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_script_dir)
    
    # Change working directory to project root
    os.chdir(project_root)
    print(f"Set working directory to: {project_root}")
    
    # Define spec file path relative to project root
    spec_file = os.path.join("references", "paella.spec")
    
    if not os.path.exists(spec_file):
        print(f"Error: Spec file not found at {spec_file}")
        sys.exit(1)
    
    # Run PyInstaller using the spec file to ensure specific exclusions/inclusions
    cmd = [
        "pyinstaller",
        "--noconfirm",
        spec_file
    ]
    
    print(f"Running command: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
        print("\nPackaging successful!")
        print("The executable can be found in the 'dist' folder.")
    except subprocess.CalledProcessError as e:
        print(f"\nError during packaging: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print("\nError: PyInstaller not found. Please install it using 'pip install pyinstaller'.")
        sys.exit(1)

if __name__ == "__main__":
    package()
