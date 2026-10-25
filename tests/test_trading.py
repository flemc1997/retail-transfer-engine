"""Tests for the trading page's numbers, on small hand-made histories."""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from transfer_engine.decisions import Decision, save_decisions
from transfer_engine.settings import TradingRules
from transfer_engine.storage import connect
from transfer_engine.trading import sales_summary, slow_movers, transfer_followup, with_total

RULES = TradingRules(
    slow_window_weeks=13, slow_max_units=2, slow_new_in_weeks=10,
    slow_seasonal_share=0.5, followup_received_share=0.7,
)


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    connection = connect(tmp_path / "retail.duckdb")
    yield connection
    connection.close()


def add(con: duckdb.DuckDBPyConnection, yr: int, wk: int, store: str, q: int, v: float, st: int,
        sty: str = "STYLE", col: str = "Red", size: str = "M", cat: str = "Shirts") -> None:
    con.execute(
        "INSERT INTO history VALUES (?, ?, ?, 'Men', 'Brand M', ?, 1, ?, ?, ?, ?, ?, 0, ?, 0, NULL)",
        [yr, wk, store, cat, sty, col, size, q, v, st],
    )


# ---------------------------------------------------------------- sales summary

def test_sales_vs_last_week_and_last_year(con: duckdb.DuckDBPyConnection) -> None:
    add(con, 2026, 42, "A", q=3, v=150.0, st=-2)  # negative stock counts as 0
    add(con, 2026, 42, "A", q=1, v=50.0, st=4, size="L")
    add(con, 2026, 41, "A", q=2, v=100.0, st=6)
    add(con, 2025, 42, "A", q=8, v=400.0, st=9)
    add(con, 2026, 42, "B", q=1, v=30.0, st=1)  # B has no history to compare with

    table = sales_summary(con, 2026, 42, ["store"]).set_index("store")

    assert table.loc["A", ["sales", "units", "stock"]].tolist() == [200.0, 4, 4]
    assert table.loc["A", "vs_lw"] == pytest.approx(1.0)  # 200 vs 100
    assert table.loc["A", "vs_ly"] == pytest.approx(-0.5)  # 200 vs 400
    assert pd.isna(table.loc["B", "vs_ly"])


def test_sales_by_category(con: duckdb.DuckDBPyConnection) -> None:
    add(con, 2026, 42, "A", q=1, v=10.0, st=1, cat="Shirts")
    add(con, 2026, 42, "B", q=1, v=20.0, st=1, cat="Shirts")
    add(con, 2026, 42, "A", q=1, v=5.0, st=1, cat="Knit", sty="JUMPER")

    table = sales_summary(con, 2026, 42, ["dept", "cat"])

    assert table[["cat", "sales"]].values.tolist() == [["Knit", 5.0], ["Shirts", 30.0]]


def test_total_row_recalculates_changes(con: duckdb.DuckDBPyConnection) -> None:
    add(con, 2026, 42, "A", q=1, v=110.0, st=1)
    add(con, 2025, 42, "A", q=1, v=100.0, st=1)  # A: +10%
    add(con, 2026, 42, "B", q=1, v=300.0, st=1)
    add(con, 2025, 42, "B", q=1, v=200.0, st=1)  # B: +50%

    total = with_total(sales_summary(con, 2026, 42, ["store"]), "store").set_index("store").loc["Total"]

    assert total["sales"] == 410.0 and total["sales_ly"] == 300.0
    assert total["vs_ly"] == pytest.approx(410 / 300 - 1)  # +36.7%, not +60%
    assert pd.isna(total["vs_lw"])  # no last week anywhere


# ---------------------------------------------------------------- slow movers

def stocked_since(con: duckdb.DuckDBPyConnection, first_wk: int, sty: str, **kwargs: object) -> None:
    """A line held from `first_wk` of 2026 through week 42, with no sales unless given."""
    add(con, 2026, first_wk, "A", q=0, v=0.0, st=5, sty=sty)
    add(con, 2026, 42, "A", q=kwargs.get("q", 0), v=kwargs.get("v", 0.0), st=5, sty=sty)


