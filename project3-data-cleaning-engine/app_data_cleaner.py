"""REFINE — Automated Data Cleaning Engine (Streamlit entry point).

Run from this folder:            streamlit run app_data_cleaner.py
Or from the repository root:     streamlit run project3-data-cleaning-engine/app_data_cleaner.py

The file name is kept from the original app so the existing Streamlit
Community Cloud deployment keeps pointing at the right entry point.
"""

import sys
from pathlib import Path

# Make refine/, ui/ and views/ importable however the app is launched.
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import streamlit as st  # noqa: E402

from ui import theme  # noqa: E402
from ui.components import about_section  # noqa: E402
from ui.sidebar import render_brand, render_status  # noqa: E402

st.set_page_config(
    page_title="REFINE · Data Cleaning Engine",
    page_icon=":material/cleaning_services:",
    layout="wide",
    initial_sidebar_state="auto",  # Open on desktop, collapsed on phones
)
st.markdown(theme.CSS, unsafe_allow_html=True)

VIEWS = APP_DIR / "views"
PAGES = [
    # The default page is always served at "/".
    st.Page(VIEWS / "upload.py", title="Upload", icon=":material/upload_file:", default=True),
    st.Page(VIEWS / "profile.py", title="Profile & Quality", icon=":material/query_stats:",
            url_path="profile"),
    st.Page(VIEWS / "clean.py", title="Clean", icon=":material/cleaning_services:", url_path="clean"),
    st.Page(VIEWS / "compare.py", title="Compare", icon=":material/compare_arrows:", url_path="compare"),
    st.Page(VIEWS / "history.py", title="History", icon=":material/history:", url_path="history"),
    st.Page(VIEWS / "export.py", title="Export", icon=":material/download:", url_path="export"),
]

render_brand()
page = st.navigation(PAGES, position="sidebar")
render_status()
page.run()
st.space("medium")
about_section()
