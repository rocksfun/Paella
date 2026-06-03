"""FastAPI application factory for Paella remote access (instrument-side only)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from helper_functions.paella_remote.bridge import PaellaRemoteBridge
from helper_functions.paella_remote.config import RemoteServerConfig
from helper_functions.paella_remote.constants import PAELLA_REMOTE_VERSION


class CommandRequest(BaseModel):
    command: str
    params: Dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None


def create_app(bridge: PaellaRemoteBridge, config: RemoteServerConfig) -> FastAPI:
    app = FastAPI(
        title="Paella Remote API",
        version=PAELLA_REMOTE_VERSION,
        description="REST and WebSocket API for Travera Paella instrument PCs (no UI)",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    @app.get("/api/v1/status")
    async def status() -> Dict[str, Any]:
        return bridge.get_status()

    @app.get("/api/v1/capabilities")
    async def capabilities() -> Dict[str, Any]:
        return bridge.execute_command("list_capabilities", {})

    @app.post("/api/v1/commands")
    async def commands(body: CommandRequest) -> Dict[str, Any]:
        result = bridge.execute_command(body.command, body.params)
        if body.request_id:
            result["request_id"] = body.request_id
        return result

    @app.websocket("/api/v1/ws/status")
    async def ws_status(websocket: WebSocket) -> None:
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

    return app
