"""Re-export shim for :mod:`media_core.core.replaygain`.

The canonical home is now :mod:`media_core.core.replaygain`.  New code
should import directly from media_core; this shim keeps existing
``from quaver.core.replaygain import ...`` call sites working without
churn.
"""

from media_core.core.replaygain import (  # noqa: F401
    AudioFilters,
    RGMode,
    apply_compressor,
    apply_eq_bands,
    apply_rg_mode,
    insert_audio_filters,
    insert_rgvolume,
)
