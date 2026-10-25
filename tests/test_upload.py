"""Tests for saving an upload: replace the week, relabel categories, refresh norms, clean up."""

import json
from pathlib import Path

import duckdb
import pytest

from conftest import export_row, raw_export
from transfer_engine.export import parse_export
from transfer_engine.settings import DEFAULT_CONFIG_PATH, ExportMapping, Settings, load_settings
from transfer_engine.storage import connect, replace_week
from transfer_engine.upload import save_upload


def history_row(yr: int, wk: int, store: str, sty: str = "MNA SHIRT", cat: str = "Old Cat") -> list[object]:
    return [yr, wk, store, "Men", "Brand M", cat, 1, sty, "Blue", "M", 1, 50.0, 0.0, 1, 0.0, None]


def seed(con: duckdb.DuckDBPyConnection, rows: list[list[object]], norms: list[list[object]] | None = None) -> None:
    con.executemany("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    if norms:
        con.executemany("INSERT INTO norms VALUES (?, ?, ?, ?, ?)", norms)


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    connection = connect(tmp_path / "retail.duckdb")
    yield connection
    connection.close()


def week_41(mapping: ExportMapping, **overrides: object):
    return parse_export(
        raw_export(
            export_row(Store_Name="XX - North Mall", Norm_Ultimo=2.0, **overrides),
            export_row(Store_Name="XX - South Park", **overrides),
        ),
        mapping,
    )


def test_replaces_only_that_week(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    seed(con, [history_row(2026, 41, "N", sty="GONE"), history_row(2026, 40, "N")])

    result = replace_week(con, week_41(mapping).history, week_41(mapping).norms)

    assert result["rows_deleted"] == 1 and result["rows_inserted"] == 2
    weeks = con.execute("SELECT wk, count(*) FROM history GROUP BY wk ORDER BY wk").fetchall()
    assert weeks == [(40, 1), (41, 2)]
    assert con.execute("SELECT count(*) FROM history WHERE sty = 'GONE'").fetchone() == (0,)


def test_style_category_is_updated_in_earlier_weeks(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    seed(con, [history_row(2026, 40, "N", cat="Old Cat"), history_row(2026, 40, "N", sty="OTHER", cat="Old Cat")])

    replace_week(con, week_41(mapping).history, week_41(mapping).norms)

    cats = dict(con.execute("SELECT DISTINCT sty, cat FROM history").fetchall())
    assert cats == {"MNA SHIRT": "Shirts", "OTHER": "Old Cat"}  # styles not in the export keep theirs


def test_norms_refresh_only_for_the_newest_week(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    seed(con, [history_row(2026, 42, "N")], norms=[["N", "OLD", "Blue", "M", 5]])

    result = replace_week(con, week_41(mapping).history, week_41(mapping).norms)  # an older week

    assert result["norms_refreshed"] is False
    assert con.execute("SELECT sty FROM norms").fetchall() == [("OLD",)]


def test_norms_replaced_by_newest_week(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    seed(con, [history_row(2026, 40, "N")], norms=[["N", "OLD", "Blue", "M", 5]])

    replace_week(con, week_41(mapping).history, week_41(mapping).norms)

    assert con.execute("SELECT store, sty, norm FROM norms").fetchall() == [("N", "MNA SHIRT", 2)]


def test_failed_save_changes_nothing(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    seed(con, [history_row(2026, 41, "N")])
    parsed = week_41(mapping)
    broken = parsed.history.assign(q=None)  # q is NOT NULL, so the insert fails mid-transaction

    with pytest.raises(duckdb.ConstraintException):
        replace_week(con, broken, parsed.norms)

    assert con.execute("SELECT count(*) FROM history WHERE yr = 2026 AND wk = 41").fetchone() == (1,)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapping: ExportMapping) -> Settings:
    """Settings pointing at a throwaway data folder, with the made-up test mapping."""
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text())
    raw["export"] = {
        "store_match": mapping.store_match,
        "exclude_stores": mapping.exclude_stores,
        "brand_prefixes": mapping.brand_prefixes,
        "split_colours": mapping.split_colours,
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(raw))
    monkeypatch.setenv("RETAIL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RETAIL_CONFIG", str(config))
    connect(tmp_path / "retail.duckdb").close()
    return load_settings(env_file=None)


def test_save_backs_up_then_deletes_the_export(settings: Settings, mapping: ExportMapping) -> None:
    export_path = settings.data_dir / "week41.xlsx"
    export_path.write_text("stand-in for the real file")

    result = save_upload(settings, week_41(mapping), export_path)

    assert result.backup_path.exists()
    assert not export_path.exists()


def test_export_kept_when_save_fails(settings: Settings, mapping: ExportMapping) -> None:
    export_path = settings.data_dir / "week41.xlsx"
    export_path.write_text("stand-in for the real file")
    parsed = week_41(mapping)
    parsed.history = parsed.history.assign(q=None)

    with pytest.raises(duckdb.ConstraintException):
        save_upload(settings, parsed, export_path)

    assert export_path.exists()  # so the upload can simply be retried
