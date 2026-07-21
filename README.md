# reTerminal PhotoPrism Photo Frame

Turns a [Seeed reTerminal E1002](https://www.seeedstudio.com/reTerminal-E1002-p-6533.html)
(ESP32-S3, 7.3" Spectra 6 color e-paper) into a digital frame with two switchable categories:
random photos from any number of sources, and a small dashboard (weather, PhotoPrism library
stats, Todoist tasks). Photo data never leaves the LAN and there are no new cloud accounts beyond
two deliberate exceptions: the weather dashboard source calls the external Open-Meteo API (a real
forecast needs a data source no amount of self-hosting can replace), and the optional `todos`
source calls the Todoist API (same reasoning - no realistic self-hosted alternative to a task
manager you actually already use).

## Architecture

```
[PhotoPrism] <--API--> [photo-frame-bridge] <--HTTP GET /photo.png?index=N--> [E1002 running ESPHome]
 (existing)              (this repo)          <--HTTP GET /dashboard.png?index=N-->  (button press)
 [adhoc_images folder] <--mounted-->             <--API--> [Open-Meteo]
```

- **`photo-frame-bridge/`** - a small self-hosted Flask service with two generic, index-based
  endpoints. `GET /photo.png?index=N` picks photo source `N % len(sources)` - sources are
  PhotoPrism favorites (cropped to 800x480, dithered to the panel's 6 native ink colors - see
  that directory's README for why *idealized* colors are used instead of "realistic" ones, a
  genuinely surprising on-device driver quirk) plus every subfolder of a mounted `adhoc_images`
  folder, discovered fresh on every request. `GET /dashboard.png?index=N` does the same for
  dashboard sources (weather, PhotoPrism stats, Todoist). Both endpoints do the source-count wraparound
  server-side, so the device never needs to know how many sources exist - a new `adhoc_images`
  subfolder just becomes reachable, no redeploy needed.
- **`esphome/`** - ESPHome firmware for the E1002. Two pages (photo / dashboard): the green
  button refreshes whichever is showing (new random photo, or fresh dashboard data - same
  source, no cycling); the two white buttons each cycle to the next source in their category. No
  Home Assistant, no cloud SaaS - the device talks directly to the bridge over the LAN.

See each subdirectory's README for setup/deployment details.

## Requirements

- A PhotoPrism instance reachable on your LAN, with some photos marked as favorites.
- Docker, for building/running the bridge (this project uses a build-locally-and-import workflow
  aimed at Synology DSM Container Manager, but any Docker host works).
- Optionally, a NAS shared folder (`adhoc_images`) mounted into the bridge container, for photo
  sources beyond PhotoPrism - each immediate subfolder becomes a source.
- A Seeed reTerminal E1002, connected via USB for the first flash.
