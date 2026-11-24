"""The "always show the last plot, marked if stale" invariant.

The plot surfaces used to break this two different ways. The overview asked
the STATION for one specific night and rendered a blank card when that night
had nothing -- even with last week's plots sitting in the archive. The station
page did walk back, but with a live station round-trip PER NIGHT (a serial
fan-out that saturated the storage-box concurrency guard) and then displayed
whatever it found with no indication of age, so a three-week-old plot looked
exactly like last night's. Both showed nothing at all when the station was
offline.

``resolve_latest_rms_plots`` answers it in one archive-local call and reports
stale/overdue. These tests pin the invariant, the date convention, and the
SSHFS budget the fan-out fix depends on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class FakeIndex:
    """Archive index stub that records how it was queried.

    ``hot`` is what the in-memory index knows; ``cold`` is only reachable via
    the on-demand SSHFS path, so the test can assert how many storage-box
    lookups a resolution costs.
    """

    def __init__(self, hot: dict[str, list[str]], cold: dict[str, list[str]] | None = None,
                 latest: tuple[str, list[str]] | None = None):
        self._hot = hot
        self._cold = cold or {}
        self._latest = latest
        self.ondemand_calls: list[str] = []

    def latest_rms_night(self, cam: str):
        """The index's resolved newest plot-bearing night (memory only).

        For a camera dark longer than the index's hot window this is the ONLY
        thing the resolvers can see -- every candidate night is absent from
        both the hot data and the on-demand cache.
        """
        if self._latest is None:
            return None
        date, files = self._latest
        return date, frozenset(files)

    def nights(self, cam: str) -> list[str]:
        return sorted(set(self._hot) | set(self._cold), reverse=True)

    def night_files(self, cam: str, date: str):
        self.ondemand_calls.append(date)
        files = self._hot.get(date, self._cold.get(date))
        if files is None:
            return None
        return type("NI", (), {"rms_files": frozenset(files)})()

    def rms_files_cached(self, cam: str, date: str):
        if date in self._hot:
            return frozenset(self._hot[date])
        return None

    def rms_plot_file(self, cam: str, date: str, suffix: str):
        ni = self.night_files(cam, date)
        if ni is None:
            return None
        return next((f for f in ni.rms_files if f.endswith(suffix)), None)


NOW = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
# expected_date is always now - 1 day: a night bucketed as D finishes near
# dawn of D+1 and is only processed during D+1, so the newest fully
# processed night at any point during D+1 is D.
EXPECTED = "20260903"


def _resolve(idx, now=NOW):
    from routes.archive import resolve_latest_rms_plots
    return resolve_latest_rms_plots(idx, "RO000W", now)


def test_expected_night_present_is_not_stale():
    idx = FakeIndex({EXPECTED: ["RO000W_20260903_ff_intervals.png"]})
    r = _resolve(idx)
    assert r["status"] == "ok"
    assert r["date"] == EXPECTED
    assert r["stale"] is False
    assert r["overdue"] is False
    assert [p["label"] for p in r["plots"]] == ["FF intervals"]


def test_falls_back_to_the_newest_night_that_has_plots():
    """THE INVARIANT: the last plot is shown, not a blank card."""
    idx = FakeIndex({
        EXPECTED: [],                                        # processed, no plots
        "20260902": [],                                      # ditto
        "20260828": ["RO000W_20260828_radiants.png"],        # last real one
        "20260820": ["RO000W_20260820_radiants.png"],        # older, must lose
    })
    r = _resolve(idx)
    assert r["status"] == "ok"
    assert r["date"] == "20260828"
    assert r["stale"] is True
    assert [p["label"] for p in r["plots"]] == ["Radiants"]


def test_stale_result_is_flagged_overdue_once_the_night_is_late():
    idx = FakeIndex({EXPECTED: [], "20260828": ["a_radiants.png"]})
    # 10:00 UTC on the 4th is well past the 6h grace after the 3rd's night end.
    assert _resolve(idx)["overdue"] is True
    # Just after midnight UTC on the 4th is still inside the grace window.
    early = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)
    assert _resolve(idx, early)["overdue"] is False


def test_no_plots_anywhere_is_no_data_not_a_crash():
    r = _resolve(FakeIndex({}))
    assert r["status"] == "no_data"
    assert r["date"] is None
    assert r["plots"] == []
    assert r["expected_date"] == EXPECTED


def test_future_nights_are_never_chosen():
    """Tonight's session has at most just started; it is not "latest"."""
    idx = FakeIndex({
        "20260905": ["a_radiants.png"],
        "20260904": ["a_radiants.png"],
        "20260828": ["a_ff_intervals.png"],
    })
    r = _resolve(idx)
    # 0903 is expected but absent -> walk back skips 0904/0905 entirely.
    assert r["date"] == "20260828"


