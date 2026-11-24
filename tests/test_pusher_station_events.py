"""Station side of the event history (rovimen-scripts/rovimen_pusher.py):
the boot / clean-stop tracker, the bounded event queue, the uplink log tailer,
the watchdog's uplink class, and delivery to the VPS."""

from __future__ import annotations

import json
import os

import pytest

import rovimen_pusher as rp


class _Clock:
    def __init__(self, now: float, uptime: float, synced: bool | None = True) -> None:
        self.now = now
        self.uptime = uptime
        self.synced = synced

    def tracker(self, tmp_path, boot_id: str, state: str = "running") -> rp.BootTracker:
        return rp.BootTracker(
            tmp_path / "alive.json", tmp_path / "stop.json",
            boot_id_fn=lambda: boot_id, uptime_fn=lambda: self.uptime,
            clock_fn=lambda: self.synced, now_fn=lambda: self.now,
            state_fn=lambda: state,
        )


T0 = 1_794_600_000.0  # 2026-11-12, ordinary epoch for the stamps




class TestBootTracker:
    def test_first_run_sends_no_boot_event(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, "boot-a")
        tr.tick(q)
        assert q.count() == 0
        alive = json.loads((tmp_path / "alive.json").read_text())
        assert alive["boot_id"] == "boot-a" and alive["trusted"] is True

    def test_service_restart_same_boot_is_not_a_boot(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, "boot-a")
        tr.tick(q)
        tr.record_stop()  # systemctl restart: SIGTERM, system "running"
        assert (tmp_path / "stop.json").exists()
        clock.now += 20
        tr2 = clock.tracker(tmp_path, "boot-a")
        tr2.tick(q)
        assert q.count() == 0
        assert not (tmp_path / "stop.json").exists(), "a restart discards its own marker"

    def test_new_boot_without_marker_is_unclean_with_downtime(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        clock.tracker(tmp_path, "boot-a").tick(q)   # last alive at T0
        # Power cut; back 2 h later, booted 120 s ago.
        clock.now = T0 + 7200 + 120
        clock.uptime = 120
        tr = clock.tracker(tmp_path, "boot-b")
        tr.tick(q)
        [ev] = q.pending()
        assert ev["id"] == "boot-boot-b" and ev["kind"] == "boot"
        d = ev["detail"]
        assert d["previous_end"] == "unclean"
        assert d["down_s"] == 7200
        assert d["previous_boot_id"] == "boot-a"
        assert "abruptly" in d["text"] and "2 h 0 min" in d["text"]
        assert ev["ts"] == d["boot_time"] == rp._iso_utc(T0 + 7200)
        assert tr.boot_block()["previous"]["end"] == "unclean"

    def test_new_boot_after_shutdown_marker_is_clean(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, "boot-a", state="stopping")
        tr.tick(q)
        tr.record_stop()   # SIGTERM during the 13:45 reboot
        clock.now = T0 + 90
        clock.uptime = 30
        tr2 = clock.tracker(tmp_path, "boot-b")
        tr2.tick(q)
        d = q.pending()[0]["detail"]
        assert d["previous_end"] == "clean" and d["down_s"] == 60
        assert not (tmp_path / "stop.json").exists()

    def test_marker_from_hand_stop_is_unknown(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, "boot-a", state="running")
        tr.tick(q)
        tr.record_stop()   # systemctl stop rovimen-pusher, then a power cut
        clock.now, clock.uptime = T0 + 600, 60
        tr2 = clock.tracker(tmp_path, "boot-b")
        tr2.tick(q)
        assert q.pending()[0]["detail"]["previous_end"] == "unknown"

    def test_unsynced_clock_waits_and_keeps_previous_stamp(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        clock.tracker(tmp_path, "boot-a").tick(q)
        # Pi without RTC: boots with a clock from the past.
        clock.now, clock.uptime, clock.synced = T0 - 86400, 40, False
        tr = clock.tracker(tmp_path, "boot-b")
        tr.tick(q)
        assert q.count() == 0
        assert tr.boot_block()["pending"] is True
        assert json.loads((tmp_path / "alive.json").read_text())["boot_id"] == "boot-a"
        # A pusher restart before NTP answers classifies the same way.
        tr = clock.tracker(tmp_path, "boot-b")
        # NTP answers: real time is 3 h after the last stamp, 200 s uptime.
        clock.now, clock.uptime, clock.synced = T0 + 10800, 200, True
        tr._clock_checked = float("-inf")
        tr.tick(q)
        d = q.pending()[0]["detail"]
        assert d["down_s"] == 10800 - 200
        assert d["clock"] == "synced"
        assert json.loads((tmp_path / "alive.json").read_text())["boot_id"] == "boot-b"

    def test_never_synced_sends_after_wait_without_downtime(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        clock.tracker(tmp_path, "boot-a").tick(q)
        clock.now, clock.uptime, clock.synced = T0 - 5000, rp.BOOT_CLOCK_WAIT_S + 1, False
        tr = clock.tracker(tmp_path, "boot-b")
        tr.tick(q)
        d = q.pending()[0]["detail"]
        assert d["down_s"] is None and d["boot_time"] is None
        assert d["clock"] == "unsynced" and d["previous_end"] == "unclean"
        assert "downtime unknown" in d["text"]
        # Its stamp is marked untrusted, so the NEXT boot gets no downtime either.
        assert json.loads((tmp_path / "alive.json").read_text())["trusted"] is False

    def test_stop_during_pending_keeps_previous_boot_marker(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, "boot-a", state="stopping")
        tr.tick(q)
        tr.record_stop()
        clock.now, clock.uptime, clock.synced = T0 - 999, 30, False
        tr = clock.tracker(tmp_path, "boot-b", state="running")
        tr.tick(q)
        tr.record_stop()  # updater restarts the pusher before NTP answers
        clock.synced = True
        clock.now, clock.uptime = T0 + 400, 100
        tr = clock.tracker(tmp_path, "boot-b")
        tr.tick(q)
        assert q.pending()[0]["detail"]["previous_end"] == "clean"

    def test_alive_stamp_rate_limited(self, tmp_path):
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, "boot-a")
        tr.tick(None)
        first = json.loads((tmp_path / "alive.json").read_text())["last_alive"]
        clock.now += 30
        tr.tick(None)
        assert json.loads((tmp_path / "alive.json").read_text())["last_alive"] == first
        clock.now += rp.ALIVE_EVERY
        tr.tick(None)
        assert json.loads((tmp_path / "alive.json").read_text())["last_alive"] == clock.now

    def test_no_boot_id_disables_tracking(self, tmp_path):
        clock = _Clock(T0, 300)
        tr = clock.tracker(tmp_path, None)
        tr.tick(rp.EventQueue(tmp_path / "q.db"))
        tr.record_stop()
        assert tr.boot_block() is None
        assert not (tmp_path / "alive.json").exists()


class TestEventQueue:
    def _ev(self, i):
        return {"id": f"e{i}", "ts": "2026-11-13T02:00:00Z", "kind": "uplink",
                "detail": {"text": f"line {i}"}}

    def test_survives_restart(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        q.add(self._ev(1))
        q.close()
        q2 = rp.EventQueue(tmp_path / "q.db")
        assert q2.pending() == [self._ev(1)]

    def test_bounded_keeps_newest(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db", max_rows=3)
        for i in range(5):
            q.add(self._ev(i))
        assert [e["id"] for e in q.pending()] == ["e2", "e3", "e4"]

    def test_duplicate_id_not_queued_twice(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        assert q.add(self._ev(1)) is True
        assert q.add(self._ev(1)) is False
        assert q.count() == 1

    def test_remove(self, tmp_path):
        q = rp.EventQueue(tmp_path / "q.db")
        for i in range(3):
            q.add(self._ev(i))
        q.remove(["e0", "e2"])
        assert [e["id"] for e in q.pending()] == ["e1"]


class TestUplinkLog:
    LINES = [
        "2026-11-13T02:00:00Z uplink ok -> isp after 3 d 1 h (gateway 192.168.1.1 on eth0)\n",
        "2026-11-13T02:06:00Z rescue wifi up on wlan0: cable uplink failed 2 runs\n",
        "2026-11-13T05:00:00Z uplink isp -> ok after 3 h 0 min (gateway 192.168.1.1 on eth0)\n",
    ]

    def test_parse_line_kinds_and_stable_id(self):
        ev = rp.parse_uplink_log_line(self.LINES[0])
        assert ev["kind"] == "uplink" and ev["ts"] == "2026-11-13T02:00:00Z"
        assert ev["detail"]["text"].startswith("uplink ok -> isp")
        assert ev["id"] == rp.parse_uplink_log_line(self.LINES[0])["id"]
        assert rp.parse_uplink_log_line(self.LINES[1])["kind"] == "rescue_wifi"
        assert rp.parse_uplink_log_line("free text")["ts"] is None
        assert rp.parse_uplink_log_line("   ") is None

    def test_tails_new_lines_once(self, tmp_path):
        log = tmp_path / "uplink_events.log"
        q = rp.EventQueue(tmp_path / "q.db")
        tail = rp.UplinkLogTailer(log, tmp_path / "state.json")
        assert tail.poll(q) == 0  # no log yet
        log.write_text(self.LINES[0])
        assert tail.poll(q) == 1
        assert tail.poll(q) == 0
        with open(log, "a") as fh:
            fh.write(self.LINES[1] + "2026-11-13T05:00:00Z partial")
        assert tail.poll(q) == 1, "an unterminated line waits"
        with open(log, "a") as fh:
            fh.write(" line\n")
        assert tail.poll(q) == 1
        assert q.count() == 3

    def test_restart_resumes_and_lost_state_is_deduplicated(self, tmp_path):
        log = tmp_path / "uplink_events.log"
        log.write_text("".join(self.LINES))
        q = rp.EventQueue(tmp_path / "q.db")
        rp.UplinkLogTailer(log, tmp_path / "state.json").poll(q)
        assert rp.UplinkLogTailer(log, tmp_path / "state.json").poll(q) == 0
        os.remove(tmp_path / "state.json")
        assert rp.UplinkLogTailer(log, tmp_path / "state.json").poll(q) == 0
        assert q.count() == 3

    def test_watchdog_trim_resumes_after_last_line(self, tmp_path):
        # rovimen_watchdog.sh keeps the newest UPLINK_EVENTS_KEEP lines by
        # writing a new file (tail > tmp && mv): a new inode, old lines kept.
        log = tmp_path / "uplink_events.log"
        log.write_text("".join(self.LINES))
        tail = rp.UplinkLogTailer(log, tmp_path / "state.json")
        tail.poll(rp.EventQueue(tmp_path / "q1.db"))
        new_line = "2026-11-14T01:00:00Z uplink ok -> router after 20 h 0 min\n"
        tmp = tmp_path / "uplink_events.log.tmp"
        tmp.write_text("".join(self.LINES[1:]) + new_line)
        tmp.replace(log)
        q2 = rp.EventQueue(tmp_path / "q2.db")
        assert tail.poll(q2) == 1
        assert q2.pending()[0]["detail"]["text"].startswith("uplink ok -> router")

    def test_truncated_log_restarts_at_zero(self, tmp_path):
        log = tmp_path / "uplink_events.log"
        log.write_text("".join(self.LINES))
        q = rp.EventQueue(tmp_path / "q.db")
        tail = rp.UplinkLogTailer(log, tmp_path / "state.json")
        tail.poll(q)
        log.write_text("2026-11-20T01:00:00Z uplink ok (first run; gateway none on none)\n")
        assert tail.poll(q) == 1


class TestUplinkState:
    def test_reads_watchdog_state(self, tmp_path):
        p = tmp_path / ".watchdog_state"
        p.write_text("tailscale_fail=0\nuplink_state=isp\nuplink_since=1794621600\n")
        assert rp.read_uplink_state(p) == {"class": "isp", "since": "2026-11-14T02:00:00Z"}

    def test_missing_or_unclassified(self, tmp_path):
        assert rp.read_uplink_state(tmp_path / "nope") is None
        p = tmp_path / ".watchdog_state"
        p.write_text("tailscale_fail=0\n")
        assert rp.read_uplink_state(p) is None


class TestDelivery:
    def _publisher(self, tmp_path, monkeypatch, results):
        sent = []

        def fake_post(url, key, body, timeout=rp.HTTP_TIMEOUT):
            sent.append((url, body))
            return results.pop(0) if results else rp.PostResult(ok=True, status=200)

        monkeypatch.setattr(rp, "post_json", fake_post)
        q = rp.EventQueue(tmp_path / "q.db")
        pub = rp.Publisher("https://vps", "gmnro09", "k", rp.SeqCounter(tmp_path / "s.json"),
                           rp.Spool(tmp_path / "spool.db"), events=q)
        return pub, q, sent

    def _fill(self, q, n):
        for i in range(n):
            q.add({"id": f"e{i}", "ts": None, "kind": "uplink", "detail": None})

    def test_delivers_in_batches_and_empties_queue(self, tmp_path, monkeypatch):
        pub, q, sent = self._publisher(tmp_path, monkeypatch, [])
        self._fill(q, rp.EVENTS_BATCH + 5)
        assert pub.push_events() is True
        assert q.count() == 0
        assert [len(b["payload"]["events"]) for _, b in sent] == [rp.EVENTS_BATCH, 5]
        assert sent[0][0] == "https://vps/api/ingest/v1/gmnro09/events"

    def test_outage_keeps_events_for_later(self, tmp_path, monkeypatch):
        pub, q, sent = self._publisher(tmp_path, monkeypatch,
                                       [rp.PostResult(ok=False, status=None)])
        self._fill(q, 3)
        assert pub.push_events() is False
        assert q.count() == 3
        # Back online: the same ids go again; the VPS dedups any it already had.
        assert pub.push_events() is True
        assert q.count() == 0
        assert [e["id"] for e in sent[1][1]["payload"]["events"]] == ["e0", "e1", "e2"]

    def test_old_vps_404_keeps_queue(self, tmp_path, monkeypatch):
        pub, q, _ = self._publisher(tmp_path, monkeypatch, [rp.PostResult(ok=False, status=404)])
        self._fill(q, 2)
        assert pub.push_events() is True
        assert q.count() == 2

    def test_422_batch_is_dropped_not_retried_forever(self, tmp_path, monkeypatch):
        pub, q, _ = self._publisher(tmp_path, monkeypatch, [rp.PostResult(ok=False, status=422)])
        self._fill(q, 2)
        pub.push_events()
        assert q.count() == 0

    def test_tails_uplink_log_before_sending(self, tmp_path, monkeypatch):
        pub, q, sent = self._publisher(tmp_path, monkeypatch, [])
        log = tmp_path / "uplink_events.log"
        log.write_text("2026-11-13T02:00:00Z uplink ok -> link after 1 h 0 min\n")
        pub._uplink_tailer = rp.UplinkLogTailer(log, tmp_path / "st.json")
        assert pub.push_events() is True
        assert sent[0][1]["payload"]["events"][0]["kind"] == "uplink"

    def test_publisher_without_history_is_a_noop(self, tmp_path):
        pub = rp.Publisher("https://vps", "gmnro09", "k", rp.SeqCounter(tmp_path / "s.json"),
                           rp.Spool(tmp_path / "spool.db"))
        assert pub.push_events() is True

    def test_heartbeat_carries_uplink_and_boot(self, tmp_path, monkeypatch):
        pub, _, sent = self._publisher(tmp_path, monkeypatch, [])
        state = tmp_path / ".watchdog_state"
        state.write_text("uplink_state=router\nuplink_since=1794621600\n")
        pub._watchdog_state_path = state
        clock = _Clock(T0, 500)
        pub._boot = clock.tracker(tmp_path, "boot-a")
        monkeypatch.setattr(rp, "fetch_status", lambda: {
            "online": True, "disk_pressure": {"since": "2026-11-13T00:00:00Z"}})
        assert pub.push_heartbeat() is True
        payload = sent[0][1]["payload"]
        assert payload["uplink"]["class"] == "router"
        assert payload["boot"]["boot_id"] == "boot-a" and payload["boot"]["uptime_s"] == 500
        assert payload["disk_pressure"]["since"] == "2026-11-13T00:00:00Z"


def test_clock_synchronized_handles_missing_timedatectl(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("timedatectl")
    monkeypatch.setattr(rp.subprocess, "run", boom)
    assert rp.clock_synchronized() is None
    assert rp.system_state() is None
