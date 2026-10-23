"""EQ band labels round, so the 1889 Hz band doesn't read "1k"."""

from media_core.ui.widgets.eq_popup import _BANDS, _band_label


def test_band_labels() -> None:
    assert [_band_label(f) for f in _BANDS] == [
        "29", "59", "119", "237", "474", "947", "1.9k", "3.8k", "7.5k", "15k",
    ]
