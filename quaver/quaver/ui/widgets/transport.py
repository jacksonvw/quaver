"""Re-export shim for :mod:`media_core.ui.widgets.transport`.

The canonical home is now :mod:`media_core.ui.widgets.transport`.
Existing ``from quaver.ui.widgets.transport import ...`` call sites
keep working via this re-export.
"""

from media_core.ui.widgets.transport import TransportWidget  # noqa: F401
