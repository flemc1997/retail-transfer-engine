"""Tests for the transfer rules, on small hand-made tables.

Default thresholds (config.example.json):
  moves:    receiver rate >= 0.5/wk and cover < 4 wks; donor >= 3 units, cover >= 12 wks and
            first stocked >= 4 wks ago;
            receiver cover after the move <= 12 wks.
  tidy-ups: >= 2 stores holding, none above 5 units, first stocked >= 6 wks ago,
            accessories only if the chain holds <= 12, chain cover >= 12 wks (or no sales).
"""

import math

import pandas as pd
import pytest

from transfer_engine.rules import (
    cover_weeks,
    run_id,
    run_sizes,
    suggest_moves,
    suggest_tidy_ups,
)
from transfer_engine.settings import DEFAULT_CONFIG_PATH, load_config

_, MOVES, TIDY, _ = load_config(DEFAULT_CONFIG_PATH)


def line(*stores: dict[str, object], sty: str = "STYLE", col: str = "Red", **shared: object) -> pd.DataFrame:
    """One style-colour across stores. Each store dict overrides the defaults below."""
    defaults = {
        "on_way": 0, "move_rate": 0.0, "tidy_rate": 0.0, "n_sizes": 1,
        "normed": False, "weeks_since_first_stocked": 20, "cat": "Shirts",
    }
    return pd.DataFrame([{"sty": sty, "col": col, **defaults, **shared, **s} for s in stores])


def pairs(suggestions: pd.DataFrame) -> list[tuple[str, str, int]]:
    return list(zip(suggestions["sender"], suggestions["receiver"], suggestions["qty"]))


# ---------------------------------------------------------------- moves

def test_slow_store_sends_to_fast_store() -> None:
    lines = line(
        {"store": "A", "stock": 1, "move_rate": 1.0},  # 1 wk cover: a receiver
        {"store": "B", "stock": 6, "move_rate": 0.0},  # no sales: a donor
    )
    assert pairs(suggest_moves(lines, MOVES)) == [("B", "A", 6)]


def test_stock_on_the_way_counts_for_the_receiver() -> None:
    lines = line(
        {"store": "A", "stock": 1, "on_way": 5, "move_rate": 1.0},  # 6 wks cover with delivery
        {"store": "B", "stock": 6, "move_rate": 0.0},
    )
    assert suggest_moves(lines, MOVES).empty


def test_donor_needs_enough_units_and_cover() -> None:
    lines = line(
        {"store": "A", "stock": 0, "move_rate": 1.0},
        {"store": "B", "stock": 2, "move_rate": 0.0},  # below 3 units
        {"store": "C", "stock": 6, "move_rate": 1.0},  # 6 wks cover, below 12
    )
    assert suggest_moves(lines, MOVES).empty


def test_new_line_cannot_donate_yet() -> None:
    def lines(weeks: int) -> pd.DataFrame:
        return line(
            {"store": "A", "stock": 1, "move_rate": 1.0},
            {"store": "B", "stock": 6, "move_rate": 0.0},  # no sales yet
            weeks_since_first_stocked=weeks,
        )

    assert suggest_moves(lines(2), MOVES).empty  # arrived 2 weeks ago: too soon to judge
    assert pairs(suggest_moves(lines(4), MOVES)) == [("B", "A", 6)]  # had 4 weeks to sell


def test_receiver_is_not_overstocked() -> None:
    lines = line(
        {"store": "A", "stock": 0, "move_rate": 0.5},  # 20 units would be 40 wks cover
        {"store": "B", "stock": 20, "move_rate": 0.0},
    )
    assert suggest_moves(lines, MOVES).empty


def test_fastest_receiver_first_then_next_donor_sees_updated_stock() -> None:
    lines = line(
        {"store": "A", "stock": 0, "move_rate": 1.0},
        {"store": "B", "stock": 0, "move_rate": 2.0},  # fastest: gets the biggest donor
        {"store": "C", "stock": 10, "move_rate": 0.0},
        {"store": "D", "stock": 8, "move_rate": 0.0},
    )
    # C (10) -> B: 5 wks after. D (8) -> B would be 9 wks, still within 12, so B again.
    assert pairs(suggest_moves(lines, MOVES)) == [("C", "B", 10), ("D", "B", 8)]


