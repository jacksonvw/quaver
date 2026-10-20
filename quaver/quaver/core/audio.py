"""Re-export shim for :mod:`media_core.core.audio`.

The canonical home is now :mod:`media_core.core.audio`.  New code
should import directly from media_core; this shim keeps existing
``from quaver.core.audio import ...`` call sites working without
churn.
"""

from media_core.core.audio import (  # noqa: F401
    AudioEngine,
    PlaybackState,
)
