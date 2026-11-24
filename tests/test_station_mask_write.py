"""Tests for the station-side browser mask editor's write path
(station_api.py's ``/api/mask/<cam>``) and the mask-location resolution it
shares with the read path.

The location matters as much as the write: ``_find_latest_mask`` used to look
only under ``~/source/Stations/<cam>/`` plus the per-night session copies,
and several stations have no ``~/source`` tree at all -- the mask RMS
actually loads lives in the camera's RMS config directory
(``~/RMS_cam2/mask.bmp``, confirmed in the capture logs). Writing anywhere
else produces a file nothing ever reads.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import station_api as sa  # noqa: E402

PIL = pytest.importorskip('PIL.Image')


def _png_b64(width: int, height: int, masked_rows: int = 0) -> str:
    """A greyscale PNG: the top *masked_rows* rows black (masked), rest white."""
    from PIL import Image
    img = Image.new('L', (width, height), 255)
    for y in range(masked_rows):
        for x in range(width):
            img.putpixel((x, y), 0)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def station(tmp_path, monkeypatch):
    """A station laid out the way the fleet is: RMS config dir per camera,
    holding .config and mask.bmp."""
    monkeypatch.setattr(sa, 'BASE', tmp_path)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config_dir = tmp_path / 'RMS_cam2'
    config_dir.mkdir()
    (config_dir / '.config').write_text('stationID: RO000H\nwidth: 1280\nheight: 720\n')
    cfg = {
        'stations': {
            'RO000H': {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam2')},
        },
    }
    monkeypatch.setattr(sa, 'load_config', lambda: cfg)
    return {'cfg': cfg, 'config_dir': config_dir, 'tmp': tmp_path}


@pytest.fixture
def client():
    return sa.app.test_client()


class TestMaskLocation:

    def test_config_dir_derived_from_rms_data_path(self, station):
        assert sa._rms_config_dir('RO000H', station['cfg']) == station['config_dir']

    def test_explicit_override_wins(self, station, tmp_path):
        station['cfg']['stations']['RO000H']['rms_config_path'] = str(tmp_path / 'elsewhere')
        assert sa._rms_config_dir('RO000H', station['cfg']) == tmp_path / 'elsewhere'

    def test_write_target_is_the_config_dir_mask(self, station):
        assert sa._mask_write_target('RO000H', station['cfg']) == station['config_dir'] / 'mask.bmp'

    def test_find_latest_prefers_config_dir_over_a_newer_session_copy(self, station, tmp_path):
        """The regression this guards: a session copy is a snapshot RMS took
        at capture start, so it can be NEWER than the live mask while not
        being the file RMS reads. Newest-wins pooling picked it and the page
        showed a mask the camera was not using."""
        live = station['config_dir'] / 'mask.bmp'
        live.write_bytes(b'live')
        session = tmp_path / 'RMS_data' / 'cam2' / 'ArchivedFiles' / 'RO000H_20260920_180000_000000'
        session.mkdir(parents=True)
        copy = session / 'mask.bmp'
        copy.write_bytes(b'copy')
        import os
        import time
        now = time.time()
        os.utime(live, (now - 3600, now - 3600))   # live mask is OLDER
        os.utime(copy, (now, now))
        assert sa._find_latest_mask('RO000H', station['cfg']) == live

    def test_falls_back_to_session_copy_when_no_config_dir_mask(self, station, tmp_path):
        session = tmp_path / 'RMS_data' / 'cam2' / 'CapturedFiles' / 'RO000H_20260920_180000_000000'
        session.mkdir(parents=True)
        (session / 'mask.bmp').write_bytes(b'copy')
        assert sa._find_latest_mask('RO000H', station['cfg']) == session / 'mask.bmp'


class TestFrameSize:

    def test_reads_width_and_height_from_rms_config(self, station):
        assert sa._rms_frame_size('RO000H', station['cfg']) == (1280, 720)

    def test_missing_config_file_returns_none(self, station):
        (station['config_dir'] / '.config').unlink()
        assert sa._rms_frame_size('RO000H', station['cfg']) is None


class TestMaskWrite:

    def test_writes_a_bmp_at_the_cameras_capture_resolution(self, station, client):
        from PIL import Image
        resp = client.post('/api/mask/RO000H', json={'png_b64': _png_b64(640, 360, masked_rows=90)})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['ok'] is True
        # Uploaded at half size; the station resamples to what RMS captures.
        assert (body['width'], body['height']) == (1280, 720)
        written = station['config_dir'] / 'mask.bmp'
        with Image.open(written) as im:
            assert im.size == (1280, 720)
            assert im.getpixel((10, 10)) == 0        # masked band
            assert im.getpixel((10, 700)) == 255     # active sky
        assert body['masked_pct'] == pytest.approx(25.0, abs=0.5)

    def test_previous_mask_is_kept_as_a_backup(self, station, client):
        live = station['config_dir'] / 'mask.bmp'
        live.write_bytes(b'BM-the-old-one')
        client.post('/api/mask/RO000H', json={'png_b64': _png_b64(1280, 720)})
        backups = list(station['config_dir'].glob('mask.bmp.bak.*'))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b'BM-the-old-one'

    def test_midtones_resolve_to_sky_not_mask(self, station, client):
        """A soft brush edge must not quietly eat FOV: RMS treats any
        non-zero pixel as observable, so anything not firmly dark is sky."""
        from PIL import Image
        img = Image.new('L', (1280, 720), 200)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        resp = client.post('/api/mask/RO000H', json={
            'png_b64': base64.b64encode(buf.getvalue()).decode()})
        assert resp.get_json()['masked_pct'] == 0.0

    def test_unknown_camera_is_rejected(self, station, client):
        resp = client.post('/api/mask/RO0099', json={'png_b64': _png_b64(16, 16)})
        assert resp.status_code == 404

    def test_bad_camera_code_is_rejected(self, station, client):
        assert client.post('/api/mask/../etc', json={'png_b64': 'x'}).status_code in (400, 404)

    def test_missing_payload_is_rejected(self, station, client):
        resp = client.post('/api/mask/RO000H', json={})
        assert resp.status_code == 400
        assert 'png_b64' in resp.get_json()['error']

    def test_non_image_payload_is_rejected_without_writing(self, station, client):
        resp = client.post('/api/mask/RO000H', json={
            'png_b64': base64.b64encode(b'not an image at all').decode()})
        assert resp.status_code == 400
        assert not (station['config_dir'] / 'mask.bmp').exists()

    def test_invalid_base64_is_rejected(self, station, client):
        resp = client.post('/api/mask/RO000H', json={'png_b64': 'not base64!!'})
        assert resp.status_code == 400

    def test_oversized_payload_is_rejected(self, station, client):
        resp = client.post('/api/mask/RO000H', json={'png_b64': 'A' * 8_000_001})
        assert resp.status_code == 413


class TestReadonlyGate:
    """The mask write is a mutation, and must still be reachable from the
    dashboard when the station is in read-only mode -- the whole feature is a
    button in a browser that is not on loopback."""

    def test_is_declared_mutating(self):
        methods = {m for r in sa.app.url_map.iter_rules()
                   if r.endpoint == 'api_mask_write' for m in r.methods}
        assert 'POST' in methods

    def test_is_network_allowed(self):
        assert 'api_mask_write' in sa._NETWORK_ALLOWED_ENDPOINTS

    def test_the_mask_read_needs_no_carve_out(self):
        assert 'api_mask_image' not in sa._NETWORK_ALLOWED_ENDPOINTS
