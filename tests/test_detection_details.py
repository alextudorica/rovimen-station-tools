"""Tests for full-measurement association on RMS detections (issue #947).

Three concerns are covered:

1. **No silent nulls.** A detection RMS actually measured must never come back
   with a null science field that its FTPdetectinfo/radiants source carried.
   ``rms_parse.missing_source_fields`` / ``audit_session_coverage`` flag any
   regression, and the FTP astrometry fallback fills begin/end RA-Dec when the
   detection was filtered out of the radiants file.

2. **Single → multi-station join.** ``gmn_data.match_trajectory`` /
   ``trajectory_for_detection`` attach the geocentric radiant, Vg and orbit to a
   detection that participated in a triangulated GMN trajectory (match on camera
   code + begin time), and return ``None`` (keep single-station values) when it
   did not.

3. **Provenance labelling.** The public projection sets ``provenance`` to
   ``multi_station`` with a ``trajectory`` block when the join hit, else
   ``single_station`` with ``trajectory: None``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import gmn_data
import rms_parse


# ── FTP astrometry fallback + coverage check ──────────────────────────────────
#
# Two FTP detections, one radiants row. The radiants row matches the SECOND
# FTP block by begin time; the first (uncalibrated, dropped from radiants) must
# still get begin/end RA-Dec from its own FTPdetectinfo frames — those columns
# are present in the frame data regardless of the radiants association.

# Frame columns: frame col row RA Dec azim elev inten mag ...
_FTP_TWO = """\
Meteor Count = 000002
-----------------------------------------------------
FF_RO000H_20260314_200000_000_0000000.fits
RO000H 0001 0002 25.00
0000.0 100.0 100.0 33.5 12.0 130.0 45.0 100 5.50 0 10 0
0025.0 110.0 110.0 34.0 12.5 131.0 46.0 100 5.20 0 10 0
-----------------------------------------------------
FF_RO000H_20260314_200005_000_0000000.fits
RO000H 0001 0002 25.00
0000.0 100.0 100.0 88.0 20.0 130.0 45.0 100 -2.00 0 10 0
0025.0 110.0 110.0 89.0 21.0 131.0 46.0 100 -1.50 0 10 0
"""

# One radiants row lining up with the SECOND FTP block (begin 20:00:05).
_RAD_ONE = """\
# RMS single station association
20260314 20:00:05.000000, 2461100.000000, 95.0, PER, 91.0, 22.0, 92.0, 23.0, 200.0, 55.0, 0,0,0,0, -2.00, -3.50, 45.0
"""


@pytest.fixture
def session_partial_radiants(tmp_path: Path) -> Path:
    d = tmp_path / "RO000H_20260314_180000_000000"
    d.mkdir()
    (d / "FTPdetectinfo_RO000H_20260314.txt").write_text(_FTP_TWO)
    (d / "RO000H_20260314_180000_000000_radiants.txt").write_text(_RAD_ONE)
    return d


def test_ftp_only_detection_keeps_begin_end_radec(session_partial_radiants: Path):
    """A detection filtered out of radiants must still carry begin/end RA-Dec —
    the FTPdetectinfo frames hold them, so the merged row must not be null."""
    rows = {r["ff_file"]: r for r in rms_parse.parse_session_detections(session_partial_radiants)}
    uncal = rows["FF_RO000H_20260314_200000_000_0000000.fits"]
    assert uncal["shower"] is None                       # no radiants partner
    assert uncal["ra_beg"] == pytest.approx(33.5)         # from FTP frame 0
    assert uncal["dec_beg"] == pytest.approx(12.0)
    assert uncal["ra_end"] == pytest.approx(34.0)         # from FTP last frame
    assert uncal["dec_end"] == pytest.approx(12.5)


def test_radiants_radec_wins_over_ftp_when_matched(session_partial_radiants: Path):
    """When radiants matched, its (more precise) begin/end RA-Dec is used, not
    the raw FTP frame values."""
    rows = {r["ff_file"]: r for r in rms_parse.parse_session_detections(session_partial_radiants)}
    per = rows["FF_RO000H_20260314_200005_000_0000000.fits"]
    assert per["shower"] == "PER"
    assert per["ra_beg"] == pytest.approx(91.0)   # radiants value, not FTP 88.0
    assert per["ra_radiant"] == pytest.approx(200.0)


def test_coverage_check_clean_session_has_no_findings(session_partial_radiants: Path):
    """Both detections now carry everything their sources hold — no findings."""
    assert rms_parse.audit_session_coverage(session_partial_radiants) == []


def test_missing_source_fields_flags_dropped_value():
    """A measured row that dropped a value its source held must be flagged."""
    ftp = {"ra_beg": 12.3, "dec_beg": 4.5, "duration_s": 1.0, "peak_mag": 2.0}
    rad = {"shower": "PER", "mag_absolute": 1.5}
    # Simulate a buggy merge that dropped ra_beg (present in FTP) and shower.
    row = {"ra_beg": None, "dec_beg": 4.5, "duration_s": 1.0,
           "mag_apparent": 2.0, "shower": None, "mag_absolute": 1.5}
    missing = rms_parse.missing_source_fields(row, ftp, rad)
    assert set(missing) == {"ra_beg", "shower"}


def test_missing_source_fields_ignores_genuinely_absent_measurements():
    """Fields the sources themselves left null are NOT flagged — RMS simply
    didn't measure them for that detection."""
    ftp = {"ra_beg": 12.3, "duration_s": 1.0, "peak_mag": 2.0}
    rad = {}  # radiants had no radiant coords, no shower
    row = {"ra_beg": 12.3, "duration_s": 1.0, "mag_apparent": 2.0,
           "ra_radiant": None, "shower": None}
    assert rms_parse.missing_source_fields(row, ftp, rad) == []


