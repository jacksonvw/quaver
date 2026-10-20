"""Re-export shim for :mod:`media_core.core.tagger`.

The canonical home is now :mod:`media_core.core.tagger`.  New code
should import directly from media_core; this shim keeps existing
``from quaver.core.tagger import ...`` call sites working without
churn.
"""

from media_core.core.tagger import (  # noqa: F401
    TrackTags,
    read_tags,
    write_tags,
)
