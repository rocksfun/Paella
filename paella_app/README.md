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

## PyInstaller

```bash
cd paella_app
pip install pyinstaller
python helper_functions/package_paella.py
```

Output: `dist/` executable. Requires `C:/Paella local/system_config.txt` on the target PC.
