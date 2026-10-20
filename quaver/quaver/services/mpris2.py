"""Re-export shim for :mod:`media_core.services.mpris2`.

The canonical home is now :mod:`media_core.services.mpris2`.  New code
should import directly from media_core; this shim keeps existing
``from quaver.services.mpris2 import ...`` call sites working without
churn.

Quaver's existing constructor call ``Mpris2Service(self._player)`` works
unchanged because the new kw-only identity parameters default to Quaver
values.  Sibling apps (Minim) override them.
"""

from media_core.services.mpris2 import (  # noqa: F401
    BUS_NAME,
    Mpris2Service,
)
