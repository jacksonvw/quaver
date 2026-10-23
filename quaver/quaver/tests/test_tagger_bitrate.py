"""Formats whose headers carry no bitrate (Opus) get an average one."""

from types import SimpleNamespace

from media_core.core import tagger


class _Opus(dict):
    """What mutagen.File(easy=True) gives for an Opus file: no info.bitrate."""

    info = SimpleNamespace(length=10.0)
    tags = None


class _Comments:
    def write(self) -> bytes:  # the Vorbis comment block, cover and all
        return b"\0" * 25_000


def test_missing_bitrate_is_averaged_over_the_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "song.opus"
    path.write_bytes(b"\0" * 125_000)
    monkeypatch.setattr(tagger.mutagen, "File", lambda p, easy=True: _Opus(title=["Song"]))
    tags = tagger.read_tags(path)
    assert tags.bitrate == 100_000  # 125 kB × 8 over 10 s
    assert tags.title == "Song"


def test_tags_are_left_out_of_the_average(tmp_path, monkeypatch) -> None:
    path = tmp_path / "song.opus"
    path.write_bytes(b"\0" * 125_000)
    audio = _Opus(title=["Song"])
    audio.tags = _Comments()
    monkeypatch.setattr(tagger.mutagen, "File", lambda p, easy=True: audio)
    assert tagger.read_tags(path).bitrate == 80_000  # (125 - 25) kB × 8 over 10 s
