"""Tests for the storage module, using tiny made-up seed files."""

import gzip
from pathlib import Path

import duckdb
import pytest

from transfer_engine.storage import (
    backup_database,
    connect,
    create_schema,
    prune_backups,
    seed_from_csv,
)

HISTORY_HEADER = "yr,wk,store,dept,brand,cat,num,sty,col,size,q,v,md,st,gp,rem"
HISTORY_ROWS = [
    "2026,41,A,Women,Brand1,Dresses,1001.0,Test Dress,Red,S,1.0,49.99,0.0,3.0,20.5,",
    "2026,41,A,Women,Brand1,Dresses,1001.0,Test Dress,Red,M,0.0,0.0,,,,2.0",
    "2026,41,B,Men,Brand2,Shirts,2002.0,Test Shirt,Blue,L,-1.0,-29.99,0.1,-1.0,,",
]
NORMS_ROWS = ["A,Test Shirt,Blue,L,4.0"]


def write_seed(folder: Path, history_rows: list[str], norms_rows: list[str]) -> Path:
    """Write a seed folder in the same format as the real one."""
    folder.mkdir(exist_ok=True)
    with gzip.open(folder / "history.csv.gz", "wt") as f:
        f.write("\n".join([HISTORY_HEADER, *history_rows]) + "\n")
    (folder / "norms.csv").write_text("\n".join(["store,sty,col,size,norm", *norms_rows]) + "\n")
    return folder


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    connection = connect(tmp_path / "retail.duckdb")
    yield connection
    connection.close()


def test_schema_has_four_tables_and_is_safe_to_rerun(con: duckdb.DuckDBPyConnection) -> None:
    create_schema(con)  # second run must not fail or wipe anything
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert tables == {"history", "norms", "batches", "decisions"}


def test_seed_loads_and_converts_types(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    seed = write_seed(tmp_path / "seed", HISTORY_ROWS, NORMS_ROWS)

    counts = seed_from_csv(con, seed)

    assert counts == {"history": 3, "norms": 1}
    # "1.0" becomes the whole number 1; an empty stock cell becomes NULL, not 0.
    row = con.execute("SELECT num, q, st, rem FROM history WHERE size = 'M'").fetchone()
    assert row == (1001, 0, None, 2)
    assert con.execute("SELECT norm FROM norms").fetchone() == (4,)


def test_seed_refuses_to_load_twice(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    seed = write_seed(tmp_path / "seed", HISTORY_ROWS, NORMS_ROWS)
    seed_from_csv(con, seed)

    with pytest.raises(RuntimeError, match="already has rows"):
        seed_from_csv(con, seed)


def test_seed_is_all_or_nothing(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    duplicate_norm = NORMS_ROWS * 2  # breaks the norms primary key, after history has loaded
    seed = write_seed(tmp_path / "seed", HISTORY_ROWS, duplicate_norm)

    with pytest.raises(duckdb.ConstraintException):
        seed_from_csv(con, seed)

    assert con.execute("SELECT count(*) FROM history").fetchone() == (0,)


@pytest.mark.parametrize(
    ("history_rows", "norms_rows", "bad_column"),
    [
        ([HISTORY_ROWS[0].replace(",1.0,49.99,", ",1.5,49.99,"), *HISTORY_ROWS[1:]], NORMS_ROWS, "q"),
        ([HISTORY_ROWS[0].replace(",3.0,20.5,", ",2.5,20.5,"), *HISTORY_ROWS[1:]], NORMS_ROWS, "st"),
        (HISTORY_ROWS, ["A,Test Shirt,Blue,L,4.5"], "norm"),
    ],
)
def test_seed_rejects_fractional_units(
    con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    history_rows: list[str],
    norms_rows: list[str],
    bad_column: str,
) -> None:
    seed = write_seed(tmp_path / "seed", history_rows, norms_rows)

    with pytest.raises(ValueError, match=f"'{bad_column}' \\(1 rows\\)"):
        seed_from_csv(con, seed)

    assert con.execute("SELECT count(*) FROM history").fetchone() == (0,)


def test_partial_decision_must_list_sizes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSERT INTO decisions (run_id, status) VALUES ('r1', 'approved')")
    con.execute(
        "INSERT INTO decisions (run_id, status, sizes) VALUES ('r2', 'partial', [{'size': 'M', 'qty': 1}])"
    )
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO decisions (run_id, status) VALUES ('r3', 'partial')")


def test_backup_keeps_only_the_newest(tmp_path: Path) -> None:
    db_path = tmp_path / "retail.duckdb"
    connect(db_path).close()
    backup_dir = tmp_path / "backups"

    made = [backup_database(db_path, backup_dir, keep=2) for _ in range(3)]

    remaining = sorted(backup_dir.iterdir())
    assert remaining == made[1:]  # the oldest was deleted


def test_prune_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a backup")
    for stamp in ("20260101", "20260102"):
        (tmp_path / f"retail-{stamp}.duckdb").write_text("")

    deleted = prune_backups(tmp_path, keep=1)

    assert [p.name for p in deleted] == ["retail-20260101.duckdb"]
    assert (tmp_path / "notes.txt").exists()
