# Paella Instrument Application

Runs on **Windows lab PCs** connected to SMR hardware (cameras, pumps, FPGA/DAQ).

## Run from source

```bash
cd paella_app
conda activate paella   # see paella_env.yml
pip install -r requirements-remote.txt
python main_gui.py
```

## Remote API (for central dashboard)

Started automatically with the GUI when `[remote_server] enabled = true` in:

- `C:/Paella local/remote_server_config.txt` (production), or
- `references/remote_server_config.txt` (development)

| Port | Purpose |
|------|---------|
| 8765 | HTTP + WebSocket (`/api/v1/...`) |
| 9876 | UDP discovery replies |

No API key — restrict access via network/firewall.

## PyInstaller — portable `PaellaApp` folder

**Must run on Windows** (builds a Windows `.exe`).

```bat
cd paella_app
build_windows.bat
```

Or manually:

```bat
conda activate paella
pip install pyinstaller
pip install -r requirements-remote.txt
python helper_functions\package_paella.py
```

**Output:** `PaellaApp\` next to the repo (entire folder — copy to each lab PC).

On each PC: create `C:\Paella local\system_config.txt`, then run `Paella.exe`.
See `PaellaApp\START_HERE.txt` after building.
