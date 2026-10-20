"""Re-export shim for :mod:`media_core.core.embedded_art`.

The canonical home is now :mod:`media_core.core.embedded_art`.  New
code should import directly from media_core; this shim keeps existing
``from quaver.core.embedded_art import ...`` call sites (notably the
art cache modules and tests) working without churn.
"""

from media_core.core.embedded_art import (  # noqa: F401
    extract_art,
    extract_embedded_art,
    find_folder_art,
)
