# Manual install

This is the step-by-step version of `./install.sh`. Do this if you want to
understand or customize every part of the setup.

## 0. Prerequisites

- Docker Engine + Docker Compose v2 (`docker compose version`).
- Your Centauri Carbon or Centauri Carbon 2 on the LAN, stock firmware.
  For the Centauri Carbon 2, enable **LAN Only Mode** (Settings → Network →
  LAN Only Mode) and note the access code shown there.
- Its IP address (find it in the printer's network screen).
- An Obico account (cloud) or a self-hosted Obico server.

Quick sanity check that the printer is reachable:

```bash
# Centauri Carbon
curl -m 3 http://192.168.1.50:3031/video   # should stream MJPEG bytes
# Centauri Carbon 2 (LAN Only Mode on): the MQTT port must be open
nc -zv 192.168.1.50 1883
```

## 1. Get the code

```bash
git clone https://github.com/<your-user>/centauri-obico-bridge.git
cd centauri-obico-bridge
```

## 2. Configure the bridge

```bash
cp .env.example .env
```

Edit `.env`:

```ini
PRINTER_PROTOCOL=sdcp               # Centauri Carbon
CENTAURI_HOST=192.168.1.50          # <-- your printer IP
CENTAURI_SDCP_PORT=3030
CENTAURI_VIDEO_PORT=3031
START_CALIBRATION=1
OBICO_SERVER_URL=https://app.obico.io   # or http://<your-server>:3334
```

For a Centauri Carbon 2:

```ini
PRINTER_PROTOCOL=cc2
CENTAURI_HOST=192.168.1.50          # <-- your printer IP
CENTAURI_ACCESS_CODE=123456         # <-- from LAN Only Mode on the printer
START_CALIBRATION=1
OBICO_SERVER_URL=https://app.obico.io
```

## 3. Configure moonraker-obico

```bash
mkdir -p config data/logs data/bridge
cp config/moonraker-obico.cfg.template config/moonraker-obico.cfg
```

The generated config points `moonraker-obico` at the bridge
(`host = 127.0.0.1`, `port = 7125`). The `[server] auth_token` is filled in
automatically when you link the printer (step 6).

Make the mounted directories writable by the agent (it runs as uid 1000):

```bash
chmod -R a+rwX config data
```

## 4. Build and start

Full stack (bridge + agent):

```bash
docker compose up -d --build
```

Bridge only (if `moonraker-obico` already runs elsewhere):

```bash
docker compose -f docker-compose.bridge-only.yml up -d --build
```

## 5. Verify the bridge

```bash
curl http://127.0.0.1:7125/health
# {"ok":true,"centauri_connected":true,...}

curl http://127.0.0.1:7125/server/info
curl 'http://127.0.0.1:7125/printer/objects/query?print_stats&virtual_sdcard&extruder&heater_bed'
curl -o snap.jpg http://127.0.0.1:7125/webcam/snapshot && file snap.jpg
```

If `centauri_connected` is `false`, re-check `CENTAURI_HOST`.

## 6. Link the printer to Obico

### Cloud (app.obico.io)

The simplest way is to run the interactive linker:

```bash
./install.sh --link
```

Then, in the Obico app or web UI:

1. Add a new printer and choose **Klipper / Moonraker**.
2. Enter the 6-digit code shown by Obico when the linker asks for it.

The token is written to `config/moonraker-obico.cfg` and the agent reconnects.

You can also let Obico auto-discover the printer if your phone/computer is on
the same LAN: open the app, the printer appears as discoverable, click
**Link Now**.

### Self-hosted Obico

Set `OBICO_SERVER_URL` to your server (e.g. `http://192.168.1.10:3334`) and use
the same 6-digit flow. If you prefer, you can generate a printer token manually
and paste it into the config:

```ini
[server]
url = http://192.168.1.10:3334
auth_token = <printer token>
```

## 7. Verify end-to-end

```bash
docker compose logs -f moonraker-obico
```

You should see `Klippy ready`, then periodic status updates. In Obico the
printer becomes **Operational**, the webcam works, and the print controls are
available.

A quick way to prove the control path works is to press **Pause** in Obico
during a print and watch the printer pause.

## Updating

```bash
git pull
docker compose up -d --build
```

## Uninstalling

```bash
docker compose down
```

Your printer is untouched: this project only talks to it over the network.
Delete the folder to remove everything.