def test_second_donor_goes_elsewhere_when_first_receiver_is_full() -> None:
    lines = line(
        {"store": "A", "stock": 0, "move_rate": 1.0},
        {"store": "B", "stock": 0, "move_rate": 1.5},
        {"store": "C", "stock": 12, "move_rate": 0.0},
        {"store": "D", "stock": 10, "move_rate": 0.0},
    )
    # C (12) -> B: 8 wks. D (10) -> B would be 14.7 wks, too many, so A: 10 wks.
    assert pairs(suggest_moves(lines, MOVES)) == [("C", "B", 12), ("D", "A", 10)]


def test_normed_line_is_never_moved() -> None:
    lines = line(
        {"store": "A", "stock": 1, "move_rate": 1.0},
        {"store": "B", "stock": 6, "move_rate": 0.0, "normed": True},
    )
    assert suggest_moves(lines, MOVES).empty


# ---------------------------------------------------------------- tidy-ups

def test_scattered_slow_line_goes_to_best_seller() -> None:
    lines = line(
        {"store": "A", "stock": 2, "tidy_rate": 0.0},
        {"store": "B", "stock": 3, "tidy_rate": 0.25},  # best seller receives
        {"store": "C", "stock": 1, "tidy_rate": 0.0},
    )
    # 6 units at 0.25/wk chain-wide = 24 wks cover: slow enough.
    assert pairs(suggest_tidy_ups(lines, TIDY)) == [("A", "B", 2), ("C", "B", 1)]


def test_tidy_ties_go_to_most_units_then_most_sizes() -> None:
    lines = line(
        {"store": "A", "stock": 2, "n_sizes": 2},
        {"store": "B", "stock": 3, "n_sizes": 1},  # nobody sold it: most units wins
    )
    assert pairs(suggest_tidy_ups(lines, TIDY)) == [("A", "B", 2)]


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"weeks_since_first_stocked": 4}, "too new"),
        ({"normed": True}, "has a norm"),
    ],
)
def test_tidy_skips(change: dict[str, object], why: str) -> None:
    lines = line({"store": "A", "stock": 2}, {"store": "B", "stock": 3}, **change)
    assert suggest_tidy_ups(lines, TIDY).empty, why


def test_tidy_needs_thin_spread_and_slow_chain() -> None:
    one_store = line({"store": "A", "stock": 3}, {"store": "B", "stock": 0})
    one_store_too_full = line({"store": "A", "stock": 6}, {"store": "B", "stock": 1})
    selling = line({"store": "A", "stock": 2, "tidy_rate": 0.5}, {"store": "B", "stock": 2})  # 8 wks cover
    for lines in (one_store, one_store_too_full, selling):
        assert suggest_tidy_ups(lines, TIDY).empty


def test_accessories_only_tidied_when_chain_total_is_small() -> None:
    small = line(*[{"store": s, "stock": 4} for s in "ABC"], cat="Belts")  # 12 units
    big = line(*[{"store": s, "stock": 5} for s in "ABC"], cat="Belts")  # 15 units
    assert len(suggest_tidy_ups(small, TIDY)) == 2
    assert suggest_tidy_ups(big, TIDY).empty


def test_lines_in_a_move_are_not_tidied() -> None:
    lines = line({"store": "A", "stock": 2}, {"store": "B", "stock": 3})
    assert suggest_tidy_ups(lines, TIDY, skip={("STYLE", "Red")}).empty


# ---------------------------------------------------------------- helpers

def test_cover_weeks() -> None:
    assert cover_weeks(6, 2) == 3
    assert math.isinf(cover_weeks(6, 0))


def test_run_id_is_stable_and_week_specific() -> None:
    first = run_id(2026, 42, "STYLE", "Red", "A", "B")
    assert first == run_id(2026, 42, "STYLE", "Red", "A", "B")
    assert first.startswith("2026-W42_")
    assert first != run_id(2026, 43, "STYLE", "Red", "A", "B")
    assert first != run_id(2026, 42, "STYLE", "Red", "B", "A")


def test_run_sizes_lists_every_size_the_sender_holds() -> None:
    suggestions = pd.DataFrame([{"sty": "STYLE", "col": "Red", "sender": "B", "receiver": "A", "qty": 3}])
    sizes = pd.DataFrame(
        [
            {"sty": "STYLE", "col": "Red", "store": "B", "size": "S", "stock": 1},
            {"sty": "STYLE", "col": "Red", "store": "B", "size": "M", "stock": 2},
            {"sty": "STYLE", "col": "Red", "store": "B", "size": "L", "stock": 0},  # nothing to send
            {"sty": "STYLE", "col": "Red", "store": "A", "size": "S", "stock": 5},  # receiver's own
        ]
    )
    rows = run_sizes(suggestions, sizes)
    assert sorted(zip(rows["size"], rows["size_qty"])) == [("M", 2), ("S", 1)]
