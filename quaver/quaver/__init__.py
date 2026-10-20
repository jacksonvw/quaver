"""Quaver — desktop music library shell built on top of media_core."""

# Make the sibling media_core/ subproject importable when running Quaver
# from a source checkout (e.g. ``python3 -m quaver.main`` from
# QuaverSuite/quaver/, or pytest from the same directory).  Looks for
# ``../media_core/media_core/__init__.py`` and prepends the project dir
# to sys.path if found.  Once media_core is published or installed via
# pip this bootstrap becomes a no-op and can be removed.
import sys as _sys
from pathlib import Path as _Path

_suite_root = _Path(__file__).resolve().parent.parent.parent
_media_core_root = _suite_root / "media_core"
if (_media_core_root / "media_core" / "__init__.py").is_file():
    _entry = str(_media_core_root)
    if _entry not in _sys.path:
        _sys.path.insert(0, _entry)
del _sys, _Path, _suite_root, _media_core_root
