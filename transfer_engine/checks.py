"""Safety checks and the preview shown before a weekly export is saved.

Structural problems (more than one week, a store missing) block the upload. A big drop
in sales also fails, but can be overridden: a store closed for a storm is a real reason
for a low week, and only the user knows that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import duckdb
import pandas as pd

from transfer_engine.export import ParsedExport
from transfer_engine.settings import UploadRules
from transfer_engine.weeks import previous_week, same_week_last_year


@dataclass(frozen=True)
class Check:
    """One check's result. `overridable` failures can be accepted by the user."""

    name: str
    status: Literal["pass", "fail", "skip"]
    detail: str
    overridable: bool = False


@dataclass(frozen=True)
class Preview:
    """What the user sees before confirming."""

    yr: int
    wk: int
    by_store: pd.DataFrame  # store, rows, units, sales, sales_prev_week, vs_prev_week, sales_ly, vs_ly
    sales: float
    sales_prev_week: float | None
    sales_ly: float | None
    rows_replaced: int  # rows already in history for this week (0 for a new week)


def week_sales(con: duckdb.DuckDBPyConnection, yr: int, wk: int) -> pd.Series:
    """Sales value per store for one week of history (empty if the week isn't there)."""
    rows = con.execute(
        "SELECT store, sum(v) AS v FROM history WHERE yr = ? AND wk = ? GROUP BY store",
        [yr, wk],
    ).fetchall()
    return pd.Series({store: v for store, v in rows}, dtype=float)


def run_checks(
    parsed: ParsedExport,
    con: duckdb.DuckDBPyConnection,
    rules: UploadRules,
    expected_stores: list[str],
) -> list[Check]:
    """Run every check from the agreed upload safety net and return all results."""
    history = parsed.history
    weeks = sorted({(int(y), int(w)) for y, w in zip(history["yr"], history["wk"])})
    checks = [_check_one_week(weeks), _check_stores(set(history["store"]), expected_stores)]

    if len(weeks) != 1:
        skip = "Skipped: needs exactly one week."
        checks += [Check("Sales vs last week", "skip", skip), Check("Sales vs same week last year", "skip", skip)]
        return checks

    yr, wk = weeks[0]
    sales = float(history["v"].sum())
    checks.append(
        _check_sales_ratio("Sales vs last week", sales, previous_week(yr, wk), con, rules.min_sales_ratio)
    )
    checks.append(
        _check_sales_ratio(
            "Sales vs same week last year", sales, same_week_last_year(yr, wk), con, rules.min_sales_ratio
        )
    )
    return checks


def build_preview(parsed: ParsedExport, con: duckdb.DuckDBPyConnection) -> Preview:
    """Week, per-store totals and comparisons for the confirm screen. Expects one week."""
    history = parsed.history
    yr, wk = int(history["yr"].iat[0]), int(history["wk"].iat[0])

    by_store = (
        history.groupby("store")
        .agg(rows=("sty", "size"), units=("q", "sum"), sales=("v", "sum"))
        .reset_index()
    )
    prev = week_sales(con, *previous_week(yr, wk))
    by_store["sales_prev_week"] = by_store["store"].map(prev)
    by_store["vs_prev_week"] = by_store["sales"] / by_store["sales_prev_week"] - 1

    ly_week = same_week_last_year(yr, wk)
    ly = week_sales(con, *ly_week) if ly_week else pd.Series(dtype=float)
    by_store["sales_ly"] = by_store["store"].map(ly)
    by_store["vs_ly"] = by_store["sales"] / by_store["sales_ly"] - 1

    rows_replaced = con.execute(
        "SELECT count(*) FROM history WHERE yr = ? AND wk = ?", [yr, wk]
    ).fetchone()[0]

    return Preview(
        yr=yr,
        wk=wk,
        by_store=by_store,
        sales=float(history["v"].sum()),
        sales_prev_week=float(prev.sum()) if len(prev) else None,
        sales_ly=float(ly.sum()) if len(ly) else None,
        rows_replaced=rows_replaced,
    )


def _check_one_week(weeks: list[tuple[int, int]]) -> Check:
    """The export must hold exactly one week, so a whole week is replaced at a time."""
    name = "Exactly one week"
    if len(weeks) == 1:
        return Check(name, "pass", f"Week {weeks[0][1]}, {weeks[0][0]}")
    if not weeks:
        return Check(name, "fail", "No rows left after cleaning.")
    found = ", ".join(f"{w}/{y}" for y, w in weeks)
    return Check(name, "fail", f"Found {len(weeks)} weeks: {found}. Export one week at a time.")


def _check_stores(found: set[str], expected: list[str]) -> Check:
    """Every store must be there: a missing store would look like a week of zero sales."""
    name = "All stores present"
    missing = sorted(set(expected) - found)
    if missing:
        return Check(name, "fail", f"Missing: {', '.join(missing)}")
    return Check(name, "pass", ", ".join(sorted(found)))


def _check_sales_ratio(
    name: str,
    sales: float,
    week: tuple[int, int] | None,
    con: duckdb.DuckDBPyConnection,
    min_ratio: float,
) -> Check:
    """Total sales must be at least `min_ratio` of a comparison week.

    A much lower total usually means a filter was left on in Power BI, so the export is
    incomplete. Skipped when history has no rows for the comparison week.
    """
    if week is None:
        return Check(name, "skip", "No comparison week (last year had no week 53).")
    compare = week_sales(con, *week).sum()
    label = f"week {week[1]}, {week[0]}"
    if not compare:
        return Check(name, "skip", f"No history for {label}.")
    ratio = sales / compare
    detail = f"€{sales:,.0f} vs €{compare:,.0f} in {label} ({ratio:.0%})"
    if ratio < min_ratio:
        return Check(name, "fail", f"{detail}, below {min_ratio:.0%}.", overridable=True)
    return Check(name, "pass", detail)
