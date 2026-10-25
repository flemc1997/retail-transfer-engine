"""The synthetic data must load like the real data and make every rule fire."""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from transfer_engine.batch import generate_batch, latest_week
from transfer_engine.checks import run_checks
from transfer_engine.export import parse_export
from transfer_engine.settings import DEFAULT_CONFIG_PATH, Settings, load_config, load_settings
from transfer_engine.storage import connect, replace_week, seed_from_csv
from transfer_engine.synthetic import generate
from transfer_engine.trading import slow_movers

LAST_WEEK = (2026, 42)


@pytest.fixture(scope="module")
def data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    *_, mapping, _ = load_config(DEFAULT_CONFIG_PATH)
    return generate(mapping, LAST_WEEK)


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    folder = tmp_path_factory.mktemp("synthetic")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RETAIL_DATA_DIR", str(folder))
        mp.delenv("RETAIL_CONFIG", raising=False)
        return load_settings(env_file=None)


@pytest.fixture(scope="module")
def con(data: tuple, settings: Settings) -> duckdb.DuckDBPyConnection:
    """The synthetic seed files, written and loaded exactly as the real ones are."""
    history, norms, _ = data
    seed = settings.data_dir / "seed"
    seed.mkdir()
    history.to_csv(seed / "history.csv.gz", index=False)
    norms.to_csv(seed / "norms.csv", index=False)
    connection = connect(settings.db_path)
    seed_from_csv(connection, seed)
    yield connection
    connection.close()


def test_history_covers_more_than_a_year(con: duckdb.DuckDBPyConnection) -> None:
    weeks = con.execute("SELECT count(DISTINCT yr * 100 + wk) FROM history").fetchone()[0]
    assert weeks > 53  # so there's a 'same week last year' to compare with
    assert latest_week(con) == LAST_WEEK


def test_every_transfer_rule_fires(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    runs, _ = generate_batch(con, settings, *LAST_WEEK)

    assert set(runs["kind"]) == {"move", "tidy"}
    normed = set(con.execute("SELECT DISTINCT sty, col FROM norms").fetchall())
    assert normed, "some lines must be replenished, so the norm rule has something to skip"
    assert not normed & set(zip(runs["sty"], runs["col"]))


def test_there_are_slow_movers(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    assert not slow_movers(con, *LAST_WEEK, settings.trading).empty


def test_split_colour_is_in_history(con: duckdb.DuckDBPyConnection) -> None:
    assert con.execute("SELECT count(*) FROM history WHERE col LIKE '% · %'").fetchone()[0] > 0


def test_export_passes_checks_and_uploads(data: tuple, con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    parsed = parse_export(data[2], settings.export)

    checks = run_checks(parsed, con, settings.upload, settings.export.store_codes)
    assert {c.status for c in checks} == {"pass"}, checks
    assert {"footer rows", "excluded stores", "placeholder styles", "other brands"} <= set(parsed.skipped)

    result = replace_week(con, parsed.history, parsed.norms)
    assert result["norms_refreshed"] and result["rows_inserted"] > 0


def test_same_seed_gives_same_data(data: tuple) -> None:
    *_, mapping, _ = load_config(DEFAULT_CONFIG_PATH)
    again, _, _ = generate(mapping, LAST_WEEK)
    pd.testing.assert_frame_equal(data[0], again)