# ── GMN trajectory join ───────────────────────────────────────────────────────


def _traj_fixture(tmp_path: Path, cam: str, begin_utc: str) -> Path:
    """Write a one-row 86-column GMN traj_summary file witnessed by ``cam``."""
    cells = ["" for _ in range(86)]
    cells[0] = "20260115200001_test0"
    cells[2] = begin_utc                 # begin UTC 'YYYY-MM-DD HH:MM:SS.ffffff'
    cells[4] = "PER"
    cells[5] = "294.5"
    cells[7] = "45.1"                    # RA geo
    cells[9] = "58.0"                    # Dec geo
    cells[15] = "59.2"                   # Vgeo
    cells[23] = "2.7"                    # a
    cells[25] = "0.62"                   # e
    cells[27] = "113.0"                  # i
    cells[29] = "150.0"                  # peri
    cells[31] = "294.5"                  # node
    cells[37] = "0.95"                   # q
    cells[43] = "4.5"                    # q_aph
    cells[49] = "1.9"                    # tisserand
    cells[59] = "60.4"                   # V initial
    cells[61] = "58.1"                   # V average
    cells[21] = "41.3"                   # V heliocentric
    cells[67] = "102.0"                  # ht begin
    cells[73] = "88.0"                   # ht end
    cells[75] = "0.42"                   # duration
    cells[76] = "-2.1"                   # peak mag
    cells[77] = "96.3"                   # ht of peak brightness
    cells[78] = "0.51"                   # F parameter
    cells[79] = "9.30e-05"               # photometric mass (kg)
    cells[80] = "31.2"                   # convergence angle
    cells[84] = "2"
    cells[85] = f"{cam},RO000Z"          # participating stations
    fixture = tmp_path / "traj_summary_monthly_202601.txt"
    fixture.write_text("# header comment line\n" + ";".join(cells) + "\n")
    return fixture


@pytest.fixture
def stub_gmn_month(tmp_path, monkeypatch):
    """Point gmn_data's monthly cache resolution at a one-row fixture and clear
    the in-process trajectory index cache so each test starts clean."""
    fixture = _traj_fixture(tmp_path, "RO000H", "2026-01-15 20:00:01.000000")
    monkeypatch.setattr(gmn_data, "_cache_path_for_month",
                        lambda year, month: fixture)
    gmn_data._traj_index_cache.clear()
    yield fixture
    gmn_data._traj_index_cache.clear()


