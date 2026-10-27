"""Keep the screen on and the machine awake while a video plays.

Asks the desktop's Inhibit portal, which a Flatpak may use without any
extra permission and which works outside one too.  The desktop drops the
request itself if the app exits without releasing it.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_PORTAL = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SUSPEND, _IDLE = 4, 8  # the portal's flags; idle covers screen blanking


class SleepInhibitor:
    """Holds one inhibit request at a time, while told to."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._handle: str | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def set_active(self, active: bool) -> None:
        if active and self._handle is None:
            self._handle = self._inhibit()
        elif not active and self._handle is not None:
            self._release(self._handle)
            self._handle = None

    def _inhibit(self) -> str | None:
        try:
            import dbus

            portal = dbus.Interface(dbus.SessionBus().get_object(_PORTAL, _PORTAL_PATH),
                                    "org.freedesktop.portal.Inhibit")
            return str(portal.Inhibit("", dbus.UInt32(_SUSPEND | _IDLE),
                                      {"reason": self._reason}))
        except Exception:
            log.warning("Couldn't ask the desktop to stay awake", exc_info=True)
            return None

    @staticmethod
    def _release(handle: str) -> None:
        try:
            import dbus

            dbus.SessionBus().get_object(_PORTAL, handle).Close(
                dbus_interface="org.freedesktop.portal.Request")
        except Exception:
            log.warning("Couldn't release the stay-awake request", exc_info=True)
