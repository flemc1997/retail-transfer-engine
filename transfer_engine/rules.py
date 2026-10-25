"""Transfer rules as pure functions: a table of stock and sales rates in, suggestions out.

Input (`lines`): one row per style, colour and store, with
    stock       booked-in units in the store (never negative)
    on_way      units invoiced or in transit to the store, not yet booked in
    move_rate   units sold per week over the moves window (e.g. 4 weeks)
    tidy_rate   units sold per week over the tidy-up window (e.g. 8 weeks)
    n_sizes     how many sizes the store holds
    normed      True if the style-colour has a norm in any store
    weeks_since_first_stocked   weeks since the chain first held this style-colour
    cat         category

Output: one row per suggested run (a whole style-colour from one store to another):
kind, sty, col, sender, receiver, qty, reason. Sizes are attached later (`run_sizes`).

Every suggestion moves the sender's whole run, never part of it.
"""

from __future__ import annotations

import hashlib
import math

import pandas as pd

from transfer_engine.settings import MoveRules, TidyUpRules

SUGGESTION_COLUMNS = ["kind", "sty", "col", "sender", "receiver", "qty", "reason"]


def cover_weeks(stock: float, rate: float) -> float:
    """Weeks the stock would last at this sales rate. No sales means it never runs out."""
    return math.inf if rate <= 0 else stock / rate


def is_receiver(stock_incl_on_way: float, rate: float, rules: MoveRules) -> bool:
    """A store that sells the line well and will run out soon, counting stock on its way."""
    return rate >= rules.receiver_min_rate and cover_weeks(stock_incl_on_way, rate) < rules.receiver_max_cover_weeks


def is_donor(stock: float, rate: float, weeks_since_first_stocked: int, rules: MoveRules) -> bool:
    """A store with enough of the line to send, and far more cover than it needs.

    New lines can't donate yet: with no sales so far their cover looks unlimited,
    but they simply haven't had time to sell.
    """
    return (
        stock >= rules.donor_min_units
        and cover_weeks(stock, rate) >= rules.donor_min_cover_weeks
        and weeks_since_first_stocked >= rules.donor_min_weeks_since_first_stocked
    )


def suggest_moves(lines: pd.DataFrame, rules: MoveRules) -> pd.DataFrame:
    """Moves: send a whole run from a store where it sits to one where it sells.

    Style-colours with a norm in any store are skipped: replenishment looks after them.
    """
    moves: list[dict[str, object]] = []
    for (sty, col), line in lines.groupby(["sty", "col"], sort=True):
        if line["normed"].any():
            continue
        moves += _moves_for_line(str(sty), str(col), line, rules)
    return pd.DataFrame(moves, columns=SUGGESTION_COLUMNS)


def _moves_for_line(sty: str, col: str, line: pd.DataFrame, rules: MoveRules) -> list[dict[str, object]]:
    """Match donors to receivers for one style-colour.

    Receivers are chosen once, up front. Donors go most-stock first; each goes to the
    fastest-selling receiver whose cover after the move stays within the limit. That
    receiver's stock is then increased, so the next donor sees it as already topped up.
    """
    receivers = {
        r.store: {"rate": r.move_rate, "stock": r.stock + r.on_way}
        for r in line.itertuples()
        if is_receiver(r.stock + r.on_way, r.move_rate, rules)
    }
    if not receivers:
        return []

    moves = []
    for donor in line.sort_values(["stock", "store"], ascending=[False, True]).itertuples():
        if donor.store in receivers or not is_donor(
            donor.stock, donor.move_rate, donor.weeks_since_first_stocked, rules
        ):
            continue
        target = _first_receiver_with_room(receivers, donor.stock, rules)
        if target is None:
            continue
        receiver = receivers[target]
        after = cover_weeks(receiver["stock"] + donor.stock, receiver["rate"])
        moves.append(
            {
                "kind": "move",
                "sty": sty,
                "col": col,
                "sender": donor.store,
                "receiver": target,
                "qty": int(donor.stock),
                "reason": (
                    f"{target} sells {receiver['rate']:.1f}/wk with {cover_weeks(receiver['stock'], receiver['rate']):.1f} wks cover "
                    f"({after:.1f} after); {donor.store} has {_weeks_text(cover_weeks(donor.stock, donor.move_rate))} cover."
                ),
            }
        )
        receiver["stock"] += donor.stock
    return moves


