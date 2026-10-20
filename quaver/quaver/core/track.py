"""Re-export shim for :mod:`media_core.core.track`.

Existing Quaver call sites use ``from quaver.core.track import ...``;
the canonical home is now :mod:`media_core.core.track`.  New code should
import directly from media_core; this shim exists to avoid a churning
rename across 20+ Quaver-side importers.
"""

from media_core.core.track import (  # noqa: F401
    EPISODE_IDS_MIME,
    PLAYLIST_IDS_MIME,
    TRACK_IDS_MIME,
    VIDEO_IDS_MIME,
    Track,
)
