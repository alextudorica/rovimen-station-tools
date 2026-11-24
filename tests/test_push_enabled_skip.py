"""Tests for the per-station push_enabled poll opt-out.

push_enabled is the poll→push cutover switch (reversed-HTTP push rollout).
A station with push_enabled=True is fed by the ingest API, so the dashboard's
server-side pollers must SKIP it:

  * station_client.start_polling status + vitals fan-out
  * index_poller._load_stations (detection-index poll)

Everything defaults to False, so normal stations keep polling exactly as
today — the flag is a conservative, additive per-station opt-out.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import station_client
import index_poller
from models import CameraConfig, StationConfig, DashboardConfig


# ── Helpers ─────────────────────────────────────────────────────────────────

def _capture_poll_targets(config, cache):
    """Run start_polling but capture the poll_status/poll_vitals closures
    instead of spawning real threads. Returns (poll_status_fn, poll_vitals_fn).
    """
    tunnels = MagicMock()
    ran_target: list = []

    def capture_thread(*args, **kwargs):
        target = kwargs.get("target") or (args[0] if args else None)
        ran_target.append(target)
        return MagicMock()

    with patch("station_client.threading") as mock_threading:
        mock_threading.Thread.side_effect = capture_thread
        station_client.start_polling(config, tunnels, cache)

    assert len(ran_target) == 2, "expected poll_status and poll_vitals threads"
    return ran_target[0], ran_target[1]


def _run_once(poll_fn) -> None:
    """Drive a poll closure through exactly one loop iteration by making
    time.sleep raise to break out of the `while True` loop."""
    def fake_sleep(_seconds):
        raise StopIteration("done")

    with patch("station_client.time") as mock_time:
        mock_time.sleep.side_effect = fake_sleep
        with pytest.raises(StopIteration):
            poll_fn()


# ── station_client status/vitals poll skip ────────────────────────────────────

class TestStatusVitalsPollSkip:
    def _config(self) -> DashboardConfig:
        return DashboardConfig(
            stations={
                "normal": StationConfig(ip="100.0.0.1", label="Normal"),
                "pusher": StationConfig(
                    ip="100.0.0.2", label="Pusher", push_enabled=True
                ),
            }
        )

    def test_status_poll_skips_push_enabled_station(self):
        config = self._config()
        cache = MagicMock()
        cache.get_status.return_value = None
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"online": True}

        poll_status_fn, _ = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_status_fn)

        assert "normal" in polled, "normal station must still be polled"
        assert "pusher" not in polled, "push_enabled station must be skipped"
        # The push station's cache entry is left untouched (ingest owns it).
        set_keys = [c.args[0] for c in cache.set_status.call_args_list]
        assert "pusher" not in set_keys

    def test_vitals_poll_skips_push_enabled_station(self):
        config = self._config()
        cache = MagicMock()
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"cpu": 1}

        _, poll_vitals_fn = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_vitals_fn)

        assert "normal" in polled
        assert "pusher" not in polled
        set_keys = [c.args[0] for c in cache.set_vitals.call_args_list]
        assert "pusher" not in set_keys

    def test_normal_station_polled_when_no_flag(self):
        """Default False: a station without push_enabled is polled as today."""
        config = DashboardConfig(
            stations={"normal": StationConfig(ip="100.0.0.1", label="Normal")}
        )
        cache = MagicMock()
        cache.get_status.return_value = None
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"online": True}

        poll_status_fn, _ = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_status_fn)

        assert polled == ["normal"]


# ── Push-station staleness demotion ──────────────────────────────────────────

class TestPushStalenessDemotion:
    """A dead push station must not stay 'online' forever.

    Ingest only writes the cache on ARRIVING pushes, so the status cycle
    demotes a push station's cached entry to offline once it is older than
    PUSH_STALE_SECS (station_client._demote_stale_push).
    """

    def _config(self) -> DashboardConfig:
        return DashboardConfig(
            stations={
                "normal": StationConfig(ip="100.0.0.1", label="Normal"),
                "pusher": StationConfig(
                    ip="100.0.0.2", label="Pusher", push_enabled=True
                ),
            }
        )

    def _cached(self, cache, pusher_status):
        cache.get_status.side_effect = (
            lambda key: pusher_status if key == "pusher" else None
        )

    def _stamp(self, seconds_ago: float) -> str:
        from datetime import datetime, timedelta, timezone
        return (
            datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        ).isoformat()

    def _drive_status_cycle(self, cache):
        config = self._config()
        poll_status_fn, _ = _capture_poll_targets(config, cache)
        with patch(
            "station_client.station_get_status",
            side_effect=lambda *a, **k: {"online": True},
        ):
            _run_once(poll_status_fn)

    def test_stale_online_push_station_demoted(self):
        cache = MagicMock()
        self._cached(cache, {"online": True, "received_at": self._stamp(1200)})
        self._drive_status_cycle(cache)

        pusher_writes = [
            c.args for c in cache.set_status.call_args_list if c.args[0] == "pusher"
        ]
        assert len(pusher_writes) == 1, "stale push station must be demoted once"
        demoted = pusher_writes[0][1]
        assert demoted["online"] is False
        assert demoted["stale"] is True
        assert "no push received since" in demoted["error"]
        # Last-seen timestamp preserved for the UI.
        assert demoted["received_at"]

    def test_fresh_push_station_left_alone(self):
        cache = MagicMock()
        self._cached(cache, {"online": True, "received_at": self._stamp(10)})
        self._drive_status_cycle(cache)
        set_keys = [c.args[0] for c in cache.set_status.call_args_list]
        assert "pusher" not in set_keys, "fresh push status must not be rewritten"

    def test_already_offline_push_station_not_rewritten(self):
        """Demotion is one-shot: an already-offline entry is never rewritten,
        so the SSE diff/fan-out broadcasts the transition exactly once."""
        cache = MagicMock()
        self._cached(
            cache,
            {"online": False, "stale": True, "received_at": self._stamp(9000)},
        )
        self._drive_status_cycle(cache)
        set_keys = [c.args[0] for c in cache.set_status.call_args_list]
        assert "pusher" not in set_keys

    def test_last_updated_fallback_for_pre_received_at_entries(self):
        """Entries cached before ingest stamped received_at still age out via
        the station-stamped last_updated."""
        cache = MagicMock()
        self._cached(cache, {"online": True, "last_updated": self._stamp(1200)})
        self._drive_status_cycle(cache)
        pusher_writes = [
            c.args for c in cache.set_status.call_args_list if c.args[0] == "pusher"
        ]
        assert len(pusher_writes) == 1
        assert pusher_writes[0][1]["online"] is False

    def test_no_timestamp_means_no_demotion(self):
        """A snapshot with no usable timestamp can't be judged stale — leave it
        (fail-safe: never flip a station offline on missing data)."""
        cache = MagicMock()
        self._cached(cache, {"online": True})
        self._drive_status_cycle(cache)
        set_keys = [c.args[0] for c in cache.set_status.call_args_list]
        assert "pusher" not in set_keys


class TestPushStatusAge:
    def test_prefers_received_at_over_last_updated(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        age = station_client._push_status_age_seconds({
            "received_at": (now - timedelta(seconds=60)).isoformat(),
            "last_updated": (now - timedelta(seconds=99999)).isoformat(),
        })
        assert age is not None and 55 < age < 65

    def test_handles_z_suffix(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = station_client._push_status_age_seconds({"last_updated": stamp})
        assert age is not None and 115 < age < 125

    def test_missing_or_garbage_timestamp_returns_none(self):
        assert station_client._push_status_age_seconds({}) is None
        assert station_client._push_status_age_seconds(
            {"received_at": "not-a-date"}
        ) is None


# ── index_poller detection-index poll skip ────────────────────────────────────

class TestIndexPollerSkip:
    def _write_config(self, tmp_path, pusher_flag: bool):
        import yaml
        cfg = {
            "station_api_port": 7779,
            "stations": {
                "normal": {
                    "ip": "100.0.0.1",
                    "label": "Normal",
                    "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
                },
                "pusher": {
                    "ip": "100.0.0.2",
                    "label": "Pusher",
                    "push_enabled": pusher_flag,
                    "cameras": [{"code": "RO000B", "cam_ip": "192.168.1.11"}],
                },
            },
        }
        path = tmp_path / "dashboard_config.yaml"
        path.write_text(yaml.safe_dump(cfg))
        return path

    def test_load_stations_skips_push_enabled(self, tmp_path):
        path = self._write_config(tmp_path, pusher_flag=True)
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "pusher" not in stations, "push_enabled station must not be polled"

    def test_load_stations_includes_when_flag_false(self, tmp_path):
        path = self._write_config(tmp_path, pusher_flag=False)
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "pusher" in stations, "default/False keeps the station polled"

    def test_include_push_knob_polls_push_stations(self, tmp_path):
        """ROVIMEN_INDEX_POLL_INCLUDE_PUSH=1 (dev tier): push stations are
        polled too — pushes only reach the prod ingest surface, so polling is
        the dev DB's only feed for them."""
        path = self._write_config(tmp_path, pusher_flag=True)
        with patch.object(index_poller, "CONFIG_PATH", path), \
                patch.object(index_poller, "INCLUDE_PUSH_STATIONS", True):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "pusher" in stations, "include-push knob must poll push stations"


