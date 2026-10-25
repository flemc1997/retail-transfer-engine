"""Streamlit entry point. Run from the repo root:  streamlit run app.py"""

import streamlit as st

st.set_page_config(page_title="Transfer engine", layout="wide")

pages = [
    st.Page("app_pages/upload.py", title="Weekly upload", icon=":material/upload_file:"),
]
st.navigation(pages).run()
