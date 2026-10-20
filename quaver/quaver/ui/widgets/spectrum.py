"""Re-export shim for :mod:`media_core.ui.widgets.spectrum`.

The canonical home is now :mod:`media_core.ui.widgets.spectrum`.
Existing ``from quaver.ui.widgets.spectrum import ...`` call sites
keep working via this re-export.
"""

from media_core.ui.widgets.spectrum import SpectrumWidget  # noqa: F401