def test_match_trajectory_hits_on_cam_and_time(stub_gmn_month):
    ev = gmn_data.match_trajectory("RO000H", "2026-01-15T20:00:01")
    assert ev is not None
    assert ev["id"] == "20260115200001_test0"
    assert ev["ra_geo"] == pytest.approx(45.1)
    assert ev["velocity"] == pytest.approx(59.2)


def test_match_trajectory_within_time_tolerance(stub_gmn_month):
    # 1 s off the GMN begin time still matches (sub-second solver disagreement).
    assert gmn_data.match_trajectory("RO000H", "2026-01-15T20:00:02") is not None


def test_match_trajectory_miss_outside_tolerance(stub_gmn_month):
    # 30 s off is a different meteor — no match.
    assert gmn_data.match_trajectory("RO000H", "2026-01-15T20:00:31") is None


def test_match_trajectory_miss_wrong_camera(stub_gmn_month):
    assert gmn_data.match_trajectory("RO000X", "2026-01-15T20:00:01") is None


def test_trajectory_for_detection_projection(stub_gmn_month):
    traj = gmn_data.trajectory_for_detection("RO000H", "2026-01-15T20:00:01")
    assert traj is not None
    assert traj["trajectory_id"] == "20260115200001_test0"
    assert traj["ra_geo_deg"] == pytest.approx(45.1)
    assert traj["dec_geo_deg"] == pytest.approx(58.0)
    assert traj["velocity_geo_km_s"] == pytest.approx(59.2)
    assert traj["orbit_a_au"] == pytest.approx(2.7)
    assert traj["orbit_e"] == pytest.approx(0.62)
    assert traj["altitude_begin_km"] == pytest.approx(102.0)
    assert traj["station_count"] == 2


def test_trajectory_for_detection_none_when_not_triangulated(stub_gmn_month):
    assert gmn_data.trajectory_for_detection("RO000H", "2026-01-15T21:00:00") is None


def test_trajectory_projection_carries_higher_order_fields(stub_gmn_month):
    """Velocities, heights, photometric mass and the convergence angle only
    exist for a triangulated meteor. They are the fields that make the panel
    under a clip worth expanding, so the projection must carry them."""
    traj = gmn_data.trajectory_for_detection("RO000H", "2026-01-15T20:00:01")
    assert traj["velocity_init_km_s"] == pytest.approx(60.4)
    assert traj["velocity_avg_km_s"] == pytest.approx(58.1)
    assert traj["velocity_hel_km_s"] == pytest.approx(41.3)
    assert traj["altitude_peak_km"] == pytest.approx(96.3)
    assert traj["peak_absolute_magnitude"] == pytest.approx(-2.1)
    assert traj["mass_kg"] == pytest.approx(9.3e-05)
    assert traj["f_param"] == pytest.approx(0.51)
    assert traj["convergence_angle_deg"] == pytest.approx(31.2)
    assert traj["stations"] == ["RO000H", "RO000Z"]
    assert traj["shower"] == "PER"


def test_attach_trajectory_labels_provenance_both_ways(stub_gmn_month):
    hit = gmn_data.attach_trajectory({}, "RO000H", "2026-01-15T20:00:01")
    assert hit["provenance"] == gmn_data.MULTI_STATION_PROVENANCE
    assert hit["trajectory"]["velocity_geo_km_s"] == pytest.approx(59.2)

    miss = gmn_data.attach_trajectory({}, "RO000H", "2026-01-15T21:00:00")
    assert miss["provenance"] == gmn_data.SINGLE_STATION_PROVENANCE
    assert miss["trajectory"] is None


