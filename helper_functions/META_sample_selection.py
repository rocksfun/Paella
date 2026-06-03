"""META Sample Selection helper module.

This module provides functionality to select a sample folder and copy it to
a destination location. The source selection starts from the nas_sample_path
specified in system_config.txt, and the destination is the local_data_path.
If either path is not configured, standard directory selection dialogs are used.
"""

import os
import sys
import shutil
from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
)

# Ensure project root is on sys.path when this file is run directly
if hasattr(sys, '_MEIPASS'):
    _PROJECT_ROOT = sys._MEIPASS
else:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

from helper_functions.SYSTEM_pull_config_io import (  # noqa: E402
    load_system_config,
    get_reference_paths,
    SYSTEM_CONFIG_PATH,
)


def _find_system_config_path() -> Optional[str]:
    """Find the system config file path.

    First tries the default system path, then falls back to project references folder.

    Returns:
        Path to system config file, or None if not found.
    """
    # Try default system path first (the actual config location)
    if os.path.exists(SYSTEM_CONFIG_PATH):
        return SYSTEM_CONFIG_PATH

    # Fall back to project references folder
    project_config_path = os.path.join(_PROJECT_ROOT, "references", "system_config.txt")
    if os.path.exists(project_config_path):
        return project_config_path

    return None


def _get_nas_sample_path() -> Optional[str]:
    """Get nas_sample_path from system config file.

    Returns:
        nas_sample_path string or None if not found.
    """
    try:
        config_path = _find_system_config_path()
        if config_path is None:
            print(f"System config file not found. Tried:")
            print(f"  - {SYSTEM_CONFIG_PATH}")
            print(f"  - {os.path.join(_PROJECT_ROOT, 'references', 'system_config.txt')}")
            return None

        print(f"Loading config from: {config_path}")
        config = load_system_config(config_path)
        
        # Debug: print what sections we have
        print(f"Config sections found: {list(config.keys())}")
        
        if "references" in config:
            ref_config = config["references"]
            print(f"References section keys: {list(ref_config.keys())}")
            if "nas_sample_path" in ref_config:
                path = str(ref_config["nas_sample_path"])
                print(f"Found nas_sample_path: {path}")
                return path
            else:
                print("nas_sample_path not found in references section")
        else:
            print("'references' section not found in config")
        return None
    except Exception as e:
        import traceback
        print(f"Error getting nas_sample_path: {e}")
        traceback.print_exc()
        return None


def _get_local_data_path() -> Optional[str]:
    """Get local_data_path from system config file.

    Returns:
        local_data_path string or None if not found.
    """
    try:
        config_path = _find_system_config_path()
        if config_path is None:
            print(f"System config file not found. Tried:")
            print(f"  - {SYSTEM_CONFIG_PATH}")
            print(f"  - {os.path.join(_PROJECT_ROOT, 'references', 'system_config.txt')}")
            return None

        print(f"Loading config from: {config_path}")
        config = load_system_config(config_path)
        
        # Debug: print what sections we have
        print(f"Config sections found: {list(config.keys())}")
        
        if "references" in config:
            ref_config = config["references"]
            print(f"References section keys: {list(ref_config.keys())}")
            if "local_data_path" in ref_config:
                path = str(ref_config["local_data_path"])
                print(f"Found local_data_path: {path}")
                return path
            else:
                print("local_data_path not found in references section")
        else:
            print("'references' section not found in config")
        return None
    except Exception as e:
        import traceback
        print(f"Error getting local_data_path: {e}")
        traceback.print_exc()
        return None


