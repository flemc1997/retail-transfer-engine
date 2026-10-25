"""Decisions on suggested runs (approve / partial / reject) and the picking list they produce.

A decision belongs to a run id. A run with no decision is pending. A partial approval
records the sizes and units actually being sent; the sender keeps the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import duckdb
import pandas as pd

Status = Literal["approved", "partial", "rejected"]
LETTER_SIZES = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]


@dataclass(frozen=True)
class Decision:
    """One decision. `sizes` is only for partial approvals: {size: units to send}."""

    run_id: str
    status: Status
    note: str = ""
    sizes: dict[str, int] | None = None


def size_sort_key(size: str) -> tuple[int, int, str]:
    """Letter sizes in wearing order (XS before S before M), then numbers (30, 32), then the rest."""
    if size in LETTER_SIZES:
        return (0, LETTER_SIZES.index(size), "")
    number = re.match(r"^(\d+)", size)
    return (1, int(number.group(1)), size) if number else (2, 0, size)


def sizes_text(sizes: dict[str, int]) -> str:
    """{'M': 2, 'S': 1} -> 'S:1 M:2', the format the stores read on the picking list."""
    return " ".join(f"{size}:{qty}" for size, qty in sorted(sizes.items(), key=lambda kv: size_sort_key(kv[0])) if qty)


def batch_runs(con: duckdb.DuckDBPyConnection, yr: int, wk: int) -> pd.DataFrame:
    """One row per suggested run for the week, with its sizes and current decision.

    Columns: run_id, kind, sender, receiver, sty, col, qty, sizes ({size: qty}), reason,
    status (None = pending), note, sent_sizes ({size: qty} for partial approvals).
    """
    runs = con.execute(
        """
        SELECT b.run_id, any_value(b.kind) AS kind, any_value(b.sender) AS sender,
               any_value(b.receiver) AS receiver, any_value(b.sty) AS sty, any_value(b.col) AS col,
               sum(b.qty)::INTEGER AS qty, map(list(b.size), list(b.qty)) AS sizes,
               any_value(b.reason) AS reason,
               any_value(d.status) AS status, any_value(d.note) AS note, any_value(d.sizes) AS sent
        FROM batches b LEFT JOIN decisions d USING (run_id)
        WHERE b.yr = ? AND b.wk = ?
        GROUP BY b.run_id
        ORDER BY kind, sender, receiver, sty, col
        """,
        [yr, wk],
    ).df()
    # pandas marks missing values as NA; turn them into plain None / "" so callers can
    # write `status is None` and `status == "partial"` without NA surprises.
    runs["status"] = pd.Series([None if pd.isna(s) else s for s in runs["status"]], index=runs.index, dtype=object)
    runs["note"] = ["" if pd.isna(n) else n for n in runs["note"]]
    runs["sizes"] = runs["sizes"].map(dict)
    runs["sent_sizes"] = [
        None if sent is None or sent is pd.NA else {x["size"]: x["qty"] for x in sent} for sent in runs["sent"]
    ]
    return runs.drop(columns="sent")


def pending_run_ids(runs: pd.DataFrame) -> list[str]:
    """Run ids in `runs` (from `batch_runs`, possibly filtered) that have no decision yet."""
    return [r.run_id for r in runs.itertuples() if r.status is None]


def partial_decision(run_id: str, held: dict[str, int], chosen: dict[str, int], note: str = "") -> Decision:
    """Turn the units chosen per size into a decision, checking they make sense.

    Choosing every unit is a full approval; choosing none must be a rejection instead.
    You can't send more of a size than the sender holds.
    """
    too_many = [size for size, qty in chosen.items() if qty > held.get(size, 0)]
    if too_many:
        raise ValueError(f"More than the sender holds for: {', '.join(too_many)}")
    sending = {size: qty for size, qty in chosen.items() if qty > 0}
    if not sending:
        raise ValueError("Nothing chosen to send: reject the run instead.")
    if sending == {size: qty for size, qty in held.items() if qty > 0}:
        return Decision(run_id, "approved", note)
    return Decision(run_id, "partial", note, sending)


def save_decisions(con: duckdb.DuckDBPyConnection, decisions: list[Decision], clear: list[str] = ()) -> None:
    """Record decisions (replacing any earlier one for the same run) and clear others back to pending.

    All in one transaction, so a save either fully happens or not at all.
    """
    con.execute("BEGIN")
    try:
        for d in decisions:
            sizes = [{"size": s, "qty": q} for s, q in d.sizes.items()] if d.sizes else None
            con.execute(
                """
                INSERT OR REPLACE INTO decisions (run_id, status, sizes, note, decided_at)
                VALUES (?, ?, ?, ?, current_timestamp)
                """,
                [d.run_id, d.status, sizes, d.note or None],
            )
        if clear:
            con.execute("DELETE FROM decisions WHERE list_contains(?, run_id)", [list(clear)])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def picking_rows(con: duckdb.DuckDBPyConnection, yr: int, wk: int) -> pd.DataFrame:
    """What to send: approved runs in full, partial runs with the chosen sizes only.

    One row per run, sorted by sending store, then receiving store, style and colour.
    """
    runs = batch_runs(con, yr, wk)
    runs = runs[runs["status"].isin(["approved", "partial"])].copy()
    sent = [r.sent_sizes if r.status == "partial" else r.sizes for r in runs.itertuples()]
    runs["sizes_text"] = [sizes_text(s) for s in sent]
    runs["units"] = [sum(s.values()) for s in sent]
    runs["full_partial"] = runs["status"].map({"approved": "Full", "partial": "Partial"})

    numbers = con.execute(
        "SELECT sty, any_value(num) AS num FROM history WHERE num IS NOT NULL GROUP BY sty"
    ).df()
    runs = runs.merge(numbers, on="sty", how="left")
    columns = ["sender", "receiver", "sty", "num", "col", "sizes_text", "units", "full_partial", "note"]
    return runs.sort_values(["sender", "receiver", "sty", "col"])[columns].reset_index(drop=True)