def test_attach_trajectory_survives_a_broken_gmn_cache(stub_gmn_month, monkeypatch):
    """GMN publishes days behind and its cache can be mid-download. A failed
    lookup must leave the single-station record intact, never fail the request
    that carries it."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("corrupt monthly cache")
    monkeypatch.setattr(gmn_data, "trajectory_for_detection", _boom)

    out = gmn_data.attach_trajectory({"shower": "PER"}, "RO000H", "2026-01-15T20:00:01")
    assert out["provenance"] == gmn_data.SINGLE_STATION_PROVENANCE
    assert out["trajectory"] is None
    assert out["shower"] == "PER"


def test_trajectories_for_camera_night_spans_noon_to_noon(tmp_path, monkeypatch):
    """A night is the stations' noon-to-noon window, so a meteor just after
    midnight belongs to the evening it started in — not the next date."""
    cells_evening = _traj_fixture(tmp_path, "RO000H", "2026-01-15 20:00:01.000000")
    monkeypatch.setattr(gmn_data, "_cache_path_for_month", lambda y, m: cells_evening)
    gmn_data._traj_index_cache.clear()

    # 20:00 on the 15th is inside night 20260115...
    rows = gmn_data.trajectories_for_camera_night("RO000H", "20260115")
    assert [r["time_utc"] for r in rows] == ["2026-01-15T20:00:01"]
    assert rows[0]["trajectory"]["velocity_geo_km_s"] == pytest.approx(59.2)

    # ...and not inside the night that starts the following evening.
    assert gmn_data.trajectories_for_camera_night("RO000H", "20260116") == []
    gmn_data._traj_index_cache.clear()


def test_trajectories_for_camera_night_claims_post_midnight_meteors(
    tmp_path, monkeypatch
):
    fixture = _traj_fixture(tmp_path, "RO000H", "2026-01-16 02:30:00.000000")
    monkeypatch.setattr(gmn_data, "_cache_path_for_month", lambda y, m: fixture)
    gmn_data._traj_index_cache.clear()

    # 02:30 on the 16th belongs to the night that began on the 15th.
    assert len(gmn_data.trajectories_for_camera_night("RO000H", "20260115")) == 1
    assert gmn_data.trajectories_for_camera_night("RO000H", "20260116") == []
    gmn_data._traj_index_cache.clear()


def test_trajectories_for_camera_night_empty_for_unknown_inputs(stub_gmn_month):
    assert gmn_data.trajectories_for_camera_night("RO000X", "20260115") == []
    assert gmn_data.trajectories_for_camera_night("RO000H", "not-a-date") == []
    assert gmn_data.trajectories_for_camera_night("", "20260115") == []


def test_attach_trajectories_labels_every_row_of_a_night(stub_gmn_month):
    rows = [
        {"ff_file": "a", "time_utc": "2026-01-15T20:00:01"},   # triangulated
        {"ff_file": "b", "time_utc": "2026-01-15T21:00:00"},   # single-station
        {"ff_file": "c", "time_utc": None},                    # no begin time
    ]
    gmn_data.attach_trajectories("RO000H", rows)
    assert [r["provenance"] for r in rows] == [
        gmn_data.MULTI_STATION_PROVENANCE,
        gmn_data.SINGLE_STATION_PROVENANCE,
        gmn_data.SINGLE_STATION_PROVENANCE,
    ]
    assert rows[0]["trajectory"] is not None
    assert rows[1]["trajectory"] is None and rows[2]["trajectory"] is None


# ── Public projection provenance ──────────────────────────────────────────────


def test_public_projection_labels_multi_station(stub_gmn_month, monkeypatch):
    import public_api

    # Pin a base URL so media URL building doesn't need a Flask request context.
    monkeypatch.setenv("ROVIMEN_PUBLIC_BASE_URL", "https://test.example")
    det = {
        "host_key": "gmnro01", "cam": "RO000H", "filename": "clip.mkv",
        "meteor_time": "2026-01-15T20:00:01", "station_label": "Test",
        "rms": {"shower": "PER", "mag_apparent": -2.0, "ra_beg": 91.0},
    }
    out = public_api._detection_to_public(det, "2026-01-15", {"gmnro01"})
    assert out is not None
    assert out["provenance"] == "multi_station"
    assert out["trajectory"] is not None
    assert out["trajectory"]["velocity_geo_km_s"] == pytest.approx(59.2)
    # Single-station begin coord is still present alongside the trajectory.
    assert out["ra_begin_deg"] == pytest.approx(91.0)


def test_public_projection_labels_single_station(stub_gmn_month, monkeypatch):
    import public_api

    monkeypatch.setenv("ROVIMEN_PUBLIC_BASE_URL", "https://test.example")
    det = {
        "host_key": "gmnro01", "cam": "RO000H", "filename": "clip.mkv",
        "meteor_time": "2026-01-15T21:00:00",   # no trajectory at this time
        "station_label": "Test", "rms": {"shower": "SPO", "mag_apparent": 1.0},
    }
    out = public_api._detection_to_public(det, "2026-01-15", {"gmnro01"})
    assert out is not None
    assert out["provenance"] == "single_station"
    assert out["trajectory"] is None


# ── Archive detection endpoint: one canonical parse ───────────────────────────
#
# Nights older than two days are served from the archive, not the station, so
# the archive parse is what feeds the detail panel for nearly every clip a
# viewer opens. It must produce the same rows the station API does.


@pytest.fixture
def archive_night(tmp_path: Path, monkeypatch) -> Path:
    """A storage-box-shaped archive holding one night's rms/ session files."""
    rms_dir = tmp_path / "RO000H" / "20260314" / "rms"
    rms_dir.mkdir(parents=True)
    (rms_dir / "FTPdetectinfo_RO000H_20260314.txt").write_text(_FTP_TWO)
    (rms_dir / "RO000H_20260314_180000_000000_radiants.txt").write_text(_RAD_ONE)

    import routes.archive as archive_routes
    monkeypatch.setattr(archive_routes, "ARCHIVE_PATH", tmp_path)
    return tmp_path


