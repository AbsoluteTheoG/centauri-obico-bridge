"""Elegoo Centauri Carbon 2 (MQTT).

The CC2 runs its own MQTT broker on port 1883 (username ``elegoo``, password =
the access code shown under Settings -> Network -> LAN Only Mode; LAN Only Mode
must be enabled).  A client registers itself, then exchanges JSON messages:

* ``elegoo/{sn}/api_register``                 -> ``{"client_id", "request_id"}``
* ``elegoo/{sn}/{request_id}/register_response`` <- ``{"client_id", "error": "ok"}``
* ``elegoo/{sn}/{client_id}/api_request``        -> ``{"id", "method", "params"}``
                                                  or ``{"type": "PING"}``
* ``elegoo/{sn}/{client_id}/api_response``       <- ``{"id", "method", "result"}``
                                                  or ``{"type": "PONG"}``
* ``elegoo/{sn}/api_status``                    <- pushed events; method 6000
                                                  carries a status *delta*

The camera is an MJPEG stream at ``http://HOST:8080/?action=stream`` once it
has been enabled with method 1042.

Protocol reference: danielcherubini/elegoo-homeassistant (``cc2/``), whose
authors measured several behaviours on real hardware that are relied on here
(resume is only acknowledged once the printer has reheated and homed; the
bed-levelling flag on start is sticky and must always be sent).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import re
import secrets
import time
from typing import Any, AsyncIterator, Optional

import aiomqtt

from .base import (
    BasePrinter,
    CommandRejected,
    PrinterNotConnected,
    PrinterTimeout,
    log,
)


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


HOST = _env("CENTAURI_HOST", "192.168.1.43")
ACCESS_CODE = _env("CENTAURI_ACCESS_CODE", "")
SERIAL = _env("CENTAURI_SERIAL", "").strip()
MQTT_PORT = int(_env("CC2_MQTT_PORT", "1883"))
VIDEO_PORT = int(_env("CC2_VIDEO_PORT", "8080"))
STATUS_REFRESH_INTERVAL = float(_env("STATUS_REFRESH_INTERVAL", "30"))
FILELIST_INTERVAL = float(_env("FILELIST_INTERVAL", "60"))
RAW_LOG = _env("RAW_LOG", _env("SDCP_RAW_LOG", ""))
# Bed levelling sent on START_PRINT.  The printer remembers the last value it
# was given, so it is always sent explicitly.
START_CALIBRATION = int(_env("START_CALIBRATION", "1"))

MQTT_USERNAME = "elegoo"
DEFAULT_ACCESS_CODE = "123456"
VIDEO_URL = f"http://{HOST}:{VIDEO_PORT}/?action=stream"

COMMAND_TIMEOUT = 10.0
REGISTRATION_TIMEOUT = 5.0
SERIAL_DISCOVERY_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_TIMEOUT = 65.0
VIDEO_ENABLE_INTERVAL = 30.0

# --------------------------------------------------------------------------- #
# Method IDs
# --------------------------------------------------------------------------- #

CMD_GET_ATTRIBUTES = 1001
CMD_GET_STATUS = 1002
CMD_START_PRINT = 1020
CMD_PAUSE_PRINT = 1021
CMD_STOP_PRINT = 1022
CMD_RESUME_PRINT = 1023
CMD_SET_TEMPERATURE = 1028
CMD_SET_FAN_SPEED = 1030
CMD_VIDEO_STREAM = 1042
CMD_GET_FILE_LIST = 1044

EVENT_STATUS = 6000
EVENT_ATTRIBUTES = 6008

CMD_NAMES = {
    CMD_START_PRINT: "START_PRINT",
    CMD_PAUSE_PRINT: "PAUSE_PRINT",
    CMD_STOP_PRINT: "STOP_PRINT",
    CMD_RESUME_PRINT: "RESUME_PRINT",
    CMD_SET_TEMPERATURE: "SET_TEMPERATURE",
    CMD_SET_FAN_SPEED: "SET_FAN_SPEED",
    CMD_VIDEO_STREAM: "VIDEO_STREAM",
}

ERROR_PRINTER_BUSY = 1009

# machine_status.status
MACHINE_PRINTING = 2
MACHINE_EMERGENCY_STOP = 14

# machine_status.sub_status
SUB_PRINTING_COMPLETED = 2077
SUB_PAUSING = 2501
SUB_PAUSED = {2502, 2505}
SUB_STOPPING = 2503
SUB_STOPPED = 2504

# print_status.state (string) -> Moonraker state, used when the numeric codes
# do not decide it.
PRINT_STATE_STRINGS = {
    "printing": "printing",
    "paused": "paused",
    "pausing": "paused",
    "complete": "complete",
    "completed": "complete",
    "finished": "complete",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "stopped": "cancelled",
    "error": "error",
}

# gcode_move.speed_mode -> speed percentage
SPEED_MODE_PCT = {0: 50, 1: 100, 2: 150, 3: 200}


def _new_client_id() -> str:
    # Matches the stock web UI: "0cli" + timestamp hex + random hex, 10 chars.
    stamp = format(int(time.time() * 1000), "x")[-5:]
    return f"0cli{stamp}{format(secrets.randbelow(4096), 'x')}"[:10]


def _new_request_id() -> str:
    return secrets.token_hex(8) + format(int(time.time() * 1000), "x")


def normalize_filename(name: str) -> str:
    """The CC2 stores files flat; accept Moonraker 'gcodes/x' and SDCP '/local/x'."""
    if not name:
        return name
    name = name.strip()
    for prefix in ("gcodes/", "/local/", "/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def _deep_merge(base: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Cc2Printer(BasePrinter):
    camera_name = "Centauri Carbon 2 Camera"

    def __init__(self) -> None:
        super().__init__()
        self.serial: str = SERIAL
        self.access_code: str = ACCESS_CODE or DEFAULT_ACCESS_CODE
        self.client: Optional[aiomqtt.Client] = None
        self.client_id = _new_client_id()
        self.request_id = _new_request_id()
        self.status: dict[str, Any] = {}
        self.file_list: list[dict] = []
        self.file_list_ts: float = 0.0
        self._status_seq: Optional[int] = None
        self._full_status_pending = False
        self._file_list_pending = False
        self._next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self._registration: Optional[asyncio.Future] = None
        self._last_pong = 0.0
        self._video_enabled_ts = 0.0
        # Job bookkeeping (the CC2 reports durations, not wall-clock times).
        self._job_uuid: Optional[str] = None
        self._job_start_ts: Optional[float] = None
        self._job_end_ts: Optional[float] = None
        if self.serial:
            self.mainboard_id = self.serial
        if not ACCESS_CODE:
            log.warning("CENTAURI_ACCESS_CODE is not set; trying the default access code")
        self.open_raw_log(RAW_LOG)

    # -- topics -------------------------------------------------------------- #

    @property
    def _request_topic(self) -> str:
        return f"elegoo/{self.serial}/{self.client_id}/api_request"

    @property
    def video_url(self) -> str:
        return VIDEO_URL

    def describe(self) -> dict:
        return {
            "protocol": "cc2",
            "mqtt": f"mqtt://{HOST}:{MQTT_PORT}",
            "serial": self.serial,
            "video": VIDEO_URL,
        }

    def debug_state(self) -> dict:
        return {
            "connected": self.connected,
            "last_rx": self.last_rx,
            "last_error": self.last_error,
            "serial": self.serial,
            "client_id": self.client_id,
            "status": self.status,
            "attributes": self.attributes,
            "file_list": self.file_list,
            "objects": self.build_objects(),
        }

    # -- connection lifecycle ---------------------------------------------- #

    async def run(self) -> None:
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                log.warning("CC2 connection lost: %s", exc)
            finally:
                self.connected = False
                self.client = None
                self._fail_pending(PrinterNotConnected("Not connected to printer"))
            await asyncio.sleep(5)

    async def _session(self) -> None:
        # Fresh identifiers per session so a stale registration on the printer
        # never collides with this one.
        self.client_id = _new_client_id()
        self.request_id = _new_request_id()
        log.info("Connecting to CC2 MQTT %s:%s", HOST, MQTT_PORT)
        async with aiomqtt.Client(
            HOST,
            MQTT_PORT,
            username=MQTT_USERNAME,
            password=self.access_code,
            identifier=self.client_id,
            keepalive=60,
        ) as client:
            self.client = client
            if not self.serial:
                await client.subscribe("elegoo/+/api_status")
                self.serial = await self._discover_serial(client)
                self.mainboard_id = self.serial
                log.info("Discovered CC2 serial %s", self.serial)
                await client.unsubscribe("elegoo/+/api_status")
            sn = self.serial
            await client.subscribe(f"elegoo/{sn}/{self.client_id}/api_response")
            await client.subscribe(f"elegoo/{sn}/api_status")
            await client.subscribe(f"elegoo/{sn}/{self.request_id}/register_response")

            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._listen(client))
                    await self._register(client)
                    self.connected = True
                    self.connect_ts = time.time()
                    self.last_error = None
                    self._last_pong = time.time()
                    log.info("Connected to Centauri Carbon 2 (%s)", sn)
                    await self._initial_query()
                    tasks.create_task(self._heartbeat())
                    tasks.create_task(self._poller())
            except* (ConnectionError, aiomqtt.MqttError) as group:
                # Surface the first underlying error rather than the group.
                raise group.exceptions[0] from None

    async def _discover_serial(self, client: aiomqtt.Client) -> str:
        """The serial is part of every topic; learn it from a status push."""
        async def first() -> str:
            async for message in client.messages:
                parts = str(message.topic).split("/")
                if len(parts) == 3 and parts[0] == "elegoo" and parts[2] == "api_status" and parts[1]:
                    return parts[1]
            raise ConnectionError("MQTT closed during serial discovery")

        try:
            return await asyncio.wait_for(first(), SERIAL_DISCOVERY_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                "No status message from the printer to learn its serial; "
                "set CENTAURI_SERIAL"
            ) from exc

    async def _register(self, client: aiomqtt.Client) -> None:
        self._registration = asyncio.get_running_loop().create_future()
        payload = {"client_id": self.client_id, "request_id": self.request_id}
        self._record("tx", {"topic": "api_register", **payload})
        await client.publish(f"elegoo/{self.serial}/api_register", json.dumps(payload))
        try:
            result = await asyncio.wait_for(self._registration, REGISTRATION_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                "Printer did not answer registration; check CENTAURI_ACCESS_CODE "
                "and that LAN Only Mode is enabled"
            ) from exc
        finally:
            self._registration = None
        error = str(result.get("error", ""))
        if error != "ok":
            raise ConnectionError(f"Printer refused registration: {error or result}")

    async def _listen(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            payload = message.payload
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", errors="replace")
            try:
                data = json.loads(payload)
            except Exception:
                log.debug("Non-JSON CC2 message on %s: %r", message.topic, payload)
                continue
            if isinstance(data, dict):
                self._handle_message(str(message.topic), data)
        raise ConnectionError("MQTT connection closed")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if time.time() - self._last_pong > HEARTBEAT_TIMEOUT:
                raise ConnectionError("No heartbeat reply from printer")
            await self._publish({"type": "PING"}, record=False)

    async def _initial_query(self) -> None:
        for method, params in (
            (CMD_GET_ATTRIBUTES, None),
            (CMD_GET_STATUS, None),
            (CMD_GET_FILE_LIST, {"storage_media": "local", "path": "/"}),
        ):
            with contextlib.suppress(Exception):
                await self.send_command(method, params, wait=True)

    async def _poller(self) -> None:
        """Status is pushed as deltas; periodically resync the full state."""
        last_status = time.time()
        last_files = time.time()
        while True:
            await asyncio.sleep(1)
            now = time.time()
            with contextlib.suppress(Exception):
                if self._full_status_pending or now - last_status > STATUS_REFRESH_INTERVAL:
                    self._full_status_pending = False
                    last_status = now
                    await self.send_command(CMD_GET_STATUS)
                if self._file_list_pending or now - last_files > FILELIST_INTERVAL:
                    self._file_list_pending = False
                    last_files = now
                    await self.send_command(CMD_GET_FILE_LIST, {"storage_media": "local", "path": "/"})

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()

    # -- send / receive ----------------------------------------------------- #

    async def _publish(self, payload: dict, *, record: bool = True) -> None:
        if not self.client:
            raise PrinterNotConnected("Not connected to printer")
        if record:
            self._record("tx", payload)
        await self.client.publish(self._request_topic, json.dumps(payload, separators=(",", ":")))

    async def send_command(
        self,
        method: int,
        params: Optional[dict] = None,
        *,
        wait: bool = False,
        timeout: float = COMMAND_TIMEOUT,
    ) -> Optional[dict]:
        """Send a method call; with ``wait`` return its ``result`` dict."""
        if not self.connected or not self.client:
            raise PrinterNotConnected("Not connected to printer")
        self._next_id += 1
        rid = self._next_id
        fut: Optional[asyncio.Future] = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self.pending[rid] = fut
        try:
            await self._publish({"id": rid, "method": method, "params": params or {}})
        except PrinterNotConnected:
            self.pending.pop(rid, None)
            raise
        except Exception as exc:
            self.pending.pop(rid, None)
            raise PrinterNotConnected(f"Failed to send to printer: {exc}") from exc
        if fut is None:
            return None
        try:
            response = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise PrinterTimeout(
                f"No response for {CMD_NAMES.get(method, method)} within {timeout}s"
            ) from exc
        finally:
            self.pending.pop(rid, None)
        res = response.get("result")
        return res if isinstance(res, dict) else {}

    def _handle_message(self, topic: str, data: dict) -> None:
        self.last_rx = time.time()
        if data.get("type") == "PONG":
            self._last_pong = time.time()
            return
        self._record("rx", {"topic": topic, **data})

        if topic.endswith("/register_response"):
            if self._registration and not self._registration.done():
                self._registration.set_result(data)
            return

        if topic.endswith("/api_response"):
            method = data.get("method")
            rid = data.get("id")
            self.responses[f"{method}:{rid}"] = data
            if len(self.responses) > 400:
                self.responses.popitem(last=False)
            self.response_history.append(data)
            if method is not None:
                self.last_response[method] = data
            self._process_response(method, data.get("result"))
            fut = self.pending.get(rid) if isinstance(rid, int) else None
            if fut and not fut.done():
                fut.set_result(data)
            return

        if topic.endswith("/api_status"):
            method = data.get("method")
            result = data.get("result")
            if method == EVENT_STATUS and isinstance(result, dict):
                self._apply_delta(data.get("id"), result)
            elif method == EVENT_ATTRIBUTES and isinstance(result, dict):
                _deep_merge(self.attributes, result)

    def _process_response(self, method: Any, result: Any) -> None:
        if not isinstance(result, dict):
            return
        error = result.get("error_code", 0)
        if method == CMD_GET_STATUS:
            # A refusal carries no machine_status; never let it wipe what we know.
            if error == 0 and isinstance(result.get("machine_status"), dict):
                self.status = {k: v for k, v in result.items() if k != "error_code"}
                self._status_changed()
        elif method == CMD_GET_ATTRIBUTES:
            if error == 0:
                self.attributes = {k: v for k, v in result.items() if k != "error_code"}
                if not self.serial and result.get("sn"):
                    self.serial = str(result["sn"])
                    self.mainboard_id = self.serial
        elif method == CMD_GET_FILE_LIST:
            entries = result.get("file_list")
            if error == 0 and isinstance(entries, list):
                self.file_list = [
                    e for e in entries
                    if isinstance(e, dict) and isinstance(e.get("filename"), str)
                    and str(e.get("type", "file")).lower() == "file"
                ]
                self.file_list_ts = time.time()

    def _apply_delta(self, seq: Any, delta: dict) -> None:
        if isinstance(seq, int):
            if self._status_seq is not None and seq != self._status_seq + 1:
                # Missed an update; ask for the full state again.
                self._full_status_pending = True
            self._status_seq = seq
        if not self.status:
            self._full_status_pending = True
        _deep_merge(self.status, delta)
        self._status_changed()

    def _status_changed(self) -> None:
        self._track_job()
        if self._machine().get("status") == MACHINE_PRINTING:
            filename = self._filename()
            if not filename:
                # A print started on the printer or from the slicer: the
                # filename can arrive after the status flips to printing.
                self._full_status_pending = True
            elif self.file_list_ts and not self.find_file(filename):
                # Uploaded after our last file list: refresh for its metadata.
                self._file_list_pending = True
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._notify_if_changed())

    # -- status mapping ---------------------------------------------------- #

    def _machine(self) -> dict:
        return self.status.get("machine_status") or {}

    def _print(self) -> dict:
        return self.status.get("print_status") or {}

    def map_print_state(self) -> str:
        if not self.connected:
            return "standby"
        machine = self._machine()
        status = machine.get("status")
        sub = machine.get("sub_status")
        if sub == SUB_PAUSING or sub in SUB_PAUSED:
            return "paused"
        if sub in (SUB_STOPPING, SUB_STOPPED):
            return "cancelled"
        if sub == SUB_PRINTING_COMPLETED:
            return "complete"
        if status == MACHINE_PRINTING:
            # Includes preheating, homing and levelling before the first layer.
            # Hold back until the filename is known: moonraker-obico registers
            # the print with Obico once, on the transition to printing.
            return "printing" if self._filename() else "standby"
        if status == MACHINE_EMERGENCY_STOP:
            return "error"
        state = str(self._print().get("state") or "").lower()
        if state in PRINT_STATE_STRINGS and state != "printing":
            return PRINT_STATE_STRINGS[state]
        return "standby"

    def progress(self) -> float:
        value = self._machine().get("progress")
        if value is None:
            value = self._print().get("progress")
        return max(0.0, min(1.0, _num(value) / 100.0))

    def _filename(self) -> str:
        return normalize_filename(self._print().get("filename") or "") or self.active_filename

    def _total_layers(self) -> Optional[int]:
        total = self._print().get("total_layer")
        if total is None:
            entry = self.find_file(self._filename())
            if entry:
                total = entry.get("layer")
        return total

    def build_print_stats(self) -> dict:
        info = self._print()
        state = self.map_print_state()
        active = state in ("printing", "paused")
        return {
            "state": state,
            "message": "",
            "filename": self._filename() if (active or info.get("filename")) else "",
            "info": {
                "total_layer": self._total_layers(),
                "current_layer": info.get("current_layer"),
            },
            "print_duration": _num(info.get("print_duration")),
            "total_duration": _num(info.get("total_duration")),
            "filament_used": None,
        }

    def build_virtual_sdcard(self) -> dict:
        progress = self.progress()
        entry = self.find_file(self._filename()) or {}
        size = int(_num(entry.get("size")))
        return {
            "progress": progress,
            "file_position": int(progress * size) if size else 0,
            "is_active": self.map_print_state() in ("printing", "paused"),
        }

    def _move(self) -> dict:
        return self.status.get("gcode_move_inf") or self.status.get("gcode_move") or {}

    def toolhead_position(self) -> list[float]:
        move = self._move()
        return [_num(move.get("x")), _num(move.get("y")), _num(move.get("z")), 0.0]

    def build_gcode_move(self) -> dict:
        mode = self._move().get("speed_mode", 1)
        return {
            "speed_factor": SPEED_MODE_PCT.get(mode, 100) / 100.0,
            "extrude_factor": 1.0,
            "gcode_position": self.toolhead_position(),
            "absolute_coordinates": True,
            "homing_origin": [0.0, 0.0, 0.0, 0.0],
        }

    def model_fan_speed(self) -> float:
        fans = self.status.get("fans") or {}
        return _num((fans.get("fan") or {}).get("speed")) / 255.0

    def extruder_state(self) -> dict:
        ext = self.status.get("extruder") or {}
        return {
            "temperature": _num(ext.get("temperature")),
            "target": _num(ext.get("target")),
            "power": 0.0,
        }

    def heater_bed_state(self) -> dict:
        bed = self.status.get("heater_bed") or {}
        return {
            "temperature": _num(bed.get("temperature")),
            "target": _num(bed.get("target")),
        }

    def printer_info(self) -> dict:
        attrs = self.attributes
        version = (attrs.get("software_version") or {}).get("ota_version")
        return {
            "state": "ready" if self.connected else "shutdown",
            "state_message": self.last_error or "",
            "hostname": attrs.get("hostname") or attrs.get("machine_model") or "centauri-carbon-2",
            "software_version": version or "Elegoo CC2 bridge",
            "cpu_info": "ELEGOO",
            "klippy_path": "",
            "python_path": "",
            "log_file": "",
            "config_file": "",
        }

    def notify_signature(self) -> Any:
        info = self._print()
        return (
            self.map_print_state(),
            info.get("current_layer"),
            self._machine().get("progress"),
            info.get("uuid"),
            info.get("print_duration"),
        )

    # -- jobs ----------------------------------------------------------------- #

    def _track_job(self) -> None:
        info = self._print()
        state = self.map_print_state()
        job_uuid = info.get("uuid") or None
        if info.get("filename"):
            self.active_filename = normalize_filename(info["filename"])
        if job_uuid and job_uuid != self._job_uuid:
            self._job_uuid = job_uuid
            elapsed = _num(info.get("total_duration"))
            self._job_start_ts = time.time() - elapsed
            self.print_started_ts = self._job_start_ts
            self._job_end_ts = None
        if state in ("printing", "paused"):
            self._job_end_ts = None
        elif self._job_uuid and self._job_end_ts is None and state in ("complete", "cancelled", "error"):
            self._job_end_ts = time.time()
        if state in ("standby", "complete", "cancelled", "error") and not info.get("filename"):
            self.active_filename = ""

    def build_jobs(self) -> list[dict]:
        info = self._print()
        state = self.map_print_state()
        job_uuid = info.get("uuid") or self._job_uuid
        filename = self._filename()
        if not job_uuid:
            # Started through the bridge but the printer has not assigned a job
            # yet: expose a placeholder so moonraker-obico can resolve it.
            if not (self.active_filename and state in ("printing", "paused")):
                return []
            job_uuid = "pending"
        if not filename:
            return []
        start = self._job_start_ts or self.print_started_ts or time.time()
        mr_status = {
            "printing": "in_progress",
            "paused": "paused",
            "complete": "completed",
            "cancelled": "cancelled",
            "error": "klippy_shutdown",
            "standby": "completed",
        }.get(state, "in_progress")
        duration = _num(info.get("print_duration"))
        return [{
            "job_id": job_uuid,
            "file": {"path": filename, "filename": pathlib.PurePosixPath(filename).name},
            "filename": filename,
            "start_time": float(start),
            "end_time": float(self._job_end_ts) if self._job_end_ts else time.time(),
            "status": mr_status,
            "print_duration": duration,
            "total_duration": _num(info.get("total_duration")),
            "filament_used": 0,
            "metadata": {"layer_count": self._total_layers()},
        }]

    # -- files ---------------------------------------------------------------- #

    def find_file(self, filename: str) -> Optional[dict]:
        target = normalize_filename(filename)
        if not target:
            return None
        for entry in self.file_list:
            if normalize_filename(entry.get("filename", "")) == target:
                return entry
        return None

    def file_entries(self) -> list[dict]:
        return [{
            "path": normalize_filename(e.get("filename", "")),
            "modified": e.get("create_time", 0),
            "size": e.get("size", 0),
            "permissions": "rw",
        } for e in self.file_list]

    def build_metadata(self, filename: str) -> dict:
        target = normalize_filename(filename)
        entry = self.find_file(target) or {}
        version = (self.attributes.get("software_version") or {}).get("ota_version", "")
        return {
            "filename": target,
            "size": entry.get("size", 0),
            "modified": entry.get("create_time", 0),
            "created": entry.get("create_time", 0),
            "layer_count": entry.get("layer"),
            "layer_height": 0,
            "first_layer_height": None,
            "object_height": None,
            "estimated_time": entry.get("print_time", 0),
            "filament_total": None,
            "filament_weight_total": entry.get("total_filament_used"),
            "thumbnails": [],
            "slicer": "Elegoo",
            "slicer_version": version,
        }

    # -- camera ----------------------------------------------------------------- #

    async def prepare_video(self) -> None:
        if not self.connected:
            raise PrinterNotConnected("Not connected to printer")
        if time.time() - self._video_enabled_ts < VIDEO_ENABLE_INTERVAL:
            return
        result = await self.send_command(CMD_VIDEO_STREAM, {"enable": 1}, wait=True)
        error = (result or {}).get("error_code", 0)
        if error:
            raise CommandRejected(f"Printer refused to enable the camera (error_code={error})", code=error)
        self._video_enabled_ts = time.time()

    async def stream_frames(self, timeout: Optional[float] = None) -> AsyncIterator[bytes]:
        # The CC2 allows few concurrent camera connections, so viewers share the
        # frame reader's single upstream connection.
        async for chunk in self.cached_frame_stream(idle_timeout=timeout):
            yield chunk

    # -- print control ------------------------------------------------------ #

    async def _call(self, method: int, params: Optional[dict] = None, timeout: float = COMMAND_TIMEOUT) -> None:
        result = await self.send_command(method, params, wait=True, timeout=timeout)
        error = (result or {}).get("error_code", 0)
        if error:
            reason = " (printer busy)" if error == ERROR_PRINTER_BUSY else ""
            raise CommandRejected(
                f"CC2 command {CMD_NAMES.get(method, method)} failed (error_code={error}){reason}",
                code=error,
            )

    async def pause(self) -> None:
        await self._call(CMD_PAUSE_PRINT)

    async def resume(self) -> None:
        # The printer only answers once it has reheated, homed and returned to
        # position (~2 minutes), so do not wait for the reply.
        await self.send_command(CMD_RESUME_PRINT, wait=False)

    async def cancel(self) -> None:
        await self._call(CMD_STOP_PRINT)

    async def start_print(self, filename: str) -> None:
        name = normalize_filename(filename)
        await self._call(
            CMD_START_PRINT,
            {
                "storage_media": "local",
                "filename": name,
                "config": {"slot_map": [], "printer_check": bool(START_CALIBRATION)},
            },
            timeout=15,
        )
        self.active_filename = name
        self.print_started_ts = time.time()
        self._job_uuid = None
        self._job_start_ts = self.print_started_ts
        self._job_end_ts = None

    async def run_gcode(self, script: str) -> None:
        """Best-effort translation of the temperature/fan gcode moonraker-obico sends.

        Homing and jogging are not translated: their CC2 parameters are not
        documented yet.
        """
        for raw_line in script.splitlines():
            line = raw_line.split(";")[0].strip()
            if not line:
                continue
            upper = line.upper()
            try:
                if "SET_HEATER_TEMPERATURE" in upper:
                    heater = re.search(r"HEATER=(\w+)", line, re.I)
                    target = re.search(r"TARGET=([0-9.]+)", line, re.I)
                    if heater and target:
                        await self._set_temperature("bed" in heater.group(1).lower(), float(target.group(1)))
                elif upper.startswith(("M104", "M109")):
                    m = re.search(r"S([0-9.]+)", upper)
                    if m:
                        await self._set_temperature(False, float(m.group(1)))
                elif upper.startswith(("M140", "M190")):
                    m = re.search(r"S([0-9.]+)", upper)
                    if m:
                        await self._set_temperature(True, float(m.group(1)))
                elif "SET_FAN_SPEED" in upper:
                    m = re.search(r"SPEED=([0-9.]+)", line, re.I)
                    if m:
                        value = float(m.group(1))
                        fraction = value if value <= 1 else value / 100.0
                        await self._set_fan(fraction)
                elif upper.startswith("M106"):
                    m = re.search(r"S([0-9.]+)", upper)
                    await self._set_fan(float(m.group(1)) / 255.0 if m else 1.0)
                elif upper.startswith("M107"):
                    await self._set_fan(0.0)
                else:
                    log.debug("CC2: gcode not translated: %r", line)
            except Exception as exc:
                log.debug("gcode translation failed for %r: %s", line, exc)

    async def _set_temperature(self, bed: bool, target: float) -> None:
        limit = 110 if bed else 320
        value = int(max(0, min(limit, target)))
        await self.send_command(CMD_SET_TEMPERATURE, {"heater_bed" if bed else "extruder": value})

    async def _set_fan(self, fraction: float) -> None:
        value = round(max(0.0, min(1.0, fraction)) * 255)
        await self.send_command(CMD_SET_FAN_SPEED, {"fan": value})
