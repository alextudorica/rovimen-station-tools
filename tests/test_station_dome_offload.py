"""PR2 — sky-dome rendering moved from the VPS to the stations.

The station composites its own dome and uploads it over the ingest byte-plane;
the dashboard serves that upload and only renders as a fallback. The scariest
regression is a silent path/contract drift between producer
(``rovimen-scripts/sky_dome_render.py``) and consumer
(``dashboard/routes/sky_dome.py``) — a mismatch means the dashboard renders
forever (no offload) or 404s. Both sides share the path contract in
``celestial_dome``; these tests pin it, prove the chosen upload paths are
actually ACCEPTED (and not misclassified) by the ingest endpoint, and cover the
station renderer's fail-safe behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone


# ── Shared contract (celestial_dome) ─────────────────────────────────────

def test_primary_cam_is_deterministic_and_order_independent():
    from celestial_dome import station_dome_primary_cam

    # Producer (station config order) and consumer (dashboard config order) can
    # list cameras differently; sorted()[0] makes them agree regardless.
    assert station_dome_primary_cam(["RO000Y", "RO0010", "RO000A"]) == "RO000A"
    assert station_dome_primary_cam(["RO000A", "RO000Y"]) == "RO000A"
    assert station_dome_primary_cam(["RO000Y", "RO000A"]) == "RO000A"
    assert station_dome_primary_cam([]) is None
    assert station_dome_primary_cam(["", None, "RO0001"]) == "RO0001"  # falsy filtered


def test_dome_png_names():
    from celestial_dome import station_dome_png_name

    assert station_dome_png_name("stack", overlay=False) == "rovimen_sky_dome.png"
    assert station_dome_png_name("stack", overlay=True) == "rovimen_sky_dome_overlay.png"
    assert station_dome_png_name("ff_max", overlay=False) == "rovimen_sky_dome_ff_max.png"


# ── Cross-module: the ingest endpoint must ACCEPT the upload paths ───────

def test_dome_upload_paths_pass_ingest_whitelist():
    """The station uploads via the generic blob endpoint, so its chosen subdirs
    + filenames MUST satisfy ingest_api's ``_safe_blob_relpath`` — otherwise
    every dome upload is a silent 422."""
    from celestial_dome import (
        STATION_DOME_PNG_SUBDIR,
        STATION_DOME_TIMELAPSE_NAME,
        STATION_DOME_TIMELAPSE_SUBDIR,
        station_dome_png_name,
    )
    from ingest_api import _safe_blob_relpath

    png = f"{STATION_DOME_PNG_SUBDIR}/{station_dome_png_name('stack', False)}"
    assert _safe_blob_relpath(png) == ("stacks", "rovimen_sky_dome.png")

    png_overlay = f"{STATION_DOME_PNG_SUBDIR}/{station_dome_png_name('stack', True)}"
    assert _safe_blob_relpath(png_overlay) == ("stacks", "rovimen_sky_dome_overlay.png")

    tl = f"{STATION_DOME_TIMELAPSE_SUBDIR}/{STATION_DOME_TIMELAPSE_NAME}"
    assert _safe_blob_relpath(tl) == ("timelapse", "rovimen_sky_dome.mp4")


def test_dome_uploads_are_not_misclassified_as_normal_media():
    """A dome blob must write bytes only — never register as a clip / timelapse /
    night-stack media pointer (which would surface it in the normal galleries)."""
    from celestial_dome import (
        STATION_DOME_PNG_SUBDIR,
        STATION_DOME_TIMELAPSE_NAME,
        STATION_DOME_TIMELAPSE_SUBDIR,
        station_dome_png_name,
    )
    from ingest_api import _blob_pointer

    assert _blob_pointer(
        STATION_DOME_TIMELAPSE_SUBDIR, STATION_DOME_TIMELAPSE_NAME, "RO000A", "20260723"
    ) is None
    assert _blob_pointer(
        STATION_DOME_PNG_SUBDIR, station_dome_png_name("stack", False), "RO000A", "20260723"
    ) is None


# ── Consumer imports the SAME contract (no duplicated constants) ─────────

def test_dashboard_and_station_share_one_contract_source():
    import celestial_dome
    import routes.sky_dome as sky_dome  # noqa: F401 - import proves it loads
    import sky_dome_render

    # The station renderer resolves its upload location purely through the
    # celestial_dome helpers — so it can never drift from what the dashboard
    # (which imports the same helpers) reads.
    assert hasattr(celestial_dome, "station_dome_primary_cam")
    assert hasattr(celestial_dome, "station_dome_png_name")
    assert sky_dome_render.render_once is not None


# ── Station renderer: capture-night must match the dashboard exactly ─────

def test_capture_night_matches_dashboard_noon_rule():
    from sky_dome_render import _dome_capture_night

    # Before noon UTC -> previous evening's date (same archive dir the dashboard
    # computes with its own _dome_capture_night).
    assert _dome_capture_night(datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)) == "20260722"
    # After noon UTC -> today.
    assert _dome_capture_night(datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)) == "20260723"
    # Noon boundary -> today.
    assert _dome_capture_night(datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)) == "20260723"


def test_station_api_base_from_config():
    from sky_dome_render import _station_api_base

    assert _station_api_base({"station_api_port": 7779}) == "http://127.0.0.1:7779"
    assert _station_api_base({}) == "http://127.0.0.1:7779"  # default port


# ── Station renderer: fail-safe, never crash the pipeline ────────────────

# The shape of a real station config.json (station_configs/gmnro12/config.json,
# gmnro12 (Iași), trimmed): cameras are the keys of ``stations``, ``rotate``
# lives in each camera's block, and the location is top-level
# ``latitude``/``longitude`` with ``overlay.coords`` as the older form. No
# station config has a ``cameras`` list, ``station_id`` or ``station_label``.
def _iasi_cfg() -> dict:
    return {
        "stations": {
            "RO0016": {"rms_data_path": "/home/rmsuser/RMS_data/cam2", "rotate": True},
            "RO0015": {"rms_data_path": "/home/rmsuser/RMS_data/cam1", "rotate": False},
        },
        "latitude": 46.711150,
        "longitude": 27.410450,
        "elevation": 87.0,
        "overlay": {"coords": "46.72N  27.87E"},
        "services": {"sky_dome_render": {"enabled": False}},
    }


def test_render_once_no_cameras_returns_false():
    import sky_dome_render

    assert sky_dome_render.render_once({"stations": {}}) is False
    assert sky_dome_render.render_once({}) is False


def test_build_plates_empty_when_station_api_unreachable(monkeypatch):
    import sky_dome_render

    # station API down -> platepar fetch returns None -> no plates, no crash.
    monkeypatch.setattr(sky_dome_render, "_get_json", lambda url: None)
    assert sky_dome_render._build_plates_with_images(
        _iasi_cfg(), "http://127.0.0.1:7779") == []


class _FakePlate:
    def __init__(self, code: str) -> None:
        self.code = code

    @classmethod
    def from_dict(cls, entry: dict, code: str) -> "_FakePlate":
        return cls(code)


def _stack_png() -> bytes:
    """A 2x1 stack: left pixel white, right pixel black."""
    import io

    from PIL import Image

    img = Image.new("RGB", (2, 1), (0, 0, 0))
    img.putpixel((0, 0), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fake_render_pass(monkeypatch, cfg: dict):
    """Run render_once on ``cfg`` against a fake station API; return what the
    dome renderer was given and what was uploaded."""
    import celestial_dome
    import sky_dome_render
    from PIL import Image

    monkeypatch.setenv("ROVIMEN_HOST_KEY", "gmnro12")
    monkeypatch.setattr(sky_dome_render, "_get_json", lambda url: {
        "RO0015": {"az_centre": 10.0}, "RO0016": {"az_centre": 200.0},
    })
    monkeypatch.setattr(sky_dome_render, "_fetch_stack_bytes",
                        lambda base, code: _stack_png())
    monkeypatch.setattr(celestial_dome, "Plate", _FakePlate)
    rendered: list[tuple[list, dict]] = []

    def _render(plates_with_images, **kwargs):
        rendered.append((plates_with_images, kwargs))
        return Image.new("RGB", (4, 4))

    monkeypatch.setattr(celestial_dome, "render_station_dome", _render)
    uploads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sky_dome_render, "_upload_png",
        lambda cfg, cam, night, name, png: uploads.append((cam, name)) or True,
    )
    ok = sky_dome_render.render_once(cfg)
    return ok, rendered, uploads


def test_render_once_renders_every_camera_of_a_real_config(monkeypatch):
    """A station enabling the service logged "station has no cameras" and
    uploaded nothing, because the renderer read a ``cameras`` list no
    config.json has."""
    ok, rendered, uploads = _fake_render_pass(monkeypatch, _iasi_cfg())
    assert ok is True
    assert uploads == [("RO0015", "rovimen_sky_dome.png"),
                       ("RO0015", "rovimen_sky_dome_overlay.png")]
    plates, kwargs = rendered[0]
    assert sorted(p.code for p, _img in plates) == ["RO0015", "RO0016"]
    assert kwargs["station"] == "gmnro12"
    assert (kwargs["lat"], kwargs["lon"]) == (46.711150, 27.410450)


def test_render_once_unrotates_only_the_rotated_camera(monkeypatch):
    _ok, rendered, _uploads = _fake_render_pass(monkeypatch, _iasi_cfg())
    images = {p.code: img for p, img in rendered[0][0]}
    # RO0015 (rotate false) keeps its white pixel on the left; RO0016 (rotate
    # true) is turned back to the sensor frame, so it moves to the right.
    assert images["RO0015"][0, 0].tolist() == [255, 255, 255]
    assert images["RO0016"][0, 1].tolist() == [255, 255, 255]


def test_render_once_location_falls_back_to_overlay_coords(monkeypatch):
    cfg = _iasi_cfg()
    del cfg["latitude"], cfg["longitude"]
    _ok, rendered, _uploads = _fake_render_pass(monkeypatch, cfg)
    kwargs = rendered[0][1]
    assert (kwargs["lat"], kwargs["lon"]) == (46.72, 27.87)
