import asyncio
import logging
import logging.handlers
import os
import re

from aioesphomeapi import APIClient, LogLevel
from aioesphomeapi.log_runner import async_run

DEVICE_HOST = os.environ["DEVICE_HOST"]
DEVICE_PORT = int(os.environ.get("DEVICE_PORT", "6053"))
# Same key as `api: encryption: key:` in the device's ESPHome YAML - this is
# a read-only log stream (device -> us), the same connection `esphome logs`
# itself uses, just kept running permanently instead of ending when a
# terminal window closes.
DEVICE_API_KEY = os.environ["DEVICE_API_KEY"]
LOG_FILE = os.environ.get("LOG_FILE", "/logs/device.log")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(20 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", "5"))

# ESPHome's log lines are ANSI-colored for terminal display - meaningless (and
# unreadable) once written to a plain file, so stripped here.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("capture_logs")

# A *separate* logger (not the root one used for our own startup/reconnect
# messages above) writing the device's own log lines to a rotating file.
# Device log lines already carry ESPHome's own timestamp (time since that
# boot, e.g. "[05:22:42.474]") - not useful across a reboot or for
# correlating against real time days later, so %(asctime)s here adds a real
# wall-clock anchor in front of each line.
device_log = logging.getLogger("device")
device_log.setLevel(logging.DEBUG)
device_log.propagate = False
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
device_log.addHandler(handler)


def on_log(msg):
    device_log.info(ANSI_RE.sub("", msg.message.decode(errors="replace")))


async def main():
    client = APIClient(DEVICE_HOST, DEVICE_PORT, password="", noise_psk=DEVICE_API_KEY)
    # async_run wraps aioesphomeapi's own ReconnectLogic - the same
    # reconnect-with-backoff handling `esphome logs`/Home Assistant's
    # integration use, not something worth reimplementing here. subscribe_states
    # (default True) additionally synthesizes log-style lines for entity state
    # changes (sensor readings, button presses), matching what `esphome logs`
    # shows interactively.
    await async_run(client, on_log, log_level=LogLevel.LOG_LEVEL_VERY_VERBOSE, name=DEVICE_HOST)
    log.info("Connected, capturing logs from %s to %s", DEVICE_HOST, LOG_FILE)
    await asyncio.Event().wait()  # run forever; ReconnectLogic handles disconnects


if __name__ == "__main__":
    asyncio.run(main())
