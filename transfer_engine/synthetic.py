"""Made-up data in the same shape as the real data, so anyone can run the app and tests.

Every style-colour is simulated week by week, per store and size: deliveries arrive,
customers buy (demand follows the season and a size curve), a few units come back, and
older fashion lines are marked down. The line types are chosen so every rule has
something to work on:

- noos:    replenished to a norm every week; the rules must skip these (norms table, `rem`)
- fashion: one delivery at launch. Stores get stock in different proportions than they
           sell, and about a third are 'misbought' (most stock where it sells least), so
           some stores run out while others sit on stock (moves); the tails left at the
           end of a line's life are thin and slow (tidy-ups)
- dud:     barely sells at all (slow movers)

Lines launched in the last few weeks exercise the 'new in' exceptions. Store names,
brand prefixes and the split colour come from the example config, so the output
loads with it as is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from transfer_engine.export import REQUIRED_COLUMNS
from transfer_engine.settings import ExportMapping
from transfer_engine.weeks import shift_week

COLOURS = ["Black", "Navy", "Stone", "Olive", "Red", "White", "Camel", "Grey"]
LETTER_SIZES = (["XS", "S", "M", "L", "XL", "XXL"], [0.05, 0.15, 0.30, 0.30, 0.15, 0.05])
MEN_WAIST = (["28", "30", "32", "34", "36", "38"], [0.05, 0.20, 0.30, 0.25, 0.15, 0.05])
WOMEN_SIZES = (["34", "36", "38", "40", "42", "44"], [0.10, 0.25, 0.30, 0.20, 0.10, 0.05])
ONE_SIZE = (["ONE SIZE"], [1.0])

# category -> (garment words for style names, price incl. VAT, sizes, season peak week, season strength)
CATEGORIES = {
    "Men": {
        "Shirts": (["OXFORD SHIRT", "LINEN SHIRT"], 50, LETTER_SIZES, 20, 0.2),
        "T-Shirts & Tops": (["CREW TEE", "PIQUE POLO"], 25, LETTER_SIZES, 26, 0.5),
        "Knit": (["MERINO KNIT", "CABLE CARDIGAN"], 60, LETTER_SIZES, 2, 0.6),
        "Outerwear": (["WOOL COAT", "PUFFER JACKET"], 150, LETTER_SIZES, 48, 0.8),
        "Trousers": (["CHINO PANT"], 60, MEN_WAIST, 15, 0.2),
        "Jeans": (["SLIM JEANS"], 70, MEN_WAIST, 40, 0.2),
        "Belts": (["LEATHER BELT"], 30, ONE_SIZE, 50, 0.3),
        "Socks": (["RIB SOCK"], 8, ONE_SIZE, 50, 0.3),
    },
    "Women": {
        "Dresses": (["MIDI DRESS", "WRAP DRESS"], 70, WOMEN_SIZES, 24, 0.5),
        "Knit": (["ROLL NECK KNIT"], 55, LETTER_SIZES, 2, 0.6),
        "T-Shirts & Tops": (["BOXY TEE"], 25, LETTER_SIZES, 26, 0.5),
        "Outerwear": (["TRENCH COAT"], 140, WOMEN_SIZES, 44, 0.6),
        "Skirts": (["PLEAT SKIRT"], 50, WOMEN_SIZES, 22, 0.4),
        "Trousers": (["WIDE PANT"], 60, WOMEN_SIZES, 15, 0.2),
        "Bags": (["TOTE BAG"], 40, ONE_SIZE, 50, 0.3),
        "Scarves": (["WOOL SCARF"], 30, ONE_SIZE, 50, 0.7),
    },
}
STORE_DEMAND = [1.3, 1.0, 0.85, 0.7]  # busiest store first
MISBOUGHT_SHARE = 0.35  # fashion lines whose stock is split against where they sell
SEASON_DROPS = (8, 34)  # weeks the spring and autumn ranges land in store
MARKDOWN_AFTER_WEEKS = 10
MARKDOWN = 0.3
COST_SHARE = 0.45  # cost price as a share of the price excluding VAT
VAT = 1.23


@dataclass
class Line:
    """One style-colour and how it behaves."""

    sty: str
    col: str
    dept: str
    brand: str
    cat: str
    num: int
    price: float
    kind: str  # 'noos', 'fashion' or 'dud'
    launch: int  # week index of the first delivery
    sizes: list[str]
    size_weights: list[float]
    demand: float  # units a week, chain-wide, at the season's average
    peak_week: int
    season_strength: float
    store_demand: dict[str, float] = field(default_factory=dict)  # this line's appeal per store
    store_alloc: dict[str, float] = field(default_factory=dict)  # this line's share of stock per store
    variants: list[str] = field(default_factory=lambda: [""])  # '' = no variant


def season(line: Line, wk: int) -> float:
    """Demand multiplier for the week: 1 + strength at the peak week, 1 - strength half a year away."""
    return 1 + line.season_strength * math.cos(2 * math.pi * (wk - line.peak_week) / 52)


def make_lines(rng: np.random.Generator, mapping: ExportMapping, weeks: list[tuple[int, int]],
               n_noos: int = 24, n_fashion: int = 90, n_duds: int = 12,
               misbought_share: float = MISBOUGHT_SHARE) -> list[Line]:
    """Invent the style-colours: replenished basics, fashion lines landing with each season's drop, duds.

    `misbought_share` of fashion lines get most of their stock where they sell least.
    """
    n_weeks = len(weeks)
    drops = [t for t, (_, wk) in enumerate(weeks) if wk in SEASON_DROPS]
    stores = mapping.store_codes
    prefixes = {dept: prefix for prefix, (dept, _) in mapping.brand_prefixes.items()}
    brands = {dept: brand for _, (dept, brand) in mapping.brand_prefixes.items()}
    lines: list[Line] = []

    def add(kind: str, launch: int, demand: float, dept: str | None = None, cat: str | None = None,
            sty: str | None = None, col: str | None = None, variants: list[str] | None = None) -> None:
        dept = dept or str(rng.choice(sorted(prefixes)))
        cat = cat or str(rng.choice(sorted(CATEGORIES[dept])))
        words, price, (sizes, weights), peak, strength = CATEGORIES[dept][cat]
        n = len(lines) + 1
        lines.append(
            Line(
                sty=sty or f"{prefixes[dept]} {rng.choice(words)} {n:03d}",
                col=col or str(rng.choice(COLOURS)),
                dept=dept, brand=brands[dept], cat=cat, num=1_000_000 + n, price=float(price),
                kind=kind, launch=launch, sizes=sizes, size_weights=weights, demand=demand,
                peak_week=peak, season_strength=strength,
                store_demand={s: STORE_DEMAND[i] * rng.lognormal(0, 0.5) for i, s in enumerate(stores)},
                store_alloc={s: rng.lognormal(0, 0.6) for s in stores},
                variants=variants or [""],
            )
        )

    for _ in range(n_noos):
        add("noos", launch=0, demand=rng.uniform(2, 6))
    for i in range(n_fashion):
        # most land within a few weeks of a season drop; the last few in the final weeks ('new in')
        if i < n_fashion - 4:
            launch = int(np.clip(drops[i % len(drops)] + rng.integers(-2, 4), 0, n_weeks - 1))
        else:
            launch = n_weeks - 1 - (i % 4)
        add("fashion", launch=launch, demand=rng.uniform(3, 9))
        if rng.random() < misbought_share:
            line = lines[-1]
            line.store_alloc = {s: 1 / d for s, d in line.store_demand.items()}
    for i in range(n_duds):
        add("dud", launch=int(rng.integers(0, max(1, n_weeks - 20))), demand=rng.uniform(0.02, 0.15))

    # The split colour from the config, sold in two variants.
    for sty, col in mapping.split_colours:
        dept = next(d for d, p in prefixes.items() if sty.upper().startswith(p.upper()))
        cat = "Dresses" if "Dresses" in CATEGORIES[dept] else sorted(CATEGORIES[dept])[0]
        add("fashion", launch=max(0, n_weeks - 9), demand=8.0, dept=dept, cat=cat, sty=sty, col=col,
            variants=["Twist", "Plain"])
    return lines


def simulate(lines: list[Line], stores: list[str], start: tuple[int, int], n_weeks: int,
             rng: np.random.Generator) -> pd.DataFrame:
    """Run every line week by week. One row per week, store, line, variant and size with any activity.

    Columns: yr, wk, store, sty, col, variant, size, q, v, md, st, gp, rem, norm.
    """
    weeks = [shift_week(*start, t) for t in range(n_weeks)]
    rows: list[dict[str, object]] = []
    for line in lines:
        for store in stores:
            for variant in line.variants:
                for size, weight in zip(line.sizes, line.size_weights):
                    rows += _simulate_cell(line, store, variant, size, weight, len(line.variants), weeks, rng)
    return pd.DataFrame(rows)


def _simulate_cell(line: Line, store: str, variant: str, size: str, weight: float, n_variants: int,
                   weeks: list[tuple[int, int]], rng: np.random.Generator) -> list[dict[str, object]]:
    """One store-size of one line, week by week."""
    norm = max(1, round(line.demand * weight * line.store_demand[store] * 1.5)) if line.kind == "noos" else 0
    depth = 2.0 if line.kind == "dud" else 9.0  # units a store gets at launch (all sizes), before the store split
    stock, on_way, sold_ever, rows = 0, 0, 0, []

    for t, (yr, wk) in enumerate(weeks):
        if t < line.launch:
            continue
        stock += on_way
        on_way = 0
        if t == line.launch:
            stock += norm if line.kind == "noos" else int(rng.poisson(depth * weight * line.store_alloc[store] / n_variants))

        rate = line.demand * weight * line.store_demand[store] / len(STORE_DEMAND) * season(line, wk) / n_variants
        sold = min(stock, int(rng.poisson(rate)))
        returned = 1 if sold_ever > 0 and rng.random() < 0.02 else 0
        stock += returned - sold
        sold_ever += sold
        if line.kind == "noos" and stock < norm:
            on_way = norm - stock  # replenishment ordered, arrives next week

        q = sold - returned
        if q == 0 and stock == 0 and on_way == 0 and norm == 0:
            continue
        md = MARKDOWN if line.kind != "noos" and t - line.launch >= MARKDOWN_AFTER_WEEKS else 0.0
        v = q * line.price * (1 - md)
        rows.append(
            {
                "yr": yr, "wk": wk, "store": store, "sty": line.sty, "col": line.col, "variant": variant,
                "size": size, "q": q, "v": round(v, 2), "md": md if q else None, "st": stock,
                "gp": round(v / VAT - q * line.price / VAT * COST_SHARE, 2) if q else None,
                "rem": on_way or None, "norm": norm or None,
            }
        )
    return rows


def line_details(lines: list[Line]) -> pd.DataFrame:
    """sty, col, dept, brand, cat, num for joining onto simulated rows."""
    return pd.DataFrame([{"sty": l.sty, "col": l.col, "dept": l.dept, "brand": l.brand, "cat": l.cat, "num": l.num} for l in lines])


def to_seed(sim: pd.DataFrame, lines: list[Line], mapping: ExportMapping) -> tuple[pd.DataFrame, pd.DataFrame]:
    """History and norms in the seed files' format. Split colours become 'colour · variant'."""
    split = {(s, c) for s, c in mapping.split_colours}
    rows = sim.merge(line_details(lines), on=["sty", "col"])
    rows["col"] = [f"{c} · {v}" if (s, c) in split and v else c for s, c, v in zip(rows["sty"], rows["col"], rows["variant"])]
    rows["num"] = rows["num"].astype(float)  # the real seed file stores it as '1001.0'

    latest = rows[(rows["yr"] * 100 + rows["wk"]) == (rows["yr"] * 100 + rows["wk"]).max()]
    norms = latest.loc[latest["norm"].notna(), ["store", "sty", "col", "size", "norm"]]

    # Like the real history: no rows without sales, stock or anything on the way.
    empty = (rows["q"] == 0) & (rows["st"] == 0) & rows["rem"].isna()
    history = rows.loc[~empty, ["yr", "wk", "store", "dept", "brand", "cat", "num", "sty", "col", "size",
                                "q", "v", "md", "st", "gp", "rem"]]
    return history.reset_index(drop=True), norms.reset_index(drop=True)


