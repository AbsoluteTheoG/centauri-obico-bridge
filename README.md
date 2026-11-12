# centauri-obico-bridge

**Use [Obico](https://www.obico.io/) (AI failure detection, remote control,
webcam) with an Elegoo Centauri Carbon or Centauri Carbon 2 running its stock
firmware — no flashing, no OpenCentauri/COSMOS, no firmware modification.**

This project is a small Docker container that speaks the printer's native
protocol (**SDCP** on the Centauri Carbon, **MQTT** on the Centauri Carbon 2)
and exposes a **Moonraker-compatible API**, which is exactly what
`moonraker-obico` expects. Everything happens on the network and in
Docker, so it is fully reversible: stop the container and your printer is back
to stock.

```
┌──────────────────────────────┐
│  Elegoo Centauri Carbon      │
│  (stock firmware)            │
│                              │
│  CC:  SDCP WebSocket :3030   │
│       MJPEG camera   :3031   │
│  CC2: MQTT broker    :1883   │
│       MJPEG camera   :8080   │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│  centauri-obico-bridge       │  ← this project (port 7125)
│  SDCP/MQTT ⇄ Moonraker API   │
└───────────────┬──────────────┘
                │  Moonraker HTTP + WebSocket JSON-RPC
                ▼
┌──────────────────────────────┐
│  moonraker-obico (agent)     │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│  Obico (cloud or self-hosted)│
└──────────────────────────────┘
```

## Why

