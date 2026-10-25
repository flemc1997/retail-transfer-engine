"""Tests for the export parser, on tiny made-up exports."""

from pathlib import Path

import pandas as pd
import pytest

from conftest import export_row, raw_export
from transfer_engine.export import ExportError, clean_variant, parse_export, read_export
from transfer_engine.settings import ExportMapping


def test_basic_row_becomes_history(mapping: ExportMapping) -> None:
    parsed = parse_export(raw_export(export_row()), mapping)

    row = parsed.history.iloc[0].to_dict()
    assert row["yr"] == 2026 and row["wk"] == 41
    assert row["store"] == "N"
    assert (row["dept"], row["brand"], row["cat"]) == ("Men", "Brand M", "Shirts")
    assert row["size"] == "M"  # sizes are upper-cased
    assert (row["q"], row["st"], row["num"]) == (1, 4, 1001)


def test_footer_placeholder_and_other_brand_rows_are_skipped(mapping: ExportMapping) -> None:
    raw = raw_export(
        export_row(),
        export_row(Style_Name="?"),
        export_row(Style_Name="ZZZ OTHER BRAND TEE"),
        export_row(Week_Name_Year="Total", Store_Name=None),
        export_row(Store_Name="XX - Outlet Village"),
    )

    parsed = parse_export(raw, mapping)

    assert len(parsed.history) == 1
    assert parsed.skipped == {
        "footer rows": 1,
        "excluded stores": 1,
        "placeholder styles": 1,
        "other brands": 1,
    }


def test_unknown_store_stops_the_upload(mapping: ExportMapping) -> None:
    with pytest.raises(ExportError, match="Unknown store names"):
        parse_export(raw_export(export_row(Store_Name="XX - Brand New Store")), mapping)


def test_blank_sales_and_stock_become_zero(mapping: ExportMapping) -> None:
    raw = raw_export(export_row(Sales_Qty=None, Sales_iV=None, Stock_EOH_QTY=None, Remaining_Qty=2.0))

    row = parsed_row(raw, mapping)

    assert (row["q"], row["v"], row["st"], row["rem"]) == (0, 0.0, 0, 2)


def test_split_colour_gets_its_variant(mapping: ExportMapping) -> None:
    raw = raw_export(
        export_row(Style_Name="WMA DRESS", Style_Colour_Name="Black", Style_Variant_Name="Detail:Twist"),
        export_row(Style_Name="WMA DRESS", Style_Colour_Name="Red", Style_Variant_Name="Detail:Twist"),
    )

    colours = set(parse_export(raw, mapping).history["col"])

    assert colours == {"Black · Twist", "Red"}  # only listed style-colours are split


def test_rows_sharing_a_key_are_added_together(mapping: ExportMapping) -> None:
    raw = raw_export(
        export_row(Style_Variant_Name="A", Sales_Qty=1.0, Sales_iV=100.0, Stock_EOH_QTY=2.0),
        export_row(Style_Variant_Name="B", Sales_Qty=3.0, Sales_iV=300.0, Stock_EOH_QTY=1.0),
    )
    raw["Mark Down iV %"] = [0.4, 0.0]  # '%' can't go in a keyword name

    parsed = parse_export(raw, mapping)

    assert len(parsed.history) == 1
    row = parsed.history.iloc[0]
    assert (row["q"], row["v"], row["st"]) == (4, 400.0, 3)
    assert row["md"] == pytest.approx(0.1)  # weighted by sales: (0.4*100 + 0*300) / 400


def test_empty_rows_leave_history_but_keep_their_norm(mapping: ExportMapping) -> None:
    raw = raw_export(
        export_row(Sales_Qty=None, Sales_iV=None, Stock_EOH_QTY=None, Norm_Ultimo=3.0),
    )

    parsed = parse_export(raw, mapping)

    assert parsed.history.empty
    assert parsed.norms.to_dict("records") == [
        {"store": "N", "sty": "MNA SHIRT", "col": "Blue", "size": "M", "norm": 3}
    ]


def test_each_style_gets_one_category(mapping: ExportMapping) -> None:
    raw = raw_export(
        export_row(Style_Size_Name="S", Style_Master_Category_Name="Shirts"),
        export_row(Style_Size_Name="M", Style_Master_Category_Name="Shirts"),
        export_row(Style_Size_Name="L", Style_Master_Category_Name="Knit"),
    )

    assert set(parse_export(raw, mapping).history["cat"]) == {"Shirts"}


def test_fractional_units_are_rejected(mapping: ExportMapping) -> None:
    with pytest.raises(ExportError, match="fractions in: q"):
        parse_export(raw_export(export_row(Sales_Qty=1.5)), mapping)


def test_read_export_names_missing_columns(tmp_path: Path) -> None:
    path = tmp_path / "export.xlsx"
    raw_export(export_row()).drop(columns=["Remaining Qty", "Norm Ultimo"]).to_excel(path, index=False)

    with pytest.raises(ExportError, match="Remaining Qty, Norm Ultimo"):
        read_export(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Detail:Twist", "Twist"), ("  ¨MIXED  stripe ", "MIXED stripe"), ("?", ""), (None, "")],
)
def test_clean_variant(value: object, expected: str) -> None:
    assert clean_variant(value) == expected


def parsed_row(raw: pd.DataFrame, mapping: ExportMapping) -> dict[str, object]:
    """The single history row parsed from `raw`."""
    history = parse_export(raw, mapping).history
    assert len(history) == 1
    return history.iloc[0].to_dict()
