# ROVIMEN Station Tools

Open tooling for [Global Meteor Network](https://globalmeteornetwork.org/) (GMN)
camera stations: video capture, colour calibration, night processing, and a
fleet-monitoring dashboard. Built and run for a real multi-station meteor camera
network (25+ stations in three countries, consumer ISPs behind CGNAT), and usable
as a reference by any operator running GMN/RMS stations.

**Status:** public snapshot of tooling developed in a private operational
repository. Live network configuration (station addresses, credentials, the
deployment pipeline) is not included; you configure your own. See
"Configuration" and "About this snapshot" below.

## About ROVIMEN

ROVIMEN, the ROmanian VIdeo MEteor NEtwork, was founded in 2013 by a group of
astronomy enthusiasts to monitor meteor activity over Romania with video
cameras. The network now runs 25+ stations with 60+ cameras across Romania,
Moldova and Germany, contributing nightly observations to the
[Global Meteor Network](https://globalmeteornetwork.org/). This repository
contains the tooling that runs that fleet.

## What's here

| Area | Path | What it does |
|------|------|--------------|
| **Dashboard** | `dashboard/` | Flask app that maps your stations and shows detections, multi-station events, orbits, timelapses and live views. Config-driven: no hard-coded stations. |
| **Public API** | `dashboard/public_api.py` | Read-only JSON (API key) and media surface for embedding data on other websites. See [`docs/public_api.md`](docs/public_api.md). |
| **Station scripts** | `rovimen-scripts/` | Colour capture, calibration, encoding, stacking, dawn processing, the station REST API, the watchdog and the updater. Deployed to each station. |
| **Colour calibration** | `lib/color_calibration.py` | Calibration core shared with the station scripts. |
| **Open hardware** | `GMN_SQR_V1/` | 3D-printable housings for common camera/board combinations (STL). |
| **Deployment** | `deployment/` | OS and RMS base installer, update-bundle signing, camera network guide. |
| **Docs** | `docs/` | [Network setup](docs/networking.md), [public API](docs/public_api.md). |

## Architecture

Three layers: cameras on an isolated LAN, stations and the dashboard server on a
private Tailscale overlay, and only the server facing the public internet.

```
 cameras ──RTSP:554──> station PC ──tailnet:7779──> dashboard server ──TLS──> public
 (isolated LAN,        (station API,                (Flask + reverse proxy,
  no internet)          capture, upload)             media archive)
```

- The dashboard reads station metadata from `dashboard_config.yaml`. Uploaded
  products (stacks, clips, timelapses, plots) are served from the media
  archive; live calls to a station's REST API (`rovimen-scripts/station_api.py`,
  port 7779) remain only for operator actions and live video. Adding a station
  is one YAML entry.
- Stations need no port forwarding, public IP or DDNS: everything rides the
  Tailscale overlay, which traverses CGNAT.
- Cameras get no default gateway, so camera firmware cannot reach the internet.

Read [`docs/networking.md`](docs/networking.md) before deploying. It covers the
network composition, a sample Tailscale ACL policy, the port map, firewall
rules and camera LAN conventions.

## Quick start (dashboard)

```bash
uv sync                                   # Python deps (uv)
cp dashboard/dashboard_config.example.yaml dashboard/dashboard_config.yaml
$EDITOR dashboard/dashboard_config.yaml   # add your stations
uv run python dashboard/rovimen_dashboard.py --port 7777
```

The GMN meteor-map overlay marks events seen by Romanian (`RO`-prefixed) camera
codes as the network's own; another network adapts this in
`dashboard/gmn_poller.py` and `dashboard/gmn_data.py`.

## Station install

1. Bring up the camera LAN per
   [`deployment/docs/CAMERA_NETWORK_SETUP.md`](deployment/docs/CAMERA_NETWORK_SETUP.md)
   (camera addressing, RTSP and decoder settings), and see
   [`docs/networking.md`](docs/networking.md) for the camera LAN conventions.
2. Prepare the OS and RMS with
   [`deployment/scripts/install_station.sh`](deployment/scripts/install_station.sh)
   (Ubuntu Server 24.04: packages, time sync, swap, watchdog, Tailscale, RMS).
   Read it first: it is written for a two-NIC box and puts a camera-LAN address
   and a DHCP server on the first Ethernet interface, which on a single-NIC
   machine is your home network.
3. Join the station to your tailnet with a tagged auth key
   (see [`docs/networking.md`](docs/networking.md)).
4. Install the station scripts. In production they ship as a signed bundle:
   your server serves `rovimen-scripts/` from `/opt/rovimen/station-bundle`,
   signed with [`deployment/sign_bundle.py`](deployment/sign_bundle.py) and your
   own key (replace `deployment/bundle_signing_pub.pem`); each station's
   [`updater.sh`](rovimen-scripts/updater.sh) pulls it over SSH from the host in
   its `config.json` (`vps_host`) and verifies it with `bundle_verify.py`. The
   station's `~/rovimen_scripts/config.json` starts from
   [`config_defaults.json`](rovimen-scripts/config_defaults.json); change it
   later with `config_patch.py`.
5. Add the station to `dashboard_config.yaml`.

The maintainers' end-to-end onboarding runbook is tied to their own
infrastructure and is not part of this snapshot.

## Configuration

- **`dashboard/dashboard_config.yaml`**: your station registry, copied from the
  `.example.yaml`. It is gitignored; never commit real IPs, SSH users or camera
  credentials.
- **`rovimen-scripts/config_defaults.json`**: station defaults; set the archive
  host and path for your own upload target.

## When things break

Start with [`docs/networking.md`](docs/networking.md), section "Frequent
problems" (station offline, nightly frame-drop bursts, camera unreachable,
upload bans). The two that hit everyone:

- **Frame drops at the same time every night**: the camera firmware's
  AutoReboot firing mid-capture on a drifted clock. Set it to a daytime hour.
- **Station "offline" but the PC is up**: check `tailscale status` and the ACLs
  first.

## Tests

```bash
npm ci && npm test                                           # JS unit tests (vitest)
uv run pytest tests/ -n auto --dist loadfile                 # Python tests
npx playwright test --config tests/e2e/playwright.config.js  # E2E (browser)
```

## About this snapshot

- **Scrubbed.** Credentials, server and account names, access paths and
  internal documents are left out. Station coordinates in comments, tests and
  fixtures are replaced with synthetic values, so a handful of tests whose
  expected values were computed at the real positions are marked as skipped.
- **References that lead nowhere.** Comments cite issue and PR numbers, design
  notes and an incident log from the private development repository; those are
  not published.
- **History.** Each public release is a curated snapshot, not the development
  history.

## Security

See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability. Please do
not open public issues for security problems.

## Maintainers

- Alex Tudorica ([@alextudorica](https://github.com/alextudorica))
- Florin Dumitrescu ([@florindumitrescu94](https://github.com/florindumitrescu94))

## License

[GPL-3.0](LICENSE), matching the RMS/GMN ecosystem.
