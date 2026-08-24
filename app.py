"""Vercel ASGI entrypoint for the WISER Streamlit app."""

from __future__ import annotations

from pathlib import Path

import streamlit as st


STREAMLIT_SCRIPT = Path(__file__).with_name("streamlit_app.py")

# Vercel's Python runtime looks for a top-level WSGI/ASGI object named `app`
# in recognized entrypoint files. The Streamlit UI remains in streamlit_app.py.
app = st.App(str(STREAMLIT_SCRIPT))


if __name__ == "__main__":
    app.run()