def _first_receiver_with_room(receivers: dict[str, dict[str, float]], units: float, rules: MoveRules) -> str | None:
    """The fastest-selling receiver that would stay within the cover limit after taking `units`."""
    for store, r in sorted(receivers.items(), key=lambda kv: (-kv[1]["rate"], kv[0])):
        if cover_weeks(r["stock"] + units, r["rate"]) <= rules.receiver_max_cover_after_weeks:
            return store
    return None


def suggest_tidy_ups(
    lines: pd.DataFrame, rules: TidyUpRules, skip: set[tuple[str, str]] = frozenset()
) -> pd.DataFrame:
    """Tidy-ups: pull a thin, slow line scattered over several stores into one store.

    `skip` holds style-colours already in a move, so a line is never in both.
    """
    tidy_ups: list[dict[str, object]] = []
    for (sty, col), line in lines.groupby(["sty", "col"], sort=True):
        if line["normed"].any() or (sty, col) in skip or not is_tidy_candidate(line, rules):
            continue
        tidy_ups += _tidy_up_line(str(sty), str(col), line)
    return pd.DataFrame(tidy_ups, columns=SUGGESTION_COLUMNS)


def is_tidy_candidate(line: pd.DataFrame, rules: TidyUpRules) -> bool:
    """A line worth tidying: thinly spread, not new, slow across the whole chain.

    - held in at least `min_stores_holding` stores, none with more than `max_units_per_store`;
    - first stocked at least `min_weeks_since_first_stocked` weeks ago (new lines get time to sell);
    - accessories only when the chain holds very few in total;
    - slow chain-wide: at least `min_chain_cover_weeks` of cover at the tidy-up rate, or no sales.
    """
    held = line[line["stock"] > 0]
    if len(held) < rules.min_stores_holding or held["stock"].max() > rules.max_units_per_store:
        return False
    if line["weeks_since_first_stocked"].iat[0] < rules.min_weeks_since_first_stocked:
        return False
    total = held["stock"].sum()
    if line["cat"].iat[0] in rules.accessory_categories and total > rules.accessories_max_chain_units:
        return False
    return cover_weeks(total, line["tidy_rate"].sum()) >= rules.min_chain_cover_weeks


def _tidy_up_line(sty: str, col: str, line: pd.DataFrame) -> list[dict[str, object]]:
    """Every holder sends its whole run to the best store for the line.

    Best = most sold over the tidy-up window; ties go to the store holding the most units,
    then the most sizes, then the store code, so the result is always the same.
    """
    held = line[line["stock"] > 0].sort_values(
        ["tidy_rate", "stock", "n_sizes", "store"], ascending=[False, False, False, True]
    )
    receiver = held.iloc[0]
    total = int(held["stock"].sum())
    why_receiver = (
        f"{receiver['store']} sells it best ({receiver['tidy_rate']:.1f}/wk)"
        if receiver["tidy_rate"] > 0
        else f"no store sold it, {receiver['store']} holds the most ({int(receiver['stock'])} units)"
    )
    return [
        {
            "kind": "tidy",
            "sty": sty,
            "col": col,
            "sender": sender.store,
            "receiver": receiver["store"],
            "qty": int(sender.stock),
            "reason": f"{total} units spread over {len(held)} stores and slow chain-wide; {why_receiver}.",
        }
        for sender in held.iloc[1:].itertuples()
    ]


def run_id(yr: int, wk: int, sty: str, col: str, sender: str, receiver: str) -> str:
    """A stable id for one run: the same week, line and stores always give the same id.

    So if a week's suggestions are regenerated, decisions already made still match.
    """
    digest = hashlib.sha1(f"{sty}|{col}|{sender}|{receiver}".encode()).hexdigest()[:10]
    return f"{yr}-W{wk:02d}_{digest}"


def run_sizes(suggestions: pd.DataFrame, sizes: pd.DataFrame) -> pd.DataFrame:
    """One row per size for each suggestion: every size the sender holds (whole runs only).

    `sizes` has sty, col, store, size, stock for the sending week.
    """
    held = sizes[sizes["stock"] > 0].rename(columns={"store": "sender", "stock": "size_qty"})
    return suggestions.merge(held, on=["sty", "col", "sender"], how="left")


def _weeks_text(weeks: float) -> str:
    return "no sales, so unlimited" if math.isinf(weeks) else f"{weeks:.0f} wks"
