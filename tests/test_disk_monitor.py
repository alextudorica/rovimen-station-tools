"""The VPS disk monitor must alert only on threshold *crossings* (not every
run) so it can't spam, and must never fail the run on a Telegram blip."""

from __future__ import annotations

import json
import types

import disk_monitor as dm


def test_usage_returns_percent(tmp_path):
    u = dm.usage(str(tmp_path))
    assert u is not None
    assert 0 <= u["used_pct"] <= 100
    assert u["free_gb"] >= 0


def test_alerts_only_on_crossing(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    monkeypatch.setattr(dm, "STATUS_PATH", str(status))
    monkeypatch.setattr(dm, "VOLUMES", [("box", "/box", 90, False)])
    sent: list[str] = []
    monkeypatch.setattr(dm, "_telegram", lambda text: sent.append(text))

    pct = {"v": 95.0}

    def fake_usage(_p):
        used = int(10 * pct["v"])
        return types.SimpleNamespace(total=1000, used=used, free=1000 - used)

    monkeypatch.setattr(dm.shutil, "disk_usage", fake_usage)

    dm.main()                                   # 95% -> newly over -> alert
    assert len(sent) == 1 and "over" in sent[0].lower()
    assert json.loads(status.read_text())["over"]["box"] is True

    dm.main()                                   # still 95% -> no repeat alert
    assert len(sent) == 1

    pct["v"] = 80.0
    dm.main()                                   # recovered -> one recovery alert
    assert len(sent) == 2 and "recover" in sent[1].lower()
    assert json.loads(status.read_text())["over"]["box"] is False


def test_telegram_failure_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setenv("ROVIMEN_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ROVIMEN_TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(dm.urllib.request, "urlopen", boom)
    dm._telegram("hi")   # must swallow the error


def _only(label: str) -> list:
    return [v for v in dm.VOLUMES if v[0] == label]


def _fake_disk_usage(pct: dict) -> object:
    def fake(_p):
        used = int(10 * pct["v"])
        return types.SimpleNamespace(total=1000, used=used, free=1000 - used)
    return fake


def test_unmounted_box_keeps_its_flag_and_alerts_once_per_outage(tmp_path, monkeypatch):
    """An unmounted sshfs mount point is a plain directory on root, so
    disk_usage() there reports root's usage. Before the mount check that read
    as "storage_box recovered" while the box was gone."""
    status = tmp_path / "status.json"
    monkeypatch.setattr(dm, "STATUS_PATH", str(status))
    monkeypatch.setattr(dm, "VOLUMES", _only("storage_box"))
    sent: list[str] = []
    monkeypatch.setattr(dm, "_telegram", lambda text: sent.append(text))
    mounted = {"v": True}
    monkeypatch.setattr(dm.os.path, "ismount", lambda _p: mounted["v"])
    pct = {"v": 95.0}
    monkeypatch.setattr(dm.shutil, "disk_usage", _fake_disk_usage(pct))

    dm.main()                                   # box 95% -> over
    assert len(sent) == 1 and "over" in sent[0].lower()

    mounted["v"] = False                        # box gone; root reads 40%
    pct["v"] = 40.0
    dm.main()
    assert len(sent) == 2
    assert "not mounted" in sent[1] and "recover" not in sent[1].lower()
    st = json.loads(status.read_text())
    assert st["over"]["storage_box"] is True    # flag kept, not reset by root's usage
    assert st["volumes"]["storage_box"]["mounted"] is False

    dm.main()                                   # same outage -> no repeat
    assert len(sent) == 2
    assert json.loads(status.read_text())["over"]["storage_box"] is True

    mounted["v"] = True                         # back, still 95%
    pct["v"] = 95.0
    dm.main()
    assert len(sent) == 3
    assert "mounted again" in sent[2] and "over" not in sent[2].lower()
    assert json.loads(status.read_text())["over"]["storage_box"] is True

    pct["v"] = 80.0                             # a real recovery is still reported
    dm.main()
    assert len(sent) == 4 and "recover" in sent[3].lower()


def test_volume_unmounted_on_the_first_run_alerts_without_a_flag(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    monkeypatch.setattr(dm, "STATUS_PATH", str(status))
    monkeypatch.setattr(dm, "VOLUMES", _only("thumb_cache_ssd"))
    sent: list[str] = []
    monkeypatch.setattr(dm, "_telegram", lambda text: sent.append(text))
    monkeypatch.setattr(dm.os.path, "ismount", lambda _p: False)
    monkeypatch.setattr(dm.shutil, "disk_usage", _fake_disk_usage({"v": 95.0}))

    dm.main()
    assert len(sent) == 1 and "thumb_cache_ssd not mounted" in sent[0]
    assert "thumb_cache_ssd" not in json.loads(status.read_text())["over"]


def test_root_is_never_mount_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(dm, "VOLUMES", _only("root"))
    sent: list[str] = []
    monkeypatch.setattr(dm, "_telegram", lambda text: sent.append(text))
    monkeypatch.setattr(dm.os.path, "ismount", lambda _p: False)
    monkeypatch.setattr(dm.shutil, "disk_usage", _fake_disk_usage({"v": 95.0}))

    dm.main()
    assert len(sent) == 1 and "root" in sent[0] and "not mounted" not in sent[0]
