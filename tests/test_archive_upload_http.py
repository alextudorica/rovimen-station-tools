"""Station-side HTTPS byte-plane transport (archive_upload.py).

archive_upload now POSTs archival file bytes to the VPS ingest blob endpoint
authenticated with the station ingest key — no SSH. These cover URL
construction, the ingest-readiness gate, retry/backoff, and the raw post_blob
helper.
"""

from __future__ import annotations

import time

import archive_upload
import ingest_blob
from archive_upload import _Uploader


def _uploader(tmp_path, ready: bool = True) -> _Uploader:
    up = _Uploader({
        "archive": {"enabled": True},
        "videocapture_path": str(tmp_path),
        "host_key": "gmn0002",
        "push": {"ingest_base_url": "https://vps.example"},
    })
    if ready:
        up.ingest_base_url = "https://vps.example"
        up.ingest_key = "SECRET"
        up.ingest_host_key = "gmn0002"
    else:
        up.ingest_base_url = up.ingest_key = up.ingest_host_key = None
    return up


class TestBlobUrl:
    def test_meteor_relpath(self, tmp_path):
        up = _uploader(tmp_path)
        assert up._blob_url("RO000M", "20260709", "meteors/x_color.mkv") == (
            "https://vps.example/api/ingest/v1/gmn0002/blob/"
            "RO000M/20260709/meteors/x_color.mkv"
        )

    def test_trailing_slash_base_not_doubled(self, tmp_path):
        up = _uploader(tmp_path)
        up.ingest_base_url = "https://vps.example/"
        # Only the scheme separator should contain '//'.
        assert up._blob_url("C", "20260709", "state.json").count("//") == 1


class TestIngestReady:
    def test_ready(self, tmp_path):
        assert _uploader(tmp_path, ready=True)._ingest_ready() is True

    def test_not_ready(self, tmp_path):
        assert _uploader(tmp_path, ready=False)._ingest_ready() is False


