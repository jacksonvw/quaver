"""Spawn sibling QuaverSuite apps with a list of file paths.

Today only Minim is wired up — the helper exists in its own module
because the same pattern (subprocess.Popen + log on failure) will be
reused for the upcoming image-viewer and any other sibling apps that
take file-path argv.

The launch is fire-and-forget — Quaver doesn't track or wait on the
spawned process.  Use case is "send the current track to Minim so I
can browse a queue in Quaver while playing something else" — Quaver
keeps its own player state untouched.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def open_in_minim(paths: list[str | Path]) -> bool:
    """Spawn Minim with *paths* as argv.

    Returns ``True`` on successful spawn, ``False`` if no ``minim``
    launcher is on PATH.  Errors during spawn are logged and swallowed
    — the calling UI surface can fall back to its own behaviour.
    """
    if not paths:
        return False
    minim = shutil.which("minim")
    if not minim:
        log.warning("minim launcher not on PATH; cannot 'Open in Minim'")
        return False
    cmd = [minim, *(str(p) for p in paths)]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError as e:
        log.warning("failed to spawn minim: %s", e)
        return False
