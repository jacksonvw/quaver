"""Re-export shim for :mod:`media_core.core.network`.

The canonical home is now :mod:`media_core.core.network`.  New code
should import directly from media_core; this shim keeps existing
``from quaver.core.network import ...`` call sites working without
churn.
"""

from media_core.core.network import (  # noqa: F401
    ensure_mounted,
    is_mounted,
    list_smb_shares,
    uri_to_local_path,
)