def _select_directory(start_dir: Optional[str] = None, title: str = "Select Directory") -> Optional[str]:
    """Open a directory selection dialog.

    Args:
        start_dir: Optional starting directory for the dialog.
        title: Dialog window title.

    Returns:
        Selected directory path or None if cancelled.
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    selected_dir = QFileDialog.getExistingDirectory(
        None,
        title,
        start_dir if start_dir else "",
        QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks
    )

    if selected_dir:
        return os.path.normpath(selected_dir)
    return None


def _is_direct_child(child_path: str, parent_path: str) -> bool:
    """Check if child_path is a direct child directory of parent_path.

    Args:
        child_path: Path to check if it's a direct child.
        parent_path: Parent directory path.

    Returns:
        True if child_path is a direct child of parent_path, False otherwise.
    """
    child_norm = os.path.normpath(child_path)
    parent_norm = os.path.normpath(parent_path)

    # Check if they're the same path
    if child_norm == parent_norm:
        return False

    # Get the parent directory of the child
    child_parent = os.path.dirname(child_norm)
    child_parent_norm = os.path.normpath(child_parent)

    # Check if the child's parent is the specified parent path
    return child_parent_norm == parent_norm


def _copy_folder(source: str, destination: str, overwrite: bool = False) -> Tuple[bool, str]:
    """Copy specific files from a source folder to a new folder in the destination.

    Creates a new folder with the same name as the source and copies only:
    'ActiveSystems.txt', 'CLIAtag.txt', 'conditions.txt', and 'sample_name.txt'

    Args:
        source: Path to the source folder to copy files from.
        destination: Path to the destination directory where the new folder will be created.
        overwrite: If True, overwrite existing files. If False and folder exists, returns error.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not os.path.exists(source):
        return False, f"Source folder does not exist: {source}"

    if not os.path.isdir(source):
        return False, f"Source path is not a directory: {source}"

    source_name = os.path.basename(os.path.normpath(source))
    dest_path = os.path.join(destination, source_name)

    # List of files to copy
    files_to_copy = ['ActiveSystems.txt', 'CLIAtag.txt', 'conditions.txt', 'sample_name.txt']

    try:
        # Ensure destination directory exists
        if not os.path.exists(destination):
            os.makedirs(destination, exist_ok=True)

        # Create the new folder if it doesn't exist
        if not os.path.exists(dest_path):
            os.makedirs(dest_path, exist_ok=True)
        elif not overwrite:
            # Folder exists and we're not overwriting - this shouldn't happen if called correctly
            return False, f"Destination folder already exists: {dest_path}"

        # Copy only the specified files
        copied_files = []
        missing_files = []
        
        for filename in files_to_copy:
            source_file = os.path.join(source, filename)
            dest_file = os.path.join(dest_path, filename)
            
            if os.path.exists(source_file):
                shutil.copy2(source_file, dest_file)
                copied_files.append(filename)
            else:
                missing_files.append(filename)

        # Build success message
        if copied_files:
            if os.path.exists(dest_path) and overwrite:
                message = f"Successfully recopied files to existing folder '{source_name}': {', '.join(copied_files)}"
            else:
                message = f"Successfully created folder '{source_name}' and copied {len(copied_files)} file(s): {', '.join(copied_files)}"
            if missing_files:
                message += f"\nNote: {len(missing_files)} file(s) were not found in source: {', '.join(missing_files)}"
            return True, message
        else:
            return False, f"No files were copied. None of the required files were found in the source folder."

    except OSError as e:
        return False, f"OS error copying files: {e}"


