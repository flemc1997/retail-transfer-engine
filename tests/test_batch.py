"""Tests for building the rules' input from history and saving a batch."""

from pathlib import Path

import duckdb
import pytest

from transfer_engine.batch import generate_batch, latest_week, save_batch
from transfer_engine.lines import build_lines, week_keys, weeks_between
from transfer_engine.settings import Settings, load_settings
from transfer_engine.storage import connect


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("RETAIL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RETAIL_CONFIG", raising=False)
    return load_settings(env_file=None)


@pytest.fixture
def con(settings: Settings) -> duckdb.DuckDBPyConnection:
    connection = connect(settings.db_path)
    yield connection
    connection.close()


def add(con: duckdb.DuckDBPyConnection, yr: int, wk: int, store: str, size: str, q: int, st: int | None,
        rem: int | None = None, sty: str = "STYLE", col: str = "Red") -> None:
    con.execute(
        "INSERT INTO history VALUES (?, ?, ?, 'Men', 'Brand M', 'Shirts', 1, ?, ?, ?, ?, ?, 0, ?, 0, ?)",
        [yr, wk, store, sty, col, size, q, q * 10.0, st, rem],
    )


def test_week_helpers_handle_week_53() -> None:
    assert week_keys(2027, 2, 4) == [202702, 202701, 202653, 202652]
    assert weeks_between(202650, 202702) == 5


def test_build_lines_rates_stock_and_first_stocked(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    add(con, 2026, 30, "A", "M", q=0, st=4)  # first stocked in week 30
    add(con, 2026, 40, "A", "M", q=3, st=4)  # inside the 4-week window (39-42)
    add(con, 2026, 42, "A", "M", q=1, st=2, rem=3)
    add(con, 2026, 42, "A", "L", q=0, st=-1)  # negative stock counts as 0
    add(con, 2026, 42, "B", "M", q=-2, st=0)  # net returns: rate 0, not negative

    lines, sizes = build_lines(con, settings, 2026, 42)

    a = lines.set_index("store").loc["A"]
    assert (a["stock"], a["on_way"], a["n_sizes"]) == (2, 3, 1)
    assert a["move_rate"] == pytest.approx(1.0)  # (3 + 1) / 4 weeks
    assert a["tidy_rate"] == pytest.approx(0.5)  # (3 + 1) / 8 weeks
    assert a["weeks_since_first_stocked"] == 12
    assert lines.set_index("store").loc["B", "move_rate"] == 0
    assert set(sizes["size"]) == {"M", "L"}


def test_lines_without_stock_are_left_out(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    add(con, 2026, 42, "A", "M", q=2, st=0, sty="SOLD OUT")
    add(con, 2026, 42, "A", "M", q=0, st=1, sty="IN STOCK")

    lines, _ = build_lines(con, settings, 2026, 42)

    assert set(lines["sty"]) == {"IN STOCK"}


def test_normed_flag_applies_to_every_store(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    add(con, 2026, 42, "A", "M", q=0, st=1)
    add(con, 2026, 42, "B", "M", q=0, st=1)
    con.execute("INSERT INTO norms VALUES ('A', 'STYLE', 'Red', 'M', 2)")

    lines, _ = build_lines(con, settings, 2026, 42)

    assert lines["normed"].all()


def test_generate_and_save_batch(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    for wk in (39, 40, 41, 42):
        add(con, 2026, wk, "A", "M", q=1, st=0 if wk == 42 else 1)  # sells 1/wk, now sold out
    add(con, 2026, 30, "B", "S", q=0, st=3)
    add(con, 2026, 42, "B", "S", q=0, st=3)  # no sales: a donor of S:3 and M:2
    add(con, 2026, 42, "B", "M", q=0, st=2)

    assert latest_week(con) == (2026, 42)
    runs, rows = generate_batch(con, settings, 2026, 42)

    assert list(zip(runs["kind"], runs["sender"], runs["receiver"], runs["qty"])) == [("move", "B", "A", 5)]
    assert sorted(zip(rows["size"], rows["qty"])) == [("M", 2), ("S", 3)]
    assert rows["run_id"].nunique() == 1

    save_batch(con, 2026, 42, rows)
    save_batch(con, 2026, 42, rows)  # regenerating replaces, never duplicates
    saved = con.execute("SELECT run_id, size, qty FROM batches ORDER BY size").fetchall()
    assert saved == [(runs["run_id"].iat[0], "M", 2), (runs["run_id"].iat[0], "S", 3)]
