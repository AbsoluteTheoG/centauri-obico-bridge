"""
centauri-obico-bridge
=====================

A Moonraker-compatible bridge for stock Elegoo Centauri printers.

Supported printers (``PRINTER_PROTOCOL``):

* ``sdcp`` (default) - Centauri Carbon.  SDCP over WebSocket
  (ws://HOST:3030/websocket), camera at http://HOST:3031/video.
* ``cc2`` - Centauri Carbon 2.  MQTT broker on the printer (HOST:1883,
  LAN Only Mode), camera at http://HOST:8080/?action=stream.

moonraker-obico only understands Moonraker, so this process translates between
the printer and the Moonraker API.  The printer-specific clients live in the
``printers`` package; this module only contains the HTTP / websocket API.

Everything is read-only against the printer firmware: no flashing, no firmware
modification.  All state is kept in this process and the deployment is fully
reversible (stop the container).

Reverse-engineering references:
  * main.js / chunk25.js (stock Elegoo web UI) - SDCP command IDs and status codes
  * raw.json - SDCP capture
  * https://github.com/danielcherubini/elegoo-homeassistant - CC2 MQTT protocol
  * https://github.com/TheSpaghettiDetective/moonraker-obico - API consumer
"""

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

from printers.base import BasePrinter, PrinterError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


PRINTER_PROTOCOL = _env("PRINTER_PROTOCOL", "sdcp").strip().lower()
HOST = _env("CENTAURI_HOST", "192.168.1.43")
MOONRAKER_PORT = int(_env("MOONRAKER_PORT", "7125"))
WEB_PORT = int(_env("WEB_PORT", "8080"))
API_KEY = _env("MOONRAKER_API_KEY", "centauri-bridge")
# Optional explicit public base URL used in /server/webcams/list.  When empty the
# bridge derives it from the incoming request, which is what moonraker-obico used.
PUBLIC_URL = _env("BRIDGE_PUBLIC_URL", "").rstrip("/")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("centauri-bridge")


def make_bridge() -> BasePrinter:
    """Create the printer client selected by ``PRINTER_PROTOCOL``."""
    if PRINTER_PROTOCOL == "sdcp":
        from printers.sdcp import SdcpPrinter

        return SdcpPrinter()
    if PRINTER_PROTOCOL == "cc2":
        from printers.cc2 import Cc2Printer

        return Cc2Printer()
    raise SystemExit(f"Unknown PRINTER_PROTOCOL {PRINTER_PROTOCOL!r}; expected 'sdcp' or 'cc2'")


bridge: BasePrinter = make_bridge()


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(bridge.run()), asyncio.create_task(bridge.frame_reader())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Centauri Obico Bridge", lifespan=lifespan)


def result(data: Any) -> JSONResponse:
    return JSONResponse({"result": data})


