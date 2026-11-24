"""Tests for config_migrate.py — merge new default fields into config.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config_migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """Set up a temporary config.json and config_defaults.json.

    Returns a helper object with paths and convenience methods.
    """
    config_path = tmp_path / "config.json"
    defaults_path = tmp_path / "config_defaults.json"

    monkeypatch.setattr(config_migrate, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_migrate, "DEFAULTS_PATH", defaults_path)

    class Env:
        cfg = config_path
        defaults = defaults_path

        @staticmethod
        def write_config(data: dict) -> None:
            config_path.write_text(json.dumps(data, indent=4))

        @staticmethod
        def write_defaults(data: dict) -> None:
            defaults_path.write_text(json.dumps(data, indent=4))

        @staticmethod
        def read_config() -> dict:
            return json.loads(config_path.read_text())

    return Env()


# -- deep_merge ---------------------------------------------------------------

class TestDeepMerge:
    def test_missing_key_filled_from_defaults(self):
        base = {"a": 1}
        defaults = {"a": 1, "b": 2}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged == {"a": 1, "b": 2}
        assert "b" in added

    def test_existing_key_not_overwritten(self):
        base = {"a": 1, "b": "original"}
        defaults = {"b": "default"}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["b"] == "original"
        assert added == []

    def test_nested_dicts_recursively_merged(self):
        base = {"outer": {"existing": True}}
        defaults = {"outer": {"existing": False, "new_field": 42}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["outer"]["existing"] is True  # not overwritten
        assert merged["outer"]["new_field"] == 42
        assert "outer.new_field" in added

    def test_stations_key_never_recursed(self):
        base = {"stations": {"RO000H": {"ip": "10.0.0.1"}}}
        defaults = {"stations": {"DEFAULT": {"ip": "0.0.0.0", "port": 7779}}}
        merged, added = config_migrate.deep_merge(base, defaults)
        # stations should be untouched
        assert "DEFAULT" not in merged["stations"]
        assert merged["stations"]["RO000H"] == {"ip": "10.0.0.1"}
        assert added == []

    def test_type_mismatch_logs_warning_and_skips(self, caplog):
        base = {"key": "i_am_a_string"}
        defaults = {"key": {"nested": True}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["key"] == "i_am_a_string"
        assert added == []
        assert "Type mismatch" in caplog.text

    def test_returns_correct_added_paths(self):
        base = {"a": 1}
        defaults = {"a": 1, "b": 2, "c": {"d": 3}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert sorted(added) == ["b", "c"]

    def test_empty_defaults_no_changes(self):
        base = {"a": 1, "b": 2}
        merged, added = config_migrate.deep_merge(base, {})
        assert merged == base
        assert added == []

    def test_empty_base_fills_everything(self):
        defaults = {"a": 1, "b": {"c": 3}}
        merged, added = config_migrate.deep_merge({}, defaults)
        assert merged == defaults
        assert sorted(added) == ["a", "b"]

    def test_deeply_nested_merge(self):
        base = {"level1": {"level2": {"existing": True}}}
        defaults = {"level1": {"level2": {"existing": False, "added": "yes"}}}
        merged, added = config_migrate.deep_merge(base, defaults)
        assert merged["level1"]["level2"]["existing"] is True
        assert merged["level1"]["level2"]["added"] == "yes"
        assert "level1.level2.added" in added

    def test_base_not_mutated(self):
        base = {"a": 1}
        original = base.copy()
        config_migrate.deep_merge(base, {"b": 2})
        assert base == original  # base dict unchanged


# -- migrate -------------------------------------------------------------------

class TestMigrate:
    def test_adds_missing_fields(self, config_env):
        config_env.write_config({"existing": True})
        config_env.write_defaults({"existing": True, "new_key": "value"})
        count = config_migrate.migrate(dry_run=False)
        assert count == 1
        result = config_env.read_config()
        assert result["new_key"] == "value"

    def test_preserves_existing_fields(self, config_env):
        config_env.write_config({"keep_me": "original", "also_keep": 42})
        config_env.write_defaults({"keep_me": "default", "also_keep": 0, "add_me": True})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert result["keep_me"] == "original"
        assert result["also_keep"] == 42

    def test_removes_keys_in_removed_keys(self, config_env, monkeypatch):
        monkeypatch.setattr(config_migrate, "REMOVED_KEYS", ["old_key", "stale_key"])
        monkeypatch.setattr(config_migrate, "REMOVED_NESTED_KEYS", {})
        config_env.write_config({"old_key": 1, "stale_key": 2, "good_key": 3})
        config_env.write_defaults({"good_key": 3})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert "old_key" not in result
        assert "stale_key" not in result
        assert result["good_key"] == 3

    def test_removes_nested_keys(self, config_env, monkeypatch):
        monkeypatch.setattr(config_migrate, "REMOVED_KEYS", [])
        monkeypatch.setattr(
            config_migrate, "REMOVED_NESTED_KEYS",
            {"capabilities": ["vaapi", "vaapi_driver"]},
        )
        config_env.write_config({
            "capabilities": {"vaapi": True, "vaapi_driver": "iHD", "keep": "this"},
        })
        config_env.write_defaults({})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert "vaapi" not in result["capabilities"]
        assert "vaapi_driver" not in result["capabilities"]
        assert result["capabilities"]["keep"] == "this"

    def test_removes_empty_parent_after_nested_purge(self, config_env, monkeypatch):
        monkeypatch.setattr(config_migrate, "REMOVED_KEYS", [])
        monkeypatch.setattr(
            config_migrate, "REMOVED_NESTED_KEYS",
            {"capabilities": ["only_child"]},
        )
        config_env.write_config({"capabilities": {"only_child": True}})
        config_env.write_defaults({})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        assert "capabilities" not in result

    def test_dry_run_does_not_write(self, config_env):
        config_env.write_config({"existing": True})
        config_env.write_defaults({"existing": True, "new_key": "value"})
        count = config_migrate.migrate(dry_run=True)
        assert count == 1
        # File should still have original content
        result = config_env.read_config()
        assert "new_key" not in result

    def test_missing_config_returns_zero(self, config_env):
        # Only write defaults, no config.json
        config_env.write_defaults({"a": 1})
        assert config_migrate.migrate() == 0

    def test_missing_defaults_returns_zero(self, config_env):
        # Only write config, no defaults
        config_env.write_config({"a": 1})
        assert config_migrate.migrate() == 0

    def test_up_to_date_config_returns_zero(self, config_env):
        config_env.write_config({"a": 1, "b": 2})
        config_env.write_defaults({"a": 1, "b": 2})
        count = config_migrate.migrate(dry_run=False)
        assert count == 0

    def test_atomic_write_uses_tmp_file(self, config_env):
        config_env.write_config({"existing": True})
        config_env.write_defaults({"existing": True, "new_key": "value"})
        config_migrate.migrate(dry_run=False)
        # After successful migration, no .tmp file should remain
        tmp_file = config_env.cfg.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_removed_keys_from_actual_list(self, config_env):
        """Verify the real REMOVED_KEYS list purges matching keys."""
        cfg = {"good": True}
        for k in config_migrate.REMOVED_KEYS[:3]:
            cfg[k] = "should_be_removed"
        config_env.write_config(cfg)
        config_env.write_defaults({"good": True})
        config_migrate.migrate(dry_run=False)
        result = config_env.read_config()
        for k in config_migrate.REMOVED_KEYS[:3]:
            assert k not in result
        assert result["good"] is True

    def test_purges_the_stale_overlay_logo_geometry(self, config_env):
        """The overlay logo size must come from overlay.py, never from a config.

        Every station carried ``overlay.bar_logo_height_frac`` = 0.62 (merged in
        from config_defaults.json on 2026-08-05), which silently outranked four
        fleet-wide shrinks of the constant. Migration has to strip it, and must
        leave the rest of the overlay block alone.
        """
        config_env.write_config({
            "overlay": {
                "style": "cinema",
                "bar_logo_height_frac": 0.62,
                "bar_logo_width_frac": 0.28,
                "bar_logo_opacity": 0.9,
            },
        })
        config_env.write_defaults({"overlay": {"style": "cinema"}})
        config_migrate.migrate(dry_run=False)
        overlay = config_env.read_config()["overlay"]
        assert "bar_logo_height_frac" not in overlay
        assert "bar_logo_width_frac" not in overlay
        assert overlay["bar_logo_opacity"] == 0.9
        assert overlay["style"] == "cinema"

    def test_defaults_never_reintroduce_the_logo_geometry(self):
        """The shipped defaults must not carry a key the migration purges.

        A key in both places would be purged and re-merged on every update, so
        the override would come back and shadow the constant again.
        """
        defaults = json.loads(
            (REPO_ROOT / "rovimen-scripts" / "config_defaults.json").read_text()
        )
        overlay_defaults = defaults.get("overlay", {})
        for key in config_migrate.REMOVED_NESTED_KEYS["overlay"]:
            assert key not in overlay_defaults

    def test_per_station_configs_never_carry_code_owned_geometry(self):
        """Nor may a checked-in station config, which the installer copies out."""
        for cfg_path in sorted((REPO_ROOT / "station_configs").glob("*/config.json")):
            overlay = json.loads(cfg_path.read_text()).get("overlay", {})
            offenders = sorted(
                set(overlay) & set(config_migrate.REMOVED_NESTED_KEYS["overlay"])
            )
            assert not offenders, f"{cfg_path.parent.name} carries {offenders}"

    def test_purge_list_tracks_overlay_module(self):
        """The purge list is derived from overlay.py, and the fallbacks match it.

        Adding a geometry constant to CODE_OWNED_OVERLAY_KEYS -- or retiring a key
        into INERT_OVERLAY_KEYS -- must purge it fleet-wide without a second edit
        here; the literal fallbacks exist only for a station whose overlay.py
        cannot be imported.
        """
        import overlay

        expected = set(overlay.CODE_OWNED_OVERLAY_KEYS) | set(overlay.INERT_OVERLAY_KEYS)
        assert set(config_migrate.REMOVED_NESTED_KEYS["overlay"]) == expected
        assert set(config_migrate._CODE_OWNED_OVERLAY_FALLBACK) == \
            set(overlay.CODE_OWNED_OVERLAY_KEYS)
        assert set(config_migrate._INERT_OVERLAY_FALLBACK) == \
            set(overlay.INERT_OVERLAY_KEYS)

    def test_dead_logo_controls_are_purged_fleet_wide(self):
        """The keys the dashboard used to offer as live spinners.

        logo_size sat in every station config and in the overlay editor as
        "px (0 = auto)"; the 2026-08-25 audit found it set to 0, 16 and 25 across
        the fleet with no rendered difference. Purging it is the only way an
        operator can tell the control was never wired.
        """
        for key in ("logo_size", "logo_opacity"):
            assert key in config_migrate.REMOVED_NESTED_KEYS["overlay"]


class TestLockedAtomicWrite:
    """config_migrate shares the config.json writer lock and never uses the
    fixed ``config.json.tmp`` that station_api's PATCH also wrote."""

    def test_takes_the_shared_config_lock(self, config_env, monkeypatch):
        import contextlib

        config_env.write_config({"a": 1})
        config_env.write_defaults({"a": 0, "b": 2})
        seen: list[Path] = []

        @contextlib.contextmanager
        def spy_lock(path):
            seen.append(Path(path))
            yield

        monkeypatch.setattr(config_migrate, "_shared_config_lock", spy_lock)
        assert config_migrate.migrate() == 1
        assert seen == [config_env.cfg]
        assert json.loads(config_env.cfg.read_text()) == {"a": 1, "b": 2}

    def test_leaves_no_temp_file_and_keeps_the_mode(self, config_env):
        config_env.write_config({"a": 1})
        config_env.cfg.chmod(0o600)
        config_env.write_defaults({"b": 2})
        config_migrate.migrate()
        assert (config_env.cfg.stat().st_mode & 0o777) == 0o600
        leftovers = [p.name for p in config_env.cfg.parent.iterdir()
                     if p.name not in ("config.json", "config_defaults.json", ".config.lock")]
        assert leftovers == []

    def test_still_writes_atomically_without_rovimen_config(self, config_env, monkeypatch):
        """A half-synced bundle without rovimen_config must not stop the migration."""
        monkeypatch.setattr(config_migrate, "_shared_config_lock", None)
        monkeypatch.setattr(config_migrate, "_shared_atomic_write", None)
        config_env.write_config({"a": 1})
        config_env.cfg.chmod(0o640)
        config_env.write_defaults({"b": 2})
        assert config_migrate.migrate() == 1
        assert json.loads(config_env.cfg.read_text()) == {"a": 1, "b": 2}
        assert (config_env.cfg.stat().st_mode & 0o777) == 0o640
        assert sorted(p.name for p in config_env.cfg.parent.iterdir()) == [
            "config.json", "config_defaults.json"]