def to_export(week_rows: pd.DataFrame, lines: list[Line], mapping: ExportMapping) -> pd.DataFrame:
    """One week in the Power BI export's format, including the messy bits the parser must handle:
    blanks instead of zeros, a placeholder row, another brand, an excluded store and footer rows.
    """
    names = {code: f"SY - {text} Centre" for text, code in mapping.store_match.items()}
    rows = week_rows.merge(line_details(lines), on=["sty", "col"])
    blank = lambda s: s.where(s != 0)  # Power BI leaves zeros blank
    export = pd.DataFrame(
        {
            "Week Name Year": [f"Week {w}, {y}" for y, w in zip(rows["yr"], rows["wk"])],
            "Store Name": rows["store"].map(names),
            "Style Name": rows["sty"],
            "Style Colour Name": rows["col"],
            "Style Variant Name": ["Detail:" + v if v else None for v in rows["variant"]],
            "Style Number Code": rows["num"].astype(float),
            "Style Size Name": rows["size"],
            "Sales Qty": blank(rows["q"].astype(float)),
            "Sales iV": blank(rows["v"]),
            "Mark Down iV %": rows["md"],
            "Stock EOH QTY": blank(rows["st"].astype(float)),
            "Gross Profit": rows["gp"],
            "Remaining Qty": rows["rem"],
            "Style Master Category Name": rows["cat"],
            "Norm Ultimo": rows["norm"],
        }
    )
    week_label = export["Week Name Year"].iat[0]
    first_store = export["Store Name"].iat[0]
    extras = pd.DataFrame(
        [
            {"Week Name Year": week_label, "Store Name": first_store, "Style Name": "?", "Style Colour Name": "?",
             "Style Number Code": -1.0, "Style Size Name": "?", "Stock EOH QTY": -3.0},
            {"Week Name Year": week_label, "Store Name": first_store, "Style Name": "ZZZ OTHER BRAND TEE",
             "Style Colour Name": "Black", "Style Size Name": "M", "Sales Qty": 1.0, "Sales iV": 20.0},
            *[
                {"Week Name Year": week_label, "Store Name": f"SY - {text} Village", "Style Name": rows["sty"].iat[0],
                 "Style Colour Name": rows["col"].iat[0], "Style Size Name": rows["size"].iat[0], "Stock EOH QTY": 5.0}
                for text in mapping.exclude_stores
            ],
            {"Week Name Year": "Total", "Sales Qty": export["Sales Qty"].sum(), "Sales iV": export["Sales iV"].sum()},
            {},
            {"Week Name Year": "Applied filters:\nWeek is " + week_label},
        ]
    )
    return pd.concat([export, extras], ignore_index=True)[list(REQUIRED_COLUMNS)]


def generate(mapping: ExportMapping, last_week: tuple[int, int], n_weeks: int = 95, seed: int = 7,
             misbought_share: float = MISBOUGHT_SHARE) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Simulate `n_weeks` up to and including the week after `last_week`.

    Returns (history, norms) up to `last_week` for seeding, and the following week as a
    Power BI export to try the upload with.
    """
    rng = np.random.default_rng(seed)
    start = shift_week(*last_week, -(n_weeks - 1))
    lines = make_lines(rng, mapping, [shift_week(*start, t) for t in range(n_weeks + 1)], misbought_share=misbought_share)
    sim = simulate(lines, mapping.store_codes, start, n_weeks + 1, rng)

    export_week = shift_week(*last_week, 1)
    key = sim["yr"] * 100 + sim["wk"]
    seed_rows, export_rows = sim[key <= last_week[0] * 100 + last_week[1]], sim[key == export_week[0] * 100 + export_week[1]]
    history, norms = to_seed(seed_rows, lines, mapping)
    return history, norms, to_export(export_rows, lines, mapping)