def select_and_copy_sample() -> Optional[str]:
    """Select a sample folder and copy it to the local data path.

    This function:
    1. Gets nas_sample_path from system_config.txt (if available) for source selection
    2. Gets local_data_path from system_config.txt (if available) for destination
    3. Opens a directory selection dialog starting from nas_sample_path
    4. Copies the selected folder to local_data_path
    5. If either path is not configured, uses standard directory dialogs

    Returns:
        Path to the copied folder, or None if cancelled or on error.
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # Get nas_sample_path from config (for source selection)
    nas_sample_path = _get_nas_sample_path()

    # If nas_sample_path is not configured, ask user to select it
    if not nas_sample_path:
        QMessageBox.information(
            None,
            "NAS Sample Path Not Configured",
            "nas_sample_path not found in system_config.txt. "
            "Please select the NAS sample path (source location)."
        )
        nas_sample_path = _select_directory(
            title="Select NAS Sample Path (Source Location)"
        )
        if not nas_sample_path:
            return None

    # Ensure the source path exists
    if not os.path.exists(nas_sample_path):
        reply = QMessageBox.question(
            None,
            "Path Does Not Exist",
            f"The NAS sample path does not exist:\n{nas_sample_path}\n\n"
            "Would you like to create it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.makedirs(nas_sample_path, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(
                    None,
                    "Error",
                    f"Failed to create directory:\n{e}"
                )
                return None
        else:
            return None

    # Get local_data_path from config (for destination)
    local_data_path = _get_local_data_path()

    # If local_data_path is not configured, ask user to select it
    if not local_data_path:
        QMessageBox.information(
            None,
            "Local Data Path Not Configured",
            "local_data_path not found in system_config.txt. "
            "Please select the local data path (destination)."
        )
        local_data_path = _select_directory(
            title="Select Local Data Path (Destination)"
        )
        if not local_data_path:
            return None

    # Ensure the destination path exists
    if not os.path.exists(local_data_path):
        reply = QMessageBox.question(
            None,
            "Path Does Not Exist",
            f"The local data path does not exist:\n{local_data_path}\n\n"
            "Would you like to create it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.makedirs(local_data_path, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(
                    None,
                    "Error",
                    f"Failed to create directory:\n{e}"
                )
                return None
        else:
            return None

    # Select source folder (starting from nas_sample_path)
    source_folder = _select_directory(
        start_dir=nas_sample_path,
        title="Select Sample Folder to Copy"
    )

    if not source_folder:
        return None

    # Validate that the selected folder is a direct child of nas_sample_path
    nas_sample_norm = os.path.normpath(nas_sample_path)
    source_norm = os.path.normpath(source_folder)

    # Check if user selected the nas_sample_path itself
    if source_norm == nas_sample_norm:
        QMessageBox.warning(
            None,
            "Invalid Selection",
            f"You selected the NAS sample path itself:\n{nas_sample_path}\n\n"
            "Please select a subdirectory within this path, not the path itself."
        )
        return None

    # Check if the selected folder is a direct child of nas_sample_path
    if not _is_direct_child(source_folder, nas_sample_path):
        QMessageBox.warning(
            None,
            "Invalid Selection",
            f"The selected folder must be a direct subdirectory of:\n{nas_sample_path}\n\n"
            f"You selected: {source_folder}\n\n"
            "Please select a folder that is directly within the NAS sample path, "
            "not a nested subdirectory."
        )
        return None

    # Check if source is the same as destination
    dest_norm = os.path.normpath(local_data_path)
    if source_norm == dest_norm:
        QMessageBox.warning(
            None,
            "Invalid Selection",
            "Cannot copy folder to itself. Please select a different folder."
        )
        return None

    # Check if the folder already exists in local_data_path
    source_name = os.path.basename(os.path.normpath(source_folder))
    dest_path = os.path.join(local_data_path, source_name)
    overwrite = False
    
    if os.path.exists(dest_path) and os.path.isdir(dest_path):
        # Folder already exists - ask user if they want to recopy
        reply = QMessageBox.question(
            None,
            "Sample Already Exists",
            f"The sample '{source_name}' was already found in local data.\n\n"
            "Do you want to recopy the contents from the server?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.No:
            # User chose not to recopy - return the existing path
            return dest_path
        
        # User chose to recopy
        overwrite = True

    # Copy the folder
    success, message = _copy_folder(source_folder, local_data_path, overwrite=overwrite)

    if success:
        QMessageBox.information(
            None,
            "Success",
            message
        )
        return dest_path
    else:
        QMessageBox.critical(
            None,
            "Error",
            message
        )
        return None


def main() -> None:
    """Standalone entry point for sample selection and copying."""
    app = QApplication(sys.argv)
    result = select_and_copy_sample()
    if result:
        print(f"Sample copied to: {result}")
    else:
        print("Operation cancelled or failed.")


if __name__ == "__main__":
    main()
