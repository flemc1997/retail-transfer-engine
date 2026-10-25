"""Trading page: how the week went, which lines are stuck, and whether last week's transfers happened."""

import streamlit as st

from transfer_engine.settings import load_settings
from transfer_engine.storage import connect
from transfer_engine.trading import sales_summary, slow_movers, transfer_followup, with_total
from transfer_engine.weeks import previous_week

SALES_COLUMNS = {
    "sales": st.column_config.NumberColumn("Sales", format="€%.0f"),
    "units": st.column_config.NumberColumn("Units", format="%d"),
    "stock": st.column_config.NumberColumn("Stock", format="%d"),
    "sales_lw": st.column_config.NumberColumn("Last week", format="€%.0f"),
    "vs_lw": st.column_config.NumberColumn("vs LW", format="percent"),
    "sales_ly": st.column_config.NumberColumn("Last year", format="€%.0f"),
    "vs_ly": st.column_config.NumberColumn("vs LY", format="percent"),
}

settings = load_settings()
st.title("Trading")

with connect(settings.db_path, read_only=True) as con:
    weeks = con.execute("SELECT DISTINCT yr, wk FROM history ORDER BY yr DESC, wk DESC").fetchall()
    yr, wk = st.selectbox("Week", weeks, format_func=lambda w: f"Week {w[1]}, {w[0]}")
    by_store = sales_summary(con, yr, wk, ["store"])
    by_cat = sales_summary(con, yr, wk, ["dept", "cat"])
    slow = slow_movers(con, yr, wk, settings.trading)
    followup = transfer_followup(con, yr, wk, settings.trading)

# ---------------------------------------------------------------- sales
total = by_store[["sales", "sales_lw", "sales_ly"]].sum(min_count=1)
cols = st.columns(3)
cols[0].metric("Sales", f"€{total['sales']:,.0f}")
if total["sales_lw"]:
    cols[1].metric("vs last week", f"€{total['sales_lw']:,.0f}", f"{total['sales'] / total['sales_lw'] - 1:+.0%}")
if total["sales_ly"]:
    cols[2].metric("vs same week last year", f"€{total['sales_ly']:,.0f}", f"{total['sales'] / total['sales_ly'] - 1:+.0%}")

st.subheader("By store")
st.dataframe(with_total(by_store, "store"), hide_index=True, column_config={"store": "Store", **SALES_COLUMNS})

st.subheader("By category")
depts = st.pills("Department", sorted(by_cat["dept"].unique()), selection_mode="multi", default=sorted(by_cat["dept"].unique()))
st.dataframe(
    by_cat[by_cat["dept"].isin(depts or [])].sort_values("sales", ascending=False),
    hide_index=True,
    column_config={"dept": "Dept", "cat": "Category", **SALES_COLUMNS},
)

# ---------------------------------------------------------------- slow movers
st.subheader("Slow movers")
rules = settings.trading
st.caption(
    f"Lines the chain holds that sold {rules.slow_max_units} units or fewer in the last "
    f"{rules.slow_window_weeks} weeks. Not listed: lines first stocked in the last {rules.slow_new_in_weeks} "
    f"weeks, and seasonal lines that sold at least {rules.slow_seasonal_share:.0%} of today's stock over "
    f"the coming {rules.slow_window_weeks} weeks last year."
)
cols = st.columns(3)
cols[0].metric("Lines", f"{len(slow):,}")
cols[1].metric("Units", f"{slow['stock'].sum():,.0f}")
cols[2].metric("Stock value", f"€{slow['value'].sum():,.0f}")
st.dataframe(
    slow,
    hide_index=True,
    column_config={
        "dept": "Dept", "cat": "Category", "sty": "Style", "col": "Colour",
        "stock": st.column_config.NumberColumn("Stock", format="%d"),
        "stores": st.column_config.NumberColumn("Stores", format="%d"),
        "recent_units": st.column_config.NumberColumn(f"Sold ({rules.slow_window_weeks} wks)", format="%d"),
        "status": "Status",
        "value": st.column_config.NumberColumn("Value", format="€%.0f"),
    },
)

# ---------------------------------------------------------------- transfer follow-up
st.subheader("Last week's transfers")
batch_yr, batch_wk = previous_week(yr, wk)
if followup.empty:
    st.caption(f"No approved transfers in the week {batch_wk}, {batch_yr} batch.")
else:
    st.caption(
        f"Approved runs from the week {batch_wk}, {batch_yr} batch, checked against this week's stock. "
        "An estimate from stock movement: deliveries from the warehouse or returns can blur it."
    )
    counts = followup["status"].value_counts()
    cols = st.columns(3)
    for col, status in zip(cols, ["Received", "Sent, not all received", "Not sent"]):
        col.metric(status, int(counts.get(status, 0)))
    st.dataframe(
        followup.drop(columns="run_id"),
        hide_index=True,
        column_config={
            "sender": "From", "receiver": "To", "sty": "Style", "col": "Colour",
            "units": st.column_config.NumberColumn("Units", format="%d"),
            "left_sender": st.column_config.NumberColumn("Left sender", format="%d"),
            "arrived": st.column_config.NumberColumn("Arrived", format="%d"),
            "status": "Status",
        },
    )
