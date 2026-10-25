"""Transfers page: generate a week's suggestions, decide on each run, download the picking list."""

import streamlit as st

from transfer_engine.batch import generate_batch, latest_week, save_batch, weeks_with_batches
from transfer_engine.decisions import (
    Decision,
    batch_runs,
    partial_decision,
    pending_run_ids,
    picking_rows,
    save_decisions,
    size_sort_key,
    sizes_text,
)
from transfer_engine.picking import picking_list_xlsx
from transfer_engine.settings import load_settings
from transfer_engine.storage import connect

LABELS = {"approved": "Approve", "partial": "Partial", "rejected": "Reject"}  # no decision = Pending
STATUSES = {"Approve": "approved", "Reject": "rejected"}

settings = load_settings()
st.title("Transfers")

if "transfers_message" in st.session_state:
    st.success(st.session_state.pop("transfers_message"))

with connect(settings.db_path, read_only=True) as con:
    weeks = sorted(set(weeks_with_batches(con)) | {latest_week(con)}, reverse=True)
yr, wk = st.selectbox("Week", weeks, format_func=lambda w: f"Week {w[1]}, {w[0]}")


def generate() -> None:
    """Work out the week's suggestions and save them (decisions on unchanged runs are kept)."""
    with connect(settings.db_path) as con:
        runs, rows = generate_batch(con, settings, yr, wk)
        save_batch(con, yr, wk, rows)
    st.session_state["transfers_message"] = f"{len(runs)} suggestions for week {wk}, {yr}."


with connect(settings.db_path, read_only=True) as con:
    runs = batch_runs(con, yr, wk)

if runs.empty:
    st.info("No suggestions for this week yet.")
    if st.button("Generate suggestions", type="primary"):
        generate()
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- summary
decided = runs["status"].value_counts()
cols = st.columns(5)
cols[0].metric("Suggestions", len(runs))
cols[1].metric("Units", f"{runs['qty'].sum():,}")
cols[2].metric("Approved", decided.get("approved", 0) + decided.get("partial", 0))
cols[3].metric("Rejected", decided.get("rejected", 0))
cols[4].metric("Pending", runs["status"].isna().sum())

with st.expander("Regenerate suggestions"):
    st.caption(
        "Works the week out again from history, e.g. after re-uploading it. Decisions stay on any "
        "suggestion that comes out the same; changed suggestions start as pending."
    )
    if st.button("Regenerate"):
        generate()
        st.rerun()

# ---------------------------------------------------------------- decide
st.subheader("Decide")
f1, f2 = st.columns(2)
kinds = f1.pills("Type", ["move", "tidy"], selection_mode="multi", default=["move", "tidy"])
senders = f2.multiselect("Send from", sorted(runs["sender"].unique()))
view = runs[runs["kind"].isin(kinds or [])]
if senders:
    view = view[view["sender"].isin(senders)]

pending = pending_run_ids(view)
if st.button(f"Approve all {len(pending)} pending (in this view)", disabled=not pending):
    with connect(settings.db_path) as con:
        save_decisions(con, [Decision(run_id, "approved") for run_id in pending])
    st.session_state["transfers_message"] = f"Approved {len(pending)} run(s)."
    st.rerun()

table = view.assign(
    sizes_shown=[sizes_text(r.sent_sizes if r.status == "partial" else r.sizes) for r in view.itertuples()],
    decision=view["status"].map(LABELS).fillna("Pending"),
).set_index("run_id")[["kind", "sender", "receiver", "sty", "col", "sizes_shown", "qty", "reason", "decision", "note"]]

edited = st.data_editor(
    table,
    key=f"editor_{yr}_{wk}",
    hide_index=True,
    width="stretch",
    disabled=["kind", "sender", "receiver", "sty", "col", "sizes_shown", "qty", "reason"],
    column_config={
        "kind": "Type",
        "sender": "From",
        "receiver": "To",
        "sty": st.column_config.TextColumn("Style", width="large"),
        "col": "Colour",
        "sizes_shown": "Sizes",
        "qty": "Units",
        "reason": st.column_config.TextColumn("Why", width="large"),
        "decision": st.column_config.SelectboxColumn("Decision", options=["Pending", "Approve", "Reject", "Partial"], required=True),
        "note": st.column_config.TextColumn("Note"),
    },
)

changed = edited[(edited["decision"] != table["decision"]) | (edited["note"] != table["note"])]
if st.button(f"Save {len(changed)} change(s)", type="primary", disabled=changed.empty):
    decisions, clear, problems = [], [], []
    for run_id, row in changed.iterrows():
        current = runs.set_index("run_id").loc[run_id]
        if row["decision"] == "Pending":
            clear.append(run_id)
        elif row["decision"] in STATUSES:
            decisions.append(Decision(run_id, STATUSES[row["decision"]], row["note"]))
        elif current["status"] == "partial":  # still partial: only the note changed
            decisions.append(Decision(run_id, "partial", row["note"], current["sent_sizes"]))
        else:
            problems.append(f"{row['sty']} {row['col']}")
    if problems:
        st.error("To send only some sizes, use 'Send only some sizes' below: " + ", ".join(problems))
    else:
        with connect(settings.db_path) as con:
            save_decisions(con, decisions, clear)
        st.session_state["transfers_message"] = f"Saved {len(changed)} change(s)."
        st.rerun()

# ---------------------------------------------------------------- partial
with st.expander("Send only some sizes"):
    labels = {r.run_id: f"{r.sender} → {r.receiver} · {r.sty} · {r.col}" for r in view.itertuples()}
    chosen_run = st.selectbox("Run", list(labels), format_func=labels.get, key="partial_run")
    if chosen_run:
        run = runs.set_index("run_id").loc[chosen_run]
        start = run["sent_sizes"] if run["status"] == "partial" else run["sizes"]
        sizes = sorted(run["sizes"], key=size_sort_key)
        boxes = st.columns(min(len(sizes), 6))
        chosen = {
            size: boxes[i % len(boxes)].number_input(
                f"{size} (holds {run['sizes'][size]})", min_value=0, max_value=int(run["sizes"][size]),
                value=int(start.get(size, 0)), key=f"partial_{chosen_run}_{size}",
            )
            for i, size in enumerate(sizes)
        }
        note = st.text_input("Note", value=run["note"], key=f"partial_note_{chosen_run}")
        if st.button("Save sizes"):
            try:
                decision = partial_decision(chosen_run, run["sizes"], chosen, note)
            except ValueError as exc:
                st.error(str(exc))
            else:
                with connect(settings.db_path) as con:
                    save_decisions(con, [decision])
                kind = "Partial approval" if decision.status == "partial" else "Every size chosen, so approved in full"
                st.session_state["transfers_message"] = f"{kind}: {labels[chosen_run]}."
                st.rerun()

# ---------------------------------------------------------------- picking list
st.subheader("Picking list")
with connect(settings.db_path, read_only=True) as con:
    picks = picking_rows(con, yr, wk)
if picks.empty:
    st.caption("Approve some runs to build the picking list.")
else:
    st.caption(f"{len(picks)} runs, {picks['units'].sum():,} units, from {picks['sender'].nunique()} store(s).")
    st.download_button(
        "Download picking list (Excel)",
        data=picking_list_xlsx(picks, yr, wk),
        file_name=f"picking-list-{yr}-W{wk:02d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
