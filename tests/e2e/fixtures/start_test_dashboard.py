"""Launch the dashboard in test mode for Playwright E2E tests.

Calls create_app() directly (not main()), so no SSH tunnels, no
background pollers, and no station connectivity required. Station API
routes will return error/offline responses, which the frontend handles
gracefully.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent
PROJECT = FIXTURES.parent.parent.parent

tmp = Path(tempfile.mkdtemp(prefix="rovimen_e2e_"))

os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
os.environ["ROVIMEN_SECRET_KEY"] = "e2e-test-secret-key-not-for-production"
os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
os.environ["RATELIMIT_ENABLED"] = "0"
os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
os.environ["ROVIMEN_FFTHUMB_DIR"] = str(tmp / "ffthumbs")
os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
os.environ["ROVIMEN_FIREBALL_DB"] = str(tmp / "cache" / "fireball_events.db")
# Keep the docstring's promise: build the routes/caches but start none of the
# long-lived poller/scheduler daemons (no SSH tunnels, no station reach needed).
os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"

# ---------------------------------------------------------------------------
# Production-path sandbox
# ---------------------------------------------------------------------------
# Every dashboard module resolves its paths from the environment AT IMPORT TIME,
# falling back to the LIVE VPS locations (/opt/rovimen/... and the Storage Box
# mount at /srv/rovimen/archive). Anything left unset here is not "unused" —
# it is pointed at production, so on a box where the Storage Box is mounted the
# E2E suite reads (and for the writable stores, writes) real fleet data. Pin
# every one of them into this run's temp dir. Keep this list in sync with
# `grep -rn 'os.environ' dashboard/` whenever a new path env var lands.
_ARCHIVE = tmp / "archive"
os.environ["ROVIMEN_ARCHIVE_PATH"] = str(_ARCHIVE)            # Storage Box mount
os.environ["THUMB_ARCHIVE_DIR"] = str(tmp / "thumb_archive")  # else ARCHIVE/.thumb_cache
os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")
os.environ["ROVIMEN_BASKET_PATH"] = str(tmp / "download_baskets.json")
os.environ["ROVIMEN_KEPT_CLIPS_PATH"] = str(tmp / "kept_clips.jsonl")
os.environ["ROVIMEN_HIDDEN_DETECTIONS_DB"] = str(tmp / "hidden_detections.db")
os.environ["ROVIMEN_COMPILATIONS_PATH"] = str(tmp / "compilations.json")
os.environ["ROVIMEN_PLATEPAR_STORE"] = str(tmp / "platepar_store.json")
os.environ["ROVIMEN_PUSHED_STATE_DIR"] = str(tmp / "pushed_state")
os.environ["ROVIMEN_STATION_EVENTS_DB"] = str(tmp / "pushed_state" / "station_events.db")
os.environ["ROVIMEN_API_KEYS_PATH"] = str(tmp / "api_keys.yaml")
os.environ["ROVIMEN_STATION_KEYS_PATH"] = str(tmp / "station_keys.yaml")
os.environ["ROVIMEN_SECRET_KEY_FILE"] = str(tmp / "secret.key")
os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
os.environ["ROVIMEN_STATION_ORBITS_HISTORY"] = "0"  # no yearly GMN downloads
os.environ["ROVIMEN_INTRO_PATH"] = str(tmp / "intro.mp4")
os.environ["ROVIMEN_MP4_CACHE_DIR"] = str(tmp / "mp4_cache")
os.environ["ROVIMEN_CLIP_CACHE_DIR"] = str(tmp / "clip_cache")
os.environ["ROVIMEN_BLOB_SPOOL_DIR"] = str(tmp / "blob_spool")
os.environ["ROVIMEN_RENDER_LEADER_LOCK"] = str(tmp / "render_leader.lock")
os.environ["ROVIMEN_BUNDLE_DIR"] = str(tmp / "bundle_root")
os.environ["ROVIMEN_YOUTUBE_TOKEN"] = str(tmp / "youtube_token.json")
os.environ["ROVIMEN_YOUTUBE_CLIENT_SECRET"] = str(tmp / "youtube_client_secret.json")
os.environ["ROVIMEN_NIGHTLY_STATE_DIR"] = str(tmp / "nightly_state")
# Fireball alert settings (alert_channels.load_settings): never the VPS's
# /etc files. The fake SMTP account only makes /alerts show its email form;
# the spec intercepts the subscribe call, so nothing is ever sent.
(tmp / "fireball.env").write_text(
    "ROVIMEN_FIREBALL_TELEGRAM_JOIN_URL=https://t.me/+e2e\n"
    "ROVIMEN_FIREBALL_DISCORD_INVITE_URL=https://discord.gg/e2e\n"
    "ROVIMEN_SMTP_USER=e2e@example.org\nROVIMEN_SMTP_PASSWORD=not-a-password\n")
os.environ["ROVIMEN_FIREBALL_ENV"] = str(tmp / "fireball.env")
os.environ["ROVIMEN_MAIL_ENV"] = str(tmp / "no-mail.env")
# The index poller and the nightly report resolve the station registry from the
# environment before falling back to dashboard/dashboard_config.yaml (the REAL
# fleet). Pin both names at the fixture registry.
os.environ["ROVIMEN_CONFIG"] = str(FIXTURES / "dashboard_config.yaml")
os.environ["ROVIMEN_DASHBOARD_CONFIG"] = str(FIXTURES / "dashboard_config.yaml")

# Outbound side effects: a stray token here would let the test process push to
# the real repo, talk to a real Redis/Cloudflare Access tenant, or emit public
# URLs pointing at the live host. Clear them rather than trusting the shell.
for _leak in (
    "GITHUB_TOKEN",
    "ROVIMEN_REDIS_URL",
    "ROVIMEN_CF_TEAM_DOMAIN",
    "ROVIMEN_CF_AUD",
    "ROVIMEN_PUBLIC_BASE_URL",
    "ROVIMEN_TELEGRAM_BOT_TOKEN",
    "ROVIMEN_TELEGRAM_CHAT_ID",
    "ROVIMEN_TELEGRAM_RO_CHAT_ID",
    "ROVIMEN_NETWORK_STATS_CACHE",
):
    os.environ.pop(_leak, None)

# The Storage-Box concurrency guard exists to keep a fan-out burst from piling
# up on one SSHFS mount; on contention it SHEDS load by returning the caller's
# default. There is no mount here (the archive is a local temp dir), so a shed
# would only turn Playwright's parallelism into nondeterministic responses.
os.environ["ROVIMEN_SSHFS_MAX_CONCURRENCY"] = "64"

_ARCHIVE.mkdir(parents=True, exist_ok=True)

# Seed one hall-of-fame entry so the landing-page section and the admin
# manager have something to render. Set before the dashboard import:
# hall_of_fame_store resolves its path at module import. The referenced
# clip/stack files don't exist on disk — the section only needs the JSON
# list; media playback isn't exercised in E2E.
_hof_path = tmp / "hall_of_fame.json"
_hof_path.write_text(json.dumps([{
    "id": "hof_e2e000000001",
    "camera": "TST001",
    "date": "2026-01-01",
    "filename": "TST001_20260101_220000_color.mkv",
    "stack": "TST001_20260101_220000_stack.webp",
    "caption": "E2E featured fireball",
    "period": "2026-01",
    "order": 1,
    "shower": "PER",
    "peak_magnitude": -4.2,
    "duration_s": 2.4,
    "time_utc": "2026-01-01T22:00:05",
    "station_label": "Test Station One",
    "detection_offset_s": 5.0,
    "added_by": "admin_test",
    "added_at": "2026-01-02T00:00:00+00:00",
}]))
os.environ["ROVIMEN_HOF_PATH"] = str(_hof_path)

sys.path.insert(0, str(PROJECT / "dashboard"))

from rovimen_dashboard import load_config, create_app  # noqa: E402

import security  # noqa: E402

security._LOGIN_THRESHOLD = 10_000
security._LOGIN_LOCKOUT_SECONDS = 0

original_init_limiter = security.init_limiter
def _noop_limiter(app):
    lim = original_init_limiter(app)
    lim.enabled = False
    return lim
security.init_limiter = _noop_limiter

config_path = FIXTURES / "dashboard_config.yaml"
config = load_config(config_path)
app = create_app(config, config_path)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5111, debug=False)
