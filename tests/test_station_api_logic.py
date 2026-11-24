"""Tests for station_api.py logic — chunk-name timestamps, detection offsets,
RMS detection merge.

Chunk filenames are UTC-stamped (color_capture pins TZ=UTC on ffmpeg, #851);
_chunk_utc_dt is the single conversion point and must return the name verbatim
by default, falling back to a per-moment zoneinfo conversion only when
``chunk_names_utc`` is false (pre-#851 local-named artefacts).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import rovimen_night
import rovimen_rms
import station_api


# ── _chunk_utc_dt: chunk filename timestamp interpretation ────────────────


class TestChunkUtcDt:
    """The chunk name is UTC by default; the legacy path (chunk_names_utc
    false) must resolve the system-timezone offset for the chunk's OWN moment,
    not for whenever the API call happens to run."""

    def test_utc_names_returned_verbatim(self):
        """Default knob: the filename already carries UTC — no conversion,
        regardless of what timezone the host runs."""
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Europe/Bucharest")):
            dt = station_api._chunk_utc_dt("20260322", "230000", cfg={})
        assert dt == datetime(2026, 3, 22, 23, 0, 0)

    def test_knob_defaults_to_true_when_absent(self):
        dt = station_api._chunk_utc_dt("20260601", "013000", cfg={"segment_duration": 20})
        assert dt == datetime(2026, 6, 1, 1, 30, 0)

    def test_legacy_summer_name_gets_summer_offset(self):
        """Legacy local name recorded under EEST (UTC+3) converts by 3 h even
        if the test (or the station API) runs in winter."""
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Europe/Bucharest")):
            dt = station_api._chunk_utc_dt(
                "20260601", "230000", cfg={"chunk_names_utc": False})
        assert dt == datetime(2026, 6, 1, 20, 0, 0)

    def test_legacy_winter_name_gets_winter_offset(self):
        """Legacy local name recorded under EET (UTC+2) converts by 2 h.

        Together with the summer case this pins the fix for the old
        _local_to_utc bug: the offset is resolved for the chunk's own
        timestamp, never sampled from time.timezone/altzone at call time."""
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Europe/Bucharest")):
            dt = station_api._chunk_utc_dt(
                "20260122", "230000", cfg={"chunk_names_utc": False})
        assert dt == datetime(2026, 1, 22, 21, 0, 0)

    def test_legacy_dst_boundary_night_both_sides_correct(self):
        """A single night spanning the spring-forward moment (Europe/Bucharest,
        2026-03-29 03:00 EET → 04:00 EEST): chunks before the switch convert
        by +2 h, chunks after by +3 h — per-chunk, in one process."""
        cfg = {"chunk_names_utc": False}
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Europe/Bucharest")):
            before = station_api._chunk_utc_dt("20260329", "023000", cfg=cfg)
            after = station_api._chunk_utc_dt("20260329", "043000", cfg=cfg)
        assert before == datetime(2026, 3, 29, 0, 30, 0)
        assert after == datetime(2026, 3, 29, 1, 30, 0)

    def test_legacy_midnight_crossing_backward(self):
        """Legacy conversion that pulls an after-midnight local name back to
        the previous UTC date."""
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Europe/Bucharest")):
            dt = station_api._chunk_utc_dt(
                "20260122", "003000", cfg={"chunk_names_utc": False})
        assert dt == datetime(2026, 1, 21, 22, 30, 0)

    def test_legacy_on_utc_system_is_identity(self):
        """Safety property: on a station already running Etc/UTC the knob
        value does not matter — offset is zero either way."""
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Etc/UTC")):
            dt = station_api._chunk_utc_dt(
                "20260322", "230000", cfg={"chunk_names_utc": False})
        assert dt == datetime(2026, 3, 22, 23, 0, 0)

    def test_legacy_chunk_tz_overrides_system_tz(self):
        """legacy_chunk_tz wins over the system timezone: artefacts recorded
        under a different TZ than the host now runs stay interpretable after
        the host is switched to Etc/UTC."""
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Etc/UTC")):
            dt = station_api._chunk_utc_dt(
                "20260122", "230000",
                cfg={"chunk_names_utc": False,
                     "legacy_chunk_tz": "Europe/Bucharest"})
        assert dt == datetime(2026, 1, 22, 21, 0, 0)

    def test_legacy_chunk_tz_bogus_falls_back_to_system(self):
        with patch.object(rovimen_night, "_system_zoneinfo",
                          return_value=ZoneInfo("Europe/Bucharest")):
            dt = station_api._chunk_utc_dt(
                "20260122", "230000",
                cfg={"chunk_names_utc": False, "legacy_chunk_tz": "Not/AZone"})
        assert dt == datetime(2026, 1, 22, 21, 0, 0)

    def test_system_zoneinfo_from_tz_env(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Berlin")
        assert station_api._system_zoneinfo() == ZoneInfo("Europe/Berlin")

    def test_system_zoneinfo_bogus_tz_falls_through(self, monkeypatch):
        """An unresolvable TZ value must not raise — the resolver falls back
        to /etc/timezone or /etc/localtime (or None)."""
        monkeypatch.setenv("TZ", "Not/AZone")
        tz = station_api._system_zoneinfo()
        assert tz is None or isinstance(tz, ZoneInfo)


# ── Detection offset calculation ──────────────────────────────────────────


class TestDetectionOffset:
    """_detection_offset_s subtracts the chunk's UTC start (from _chunk_utc_dt)
    from the detection's UTC time as full datetimes, so midnight rollover is
    exact and no timezone mixing can occur."""

    def test_simple_offset_from_meteor_time(self):
        """Meteor at second 5.5 of a 20s chunk → offset 5.5."""
        chunk_dt = datetime(2026, 3, 22, 21, 0, 0)
        off = station_api._detection_offset_s(
            chunk_dt, "2026-03-22T21:00:05.500000", None)
        assert off == pytest.approx(5.5)

    def test_meteor_time_preferred_over_detection_time(self):
        chunk_dt = datetime(2026, 3, 22, 21, 0, 0)
        off = station_api._detection_offset_s(
            chunk_dt, "2026-03-22T21:00:05.500000", "20260322_210012")
        assert off == pytest.approx(5.5)

    def test_midnight_rollover(self):
        """Chunk at 23:59:50, meteor at 00:00:05 the next day → 15 s, computed
        from full dates rather than a ±43200 s seconds-of-day wrap."""
        chunk_dt = datetime(2026, 3, 22, 23, 59, 50)
        off = station_api._detection_offset_s(
            chunk_dt, "2026-03-23T00:00:05", None)
        assert off == pytest.approx(15.0)

    def test_midnight_rollover_detection_time_fallback(self):
        chunk_dt = datetime(2026, 3, 22, 23, 59, 50)
        off = station_api._detection_offset_s(chunk_dt, None, "20260323_000005")
        assert off == pytest.approx(15.0)

    def test_aware_meteor_time_normalized_to_utc(self):
        """An offset-aware meteor_time converts to UTC before subtraction."""
        chunk_dt = datetime(2026, 3, 22, 21, 0, 0)
        off = station_api._detection_offset_s(
            chunk_dt, "2026-03-22T23:00:05+02:00", None)
        assert off == pytest.approx(5.0)

    def test_spillover_from_previous_chunk_clamps_to_zero(self):
        """A detection 2 s before this chunk's start (stitch spill from the
        previous chunk) seeks to 0, not to a negative offset."""
        chunk_dt = datetime(2026, 3, 22, 21, 0, 10)
        off = station_api._detection_offset_s(
            chunk_dt, "2026-03-22T21:00:08", None)
        assert off == 0.0

    def test_time_only_detection_time_anchors_to_chunk_date(self):
        """Legacy sidecars carry only HHMMSS; it is anchored to the chunk's
        UTC date, including across midnight."""
        chunk_dt = datetime(2026, 3, 22, 23, 59, 50)
        off = station_api._detection_offset_s(chunk_dt, None, "000005")
        assert off == pytest.approx(15.0)

    def test_no_detection_info_returns_none(self):
        chunk_dt = datetime(2026, 3, 22, 21, 0, 0)
        assert station_api._detection_offset_s(chunk_dt, None, None) is None
        assert station_api._detection_offset_s(chunk_dt, None, "bogus") is None

    def test_utc_chunk_name_no_timezone_skew(self):
        """The old bug: a local-named chunk (23:00 EET) compared seconds-of-day
        against a UTC meteor_time and clamped -7195 s to 0. With UTC names the
        same physical chunk enters as 21:00 UTC and the offset is exact."""
        chunk_dt = station_api._chunk_utc_dt("20260322", "210000", cfg={})
        off = station_api._detection_offset_s(
            chunk_dt, "2026-03-22T21:00:05", None)
        assert off == pytest.approx(5.0)


# ── P0 #2: RMS detection merge — time-join (fixed) ────────────────────────
#
# The old index-join is replaced by a nearest-begin-time join matching
# rms_parse.parse_session_detections.  The tests below run the parser and
# join api_rms_detections uses (rovimen_rms.session_detections) over
# synthetic on-disk session fixtures, so the pairing is asserted end-to-end.

# FTPdetectinfo with 3 meteors at 20:00:00 (uncalibrated), 20:00:05, 20:05:10.
# Each FF filename encodes the block start; frame 0 at 25 fps adds 0 s offset.
_FTP_3 = """\
Meteor Count = 000003
-------------------------------------------------------------
FF_RO000H_20260314_200000_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 5.50 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 5.20 0 10 0
-------------------------------------------------------------
FF_RO000H_20260314_200005_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 -2.00 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 -1.50 0 10 0
-------------------------------------------------------------
FF_RO000H_20260314_200510_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 1.80 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 2.00 0 10 0
"""

# Radiants for only the 2nd and 3rd meteors (1st was filtered as uncalibrated).
_RAD_2 = """\
# RMS single station association
20260314 20:00:05.000000, 2461100.000000, 95.0, PER, 10.0, 20.0, 11.0, 21.0, None, None, 0,0,0,0, -2.00, -3.50, 45.0
20260314 20:05:10.000000, 2461100.100000, 95.1, SPO, 12.0, 22.0, 13.0, 23.0, None, None, 0,0,0,0,  1.80, None, 40.0
"""


class TestRmsDetectionTimeJoin:
    """api_rms_detections joins radiants to FTP by nearest begin-time (within
    rovimen_rms.JOIN_TOLERANCE_S) instead of list index, per session, through
    rovimen_rms.session_detections (the indexer's parser and join)."""

    def _session(self, tmp_path: Path, ftp_text: str) -> Path:
        session = tmp_path / "RO000H_20260314_180000_000000"
        session.mkdir()
        (session / "FTPdetectinfo_RO000H_20260314.txt").write_text(ftp_text)
        (session / "RO000H_20260314_180000_000000_radiants.txt").write_text(_RAD_2)
        return session

    def _run_join(self, tmp_path: Path) -> list[dict]:
        return rovimen_rms.session_detections(self._session(tmp_path, _FTP_3))

    def test_mismatched_counts_correct_pairing(self, tmp_path: Path):
        """FTP has 3 entries; radiants has 2 (first FTP entry filtered out).
        Time-join must attach PER to the 2nd FTP entry and SPO to the 3rd,
        NOT PER to index 0 (the uncalibrated detection)."""
        detections = self._run_join(tmp_path)
        assert len(detections) == 3

        by_ff = {d["ff_file"]: d for d in detections}

        # First FTP entry (no radiant partner) must NOT inherit a shower.
        uncal = by_ff["FF_RO000H_20260314_200000_000_0000000.fits"]
        assert uncal["shower"] is None, (
            "uncalibrated meteor must not be assigned a shower from radiants"
        )
        assert uncal["mag_apparent"] == pytest.approx(5.20)

        # Second FTP entry (begin 20:00:05) matches PER radiant.
        per = by_ff["FF_RO000H_20260314_200005_000_0000000.fits"]
        assert per["shower"] == "PER"
        assert per["mag_apparent"] == pytest.approx(-2.00)
        assert per["mag_absolute"] == pytest.approx(-3.50)

        # Third FTP entry (begin 20:05:10) matches SPO radiant.
        spo = by_ff["FF_RO000H_20260314_200510_000_0000000.fits"]
        assert spo["shower"] == "SPO"
        assert spo["mag_apparent"] == pytest.approx(1.80)

    def test_begin_dt_not_leaked_into_detection(self, tmp_path: Path):
        """_begin_dt is an internal join key and must not appear in the output."""
        detections = self._run_join(tmp_path)
        for det in detections:
            assert "_begin_dt" not in det

    def test_equal_counts_still_correct(self, tmp_path: Path):
        """When FTP and radiants counts match, time-join gives the same result as
        index-join would for perfectly aligned lists."""
        ftp_2 = """\
Meteor Count = 000002
-------------------------------------------------------------
FF_RO000H_20260314_200005_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 -2.00 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 -1.50 0 10 0
-------------------------------------------------------------
FF_RO000H_20260314_200510_000_0000000.fits
RO000H 0001 0002 25.00 0 0 0 0 0 0
0000.0 100.0 100.0 10.0 5.0 130.0 45.0 100 1.80 0 10 0
0025.0 110.0 110.0 10.0 5.0 131.0 46.0 100 2.00 0 10 0
"""
        detections = rovimen_rms.session_detections(self._session(tmp_path, ftp_2))
        assert len(detections) == 2
        paired = {d["ff_file"]: d["shower"] for d in detections}
        assert paired["FF_RO000H_20260314_200005_000_0000000.fits"] == "PER"
        assert paired["FF_RO000H_20260314_200510_000_0000000.fits"] == "SPO"


# ── Live-capture: never serve the segment ffmpeg is still writing ─────────


class TestSegmentInProgress:
    """_segment_in_progress guards /api/chunks from returning the currently
    growing colour segment as a truncated 1-7 s clip during the live night.

    During capture ffmpeg writes each 20 s segment straight to its final
    filename, so the newest file in a night dir keeps growing until the next
    segment starts.  There is no state.json yet (it is written at dawn), so the
    endpoint would otherwise offer the half-written newest file for playback.
    """

    NEWEST = "MD0001_20260715_194719_color.mkv"
    OLDER = "MD0001_20260715_194659_color.mkv"

    def test_newest_recently_written_is_in_progress(self):
        now = 1_000_000.0
        assert station_api._segment_in_progress(
            self.NEWEST, self.NEWEST, mtime=now - 3, now=now, seg_dur=20) is True

    def test_older_segment_never_in_progress(self):
        now = 1_000_000.0
        # An older segment is closed even if just touched — a newer one exists.
        assert station_api._segment_in_progress(
            self.OLDER, self.NEWEST, mtime=now - 1, now=now, seg_dur=20) is False

    def test_last_clip_of_finished_night_ages_out(self):
        """Once capture stops, the newest file stops growing; after one segment
        window (+grace) it is finalized and must be shown."""
        now = 1_000_000.0
        stale = now - (20 + station_api._INPROGRESS_GRACE_S + 5)
        assert station_api._segment_in_progress(
            self.NEWEST, self.NEWEST, mtime=stale, now=now, seg_dur=20) is False

    def test_boundary_just_inside_window_is_in_progress(self):
        now = 1_000_000.0
        just_inside = now - (20 + station_api._INPROGRESS_GRACE_S - 1)
        assert station_api._segment_in_progress(
            self.NEWEST, self.NEWEST, mtime=just_inside, now=now, seg_dur=20) is True

    def test_empty_dir_no_newest(self):
        now = 1_000_000.0
        assert station_api._segment_in_progress(
            self.NEWEST, "", mtime=now, now=now, seg_dur=20) is False


class TestRmsCamerasLayouts:
    """/api/rms_cameras must discover single-camera Pis that run RMS straight
    from ~/source/RMS/.config (gmnro16/gmnro18). Without Layout 3 it returns []
    and the pusher never sends a live thumbnail for the station."""

    def test_single_cam_source_layout_discovered(self, tmp_path, monkeypatch, station_patch):
        rms_dir = tmp_path / "source" / "RMS"
        rms_dir.mkdir(parents=True)
        (rms_dir / ".config").write_text(
            "stationID: RO001E\n"
            "device: rtsp://admin:@192.168.4.220:554/user=admin&stream=0.sdp\n"
        )
        station_patch("BASE", tmp_path)
        resp = station_api.app.test_client().get("/api/rms_cameras")
        assert resp.status_code == 200
        data = resp.get_json()
        codes = [e["code"] for e in data]
        assert "RO001E" in codes
        entry = next(e for e in data if e["code"] == "RO001E")
        assert entry["cam_ip"] == "192.168.4.220"

    def test_source_layout_not_double_counted_with_stations_symlink(self, tmp_path, monkeypatch, station_patch):
        # If both ~/source/Stations/RO001E/.config and ~/source/RMS/.config
        # resolve to the same station (the interim symlink workaround), the code
        # appears exactly once.
        rms_dir = tmp_path / "source" / "RMS"
        rms_dir.mkdir(parents=True)
        cfg = rms_dir / ".config"
        cfg.write_text("stationID: RO001E\ndevice: rtsp://192.168.4.220:554/x\n")
        st_dir = tmp_path / "source" / "Stations" / "RO001E"
        st_dir.mkdir(parents=True)
        (st_dir / ".config").symlink_to(cfg)
        station_patch("BASE", tmp_path)
        data = station_api.app.test_client().get("/api/rms_cameras").get_json()
        assert [e["code"] for e in data].count("RO001E") == 1

    def test_no_cameras_when_no_config(self, tmp_path, monkeypatch, station_patch):
        station_patch("BASE", tmp_path)
        data = station_api.app.test_client().get("/api/rms_cameras").get_json()
        assert data == []