def test_at_most_one_storage_box_lookup_per_camera():
    """THE FAN-OUT BUDGET: the walk must read memory only.

    This runs once per camera and a page renders every camera at once. If the
    walk could reach the storage box per night, a single page load fans out
    N_cameras x N_nights SSHFS ops into a 6-permit guard -- which is the
    timeout burst this fix exists to remove.
    """
    idx = FakeIndex(
        hot={"20260828": ["a_radiants.png"]},
        cold={EXPECTED: [], "20260902": [], "20260901": []},
    )
    r = _resolve(idx)
    assert r["date"] == "20260828"
    assert idx.ondemand_calls == [EXPECTED], (
        f"expected exactly one uncached lookup, got {idx.ondemand_calls}"
    )


def test_a_camera_dark_past_the_hot_window_still_shows_its_last_plots():
    """THE REGRESSION THIS FIXES: nothing to walk, so nothing was shown.

    The index keeps file listings for the last three nights only. A camera
    that stopped uploading weeks ago has date-only entries for every night it
    ever had, so ``rms_files_cached`` returns None for all of them and the
    memory-only walk above finds nothing -- while the plots sit in the
    archive. The index resolves that night in its background writers; the
    resolver's job is to use it.
    """
    idx = FakeIndex(
        hot={},
        cold={"20260818": ["RO0015_x_captured_stack.jpg"]},
        latest=("20260818", ["RO0015_x_captured_stack.jpg"]),
    )
    r = _resolve(idx)
    assert r["status"] == "ok"
    assert r["date"] == "20260818"
    assert r["stale"] is True
    assert r["overdue"] is True
    assert [p["label"] for p in r["plots"]] == ["Captured stack"]


def test_the_resolved_night_never_outranks_a_walkable_one():
    """It is the last resort, not a shortcut: a night the walk can see is
    newer by construction and must win."""
    idx = FakeIndex(
        hot={"20260902": ["a_radiants.png"]},
        latest=("20260818", ["a_captured_stack.jpg"]),
    )
    assert _resolve(idx)["date"] == "20260902"


def test_a_resolved_night_newer_than_expected_is_ignored():
    """Same rule as the walk: tonight's session is not "latest"."""
    idx = FakeIndex(hot={}, latest=("20260905", ["a_radiants.png"]))
    assert _resolve(idx)["status"] == "no_data"


def test_an_index_without_the_accessor_behaves_as_before():
    """Contract with older/stub indexes: report no_data, never raise."""
    class Bare:
        def nights(self, cam):
            return []

        def night_files(self, cam, date):
            return None

        def rms_files_cached(self, cam, date):
            return None

    assert _resolve(Bare())["status"] == "no_data"