class TestPostOne:
    def test_posts_bytes_with_correct_url_and_key(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "RO000M_20260709_1_color.mkv"
        f.write_bytes(b"payload-bytes")
        seen: dict = {}
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda url, key, data, timeout=180:
                            (seen.update(url=url, key=key, data=data), 200)[1])
        assert up._post_one(f, "RO000M", "20260709", "meteors") is True
        assert seen["url"].endswith(
            "/blob/RO000M/20260709/meteors/RO000M_20260709_1_color.mkv")
        assert seen["key"] == "SECRET"
        assert seen["data"] == b"payload-bytes"

    def test_state_json_root_relpath(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "state.json"
        f.write_bytes(b"{}")
        urls: list[str] = []
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda url, key, data, timeout=180: (urls.append(url), 200)[1])
        assert up._post_one(f, "RO000M", "20260709", "") is True
        assert urls[0].endswith("/blob/RO000M/20260709/state.json")

    def test_not_ready_never_posts(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path, ready=False)
        f = tmp_path / "x_color.mkv"
        f.write_bytes(b"x")
        called: list[int] = []
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: (called.append(1), 200)[1])
        assert up._post_one(f, "C", "20260709", "meteors") is False
        assert called == []

    def test_retries_then_succeeds(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_color.mkv"
        f.write_bytes(b"x")
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        n = {"c": 0}

        def flaky(url, key, data, timeout=180):
            n["c"] += 1
            return 200 if n["c"] >= 2 else 503

        monkeypatch.setattr(archive_upload, "post_blob_status", flaky)
        assert up._post_one(f, "C", "20260709", "meteors") is True
        assert n["c"] == 2

    def test_rate_limit_waits_out_the_window_instead_of_burning_attempts(
            self, monkeypatch, tmp_path):
        """A 429 is a per-minute window, not a network blip: the generic backoff
        put all three attempts inside its first three seconds, so a dawn stack
        flush that crossed the ingest budget failed wholesale and waited for the
        next cycle. Sit out the window and the retry lands in a fresh one."""
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"x")
        slept: list[float] = []
        monkeypatch.setattr(archive_upload.time, "sleep", lambda s: slept.append(s))
        n = {"c": 0}

        def limited(url, key, data, timeout=180):
            n["c"] += 1
            return 200 if n["c"] > 2 else 429

        monkeypatch.setattr(archive_upload, "post_blob_status", limited)
        assert up._post_one(f, "C", "20260709", "stacks") is True
        assert n["c"] == 3
        assert slept == [archive_upload._BLOB_RATE_LIMIT_WAIT_S] * 2
        # The 5xx/no-response attempt budget was untouched by the rate limiting.
        assert up._is_quarantined(up._blob_url("C", "20260709", "stacks/x_stack.webp"), 1) is False

    def test_rate_limit_gives_up_after_max_waits_without_quarantine(
            self, monkeypatch, tmp_path):
        """A station over its *hourly* budget must not stall the run — but the
        file is left un-marked so the next cycle picks it up, never dropped."""
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"x")
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        n = {"c": 0}
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: (n.__setitem__("c", n["c"] + 1), 429)[1])
        assert up._post_one(f, "C", "20260709", "stacks") is False
        assert n["c"] == archive_upload._BLOB_RATE_LIMIT_MAX_WAITS + 1
        url = up._blob_url("C", "20260709", "stacks/x_stack.webp")
        assert url not in up._quarantine, "rate limiting is never a verdict on the bytes"

    def test_gives_up_after_max_attempts(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_color.mkv"
        f.write_bytes(b"x")
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        n = {"c": 0}
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: (n.__setitem__("c", n["c"] + 1), 503)[1])
        assert up._post_one(f, "C", "20260709", "meteors") is False
        assert n["c"] == archive_upload._BLOB_MAX_ATTEMPTS

    def test_spool_full_pauses_the_whole_batch(self, monkeypatch, tmp_path):
        """A 507 means the VPS spool disk is full: one POST, then no more blobs
        until the pause passes, and nothing quarantined or marked uploaded."""
        up = _uploader(tmp_path)
        files = []
        for i in range(3):
            f = tmp_path / f"x{i}_stack.webp"
            f.write_bytes(b"x")
            files.append(f)
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        n = {"c": 0}
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: (n.__setitem__("c", n["c"] + 1), 507)[1])
        assert [up._post_one(f, "C", "20260709", "stacks") for f in files] == [False] * 3
        assert n["c"] == 1
        assert up._quarantine == {}

    def test_spool_full_pause_expires(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"x")
        clock = {"t": 1000.0}
        monkeypatch.setattr(archive_upload.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        codes = iter([507, 201])
        monkeypatch.setattr(archive_upload, "post_blob_status", lambda *a, **k: next(codes))
        assert up._post_one(f, "C", "20260709", "stacks") is False
        clock["t"] += archive_upload._BLOB_SPOOL_FULL_PAUSE_S - 1
        assert up._post_one(f, "C", "20260709", "stacks") is False  # still paused, no POST
        clock["t"] += 2
        assert up._post_one(f, "C", "20260709", "stacks") is True


class TestPostBlobHelper:
    def test_2xx_ok(self, monkeypatch):
        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(ingest_blob.urllib.request, "urlopen",
                            lambda *a, **k: Resp())
        assert archive_upload.post_blob("http://x", "k", b"d") is True

    def test_http_error_false(self, monkeypatch):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("http://x", 413, "too big", {}, None)
        monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", boom)
        assert archive_upload.post_blob("http://x", "k", b"d") is False

    def test_network_error_false(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", boom)
        assert archive_upload.post_blob("http://x", "k", b"d") is False


class TestPostBlobStatus:
    """The status-returning seam under post_blob: a caller that needs to tell a
    rejected payload (4xx) from a broken link (5xx / no response) reads this."""

    def test_returns_status_on_success(self, monkeypatch):
        class Resp:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(ingest_blob.urllib.request, "urlopen",
                            lambda *a, **k: Resp())
        assert archive_upload.post_blob_status("http://x", "k", b"d") == 201

    def test_returns_status_on_http_error(self, monkeypatch):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("http://x", 422, "empty", {}, None)
        monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", boom)
        assert archive_upload.post_blob_status("http://x", "k", b"d") == 422

    def test_network_error_is_no_response_not_a_status(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", boom)
        assert (archive_upload.post_blob_status("http://x", "k", b"d")
                == archive_upload._BLOB_NO_RESPONSE)


class TestZeroByteSourceNeverUploads:
    """gmnro10 (RO000E/20260818): five 0-byte ``*_stack.webp`` were POSTed ~186
    times each per day, every one answered 422 empty_blob. An empty source can
    only ever be rejected, so it must never cost a request."""

    def test_zero_byte_file_is_never_posted(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "RO000E_20260818_215030_stack.webp"
        f.write_bytes(b"")
        calls: list[str] = []
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda url, *a, **k: (calls.append(url), 422)[1])
        assert up._post_one(f, "RO000E", "20260818", "stacks") is False
        assert calls == []

    def test_repeated_cycles_log_the_reason_once(self, monkeypatch, tmp_path, caplog):
        up = _uploader(tmp_path)
        f = tmp_path / "RO000E_20260818_230049_stack.webp"
        f.write_bytes(b"")
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: 422)
        with caplog.at_level("WARNING", logger=archive_upload.logger.name):
            for _ in range(20):
                up._post_one(f, "RO000E", "20260818", "stacks")
        giving_up = [r for r in caplog.records if "giving up" in r.getMessage()]
        assert len(giving_up) == 1
        assert "0 bytes" in giving_up[0].getMessage()

    def test_regenerated_file_uploads_again(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "RO000E_20260818_215030_stack.webp"
        f.write_bytes(b"")
        monkeypatch.setattr(archive_upload, "post_blob_status", lambda *a, **k: 200)
        assert up._post_one(f, "RO000E", "20260818", "stacks") is False
        # The stacker re-renders the stack — the new bytes get a fresh verdict.
        f.write_bytes(b"real-webp-bytes")
        assert up._post_one(f, "RO000E", "20260818", "stacks") is True


class TestPermanentRejectionStopsRetrying:
    """A 4xx is a verdict on the payload: identical bytes can only be rejected
    identically, so the file is parked instead of re-POSTed every cycle."""

    def _counting(self, monkeypatch, status: int) -> list[int]:
        calls: list[int] = []
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: (calls.append(status), status)[1])
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        return calls

    def test_4xx_is_not_retried_within_the_attempt_loop(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        calls = self._counting(monkeypatch, 422)
        assert up._post_one(f, "C", "20260709", "stacks") is False
        assert len(calls) == 1

    def test_4xx_is_not_retried_on_later_cycles(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        calls = self._counting(monkeypatch, 422)
        for _ in range(10):
            up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == 1

    def test_invalid_path_400_also_parks(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        calls = self._counting(monkeypatch, 413)
        for _ in range(5):
            up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == 1

    def test_429_stays_transient_and_keeps_retrying(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        calls = self._counting(monkeypatch, 429)
        assert up._post_one(f, "C", "20260709", "stacks") is False
        assert len(calls) == archive_upload._BLOB_MAX_ATTEMPTS
        # …and the next cycle tries again — rate limiting is not a verdict.
        up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == 2 * archive_upload._BLOB_MAX_ATTEMPTS

    def test_5xx_is_never_a_verdict_on_the_file(self, monkeypatch, tmp_path):
        """A 5xx never parks the file. A run of them trips the breaker, which
        holds every upload for a while; a file that keeps failing is also set
        aside for an hour (so it cannot hold the queue), then tries again."""
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        calls = self._counting(monkeypatch, 503)
        clock = {"t": 1000.0}
        monkeypatch.setattr(archive_upload.time, "monotonic", lambda: clock["t"])
        for _ in range(3):
            up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == ingest_blob.BREAKER_FAILURES
        assert up._quarantine == {}
        assert up._quarantine == {}
        assert up._sender.is_deferred(up._blob_url("C", "20260709", "stacks/x_stack.webp"), 5)
        clock["t"] += ingest_blob.BREAKER_PAUSE_S + 1
        up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == ingest_blob.BREAKER_FAILURES, "still set aside"
        clock["t"] += ingest_blob.FILE_DEFER_S
        up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == ingest_blob.BREAKER_FAILURES + 1

    def test_quarantine_expires_so_the_file_gets_one_more_try(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        calls = self._counting(monkeypatch, 422)
        assert up._post_one(f, "C", "20260709", "stacks") is False
        clock = {"t": time.monotonic() + archive_upload._BLOB_PERMANENT_RETRY_AFTER_S + 1}
        monkeypatch.setattr(archive_upload.time, "monotonic", lambda: clock["t"])
        up._post_one(f, "C", "20260709", "stacks")
        assert len(calls) == 2

    def test_success_clears_a_previous_park(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_stack.webp"
        f.write_bytes(b"bytes")
        statuses = [500, 500, 500, 200]
        monkeypatch.setattr(archive_upload.time, "sleep", lambda *_: None)
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda *a, **k: statuses.pop(0))
        assert up._post_one(f, "C", "20260709", "stacks") is False
        assert up._post_one(f, "C", "20260709", "stacks") is True
        assert up._quarantine == {}


class TestTransientBackoff:
    def test_backoff_grows_and_is_capped(self, monkeypatch, tmp_path):
        up = _uploader(tmp_path)
        f = tmp_path / "x_color.mkv"
        f.write_bytes(b"bytes")
        slept: list[float] = []
        monkeypatch.setattr(archive_upload.time, "sleep", slept.append)
        monkeypatch.setattr(archive_upload, "post_blob_status", lambda *a, **k: 500)
        monkeypatch.setattr(ingest_blob, "MAX_ATTEMPTS", 6)
        monkeypatch.setattr(ingest_blob, "BREAKER_FAILURES", 99)
        monkeypatch.setattr(ingest_blob, "BACKOFF_CAP_S", 4.0)
        up._post_one(f, "C", "20260709", "meteors")
        assert slept == [1.0, 2.0, 4.0, 4.0, 4.0]


class TestFindRmsFilesBothDirs:
    """_find_rms_files reads CapturedFiles too (RMS writes there first), with
    the ArchivedFiles copy winning on name collisions."""

    def _cfg(self, tmp_path):
        return {
            "archive": {"enabled": True},
            "videocapture_path": str(tmp_path / "cap"),
            "stations": {"RO000H": {"rms_data_path": str(tmp_path / "rms")}},
        }

    def _session(self, tmp_path, sub, ftp_body):
        d = tmp_path / "rms" / sub / "RO000H_20260716_210000_000000"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "FTPdetectinfo_RO000H_20260716_210000_000000.txt"
        f.write_text(ftp_body)
        return f

    def test_capturedfiles_only(self, tmp_path):
        up = _Uploader(self._cfg(tmp_path))
        self._session(tmp_path, "CapturedFiles", "captured")
        files = up._find_rms_files("RO000H", "20260716")
        assert [f.read_text() for f in files] == ["captured"]

    def test_archived_copy_wins(self, tmp_path):
        up = _Uploader(self._cfg(tmp_path))
        self._session(tmp_path, "CapturedFiles", "captured")
        self._session(tmp_path, "ArchivedFiles", "archived")
        files = up._find_rms_files("RO000H", "20260716")
        assert [f.read_text() for f in files] == ["archived"]
