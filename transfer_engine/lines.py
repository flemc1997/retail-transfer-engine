"""Build the rules' input from history: one row per style, colour and store for a week.

This is the only part of the transfer engine that touches the database; the rules in
`rules.py` just get the tables built here.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from transfer_engine.settings import Settings
from transfer_engine.weeks import shift_week


def week_keys(yr: int, wk: int, n: int) -> list[int]:
    """The n weeks ending at (yr, wk), inclusive, as yr*100+wk keys (handles week 53)."""
    return [y * 100 + w for y, w in (shift_week(yr, wk, -k) for k in range(n))]


def weeks_between(earlier_key: int, later_key: int) -> int:
    """Whole weeks from one yr*100+wk key to another, on the ISO calendar."""
    start = date.fromisocalendar(earlier_key // 100, earlier_key % 100, 1)
    end = date.fromisocalendar(later_key // 100, later_key % 100, 1)
    return (end - start).days // 7


def build_lines(con: duckdb.DuckDBPyConnection, settings: Settings, yr: int, wk: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (lines, sizes) for the week, ready for the rules.

    - Stock and on-the-way come from that week; negative values count as 0.
    - Rates are units sold per week over each rule's window; a net-negative store
      (more returns than sales) counts as 0.
    - Only style-colours with stock somewhere are included: nothing else can move.
    """
    now = yr * 100 + wk
    move_weeks = week_keys(yr, wk, settings.moves.rate_weeks)
    tidy_weeks = week_keys(yr, wk, settings.tidy_ups.rate_weeks)

    sizes = con.execute(
        """
        SELECT sty, col, store, size, greatest(st, 0) AS stock, greatest(coalesce(rem, 0), 0) AS on_way
        FROM history WHERE yr = ? AND wk = ?
        """,
        [yr, wk],
    ).df()

    lines = con.execute(
        """
        WITH now_stock AS (
            SELECT sty, col, store,
                   sum(greatest(st, 0)) AS stock,
                   sum(greatest(coalesce(rem, 0), 0)) AS on_way,
                   count(*) FILTER (WHERE st > 0) AS n_sizes
            FROM history WHERE yr = $yr AND wk = $wk
            GROUP BY ALL
        ), sales AS (
            SELECT sty, col, store,
                   greatest(sum(q) FILTER (WHERE list_contains($move_weeks, yr * 100 + wk)), 0) AS move_units,
                   greatest(sum(q) FILTER (WHERE list_contains($tidy_weeks, yr * 100 + wk)), 0) AS tidy_units
            FROM history WHERE list_contains($tidy_weeks || $move_weeks, yr * 100 + wk)
            GROUP BY ALL
        ), joined AS (
            SELECT sty, col, store,
                   coalesce(s.stock, 0) AS stock, coalesce(s.on_way, 0) AS on_way,
                   coalesce(s.n_sizes, 0) AS n_sizes,
                   coalesce(u.move_units, 0) AS move_units, coalesce(u.tidy_units, 0) AS tidy_units
            FROM now_stock s FULL OUTER JOIN sales u USING (sty, col, store)
        ), in_stock AS (
            SELECT sty, col FROM joined GROUP BY ALL HAVING sum(stock) > 0
        ), first_stocked AS (
            SELECT sty, col, min(yr * 100 + wk) AS first_key
            FROM history WHERE st > 0 AND yr * 100 + wk <= $now GROUP BY ALL
        ), normed AS (
            SELECT DISTINCT sty, col FROM norms WHERE norm > 0
        ), category AS (
            SELECT sty, any_value(cat) AS cat FROM history GROUP BY sty
        )
        SELECT j.sty, j.col, j.store, j.stock, j.on_way, j.n_sizes,
               j.move_units, j.tidy_units,
               n.sty IS NOT NULL AS normed, f.first_key, c.cat
        FROM joined j
        JOIN in_stock USING (sty, col)
        LEFT JOIN normed n USING (sty, col)
        LEFT JOIN first_stocked f USING (sty, col)
        LEFT JOIN category c USING (sty)
        ORDER BY j.sty, j.col, j.store
        """,
        {"yr": yr, "wk": wk, "now": now, "move_weeks": move_weeks, "tidy_weeks": tidy_weeks},
    ).df()

    lines["move_rate"] = lines["move_units"] / settings.moves.rate_weeks
    lines["tidy_rate"] = lines["tidy_units"] / settings.tidy_ups.rate_weeks
    lines["weeks_since_first_stocked"] = [weeks_between(int(k), now) for k in lines["first_key"]]
    columns = ["sty", "col", "store", "stock", "on_way", "move_rate", "tidy_rate", "n_sizes",
               "normed", "weeks_since_first_stocked", "cat"]
    return lines[columns], sizes