def test_slow_movers_and_exceptions(con: duckdb.DuckDBPyConnection) -> None:
    stocked_since(con, 20, "NEVER SOLD")
    stocked_since(con, 20, "SELLING", q=3, v=60.0)  # 3 units in 13 weeks: not slow
    stocked_since(con, 36, "NEW IN")  # first stocked 6 weeks ago
    stocked_since(con, 20, "COAT")
    add(con, 2025, 45, "A", q=3, v=300.0, st=0, sty="COAT")  # sold 3 of today's 5 coming up last year

    table = slow_movers(con, 2026, 42, RULES)

    assert table[["sty", "status"]].values.tolist() == [["NEVER SOLD", "Never sold"]]


def test_slow_mover_value_uses_line_price(con: duckdb.DuckDBPyConnection) -> None:
    add(con, 2025, 10, "A", q=2, v=80.0, st=10, sty="OLD")  # sold 2 at 40 each, long ago
    add(con, 2026, 42, "A", q=0, v=0.0, st=5, sty="OLD")

    row = slow_movers(con, 2026, 42, RULES).iloc[0]

    assert (row["status"], row["value"]) == ("Non-mover", 200.0)


# ---------------------------------------------------------------- transfer follow-up

def approve_run(con: duckdb.DuckDBPyConnection, run_id: str, sizes: dict[str, int], partial: dict[str, int] | None = None) -> None:
    """A run from A to B in week 41's batch, approved (or partially approved)."""
    for size, qty in sizes.items():
        con.execute(
            "INSERT INTO batches (run_id, yr, wk, kind, sender, receiver, sty, col, size, qty) "
            "VALUES (?, 2026, 41, 'move', 'A', 'B', 'STYLE', 'Red', ?, ?)",
            [run_id, size, qty],
        )
    status = "partial" if partial else "approved"
    save_decisions(con, [Decision(run_id, status, sizes=partial)])


def test_followup_received(con: duckdb.DuckDBPyConnection) -> None:
    approve_run(con, "r1", {"M": 4})
    add(con, 2026, 41, "A", q=0, v=0.0, st=4)
    add(con, 2026, 41, "B", q=0, v=0.0, st=0)
    add(con, 2026, 42, "A", q=0, v=0.0, st=0)  # sender is empty...
    add(con, 2026, 42, "B", q=1, v=10.0, st=3)  # ...receiver has 3 and sold 1: all 4 arrived

    row = transfer_followup(con, 2026, 42, RULES).iloc[0]

    assert (row["units"], row["left_sender"], row["arrived"], row["status"]) == (4, 4, 4, "Received")


def test_followup_not_sent(con: duckdb.DuckDBPyConnection) -> None:
    approve_run(con, "r1", {"M": 4})
    add(con, 2026, 41, "A", q=0, v=0.0, st=4)
    add(con, 2026, 42, "A", q=0, v=0.0, st=4)  # nothing moved

    assert transfer_followup(con, 2026, 42, RULES)["status"].tolist() == ["Not sent"]


def test_followup_partial_only_checks_sizes_sent(con: duckdb.DuckDBPyConnection) -> None:
    approve_run(con, "r1", {"M": 2, "L": 3}, partial={"M": 2})
    for size, then_a, now_a, now_b in (("M", 2, 0, 2), ("L", 3, 3, 0)):  # M went, L stayed (as agreed)
        add(con, 2026, 41, "A", q=0, v=0.0, st=then_a, size=size)
        add(con, 2026, 42, "A", q=0, v=0.0, st=now_a, size=size)
        add(con, 2026, 42, "B", q=0, v=0.0, st=now_b, size=size)

    row = transfer_followup(con, 2026, 42, RULES).iloc[0]

    assert (row["units"], row["status"]) == (2, "Received")


def test_followup_sent_but_not_arrived(con: duckdb.DuckDBPyConnection) -> None:
    approve_run(con, "r1", {"M": 4})
    add(con, 2026, 41, "A", q=0, v=0.0, st=4)
    add(con, 2026, 42, "A", q=0, v=0.0, st=0)  # left the sender, not yet at the receiver

    assert transfer_followup(con, 2026, 42, RULES)["status"].tolist() == ["Sent, not all received"]


def test_followup_with_no_approved_runs_is_empty(con: duckdb.DuckDBPyConnection) -> None:
    assert transfer_followup(con, 2026, 42, RULES).empty
