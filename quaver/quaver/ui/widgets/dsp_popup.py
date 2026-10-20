"""Re-export shim for :mod:`media_core.ui.widgets.dsp_popup`.

The canonical home is now :mod:`media_core.ui.widgets.dsp_popup`.
Existing ``from quaver.ui.widgets.dsp_popup import ...`` call sites
keep working via this re-export.
"""

from media_core.ui.widgets.dsp_popup import DspPopup  # noqa: F401
