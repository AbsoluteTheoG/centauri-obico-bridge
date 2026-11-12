"""Elegoo Centauri Carbon (SDCP over WebSocket).

The printer speaks SDCP over WebSocket (ws://HOST:3030/websocket) and exposes
its camera as an MJPEG stream (http://HOST:3031/video).

Reverse-engineering references:
  * main.js / chunk25.js (stock Elegoo web UI) - command IDs and status codes
  * raw.json - SDCP capture
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import re
import time
import uuid
from typing import Any, Optional

import httpx
import websockets

from .base import (
    BasePrinter,
    CommandRejected,
    PrinterNotConnected,
    PrinterTimeout,
    UpstreamError,
    log,
)


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


HOST = _env("CENTAURI_HOST", "192.168.1.43")
SDCP_PORT = int(_env("CENTAURI_SDCP_PORT", "3030"))
VIDEO_PORT = int(_env("CENTAURI_VIDEO_PORT", "3031"))
POLL_INTERVAL = float(_env("POLL_INTERVAL", "2"))
FILELIST_INTERVAL = float(_env("FILELIST_INTERVAL", "60"))
HISTORY_INTERVAL = float(_env("HISTORY_INTERVAL", "20"))
RAW_LOG = _env("SDCP_RAW_LOG", "")
# Bed levelling (Calibration_switch) sent on START_PRINT.
START_CALIBRATION = int(_env("START_CALIBRATION", "1"))
START_PLATFORM_TYPE = int(_env("START_PLATFORM_TYPE", "0"))
START_TIMELAPSE = int(_env("START_TIMELAPSE", "0"))

SDCP_WS = f"ws://{HOST}:{SDCP_PORT}/websocket"
VIDEO_URL = f"http://{HOST}:{VIDEO_PORT}/video"


# --------------------------------------------------------------------------- #
# SDCP command IDs (from the stock UI webpack module 543)
# --------------------------------------------------------------------------- #

CMD_STATUS = 0
CMD_ATTR = 1
CMD_DISCONNECT = 64
CMD_START_PRINT = 128
CMD_SUSPEND_PRINT = 129
CMD_STOP_PRINT = 130
CMD_RESTORE_PRINT = 131
CMD_BLACKOUT_STATUS = 134
CMD_BLACKOUT_ACTION = 135
CMD_EDIT_NAME = 192
CMD_EDIT_FILE_NAME = 257
CMD_FILE_LIST = 258
CMD_DELETE_FILE_LIST = 259
CMD_FILE_DETAIL = 260
CMD_HISTORY_ID = 320
CMD_TASK_DETAIL = 321
CMD_DELETE_HISTORY = 322
CMD_HISTORY_VIDEO = 323
CMD_MATERIAL_DATA = 324
CMD_VIDEO_STREAMING = 386
CMD_TIME_LAPSE = 387
CMD_AXIS_NUMBER = 401
CMD_AXIS_ZERO = 402
CMD_STATUS_DATA = 403
CMD_FILE_COLOR = 503

CMD_NAMES = {
    CMD_STATUS: "GET_PRINTER_STATUS",
    CMD_ATTR: "GET_PRINTER_ATTR",
    CMD_START_PRINT: "START_PRINT",
    CMD_SUSPEND_PRINT: "SUSPEND_PRINT",
    CMD_STOP_PRINT: "STOP_PRINT",
    CMD_RESTORE_PRINT: "RESTORE_PRINT",
    CMD_FILE_LIST: "GET_PRINTER_FILE_LIST",
    CMD_FILE_DETAIL: "GET_PRINTER_FILE_DETAIL",
    CMD_HISTORY_ID: "GET_PRINTER_HISTORY_ID",
    CMD_TASK_DETAIL: "GET_PRINTER_TASK_DETAIL",
    CMD_AXIS_NUMBER: "EDIT_PRINTER_AXIS_NUMBER",
    CMD_AXIS_ZERO: "EDIT_PRINTER_AXIS_ZERO",
    CMD_STATUS_DATA: "EDIT_PRINTER_STATUS_DATA",
}

# PrintInfo.Status -> UI state (chunk25.js printerStatus constants)
STATUS_PRINTING = {13}
STATUS_PAUSED = {5, 6}
STATUS_COMPLETE = {9}
STATUS_CANCELLED = {8, 14}
STATUS_LOADING = {0, 1, 15, 16, 18, 19, 20, 21}
STATUS_FILE_DETECTION = {10}
STATUS_RECOVERY = {12}


def sdcp_envelope(cmd: int, data: Any = None, mainboard_id: str = "") -> dict:
    return {
        "Id": "",
        "Data": {
            "Cmd": cmd,
            "Data": data if data is not None else {},
            "RequestID": uuid.uuid4().hex,
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time() * 1000),
            "From": 1,
        },
    }


def basename(path: str) -> str:
    return pathlib.PurePosixPath(path or "").name


def normalize_filename(name: str) -> str:
    """Accept Moonraker-style 'gcodes/foo.gcode' and SDCP '/local/foo.gcode'."""
    if not name:
        return name
    name = name.strip()
    if name.startswith("gcodes/"):
        name = "/local/" + name[len("gcodes/"):]
    elif not name.startswith("/local/") and not name.startswith("/"):
        name = "/local/" + name
    return name


# --------------------------------------------------------------------------- #
# Bridge state / SDCP client
# --------------------------------------------------------------------------- #

class SdcpPrinter(BasePrinter):
    def __init__(self) -> None:
        super().__init__()
        self.ws: Optional[Any] = None
        self.status: dict[str, Any] = {}
        self.pending: dict[str, asyncio.Future] = {}

        self.file_list: list[dict] = []
        self.file_list_ts: float = 0
        self.file_detail: dict[str, Any] = {}
        self.task_detail: dict[str, Any] = {}
        self.history_ids: list[str] = []
        self._last_task_id: Optional[str] = None
        self.open_raw_log(RAW_LOG)

    @property
    def video_url(self) -> str:
        return VIDEO_URL

    def describe(self) -> dict:
        return {"sdcp": SDCP_WS, "video": VIDEO_URL}

    def debug_state(self) -> dict:
        return {
            "connected": self.connected,
            "last_rx": self.last_rx,
            "last_error": self.last_error,
            "mainboard_id": self.mainboard_id,
            "status": self.status,
            "attributes": self.attributes,
            "task_detail": self.task_detail,
            "file_list": self.file_list,
            "history_ids": self.history_ids,
            "objects": self.build_objects(),
        }

    # -- SDCP send / receive ----------------------------------------------- #

    async def send_command(
        self,
        cmd: int,
        data: Any = None,
        *,
        wait: bool = False,
        timeout: float = 8.0,
    ) -> Optional[dict]:
        if not self.ws or not self.connected:
            raise RuntimeError("Not connected to printer SDCP")
        msg = sdcp_envelope(cmd, data, self.mainboard_id)
        rid = msg["Data"]["RequestID"]
        fut: Optional[asyncio.Future] = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self.pending[rid] = fut
        self._record("tx", msg)
        await self.ws.send(json.dumps(msg, separators=(",", ":")))
        if not wait:
            return None
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            return resp.get("Data")
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            raise TimeoutError(f"No SDCP response for cmd {cmd} within {timeout}s")

    def _handle_message(self, msg: dict) -> None:
        topic = str(msg.get("Topic", ""))
        self.last_rx = time.time()
        self._record("rx", msg)

        if "status" in topic and isinstance(msg.get("Status"), dict):
            self.status = msg["Status"]
            if msg.get("MainboardID"):
                self.mainboard_id = msg["MainboardID"]
            self._track_filename()
            self._maybe_fetch_task_detail()
            asyncio.create_task(self._notify_if_changed())
        elif "attributes" in topic and isinstance(msg.get("Attributes"), dict):
            self.attributes = msg["Attributes"]
            if msg.get("MainboardID"):
                self.mainboard_id = msg["MainboardID"]
        elif "response" in topic:
            d = msg.get("Data") or {}
            cmd = d.get("Cmd")
            rid = d.get("RequestID")
            key = f"{cmd}:{rid}"
            self.responses[key] = msg
            if len(self.responses) > 400:
                self.responses.popitem(last=False)
            self.response_history.append(msg)
            if cmd is not None:
                self.last_response[cmd] = d
            self._process_response(d)
            fut = self.pending.pop(rid, None) if rid else None
            if fut and not fut.done():
                fut.set_result(d)
        elif "error" in topic:
            self.responses[f"error:{time.time()}"] = msg
            self.response_history.append(msg)

    def _process_response(self, d: dict) -> None:
        cmd = d.get("Cmd")
        payload = d.get("Data") or {}
        if cmd == CMD_FILE_LIST and payload.get("Ack") == 0:
            self.file_list = payload.get("FileList") or []
            self.file_list_ts = time.time()
        elif cmd == CMD_FILE_DETAIL and payload.get("Ack") == 0:
            info = payload.get("FileInfo") or {}
            if info:
                self.file_detail = info
        elif cmd == CMD_TASK_DETAIL and payload.get("Ack") == 0:
            details = payload.get("HistoryDetailList") or []
            if details:
                self.task_detail = details[0]
        elif cmd == CMD_HISTORY_ID and payload.get("Ack") == 0:
            self.history_ids = payload.get("HistoryData") or []

    def _track_filename(self) -> None:
        """Keep a filename available during the printer's prepare phase.

        The printer briefly reports ``standby`` right after START_PRINT and only
        fills ``PrintInfo.Filename`` once actual printing begins, so we must not
        clear the remembered filename on a transient standby.
        """
        info = self.status.get("PrintInfo") or {}
        if info.get("Filename"):
            self.active_filename = info["Filename"]
        elif not info.get("TaskId") and self.map_print_state() in ("cancelled", "complete"):
            self.active_filename = ""
            self.print_started_ts = None

    def _maybe_fetch_task_detail(self) -> None:
        info = self.status.get("PrintInfo") or {}
        task_id = info.get("TaskId")
        if task_id and task_id != self._last_task_id:
            self._last_task_id = task_id
            self.print_started_ts = time.time()
            asyncio.create_task(self._fetch_task_detail(task_id))
            filename = info.get("Filename")
            if filename:
                asyncio.create_task(self._fetch_file_detail(filename))

    async def _fetch_task_detail(self, task_id: str) -> None:
        with contextlib.suppress(Exception):
            await self.send_command(CMD_TASK_DETAIL, {"Id": [task_id]}, wait=False)

    async def _fetch_file_detail(self, filename: str) -> None:
        with contextlib.suppress(Exception):
            await self.send_command(CMD_FILE_DETAIL, {"Url": normalize_filename(filename)}, wait=False)

    def notify_signature(self) -> Any:
        state = self.map_print_state()
        info = self.status.get("PrintInfo") or {}
        layer = info.get("CurrentLayer")
        progress = info.get("Progress")
        return (state, layer, progress, info.get("TaskId"), info.get("CurrentTicks"))

    # -- connection lifecycle ---------------------------------------------- #

    async def run(self) -> None:
        while True:
            try:
                log.info("Connecting to %s", SDCP_WS)
                async with websockets.connect(
                    SDCP_WS, ping_interval=None, max_size=32 * 1024 * 1024
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    self.connect_ts = time.time()
                    self.last_error = None
                    log.info("Connected to Centauri SDCP")
                    await self._initial_query()
                    await asyncio.gather(
                        self._receiver(ws),
                        self._poller(),
                        self._keepalive(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = repr(exc)
                log.warning("SDCP connection lost: %s", exc)
                await asyncio.sleep(3)

    async def _initial_query(self) -> None:
        for cmd, data in (
            (CMD_STATUS, None),
            (CMD_ATTR, None),
            (CMD_HISTORY_ID, None),
            (CMD_FILE_LIST, {"Url": "/local"}),
        ):
            with contextlib.suppress(Exception):
                await self.send_command(cmd, data)

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            if raw == "pong":
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                log.debug("Non-JSON SDCP message: %r", raw)
                continue
            if isinstance(msg, dict):
                self._handle_message(msg)

    async def _poller(self) -> None:
        last_filelist = 0.0
        last_history = 0.0
        while True:
            with contextlib.suppress(Exception):
                await self.send_command(CMD_STATUS)
                await self.send_command(CMD_ATTR)
                now = time.time()
                if now - last_history > HISTORY_INTERVAL:
                    last_history = now
                    await self.send_command(CMD_HISTORY_ID)
                if now - last_filelist > FILELIST_INTERVAL:
                    last_filelist = now
                    await self.send_command(CMD_FILE_LIST, {"Url": "/local"})
            await asyncio.sleep(POLL_INTERVAL)

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(25)
            with contextlib.suppress(Exception):
                await self.ws.send("ping")

    # -- status mapping ---------------------------------------------------- #

    def _print_info(self) -> dict:
        return self.status.get("PrintInfo") or {}

    def map_print_state(self) -> str:
        if not self.connected:
            return "standby"
        info = self._print_info()
        ps = info.get("Status")
        cs = self.status.get("CurrentStatus") or []
        if ps in STATUS_CANCELLED:
            return "cancelled"
        if ps in STATUS_COMPLETE:
            return "complete"
        if ps in STATUS_PAUSED:
            return "paused"
        if ps in STATUS_PRINTING:
            return "printing"
        if ps == 0:
            return "standby"
        # Preparing / heating / homing / levelling / file checking / recovery.
        if ps in (STATUS_LOADING - {0}) or ps in STATUS_FILE_DETECTION or ps in STATUS_RECOVERY:
            return "printing"
        if 6 in cs:
            return "paused"
        if 1 in cs:
            return "printing"
        if 9 in cs:
            return "complete"
        if 8 in cs:
            return "cancelled"
        return "standby"

    def progress(self) -> float:
        info = self._print_info()
        try:
            v = float(info.get("Progress") or 0)
        except (TypeError, ValueError):
            v = 0.0
        return max(0.0, min(1.0, v / 100.0))

    def build_print_stats(self) -> dict:
        info = self._print_info()
        state = self.map_print_state()
        duration = info.get("CurrentTicks") or 0
        return {
            "state": state,
            "message": "",
            "filename": info.get("Filename") or self.active_filename or "",
            "info": {
                "total_layer": info.get("TotalLayer"),
                "current_layer": info.get("CurrentLayer"),
            },
            "print_duration": duration,
            "total_duration": duration,
            "filament_used": None,
        }

    def build_virtual_sdcard(self) -> dict:
        info = self._print_info()
        progress = self.progress()
        size = 0
        for f in self.file_list:
            if f.get("name") == info.get("Filename"):
                size = f.get("FileSize") or 0
                break
        state = self.map_print_state()
        return {
            "progress": progress,
            "file_position": int(progress * size) if size else 0,
            "is_active": state in ("printing", "paused"),
        }

    def _coord(self) -> list[float]:
        raw = self.status.get("CurrenCoord")
        if not raw:
            return [0.0, 0.0, 0.0, 0.0]
        with contextlib.suppress(Exception):
            parts = [float(x) for x in str(raw).split(",")]
            while len(parts) < 3:
                parts.append(0.0)
            return [parts[0], parts[1], parts[2], 0.0]
        return [0.0, 0.0, 0.0, 0.0]

    def toolhead_position(self) -> list[float]:
        return self._coord()

    def build_gcode_move(self) -> dict:
        info = self._print_info()
        try:
            speed_factor = float(info.get("PrintSpeedPct") or 100) / 100.0
        except (TypeError, ValueError):
            speed_factor = 1.0
        return {
            "speed_factor": speed_factor,
            "extrude_factor": 1.0,
            "gcode_position": self._coord(),
            "absolute_coordinates": True,
            "homing_origin": [0.0, 0.0, 0.0, 0.0],
        }

    def model_fan_speed(self) -> float:
        fan = self.status.get("CurrentFanSpeed") or {}
        return float(fan.get("ModelFan") or 0) / 100.0

    def extruder_state(self) -> dict:
        return {
            "temperature": self.status.get("TempOfNozzle", 0),
            "target": self.status.get("TempTargetNozzle", 0),
            "power": 0.0,
        }

    def heater_bed_state(self) -> dict:
        return {
            "temperature": self.status.get("TempOfHotbed", 0),
            "target": self.status.get("TempTargetHotbed", 0),
        }

    def printer_info(self) -> dict:
        attrs = self.attributes
        return {
            "state": "ready" if self.connected else "shutdown",
            "state_message": self.last_error or "",
            "hostname": attrs.get("Name") or attrs.get("MachineName") or "centauri-carbon",
            "software_version": attrs.get("FirmwareVersion", "Elegoo SDCP bridge"),
            "cpu_info": attrs.get("BrandName", "ELEGOO"),
            "klippy_path": "",
            "python_path": "",
            "log_file": "",
            "config_file": "",
        }

    def cpu_info(self) -> str:
        return self.attributes.get("BrandName", "ELEGOO")

    # -- files / metadata / history ---------------------------------------- #

    def file_entries(self) -> list[dict]:
        out = []
        for f in self.file_list:
            name = normalize_filename(f.get("name", ""))
            out.append({
                "path": name,
                "modified": f.get("CreateTime", 0),
                "size": f.get("FileSize", 0),
                "permissions": "rw",
            })
        return out

    def find_file(self, filename: str) -> Optional[dict]:
        target = normalize_filename(filename)
        for f in self.file_list:
            if normalize_filename(f.get("name", "")) == target:
                return f
        return None

    def build_metadata(self, filename: str) -> dict:
        target = normalize_filename(filename)
        f = self.find_file(target) or {}
        detail = self.file_detail if target == normalize_filename(self._print_info().get("Filename", "")) else {}
        est = detail.get("EstTime") or 0
        return {
            "filename": target,
            "size": f.get("FileSize", 0),
            "modified": f.get("CreateTime", 0),
            "created": f.get("CreateTime", 0),
            "layer_count": detail.get("TotalLayers") or f.get("TotalLayers"),
            "layer_height": f.get("LayerHeight", 0),
            "first_layer_height": None,
            "object_height": None,
            "estimated_time": est,
            "filament_total": detail.get("EstWeight"),
            "filament_weight_total": detail.get("EstWeight"),
            "thumbnails": [{"width": 0, "height": 0, "size": 0, "relative_path": detail.get("Thumbnail", "")}] if detail.get("Thumbnail") else [],
            "slicer": "Elegoo",
            "slicer_version": self.attributes.get("FirmwareVersion", ""),
        }

    def build_jobs(self) -> list[dict]:
        info = self._print_info()
        task_id = info.get("TaskId")
        state = self.map_print_state()
        if not task_id:
            # The printer only assigns a TaskId once actual printing starts.
            # Expose a synthetic job during the prepare phase so moonraker-obico
            # can resolve start_time/filename right away.
            if not (self.active_filename and state in ("printing", "paused")):
                return []
            task_id = "pending"
            start = self.print_started_ts or time.time()
            end = 0
            filename = self.active_filename
            duration = info.get("CurrentTicks") or 0
            td = {}
        else:
            td = self.task_detail if self.task_detail.get("TaskId") == task_id else {}
            start = td.get("BeginTime") or self.print_started_ts or time.time()
            end = td.get("EndTime") or 0
            filename = td.get("TaskName") or info.get("Filename") or ""
            duration = info.get("CurrentTicks") or 0
        status = self.map_print_state()
        mr_status = {
            "printing": "in_progress",
            "paused": "paused",
            "complete": "completed",
            "cancelled": "cancelled",
            "standby": "completed",
        }.get(status, "in_progress")
        return [{
            "job_id": task_id,
            "file": {"path": normalize_filename(filename), "filename": basename(filename)},
            "filename": normalize_filename(filename),
            "start_time": float(start),
            "end_time": float(end) if end else time.time(),
            "status": mr_status,
            "print_duration": duration,
            "total_duration": duration,
            "filament_used": 0,
            "metadata": {
                "layer_count": td.get("SliceInformation", {}).get("total_layer_numbers"),
            },
        }]

    # -- print control ------------------------------------------------------ #

    async def _run_command(self, cmd: int, data: Any = None, timeout: float = 10.0) -> None:
        try:
            payload = await self.send_command(cmd, data, wait=True, timeout=timeout)
        except RuntimeError as exc:
            raise PrinterNotConnected(str(exc)) from exc
        except TimeoutError as exc:
            raise PrinterTimeout(str(exc)) from exc
        ack = (payload or {}).get("Ack")
        if ack not in (0, None):
            raise CommandRejected(f"SDCP command {CMD_NAMES.get(cmd, cmd)} failed (Ack={ack})", code=ack)

    async def pause(self) -> None:
        await self._run_command(CMD_SUSPEND_PRINT, {})

    async def resume(self) -> None:
        await self._run_command(CMD_RESTORE_PRINT, {})

    async def cancel(self) -> None:
        await self._run_command(CMD_STOP_PRINT, {})

    async def start_print(self, filename: str) -> None:
        normalized = normalize_filename(filename)
        data = {
            "Filename": normalized,
            "StartLayer": 0,
            "Calibration_switch": START_CALIBRATION,
            "PrintPlatformType": START_PLATFORM_TYPE,
            "Tlp_Switch": START_TIMELAPSE,
            "slot_map": [],
        }
        await self._run_command(CMD_START_PRINT, data, timeout=15)
        self.active_filename = normalized
        self.print_started_ts = time.time()
        self._last_task_id = None

    async def run_gcode(self, script: str) -> None:
        """Best-effort translation of the gcode moonraker-obico sends."""
        for raw_line in script.splitlines():
            line = raw_line.split(";")[0].strip()
            if not line:
                continue
            upper = line.upper()
            try:
                if upper.startswith("G28"):
                    axes = "".join(a for a in upper[3:] if a in "XYZ") or "XYZ"
                    await self.send_command(CMD_AXIS_ZERO, {"Axis": axes})
                elif upper.startswith("G0") or upper.startswith("G1"):
                    for axis in "XYZ":
                        m = re.search(rf"{axis}(-?[0-9.]+)", upper)
                        if m:
                            await self.send_command(CMD_AXIS_NUMBER, {"Axis": axis, "Step": float(m.group(1))})
                elif "SET_HEATER_TEMPERATURE" in upper:
                    heater = re.search(r"HEATER=(\w+)", line, re.I)
                    target = re.search(r"TARGET=([0-9.]+)", line, re.I)
                    if heater and target:
                        key = "TempTargetHotbed" if "bed" in heater.group(1).lower() else "TempTargetNozzle"
                        await self.send_command(CMD_STATUS_DATA, {key: float(target.group(1))})
                elif upper.startswith("M104") or upper.startswith("M109"):
                    m = re.search(r"S([0-9.]+)", upper)
                    if m:
                        await self.send_command(CMD_STATUS_DATA, {"TempTargetNozzle": float(m.group(1))})
                elif upper.startswith("M140") or upper.startswith("M190"):
                    m = re.search(r"S([0-9.]+)", upper)
                    if m:
                        await self.send_command(CMD_STATUS_DATA, {"TempTargetHotbed": float(m.group(1))})
                elif "SET_FAN_SPEED" in upper:
                    m = re.search(r"SPEED=([0-9.]+)", line, re.I)
                    if m:
                        pct = float(m.group(1))
                        pct = pct * 100 if pct <= 1 else pct
                        await self.send_command(CMD_STATUS_DATA, {"TargetFanSpeed": {"ModelFan": pct}})
                elif "M106" in upper:
                    m = re.search(r"S([0-9.]+)", upper)
                    pct = float(m.group(1)) / 255.0 * 100 if m else 100.0
                    await self.send_command(CMD_STATUS_DATA, {"TargetFanSpeed": {"ModelFan": pct}})
            except Exception as exc:
                log.debug("gcode translation failed for %r: %s", line, exc)

    async def upload_file(self, filename: str, data: bytes) -> dict:
        """Proxy a Moonraker upload to the printer's HTTP upload endpoint.

        The stock UI uploads to ``http://PRINTER/uploadFile/upload`` in 1 MiB chunks
        with the fields TotalSize / Uuid / Offset / Check / S-File-MD5 / File.
        """
        # The printer stores uploads flat under /local regardless of any subfolder.
        path = "gcodes"
        md5 = hashlib.md5(data).hexdigest()
        upload_uuid = uuid.uuid4().hex
        chunk_size = 1024 * 1024
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                for offset in range(0, max(len(data), 1), chunk_size):
                    piece = data[offset:offset + chunk_size]
                    resp = await client.post(
                        f"http://{HOST}/uploadFile/upload",
                        data={
                            "TotalSize": str(len(data)),
                            "Uuid": upload_uuid,
                            "Offset": str(offset),
                            "Check": "1",
                            "S-File-MD5": md5,
                        },
                        files={"File": (filename, piece, "application/octet-stream")},
                    )
                    if resp.status_code != 200:
                        raise UpstreamError(f"Printer upload failed: HTTP {resp.status_code} {resp.text[:200]}")
                    body = {}
                    with contextlib.suppress(Exception):
                        body = resp.json()
                    if isinstance(body, dict) and body.get("success") is False:
                        raise UpstreamError(f"Printer upload rejected: {body}")
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError(f"Printer upload error: {exc}") from exc

        with contextlib.suppress(Exception):
            await self.send_command(CMD_FILE_LIST, {"Url": "/local"})
            # Give the printer a moment to index the new file so that a subsequent
            # /server/files/metadata call returns the real size/modified values.
            for _ in range(10):
                await asyncio.sleep(0.4)
                if self.find_file(filename):
                    break
        found = self.find_file(filename) or {}
        return {
            "path": f"{path}/{filename}",
            "root": path,
            "size": found.get("FileSize", len(data)),
            "modified": found.get("CreateTime", time.time()),
        }
