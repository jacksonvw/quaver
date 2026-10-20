"""Re-export shim for :mod:`media_core.ui.widgets.eq_popup`.

The canonical home is now :mod:`media_core.ui.widgets.eq_popup`.
Existing ``from quaver.ui.widgets.eq_popup import ...`` call sites
keep working via this re-export.
"""

from media_core.ui.widgets.eq_popup import EqPopup  # noqa: F401
