"""Embedded ASS/SSA subtitles must pass through decodebin3 undecoded.

ssaparse is de-ranked so ASS is rendered by libass, not flattened to
pango-markup.  Without ASS in uridecodebin3's raw caps, decodebin3 finds
no decoder, drops the subtitle stream, and races playsink into a silent
READY→PLAYING hang on any MKV with embedded ASS (most anime releases).
"""

from __future__ import annotations


def test_uridecodebin3_treats_ass_as_raw() -> None:
    from media_core.core.audio import AudioEngine, Gst

    engine = AudioEngine()
    try:
        caps = engine._playbin.get_by_name("uridecodebin3").get_property("caps")
        for mime in ("application/x-ass", "application/x-ssa"):
            assert caps.can_intersect(Gst.Caps.from_string(mime)), mime
        # Default raw caps must survive the extension.
        assert caps.can_intersect(Gst.Caps.from_string("video/x-raw"))
        assert caps.can_intersect(Gst.Caps.from_string("audio/x-raw"))
    finally:
        engine.shutdown()
