"""Build portable PaellaApp folder for Windows lab PCs (PyInstaller)."""

import os
import shutil
import subprocess
import sys


def _write_start_here(dest_dir: str) -> None:
    text = """Paella Instrument Application — ready to run on this PC
============================================================

1. FIRST TIME ON THIS COMPUTER
   Create folder:  C:\\Paella local\\
   Copy your lab system_config.txt to:
      C:\\Paella local\\system_config.txt

   (Optional) For central dashboard remote control, copy:
      ConfigExamples\\remote_server_config.txt
   to:
      C:\\Paella local\\remote_server_config.txt

2. RUN PAELLA
   Double-click:  Paella.exe

3. COPY TO OTHER PCs
   Copy this ENTIRE PaellaApp folder to each instrument PC.
   Each PC still needs its own C:\\Paella local\\system_config.txt.

4. DASHBOARD
   The central dashboard is a separate build (paella_dashboard).
   This folder is only for instrument / lab PCs.

Support: see repo README or paella_app/docs/PAELLA_REMOTE_ARCHITECTURE.md
"""
    with open(os.path.join(dest_dir, "START_HERE.txt"), "w", encoding="utf-8") as f:
        f.write(text)


def _copy_config_examples(paella_app_root: str, dest_dir: str) -> None:
    examples = os.path.join(dest_dir, "ConfigExamples")
    os.makedirs(examples, exist_ok=True)
    src_remote = os.path.join(paella_app_root, "references", "remote_server_config.txt")
    if os.path.isfile(src_remote):
        shutil.copy2(src_remote, os.path.join(examples, "remote_server_config.txt"))
    note = os.path.join(examples, "README.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write(
            "Copy remote_server_config.txt to C:\\Paella local\\ if using the central dashboard.\n"
            "system_config.txt is NOT included — obtain from your lab configuration.\n"
        )


def package():
    print("Starting Paella packaging → PaellaApp folder...")
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_script_dir)
    repo_root = os.path.dirname(project_root)
    paella_app_out = os.path.join(repo_root, "PaellaApp")
    spec_file = os.path.join(project_root, "references", "paella.spec")

    os.chdir(project_root)
    print(f"Working directory: {project_root}")

    if not os.path.exists(spec_file):
        print(f"Error: Spec file not found at {spec_file}")
        sys.exit(1)

    if sys.platform != "win32":
        print(
            "\n*** NOTE: You are not on Windows. PyInstaller will build for THIS OS only.\n"
            "    For a Windows .exe, run this same script on a Windows PC with conda env 'paella':\n"
            "    cd paella_app\n"
            "    build_windows.bat\n"
            "***\n"
        )

    dist_default = os.path.join(project_root, "dist", "PaellaApp")
    if os.path.isdir(paella_app_out):
        shutil.rmtree(paella_app_out)
    if os.path.isdir(dist_default):
        shutil.rmtree(dist_default)

    base_args = ["--noconfirm", "--clean", "--distpath", repo_root, spec_file]
    for cmd in (
        ["pyinstaller"] + base_args,
        [sys.executable, "-m", "PyInstaller"] + base_args,
    ):
        print(f"Running: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
            break
        except subprocess.CalledProcessError as e:
            print(f"\nError during packaging: {e}")
            sys.exit(1)
        except FileNotFoundError:
            continue
    else:
        print(
            "\nError: PyInstaller not found in this Python environment.\n"
            f"  Python used: {sys.executable}\n"
            "  Fix (run in the same terminal):\n"
            "    pip install pyinstaller\n"
            "    pip install -r requirements-remote.txt\n"
            "  If you use conda:\n"
            "    conda activate paella\n"
            "    pip install pyinstaller\n"
            "    python helper_functions\\package_paella.py"
        )
        sys.exit(1)

    if not os.path.isdir(paella_app_out):
        if os.path.isdir(dist_default):
            shutil.move(dist_default, paella_app_out)
        else:
            print("Error: PaellaApp folder was not created.")
            sys.exit(1)

    _write_start_here(paella_app_out)
    _copy_config_examples(project_root, paella_app_out)

    print("\nPackaging successful!")
    print(f"Portable app folder: {paella_app_out}")
    print("Copy the entire PaellaApp folder to each Windows lab PC.")
    print("Run Paella.exe inside that folder after setting C:\\Paella local\\system_config.txt")


if __name__ == "__main__":
    package()
