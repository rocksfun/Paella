# Paella Remote API — Architecture (v1.2)

## Packages

| Package | Machine | Entry |
|---------|---------|--------|
| `paella_app` | Instrument PC | `main_gui.py` |
| `paella_dashboard` | Operator PC | `run_dashboard.py` |

No shared Python install required. Protocol constants are duplicated in `paella_app/helper_functions/paella_remote/constants.py` and `paella_dashboard/protocol/constants.py` — keep in sync for PyInstaller builds.

## Network protocol

- **Discovery (UDP 9876):** Dashboard broadcasts `PAELLA_DISCOVER` → each Paella replies `PAELLA_ANNOUNCE` with `api_host`, `api_port`, `system_id`.
- **API (HTTP/WS 8765):** No API key (trusted LAN). Optional `discovery_secret` in `remote_server_config.txt` on instruments only.

### REST (`/api/v1`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness |
| GET | `/status` | Status snapshot |
| GET | `/capabilities` | Supported commands |
| POST | `/commands` | Execute command |

### WebSocket

| Path | Stream |
|------|--------|
| `/api/v1/ws/status` | JSON status @ `status_interval_hz` |

### Commands

`run_clean`, `run_final_clean`, `run_kickback`, `set_kickback_volume`, `pumps_connect`, `pumps_disconnect`, `pumps_initialize`, `ping`, `get_status`, `list_capabilities`

## Running

**Instrument:** `cd paella_app && python main_gui.py`

**Dashboard:** `cd paella_dashboard && python run_dashboard.py` → http://127.0.0.1:9080/

## PyInstaller

- Instrument: `paella_app/references/paella.spec` via `helper_functions/package_paella.py`
- Dashboard: `paella_dashboard/paella_dashboard.spec`

## Security

Use VLAN/firewall to limit who can reach ports 8765 and 9876 on instrument PCs.
