"""Re-export shim for :mod:`media_core.core.libass_renderer`.

The canonical home is now :mod:`media_core.core.libass_renderer`.  New
code should import directly from media_core; this shim keeps existing
``from quaver.core.libass_renderer import ...`` call sites working
without churn.
"""

from media_core.core.libass_renderer import (  # noqa: F401
    LibassRenderer,
    SubtitleBitmap,
)
