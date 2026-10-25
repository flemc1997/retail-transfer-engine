"""The trading page's numbers: sales vs last week and last year, slow movers, and whether
last week's approved transfers actually happened.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from transfer_engine.decisions import batch_runs
from transfer_engine.settings import TradingRules
from transfer_engine.weeks import previous_week, same_week_last_year, shift_week


def _key(week: tuple[int, int] | None) -> int | None:
    return None if week is None else week[0] * 100 + week[1]


def sales_summary(con: duckdb.DuckDBPyConnection, yr: int, wk: int, by: list[str]) -> pd.DataFrame:
    """Sales this week vs last week and the same week last year, grouped by `by` (e.g. ['store']).

    Columns: the `by` columns, sales, units, stock, sales_lw, vs_lw, sales_ly, vs_ly.
    vs_* are changes as fractions (0.1 = +10%); blank when there's nothing to compare with.
    Stock is closing stock this week, with negative counts treated as 0.
    """
    group = ", ".join(by)
    table = con.execute(
        f"""
        SELECT {group},
               sum(v) FILTER (WHERE yr * 100 + wk = $now) AS sales,
               sum(q) FILTER (WHERE yr * 100 + wk = $now) AS units,
               sum(greatest(st, 0)) FILTER (WHERE yr * 100 + wk = $now) AS stock,
               sum(v) FILTER (WHERE yr * 100 + wk = $lw) AS sales_lw,
               sum(v) FILTER (WHERE yr * 100 + wk = $ly) AS sales_ly
        FROM history
        WHERE yr * 100 + wk IN ($now, $lw, coalesce($ly, -1))
        GROUP BY {group}
        ORDER BY {group}
        """,
        {"now": yr * 100 + wk, "lw": _key(previous_week(yr, wk)), "ly": _key(same_week_last_year(yr, wk))},
    ).df()
    table = table.fillna({"sales": 0.0, "units": 0, "stock": 0})
    table["vs_lw"] = _change(table["sales"], table["sales_lw"])
    table["vs_ly"] = _change(table["sales"], table["sales_ly"])
    return table[[*by, "sales", "units", "stock", "sales_lw", "vs_lw", "sales_ly", "vs_ly"]]


def with_total(table: pd.DataFrame, label_column: str) -> pd.DataFrame:
    """Add a 'Total' row to a `sales_summary` table.

    Amounts are summed; the changes are worked out again from the summed amounts,
    because percentages don't add up (+10% in one store and +10% in another isn't +20%).
    """
    sums = table[["sales", "units", "stock", "sales_lw", "sales_ly"]].sum(min_count=1)
    total = pd.DataFrame([{label_column: "Total", **sums.to_dict()}])
    total["vs_lw"] = _change(total["sales"], total["sales_lw"])
    total["vs_ly"] = _change(total["sales"], total["sales_ly"])
    return pd.concat([table, total], ignore_index=True)


def _change(now: pd.Series, before: pd.Series) -> pd.Series:
    """now / before - 1, blank when before is missing or zero."""
    before = before.where(before != 0)
    return now / before - 1


def slow_movers(con: duckdb.DuckDBPyConnection, yr: int, wk: int, rules: TradingRules) -> pd.DataFrame:
    """Style-colours the chain holds that have barely sold recently, most stock value first.

    A line is slow if it sold `slow_max_units` or fewer over the last `slow_window_weeks`,
    except when it's:
    - new in: first stocked fewer than `slow_new_in_weeks` ago (it hasn't had time), or
    - seasonal: over the same coming weeks last year it sold at least `slow_seasonal_share`
      of today's stock (its season is about to start).
    Status is 'Never sold' or 'Non-mover'. Value = stock x the line's average selling price
    (its category's when the line never sold).
    """
    now = yr * 100 + wk
    window = [_key(shift_week(yr, wk, -k)) for k in range(rules.slow_window_weeks)]
    first_new_in = _key(shift_week(yr, wk, -(rules.slow_new_in_weeks - 1)))
    ly_start = same_week_last_year(yr, wk) or (yr - 1, 52)
    ly_coming = [_key(shift_week(*ly_start, k)) for k in range(1, rules.slow_window_weeks + 1)]

    table = con.execute(
        """
        WITH lines AS (
            SELECT sty, col, any_value(dept) AS dept, any_value(cat) AS cat,
                   sum(greatest(st, 0)) FILTER (WHERE yr * 100 + wk = $now) AS stock,
                   count(DISTINCT store) FILTER (WHERE yr * 100 + wk = $now AND st > 0) AS stores,
                   coalesce(sum(q) FILTER (WHERE list_contains($window, yr * 100 + wk)), 0) AS recent_units,
                   coalesce(sum(q) FILTER (WHERE list_contains($ly_coming, yr * 100 + wk)), 0) AS ly_coming_units,
                   coalesce(sum(q) FILTER (WHERE yr * 100 + wk <= $now), 0) AS units_ever,
                   min(yr * 100 + wk) FILTER (WHERE st > 0 AND yr * 100 + wk <= $now) AS first_stocked,
                   sum(v) FILTER (WHERE q > 0) / nullif(sum(q) FILTER (WHERE q > 0), 0) AS line_price
            FROM history
            GROUP BY sty, col
        ), cat_price AS (
            SELECT cat, sum(v) FILTER (WHERE q > 0) / nullif(sum(q) FILTER (WHERE q > 0), 0) AS cat_price
            FROM history GROUP BY cat
        )
        SELECT l.*, coalesce(l.line_price, c.cat_price, 0) * l.stock AS value
        FROM lines l LEFT JOIN cat_price c USING (cat)
        WHERE l.stock > 0
          AND l.recent_units <= $max_units
          AND l.first_stocked < $first_new_in
          AND l.ly_coming_units < $seasonal_share * l.stock
        ORDER BY value DESC, sty, col
        """,
        {
            "now": now, "window": window, "ly_coming": ly_coming, "first_new_in": first_new_in,
            "max_units": rules.slow_max_units, "seasonal_share": rules.slow_seasonal_share,
        },
    ).df()
    table["status"] = ["Never sold" if u <= 0 else "Non-mover" for u in table["units_ever"]]
    return table[["dept", "cat", "sty", "col", "stock", "stores", "recent_units", "status", "value"]]


def transfer_followup(con: duckdb.DuckDBPyConnection, yr: int, wk: int, rules: TradingRules) -> pd.DataFrame:
    """Did the transfers approved in last week's batch happen? Estimated from stock movement.

    For each approved (or partial) run, looking only at the sizes that were sent:
    - left the sender  = sender's stock then - its stock now - what it sold this week
    - arrived          = receiver's stock now + what it sold this week - its stock then
    'Received' when at least `followup_received_share` of the units arrived; 'Not sent' when
    nothing moved; otherwise 'Sent, not all received'. Deliveries from the warehouse or
    returns can blur this, so it's an estimate, not a ledger.
    """
    batch_yr, batch_wk = previous_week(yr, wk)
    runs = batch_runs(con, batch_yr, batch_wk)
    runs = runs[runs["status"].isin(["approved", "partial"])]
    columns = ["run_id", "sender", "receiver", "sty", "col", "units", "left_sender", "arrived", "status"]
    if runs.empty:
        return pd.DataFrame(columns=columns)

    sent = pd.DataFrame(
        [
            {"run_id": r.run_id, "sender": r.sender, "receiver": r.receiver, "sty": r.sty, "col": r.col, "size": size, "qty": qty}
            for r in runs.itertuples()
            for size, qty in (r.sent_sizes if r.status == "partial" else r.sizes).items()
        ]
    )
    con.register("sent_sizes", sent)
    try:
        moved = con.execute(
            """
            WITH stock AS (
                SELECT store, sty, col, size,
                       coalesce(sum(greatest(st, 0)) FILTER (WHERE yr * 100 + wk = $then), 0) AS then_st,
                       coalesce(sum(greatest(st, 0)) FILTER (WHERE yr * 100 + wk = $now), 0) AS now_st,
                       coalesce(sum(greatest(q, 0)) FILTER (WHERE yr * 100 + wk = $now), 0) AS sold
                FROM history WHERE yr * 100 + wk IN ($then, $now)
                GROUP BY ALL
            )
            SELECT s.run_id,
                   any_value(s.sender) AS sender, any_value(s.receiver) AS receiver,
                   any_value(s.sty) AS sty, any_value(s.col) AS col,
                   sum(s.qty) AS units,
                   greatest(sum(coalesce(f.then_st, 0) - coalesce(f.now_st, 0) - coalesce(f.sold, 0)), 0) AS left_sender,
                   greatest(sum(coalesce(t.now_st, 0) + coalesce(t.sold, 0) - coalesce(t.then_st, 0)), 0) AS arrived
            FROM sent_sizes s
            LEFT JOIN stock f
                   ON f.store = s.sender AND f.sty = s.sty AND f.col = s.col AND f.size = s.size
            LEFT JOIN stock t
                   ON t.store = s.receiver AND t.sty = s.sty AND t.col = s.col AND t.size = s.size
            GROUP BY s.run_id
            """,
            {"then": batch_yr * 100 + batch_wk, "now": yr * 100 + wk},
        ).df()
    finally:
        con.unregister("sent_sizes")

    moved["status"] = [
        _followup_status(units, left, arrived, rules.followup_received_share)
        for units, left, arrived in zip(moved["units"], moved["left_sender"], moved["arrived"])
    ]
    order = {"Not sent": 0, "Sent, not all received": 1, "Received": 2}
    return moved.sort_values(["status", "sender", "sty"], key=lambda c: c.map(order) if c.name == "status" else c)[columns]


def _followup_status(units: float, left_sender: float, arrived: float, received_share: float) -> str:
    if arrived >= received_share * units:
        return "Received"
    if left_sender < 1 and arrived < 1:
        return "Not sent"
    return "Sent, not all received"
