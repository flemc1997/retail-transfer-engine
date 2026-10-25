"""Read the weekly Power BI export and turn it into rows for `history` and `norms`.

The export has one row per week, store, style, colour and size, plus a few footer rows
(Total, blank, 'Applied filters'). `parse_export` is a pure function: a raw table in,
clean rows out, so it can be tested on small hand-made tables.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from transfer_engine.settings import ExportMapping
from transfer_engine.storage import HISTORY_COLUMNS, NORMS_COLUMNS

# Power BI column name -> what it becomes.
REQUIRED_COLUMNS = {
    "Week Name Year": "yr / wk",
    "Store Name": "store",
    "Style Name": "sty",
    "Style Colour Name": "col",
    "Style Variant Name": "col (split colours only)",
    "Style Number Code": "num",
    "Style Size Name": "size",
    "Sales Qty": "q",
    "Sales iV": "v",
    "Mark Down iV %": "md",
    "Stock EOH QTY": "st",
    "Gross Profit": "gp",
    "Remaining Qty": "rem",
    "Style Master Category Name": "cat",
    "Norm Ultimo": "norm",
}

KEY = ["store", "sty", "col", "size"]
WEEK_PATTERN = r"Week (\d+), (\d{4})"
PLACEHOLDER_STYLES = {"?", "Total"}


class ExportError(Exception):
    """The export can't be used as it is; the message says what to fix."""


@dataclass
class ParsedExport:
    """One export, cleaned and ready to save."""

    history: pd.DataFrame  # rows for the history table, in its column order
    norms: pd.DataFrame  # store, sty, col, size, norm (only norm > 0)
    skipped: dict[str, int] = field(default_factory=dict)  # rows left out, and why


