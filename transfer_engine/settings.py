"""Settings loader: where the data lives and which thresholds the rules use.

Two sources:
- `.env` / environment: `RETAIL_DATA_DIR` (required) points at the folder outside
  the repo that holds the real data. `RETAIL_CONFIG` (optional) picks a thresholds file.
- A JSON thresholds file, by default `config/config.example.json`.

Everything else in the app asks `load_settings()` for paths and thresholds, so no
module hard-codes a path or a number.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.example.json"


class SettingsError(Exception):
    """Raised when the environment or config file is missing or malformed."""


def _require_positive_rate_weeks(rate_weeks: int, section: str) -> None:
    """The sales rate divides by the number of weeks, so it must be at least 1."""
    if rate_weeks <= 0:
        raise SettingsError(
            f"Config section '{section}': rate_weeks must be greater than 0, got {rate_weeks}"
        )


@dataclass(frozen=True)
class UploadRules:
    """Safety checks applied to each weekly export before it is appended."""

    min_sales_ratio: float  # total sales must be at least this share of last week and of LY
    backups_to_keep: int  # how many database copies to keep in backups/


@dataclass(frozen=True)
class MoveRules:
    """Thresholds for moves: sending a run from a slow store to a fast one."""

    rate_weeks: int  # sales rate = units per week over this many recent weeks
    receiver_min_rate: float  # a receiver must sell at least this many units a week...
    receiver_max_cover_weeks: float  # ...and have less than this many weeks of cover
    donor_min_units: int  # a donor must hold at least this many units...
    donor_min_cover_weeks: float  # ...and at least this many weeks of cover
    receiver_max_cover_after_weeks: float  # don't overstock: receiver cover after the move

    def __post_init__(self) -> None:
        _require_positive_rate_weeks(self.rate_weeks, "moves")


@dataclass(frozen=True)
class TidyUpRules:
    """Thresholds for tidy-ups: pulling scattered slow stock into one store."""

    rate_weeks: int  # chain-wide sales rate is measured over this many weeks
    min_stores_holding: int  # only lines held in at least this many stores
    max_units_per_store: int  # ...where no store holds more than this
    min_weeks_since_first_stocked: int  # give new lines time to sell first
    min_chain_cover_weeks: float  # slow = at least this many weeks of chain cover
    accessories_dept: str  # department name treated as accessories
    accessories_max_chain_units: int  # accessories only tidied when the chain total is this small

    def __post_init__(self) -> None:
        _require_positive_rate_weeks(self.rate_weeks, "tidy_ups")


@dataclass(frozen=True)
class ExportMapping:
    """How to read the Power BI export: which stores and brands to keep, which colours to split.

    These are real store and product names, so the real values live only in the local
    config outside the repo; `config.example.json` has made-up ones.
    """

    store_match: dict[str, str]  # text found in 'Store Name' -> store code
    exclude_stores: list[str]  # text in 'Store Name' for stores to ignore
    brand_prefixes: dict[str, list[str]]  # style-name prefix -> [dept, brand]; other brands are dropped
    split_colours: list[list[str]]  # [style, colour] pairs whose colour is split by variant

    def __post_init__(self) -> None:
        for prefix, dept_brand in self.brand_prefixes.items():
            if len(dept_brand) != 2:
                raise SettingsError(f"Config section 'export': brand_prefixes['{prefix}'] must be [dept, brand]")

    @property
    def store_codes(self) -> list[str]:
        """Every store the export must contain."""
        return sorted(set(self.store_match.values()))


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to know about its environment."""

    data_dir: Path
    config_path: Path
    upload: UploadRules
    moves: MoveRules
    tidy_ups: TidyUpRules
    export: ExportMapping

    @property
    def db_path(self) -> Path:
        """The single DuckDB file holding history, norms, batches and decisions."""
        return self.data_dir / "retail.duckdb"

    @property
    def seed_dir(self) -> Path:
        """Folder with the one-off seed files (history.csv.gz, norms.csv)."""
        return self.data_dir / "seed"

    @property
    def backup_dir(self) -> Path:
        """Where database copies go before each upload."""
        return self.data_dir / "backups"

    @property
    def legacy_dir(self) -> Path:
        """The old engine, kept for reference only."""
        return self.data_dir / "legacy-engine"


def _build_section(cls: type, raw: dict[str, Any], name: str) -> Any:
    """Turn one JSON section into its dataclass, failing loudly on missing or unknown keys.

    A typo in the config file should stop the app, not silently fall back to a default.
    """
    expected = {f.name for f in fields(cls)}
    missing = expected - raw.keys()
    unknown = raw.keys() - expected
    if missing or unknown:
        raise SettingsError(
            f"Config section '{name}': missing {sorted(missing) or 'none'}, "
            f"unknown {sorted(unknown) or 'none'}"
        )
    return cls(**raw)

def load_config(path: Path) -> tuple[UploadRules, MoveRules, TidyUpRules, ExportMapping]:
    """Read the config file and return one typed object per section."""
    if not path.is_file():
        raise SettingsError(f"Config file not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SettingsError(f"Config file is not valid JSON: {path} ({exc})") from exc

    sections = {
        "upload": UploadRules,
        "moves": MoveRules,
        "tidy_ups": TidyUpRules,
        "export": ExportMapping,
    }
    for name in sections:
        if name not in raw:
            raise SettingsError(f"Config file is missing the '{name}' section: {path}")
    return tuple(_build_section(cls, raw[name], name) for name, cls in sections.items())  # type: ignore[return-value]


def load_settings(env_file: Path | None = REPO_ROOT / ".env") -> Settings:
    """Load `.env`, check the data folder exists, and read the thresholds file.

    Real environment variables win over `.env`, so tests and shells can override it.
    """
    if env_file is not None:
        load_dotenv(env_file, override=False)

    data_dir_raw = os.environ.get("RETAIL_DATA_DIR")
    if not data_dir_raw:
        raise SettingsError("RETAIL_DATA_DIR is not set. Copy .env.example to .env and fill it in.")
    data_dir = Path(data_dir_raw).expanduser()
    if not data_dir.is_dir():
        raise SettingsError(f"RETAIL_DATA_DIR does not exist or is not a folder: {data_dir}")

    config_raw = os.environ.get("RETAIL_CONFIG")
    config_path = Path(config_raw).expanduser() if config_raw else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path

    upload, moves, tidy_ups, export = load_config(config_path)
    return Settings(data_dir, config_path, upload, moves, tidy_ups, export)
