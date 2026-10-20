"""Re-export shim for :mod:`media_core.ui.widgets.fullscreen_overlay`.

The canonical home is now :mod:`media_core.ui.widgets.fullscreen_overlay`.
Existing ``from quaver.ui.widgets.fullscreen_overlay import ...`` call
sites keep working via this re-export.
"""

from media_core.ui.widgets.fullscreen_overlay import FullscreenOverlay  # noqa: F401
