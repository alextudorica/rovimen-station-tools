"""Overlay gate for downloadable per-detection meteor stacks.

The station stacker burns the ROVIMEN/astromania branding+data strip into every
per-chunk ``*_stack.webp`` at generation (``overlay.annotate_still``), stamping
an EXIF marker so the artifact self-describes as branded. The dashboard serve/
download routes funnel every per-detection stack through
``stack_overlay.ensure_overlaid_stack_bytes`` before serving: an already-branded
stack passes through untouched (marker present), a legacy un-branded stack is
rendered on demand, and a stack that cannot be branded is refused rather than
served raw -- mirroring the clip download gate (station_api ``_ensure_overlaid``).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from PIL import Image

import overlay


REPO_ROOT = Path(__file__).resolve().parent.parent
FONT = REPO_ROOT / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = REPO_ROOT / "rovimen-scripts" / "assets" / "astromania_text.png"

CAM = "RO0017"
STACK_NAME = f"{CAM}_20260730_214442_stack.webp"


def _blank_stack(path: Path, size: tuple[int, int] = (1280, 720)) -> Path:
    Image.new("RGB", size, (5, 8, 20)).save(path, "WEBP", quality=95)
    return path


def _overlay_cfg() -> dict:
    return {
        "enabled": True,
        "font": str(FONT),
        "logo": str(LOGO),
        "font_size": 20,
        "network": "ROVIMEN",
        "style": "cinema",
        "show_logo": True,
        "show_network": True,
        "show_station": True,
        "show_coords": True,
        "show_pointing": True,
        "show_timestamp": True,
        "coords": "45.0N 25.0E",
    }


# ---------------------------------------------------------------------------
# Station-side marker: overlay.annotate_still stamps and reads back the marker.
# ---------------------------------------------------------------------------


class TestStillOverlayMarker:
    def test_blank_stack_is_not_marked(self, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        assert overlay.still_is_overlaid(src) is False

    def test_annotate_still_marks_the_output(self, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        ok = overlay.annotate_still(
            src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )
        assert ok is True
        # The branded stack carries the marker AND the cinema data strip. The
        # bottom bar height is what build_drawtext_annotations reports for this
        # config (it scales with text_scale=1.5), so derive it rather than
        # hardcoding: a stale literal would drift when the branding is retuned.
        _, bar_h = overlay.build_drawtext_annotations(
            _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )
        assert bar_h > 0
        assert overlay.still_is_overlaid(src) is True
        assert Image.open(src).size == (1280, 720 + bar_h)

    def test_mark_and_read_round_trip(self, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        assert overlay.mark_still_overlaid(src) is True
        assert overlay.still_is_overlaid(src) is True
        # The stamped EXIF UserComment carries the published marker string.
        exif = Image.open(src).getexif()
        raw = exif.get(0x9286)
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "ignore")
        assert overlay.still_overlay_marker() in raw


# ---------------------------------------------------------------------------
# Dashboard gate: ensure_overlaid_stack_bytes never returns an un-branded image.
# ---------------------------------------------------------------------------


@pytest.fixture
def stack_gate(monkeypatch):
    """Import stack_overlay against a fake station bundle carrying overlay.py.

    The gate loads overlay.py + font + logo from ``ROVIMEN_BUNDLE_DIR`` the same
    way it does from ``/opt/rovimen`` in production.
    """
    bundle_root = Path(tempfile.mkdtemp(prefix="rovimen_bundle_"))
    bdir = bundle_root / "station-bundle"
    (bdir / "fonts").mkdir(parents=True)
    (bdir / "assets").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "rovimen-scripts" / "overlay.py", bdir / "overlay.py")
    shutil.copy(FONT, bdir / "fonts" / FONT.name)
    shutil.copy(LOGO, bdir / "assets" / LOGO.name)
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(bundle_root))

    import importlib
    import stack_overlay as _mod

    _mod = importlib.reload(_mod)
    try:
        yield _mod
    finally:
        shutil.rmtree(bundle_root, ignore_errors=True)
        importlib.reload(_mod)  # restore the default (no-bundle) module state


class TestEnsureOverlaidStackBytes:
    def test_unbranded_stack_is_rendered_and_marked(self, stack_gate, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        assert stack_gate.stack_is_overlaid(src) is False

        out = stack_gate.ensure_overlaid_stack_bytes(
            src, STACK_NAME, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}
        )
        assert out is not None
        # Bytes differ from the raw stack and carry the branding marker.
        assert out != src.read_bytes()
        rendered = tmp_path / "rendered.webp"
        rendered.write_bytes(out)
        assert overlay.still_is_overlaid(rendered) is True

    def test_already_branded_stack_passes_through_untouched(self, stack_gate, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        overlay.annotate_still(
            src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )
        original = src.read_bytes()
        assert stack_gate.stack_is_overlaid(src) is True

        out = stack_gate.ensure_overlaid_stack_bytes(
            src, STACK_NAME, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}
        )
        # No re-render: identical bytes back, so no double stamping.
        assert out == original

    def test_overlay_disabled_returns_original(self, stack_gate, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        original = src.read_bytes()
        out = stack_gate.ensure_overlaid_stack_bytes(
            src, STACK_NAME, {"enabled": False}, CAM, {}
        )
        assert out == original

    def test_missing_source_returns_none(self, stack_gate, tmp_path):
        out = stack_gate.ensure_overlaid_stack_bytes(
            tmp_path / "does_not_exist.webp", STACK_NAME, _overlay_cfg(), CAM, {}
        )
        assert out is None

    def test_no_bundle_refuses_rather_than_serving_raw(self, monkeypatch, tmp_path):
        """With no station bundle to render with, the gate must refuse (None),
        never return the un-branded original."""
        empty = Path(tempfile.mkdtemp(prefix="rovimen_nobundle_"))
        monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(empty))
        import importlib
        import stack_overlay as _mod

        _mod = importlib.reload(_mod)
        try:
            src = _blank_stack(tmp_path / STACK_NAME)
            out = _mod.ensure_overlaid_stack_bytes(
                src, STACK_NAME, _overlay_cfg(), CAM, {}
            )
            assert out is None
        finally:
            shutil.rmtree(empty, ignore_errors=True)
            importlib.reload(_mod)


class TestOverlaidStackBytesRenderedFlag:
    """The flag tells the archive route whether the bytes are a new render
    (worth caching) or the source passed through unchanged (not)."""

    def test_unbranded_stack_is_rendered(self, stack_gate, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        data, rendered = stack_gate.overlaid_stack_bytes(
            src, STACK_NAME, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}
        )
        assert data is not None and data != src.read_bytes()
        assert rendered is True

    def test_already_branded_stack_is_not_rendered(self, stack_gate, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        overlay.annotate_still(
            src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )
        assert stack_gate.overlaid_stack_bytes(
            src, STACK_NAME, _overlay_cfg(), CAM, {}
        ) == (src.read_bytes(), False)

    def test_stitched_stack_is_not_rendered(self, stack_gate, tmp_path):
        name = f"{CAM}_20260730_214442_214502_stack.webp"
        src = _blank_stack(tmp_path / name)
        assert stack_gate.overlaid_stack_bytes(
            src, name, _overlay_cfg(), CAM, {}
        ) == (src.read_bytes(), False)

    def test_overlay_disabled_is_not_rendered(self, stack_gate, tmp_path):
        src = _blank_stack(tmp_path / STACK_NAME)
        assert stack_gate.overlaid_stack_bytes(
            src, STACK_NAME, {"enabled": False}, CAM, {}
        ) == (src.read_bytes(), False)

    def test_failures_are_not_rendered(self, stack_gate, tmp_path):
        assert stack_gate.overlaid_stack_bytes(
            tmp_path / "does_not_exist.webp", STACK_NAME, _overlay_cfg(), CAM, {}
        ) == (None, False)


class TestBundleLogoResolution:
    """The dashboard must render each station's OWN mark, not the network's.

    The station config points ``logo`` at the station's home dir, which does not
    exist on the VPS, so the path is remapped to the bundle copy before the
    dashboard renders. Until 2026-08-25 that remap was a fixed
    ``astromania_text.png``, which re-attributed every dashboard-rendered image:
    the partner clubs' emblems and the GMN sphere
    all came back stamped
    ASTROMANIA -- on exactly the images a visitor downloads. Resolution is by
    basename now, with the wordmark as a last resort only.
    """

    def _add_asset(self, gate, name: str) -> Path:
        src = REPO_ROOT / "rovimen-scripts" / "assets" / name
        dst = gate._bundle_path() / "assets" / name
        shutil.copy(src, dst)
        return dst

    def test_station_keeps_its_own_mark(self, stack_gate):
        own = self._add_asset(stack_gate, "sternfreunde_berlin.png")
        cfg = {"logo": "/home/gmn/rovimen_scripts/assets/sternfreunde_berlin.png"}
        assert stack_gate.bundle_logo_for(cfg) == own

    def test_every_shipped_mark_resolves_to_itself(self, stack_gate):
        for name in ("astroian_observatory.png", "abtext.png", "gmn_sphere.png",
                     "povesti_cu_stele_observatory.png", "sternfreunde_berlin.png",
                     "zeta_aquarii_observatory.png"):
            own = self._add_asset(stack_gate, name)
            cfg = {"logo": f"/home/whoever/rovimen_scripts/assets/{name}"}
            assert stack_gate.bundle_logo_for(cfg) == own, name

    def test_unbundled_mark_falls_back_to_the_wordmark(self, stack_gate):
        """Branded beats un-branded -- but only when we have nothing else."""
        cfg = {"logo": "/home/x/rovimen_scripts/assets/never_shipped.png"}
        resolved = stack_gate.bundle_logo_for(cfg)
        assert resolved is not None and resolved.name == "astromania_text.png"

    def test_no_logo_configured_falls_back_to_the_wordmark(self, stack_gate):
        assert stack_gate.bundle_logo_for({}).name == "astromania_text.png"

    def test_empty_bundle_resolves_to_nothing(self, stack_gate, monkeypatch, tmp_path):
        monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(tmp_path))
        assert stack_gate.bundle_logo_for({"logo": "/home/x/assets/abtext.png"}) is None

    def test_remap_rewrites_the_cfg_to_the_bundle_copy(self, stack_gate):
        own = self._add_asset(stack_gate, "abtext.png")
        cfg = stack_gate._bundle_overlay_cfg(
            {"logo": "/home/gmn/rovimen_scripts/assets/abtext.png", "network": "ROVIMEN"}
        )
        assert cfg["logo"] == str(own)
        assert cfg["network"] == "ROVIMEN"        # untouched
        assert Path(cfg["font"]).name == FONT.name
