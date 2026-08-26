#!/bin/bash
# Screenshot demo: show what architectures an APK carries and what each is.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python gmtv-patch.py AM2R-1.5.2-ORIGINAL-unpatched.apk --list-abis
