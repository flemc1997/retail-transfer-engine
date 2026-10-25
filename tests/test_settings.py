"""Tests for the settings loader."""

import json
from pathlib import Path

import pytest

from transfer_engine.settings import DEFAULT_CONFIG_PATH, SettingsError, load_settings


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway data folder, pointed to by RETAIL_DATA_DIR."""
    folder = tmp_path / "retail-data"
    folder.mkdir()
    monkeypatch.setenv("RETAIL_DATA_DIR", str(folder))
    monkeypatch.delenv("RETAIL_CONFIG", raising=False)
    return folder


def test_loads_example_config_and_paths(data_dir: Path) -> None:
    settings = load_settings(env_file=None)

    assert settings.db_path == data_dir / "retail.duckdb"
    assert settings.backup_dir == data_dir / "backups"
    assert settings.config_path == DEFAULT_CONFIG_PATH
    assert settings.moves.receiver_min_rate == 0.5
    assert settings.tidy_ups.rate_weeks == 8


def test_missing_data_dir_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETAIL_DATA_DIR", raising=False)
    with pytest.raises(SettingsError, match="not set"):
        load_settings(env_file=None)


def test_data_dir_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETAIL_DATA_DIR", str(tmp_path / "nope"))
    with pytest.raises(SettingsError, match="does not exist"):
        load_settings(env_file=None)


def test_typo_in_config_is_rejected(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text())
    raw["moves"]["donor_min_unit"] = raw["moves"].pop("donor_min_units")  # typo
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    monkeypatch.setenv("RETAIL_CONFIG", str(bad))

    with pytest.raises(SettingsError, match="donor_min_unit"):
        load_settings(env_file=None)


@pytest.mark.parametrize("section", ["moves", "tidy_ups"])
@pytest.mark.parametrize("bad_value", [0, -1])
def test_rate_weeks_must_be_positive(
    section: str, bad_value: int, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text())
    raw[section]["rate_weeks"] = bad_value
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    monkeypatch.setenv("RETAIL_CONFIG", str(bad))

    with pytest.raises(SettingsError, match=f"'{section}': rate_weeks"):
        load_settings(env_file=None)
