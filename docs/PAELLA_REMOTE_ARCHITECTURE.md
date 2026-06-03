# Paella Remote Dashboard — Architecture (v1.1)

## Roles

| Component | Location | Responsibility |
|-----------|----------|----------------|
| **Paella GUI + Remote Server** | Each Windows lab PC | Owns hardware; exposes REST + WebSocket on LAN |
| **Central Dashboard** | Any browser on LAN/VPN | Discovers hosts, streams status/frequency, sends commands |

## Network protocol (v1)

- **Base URL:** `http://<host>:<port>` (default port `8765`)
- **Auth:** header `X-Paella-Api-Key` (config: `references/remote_server_config.txt`)
- **API prefix:** `/api/v1`

### REST

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Liveness (no auth) |
| GET | `/api/v1/status` | Full status snapshot (auth) |
| POST | `/api/v1/commands` | Execute command `{ "command": "...", "params": {} }` |

### WebSocket

| Path | Stream |
|------|--------|
| `/api/v1/ws/status` | JSON status @ `status_interval_hz` (default 2 Hz) |
| `/api/v1/ws/frequency` | Throttled frequency packets when UDP active (default max 10 Hz) |

### Dashboard UI

- Served from Paella at `/dashboard/` (same origin per host) **or** open `dashboard/index.html` and point at multiple `host:port` entries.
- Phase 1: multi-host tiles, status polling, frequency charts, `ping` command test.

## Phased delivery

1. **Phase 1 (this implementation):** read-only status + frequency relay + `ping` command + static dashboard.
2. **Phase 2:** safe commands (request status refresh, GUI mode, read-only queries).
3. **Phase 3:** fluidic/SMR/camera actions with locks, confirmations, audit log.
4. **Phase 4:** optional registry service (central list of online systems).

## Safety rules (all phases)

- Only Paella process touches hardware.
- Commands are serialized per machine (queue + busy flag).
- Destructive actions require explicit params + future role checks.

## Running (v1.1)

**Lab PC:** `pip install -r requirements-remote.txt` → set `api_key` in `remote_server_config.txt` → `python main_gui.py`

**Dashboard PC:** `python run_dashboard.py` → open http://127.0.0.1:9080/ → Scan network

**Discovery:** UDP `9876` broadcast `PAELLA_DISCOVER` → reply `PAELLA_ANNOUNCE` with `api_host`, `api_port`, `system_id`

**Commands:** `run_clean` (complete_clean | rapid_clean | media_purge), `run_final_clean` (confirmed), `run_kickback`, `set_kickback_volume`, `pumps_connect`, `pumps_disconnect`, `pumps_initialize`
