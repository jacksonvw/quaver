"""Re-export shim for :mod:`media_core.ui.widgets.subtitle_overlay`.

The canonical home is now :mod:`media_core.ui.widgets.subtitle_overlay`.
Existing ``from quaver.ui.widgets.subtitle_overlay import ...`` call
sites keep working via this re-export.
"""

from media_core.ui.widgets.subtitle_overlay import SubtitleOverlay  # noqa: F401
