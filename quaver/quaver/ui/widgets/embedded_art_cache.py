"""Re-export shim for :mod:`media_core.ui.widgets.embedded_art_cache`.

The canonical home is now :mod:`media_core.ui.widgets.embedded_art_cache`.
Quaver's library-flavored ``art_cache.py`` reaches into the cache state
(``_cache``, ``_get_pool``, ``_result``, ``_scale_to_pixmap``,
``_is_network_path``) to share allocations with the generic cache, so
those underscore-prefixed names are re-exported alongside the public
API for backwards compatibility.
"""

from media_core.ui.widgets.embedded_art_cache import (  # noqa: F401
    ArtResult,
    _cache,
    _get_pool,
    _is_network_path,
    _result,
    _scale_to_pixmap,
    clear_cache,
    get_thumbnail,
    get_thumbnail_sync,
    on_art_ready,
)
