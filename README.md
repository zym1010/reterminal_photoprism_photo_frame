# reTerminal PhotoPrism Photo Frame

Turns a [Seeed reTerminal E1002](https://www.seeedstudio.com/reTerminal-E1002-p-6533.html)
(ESP32-S3, 7.3" Spectra 6 color e-paper) into a random-photo digital frame powered by a
self-hosted [PhotoPrism](https://www.photoprism.app/) library. Fully self-hosted: photo data never
leaves the LAN, no cloud accounts of any kind.

## Architecture

```
[PhotoPrism] <--API--> [photo-frame-bridge] <--HTTP GET /frame.png, /stats.png--> [E1002 running ESPHome]
 (existing)              (this repo)                                               (polls / button press)
```

- **`photo-frame-bridge/`** - a small self-hosted Flask service with two endpoints: a random
  favorited photo (cropped to 800x480, dithered to the panel's 6 native ink colors - see that
  directory's README for why *idealized* colors are used instead of "realistic" ones, a genuinely
  surprising on-device driver quirk) and a PhotoPrism library stats card, also rendered as an
  image so the device side stays simple.
- **`esphome/`** - ESPHome firmware for the E1002. Two pages (slideshow / stats), switched with
  the two white buttons; the green button fetches a new random photo on demand. No Home
  Assistant, no cloud - the device talks directly to the bridge over the LAN.

See each subdirectory's README for setup/deployment details.

## Requirements

- A PhotoPrism instance reachable on your LAN, with some photos marked as favorites.
- Docker, for building/running the bridge (this project uses a build-locally-and-import workflow
  aimed at Synology DSM Container Manager, but any Docker host works).
- A Seeed reTerminal E1002, connected via USB for the first flash.
