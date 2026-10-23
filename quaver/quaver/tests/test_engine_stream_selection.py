"""Switching audio/subtitle tracks must not wedge the pipeline.

Two ways it used to freeze on the last frame with no recovery:

1. STREAMS_SELECTED was parsed as the whole collection, so every stream
   counted as "selected" and the next switch asked playsink for four audio
   tracks at once — "playsink pad already used", pipeline to NULL.
2. Selecting a stream with no installed decoder (E-AC3 on ffmpeg-free)
   left decodebin3 with nothing to link, hanging playback silently.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def engine():
    from media_core.core.audio import AudioEngine

    eng = AudioEngine()
    yield eng
    eng.shutdown()


def _collection(*specs):
    """Build a StreamCollection from (stream_id, caps, stream_type) triples."""
    from media_core.core.audio import Gst

    coll = Gst.StreamCollection.new("test")
    for stream_id, caps, stream_type in specs:
        coll.add_stream(
            Gst.Stream.new(stream_id, Gst.Caps.from_string(caps), stream_type,
                           Gst.StreamFlags.NONE)
        )
    return coll


def _video_audio_text():
    from media_core.core.audio import Gst

    return _collection(
        ("v0", "video/x-h265", Gst.StreamType.VIDEO),
        ("a0", "audio/mpeg, mpegversion=(int)4", Gst.StreamType.AUDIO),
        ("a1", "audio/x-ac3", Gst.StreamType.AUDIO),
        ("t0", "application/x-ass", Gst.StreamType.TEXT),
        ("t1", "application/x-ass", Gst.StreamType.TEXT),
    )


def test_switch_selects_one_stream_per_type(engine) -> None:
    """A switch must never request two streams of the same type."""
    from media_core.core.audio import Gst

    engine._stream_collection = _video_audio_text()
    # The bug: every stream in the collection recorded as selected.
    engine._selected_streams = {"v0", "a0", "a1", "t0", "t1"}

    sent: list[list[str]] = []
    engine._select_streams = sent.append

    engine.set_current_subtitle(1)
    assert sent, "no SELECT_STREAMS sent"
    by_type = {}
    for sid in sent[-1]:
        stream = next(s for s in engine._get_stream_collection()
                      if s.get_stream_id() == sid)
        by_type.setdefault(stream.get_stream_type(), []).append(sid)
    assert by_type[Gst.StreamType.TEXT] == ["t1"]
    assert len(by_type[Gst.StreamType.AUDIO]) == 1
    assert len(by_type[Gst.StreamType.VIDEO]) == 1


def test_switch_keeps_video_when_selection_unknown(engine) -> None:
    """A switch before STREAMS_SELECTED arrives must not drop video/audio."""
    from media_core.core.audio import Gst

    engine._stream_collection = _video_audio_text()
    engine._selected_streams = set()

    sent: list[list[str]] = []
    engine._select_streams = sent.append

    engine.set_current_subtitle(0)
    types = {
        next(s for s in engine._get_stream_collection()
             if s.get_stream_id() == sid).get_stream_type()
        for sid in sent[-1]
    }
    assert Gst.StreamType.VIDEO in types
    assert Gst.StreamType.AUDIO in types


def test_undecodable_track_is_refused(engine) -> None:
    """No decoder → refuse the switch and report, rather than hang."""
    from media_core.core.audio import Gst

    engine._stream_collection = _collection(
        ("v0", "video/x-h265", Gst.StreamType.VIDEO),
        ("a0", "audio/mpeg, mpegversion=(int)4", Gst.StreamType.AUDIO),
        ("a1", "audio/x-nonexistent-codec", Gst.StreamType.AUDIO),
    )
    engine._selected_streams = {"v0", "a0"}
    reported: list[str] = []
    engine.on_error = reported.append
    sent: list[list[str]] = []
    engine._select_streams = sent.append

    assert engine.get_audio_track_support() == [True, False]

    engine.set_current_audio(1)
    assert not sent, "switched to a track with no decoder"
    assert reported and "decoder" in reported[0]

    engine.set_current_audio(0)
    assert sent, "a decodable track must still switch"


def test_ass_subtitles_count_as_decodable(engine) -> None:
    """ASS passes through undecoded — it must not be greyed out as unplayable."""
    from media_core.core.audio import Gst

    engine._stream_collection = _collection(
        ("t0", "application/x-ass", Gst.StreamType.TEXT),
        ("t1", "subpicture/x-pgs", Gst.StreamType.TEXT),
    )
    assert engine.get_subtitle_track_support() == [True, True]


def test_subtitle_serial_changes_only_with_the_stream(engine) -> None:
    """The overlay resets on the serial; off and on again must not reset."""
    engine._stream_collection = _video_audio_text()
    engine._selected_streams = {"v0", "a0", "t0"}
    engine._sub_stream_id = "t0"  # playbin3's own pick, from STREAMS_SELECTED
    engine._select_streams = lambda ids: None
    serial = engine.get_subtitle_serial()

    engine.set_subtitle_enabled(False)
    engine.set_subtitle_enabled(True)  # back on: track 0 again
    engine.set_current_subtitle(0)
    assert engine.get_subtitle_serial() == serial

    engine.set_current_subtitle(1)
    assert engine.get_subtitle_serial() == serial + 1
