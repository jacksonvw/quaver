"""Re-export shim for :mod:`media_core.ui.widgets.queue_widget`.

The canonical home is now :mod:`media_core.ui.widgets.queue_widget`.
Existing ``from quaver.ui.widgets.queue_widget import ...`` call sites
keep working via this re-export.

**Side-effect on import:** installs Quaver's library-flavored
``art_cache`` (which adds scraper ``poster_path`` support) as the
queue's art provider so video tracks show scraper-provided stills in
the queue.  Minim never imports this shim, so its queue uses the
default :mod:`media_core.ui.widgets.embedded_art_cache`.
"""

from media_core.ui.widgets.queue_widget import (  # noqa: F401
    QueueWidget,
    build_stream_menus,
    set_art_provider,
)

# Wire Quaver's art_cache (with poster_path support) into the queue.
# Done at import time so any code that constructs a QueueWidget
# afterwards picks up the right provider.
from quaver.ui.widgets import art_cache as _quaver_art_cache

set_art_provider(_quaver_art_cache)
