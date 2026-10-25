"""Weekly upload page: pick the export, see the checks and preview, confirm to save."""

import streamlit as st

from transfer_engine.checks import build_preview, run_checks
from transfer_engine.export import ExportError, parse_export, read_export
from transfer_engine.settings import load_settings
from transfer_engine.storage import connect
from transfer_engine.upload import save_upload

ICONS = {"pass": ":material/check_circle:", "fail": ":material/cancel:", "skip": ":material/remove_circle:"}

settings = load_settings()
st.title("Weekly upload")

if "upload_message" in st.session_state:
    st.success(st.session_state.pop("upload_message"))

exports = sorted(settings.data_dir.glob("*.xlsx"))
if not exports:
    st.info(f"Save the weekly Power BI export (.xlsx) in {settings.data_dir}, then reload this page.")
    st.stop()

path = st.selectbox("Export file", exports, format_func=lambda p: p.name)

try:
    parsed = parse_export(read_export(path), settings.export)
except ExportError as exc:
    st.error(str(exc))
    st.stop()

with connect(settings.db_path, read_only=True) as con:
    checks = run_checks(parsed, con, settings.upload, settings.export.store_codes)
    preview = build_preview(parsed, con) if checks[0].status == "pass" else None

st.subheader("Checks")
for check in checks:
    st.markdown(f"{ICONS[check.status]} **{check.name}**: {check.detail}")

if preview:
    st.subheader(f"Week {preview.wk}, {preview.yr}")
    cols = st.columns(3)
    cols[0].metric("Sales", f"€{preview.sales:,.0f}")
    if preview.sales_prev_week:
        cols[1].metric("vs last week", f"€{preview.sales_prev_week:,.0f}",
                       f"{preview.sales / preview.sales_prev_week - 1:+.0%}")
    if preview.sales_ly:
        cols[2].metric("vs same week last year", f"€{preview.sales_ly:,.0f}",
                       f"{preview.sales / preview.sales_ly - 1:+.0%}")
    st.dataframe(
        preview.by_store,
        hide_index=True,
        column_config={
            "sales": st.column_config.NumberColumn("Sales", format="€%.0f"),
            "sales_prev_week": st.column_config.NumberColumn("Sales last week", format="€%.0f"),
            "vs_prev_week": st.column_config.NumberColumn("vs last week", format="percent"),
            "sales_ly": st.column_config.NumberColumn("Sales LY", format="€%.0f"),
            "vs_ly": st.column_config.NumberColumn("vs LY", format="percent"),
        },
    )
    if preview.rows_replaced:
        st.warning(f"This week is already in history: its {preview.rows_replaced:,} rows will be replaced.")
    if parsed.skipped:
        st.caption("Left out: " + ", ".join(f"{n:,} {why}" for why, n in parsed.skipped.items()))

blocking = [c for c in checks if c.status == "fail" and not c.overridable]
overridable = [c for c in checks if c.status == "fail" and c.overridable]
if blocking:
    st.error("Fix the export and try again. Nothing has been saved.")
    st.stop()

accepted = True
if overridable:
    accepted = st.checkbox("I've checked the low sales figures and want to save anyway")

if st.button("Confirm and save", type="primary", disabled=not accepted):
    result = save_upload(settings, parsed, path)
    st.session_state["upload_message"] = (
        f"Saved week {preview.wk}, {preview.yr}: {result.rows_inserted:,} rows "
        f"({result.rows_deleted:,} replaced), {result.categories_changed:,} category updates, "
        f"norms {'refreshed' if result.norms_refreshed else 'kept (not the newest week)'}. "
        f"Backup: {result.backup_path.name}. {path.name} deleted."
    )
    st.rerun()
