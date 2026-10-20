"""Network mount helper — KIO FUSE for SMB shares on KDE Plasma.

Uses KIO FUSE (org.kde.KIOFuse D-Bus service) to expose smb:// URIs as
local FUSE paths that GStreamer, the scanner, and watchdog can access
transparently — no GVFS, no gio mount, no credentials needed (KIO
handles auth the same way Dolphin does).

On non-Linux platforms the public functions return safe no-op values.
Windows accesses SMB shares natively as UNC paths (\\\\server\\share).

No PySide6 imports — must be usable from core and services layers.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# D-Bus constants for KIO FUSE.
_KIO_FUSE_BUS = "org.kde.KIOFuse"
_KIO_FUSE_PATH = "/org/kde/KIOFuse"
_KIO_FUSE_IFACE = "org.kde.KIOFuse.VFS"

# In-process cache of URIs we've already asked KIO to mount.
# Avoids re-stat'ing FUSE paths (which goes through the SMB stack and wakes
# the disk on every check).  KIO itself caches mounts, so calling mountUrl
# again is cheap; the expensive part was Path.is_dir() on the fuse path.
_mounted_in_session: set[str] = set()


def ensure_mounted(uri: str, timeout_sec: float = 15.0) -> tuple[bool, str]:
    """Mount an SMB URI via KIO FUSE.  Returns (success, error_message).

    On success the local FUSE path can be obtained via ``uri_to_local_path``.
    If already mounted, returns immediately.
    """
    if sys.platform != "linux":
        return False, "Network mounting via KIO FUSE is only available on Linux"
    uri = _normalise_uri(uri)

    # Fast path: this process already mounted it.  Don't stat the FUSE path
    # to verify — that would go through SMB and spin up the NAS.
    if uri in _mounted_in_session:
        return True, ""

    log.info("Mounting %s via KIO FUSE …", uri)
    try:
        result = subprocess.run(
            [
                "dbus-send", "--session", "--print-reply",
                "--dest=" + _KIO_FUSE_BUS,
                "--type=method_call",
                _KIO_FUSE_PATH,
                _KIO_FUSE_IFACE + ".mountUrl",
                f"string:{uri}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if result.returncode == 0:
            log.info("Mounted %s", uri)
            _mounted_in_session.add(uri)
            return True, ""

        stderr = result.stderr.strip()
        if "org.kde.KIOFuse" in stderr and "not provided" in stderr.lower():
            return False, (
                "KIO FUSE service not available.\n\n"
                "Install kio-fuse:\n"
                "  sudo dnf install kio-fuse   (Fedora)\n"
                "  sudo apt install kio-fuse   (Debian/Ubuntu)"
            )
        log.warning("KIO FUSE mount failed for %s: %s", uri, stderr)
        return False, stderr
    except subprocess.TimeoutExpired:
        log.warning("KIO FUSE mount timed out for %s (%.0fs)", uri, timeout_sec)
        return False, f"Mount timed out after {timeout_sec:.0f}s — is the host reachable?"
    except FileNotFoundError:
        return False, "'dbus-send' not found — is D-Bus installed?"


def uri_to_local_path(uri: str) -> str | None:
    """Convert an SMB URI to its KIO FUSE local path, or None if unknown.

    KIO FUSE paths follow the pattern:
        /run/user/<UID>/kio-fuse-XXXXXX/smb/<host>/<share>/<subpath>

    We find the kio-fuse mount root dynamically (the suffix is random).
    """
    if sys.platform != "linux":
        return None
    uri = _normalise_uri(uri)
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    path = parsed.path.strip("/")
    if not host:
        return None

    fuse_root = _find_kio_fuse_root()
    if fuse_root is None:
        return None

    local = fuse_root / "smb" / host / path
    return str(local)


def is_mounted(uri: str) -> bool:
    """Check whether this process has mounted *uri* via KIO FUSE.

    Uses the in-session cache instead of stat'ing the FUSE path — stat'ing
    it would round-trip through SMB and wake the NAS.
    """
    if sys.platform != "linux":
        return False
    return _normalise_uri(uri) in _mounted_in_session


def list_smb_shares(host: str, timeout_sec: float = 10.0) -> list[str]:
    """List available SMB shares on a host via smbclient.

    Returns share names (not full URIs). Best-effort — returns [] on failure.
    """
    if sys.platform != "linux":
        return []
    try:
        result = subprocess.run(
            ["smbclient", "-N", "-L", host],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        shares = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if "\tDisk" in line or "  Disk  " in line:
                name = re.split(r"\s{2,}|\t", line)[0].strip()
                if name and not name.startswith("$"):
                    shares.append(name)
        return shares
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


# ── internal ──────────────────────────────────────────────────────────

def _normalise_uri(uri: str) -> str:
    """Strip trailing slashes and ensure scheme is lowercase."""
    return uri.rstrip("/")


def _find_kio_fuse_root() -> Path | None:
    """Locate the kio-fuse mount directory under /run/user/<UID>/."""
    import os
    run_user = Path(f"/run/user/{os.getuid()}")
    try:
        for child in run_user.iterdir():
            if child.name.startswith("kio-fuse-") and child.is_dir():
                return child
    except OSError:
        pass
    return None
