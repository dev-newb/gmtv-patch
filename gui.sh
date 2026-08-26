#!/bin/bash
# Launch the Android TV Patcher front end.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python gmtv_gui.py
