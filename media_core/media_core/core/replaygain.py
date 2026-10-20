"""Audio filter chain: ReplayGain + Equalizer + Compressor + Spectrum via GStreamer.

No PySide6 imports — must be testable without a display.
"""

from __future__ import annotations

from enum import Enum, auto

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


class RGMode(Enum):
    OFF = auto()
    TRACK = auto()
    ALBUM = auto()


class AudioFilters:
    """Container for all audio filter elements in the chain."""
    __slots__ = ("rgvolume", "equalizer", "compressor", "spectrum")

    def __init__(self, rgvolume, equalizer, compressor, spectrum):
        self.rgvolume = rgvolume
        self.equalizer = equalizer
        self.compressor = compressor
        self.spectrum = spectrum


def insert_audio_filters(playbin: Gst.Element) -> AudioFilters:
    """Create an audio filter bin and attach it to *playbin*.

    Chain: rgvolume → rglimiter → equalizer → compressor → spectrum.
    Any element may be None if its GStreamer plugin is missing.
    """
    rgvolume = Gst.ElementFactory.make("rgvolume", "rgvolume")
    rglimiter = Gst.ElementFactory.make("rglimiter", "rglimiter")
    equalizer = Gst.ElementFactory.make("equalizer-10bands", "equalizer")
    compressor = Gst.ElementFactory.make("audiodynamic", "compressor")
    spectrum = Gst.ElementFactory.make("spectrum", "spectrum")

    if spectrum:
        spectrum.set_property("bands", 32)
        spectrum.set_property("interval", 50_000_000)  # 50 ms
        spectrum.set_property("threshold", -80)
        spectrum.set_property("post-messages", True)
        spectrum.set_property("message-magnitude", True)

    if compressor:
        # Start with compressor in passthrough (ratio 1:1).
        compressor.set_property("mode", "compressor")
        compressor.set_property("characteristics", "soft-knee")
        compressor.set_property("ratio", 1.0)  # Disabled by default.
        compressor.set_property("threshold", 1.0)

    audio_bin = Gst.Bin.new("audio-filter-bin")

    # Build chain: rgvolume → rglimiter → equalizer → compressor → spectrum.
    elements = []
    if rgvolume:
        elements.append(rgvolume)
    if rglimiter:
        elements.append(rglimiter)
    if equalizer:
        elements.append(equalizer)
    if compressor:
        elements.append(compressor)
    if spectrum:
        elements.append(spectrum)

    if not elements:
        return AudioFilters(None, None, None, None)

    for el in elements:
        audio_bin.add(el)
    for i in range(len(elements) - 1):
        elements[i].link(elements[i + 1])

    # Ghost pads.
    sink_pad = elements[0].get_static_pad("sink")
    audio_bin.add_pad(Gst.GhostPad.new("sink", sink_pad))
    src_pad = elements[-1].get_static_pad("src")
    audio_bin.add_pad(Gst.GhostPad.new("src", src_pad))

    playbin.set_property("audio-filter", audio_bin)
    return AudioFilters(rgvolume, equalizer, compressor, spectrum)


# Keep old function name as alias for compatibility.
def insert_rgvolume(playbin: Gst.Element) -> Gst.Element | None:
    filters = insert_audio_filters(playbin)
    return filters.rgvolume


def apply_rg_mode(rgvolume: Gst.Element | None, mode: RGMode) -> None:
    """Configure the rgvolume element for the given mode."""
    if rgvolume is None:
        return
    if mode == RGMode.OFF:
        rgvolume.set_property("pre-amp", 0.0)
        rgvolume.set_property("fallback-gain", 0.0)
    elif mode == RGMode.TRACK:
        rgvolume.set_property("album-mode", False)
        rgvolume.set_property("pre-amp", 0.0)
        rgvolume.set_property("fallback-gain", -6.0)
    elif mode == RGMode.ALBUM:
        rgvolume.set_property("album-mode", True)
        rgvolume.set_property("pre-amp", 0.0)
        rgvolume.set_property("fallback-gain", -6.0)


def apply_eq_bands(equalizer: Gst.Element | None, gains: list[float]) -> None:
    """Set equalizer band gains. *gains* is a list of 10 float values in dB."""
    if equalizer is None:
        return
    for i, gain in enumerate(gains[:10]):
        equalizer.set_property(f"band{i}", float(gain))


def apply_compressor(
    compressor: Gst.Element | None,
    enabled: bool,
    ratio: float = 4.0,
    threshold: float = 0.5,
) -> None:
    """Configure the dynamic range compressor.

    *ratio*: compression ratio (1.0 = no compression, 10.0 = heavy).
    *threshold*: level above which compression kicks in (0.0–1.0).
    """
    if compressor is None:
        return
    if enabled:
        compressor.set_property("ratio", max(1.0, min(20.0, ratio)))
        compressor.set_property("threshold", max(0.0, min(1.0, threshold)))
    else:
        compressor.set_property("ratio", 1.0)
        compressor.set_property("threshold", 1.0)
