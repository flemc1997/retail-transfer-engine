"""Streamlit entry point. Run from the repo root:  streamlit run app.py"""

import streamlit as st

from transfer_engine.settings import load_settings
from transfer_engine.storage import connect

st.set_page_config(page_title="Transfer engine", layout="wide")

# Bring the database up to date (new tables or columns) before any page opens it,
# since pages that only look use read-only connections, which can't change it.
connect(load_settings().db_path).close()

pages = [
    st.Page("app_pages/upload.py", title="Weekly upload", icon=":material/upload_file:"),
    st.Page("app_pages/transfers.py", title="Transfers", icon=":material/local_shipping:"),
]
st.navigation(pages).run()
