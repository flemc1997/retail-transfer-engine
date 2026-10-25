"""Tests for decisions, partial approvals and the picking list."""

from io import BytesIO
from pathlib import Path

import duckdb
import pytest
from openpyxl import load_workbook

from transfer_engine.decisions import (
    Decision,
    batch_runs,
    partial_decision,
    pending_run_ids,
    picking_rows,
    save_decisions,
    size_sort_key,
    sizes_text,
)
from transfer_engine.picking import picking_list_xlsx
from transfer_engine.storage import connect


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    """A database with one week's batch: run r1 (S:1 M:2, A -> B) and run r2 (L:3, C -> B)."""
    connection = connect(tmp_path / "retail.duckdb")
    rows = [
        ("r1", "move", "A", "B", "SHIRT", "Blue", "M", 2),
        ("r1", "move", "A", "B", "SHIRT", "Blue", "S", 1),
        ("r2", "tidy", "C", "B", "DRESS", "Red", "L", 3),
    ]
    for run_id, kind, sender, receiver, sty, col, size, qty in rows:
        connection.execute(
            "INSERT INTO batches (run_id, yr, wk, kind, sender, receiver, sty, col, size, qty, reason) "
            "VALUES (?, 2026, 42, ?, ?, ?, ?, ?, ?, ?, 'because')",
            [run_id, kind, sender, receiver, sty, col, size, qty],
        )
    connection.execute(
        "INSERT INTO history VALUES (2026, 42, 'A', 'Men', 'B', 'Shirts', 1001, 'SHIRT', 'Blue', 'M', 0, 0, 0, 2, 0, NULL)"
    )
    yield connection
    connection.close()


def test_sizes_sort_in_wearing_order() -> None:
    assert sorted(["XL", "S", "32", "M", "ONE SIZE", "30"], key=size_sort_key) == ["S", "M", "XL", "30", "32", "ONE SIZE"]
    assert sizes_text({"M": 2, "S": 1, "L": 0}) == "S:1 M:2"


def test_runs_start_pending_with_their_sizes(con: duckdb.DuckDBPyConnection) -> None:
    runs = batch_runs(con, 2026, 42).set_index("run_id")

    assert runs.loc["r1", "sizes"] == {"S": 1, "M": 2}
    assert runs.loc["r1", "qty"] == 3
    assert runs["status"].isna().all()


@pytest.mark.parametrize(
    ("chosen", "status", "sizes"),
    [
        ({"S": 1, "M": 1}, "partial", {"S": 1, "M": 1}),
        ({"S": 0, "M": 2}, "partial", {"M": 2}),  # zero sizes are left out
        ({"S": 1, "M": 2}, "approved", None),  # everything chosen = a full approval
    ],
)
def test_partial_decision(chosen: dict[str, int], status: str, sizes: dict[str, int] | None) -> None:
    decision = partial_decision("r1", {"S": 1, "M": 2}, chosen)
    assert (decision.status, decision.sizes) == (status, sizes)


@pytest.mark.parametrize(
    ("chosen", "message"),
    [({"S": 0, "M": 0}, "reject the run instead"), ({"S": 2, "M": 0}, "More than the sender holds")],
)
def test_partial_decision_rejects_nonsense(chosen: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        partial_decision("r1", {"S": 1, "M": 2}, chosen)


def test_save_replace_and_clear(con: duckdb.DuckDBPyConnection) -> None:
    save_decisions(con, [Decision("r1", "approved"), Decision("r2", "rejected", "Too few")])
    save_decisions(con, [Decision("r1", "partial", "Keep the M", {"S": 1})], clear=["r2"])

    runs = batch_runs(con, 2026, 42).set_index("run_id")
    assert (runs.loc["r1", "status"], runs.loc["r1", "sent_sizes"], runs.loc["r1", "note"]) == ("partial", {"S": 1}, "Keep the M")
    assert runs.loc["r2", "status"] is None  # cleared back to pending
    assert con.execute("SELECT count(*) FROM decisions").fetchone() == (1,)


def test_pending_runs_respect_decisions_and_filters(con: duckdb.DuckDBPyConnection) -> None:
    save_decisions(con, [Decision("r1", "rejected")])
    runs = batch_runs(con, 2026, 42)

    assert pending_run_ids(runs) == ["r2"]
    assert pending_run_ids(runs[runs["kind"] == "move"]) == []  # only r1 is a move, and it's decided


def test_picking_list_sends_full_and_partial_sizes(con: duckdb.DuckDBPyConnection) -> None:
    save_decisions(con, [Decision("r1", "partial", "Keep the M", {"S": 1}), Decision("r2", "approved")])

    rows = picking_rows(con, 2026, 42)

    assert rows[["sender", "sizes_text", "units", "full_partial"]].values.tolist() == [
        ["A", "S:1", 1, "Partial"],
        ["C", "L:3", 3, "Full"],
    ]
    assert rows.loc[0, "num"] == 1001


def test_rejected_and_pending_runs_are_not_picked(con: duckdb.DuckDBPyConnection) -> None:
    save_decisions(con, [Decision("r1", "rejected")])
    assert picking_rows(con, 2026, 42).empty


def test_picking_list_workbook(con: duckdb.DuckDBPyConnection) -> None:
    save_decisions(con, [Decision("r2", "approved", "Fragile")])

    wb = load_workbook(BytesIO(picking_list_xlsx(picking_rows(con, 2026, 42), 2026, 42)))

    ws = wb["Picking list"]
    assert "week 42, 2026" in ws["A1"].value
    assert [c.value for c in ws[3]] == [
        "Send from", "Send to", "Style", "Style No.", "Colour", "Sizes", "Units", "Full / Partial", "Notes", "Sent ✓",
    ]
    assert [c.value for c in ws[4]] == ["C", "B", "DRESS", None, "Red", "L:3", 3, "Full", "Fragile", None]
