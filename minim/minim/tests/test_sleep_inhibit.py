"""Minim keeps the machine awake while a video plays, and only then."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import minim  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from media_core.core.audio import PlaybackState
from media_core.services.sleep_inhibitor import SleepInhibitor


@pytest.fixture
def portal(monkeypatch) -> list:
    calls: list = []

    class _Request:
        def __init__(self, path: str) -> None:
            self.path = path

        def Close(self, dbus_interface: str) -> None:
            calls.append(("close", self.path))

    class _Inhibit:
        def Inhibit(self, window, flags, options):
            calls.append(("inhibit", int(flags), options["reason"]))
            return "/request/1"

    monkeypatch.setitem(sys.modules, "dbus", SimpleNamespace(
        SessionBus=lambda: SimpleNamespace(get_object=lambda name, path: _Request(path)),
        Interface=lambda obj, iface: _Inhibit(),
        UInt32=int,
    ))
    return calls


def test_one_request_held_then_released(portal) -> None:
    inhibitor = SleepInhibitor("Playing a video")
    inhibitor.set_active(True)
    inhibitor.set_active(True)
    assert inhibitor.active
    inhibitor.set_active(False)
    inhibitor.set_active(False)
    # Suspend (4) and idle (8): no sleep, no screen blanking.
    assert portal == [("inhibit", 12, "Playing a video"), ("close", "/request/1")]


@pytest.mark.parametrize("media_type, state, awake", [
    ("movie", PlaybackState.PLAYING, True),
    ("movie", PlaybackState.PAUSED, False),
    ("audio", PlaybackState.PLAYING, False),  # a commentary track beside the film
])
def test_only_a_playing_video_keeps_it_awake(portal, media_type, state, awake) -> None:
    from minim.ui.main_window import MainWindow

    window = SimpleNamespace(
        _player=SimpleNamespace(current_track=SimpleNamespace(media_type=media_type),
                                engine=SimpleNamespace(state=state)),
        _sleep_inhibitor=SleepInhibitor("Playing a video"),
    )
    MainWindow._update_sleep_inhibit(window)
    assert window._sleep_inhibitor.active is awake
