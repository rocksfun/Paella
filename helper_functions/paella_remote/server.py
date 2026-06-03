"""FastAPI application factory for Paella remote access."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from helper_functions.paella_remote.bridge import PaellaRemoteBridge
from helper_functions.paella_remote.config import RemoteServerConfig
from helper_functions.paella_remote.constants import PAELLA_REMOTE_VERSION

_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"


class CommandRequest(BaseModel):
    command: str
    params: Dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None


def create_app(bridge: PaellaRemoteBridge, config: RemoteServerConfig) -> FastAPI:
    app = FastAPI(
        title="Paella Remote API",
        version=PAELLA_REMOTE_VERSION,
        description="REST and WebSocket interface for Travera Paella lab systems",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def verify_api_key(x_paella_api_key: Optional[str] = Header(default=None)) -> None:
        if not x_paella_api_key or x_paella_api_key != config.api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Paella-Api-Key")

    @app.get("/api/v1/health")
    async def health() -> Dict[str, Any]:
        status = bridge.get_status()
        return {
            "ok": True,
            "service": "paella-remote",
            "version": PAELLA_REMOTE_VERSION,
            "system_id": status.get("system_id"),
            "hostname": status.get("hostname"),
        }

    @app.get("/api/v1/status", dependencies=[Depends(verify_api_key)])
    async def status() -> Dict[str, Any]:
        return bridge.get_status()

    @app.get("/api/v1/capabilities", dependencies=[Depends(verify_api_key)])
    async def capabilities() -> Dict[str, Any]:
        return bridge.execute_command("list_capabilities", {})

    @app.post("/api/v1/commands", dependencies=[Depends(verify_api_key)])
    async def commands(body: CommandRequest) -> Dict[str, Any]:
        result = bridge.execute_command(body.command, body.params)
        if body.request_id:
            result["request_id"] = body.request_id
        return result

    @app.websocket("/api/v1/ws/status")
    async def ws_status(websocket: WebSocket) -> None:
        key = websocket.query_params.get("api_key")
        if key != config.api_key:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        interval = 1.0 / max(config.status_interval_hz, 0.1)
        try:
            while True:
                await websocket.send_json(bridge.get_status())
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass

    if _DASHBOARD_DIR.is_dir():
        app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")

        @app.get("/")
        async def root_redirect():
            index = _DASHBOARD_DIR / "index.html"
            if index.exists():
                return FileResponse(index)
            return {"message": "Paella Remote API", "docs": "/docs"}

    return app
