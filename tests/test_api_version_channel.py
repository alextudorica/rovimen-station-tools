"""``/api/version`` must report the bundle channel of the tier serving it.

The route used to read a hard-coded ``/opt/rovimen/station-bundle/.version``, so
the dev tier at ``/opt/rovimen-dev`` reported the PROD build number — the exact
number operators compare against a station's installed bundle when deciding
whether an update landed. Both channels are staged under the prod tree (CI
pushes the dev bundle to ``/opt/rovimen/station-bundle-dev``), so the base dir is
shared and only the channel sub-directory is tier-specific.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from routes import network


@pytest.fixture
def bundle_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for channel, version in (
        ("station-bundle", "1234.2026.08.25"),
        ("station-bundle-dev", "dev.5678.2026.08.25"),
    ):
        d = tmp_path / channel
        d.mkdir()
        (d / ".version").write_text(version + "\n")
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(tmp_path))
    return tmp_path


def test_defaults_to_the_main_channel(bundle_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROVIMEN_BUNDLE_CHANNEL", raising=False)
    assert network.bundle_version() == "1234.2026.08.25"


def test_dev_channel_reports_the_dev_bundle(bundle_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROVIMEN_BUNDLE_CHANNEL", "dev")
    assert network.bundle_version() == "dev.5678.2026.08.25"


def test_dev_channel_is_a_different_build_than_prod(bundle_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROVIMEN_BUNDLE_CHANNEL", "main")
    prod = network.bundle_version()
    monkeypatch.setenv("ROVIMEN_BUNDLE_CHANNEL", "dev")
    assert network.bundle_version() != prod


def test_missing_version_file_is_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(tmp_path))
    monkeypatch.delenv("ROVIMEN_BUNDLE_CHANNEL", raising=False)
    assert network.bundle_version() == "unknown"


def test_channel_inferred_from_a_dev_install_root(monkeypatch, tmp_path: Path) -> None:
    """With no explicit channel, an /opt/rovimen-dev install picks the dev bundle."""
    monkeypatch.delenv("ROVIMEN_BUNDLE_CHANNEL", raising=False)
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(tmp_path))
    fake_module = tmp_path / "rovimen-dev" / "routes" / "network.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    monkeypatch.setattr(network, "__file__", str(fake_module))
    assert network._bundle_version_file().name == ".version"
    assert network._bundle_version_file().parent.name == "station-bundle-dev"
