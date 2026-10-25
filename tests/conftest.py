"""Shared test helpers: a made-up export mapping and a builder for tiny raw exports."""

import pandas as pd
import pytest

from transfer_engine.settings import ExportMapping


@pytest.fixture
def mapping() -> ExportMapping:
    return ExportMapping(
        store_match={"North": "N", "South": "S"},
        exclude_stores=["Outlet"],
        brand_prefixes={"MNA": ["Men", "Brand M"], "WMA": ["Women", "Brand W"]},
        split_colours=[["WMA DRESS", "Black"]],
    )


def export_row(**overrides: object) -> dict[str, object]:
    """One raw export row with sensible defaults; override any Power BI column by keyword.

    Keyword names use underscores for spaces, e.g. `Sales_Qty=2`.
    """
    row: dict[str, object] = {
        "Week Name Year": "Week 41, 2026",
        "Store Name": "XX - North Mall",
        "Style Name": "MNA SHIRT",
        "Style Colour Name": "Blue",
        "Style Variant Name": None,
        "Style Number Code": 1001.0,
        "Style Size Name": "m",
        "Sales Qty": 1.0,
        "Sales iV": 50.0,
        "Mark Down iV %": 0.0,
        "Stock EOH QTY": 4.0,
        "Gross Profit": 20.0,
        "Remaining Qty": None,
        "Style Master Category Name": "Shirts",
        "Norm Ultimo": None,
    }
    for key, value in overrides.items():
        row[key.replace("_", " ")] = value
    return row


def raw_export(*rows: dict[str, object]) -> pd.DataFrame:
    """A raw export table from rows built with `export_row`."""
    return pd.DataFrame(list(rows))
