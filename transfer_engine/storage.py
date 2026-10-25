"""Storage: the DuckDB master file, its tables, the one-off seed load and backups.

Everything lives in one file, `$RETAIL_DATA_DIR/retail.duckdb`. Each weekly export is
appended to it and then deleted, so this file is the only copy of the history: the
`history` table keeps every column the export has, even ones no rule uses yet.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import duckdb

# One row per year, week, store, style, colour and size.
HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS history (
    yr    SMALLINT NOT NULL,
    wk    SMALLINT NOT NULL CHECK (wk BETWEEN 1 AND 53),  -- ISO weeks: some years have 53
    store VARCHAR  NOT NULL,
    dept  VARCHAR  NOT NULL,
    brand VARCHAR  NOT NULL,
    cat   VARCHAR  NOT NULL,
    num   BIGINT,                 -- style number
    sty   VARCHAR  NOT NULL,      -- style name
    col   VARCHAR  NOT NULL,      -- colour
    size  VARCHAR  NOT NULL,
    q     INTEGER  NOT NULL,      -- units sold (negative = net returns)
    v     DOUBLE   NOT NULL,      -- sales value incl. VAT
    md    DOUBLE,                 -- markdown as a fraction of full price (0.3 = 30%)
    st    INTEGER,                -- closing stock; NULL = not reported, can be negative
    gp    DOUBLE,                 -- gross profit
    rem   INTEGER,                -- remaining qty: invoiced / in transit, not yet booked in
    PRIMARY KEY (yr, wk, store, sty, col, size)
)
"""

# Latest snapshot of auto-replenishment levels. A norm > 0 means the line is replenished.
NORMS_DDL = """
CREATE TABLE IF NOT EXISTS norms (
    store VARCHAR NOT NULL,
    sty   VARCHAR NOT NULL,
    col   VARCHAR NOT NULL,
    size  VARCHAR NOT NULL,
    norm  INTEGER NOT NULL CHECK (norm >= 0),
    PRIMARY KEY (store, sty, col, size)
)
"""

# Transfer suggestions. One row per size; run_id groups the sizes of one style-colour run
# going from one store to another, and stays the same if the week is regenerated.
BATCHES_DDL = """
CREATE TABLE IF NOT EXISTS batches (
    run_id     VARCHAR   NOT NULL,
    yr         SMALLINT  NOT NULL,
    wk         SMALLINT  NOT NULL,
    kind       VARCHAR   NOT NULL CHECK (kind IN ('move', 'tidy')),
    sender     VARCHAR   NOT NULL,
    receiver   VARCHAR   NOT NULL CHECK (receiver <> sender),
    sty        VARCHAR   NOT NULL,
    col        VARCHAR   NOT NULL,
    size       VARCHAR   NOT NULL,
    qty        INTEGER   NOT NULL CHECK (qty > 0),
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (run_id, size)
)
"""

