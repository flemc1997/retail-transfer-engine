"""The Excel picking list the stores work from: one row per run, grouped by sending store."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# (header, column in `picking_rows`, width). 'Sent ✓' is left blank for the store to tick.
COLUMNS = [
    ("Send from", "sender", 11),
    ("Send to", "receiver", 11),
    ("Style", "sty", 40),
    ("Style No.", "num", 11),
    ("Colour", "col", 18),
    ("Sizes", "sizes_text", 26),
    ("Units", "units", 7),
    ("Full / Partial", "full_partial", 11),
    ("Notes", "note", 34),
    ("Sent ✓", None, 8),
]
HEADER_ROW = 3


def picking_list_xlsx(rows: pd.DataFrame, yr: int, wk: int) -> bytes:
    """Build the picking list workbook and return it as bytes, ready to download."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Picking list"
    ws["A1"] = (
        f"Approved transfers – week {wk}, {yr}. Send exactly the sizes and units listed. "
        "Partial = the sender keeps some of that style-colour. Tick Sent when boxed."
    )
    ws["A1"].font = Font(bold=True, size=12)

    for j, (header, _, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(HEADER_ROW, j, header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="22344F")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = width

    for i, row in enumerate(rows.itertuples(index=False), start=HEADER_ROW + 1):
        values = row._asdict()
        for j, (_, key, _) in enumerate(COLUMNS, start=1):
            value = values.get(key) if key else None
            ws.cell(i, j, None if pd.isna(value) else value)

    last_row = HEADER_ROW + max(len(rows), 1)
    table = Table(displayName="PickingList", ref=f"A{HEADER_ROW}:{get_column_letter(len(COLUMNS))}{last_row}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(table)
    ws.freeze_panes = f"A{HEADER_ROW + 1}"

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