def test_archive_detections_pair_each_meteor_with_its_own_photometry(archive_night):
    """The single radiants row belongs to the SECOND FF block. A positional
    merge would hand its shower and magnitude to the first."""
    from routes.archive import archive_rms_detections

    rows = {r["ff_file"]: r for r in archive_rms_detections("RO000H", "20260314")}
    assert set(rows) == {
        "FF_RO000H_20260314_200000_000_0000000.fits",
        "FF_RO000H_20260314_200005_000_0000000.fits",
    }
    assert rows["FF_RO000H_20260314_200005_000_0000000.fits"]["shower"] == "PER"
    assert rows["FF_RO000H_20260314_200000_000_0000000.fits"]["shower"] is None


def test_archive_detections_carry_the_full_science_set(archive_night):
    """Kinematics the old positional merge never returned (az/elev, angular
    velocity, radiant) must reach the browser — they are what the detail panel
    under a clip renders."""
    from routes.archive import archive_rms_detections

    rows = {r["ff_file"]: r for r in archive_rms_detections("RO000H", "20260314")}
    per = rows["FF_RO000H_20260314_200005_000_0000000.fits"]
    assert per["azim_beg"] == pytest.approx(130.0)
    assert per["elev_beg"] == pytest.approx(45.0)
    assert per["azim_end"] == pytest.approx(131.0)
    assert per["elev_end"] == pytest.approx(46.0)
    assert per["angular_velocity"] is not None
    assert per["fps"] == pytest.approx(25.0)
    assert per["num_segments"] == 2
    assert per["ra_radiant"] == pytest.approx(200.0)
    assert per["radiant_elev"] == pytest.approx(45.0)


def test_archive_detections_empty_when_night_absent(archive_night):
    from routes.archive import archive_rms_detections

    assert archive_rms_detections("RO000H", "20991231") == []


def test_detections_archive_rms_rows_keyed_by_time(archive_night, monkeypatch):
    """Both detections-payload paths (index fallback and archive walk) merge
    RMS science onto locked chunks through one reader of the night's
    ``rms/*_radiants.txt``, keyed by the radiants row's ``time_utc``."""
    import routes.detections as det

    monkeypatch.setattr(det, "ARCHIVE_PATH", archive_night)
    rows = det._archive_rms_by_time("RO000H", "20260314")
    assert len(rows) == 1
    (time_utc, row), = rows.items()
    assert row["time_utc"] == time_utc
    assert row["shower"] == "PER"
    assert det._archive_rms_by_time("RO000H", "20991231") == {}
