"""Subtitles as VLC does them: files found beside the video and added from
the menu or by a drop become tracks of their own, V cycles through them, G
and H move them, and the Subtitle menu has VLC's shape.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import minim  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from media_core.core import subtitle_files
from media_core.core.subtitle_files import SubtitleError

SRT = """1
00:00:01,000 --> 00:00:02,500
Hello
there
2
00:00:03,000 --> 00:00:04,000
<i>Again</i>

00:00:05.5 --> 00:00:06,000 X1:10
No number
"""


def _write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, str):
        text = text.encode("utf-8")
    path.write_bytes(text)
    return path


# -- Reading files -----------------------------------------------------------


def test_srt_survives_missing_numbers_and_blank_lines(tmp_path) -> None:
    sub = subtitle_files.load(_write(tmp_path / "Film.srt", SRT))
    assert [(c.start_ms, c.end_ms, c.text) for c in sub.cues] == [
        (1000, 2500, "Hello\nthere"),
        (3000, 4000, "<i>Again</i>"),
        (5500, 6000, "No number"),
    ]
    assert not sub.is_ass


def test_old_files_in_windows_1252_still_read(tmp_path) -> None:
    data = "1\n00:00:01,000 --> 00:00:02,000\nCafé\n".encode("cp1252")
    assert subtitle_files.load(_write(tmp_path / "a.srt", data)).cues[0].text == "Café"


def test_a_utf8_bom_is_not_text(tmp_path) -> None:
    data = b"\xef\xbb\xbf1\n00:00:01,000 --> 00:00:02,000\nHi\n"
    assert subtitle_files.load(_write(tmp_path / "a.srt", data)).cues[0].text == "Hi"


def test_webvtt_skips_notes_and_styles_and_inline_times(tmp_path) -> None:
    vtt = ("WEBVTT\n\nNOTE a comment\nover two lines\n\nSTYLE\n::cue { color: yellow }\n\n"
           "intro\n00:01.000 --> 00:02.000 align:start\n<v Bob>Hi <00:01.500>there</v>\n")
    sub = subtitle_files.load(_write(tmp_path / "a.vtt", vtt))
    assert [(c.start_ms, c.end_ms, c.text) for c in sub.cues] == [
        (1000, 2000, "<v Bob>Hi there</v>"),
    ]


def test_ass_is_kept_whole_for_libass(tmp_path) -> None:
    ass = "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start\n"
    sub = subtitle_files.load(_write(tmp_path / "a.ass", ass))
    assert sub.is_ass and sub.script == ass.encode("utf-8")


def test_microdvd_takes_its_frame_rate_from_the_first_line(tmp_path) -> None:
    sub = subtitle_files.load(_write(tmp_path / "a.sub", "{1}{1}25\n{25}{50}One|Two\n{75}{100}{y:i}Three\n"))
    assert [(c.start_ms, c.end_ms, c.text) for c in sub.cues] == [
        (1000, 2000, "One\nTwo"), (3000, 4000, "Three"),
    ]


def test_subviewer(tmp_path) -> None:
    text = "[INFORMATION]\n[TITLE]x\n[END INFORMATION]\n\n00:00:01.00,00:00:02.50\nHello[br]world\n"
    sub = subtitle_files.load(_write(tmp_path / "a.sub", text))
    assert [(c.start_ms, c.end_ms, c.text) for c in sub.cues] == [(1000, 2500, "Hello\nworld")]


def test_pictures_and_empty_files_are_refused(tmp_path) -> None:
    with pytest.raises(SubtitleError):
        subtitle_files.load(_write(tmp_path / "vobsub.sub", b"\x00\x00\x01\xba" + bytes(200)))
    with pytest.raises(SubtitleError):
        subtitle_files.load(_write(tmp_path / "empty.srt", "\n\n"))


def test_overlapping_cues_show_together(tmp_path) -> None:
    sub = subtitle_files.SubtitleFile(tmp_path / "a.srt", "a.srt", cues=[
        subtitle_files.Cue(0, 10000, "A"), subtitle_files.Cue(2000, 3000, "B")])
    assert sub.text_at(2500) == "A\nB"
    assert sub.text_at(5000) == "A"
    assert sub.text_at(10000) == ""


# -- Finding them, VLC's way -------------------------------------------------


def test_found_beside_the_video_and_in_subs_folders(tmp_path) -> None:
    video = _write(tmp_path / "Film.mkv", b"")
    for name in ("Film.srt", "film.EN.srt", "Subs/Film.forced.srt",
                 "Subs/Film/English.srt", "Other.srt", "Film.txt"):
        _write(tmp_path / name, SRT)
    found = [str(p.relative_to(tmp_path)) for p in subtitle_files.find_beside(video)]
    assert found[0] == "Film.srt"  # an exact match is best
    assert sorted(found) == sorted(
        ["Film.srt", "film.EN.srt", "Subs/Film.forced.srt", "Subs/Film/English.srt"])


# -- The engine's tracks -----------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def engine(qapp):
    # After the QApplication, as in the apps: an engine's GLib loop started
    # first leaves GLib asserting at exit once Qt shares its context.
    from media_core.core.audio import AudioEngine

    eng = AudioEngine()
    eng._select_streams = lambda ids: None
    yield eng
    eng.shutdown()


def _two_embedded(engine) -> None:
    from media_core.core.audio import Gst

    coll = Gst.StreamCollection.new("test")
    for sid, caps, kind in (("v0", "video/x-h264", Gst.StreamType.VIDEO),
                            ("t0", "application/x-ass", Gst.StreamType.TEXT),
                            ("t1", "application/x-ass", Gst.StreamType.TEXT)):
        coll.add_stream(Gst.Stream.new(sid, Gst.Caps.from_string(caps), kind,
                                       Gst.StreamFlags.NONE))
    engine._stream_collection = coll
    engine._selected_streams = {"v0", "t0"}


def test_an_added_file_follows_the_embedded_tracks(engine, tmp_path) -> None:
    _two_embedded(engine)
    srt = _write(tmp_path / "Film.srt", SRT)
    serial = engine.get_subtitle_serial()

    assert engine.add_subtitle_file(srt) == 2
    assert engine.get_subtitle_tracks()[2] == (2, "Film.srt")
    assert engine.get_current_subtitle() == 2
    assert engine.get_active_external().path == srt
    assert engine.get_subtitle_serial() == serial + 1

    # Added again, it's only chosen again.
    engine.set_current_subtitle(0)
    assert engine.add_subtitle_file(srt) == 2
    assert len(engine.get_subtitle_tracks()) == 3


def test_back_to_an_embedded_track_resets_the_overlay(engine, tmp_path) -> None:
    _two_embedded(engine)
    engine.add_subtitle_file(_write(tmp_path / "Film.srt", SRT))
    serial = engine.get_subtitle_serial()
    engine.set_current_subtitle(0)
    assert engine.get_active_external() is None
    assert engine.get_current_subtitle() == 0
    assert engine.get_subtitle_serial() == serial + 1


def test_v_goes_round_the_tracks_then_disable(engine, tmp_path) -> None:
    from media_core.ui import subtitles

    _two_embedded(engine)
    engine.add_subtitle_file(_write(tmp_path / "Film.srt", SRT))
    engine.set_current_subtitle(0)

    assert subtitles.cycle(engine) == "Subtitle track: Subtitle 2"
    assert subtitles.cycle(engine) == "Subtitle track: Film.srt"
    assert subtitles.cycle(engine) == "Subtitle track: Disable"
    assert engine.get_current_subtitle() == -1
    assert subtitles.cycle(engine) == "Subtitle track: Subtitle 1"
    assert subtitles.cycle(engine, -1) == "Subtitle track: Disable"  # Alt+V


def test_shift_v_turns_the_last_track_back_on(engine, tmp_path) -> None:
    from media_core.ui import subtitles

    _two_embedded(engine)
    engine.add_subtitle_file(_write(tmp_path / "Film.srt", SRT))
    assert subtitles.toggle(engine) == "Subtitle track: Disable"
    assert engine.get_active_external() is None
    assert subtitles.toggle(engine) == "Subtitle track: Film.srt"


def test_g_and_h_move_the_subtitles(engine) -> None:
    from media_core.core.audio import Gst
    from media_core.ui import subtitles

    assert subtitles.shift_delay(engine, -subtitles.DELAY_STEP_MS) == "Subtitle delay -50 ms"
    assert subtitles.shift_delay(engine, 150) == "Subtitle delay 100 ms"
    assert engine._text_appsink.get_property("ts-offset") == 100 * Gst.MSECOND


def test_loading_a_video_finds_its_files_and_chooses_the_best(engine, tmp_path) -> None:
    video = _write(tmp_path / "Film.mkv", b"")
    _write(tmp_path / "Film.en.srt", SRT)
    _write(tmp_path / "Film.srt", SRT)
    _write(tmp_path / "Film.sub", b"\x00\x00\x01\xba" + bytes(64))  # VobSub: skipped
    engine.set_subtitle_delay(500)

    engine._load_external_subtitles(video)
    assert [label for _, label in engine.get_subtitle_tracks()] == ["Film.srt", "Film.en.srt"]
    assert engine.get_active_external().label == "Film.srt"
    assert engine.get_subtitle_delay() == 0  # a new file starts in sync


def test_an_unreadable_file_says_why(engine, tmp_path) -> None:
    from media_core.ui import subtitles

    bad = _write(tmp_path / "bad.srt", "not subtitles at all\n")
    assert subtitles.add_subtitle_paths(engine, [bad]).startswith("Couldn't read bad.srt")
    assert engine.get_subtitle_tracks() == []


# -- The overlay -------------------------------------------------------------


class _Engine:
    def __init__(self, sub, pos_ms: int, delay_ms: int = 0) -> None:
        self.sub, self.pos_ms, self.delay_ms = sub, pos_ms, delay_ms

    def get_subtitle_serial(self) -> int:
        return 0

    def get_subtitle_flags(self) -> bool:
        return True

    def get_position_ms(self) -> int:
        return self.pos_ms

    def get_subtitle_delay(self) -> int:
        return self.delay_ms

    def get_active_external(self):
        return self.sub

    def pull_ass_chunks(self) -> list:
        return []

    def pull_subtitle_text(self):
        return None


def test_a_file_is_drawn_at_the_position_less_the_delay(qapp, tmp_path) -> None:
    from media_core.ui.widgets.subtitle_overlay import SubtitleOverlay

    engine = _Engine(subtitle_files.load(_write(tmp_path / "Film.srt", SRT)), pos_ms=1200)
    overlay = SubtitleOverlay()
    overlay.start(engine, None)
    overlay._poll()
    assert overlay._current_text == "Hello\nthere"

    engine.delay_ms = 1000  # a second later: at 1.2 s, nothing yet
    overlay._poll()
    assert overlay._current_text == ""
    engine.pos_ms = 4200  # shows what was due at 3.2 s
    overlay._poll()
    assert overlay._current_text == "Again"  # tags stripped
    overlay.stop()


def test_an_ass_file_goes_to_libass_whole(qapp, tmp_path) -> None:
    from media_core.ui.widgets.subtitle_overlay import SubtitleOverlay

    class _Libass:
        available = True

        def __init__(self) -> None:
            self.script = None

        def load_script(self, script: bytes) -> bool:
            self.script = script
            return True

        def set_frame_size(self, w: int, h: int) -> None:
            pass

        def render_frame(self, now_ms: int) -> list:
            return [now_ms]

    ass = _write(tmp_path / "Film.ass", "[Script Info]\nScriptType: v4.00+\n")
    overlay = SubtitleOverlay()
    overlay.resize(640, 360)
    overlay._libass = _Libass()
    overlay.start(_Engine(subtitle_files.load(ass), pos_ms=4000, delay_ms=-500), None)
    overlay._poll()
    assert overlay._libass.script == ass.read_bytes()
    assert overlay._ass_bitmaps == [4500]  # rendered half a second ahead
    overlay.stop()


def test_messages_show_then_go(qapp) -> None:
    from media_core.ui.widgets.subtitle_overlay import SubtitleOverlay

    overlay = SubtitleOverlay()
    overlay.show_message("Subtitle track: Film.srt")
    assert overlay._osd_text == "Subtitle track: Film.srt"
    assert overlay._osd_timer.isActive()
    overlay._osd_timer.timeout.emit()
    assert overlay._osd_text == ""


# -- The menu ----------------------------------------------------------------


class _MenuEngine:
    def __init__(self, tracks, current) -> None:
        self._tracks, self._current = tracks, current

    def get_audio_tracks(self):
        return [(0, "Stereo")]

    def get_subtitle_tracks(self):
        return self._tracks

    def get_subtitle_track_support(self):
        return [True] * len(self._tracks)

    def get_current_subtitle(self):
        return self._current


def _submenu(menu, title):
    return next(a.menu() for a in menu.actions() if a.text() == title)


def test_the_subtitle_menu_is_vlcs(qapp) -> None:
    from PySide6.QtWidgets import QMenu

    from media_core.ui.widgets.queue_widget import build_stream_menus

    menu = QMenu()
    build_stream_menus(menu, _MenuEngine([(0, "English"), (1, "Film.srt")], current=1))
    subtitle = _submenu(menu, "Subtitle")
    assert [a.text() for a in subtitle.actions()] == ["Add Subtitle File…", "Sub Track"]
    tracks = _submenu(subtitle, "Sub Track")
    assert [(a.text(), a.isChecked()) for a in tracks.actions()] == [
        ("Disable", False), ("English", False), ("Film.srt", True)]
    assert tracks.actions()[0].actionGroup().isExclusive()


def test_add_subtitle_file_is_there_with_no_tracks(qapp) -> None:
    from PySide6.QtWidgets import QMenu

    from media_core.ui.widgets.queue_widget import build_stream_menus

    menu = QMenu()
    build_stream_menus(menu, _MenuEngine([], current=-1))
    subtitle = _submenu(menu, "Subtitle")
    assert subtitle.actions()[0].text() == "Add Subtitle File…"
    assert not _submenu(subtitle, "Sub Track").isEnabled()


# -- Drops on Minim ----------------------------------------------------------


def test_subtitle_files_dropped_on_a_video_become_its_tracks(engine, tmp_path) -> None:
    from minim.ui.main_window import MainWindow

    srt = _write(tmp_path / "Film.srt", SRT)
    shown: list[str] = []
    window = SimpleNamespace(
        _video_engine=lambda: engine,
        _nowplaying=SimpleNamespace(show_osd_message=shown.append),
    )
    rest = MainWindow._take_subtitle_paths(window, [str(srt), "/films/Next.mkv"])
    assert rest == ["/films/Next.mkv"]
    assert engine.get_active_external().path == srt
    assert shown == ["Subtitle track: Film.srt"]


def test_with_no_video_a_dropped_subtitle_file_is_left_alone(tmp_path) -> None:
    from minim.ui.main_window import MainWindow

    window = SimpleNamespace(_video_engine=lambda: None)
    paths = [str(tmp_path / "Film.srt")]
    assert MainWindow._take_subtitle_paths(window, paths) == paths


# -- Where Add Subtitle File opens -------------------------------------------


def test_the_dialog_opens_where_the_video_really_is(monkeypatch) -> None:
    """A NAS file reaches the Flatpak as a document-portal stand-in; the
    dialog should open in its real folder, which the portal knows."""
    import sys

    from media_core.ui import subtitles

    real = "/run/user/1000/kio-fuse-AbC/smb/nas/Movies/Film (1978)/Film.mp4"

    class _Documents:
        def GetHostPaths(self, ids):
            assert ids == ["dOc1D"]
            return {"dOc1D": [ord(c) for c in real] + [0]}

    fake_dbus = SimpleNamespace(
        SessionBus=lambda: SimpleNamespace(get_object=lambda *a: None),
        Interface=lambda obj, iface: _Documents(),
    )
    monkeypatch.setitem(sys.modules, "dbus", fake_dbus)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    stand_in = SimpleNamespace(current_path=Path("/run/user/1000/doc/dOc1D/Film.mp4"))
    assert subtitles.playing_folder(stand_in) == Path(real).parent
    local = SimpleNamespace(current_path=Path("/home/deck/Videos/Film.mkv"))
    assert subtitles.playing_folder(local) == Path("/home/deck/Videos")
    assert subtitles.playing_folder(SimpleNamespace(current_path=None)) is None
