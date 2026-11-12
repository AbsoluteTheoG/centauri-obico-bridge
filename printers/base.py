"""Printer-agnostic bridge state and Moonraker object assembly.

Each supported printer family subclasses :class:`BasePrinter` and implements
the protocol client (connection, status parsing, commands).  The FastAPI layer
in ``app.py`` only talks to the methods defined here, so it does not need to
know which printer it is bridging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict, deque
from typing import Any, AsyncIterator, Optional

import httpx

log = logging.getLogger("centauri-bridge")


# --------------------------------------------------------------------------- #
# Errors surfaced to Moonraker clients
# --------------------------------------------------------------------------- #

class PrinterError(Exception):
    """A printer operation failed.  ``status`` is the HTTP status returned to
    the Moonraker client and ``code`` the value of ``error.code``."""

    status = 500

    def __init__(self, message: str, code: Optional[int] = None, status: Optional[int] = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        self.code = self.status if code is None else code


class PrinterNotConnected(PrinterError):
    status = 503


class PrinterTimeout(PrinterError):
    status = 504


class CommandRejected(PrinterError):
    status = 400


class UpstreamError(PrinterError):
    status = 502


class NotSupported(PrinterError):
    status = 501


# --------------------------------------------------------------------------- #
# Base bridge
# --------------------------------------------------------------------------- #

class BasePrinter:
    """Shared state, websocket fan-out, camera caching and Moonraker objects."""

    #: Name shown in ``/server/webcams/list``.
    camera_name = "Centauri Camera"

    def __init__(self) -> None:
        self.connected = False
        self.last_rx: Optional[float] = None
        self.last_error: Optional[str] = None
        self.connect_ts: Optional[float] = None
        # Printer identifier (SDCP MainboardID / CC2 serial number).
        self.mainboard_id: str = ""
        self.attributes: dict[str, Any] = {}

        # Raw protocol traffic, newest last.
        self.messages: deque = deque(maxlen=1000)
        # Response history keyed by command/request so nothing is overwritten.
        self.responses: OrderedDict = OrderedDict()
        self.response_history: deque = deque(maxlen=300)
        self.last_response: dict[Any, dict] = {}

        self.print_started_ts: Optional[float] = None
        # Filename of the print we last started (some printers only report the
        # filename once actual printing begins).
        self.active_filename: str = ""

        self.clients: set = set()
        self._last_notified_state: Optional[Any] = None
        self.notify_count = 0
        # Latest JPEG frame, kept fresh by a single persistent MJPEG reader so
        # that /webcam/snapshot does not open a new printer stream per request.
        self.latest_frame: bytes = b""
        self.latest_frame_ts: float = 0.0
        self._raw_fh = None

    # -- raw logging ------------------------------------------------------- #

    def open_raw_log(self, path: str) -> None:
        if path:
            with contextlib.suppress(Exception):
                self._raw_fh = open(path, "a", buffering=1)

    def _record(self, direction: str, payload: Any) -> None:
        rec = {"ts": time.time(), "dir": direction, "payload": payload}
        self.messages.append(rec)
        if self._raw_fh:
            with contextlib.suppress(Exception):
                self._raw_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # -- lifecycle (implemented per printer) ------------------------------- #

    async def run(self) -> None:
        """Connect to the printer and keep the connection alive forever."""
        raise NotImplementedError

    @property
    def video_url(self) -> str:
        raise NotImplementedError

    async def prepare_video(self) -> None:
        """Hook run before opening the camera stream (e.g. to enable it)."""

    def describe(self) -> dict:
        """Extra connection details shown on ``GET /``."""
        return {}

    def debug_state(self) -> dict:
        return {}

    # -- status (implemented per printer) ---------------------------------- #

    def map_print_state(self) -> str:
        """Moonraker ``print_stats.state``: standby/printing/paused/complete/cancelled/error."""
        raise NotImplementedError

    def progress(self) -> float:
        raise NotImplementedError

    def build_print_stats(self) -> dict:
        raise NotImplementedError

    def build_virtual_sdcard(self) -> dict:
        raise NotImplementedError

    def build_gcode_move(self) -> dict:
        raise NotImplementedError

    def toolhead_position(self) -> list[float]:
        raise NotImplementedError

    def model_fan_speed(self) -> float:
        """Part-cooling fan speed, 0.0-1.0 (clamped by the caller)."""
        raise NotImplementedError

    def extruder_state(self) -> dict:
        raise NotImplementedError

    def heater_bed_state(self) -> dict:
        raise NotImplementedError

    def build_jobs(self) -> list[dict]:
        raise NotImplementedError

    def build_metadata(self, filename: str) -> dict:
        raise NotImplementedError

    def file_entries(self) -> list[dict]:
        """Moonraker ``/server/files/list`` items."""
        raise NotImplementedError

    def printer_info(self) -> dict:
        raise NotImplementedError

    def cpu_info(self) -> str:
        return "ELEGOO"

    def notify_signature(self) -> Any:
        """Value that changes whenever a ``notify_status_update`` is due."""
        raise NotImplementedError

    # -- commands (implemented per printer) -------------------------------- #

    async def pause(self) -> None:
        raise NotSupported("pause is not supported by this printer")

    async def resume(self) -> None:
        raise NotSupported("resume is not supported by this printer")

    async def cancel(self) -> None:
        raise NotSupported("cancel is not supported by this printer")

    async def start_print(self, filename: str) -> None:
        raise NotSupported("starting prints is not supported by this printer")

    async def run_gcode(self, script: str) -> None:
        """Best-effort translation of gcode sent by moonraker-obico."""

    async def upload_file(self, filename: str, data: bytes) -> dict:
        """Store an uploaded file on the printer; returns the Moonraker item."""
        raise NotSupported("file upload is not supported by this printer")

    # -- shared Moonraker objects ------------------------------------------ #

    def build_heaters(self) -> dict:
        return {
            "available_heaters": ["extruder", "heater_bed"],
            "available_sensors": [],
            "available_monitors": [],
        }

    def build_objects(self) -> dict:
        state = self.map_print_state()
        progress = self.progress()
        coord = self.toolhead_position()
        try:
            fan_speed = float(self.model_fan_speed())
        except (TypeError, ValueError):
            fan_speed = 0.0
        jobs = self.build_jobs()
        return {
            "print_stats": self.build_print_stats(),
            "virtual_sdcard": self.build_virtual_sdcard(),
            "display_status": {"progress": progress, "message": ""},
            "gcode_move": self.build_gcode_move(),
            "toolhead": {
                "position": coord,
                "homed_axes": "xyz",
                "extruder": "extruder",
            },
            "fan": {"speed": max(0.0, min(1.0, fan_speed))},
            "extruder": self.extruder_state(),
            "heater_bed": self.heater_bed_state(),
            "heaters": self.build_heaters(),
            "webhooks": {
                "state": "ready" if self.connected else "shutdown",
                "state_message": self.last_error or "",
            },
            "idle_timeout": {"state": "Idle" if state == "standby" else "Printing"},
            "history": {"job_history": {"jobs": jobs, "count": len(jobs)}},
            "configfile": {"config": {}, "settings": {}, "warnings": [], "save_config_pending": False},
        }

    # -- websocket fan-out -------------------------------------------------- #

    async def _notify_if_changed(self) -> None:
        signature = self.notify_signature()
        if signature != self._last_notified_state:
            self._last_notified_state = signature
            self.notify_count += 1
            await self.broadcast({
                "jsonrpc": "2.0",
                "method": "notify_status_update",
                "params": [{"print_stats": self.build_print_stats()}, time.time()],
            })

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(message, separators=(",", ":"))
        dead = []
        for client in list(self.clients):
            try:
                await client.send_text(text)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)

    # -- camera ------------------------------------------------------------- #

    async def frame_reader(self) -> None:
        """Continuously cache the latest JPEG from the printer MJPEG stream."""
        while True:
            try:
                await self.prepare_video()
                buf = bytearray()
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", self.video_url) as upstream:
                        async for chunk in upstream.aiter_bytes():
                            buf.extend(chunk)
                            while True:
                                start = buf.find(b"\xff\xd8")
                                if start < 0:
                                    if len(buf) > 1:
                                        del buf[:-1]
                                    break
                                end = buf.find(b"\xff\xd9", start + 2)
                                if end < 0:
                                    if start > 0:
                                        del buf[:start]
                                    break
                                self.latest_frame = bytes(buf[start:end + 2])
                                self.latest_frame_ts = time.time()
                                del buf[:end + 2]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("frame reader error: %s", exc)
                await asyncio.sleep(2)

    async def stream_frames(self, timeout: Optional[float] = None) -> AsyncIterator[bytes]:
        """Body of ``/webcam/stream``: proxy the printer's MJPEG stream."""
        await self.prepare_video()
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", self.video_url) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    async def cached_frame_stream(
        self,
        boundary: str = "--foo",
        fps: float = 10.0,
        idle_timeout: Optional[float] = None,
    ) -> AsyncIterator[bytes]:
        """MJPEG stream re-served from :attr:`latest_frame`.

        Used by printers that only allow a small number of concurrent camera
        connections, so viewers share the frame reader's single upstream.
        Ends when no new frame arrives for ``idle_timeout`` seconds.
        """
        last_ts = 0.0
        interval = 1.0 / fps
        last_new = time.monotonic()
        while True:
            if idle_timeout is not None and time.monotonic() - last_new > idle_timeout:
                return
            if self.latest_frame and self.latest_frame_ts != last_ts:
                last_ts = self.latest_frame_ts
                last_new = time.monotonic()
                frame = self.latest_frame
                yield (
                    f"{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode() + frame + b"\r\n"
            await asyncio.sleep(interval)
