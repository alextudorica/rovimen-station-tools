# Station deployment tooling

Setup and signing tooling for ROVIMEN / GMN meteor stations. A new station
is set up with the maintainers' station onboarding runbook (not published);
the Clonezilla golden-master workflow and its scripts, and the self-extracting
interactive installer and its build script, were removed in 2026-11 and are in
git history.

## Hardware

- **Target**: HP EliteDesk 800 G4 (or similar)
- **RAM**: 16GB (fleet baseline; 8GB boxes work but leave no headroom)
- **Storage**: NVMe/SATA SSD
- **OS**: Ubuntu Server 24.04 LTS

## Directory Structure

```
deployment/
├── README.md                # This file
├── sign_bundle.py           # CI signer for station update bundles
├── bundle_signing_pub.pem   # Public key the stations verify bundles with
├── scripts/
│   └── install_station.sh   # OS + RMS base install (packages, time sync, swap, RMS)
└── docs/                    # Camera network guide
```

## Network Services

| Service    | Port  | Purpose |
|------------|-------|---------|
| SSH        | 22    | Remote access |
| Cockpit    | 9090  | Web dashboard (HTTPS) |
| Tailscale  | -     | VPN mesh network |

## Key Paths

| Path | Purpose |
|------|---------|
| `~/RMS` | RMS software installation |
| `~/RMS/venv` | Python virtual environment |
| `~/RMS/.config` | Station-specific camera config |

## Maintenance Commands

```bash
# Update RMS on a station
cd ~/RMS && git pull
source venv/bin/activate
pip install -r requirements.txt

# Check time sync
chronyc tracking

# Check Tailscale status
tailscale status

```

## Troubleshooting

### Station not reachable via SSH
```bash
# Check if online
ping <station-ip>

# If SSH key changed warning
ssh-keygen -R <station-ip>
```

### Time sync issues
```bash
sudo chronyc makestep    # Force immediate sync
chronyc sources -v       # Check time sources
```

### RMS Python issues
```bash
cd ~/RMS
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
