"""Quaver-flavored Now Playing panel — wraps :class:`media_core.ui.panels.nowplaying_panel.NowPlayingPanel`.

The actual implementation lives in media_core; this module is a thin
adapter that pre-binds Quaver's config persistence, scraper poster
fetcher, and a Quaver-only "Open in Minim" right-click action.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from media_core.ui.panels.nowplaying_panel import (
    NowPlayingPanel as _MediaCoreNowPlayingPanel,
)
from quaver.core.config import load_config, save_config
from quaver.core.external_apps import open_in_minim
from quaver.ui.widgets.art_cache import _read_poster_file


class NowPlayingPanel(_MediaCoreNowPlayingPanel):
    """Now Playing panel pre-wired with Quaver's config + scraper posters.

    Quaver's ``AppConfig`` inherits from media_core's ``PlayerConfig``
    so the load/save callables are signature-compatible.

    Also injects an "Open in Minim" entry on the right-click menu via
    :paramref:`extra_context_actions` — sends the currently-playing
    file to a fresh Minim instance without disturbing Quaver's queue.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            load_config=load_config,
            save_config=save_config,
            poster_reader=_read_poster_file,
            extra_context_actions=self._extra_context_actions,
        )

    def _extra_context_actions(self) -> list[tuple[str, object]]:
        """Build the Quaver-only context-menu items for the Now Playing
        right-click.  Currently just "Open in Minim" — added when there
        is a current track and its filepath is a real local file (not
        a ``youtube://`` URI).
        """
        track = getattr(self, "_current_track", None)
        if track is None or not track.filepath:
            return []
        if track.filepath.startswith(("youtube://", "http://", "https://")):
            return []
        return [
            ("Open in Minim", lambda _p=track.filepath: open_in_minim([_p])),
        ]
