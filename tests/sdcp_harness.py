"""Drive the SDCP (Centauri Carbon) backend with synthetic printer messages and
record everything the Moonraker-compatible API would expose.

The recorded output is compared against ``tests/golden/sdcp_golden.json``, which
was generated from the bridge *before* multi-printer support was added.  This
guards the original Centauri Carbon behaviour without needing the hardware.

Regenerate (only when an SDCP behaviour change is intended):

    python -m tests.sdcp_harness > tests/golden/sdcp_golden.json
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from typing import Any

FIXED_NOW = 1_700_000_000.0

os.environ.setdefault("CENTAURI_HOST", "192.0.2.10")
os.environ.pop("PRINTER_PROTOCOL", None)

FILENAME = "/local/benchy.gcode"
TASK_ID = "11111111-2222-3333-4444-555555555555"

ATTRIBUTES_MSG = {
    "Topic": "sdcp/attributes/MB123",
    "MainboardID": "MB123",
    "Attributes": {
        "Name": "My Centauri",
        "MachineName": "Centauri Carbon",
        "BrandName": "ELEGOO",
        "FirmwareVersion": "V1.1.29",
        "MainboardIP": "192.0.2.10",
    },
}

FILE_LIST_MSG = {
    "Topic": "sdcp/response/MB123",
    "Data": {
        "Cmd": 258,
        "RequestID": "r-files",
        "Data": {
            "Ack": 0,
            "FileList": [
                {"name": FILENAME, "FileSize": 4_000_000, "CreateTime": 1_699_000_000,
                 "TotalLayers": 240, "LayerHeight": 0.2, "type": 1},
                {"name": "/local/cube.gcode", "FileSize": 500_000, "CreateTime": 1_698_000_000,
                 "TotalLayers": 100, "LayerHeight": 0.2, "type": 1},
            ],
        },
    },
}

FILE_DETAIL_MSG = {
    "Topic": "sdcp/response/MB123",
    "Data": {
        "Cmd": 260,
        "RequestID": "r-detail",
        "Data": {"Ack": 0, "FileInfo": {"EstTime": 2510, "TotalLayers": 240,
                                         "EstWeight": 15.0, "Thumbnail": "/thumb/benchy.png"}},
    },
}

TASK_DETAIL_MSG = {
    "Topic": "sdcp/response/MB123",
    "Data": {
        "Cmd": 321,
        "RequestID": "r-task",
        "Data": {"Ack": 0, "HistoryDetailList": [{
            "TaskId": TASK_ID, "TaskName": FILENAME,
            "BeginTime": 1_699_999_000, "EndTime": 0,
            "SliceInformation": {"total_layer_numbers": 240},
        }]},
    },
}


def status_msg(print_status: int, current_status: list[int], *, filename: str = FILENAME,
               task_id: str = TASK_ID, progress: int = 20, layer: int = 45) -> dict:
    return {
        "Topic": "sdcp/status/MB123",
        "MainboardID": "MB123",
        "Status": {
            "CurrentStatus": current_status,
            "TempOfNozzle": 209.9, "TempTargetNozzle": 210,
            "TempOfHotbed": 60.1, "TempTargetHotbed": 60,
            "TempOfBox": 32.0, "TempTargetBox": 0,
            "CurrenCoord": "134.41,120.71,8.32",
            "CurrentFanSpeed": {"ModelFan": 98, "AuxiliaryFan": 0, "BoxFan": 68},
            "LightStatus": {"SecondLight": 1, "RgbLight": [0, 0, 0]},
            "PrintInfo": {
                "Status": print_status, "CurrentLayer": layer, "TotalLayer": 240,
                "CurrentTicks": 458.2, "TotalTicks": 2068,
                "Filename": filename, "TaskId": task_id,
                "PrintSpeedPct": 100, "Progress": progress,
            },
        },
    }


IDLE_MSG = {
    "Topic": "sdcp/status/MB123",
    "MainboardID": "MB123",
    "Status": {
        "CurrentStatus": [0],
        "TempOfNozzle": 25.0, "TempTargetNozzle": 0,
        "TempOfHotbed": 24.0, "TempTargetHotbed": 0,
        "CurrenCoord": "0.00,0.00,0.00",
        "CurrentFanSpeed": {"ModelFan": 0},
        "PrintInfo": {"Status": 0, "CurrentLayer": 0, "TotalLayer": 0, "CurrentTicks": 0,
                      "Filename": "", "TaskId": "", "PrintSpeedPct": 100, "Progress": 0},
    },
}

SCENARIOS: list[tuple[str, list[dict]]] = [
    ("idle", [ATTRIBUTES_MSG, FILE_LIST_MSG, IDLE_MSG]),
    ("preparing_no_task", [ATTRIBUTES_MSG, FILE_LIST_MSG,
                           status_msg(1, [1], filename="", task_id="", progress=0, layer=0)]),
    ("printing", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(13, [1]), FILE_DETAIL_MSG, TASK_DETAIL_MSG]),
    ("paused", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(6, [6]), TASK_DETAIL_MSG]),
    ("pausing", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(5, [1])]),
    ("complete", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(9, [0], progress=100, layer=240)]),
    ("cancelled", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(8, [0])]),
    ("stopped_14", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(14, [0])]),
    ("file_check", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(10, [1])]),
    ("recovery", [ATTRIBUTES_MSG, FILE_LIST_MSG, status_msg(12, [1])]),
    ("unknown_status_flag_printing", [ATTRIBUTES_MSG, status_msg(99, [1])]),
    ("unknown_status_flag_paused", [ATTRIBUTES_MSG, status_msg(99, [6])]),
    ("unknown_status_flag_complete", [ATTRIBUTES_MSG, status_msg(99, [9])]),
    ("unknown_status_flag_cancelled", [ATTRIBUTES_MSG, status_msg(99, [8])]),
    ("unknown_status_none", [ATTRIBUTES_MSG, status_msg(99, [])]),
    ("garbage_values", [{
        "Topic": "sdcp/status/MB123",
        "Status": {"CurrenCoord": "bad", "CurrentFanSpeed": {"ModelFan": "x"},
                   "PrintInfo": {"Status": 13, "Progress": "nope", "PrintSpeedPct": "fast",
                                 "Filename": FILENAME, "TaskId": TASK_ID}},
    }]),
]

HTTP_GETS = [
    "/server/info",
    "/server/config",
    "/access/api_key",
    "/printer/info",
    "/printer/objects/list",
    "/printer/objects/query",
    "/printer/objects/query?print_stats&extruder",
    "/server/files/list",
    "/server/files/metadata?filename=" + FILENAME,
    "/server/files/metadata?filename=gcodes/benchy.gcode",
    "/server/files/metadata?filename=",
    "/server/history/list",
    "/server/history/totals",
    "/server/database/item?namespace=mainsail&key=presets",
    "/machine/update/status",
    "/machine/system_info",
    "/machine/device_power/devices",
    "/server/webcams/list",
    "/webcams/list",
    "/api/status",
    "/",
]


def _scrub(value: Any) -> Any:
    """Drop fields that legitimately vary between runs."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("eventtime", "connection_id"):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _load_app():
    for name in list(sys.modules):
        if name == "app" or name.startswith("printers"):
            del sys.modules[name]
    import app as app_module  # noqa: WPS433

    return app_module


