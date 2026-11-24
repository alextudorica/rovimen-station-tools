"""Wide-field streak trigger: impostor rejection.

The first spectro night (20260917) produced 7 "spectra", every one a moonlit
cloud edge drifting through GH662W. These cover the three vetoes that reject
them: thinness (a cloud edge is a broad ramp), per-frame count (a cloud deck
fits many lines at once), and persistence across frames (a meteor lives in
exactly one 10 s exposure).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "rovimen-scripts"))

cv2 = pytest.importorskip("cv2", reason="wide detector needs OpenCV")

from spectro_pipeline import (  # noqa: E402
    WIDE_MAX_STREAKS_PER_FRAME,
    WIDE_PERSIST_FRAMES,
    _same_line,
    detect_streaks,
)

H, W = 720, 1280


def _blank() -> np.ndarray:
    return np.zeros((H, W), dtype=float)


def _thin_trail(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int,
                value: float = 90.0) -> np.ndarray:
    """A meteor-like trail: 2 px wide, sharp edges."""
    out = frame.copy()
    cv2.line(out, (x0, y0), (x1, y1), value, thickness=2)
    return out


def _cloud_edge(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int,
                value: float = 90.0) -> np.ndarray:
    """A cloud edge: same brightness, but a broad blurred ramp."""
    out = frame.copy()
    cv2.line(out, (x0, y0), (x1, y1), value, thickness=25)
    return cv2.GaussianBlur(out, (31, 31), 0)


class TestThinnessVeto:

    def test_thin_trail_is_detected(self):
        prev = _blank()
        frame = _thin_trail(prev, 200, 200, 400, 300)
        streaks = detect_streaks(frame, prev)
        assert streaks, "a sharp 2 px trail must survive every veto"
        assert streaks[0]["off_ratio"] <= 0.5

    def test_broad_cloud_edge_is_rejected(self):
        prev = _blank()
        frame = _cloud_edge(prev, 200, 200, 600, 400)
        assert detect_streaks(frame, prev) == [], "broad ramp must be vetoed"

    def test_off_ratio_reported_on_survivors(self):
        prev = _blank()
        frame = _thin_trail(prev, 100, 500, 500, 520)
        streaks = detect_streaks(frame, prev)
        assert streaks
        assert "off_ratio" in streaks[0]

    def test_threshold_is_the_discriminator(self):
        """With the veto disabled the same cloud edge WOULD be accepted, so the
        rejection comes from thinness, not from the edge being undetectable."""
        prev = _blank()
        frame = _cloud_edge(prev, 200, 200, 600, 400)
        assert detect_streaks(frame, prev, max_off_ratio=10.0)


class TestPersistenceOverlap:

    def test_identical_line_matches(self):
        a = {"p0": [100, 100], "p1": [300, 200]}
        assert _same_line(a, dict(a))

    def test_drifted_cloud_edge_still_matches(self):
        """A cloud edge re-fits with shifted endpoints between 10 s frames; the
        old both-endpoints-within-15 px test missed exactly this case."""
        a = {"p0": [100, 100], "p1": [300, 200]}
        b = {"p0": [118, 112], "p1": [332, 224]}
        assert _same_line(a, b)

    def test_partial_refit_matches_on_midpoint(self):
        a = {"p0": [100, 100], "p1": [300, 200]}
        b = {"p0": [190, 145], "p1": [420, 265]}   # shares the midpoint region
        assert _same_line(a, b)

    def test_distant_line_does_not_match(self):
        a = {"p0": [100, 100], "p1": [300, 200]}
        b = {"p0": [900, 600], "p1": [1100, 700]}
        assert not _same_line(a, b)

    def test_persist_window_covers_several_frames(self):
        assert WIDE_PERSIST_FRAMES >= 3


class TestCloudFieldCap:

    def test_cap_is_small(self):
        assert 1 <= WIDE_MAX_STREAKS_PER_FRAME <= 5

    def test_cloud_deck_fits_more_lines_than_the_cap(self):
        """Many simultaneous thin lines (a structured deck) exceed the cap, so
        run_wide_trigger skips the frame wholesale."""
        prev = _blank()
        frame = prev.copy()
        for i in range(8):
            frame = _thin_trail(frame, 80 + i * 140, 60, 140 + i * 140, 660)
        assert len(detect_streaks(frame, prev)) > WIDE_MAX_STREAKS_PER_FRAME


class TestClustering:
    """One bright trail fits as several overlapping Hough segments; counting
    those as separate objects would reject real meteors as 'cloud fields'
    (verified against the RO000A 2026-09-17 21:14:27 meteor, 5 fits)."""

    def test_overlapping_fits_collapse_to_one(self):
        from spectro_pipeline import cluster_streaks
        base = {"p0": [200, 200], "p1": [400, 300], "length": 56, "filled": 0.96,
                "off_ratio": 0.04, "mean_excess": 22.7}
        frags = [
            base,
            {**base, "p0": [210, 205], "p1": [395, 297], "length": 49},
            {**base, "p0": [260, 230], "p1": [400, 300], "length": 31},
        ]
        assert len(cluster_streaks(frags)) == 1

    def test_cluster_keeps_longest_representative(self):
        from spectro_pipeline import cluster_streaks
        base = {"p0": [200, 200], "p1": [400, 300], "length": 31, "filled": 0.9,
                "off_ratio": 0.05, "mean_excess": 10.0}
        frags = [base, {**base, "p0": [205, 202], "p1": [402, 302], "length": 56}]
        assert cluster_streaks(frags)[0]["length"] == 56

    def test_distinct_objects_stay_separate(self):
        from spectro_pipeline import cluster_streaks
        a = {"p0": [100, 100], "p1": [300, 200], "length": 40, "filled": 0.9,
             "off_ratio": 0.05, "mean_excess": 10.0}
        b = {**a, "p0": [900, 600], "p1": [1100, 700]}
        assert len(cluster_streaks([a, b])) == 2

    def test_real_meteor_fragment_set_survives_the_cap(self):
        """The five fits of the 21:14:27 meteor must read as ONE object, i.e.
        under the cloud-field cap rather than over it."""
        from spectro_pipeline import cluster_streaks
        obs = [(56, 0.04), (56, 0.16), (49, 0.21), (31, 0.06), (41, 0.08)]
        frags = [{"p0": [200 + i * 4, 200 + i * 2], "p1": [400 - i * 3, 300 - i],
                  "length": ln, "filled": 0.95, "off_ratio": off, "mean_excess": 18.0}
                 for i, (ln, off) in enumerate(obs)]
        assert len(cluster_streaks(frags)) <= WIDE_MAX_STREAKS_PER_FRAME