def error_response(exc: PrinterError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": str(exc)}})


async def request_params(request: Request) -> dict:
    """Merge query params with a JSON or form-encoded body.

    moonraker-obico's ``api_post`` sends parameters as form data
    (``application/x-www-form-urlencoded``), while curl/other clients may use
    the query string or JSON, so accept all three.
    """
    params: dict[str, Any] = dict(request.query_params)
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
    else:
        with contextlib.suppress(Exception):
            form = await request.form()
            params.update({k: v for k, v in form.items()})
    return params


# -- diagnostics ----------------------------------------------------------- #

@app.get("/health")
async def health():
    return {
        "ok": True,
        "centauri_connected": bridge.connected,
        "last_rx": bridge.last_rx,
        "last_error": bridge.last_error,
        "mainboard_id": bridge.mainboard_id,
        "print_state": bridge.map_print_state(),
        "ws_clients": len(bridge.clients),
        "notify_count": bridge.notify_count,
    }


@app.get("/debug/raw")
async def debug_raw():
    return JSONResponse({
        "responses": list(bridge.responses.values()),
        "response_history": list(bridge.response_history),
        "last_response": {str(k): v for k, v in bridge.last_response.items()},
    })


@app.get("/debug/messages")
async def debug_messages(limit: int = 100):
    return JSONResponse({"messages": list(bridge.messages)[-limit:]})


@app.get("/debug/state")
async def debug_state():
    return JSONResponse(bridge.debug_state())


# -- Moonraker server / printer info --------------------------------------- #

@app.get("/server/info")
async def server_info():
    return result({
        "klippy_connected": bridge.connected,
        "klippy_state": "ready" if bridge.connected else "shutdown",
        "components": ["application", "websockets", "klippy_connection", "history"],
        "failed_components": [],
        "registered_directories": ["gcodes"],
        "moonraker_version": "centauri-bridge",
        "api_version": [1, 0, 0],
        "api_version_string": "1.0.0",
    })


@app.get("/server/config")
async def server_config():
    return result({
        "config": {},
        "orig": {},
        "files": [],
        "warnings": [],
    })


@app.get("/access/api_key")
async def access_api_key():
    return result(API_KEY)


@app.get("/printer/info")
async def printer_info():
    return result(bridge.printer_info())


# -- printer objects ------------------------------------------------------- #

ALL_OBJECTS = [
    "print_stats", "virtual_sdcard", "display_status", "gcode_move", "toolhead",
    "fan", "extruder", "heater_bed", "heaters", "webhooks", "idle_timeout",
    "history", "configfile",
]


@app.get("/printer/objects/list")
async def objects_list():
    return result({"objects": ALL_OBJECTS})


def select_objects(requested: Any) -> dict:
    objects = bridge.build_objects()
    if not requested:
        return objects
    keys = set(requested.keys()) if isinstance(requested, dict) else set(requested)
    if not keys:
        return objects
    return {k: v for k, v in objects.items() if k in keys or k == "heaters"}


@app.get("/printer/objects/query")
async def objects_query(request: Request):
    objects = select_objects(dict(request.query_params))
    return result({"status": objects, "eventtime": time.time()})


@app.post("/printer/objects/query")
async def objects_query_post(request: Request):
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    objects = select_objects(body.get("objects") if isinstance(body, dict) else None)
    return result({"status": objects, "eventtime": time.time()})


@app.post("/printer/objects/subscribe")
async def objects_subscribe(request: Request):
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    objects = select_objects(body.get("objects") if isinstance(body, dict) else None)
    return result({"status": objects, "eventtime": time.time()})


# -- files ----------------------------------------------------------------- #

@app.get("/server/files/list")
async def files_list():
    return result(bridge.file_entries())


@app.get("/server/files/metadata")
async def files_metadata(filename: str = ""):
    # Always return a valid "result" object: moonraker-obico dereferences it
    # even for an empty filename during the printer's prepare phase.
    return result(bridge.build_metadata(filename))


@app.post("/server/files/upload")
async def files_upload(request: Request):
    """Store a Moonraker upload on the printer."""
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return JSONResponse(status_code=400, content={"error": {"code": 400, "message": "file is required"}})
    data = await upload.read()
    filename = pathlib.PurePosixPath(upload.filename).name
    try:
        item = await bridge.upload_file(filename, data)
    except PrinterError as exc:
        return error_response(exc)
    # Moonraker wraps in "result"; moonraker-obico reads the top-level "item".
    return JSONResponse({"result": {"item": item}, "item": item})


# -- history --------------------------------------------------------------- #

@app.get("/server/history/list")
async def history_list(limit: int = 50):
    jobs = bridge.build_jobs()
    return result({"jobs": jobs[:limit], "count": len(jobs)})


@app.get("/server/history/totals")
async def history_totals():
    jobs = bridge.build_jobs()
    return result({
        "job_totals": {
            "total_jobs": len(jobs),
            "total_print_time": sum(j.get("print_duration", 0) for j in jobs),
            "total_filament_used": 0,
            "longest_print": max([j.get("print_duration", 0) for j in jobs] or [0]),
        }
    })


# -- database / machine ---------------------------------------------------- #

_db: dict[str, Any] = {}


@app.get("/server/database/item")
async def database_item_get(namespace: str = "", key: str = ""):
    value = _db.get(f"{namespace}/{key}")
    if value is None and namespace == "mainsail" and key == "presets":
        value = {"presets": {}}
    return result({"namespace": namespace, "key": key, "value": value})


@app.post("/server/database/item")
async def database_item_post(request: Request):
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    ns = body.get("namespace", "")
    key = body.get("key", "")
    _db[f"{ns}/{key}"] = body.get("value")
    return result({"namespace": ns, "key": key, "value": body.get("value")})


@app.get("/machine/update/status")
async def machine_update_status():
    return result({"version_info": {}, "busy": False})


@app.get("/machine/system_info")
async def machine_system_info():
    return result({"system_info": {"cpu_info": bridge.cpu_info()}})


@app.get("/machine/device_power/devices")
async def device_power_devices():
    return result({"devices": []})


# -- print control --------------------------------------------------------- #

async def _control(action) -> JSONResponse:
    try:
        await action()
    except PrinterError as exc:
        return error_response(exc)
    return result("ok")


@app.post("/printer/print/start")
async def print_start(request: Request):
    params = await request_params(request)
    filename = str(params.get("filename") or "")
    if not filename:
        return JSONResponse(status_code=400, content={"error": {"code": 400, "message": "filename is required"}})
    return await _control(lambda: bridge.start_print(filename))


@app.post("/printer/print/pause")
async def print_pause():
    return await _control(bridge.pause)


@app.post("/printer/print/resume")
async def print_resume():
    return await _control(bridge.resume)


@app.post("/printer/print/cancel")
async def print_cancel():
    return await _control(bridge.cancel)


@app.post("/printer/print/stop")
async def print_stop():
    return await _control(bridge.cancel)


@app.post("/printer/emergency_stop")
async def emergency_stop():
    return await _control(bridge.cancel)


# -- gcode script translation ---------------------------------------------- #

@app.post("/printer/gcode/script")
async def gcode_script(request: Request):
    params = await request_params(request)
    script = str(params.get("script") or "")
    await bridge.run_gcode(script)
    return result("ok")


# -- webcams --------------------------------------------------------------- #

def _base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    return str(request.base_url).rstrip("/")


@app.get("/server/webcams/list")
async def webcams_list(request: Request):
    base = _base_url(request)
    return result({
        "webcams": [{
            "name": bridge.camera_name,
            "location": "printer",
            "service": "mjpegstreamer",
            "stream_url": f"{base}/webcam/stream",
            "snapshot_url": f"{base}/webcam/snapshot",
            "flip_horizontal": False,
            "flip_vertical": False,
            "rotation": 0,
            "target_fps": 15,
            "enabled": True,
        }]
    })


@app.get("/webcam/stream")
async def webcam_stream():
    return StreamingResponse(
        bridge.stream_frames(),
        media_type="multipart/x-mixed-replace; boundary=--foo",
        headers={"Content-Type": "multipart/x-mixed-replace; boundary=--foo"},
    )


@app.get("/webcam/snapshot")
async def webcam_snapshot():
    """Return the latest cached JPEG frame (falling back to a live capture)."""
    if bridge.latest_frame and time.time() - bridge.latest_frame_ts < 10:
        return Response(content=bridge.latest_frame, media_type="image/jpeg")
    buf = bytearray()
    try:
        async for chunk in bridge.stream_frames(timeout=10):
            buf.extend(chunk)
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if start >= 0 and end >= 0:
                return Response(content=bytes(buf[start:end + 2]), media_type="image/jpeg")
            if len(buf) > 8_000_000:
                break
    except Exception as exc:
        log.warning("snapshot failed: %s", exc)
    return JSONResponse(status_code=502, content={"error": {"code": 502, "message": "Unable to capture camera frame"}})


@app.get("/video")
async def video():
    return await webcam_stream()


# -- websocket JSON-RPC (Moonraker compatible) ----------------------------- #

def _jsonrpc_result(msg_id: Any, res: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": res}


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


@app.websocket("/websocket")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    bridge.clients.add(ws)
    try:
        await ws.send_text(json.dumps({
            "jsonrpc": "2.0",
            "method": "notify_klippy_ready",
            "params": [],
        }))
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            method = data.get("method")
            msg_id = data.get("id")
            params = data.get("params") or {}
            if method == "server.connection.identify":
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, {
                    "connection_id": uuid.uuid4().hex,
                    "client_name": params.get("client_name", ""),
                    "version": params.get("version", ""),
                    "type": params.get("type", ""),
                })))
            elif method == "connection.register_remote_method":
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, "ok")))
            elif method == "printer.objects.list":
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, {"objects": ALL_OBJECTS})))
            elif method == "printer.objects.subscribe":
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, {
                    "status": bridge.build_objects(),
                    "eventtime": time.time(),
                })))
            elif method == "printer.objects.query":
                requested = params.get("objects") if isinstance(params, dict) else None
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, {
                    "status": select_objects(requested),
                    "eventtime": time.time(),
                })))
            elif method == "printer.gcode.script":
                script = params.get("script", "") if isinstance(params, dict) else ""
                await bridge.run_gcode(script)
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, "ok")))
            elif method == "printer.info":
                await ws.send_text(json.dumps(_jsonrpc_result(msg_id, {
                    "state": "ready" if bridge.connected else "shutdown",
                })))
            else:
                await ws.send_text(json.dumps(_jsonrpc_error(
                    msg_id, -32601, f"Method not found: {method}"
                )))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket error: %s", exc)
    finally:
        bridge.clients.discard(ws)


# -- legacy aliases -------------------------------------------------------- #

@app.get("/webcams/list")
async def webcams_list_legacy(request: Request):
    return await webcams_list(request)


@app.get("/api/status")
async def api_status():
    return JSONResponse(bridge.build_objects())


@app.get("/")
async def root():
    return {
        "service": "centauri-obico-bridge",
        "printer": HOST,
        **bridge.describe(),
        "moonraker_compat": f"http://0.0.0.0:{MOONRAKER_PORT}",
        "connected": bridge.connected,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=MOONRAKER_PORT)
