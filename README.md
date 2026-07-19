# reTerminal PhotoPrism Photo Frame

Turns a [Seeed reTerminal E1002](https://www.seeedstudio.com/reTerminal-E1002-p-6533.html)
(ESP32-S3, 7.3" Spectra 6 color e-paper) into a random-photo digital frame powered by a
self-hosted [PhotoPrism](https://www.photoprism.app/) library. Fully self-hosted: photo data never
leaves the LAN, no cloud accounts of any kind.

## Architecture

```
[PhotoPrism] <--API--> [photo-frame-bridge] <--HTTP GET /frame.png--> [E1002 running ESPHome]
 (existing)              (this repo)                                   (polls periodically)
```

- **`photo-frame-bridge/`** - a small self-hosted Flask service. On every request it asks
  PhotoPrism for a random favorited JPEG, crops it to 800x480, and dithers it to the panel's 6
  native ink colors (see that directory's README for why *idealized* colors are used instead of
  "realistic" ones - a genuinely surprising on-device driver quirk).
- **`esphome/`** - ESPHome firmware for the E1002. Polls the bridge on a timer (and on a physical
  button press) and displays whatever comes back. No Home Assistant, no cloud - the device talks
  directly to the bridge over the LAN.

See each subdirectory's README for setup/deployment details.

## Requirements

- A PhotoPrism instance reachable on your LAN, with some photos marked as favorites.
- Docker, for building/running the bridge (this project uses a build-locally-and-import workflow
  aimed at Synology DSM Container Manager, but any Docker host works).
- A Seeed reTerminal E1002, connected via USB for the first flash.
