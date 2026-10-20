"""Re-export shim for :mod:`media_core.ui.widgets.icons`.

The canonical home is now :mod:`media_core.ui.widgets.icons`.  Existing
``from quaver.ui.icons import icon_play, …`` call sites keep working
via this re-export.

Note: the location moved one level deeper (Quaver had ``ui/icons.py``;
media_core has ``ui/widgets/icons.py``) so the icons live alongside
the widgets that consume them.
"""

from media_core.ui.widgets.icons import (  # noqa: F401
    icon_app,
    icon_fullscreen,
    icon_hamburger,
    icon_next,
    icon_pause,
    icon_play,
    icon_prev,
    icon_queue_panel,
    icon_repeat,
    icon_repeat_off,
    icon_repeat_one,
    icon_shuffle,
    icon_stop,
    icon_volume,
    icon_volume_mute,
    set_icon_button,
)