def run() -> dict:
    from fastapi.testclient import TestClient

    real_time = time.time
    time.time = lambda: FIXED_NOW
    try:
        app_module = _load_app()
        results: dict[str, Any] = {}

        for name, messages in SCENARIOS:
            app_module.bridge = app_module.make_bridge() if hasattr(app_module, "make_bridge") \
                else app_module.CentauriBridge()
            bridge = app_module.bridge
            sent: list[Any] = []

            async def fake_send(cmd, data=None, *, wait=False, timeout=8.0, _sent=sent):
                _sent.append({"cmd": cmd, "data": copy.deepcopy(data)})
                return {"Ack": 0} if wait else None

            bridge.send_command = fake_send  # type: ignore[assignment]

            async def feed():
                bridge.connected = True
                for msg in messages:
                    bridge._handle_message(copy.deepcopy(msg))
                    await asyncio.sleep(0)
                await asyncio.sleep(0)

            asyncio.run(feed())
            client = TestClient(app_module.app)
            scenario: dict[str, Any] = {
                "print_state": bridge.map_print_state(),
                "objects": _scrub(bridge.build_objects()),
                "jobs": _scrub(bridge.build_jobs()),
                "metadata": _scrub(bridge.build_metadata(FILENAME)),
                "background_commands": sent[:],
                "http": {},
            }
            for path in HTTP_GETS:
                resp = client.get(path)
                scenario["http"][path] = {"status": resp.status_code, "body": _scrub(resp.json())}
            results[name] = scenario

        # Disconnected printer.
        app_module.bridge = app_module.make_bridge() if hasattr(app_module, "make_bridge") \
            else app_module.CentauriBridge()
        client = TestClient(app_module.app)
        results["disconnected"] = {
            "objects": _scrub(app_module.bridge.build_objects()),
            "server_info": _scrub(client.get("/server/info").json()),
            "printer_info": _scrub(client.get("/printer/info").json()),
            "pause_not_connected": {"status": client.post("/printer/print/pause").status_code},
        }

        # Print control and gcode translation.
        app_module.bridge = app_module.make_bridge() if hasattr(app_module, "make_bridge") \
            else app_module.CentauriBridge()
        bridge = app_module.bridge
        bridge.connected = True
        sent = []
        ack = {"value": 0}

        async def fake_send2(cmd, data=None, *, wait=False, timeout=8.0):
            sent.append({"cmd": cmd, "data": copy.deepcopy(data), "wait": wait})
            return {"Ack": ack["value"]} if wait else None

        bridge.send_command = fake_send2  # type: ignore[assignment]
        client = TestClient(app_module.app)
        control: dict[str, Any] = {}
        for path, kwargs in [
            ("/printer/print/pause", {}),
            ("/printer/print/resume", {}),
            ("/printer/print/cancel", {}),
            ("/printer/print/stop", {}),
            ("/printer/emergency_stop", {}),
            ("/printer/print/start", {"data": {"filename": "gcodes/benchy.gcode"}}),
            ("/printer/print/start", {"json": {"filename": "cube.gcode"}}),
            ("/printer/print/start", {}),
            ("/printer/gcode/script", {"data": {"script": "\n".join([
                "G28", "G28 X Y", "G1 X10 Y20 Z5 F3000", "M104 S210", "M109 S215",
                "M140 S60", "M190 S65", "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200",
                "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=55", "M106 S128", "M106",
                "SET_FAN_SPEED FAN=fan SPEED=0.5", "SET_FAN_SPEED FAN=fan SPEED=40",
                "; comment only", "M117 hello",
            ])}}),
        ]:
            sent.clear()
            resp = client.post(path, **kwargs)
            control[f"{path} {json.dumps(kwargs, sort_keys=True)}"] = {
                "status": resp.status_code, "body": resp.json(), "sent": sent[:],
            }
        ack["value"] = 1
        sent.clear()
        resp = client.post("/printer/print/pause")
        control["pause_ack_1"] = {"status": resp.status_code, "body": resp.json(), "sent": sent[:]}
        results["control"] = control
        results["control_state_after_start"] = {
            "active_filename": bridge.active_filename,
            "print_started_ts": bridge.print_started_ts,
        }
        return results
    finally:
        time.time = real_time


if __name__ == "__main__":
    json.dump(run(), sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write("\n")
