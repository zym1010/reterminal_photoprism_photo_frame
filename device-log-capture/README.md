# device-log-capture

Small always-on service that solves one specific problem: `esphome logs` (or the ESPHome
Dashboard's log viewer) only streams live - close the window and everything before that moment is
gone. ESPHome itself doesn't persist logs anywhere either (confirmed - the Dashboard is a live view
only, same as the CLI). This connects to the eink frame's native API - the same connection
`esphome logs` uses - and keeps appending everything to a rotating file, so it survives closing
your terminal, browser, laptop sleep, etc.

Deliberately *not* built on the full `esphome` PyPI package (which drags in the whole C++
build/compiler toolchain needed for `esphome compile`/`run` - unnecessary weight just to tail
logs). Uses `aioesphomeapi` directly - the same library ESPHome's own CLI and Home Assistant's
integration use under the hood for this - specifically its `log_runner.async_run()` helper, which
wraps the same reconnect-with-backoff logic (`ReconnectLogic`) those tools rely on, rather than
reimplementing reconnect handling here.

## What it captures

Everything the device would show via `esphome logs`: its own log lines (config dump on connect,
button presses, refresh cycles, errors, ...) plus synthesized log-style lines for entity state
changes (sensor readings) - `async_run`'s default behavior, matching what you'd see interactively.
ANSI color codes are stripped (meaningless in a plain file) and each line gets a real wall-clock
timestamp prepended - the device's own embedded timestamp is relative to *that boot* (resets to
`00:00:00` on every reboot), not useful for correlating against real time days later on its own.

## Environment variables

| Var               | Default              | Meaning                                              |
|--------------------|----------------------|-------------------------------------------------------|
| `DEVICE_HOST`      | *(required)*         | Device's IP address - prefer this over its `eink-photo-frame.local` mDNS name (see "Networking" below) |
| `DEVICE_API_KEY`   | *(required)*         | Same value as `eink_photo_frame_api_key` in `esphome/secrets.yaml` - this connection is read-only (logs flow device → here only), so this key can't be used to control the device |
| `DEVICE_PORT`      | `6053`               | ESPHome native API port                                |
| `LOG_FILE`         | `/logs/device.log`   | Where logs are written (put this on a mounted volume)  |
| `LOG_MAX_BYTES`    | `20971520` (20MB)    | Rotate once a log file reaches this size                |
| `LOG_BACKUP_COUNT` | `5`                  | How many rotated files to keep (~100MB total at defaults) |
| `TZ`               | *(container default, UTC)* | Standard Docker/glibc timezone var - the wall-clock timestamp prepended to each line uses the container's own clock, which is UTC by default regardless of the NAS's own timezone. Set to e.g. `America/Los_Angeles` (same as the device's own `timezone:` in the ESPHome YAML) for local time instead. The device's own embedded per-line timestamp (relative to that boot) is unaffected either way. |

## Local testing (without Docker)

```bash
cd device-log-capture
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in DEVICE_HOST / DEVICE_API_KEY
set -a && source .env && set +a
.venv/bin/python capture_logs.py
tail -f /tmp/device.log   # in another terminal
```

## Build the Docker image

Same "build locally, import on NAS" workflow as `photo-frame-bridge` - no docker-compose or
registry needed. Match your NAS's CPU architecture (most Synology models are `linux/amd64`).

```bash
cd device-log-capture
docker build --platform linux/amd64 -t device-log-capture:latest .
docker save device-log-capture:latest -o device-log-capture.tar
```

## Networking

Plain bridge networking (Container Manager's default) is enough - this container only ever makes
an *outbound* connection to the device, nothing needs to reach it, and outbound LAN/internet
traffic through NAT on bridge networking already works fine in this project (`photo-frame-bridge`
reaches PhotoPrism, Open-Meteo, and Todoist the same way). No need for host networking.

The one real gotcha: if `DEVICE_HOST` is set to the device's `eink-photo-frame.local` mDNS name
instead of its IP, resolution can fail from inside a bridge-networked container - Docker's bridge
network doesn't forward the multicast traffic mDNS needs. Simplest fix, and the one used here: set
`DEVICE_HOST` to the device's actual IP address instead (matching `PHOTOPRISM_URL`/`bridge_url`
elsewhere in this project, both already hardcoded IPs for the same reason) - make sure your
router/NAS has a DHCP reservation for the device's MAC address so that IP doesn't drift over time.

## Deploy to Synology DSM (Container Manager)

1. Copy `device-log-capture.tar` to the NAS and import it (**Container Manager → Image → Add →
   Add From File**).
2. Create a NAS shared folder for the logs (e.g. `eink-logs`) so they survive container
   recreation/updates, not just restarts.
3. **Container Manager → Container → Create**, pick the imported image:
   - **Network**: default bridge is fine - see "Networking" above.
   - **Volume/Folder mapping**: mount the `eink-logs` folder to `/logs` inside the container.
   - **Environment variables**: set `DEVICE_HOST` (the device's IP, not its `.local` name) and
     `DEVICE_API_KEY` (see table above).
   - **Auto-restart**: enable - this needs to survive NAS reboots to actually be "long-term"
     logging, and `aioesphomeapi`'s reconnect logic already handles the device itself
     rebooting/going briefly offline (OTA updates, network hiccups) without needing the container
     itself to restart for that.
4. Start the container. Check the container's own log output once (**Container Manager →
   Container → [name] → Log**) for `Connected, capturing logs from ... to ...` - confirms it
   reached the device. After that, everything useful lives in the mounted `/logs/device.log` file
   itself, not the container's own stdout.

## Verify

From the NAS (File Station, or SSH):

```bash
tail -f /volume1/eink-logs/device.log
```

Should show the same lines `esphome logs` shows live, with a real timestamp prepended to each.