def test_suffix_resolution_uses_the_resolved_night_too():
    """The calibration endpoints (FF intervals, astrometry residual) share the
    walk, so they went blank for a dark camera the same way."""
    from routes.archive import resolve_rms_plot_status
    idx = FakeIndex(hot={}, latest=(
        "20260818",
        ["a_ff_intervals.png", "a_captured_stack.jpg"],
    ))
    r = resolve_rms_plot_status(idx, "RO0015", "_ff_intervals.png", NOW)
    assert r["status"] == "ok"
    assert r["date"] == "20260818"
    assert r["filename"] == "a_ff_intervals.png"
    assert r["overdue"] is True

    missing = resolve_rms_plot_status(idx, "RO0015", "_photometry_variation.png", NOW)
    assert missing["status"] == "no_data"


def test_lookback_is_bounded_by_indexed_nights_not_calendar_distance():
    """The cap is the newest 30 INDEXED nights, matching
    resolve_rms_plot_status. A calendar-distant plot is still shown (flagged
    red) because a marked old plot beats a blank card -- but a camera with
    more than 30 newer plot-less nights stops the walk before older ones."""
    # 40 plot-less nights newer than the only night that has plots.
    hot = {f"202608{d:02d}": [] for d in range(1, 32)}
    hot.update({f"202607{d:02d}": [] for d in range(22, 32)})
    hot["20260701"] = ["a_radiants.png"]
    hot[EXPECTED] = []
    r = _resolve(FakeIndex(hot))
    assert r["status"] == "no_data", "walk must stop after 30 indexed nights"

    # With few enough intervening nights, a distant plot IS surfaced -- old
    # and flagged is better than nothing.
    r2 = _resolve(FakeIndex({EXPECTED: [], "20260301": ["a_radiants.png"]}))
    assert r2["status"] == "ok"
    assert r2["date"] == "20260301"
    assert r2["stale"] is True
    assert r2["overdue"] is True


@pytest.mark.parametrize("filename,label", [
    ("RO000W_20260903_captured_stack.jpg", "Captured stack"),
    ("RO000W_20260903_radiants.png", "Radiants"),
    ("RO000W_20260903_calibration_variation.png", "Calibration variation"),
    ("RO000W_20260903_stack_12_meteors.jpg", "Meteor stack"),
])
def test_known_plots_are_labelled(filename, label):
    from routes.archive import label_rms_plots
    got = label_rms_plots([filename])
    assert len(got) == 1
    assert got[0]["filename"] == filename
    assert got[0]["label"] == label


def test_unrecognised_archive_files_are_dropped():
    """The archive holds non-plot artefacts in the same directory."""
    from routes.archive import label_rms_plots
    assert label_rms_plots([
        "RO000W_20260903_platepar.cal",
        "RO000W_20260903.txt",
        "notes.md",
    ]) == []


def test_plots_come_back_in_display_order():
    from routes.archive import label_rms_plots
    got = [p["label"] for p in label_rms_plots([
        "a_radiants.png",
        "a_captured_stack.jpg",
        "a_DETECTED_thumbs.jpg",
    ])]
    assert got == ["Detected thumbnails", "Captured stack", "Radiants"]


def test_dashboard_labels_match_the_station_exactly():
    """Contract test: the station and the dashboard are separate deployments
    with no shared package, so the label map is duplicated. If they drift, the
    same plot gets two different names depending on which surface you look
    at, and the overview's label-union toggle grows phantom buttons."""
    import re
    from pathlib import Path

    from routes.archive import RMS_PLOT_LABELS

    src = (
        Path(__file__).resolve().parents[1] / "rovimen-scripts/station_api.py"
    ).read_text()
    m = re.search(
        r"_RMS_PLOT_LABELS: dict\[str, str\] = \{(.*?)\n\}", src, re.DOTALL
    )
    assert m, "station_api._RMS_PLOT_LABELS not found"
    station = dict(re.findall(r"'([^']+)':\s*'([^']+)'", m.group(1)))
    assert station, "failed to parse the station label map"
    assert station == RMS_PLOT_LABELS
    # Display order must agree too — the station sorts by its dict order.
    assert list(station) == list(RMS_PLOT_LABELS)
