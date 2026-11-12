"""Centauri Carbon 2 (MQTT) backend.

``fixtures/cc2_printing.json`` is a real capture from a Centauri Carbon 2
(firmware 01.03.01.89) mid-print: the 1001/1002/1044 replies and the status
deltas (event 6000) that followed.  Serial and IP are anonymised.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib
import sys

import pytest

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "cc2_printing.json").read_text())
SN = "TESTSERIAL00001"
FILENAME = "ECC2_0.4_Spool adapter_Elegoo PLA _0.2_1h12m.gcode"


@pytest.fixture
def cc2_app(monkeypatch):
    monkeypatch.setenv("PRINTER_PROTOCOL", "cc2")
    monkeypatch.setenv("CENTAURI_HOST", "192.0.2.10")
    monkeypatch.setenv("CENTAURI_ACCESS_CODE", "abc123")
    monkeypatch.setenv("CENTAURI_SERIAL", SN)
    for name in list(sys.modules):
        if name == "app" or name.startswith("printers"):
            del sys.modules[name]
    import app as app_module

    yield app_module
    for name in list(sys.modules):
        if name == "app" or name.startswith("printers"):
            del sys.modules[name]


def _response(method_payload: dict) -> tuple[str, dict]:
    return f"elegoo/{SN}/0cliXXXXXX/api_response", copy.deepcopy(method_payload)


def _status(delta: dict) -> tuple[str, dict]:
    return f"elegoo/{SN}/api_status", copy.deepcopy(delta)


def feed(bridge, messages, *, connected=True):
    async def go():
        bridge.connected = connected
        for topic, data in messages:
            bridge._handle_message(topic, data)
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())


def printing_bridge(app_module):
    bridge = app_module.make_bridge()
    feed(bridge, [
        _response(FIXTURE["attributes"]),
        _response(FIXTURE["status"]),
        _response(FIXTURE["file_list"]),
        *[_status(d) for d in FIXTURE["deltas"]],
    ])
    return bridge


def test_selects_cc2_backend(cc2_app):
    from printers.cc2 import Cc2Printer

    assert isinstance(cc2_app.bridge, Cc2Printer)
    assert cc2_app.bridge.serial == SN


def test_printing_capture_maps_to_moonraker_objects(cc2_app):
    bridge = printing_bridge(cc2_app)
    objects = bridge.build_objects()
    last = FIXTURE["deltas"][-1]["result"]

    stats = objects["print_stats"]
    assert stats["state"] == "printing"
    assert stats["filename"] == FILENAME
    assert stats["info"] == {"total_layer": 300, "current_layer": 55}
    assert stats["print_duration"] == last["print_status"]["print_duration"]
    assert stats["total_duration"] == last["print_status"]["total_duration"]

    assert objects["virtual_sdcard"]["is_active"] is True
    assert objects["virtual_sdcard"]["progress"] == pytest.approx(0.17)
    assert objects["display_status"]["progress"] == pytest.approx(0.17)
    # Deltas are merged: x/y from the last delta, z from an earlier one.
    assert objects["toolhead"]["position"] == [last["gcode_move"]["x"], last["gcode_move"]["y"], 11.0, 0.0]
    assert objects["extruder"]["target"] == 210
    assert objects["heater_bed"] == {"temperature": 59.0, "target": 60.0}
    assert objects["fan"]["speed"] == pytest.approx(247 / 255)
    assert objects["gcode_move"]["speed_factor"] == 1.0
    assert objects["webhooks"]["state"] == "ready"
    assert objects["idle_timeout"]["state"] == "Printing"

    jobs = objects["history"]["job_history"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == FIXTURE["status"]["result"]["print_status"]["uuid"]
    assert jobs[0]["status"] == "in_progress"
    assert jobs[0]["metadata"]["layer_count"] == 300


def test_file_list_and_metadata(cc2_app):
    bridge = printing_bridge(cc2_app)
    paths = [e["path"] for e in bridge.file_entries()]
    assert FILENAME in paths
    for name in (FILENAME, f"gcodes/{FILENAME}", f"/local/{FILENAME}"):
        meta = bridge.build_metadata(name)
        assert meta["filename"] == FILENAME
        assert meta["size"] == 13255776
        assert meta["layer_count"] == 300
        assert meta["estimated_time"] == 4329
        assert meta["filament_weight_total"] == 41.04
        assert meta["slicer_version"] == "01.03.01.89"
    assert bridge.build_metadata("")["filename"] == ""


@pytest.mark.parametrize("machine,sub,state_str,expected", [
    (2, 2075, "printing", "printing"),
    (2, 1045, "printing", "printing"),       # preheating before first layer
    (2, 2901, "printing", "printing"),       # levelling during a print
    (2, 2501, "printing", "paused"),         # pausing
    (2, 2502, "paused", "paused"),
    (2, 2505, "paused", "paused"),
    (2, 2503, "printing", "cancelled"),      # stopping
    (1, 2504, "", "cancelled"),
    (1, 2077, "", "complete"),
    (1, 0, "complete", "complete"),
    (1, 0, "cancelled", "cancelled"),
    (1, 0, "", "standby"),
    (1, 0, "printing", "standby"),           # stale string while idle
    (14, 0, "", "error"),                    # emergency stop
])
def test_state_mapping(cc2_app, machine, sub, state_str, expected):
    bridge = cc2_app.make_bridge()
    bridge.connected = True
    bridge.status = {"machine_status": {"status": machine, "sub_status": sub},
                     "print_status": {"state": state_str, "filename": FILENAME}}
    assert bridge.map_print_state() == expected


def test_disconnected_is_standby_and_shutdown(cc2_app):
    bridge = printing_bridge(cc2_app)
    bridge.connected = False
    assert bridge.map_print_state() == "standby"
    assert bridge.build_objects()["webhooks"]["state"] == "shutdown"


def test_refused_full_status_keeps_previous_state(cc2_app):
    bridge = printing_bridge(cc2_app)
    feed(bridge, [_response({"id": 99, "method": 1002, "result": {"error_code": 1003}})])
    assert bridge.map_print_state() == "printing"
    assert bridge.build_print_stats()["filename"] == FILENAME


def test_sequence_gap_requests_full_status(cc2_app):
    bridge = printing_bridge(cc2_app)
    assert bridge._full_status_pending is False
    gap = copy.deepcopy(FIXTURE["deltas"][-1])
    gap["id"] += 5
    feed(bridge, [_status(gap)])
    assert bridge._full_status_pending is True


def test_job_end_time_is_recorded_when_print_finishes(cc2_app, monkeypatch):
    bridge = printing_bridge(cc2_app)
    monkeypatch.setattr("time.time", lambda: 2_000_000_000.0)
    feed(bridge, [_status({"id": 2000, "method": 6000, "result": {
        "machine_status": {"status": 1, "sub_status": 2077, "progress": 100}}})])
    job = bridge.build_jobs()[0]
    assert job["status"] == "completed"
    assert job["end_time"] == 2_000_000_000.0
    assert bridge.build_objects()["virtual_sdcard"]["is_active"] is False


class Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"error_code": 0}

    async def __call__(self, method, params=None, *, wait=False, timeout=10.0):
        self.calls.append({"method": method, "params": copy.deepcopy(params), "wait": wait})
        return dict(self.result) if wait else None


def test_print_control_routes(cc2_app):
    from fastapi.testclient import TestClient

    bridge = printing_bridge(cc2_app)
    cc2_app.bridge = bridge
    rec = Recorder()
    bridge.send_command = rec
    client = TestClient(cc2_app.app)

    assert client.post("/printer/print/pause").json() == {"result": "ok"}
    assert client.post("/printer/print/cancel").json() == {"result": "ok"}
    assert client.post("/printer/print/resume").json() == {"result": "ok"}
    assert client.post("/printer/print/start", data={"filename": f"gcodes/{FILENAME}"}).status_code == 200
    assert rec.calls == [
        {"method": 1021, "params": None, "wait": True},
        {"method": 1022, "params": None, "wait": True},
        # Resume is fire-and-forget: the printer only replies after reheating.
        {"method": 1023, "params": None, "wait": False},
        {"method": 1020, "wait": True, "params": {
            "storage_media": "local", "filename": FILENAME,
            "config": {"slot_map": [], "printer_check": True}}},
    ]
    assert bridge.active_filename == FILENAME


def test_rejected_command_returns_error(cc2_app):
    from fastapi.testclient import TestClient

    bridge = printing_bridge(cc2_app)
    cc2_app.bridge = bridge
    bridge.send_command = Recorder(result={"error_code": 1009})
    resp = TestClient(cc2_app.app).post("/printer/print/start", json={"filename": FILENAME})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == 1009
    assert "busy" in resp.json()["error"]["message"]


def test_not_connected_returns_503(cc2_app):
    from fastapi.testclient import TestClient

    bridge = cc2_app.make_bridge()
    cc2_app.bridge = bridge
    resp = TestClient(cc2_app.app).post("/printer/print/pause")
    assert resp.status_code == 503


def test_gcode_translation(cc2_app):
    bridge = printing_bridge(cc2_app)
    rec = Recorder()
    bridge.send_command = rec
    asyncio.run(bridge.run_gcode("\n".join([
        "M104 S215", "M140 S65", "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=999",
        "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=55", "M106 S128", "M107",
        "SET_FAN_SPEED FAN=fan SPEED=0.5", "G28", "G1 X10", "; comment",
    ])))
    assert [(c["method"], c["params"]) for c in rec.calls] == [
        (1028, {"extruder": 215}),
        (1028, {"heater_bed": 65}),
        (1028, {"extruder": 320}),        # clamped
        (1028, {"heater_bed": 55}),
        (1030, {"fan": 128}),
        (1030, {"fan": 0}),
        (1030, {"fan": 128}),
    ]


def test_http_endpoints(cc2_app):
    from fastapi.testclient import TestClient

    bridge = printing_bridge(cc2_app)
    cc2_app.bridge = bridge
    client = TestClient(cc2_app.app)
    info = client.get("/printer/info").json()["result"]
    assert info["state"] == "ready"
    assert info["hostname"] == "Elegoo Centauri Carbon 2"
    assert info["software_version"] == "01.03.01.89"
    root = client.get("/").json()
    assert root["protocol"] == "cc2" and root["serial"] == SN
    assert client.get("/server/history/list").json()["result"]["count"] == 1
    assert client.get("/printer/objects/query?print_stats").json()["result"]["status"]["print_stats"]["state"] == "printing"
    cams = client.get("/server/webcams/list").json()["result"]["webcams"]
    assert cams[0]["snapshot_url"].endswith("/webcam/snapshot")
    upload = client.post("/server/files/upload", files={"file": ("a.gcode", b"G28\n")})
    assert upload.status_code == 501


def test_cached_stream_ends_after_idle_timeout(cc2_app):
    bridge = cc2_app.make_bridge()
    bridge.latest_frame = b"\xff\xd8jpeg\xff\xd9"
    bridge.latest_frame_ts = 1.0

    async def collect():
        return [chunk async for chunk in bridge.stream_frames(timeout=0.3)]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert chunks[0].endswith(b"\xff\xd8jpeg\xff\xd9\r\n")


def test_printing_is_held_until_filename_is_known(cc2_app):
    # Print started from the slicer: status flips to printing before the
    # printer reports the filename. moonraker-obico must not see "printing"
    # with an empty filename, or Obico rejects the g-code file record.
    bridge = cc2_app.make_bridge()
    feed(bridge, [
        _response(FIXTURE["attributes"]),
        _response(FIXTURE["file_list"]),
        _status({"id": 1, "method": 6000, "result": {
            "machine_status": {"status": 2, "sub_status": 1045}}}),
    ])
    assert bridge.map_print_state() == "standby"
    assert bridge.build_print_stats()["filename"] == ""
    assert bridge._full_status_pending is True

    feed(bridge, [_status({"id": 2, "method": 6000, "result": {
        "print_status": {"filename": FILENAME}}})])
    assert bridge.map_print_state() == "printing"
    assert bridge.build_print_stats()["filename"] == FILENAME
    assert bridge.build_metadata(FILENAME)["size"] == 13255776
    assert bridge._file_list_pending is False


def test_unknown_print_file_triggers_file_list_refresh(cc2_app):
    bridge = cc2_app.make_bridge()
    feed(bridge, [
        _response(FIXTURE["file_list"]),
        _status({"id": 1, "method": 6000, "result": {
            "machine_status": {"status": 2, "sub_status": 1045},
            "print_status": {"filename": "just-uploaded.gcode"}}}),
    ])
    assert bridge.map_print_state() == "printing"
    assert bridge._file_list_pending is True