# One decision per run. A partial approval lists the sizes and quantities actually sent;
# approved and rejected runs leave `sizes` empty.
DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS decisions (
    run_id     VARCHAR   PRIMARY KEY,
    status     VARCHAR   NOT NULL CHECK (status IN ('approved', 'partial', 'rejected')),
    sizes      STRUCT(size VARCHAR, qty INTEGER)[],
    note       VARCHAR,
    decided_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    CHECK ((status = 'partial') = (sizes IS NOT NULL))
)
"""

TABLE_DDLS = [HISTORY_DDL, NORMS_DDL, BATCHES_DDL, DECISIONS_DDL]

# The seed files store numbers as text like "1.0"; read as DOUBLE, then cast to whole
# numbers where the column holds units.
HISTORY_CSV_TYPES = {
    "yr": "SMALLINT", "wk": "SMALLINT", "store": "VARCHAR", "dept": "VARCHAR",
    "brand": "VARCHAR", "cat": "VARCHAR", "num": "DOUBLE", "sty": "VARCHAR",
    "col": "VARCHAR", "size": "VARCHAR", "q": "DOUBLE", "v": "DOUBLE", "md": "DOUBLE",
    "st": "DOUBLE", "gp": "DOUBLE", "rem": "DOUBLE",
}
NORMS_CSV_TYPES = {
    "store": "VARCHAR", "sty": "VARCHAR", "col": "VARCHAR", "size": "VARCHAR", "norm": "DOUBLE",
}

# Columns that hold counts (units or an ID number) and become INTEGER / BIGINT on load.
HISTORY_WHOLE_NUMBER_COLUMNS = ["num", "q", "st", "rem"]
NORMS_WHOLE_NUMBER_COLUMNS = ["norm"]

BACKUP_PREFIX = "retail-"


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open the master file (creating it if needed) and make sure every table exists."""
    con = duckdb.connect(str(db_path))
    create_schema(con)
    return con


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create any missing tables. Safe to run every time: existing tables are left alone."""
    for ddl in TABLE_DDLS:
        con.execute(ddl)


def check_whole_numbers(
    con: duckdb.DuckDBPyConnection, csv_path: Path, column_types: dict[str, str], columns: list[str]
) -> None:
    """Stop if a units column holds a fraction like 1.5.

    Converting 1.5 to a whole number would silently round it to 2, so a fraction
    means the file is wrong and must be fixed, not guessed at. Empty cells are fine.
    """
    counts = ", ".join(f"count(*) FILTER (WHERE {c} <> floor({c}))" for c in columns)
    found = con.execute(
        f"SELECT {counts} FROM read_csv(?, header = true, columns = ?)",
        [str(csv_path), column_types],
    ).fetchone()
    bad = {column: n for column, n in zip(columns, found) if n}
    if bad:
        detail = ", ".join(f"'{column}' ({n:,} rows)" for column, n in bad.items())
        raise ValueError(f"{csv_path.name}: expected whole numbers but found fractions in {detail}")


def seed_from_csv(con: duckdb.DuckDBPyConnection, seed_dir: Path) -> dict[str, int]:
    """Load the one-off seed files into empty `history` and `norms` tables.

    Checks first that unit columns hold whole numbers. Then loads in one transaction:
    if any row breaks a rule in the schema (a duplicate key, a missing store),
    nothing is loaded. Returns the row count per table.
    """
    history_csv = seed_dir / "history.csv.gz"
    norms_csv = seed_dir / "norms.csv"
    for path in (history_csv, norms_csv):
        if not path.is_file():
            raise FileNotFoundError(f"Seed file not found: {path}")

    for table in ("history", "norms"):
        if con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]:
            raise RuntimeError(f"Table '{table}' already has rows; seeding is for an empty database.")

    check_whole_numbers(con, history_csv, HISTORY_CSV_TYPES, HISTORY_WHOLE_NUMBER_COLUMNS)
    check_whole_numbers(con, norms_csv, NORMS_CSV_TYPES, NORMS_WHOLE_NUMBER_COLUMNS)

    con.execute("BEGIN")
    try:
        con.execute(
            """
            INSERT INTO history
            SELECT yr, wk, store, dept, brand, cat, num::BIGINT, sty, col, size,
                   q::INTEGER, v, md, st::INTEGER, gp, rem::INTEGER
            FROM read_csv(?, header = true, columns = ?)
            """,
            [str(history_csv), HISTORY_CSV_TYPES],
        )
        con.execute(
            """
            INSERT INTO norms
            SELECT store, sty, col, size, norm::INTEGER
            FROM read_csv(?, header = true, columns = ?)
            """,
            [str(norms_csv), NORMS_CSV_TYPES],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("history", "norms")}


def backup_database(db_path: Path, backup_dir: Path, keep: int) -> Path:
    """Copy the master file into `backup_dir` and delete all but the newest `keep` copies.

    Call this with no other connection open. The file is checkpointed first so any
    pending writes (DuckDB's .wal file) are folded into the copy.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"Nothing to back up: {db_path}")

    with duckdb.connect(str(db_path)) as con:
        con.execute("CHECKPOINT")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"{BACKUP_PREFIX}{stamp}.duckdb"
    shutil.copy2(db_path, target)

    prune_backups(backup_dir, keep)
    return target


def prune_backups(backup_dir: Path, keep: int) -> list[Path]:
    """Delete the oldest backups so only the newest `keep` remain. Returns what was deleted.

    The timestamp in each file name sorts oldest-first, so the name order is the age order.
    """
    backups = sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.duckdb"))
    to_delete = backups[:-keep] if keep > 0 else backups
    for path in to_delete:
        path.unlink()
    return to_delete
