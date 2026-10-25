"""A week's batch: run the rules, give each run a stable id, save one row per size.

Moves are worked out first; tidy-ups then skip any style-colour already in a move.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from transfer_engine.lines import build_lines
from transfer_engine.rules import run_id, run_sizes, suggest_moves, suggest_tidy_ups
from transfer_engine.settings import Settings

BATCH_COLUMNS = ["run_id", "yr", "wk", "kind", "sender", "receiver", "sty", "col", "size", "qty", "reason"]


def weeks_with_batches(con: duckdb.DuckDBPyConnection) -> list[tuple[int, int]]:
    """Weeks that already have saved suggestions, newest first."""
    return con.execute("SELECT DISTINCT yr, wk FROM batches ORDER BY yr DESC, wk DESC").fetchall()


def latest_week(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """The newest week in history: the one the batch is for by default."""
    key = con.execute("SELECT max(yr * 100 + wk) FROM history").fetchone()[0]
    if key is None:
        raise ValueError("History is empty: upload a week first.")
    return key // 100, key % 100


def generate_batch(con: duckdb.DuckDBPyConnection, settings: Settings, yr: int, wk: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (runs, batch_rows) for the week.

    runs: one row per suggestion, with run_id and the reason in business words.
    batch_rows: one row per size, in the `batches` table's columns.
    """
    lines, sizes = build_lines(con, settings, yr, wk)
    moves = suggest_moves(lines, settings.moves)
    in_a_move = set(zip(moves["sty"], moves["col"]))
    tidy_ups = suggest_tidy_ups(lines, settings.tidy_ups, skip=in_a_move)

    runs = pd.concat([moves, tidy_ups], ignore_index=True)
    runs.insert(
        0,
        "run_id",
        [run_id(yr, wk, r.sty, r.col, r.sender, r.receiver) for r in runs.itertuples()],
    )

    rows = run_sizes(runs, sizes).assign(yr=yr, wk=wk, qty=lambda d: d["size_qty"].astype(int))
    return runs, rows[BATCH_COLUMNS]


def save_batch(con: duckdb.DuckDBPyConnection, yr: int, wk: int, rows: pd.DataFrame) -> int:
    """Replace the week's batch with `rows`, in one transaction. Returns rows saved.

    Decisions are kept: they're keyed by run id, and regenerating the same suggestion
    gives the same id.
    """
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM batches WHERE yr = ? AND wk = ?", [yr, wk])
        con.register("new_batch", rows[BATCH_COLUMNS])
        con.execute(f"INSERT INTO batches ({', '.join(BATCH_COLUMNS)}) SELECT * FROM new_batch")
        con.unregister("new_batch")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(rows)
