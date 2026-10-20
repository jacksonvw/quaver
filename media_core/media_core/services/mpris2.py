"""MPRIS2 D-Bus service.

Exposes org.mpris.MediaPlayer2 and org.mpris.MediaPlayer2.Player on the
session bus so KDE Plasma (and other MPRIS clients) can control playback.

Uses dbus-python + dbus.service since it ships with the system Python on
most Linux distros.

The MPRIS2 identity (bus-name suffix, ``Identity``, ``DesktopEntry``) is
parameterized so each shell — Quaver and Minim — registers under its
own well-known name.  Defaults match Quaver for backwards compatibility
with existing call sites.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import dbus
import dbus.mainloop.glib
import dbus.service

from media_core.core.audio import PlaybackState

if TYPE_CHECKING:
    from media_core.services.player_service import PlayerService

# Must be called before any D-Bus connection is made.
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

OBJECT_PATH = "/org/mpris/MediaPlayer2"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_PROPERTIES = "org.freedesktop.DBus.Properties"

# Default identity — preserved for back-compat with existing Quaver
# callers that construct ``Mpris2Service(player)`` with no overrides.
# Sibling apps (Minim) pass their own values via the kw-only params.
_DEFAULT_BUS_NAME = "org.mpris.MediaPlayer2.quaver"
_DEFAULT_IDENTITY = "Quaver"
_DEFAULT_DESKTOP_ENTRY = "quaver"

# Re-export the legacy module-level constant for any code that relied
# on it.  New callers should pass ``bus_name=…`` to the constructor.
BUS_NAME = _DEFAULT_BUS_NAME


def _playback_status(state: PlaybackState) -> str:
    return {
        PlaybackState.PLAYING: "Playing",
        PlaybackState.PAUSED: "Paused",
        PlaybackState.STOPPED: "Stopped",
    }.get(state, "Stopped")


class Mpris2Service(dbus.service.Object):
    """MPRIS2-compliant D-Bus object.

    Construct with ``player`` (the :class:`PlayerService` to control)
    and optionally override the MPRIS2 identity for non-Quaver shells:

    .. code-block:: python

        # Quaver (defaults)
        Mpris2Service(player)

        # Minim
        Mpris2Service(
            player,
            bus_name="org.mpris.MediaPlayer2.minim",
            identity="Minim",
            desktop_entry="minim",
        )
    """

    def __init__(
        self,
        player: PlayerService,
        *,
        bus_name: str = _DEFAULT_BUS_NAME,
        identity: str = _DEFAULT_IDENTITY,
        desktop_entry: str = _DEFAULT_DESKTOP_ENTRY,
    ) -> None:
        self._player = player
        self._identity = identity
        self._desktop_entry = desktop_entry
        self._bus = dbus.SessionBus()
        self._bus_name = dbus.service.BusName(bus_name, self._bus)
        super().__init__(self._bus_name, OBJECT_PATH)

        # Connect player signals for property change notifications.
        player.state_changed.connect(self._notify_playback_status)
        player.track_changed.connect(self._notify_metadata)

    # ------------------------------------------------------------------
    # org.mpris.MediaPlayer2
    # ------------------------------------------------------------------

    @dbus.service.method(IFACE_ROOT, in_signature="", out_signature="")
    def Raise(self) -> None:
        pass  # Could raise the window — optional.

    @dbus.service.method(IFACE_ROOT, in_signature="", out_signature="")
    def Quit(self) -> None:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.quit()

    # ------------------------------------------------------------------
    # org.mpris.MediaPlayer2.Player
    # ------------------------------------------------------------------

    @dbus.service.method(IFACE_PLAYER, in_signature="", out_signature="")
    def Next(self) -> None:
        self._player.next_track()

    @dbus.service.method(IFACE_PLAYER, in_signature="", out_signature="")
    def Previous(self) -> None:
        self._player.prev_track()

    @dbus.service.method(IFACE_PLAYER, in_signature="", out_signature="")
    def Pause(self) -> None:
        self._player.pause()

    @dbus.service.method(IFACE_PLAYER, in_signature="", out_signature="")
    def PlayPause(self) -> None:
        self._player.toggle_pause()

    @dbus.service.method(IFACE_PLAYER, in_signature="", out_signature="")
    def Stop(self) -> None:
        self._player.stop()

    @dbus.service.method(IFACE_PLAYER, in_signature="", out_signature="")
    def Play(self) -> None:
        state = self._player.engine.state
        if state == PlaybackState.PAUSED:
            self._player.resume()
        elif state == PlaybackState.STOPPED and self._player.current_track:
            self._player.toggle_pause()

    @dbus.service.method(IFACE_PLAYER, in_signature="x", out_signature="")
    def Seek(self, offset_us: int) -> None:
        pos = self._player.engine.get_position_ms()
        self._player.seek(pos + offset_us // 1000)

    @dbus.service.method(IFACE_PLAYER, in_signature="ox", out_signature="")
    def SetPosition(self, track_id: str, position_us: int) -> None:
        self._player.seek(position_us // 1000)

    @dbus.service.method(IFACE_PLAYER, in_signature="s", out_signature="")
    def OpenUri(self, uri: str) -> None:
        self._player.engine.play_uri(uri)

    # ------------------------------------------------------------------
    # Properties (via org.freedesktop.DBus.Properties)
    # ------------------------------------------------------------------

    @dbus.service.method(IFACE_PROPERTIES, in_signature="ss", out_signature="v")
    def Get(self, interface: str, prop: str):
        return self._get_all(interface).get(prop, "")

    @dbus.service.method(IFACE_PROPERTIES, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str):
        return self._get_all(interface)

    @dbus.service.method(IFACE_PROPERTIES, in_signature="ssv", out_signature="")
    def Set(self, interface: str, prop: str, value) -> None:
        if interface == IFACE_PLAYER:
            if prop == "Volume":
                self._player.set_volume(float(value))

    def _get_all(self, interface: str) -> dict:
        if interface == IFACE_ROOT:
            return {
                "CanQuit": dbus.Boolean(True),
                "CanRaise": dbus.Boolean(False),
                "HasTrackList": dbus.Boolean(False),
                "Identity": dbus.String(self._identity),
                "DesktopEntry": dbus.String(self._desktop_entry),
                "SupportedUriSchemes": dbus.Array(["file"], signature="s"),
                "SupportedMimeTypes": dbus.Array([
                    "audio/mpeg", "audio/flac", "audio/ogg",
                    "audio/x-vorbis+ogg", "audio/mp4", "audio/x-wav",
                ], signature="s"),
            }

        if interface == IFACE_PLAYER:
            track = self._player.current_track
            metadata = self._build_metadata(track)
            pos_us = dbus.Int64(self._player.engine.get_position_ms() * 1000)

            return {
                "PlaybackStatus": dbus.String(
                    _playback_status(self._player.engine.state)
                ),
                "Rate": dbus.Double(1.0),
                "Metadata": dbus.Dictionary(metadata, signature="sv"),
                "Volume": dbus.Double(self._player.get_volume()),
                "Position": pos_us,
                "MinimumRate": dbus.Double(1.0),
                "MaximumRate": dbus.Double(1.0),
                "CanGoNext": dbus.Boolean(True),
                "CanGoPrevious": dbus.Boolean(True),
                "CanPlay": dbus.Boolean(True),
                "CanPause": dbus.Boolean(True),
                "CanSeek": dbus.Boolean(True),
                "CanControl": dbus.Boolean(True),
            }

        return {}

    def _build_metadata(self, track) -> dict:
        if track is None:
            return {"mpris:trackid": dbus.ObjectPath("/org/mpris/MediaPlayer2/NoTrack")}

        track_path = f"/org/mpris/MediaPlayer2/Track/{abs(hash(track.filepath))}"
        meta: dict = {
            "mpris:trackid": dbus.ObjectPath(track_path),
            "xesam:title": dbus.String(track.title or ""),
            "xesam:artist": dbus.Array([track.artist or ""], signature="s"),
            "xesam:album": dbus.String(track.album or ""),
        }
        if track.duration_ms:
            meta["mpris:length"] = dbus.Int64(track.duration_ms * 1000)
        if track.filepath:
            from pathlib import Path
            meta["xesam:url"] = dbus.String(Path(track.filepath).as_uri())
        return meta

    # ------------------------------------------------------------------
    # Change notifications
    # ------------------------------------------------------------------

    @dbus.service.signal(IFACE_PROPERTIES, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def _notify_playback_status(self, state: PlaybackState) -> None:
        self.PropertiesChanged(
            IFACE_PLAYER,
            {"PlaybackStatus": dbus.String(_playback_status(state))},
            [],
        )

    def _notify_metadata(self, track) -> None:
        metadata = self._build_metadata(track)
        self.PropertiesChanged(
            IFACE_PLAYER,
            {"Metadata": dbus.Dictionary(metadata, signature="sv")},
            [],
        )
