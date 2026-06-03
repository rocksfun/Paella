"""FastAPI server for the central Paella dashboard (static UI + UDP scan API)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.discovery_client import scan_network

_DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
_STATIC_DIR = _DASHBOARD_ROOT / "static"
_DISCOVERED: List[Dict[str, Any]] = []


class ScanRequest(BaseModel):
    discovery_port: int = 9876
    discovery_secret: str = ""
    timeout_sec: float = 3.0


def create_dashboard_app() -> FastAPI:
    app = FastAPI(title="Paella Central Dashboard", version="1.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/api/scan")
    async def scan(body: ScanRequest) -> Dict[str, Any]:
        global _DISCOVERED
        _DISCOVERED = scan_network(
            discovery_port=body.discovery_port,
            discovery_secret=body.discovery_secret,
            timeout_sec=body.timeout_sec,
        )
        return {"ok": True, "count": len(_DISCOVERED), "systems": _DISCOVERED}

    @app.get("/api/systems")
    async def systems() -> Dict[str, Any]:
        return {"ok": True, "systems": _DISCOVERED}

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(_STATIC_DIR / "index.html")

    return app
