# Paella Central Dashboard

Runs on an **operator PC** on the lab network. Does **not** include Paella hardware code or `main_gui.py`.

## Run from source

```bash
cd paella_dashboard
pip install -r requirements.txt
python run_dashboard.py
```

Browser: **http://127.0.0.1:9080/** → **Scan network** to find instrument PCs running Paella.

## PyInstaller (dashboard .exe)

1. Install PyInstaller in a dedicated env with `requirements.txt`.
2. From `paella_dashboard/`:

```bash
pyinstaller --noconfirm paella_dashboard.spec
```

3. Distribute `dist/PaellaDashboard/` — run the executable on any operator PC on the VLAN.

**Bundle requirements:** `static/index.html`, `server/`, `protocol/` (see `.spec` file).

## Protocol sync

`protocol/constants.py` must match `paella_app/helper_functions/paella_remote/constants.py` (discovery magic strings and default ports) when you change the wire format.

## Optional discovery secret

If instrument configs set `discovery_secret`, enter the same value in the dashboard scan UI.
