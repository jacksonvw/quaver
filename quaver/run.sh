#!/bin/bash
# Pin /usr/bin/python3: the dependencies are system packages, and a bare
# `python3` can resolve to a virtualenv earlier on PATH that lacks PySide6.
cd "$(dirname "$0")"
/usr/bin/python3 -m quaver.main "$@"
