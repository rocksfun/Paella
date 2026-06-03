# Paella

Travera SMR lab software: **instrument control** on each bench PC and a **central dashboard** on a separate operator PC.

## Repository layout

```
Paella/
├── paella_app/           # Install on each instrument PC (Windows + hardware)
│   ├── main_gui.py       # Entry point
│   ├── pyImage.py, pyPump.py, pySMR.py
│   ├── helper_functions/ # Includes paella_remote (REST + UDP discovery)
│   ├── references/       # Config, pump routines, paella.spec (PyInstaller)
│   └── requirements-remote.txt
│
└── paella_dashboard/     # Install on operator PC only (no Paella GUI)
    ├── run_dashboard.py  # Entry point
    ├── server/           # Local web server + UDP scan
    ├── static/           # Browser UI
    ├── protocol/         # Discovery constants (keep in sync with paella_app)
    ├── requirements.txt
    └── paella_dashboard.spec
```

The two packages talk over the **LAN** only. They do not need to live on the same machine or share a Python environment.

## Quick start

### Instrument PC

```bash
cd paella_app
pip install -r requirements-remote.txt
# conda env from paella_env.yml for full hardware stack
python main_gui.py
```

Remote API (no auth): `http://<pc-ip>:8765` · UDP discovery: port `9876`

### Dashboard PC

```bash
cd paella_dashboard
pip install -r requirements.txt
python run_dashboard.py
```

Open **http://127.0.0.1:9080/** → **Scan network**

## PyInstaller

- **Instrument (Windows):** `cd paella_app` → `build_windows.bat` → copy entire **`PaellaApp/`** folder to each lab PC. See **`BUILD_ON_WINDOWS.txt`**. Each PC needs `C:\Paella local\system_config.txt`.
- **Dashboard (Windows):** from `paella_dashboard`, build with `paella_dashboard.spec`. Entry: `run_dashboard.py`. Bundle `static/` and `protocol/` as data files.

See `paella_app/docs/PAELLA_REMOTE_ARCHITECTURE.md` for API and protocol details.

## Security note

Remote control assumes a **trusted lab LAN**. Use firewall rules and optional `discovery_secret` in `paella_app/references/remote_server_config.txt` if needed.