def read_export(path: Path) -> pd.DataFrame:
    """Read the export's first sheet and stop if any column we need is missing."""
    with warnings.catch_warnings():
        # Power BI files have no default style; harmless, but openpyxl warns every time.
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        raw = pd.read_excel(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ExportError(f"{path.name} is missing these columns: {', '.join(missing)}")
    return raw


def clean_variant(value: object) -> str:
    """Tidy a variant name: 'Detail:Twist' -> 'Twist'; blank or '?' -> ''; stray leading symbols dropped."""
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text in ("", "?", "nan"):
        return ""
    if ":" in text:
        text = text.split(":", 1)[1]
    text = re.sub(r"^[^0-9A-Za-z]+", "", text.strip())
    return re.sub(r"\s+", " ", text)


def split_colour(sty: str, col: str, variant: object, split: set[tuple[str, str]]) -> str:
    """For style-colours on the split list, the colour becomes 'colour · variant'.

    Everything else keeps its plain colour. This matches how history was built.
    """
    cleaned = clean_variant(variant)
    if (sty, col) in split and cleaned:
        return f"{col} · {cleaned}"
    return col


def store_code(store_name: str, store_match: dict[str, str]) -> str | None:
    """The store code whose match text appears in the Power BI store name."""
    for text, code in store_match.items():
        if text in store_name:
            return code
    return None


def brand_for(sty: str, brand_prefixes: dict[str, list[str]]) -> tuple[str, str] | None:
    """(dept, brand) from the start of the style name, or None for a brand we don't carry."""
    upper = sty.upper()
    for prefix, (dept, brand) in brand_prefixes.items():
        if upper.startswith(prefix.upper()):
            return dept, brand
    return None


def parse_export(raw: pd.DataFrame, mapping: ExportMapping) -> ParsedExport:
    """Turn the raw export into history and norms rows.

    Rules, in order:
    - Only rows whose week reads 'Week N, YYYY' (drops the footer rows).
    - Excluded stores are dropped; any other store must be in `store_match`, or we stop.
    - Placeholder styles ('?', 'Total', blank) and brands we don't carry are dropped.
    - Blank sales count as 0. Blank stock counts as 0 (Power BI leaves it blank when
      there's none; recent history stores 0 too).
    - Split colours get their variant; rows that then share a key are added together.
    - Each style gets one category: its most common master category in the export.
    - Rows with no sales, no stock and nothing in transit stay out of history,
      but their norms are kept.
    """
    skipped: dict[str, int] = {}
    rows = raw[raw["Week Name Year"].astype(str).str.fullmatch(WEEK_PATTERN)].copy()
    skipped["footer rows"] = len(raw) - len(rows)

    store_names = rows["Store Name"].astype(str)
    excluded = store_names.map(lambda s: any(text in s for text in mapping.exclude_stores))
    skipped["excluded stores"] = int(excluded.sum())
    rows = rows[~excluded]

    rows["store"] = rows["Store Name"].astype(str).map(lambda s: store_code(s, mapping.store_match))
    unknown = sorted(rows.loc[rows["store"].isna(), "Store Name"].astype(str).unique())
    if unknown:
        raise ExportError(f"Unknown store names (add them to store_match or exclude_stores): {unknown}")

    placeholder = rows["Style Name"].isna() | rows["Style Name"].astype(str).isin(PLACEHOLDER_STYLES)
    skipped["placeholder styles"] = int(placeholder.sum())
    rows = rows[~placeholder]

    brands = rows["Style Name"].astype(str).map(lambda s: brand_for(s, mapping.brand_prefixes))
    skipped["other brands"] = int(brands.isna().sum())
    rows, brands = rows[brands.notna()], brands[brands.notna()]

    week = rows["Week Name Year"].astype(str).str.extract(WEEK_PATTERN).astype(int)
    split = {(sty, col) for sty, col in mapping.split_colours}
    frame = pd.DataFrame(
        {
            "yr": week[1],
            "wk": week[0],
            "store": rows["store"],
            "dept": brands.str[0],
            "brand": brands.str[1],
            "cat": rows["Style Master Category Name"].astype(str),
            "num": rows["Style Number Code"],
            "sty": rows["Style Name"].astype(str),
            "col": [
                split_colour(s, c, v, split)
                for s, c, v in zip(
                    rows["Style Name"].astype(str),
                    rows["Style Colour Name"].astype(str),
                    rows["Style Variant Name"],
                )
            ],
            "size": rows["Style Size Name"].fillna("?").astype(str).str.upper(),
            "q": rows["Sales Qty"].fillna(0),
            "v": rows["Sales iV"].fillna(0),
            "md": rows["Mark Down iV %"],
            "st": rows["Stock EOH QTY"].fillna(0),
            "gp": rows["Gross Profit"],
            "rem": rows["Remaining Qty"],
            "norm": rows["Norm Ultimo"].fillna(0),
        }
    )
    _check_whole_numbers(frame, ["num", "q", "st", "rem", "norm"])

    frame = _combine_duplicate_keys(frame)
    frame["cat"] = frame.groupby("sty")["cat"].transform(lambda c: c.mode().iat[0])

    norms = frame.loc[frame["norm"] > 0, NORMS_COLUMNS].astype({"norm": int})

    empty = (frame["q"] == 0) & (frame["v"] == 0) & (frame["st"] == 0) & (frame["rem"].fillna(0) == 0)
    skipped["empty rows (no sales, stock or in transit)"] = int(empty.sum())
    history = frame.loc[~empty, HISTORY_COLUMNS].astype(
        {"q": int, "st": int, "md": float, "num": "Int64", "rem": "Int64"}
    )

    return ParsedExport(
        history=history.reset_index(drop=True),
        norms=norms.reset_index(drop=True),
        skipped={reason: n for reason, n in skipped.items() if n},
    )


def _check_whole_numbers(frame: pd.DataFrame, columns: list[str]) -> None:
    """Stop if a units column holds a fraction like 1.5 (it would otherwise be rounded)."""
    bad = [c for c in columns if (frame[c].dropna() % 1 != 0).any()]
    if bad:
        raise ExportError(f"Expected whole numbers but found fractions in: {', '.join(bad)}")


def _combine_duplicate_keys(frame: pd.DataFrame) -> pd.DataFrame:
    """Add up rows that share week, store, style, colour and size.

    This happens when a style has variants but isn't on the split list: its variants
    all fall back to the plain colour. Units, sales, stock, profit, in-transit and norms
    are summed; markdown is averaged weighted by sales value. Rows with a unique key
    are left exactly as they are.
    """
    group = ["yr", "wk", *KEY]
    is_dup = frame.duplicated(group, keep=False)
    if not is_dup.any():
        return frame

    def combine(g: pd.DataFrame) -> pd.Series:
        sales = g["v"].sum()
        return pd.Series(
            {
                "dept": g["dept"].iat[0],
                "brand": g["brand"].iat[0],
                "cat": g["cat"].iat[0],
                "num": g["num"].iat[0],
                "q": g["q"].sum(),
                "v": sales,
                "md": (g["md"] * g["v"]).sum() / sales if sales and g["md"].notna().any() else float("nan"),
                "st": g["st"].sum(),
                "gp": g["gp"].sum(min_count=1),
                "rem": g["rem"].sum(min_count=1),
                "norm": g["norm"].sum(),
            }
        )

    combined = frame[is_dup].groupby(group, sort=False).apply(combine, include_groups=False).reset_index()
    return pd.concat([frame[~is_dup], combined], ignore_index=True)