Obico only talks to printers through OctoPrint or Moonraker. The Centauri
Carbon speaks SDCP (Chitu's Smart Device Control Protocol), the Centauri
Carbon 2 speaks its own MQTT protocol, and neither has a Moonraker server. This bridge translates between the two so you get:

- AI failure detection (spaghetti detection) with automatic **pause**
- Live webcam in the Obico app
- Print control: **start / pause / resume / cancel**
- Print status, progress, temperatures, layer info
- Upload a G-code to Obico and print it on the Centauri

## Features

| Feature | Centauri Carbon | Centauri Carbon 2 |
| --- | --- | --- |
| Live status (progress, layers, temps, position) | ✅ | ✅ |
| Webcam MJPEG stream + JPEG snapshot | ✅ | ✅ |
| Obico failure detection → pause | ✅ | ✅ (same pause command as below) |
| Pause / resume | ✅ | ✅ |
| Start / cancel from Obico | ✅ | 🧪 not yet verified on hardware |
| Upload & print G-code from Obico | ✅ | ❌ (print files already on the printer) |
| Temperature / fan gcode | ✅ | 🧪 not yet verified on hardware |
| Home / jog gcode | ✅ | ❌ |
| Job history / metadata | ✅ | ✅ |
| Moonraker WebSocket JSON-RPC | ✅ | ✅ |
| No firmware modification | ✅ | ✅ |
| Fully reversible | ✅ | ✅ |

## Requirements

- An Elegoo Centauri Carbon or Centauri Carbon 2 on your LAN with the stock
  firmware. The **Centauri Carbon 2** must have **LAN Only Mode** enabled
  (printer: *Settings → Network → LAN Only Mode*); note the **access code**
  shown there. LAN Only Mode disables Elegoo's cloud features.
- A host that can run Docker (Linux recommended; the agent uses host
  networking, so Docker Desktop on macOS/Windows is not supported).
- An Obico account: [cloud](https://app.obico.io) or a
  [self-hosted server](https://github.com/TheSpaghettiDetective/obico-server).

## Quick install (script)

```bash
git clone https://github.com/absolutetheoG/centauri-obico-bridge.git
cd centauri-obico-bridge
./install.sh
```

The script will:

1. Check for Docker/Compose (and offer to install Docker if missing).
2. Ask for your **printer IP**, **printer model** (and, for the Centauri
   Carbon 2, its **access code**) and your **Obico server URL**.
3. Generate `.env` and `config/moonraker-obico.cfg`.
4. Build and start the bridge **and** `moonraker-obico`.
5. Wait until the bridge is healthy.

Then link the printer to Obico:

```bash
./install.sh --link
```

Open the Obico app/web UI, add a Klipper printer, and enter the 6-digit code
when prompted. That's it.

Useful commands:

```bash
./install.sh --status     # containers + bridge health
./install.sh --link       # link / re-link to Obico
docker compose logs -f    # follow logs
docker compose down       # stop everything
```

## Manual install

Prefer to understand every step? See
**[docs/MANUAL_INSTALL.md](docs/MANUAL_INSTALL.md)**. In short:

```bash
cp .env.example .env
# edit CENTAURI_HOST (and OBICO_SERVER_URL if self-hosting)
# Centauri Carbon 2: also set PRINTER_PROTOCOL=cc2 and CENTAURI_ACCESS_CODE
cp config/moonraker-obico.cfg.template config/moonraker-obico.cfg
docker compose up -d --build
```

If you already run `moonraker-obico` elsewhere, use the bridge only:

```bash
docker compose -f docker-compose.bridge-only.yml up -d --build
```

## Configuration

All settings live in `.env` (see `.env.example`):

| Variable | Default | Description |
| --- | --- | --- |
| `PRINTER_PROTOCOL` | `sdcp` | `sdcp` = Centauri Carbon, `cc2` = Centauri Carbon 2 |
| `CENTAURI_HOST` | – | **Required.** Printer IP or hostname |
| `START_CALIBRATION` | `1` | Bed levelling when a print is started from Obico (1/0) |
| `OBICO_SERVER_URL` | `https://app.obico.io` | Used to generate the agent config |

Centauri Carbon (`sdcp`) only:

| Variable | Default | Description |
| --- | --- | --- |
| `CENTAURI_SDCP_PORT` | `3030` | SDCP WebSocket port |
| `CENTAURI_VIDEO_PORT` | `3031` | MJPEG camera port |

Centauri Carbon 2 (`cc2`) only:

| Variable | Default | Description |
| --- | --- | --- |
| `CENTAURI_ACCESS_CODE` | – | **Required.** Access code from *LAN Only Mode* |
| `CENTAURI_SERIAL` | *(auto)* | Serial number; discovered from the printer if empty |
| `CC2_MQTT_PORT` | `1883` | Printer MQTT broker port |
| `CC2_VIDEO_PORT` | `8080` | MJPEG camera port |
| `STATUS_REFRESH_INTERVAL` | `30` | Seconds between full status refreshes (deltas are pushed) |

Additional bridge options (advanced, set in `docker-compose.yml`):

| Variable | Default | Description |
| --- | --- | --- |
| `POLL_INTERVAL` | `2` | Seconds between status polls (SDCP) |
| `BRIDGE_PUBLIC_URL` | *(auto)* | Public URL used in `/server/webcams/list` |
| `SDCP_RAW_LOG` / `RAW_LOG` | – | Path to append raw printer traffic (JSONL) |

## How it works

The printer client is chosen with `PRINTER_PROTOCOL`; the code lives in
`printers/sdcp.py` and `printers/cc2.py`, and `app.py` serves the Moonraker API
for either.

### Centauri Carbon (SDCP)

The bridge connects to `ws://<printer>:3030/websocket` and:

- keys every SDCP response by `Cmd` + `RequestID` (they all share the same
  `sdcp/response/<MainboardID>` topic),
- keeps the latest `sdcp/status/<MainboardID>` and
  `sdcp/attributes/<MainboardID>`,
- maps `PrintInfo.Status` to Moonraker `print_stats.state`,
- translates Moonraker print commands back into SDCP commands,
- proxies the camera and extracts JPEG snapshots from the MJPEG stream,
- proxies G-code uploads to the printer's HTTP upload endpoint.

### Centauri Carbon 2 (MQTT)

The printer runs its own MQTT broker. The bridge logs in as `elegoo` with the
access code and:

- learns the serial number from `elegoo/<sn>/api_status` (or uses
  `CENTAURI_SERIAL`),
- registers as a client on `elegoo/<sn>/api_register` and sends a heartbeat,
- requests attributes (1001), full status (1002) and the file list (1044),
  then merges the status deltas the printer pushes (event 6000), asking for a
  full status again when a delta is missed,
- maps `machine_status.status` / `sub_status` to Moonraker
  `print_stats.state`,
- sends pause (1021), resume (1023), stop (1022), start (1020), temperature
  (1028) and fan (1030) commands,
- enables the camera (1042) and re-serves `http://<printer>:8080/?action=stream`
  from one upstream connection, since the printer allows few camera viewers.

The CC2 protocol details come from
[elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant).

### Exposed Moonraker API (port 7125)

`/server/info`, `/printer/info`, `/printer/objects/{list,query,subscribe}`,
`/server/files/{list,metadata,upload}`, `/server/history/list`,
`/server/webcams/list`, `/server/database/item`, `/machine/update/status`,
`/printer/print/{start,pause,resume,cancel}`, `/printer/gcode/script`, and a
Moonraker-compatible WebSocket at `/websocket`.

### Camera

- `GET /webcam/stream` – MJPEG proxy
- `GET /webcam/snapshot` – single JPEG frame (cached)

### Diagnostics

- `GET /health`
- `GET /debug/state` – parsed status / attributes / objects
- `GET /debug/raw` – SDCP responses keyed by Cmd/RequestID
- `GET /debug/messages` – last raw SDCP messages

## Troubleshooting

**The bridge reports `centauri_connected: false`.**
Centauri Carbon: check the printer IP and that ports 3030/3031 are reachable:
`curl http://<printer>:3031/video` should stream MJPEG.
Centauri Carbon 2: check that LAN Only Mode is on, that `CENTAURI_ACCESS_CODE`
matches the code on the printer (it changes when LAN Only Mode is toggled) and
that port 1883 is reachable. `GET /health` shows the last error.

**Obico shows the printer offline.**
Check `docker compose logs -f moonraker-obico`. The agent must reach the bridge
on `127.0.0.1:7125` (it uses host networking).

**A print started from Obico doesn't seem to launch.**
The printer runs a long calibration (nozzle PID tuning + bed levelling) before
actually printing — up to ~5 minutes. During that time the state is reported
as `printing` but pause/cancel are ignored by the firmware. Wait for the
nozzle to reach temperature and the first layer to begin.

**Uploading a G-code fails.**
The printer only accepts uploads while idle, not during a print.

## Limitations

- SDCP is a reverse-engineered, undocumented protocol. Mappings were validated
  against firmware **V1.4.49**; other firmware versions may differ.
- The Centauri Carbon 2 support was validated against firmware **01.03.01.89**.
  G-code upload from Obico, homing and jogging are not supported on it yet.
- On the Centauri Carbon 2, resume is sent without waiting for a reply: the
  printer only acknowledges it after reheating and homing (~2 minutes).
- Obico's failure detection **pauses** the printer; it never auto-cancels.
- During the printer's calibration phase, pause/cancel commands are ignored by
  the firmware.
- `server/files/delete`, timelapse and history video are not implemented.
- Jog / home / temperature commands are translated best-effort.

## Reverse engineering

How the protocol was mapped (SDCP command IDs, status codes, upload format) is
documented in [docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md).

## License

[MIT](LICENSE). Not affiliated with Elegoo or Obico. `moonraker-obico` is a
separate GPL-3.0 project; this repository only runs its published image.

## Built with AI

This project was built with the help of AI, using the **DeepSeek API**
(`deepseek-v4-flash-vision-exp`) through [opencode](https://opencode.ai).

The SDCP protocol was reverse-engineered by inspecting the stock web UI and by
capturing live traffic, and the bridge, installer and documentation were
written iteratively by an AI assistant that ran real tests against an actual
Centauri Carbon (status capture, webcam, pause/resume/cancel, G-code upload and
the full Obico integration). Review the code before trusting it on your own
hardware.
