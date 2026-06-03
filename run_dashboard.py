#!/usr/bin/env python3
"""Run the Paella central dashboard (local web app + UDP discovery)."""

import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if __name__ == "__main__":
    import uvicorn
    from dashboard_server.app import create_dashboard_app

    port = int(os.environ.get("PAELLA_DASHBOARD_PORT", "9080"))
    print(f"Paella Central Dashboard: http://127.0.0.1:{port}/")
    uvicorn.run(create_dashboard_app(), host="127.0.0.1", port=port, log_level="info")
