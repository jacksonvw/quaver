"""Re-export shim for :mod:`media_core.ui.widgets.video_surface`.

The canonical home is now :mod:`media_core.ui.widgets.video_surface`.
Existing ``from quaver.ui.widgets.video_surface import ...`` call sites
keep working via this re-export.
"""

from media_core.ui.widgets.video_surface import VideoSurface  # noqa: F401
