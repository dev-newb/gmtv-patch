#!/bin/bash
# Screenshot demo: a full patch run against the live Shield. Writes nothing.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python gmtv-patch.py AM2R-1.5.2-ORIGINAL-unpatched.apk \
     --dry-run --from-device 192.168.1.66:5555
