"""Tests for the upload checks and preview."""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from conftest import export_row, raw_export
from transfer_engine.checks import build_preview, run_checks
from transfer_engine.export import parse_export
from transfer_engine.settings import ExportMapping, UploadRules
from transfer_engine.storage import connect

RULES = UploadRules(min_sales_ratio=0.6, backups_to_keep=4)


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    connection = connect(tmp_path / "retail.duckdb")
    yield connection
    connection.close()


def add_week_sales(con: duckdb.DuckDBPyConnection, yr: int, wk: int, store: str, sales: float) -> None:
    """Put one history row with the given sales value in a past week."""
    con.execute(
        "INSERT INTO history VALUES (?, ?, ?, 'Men', 'Brand M', 'Shirts', 1, 'MNA SHIRT', 'Blue', 'M', 1, ?, 0, 1, 0, NULL)",
        [yr, wk, store, sales],
    )


def both_stores(sales_each: float = 50.0) -> list[dict[str, object]]:
    return [
        export_row(Store_Name="XX - North Mall", Sales_iV=sales_each),
        export_row(Store_Name="XX - South Park", Sales_iV=sales_each),
    ]


def statuses(checks: list) -> dict[str, str]:
    return {c.name: c.status for c in checks}


def test_good_week_passes(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    add_week_sales(con, 2026, 40, "N", 100.0)  # last week
    add_week_sales(con, 2025, 41, "N", 100.0)  # same week last year
    parsed = parse_export(raw_export(*both_stores()), mapping)

    checks = run_checks(parsed, con, RULES, mapping.store_codes)

    assert set(statuses(checks).values()) == {"pass"}


def test_missing_store_fails(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    parsed = parse_export(raw_export(export_row()), mapping)

    check = next(c for c in run_checks(parsed, con, RULES, mapping.store_codes) if c.name == "All stores present")

    assert check.status == "fail" and "S" in check.detail and not check.overridable


def test_two_weeks_fail(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    rows = [*both_stores(), export_row(Week_Name_Year="Week 42, 2026")]
    parsed = parse_export(raw_export(*rows), mapping)

    result = statuses(run_checks(parsed, con, RULES, mapping.store_codes))

    assert result["Exactly one week"] == "fail"
    assert result["Sales vs last week"] == "skip"


def test_low_sales_fail_but_can_be_overridden(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    add_week_sales(con, 2026, 40, "N", 1000.0)
    parsed = parse_export(raw_export(*both_stores(sales_each=50.0)), mapping)  # 100 vs 1000

    check = next(c for c in run_checks(parsed, con, RULES, mapping.store_codes) if c.name == "Sales vs last week")

    assert check.status == "fail" and check.overridable


def test_no_comparison_week_is_skipped(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    parsed = parse_export(raw_export(*both_stores()), mapping)

    result = statuses(run_checks(parsed, con, RULES, mapping.store_codes))

    assert result["Sales vs last week"] == "skip"
    assert result["Sales vs same week last year"] == "skip"


def test_preview_totals_and_replaced_rows(con: duckdb.DuckDBPyConnection, mapping: ExportMapping) -> None:
    add_week_sales(con, 2025, 41, "N", 40.0)
    add_week_sales(con, 2026, 40, "N", 100.0)  # last week
    add_week_sales(con, 2026, 41, "N", 10.0)  # this week already uploaded once
    parsed = parse_export(raw_export(*both_stores()), mapping)

    preview = build_preview(parsed, con)

    assert (preview.yr, preview.wk, preview.sales, preview.sales_ly) == (2026, 41, 100.0, 40.0)
    assert preview.rows_replaced == 1
    by_store = preview.by_store.set_index("store")
    assert by_store.loc["N", "vs_ly"] == pytest.approx(0.25)
    assert by_store.loc["N", "sales_prev_week"] == 100.0
    assert by_store.loc["N", "vs_prev_week"] == pytest.approx(-0.5)
    assert pd.isna(by_store.loc["S", "vs_prev_week"])  # no history for S last week: blank, not an error
