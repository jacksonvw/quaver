"""Filename → (season, episode, title) parsing."""

from pathlib import Path

from quaver.core.video_parser import (
    is_extras_path,
    minimal_clean,
    parse_series_path,
)


def test_parse_sxxexx() -> None:
    r = parse_series_path(Path("Show.S01E03.mkv"))
    assert r is not None
    assert r.disc_number == 1
    assert r.track_number == 3


def test_parse_lowercase_sxe() -> None:
    r = parse_series_path(Path("Show.s1e3.mkv"))
    assert r is not None
    assert r.disc_number == 1
    assert r.track_number == 3


def test_parse_xxx_format() -> None:
    r = parse_series_path(Path("Show 1x03.mkv"))
    assert r is not None
    assert r.disc_number == 1
    assert r.track_number == 3


def test_parse_three_digit_format() -> None:
    r = parse_series_path(Path("Show.103.mkv"))
    assert r is not None
    assert r.disc_number == 1
    assert r.track_number == 3


def test_parse_ep_pattern_anime() -> None:
    # FLCL-style "EP01" should resolve to episode 1.
    r = parse_series_path(Path("FLCL/FLCL.EP01.mkv"))
    assert r is not None
    assert r.track_number == 1


def test_extras_path_detection() -> None:
    assert is_extras_path(Path("Show/Behind The Scenes/x.mkv")) is True
    assert is_extras_path(Path("Show/Featurettes/y.mkv")) is True
    assert is_extras_path(Path("Show/Season 01/ep.mkv")) is False


def test_minimal_clean_extracts_year() -> None:
    title, year = minimal_clean("Show.Name.2010.1080p.x264-RARBG")
    assert title == "Show Name"
    assert year == 2010


def test_minimal_clean_strips_identifier() -> None:
    title, year = minimal_clean("Show Name (2018) [tvdb-12345]")
    assert title == "Show Name"
    assert year == 2018
