"""The duration shown doesn't jump ahead during a gapless switch.

Once about-to-finish queues the next file, playbin3 answers duration
queries with the next track's length while this one still plays out, so
the time label read e.g. 2:44 / 3:12 for about a second.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def engine():
    from media_core.core.audio import AudioEngine

    eng = AudioEngine()
    yield eng
    eng.shutdown()


def test_duration_holds_until_the_next_track_starts(engine) -> None:
    from media_core.core.audio import Gst

    class _Playbin:
        dur_ms = 165_000

        def query_duration(self, fmt):
            return True, self.dur_ms * Gst.MSECOND

    real, fake = engine._playbin, _Playbin()
    engine._playbin = fake
    try:
        assert engine.get_duration_ms() == 165_000
        engine._gapless_pending = True  # about-to-finish queued the next file
        fake.dur_ms = 192_000  # ...and playbin3 already reports its length
        assert engine.get_duration_ms() == 165_000
        engine._gapless_pending = False  # STREAM_START: the next track plays
        assert engine.get_duration_ms() == 192_000
    finally:
        engine._playbin = real
