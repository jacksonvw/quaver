"""Re-export shim for :mod:`media_core.services.player_service`.

The canonical home is now :mod:`media_core.services.player_service`.
New code should import directly from media_core; this shim keeps
existing ``from quaver.services.player_service import ...`` call sites
working without churn.

Quaver passes its ``LibraryDB`` instance for ``db`` — duck-typed via
the ``_DurationSink`` Protocol in media_core.  Minim passes ``None``.
"""

from media_core.services.player_service import (  # noqa: F401
    PlayerService,
    RepeatMode,
)