# ── Round-trip through the admin save serializer ─────────────────────────────

class TestPushEnabledRoundTrip:
    @pytest.mark.parametrize("flag", [True, False])
    def test_flag_round_trips_through_an_admin_save(self, flag):
        from config_file import config_to_dict

        cfg = DashboardConfig(stations={"pusher": StationConfig(
            ip="100.0.0.2",
            label="Pusher",
            cameras=[CameraConfig(code="RO000B", cam_ip="192.168.1.11")],
            push_enabled=flag,
        )})
        written = config_to_dict(cfg)["stations"]["pusher"]
        # Written even at the default, so the cutover state is explicit in the file.
        assert written["push_enabled"] is flag
        assert StationConfig.model_validate(written).push_enabled is flag


# ── Failed poll keeps the last successful contact ────────────────────────────

class TestFailedPollKeepsLastContact:
    """last_updated is the last successful contact, shown as "Last contact".

    A failed poll used to stamp now, so gmnro21 (Vârteșcoiu), dark for 5 days,
    read "13s ago" on the overview.
    """

    def _config(self) -> DashboardConfig:
        return DashboardConfig(
            stations={"dead": StationConfig(ip="100.0.0.1", label="Dead")}
        )

    def _poll_failing(self, prev: dict | None) -> list:
        cache = MagicMock()
        cache.get_status.return_value = prev
        poll_status_fn, _ = _capture_poll_targets(self._config(), cache)
        failed = {"online": False, "error": "timeout",
                  "last_updated": "2026-11-18T18:00:00+00:00"}
        with patch("station_client.station_get_status", return_value=dict(failed)):
            _run_once(poll_status_fn)
        return [c.args[1] for c in cache.set_status.call_args_list]

    def test_offline_station_keeps_previous_contact(self):
        prev = {"online": False, "last_updated": "2026-11-13T09:11:47+00:00"}
        written = self._poll_failing(prev)
        assert written and written[-1]["last_updated"] == "2026-11-13T09:11:47+00:00"
        assert written[-1]["online"] is False

    def test_never_reached_station_has_no_contact(self):
        written = self._poll_failing(None)
        assert written and written[-1]["last_updated"] is None
