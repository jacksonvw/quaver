"""GStreamer playbin3 media engine.

Handles both audio and video playback.  Video frames are delivered via
appsink — the UI pulls decoded RGBA frames and renders them with QPainter.
Audio goes through the filter chain (EQ, spectrum, compressor) as before.

All public methods are safe to call from any thread. The engine uses
GStreamer's own main loop for pipeline events and communicates state
changes via callbacks that the caller registers.

No PySide6 imports — this module must be testable without a display.
"""

from __future__ import annotations

import collections
import logging
import threading
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp  # noqa: F401 — GstApp import activates appsink methods

Gst.init(None)

log = logging.getLogger(__name__)

# De-rank ssaparse so ASS/SSA is never decoded to pango-markup (which loses
# all positioning, font, color, and drawing information).  We render ASS
# natively via libass.  De-ranking alone does NOT deliver raw ASS to the
# text appsink — see _setup_text_sink(), which marks ASS/SSA as raw caps.
_ssaparse_factory = Gst.ElementFactory.find("ssaparse")
if _ssaparse_factory:
    _ssaparse_factory.set_rank(Gst.Rank.NONE)
    log.info("De-ranked ssaparse — raw ASS data will reach text appsink")


class PlaybackState(Enum):
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


class AudioEngine:
    """Wraps a GStreamer playbin3 element for media playback.

    Video frames are delivered via appsink as RGBA data — the UI pulls
    frames with ``pull_video_frame()`` and renders them with QPainter.
    No X11 window handles, no ximagesink, no separate windows.

    Gapless playback is achieved by connecting to the about-to-finish
    signal and setting the next URI there — no stop/play cycle.
    """

    def __init__(self) -> None:
        self._playbin = Gst.ElementFactory.make("playbin3", "playbin")
        if self._playbin is None:
            raise RuntimeError(
                "Could not create playbin3. Is gstreamer1-plugins-base installed?"
            )

        # Video sink: videoconvert → appsink (delivers RGBA frames).
        # Always present — unpulled frames are silently dropped.
        # For audio-only content there's no video stream, so zero cost.
        self._video_appsink: Gst.Element | None = None
        self._latest_video_frame: tuple[bytes, int, int] | None = None
        self._setup_video_sink()

        # Text sink: appsink for subtitle data.
        # Dual-path: raw ASS/SSA → _ass_chunks deque (fed to libass by overlay),
        #            text/x-raw → _subtitle_queue (rendered by QPainter).
        self._text_appsink: Gst.Element | None = None
        self._subtitle_queue: collections.deque = collections.deque(maxlen=64)
        self._ass_chunks: collections.deque = collections.deque(maxlen=256)
        self._ass_header: bytes | None = None  # ASS codec_data (styles/script info)
        self._ass_header_extracted = False
        self._subtitles_enabled: bool = True
        self._current_sub_index: int = -1  # local tracking (authoritative)
        self._sub_serial = 0  # bumped when the subtitle stream changes
        self._sub_stream_id: str | None = None  # the stream it was bumped for
        self._setup_text_sink()

        self._state = PlaybackState.STOPPED
        self._current_uri: str | None = None
        self._playback_rate: float = 1.0

        # Callbacks — set by the consumer (e.g. the UI bridge).
        self.on_state_changed: Callable[[PlaybackState], None] | None = None
        self.on_position_updated: Callable[[int, int], None] | None = None
        self.on_track_finished: Callable[[], None] | None = None
        self.on_about_to_finish: Callable[[], None] | None = None
        self.on_gapless_transition: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        # Next URI for gapless — set by the consumer before (or in)
        # the about-to-finish callback.
        self._next_uri: str | None = None
        self._gapless_pending = False
        self._last_duration_ms = 0  # this track's, held through a gapless switch

        # playbin3 stream tracking.
        self._stream_collection: Gst.StreamCollection | None = None
        self._selected_streams: set[str] = set()

        self._playbin.connect("about-to-finish", self._on_about_to_finish)

        # Bus watch for EOS, errors, state changes.
        bus = self._playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # GLib main loop in a daemon thread for signal dispatching.
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, daemon=True, name="gst-mainloop"
        )
        self._loop_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def current_uri(self) -> str | None:
        return self._current_uri

    def play_file(self, path: str | Path) -> None:
        """Start playback of *path* (a local file or http/https URI)."""
        s = str(path)
        if s.startswith(("http://", "https://")):
            self.play_uri(s)
            return
        uri = self._path_to_uri(path)
        self._stop_pipeline()
        self._current_uri = uri
        self._playbin.set_property("uri", uri)
        # Auto-detect external subtitle file (e.g. video.srt beside video.mkv).
        sub = self._find_external_subtitle(Path(path))
        self._playbin.set_property("suburi", sub.as_uri() if sub else None)
        self._playbin.set_state(Gst.State.PLAYING)
        self._set_state(PlaybackState.PLAYING)

    def play_uri(self, uri: str) -> None:
        """Start playback of an arbitrary URI."""
        self._stop_pipeline()
        self._current_uri = uri
        self._playbin.set_property("uri", uri)
        self._playbin.set_property("suburi", None)  # No external subs for URIs.
        self._playbin.set_state(Gst.State.PLAYING)
        self._set_state(PlaybackState.PLAYING)

    def pause(self) -> None:
        if self._state == PlaybackState.PLAYING:
            self._playbin.set_state(Gst.State.PAUSED)
            self._set_state(PlaybackState.PAUSED)

    def resume(self) -> None:
        if self._state == PlaybackState.PAUSED:
            self._playbin.set_state(Gst.State.PLAYING)
            self._set_state(PlaybackState.PLAYING)

    def toggle_pause(self) -> None:
        if self._state == PlaybackState.PLAYING:
            self.pause()
        elif self._state == PlaybackState.PAUSED:
            self.resume()

    def stop(self) -> None:
        self._stop_pipeline()
        self._set_state(PlaybackState.STOPPED)

    def seek(self, position_ms: int) -> None:
        """Seek to *position_ms* milliseconds from the start."""
        self._playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            position_ms * Gst.MSECOND,
        )
        self.flush_subtitle_text()

    def set_volume(self, level: float) -> None:
        """Set volume as a linear float from 0.0 to 1.0."""
        self._playbin.set_property("volume", max(0.0, min(1.0, level)))

    def get_volume(self) -> float:
        return self._playbin.get_property("volume")

    def get_position_ms(self) -> int:
        """Return current playback position in milliseconds, or 0."""
        ok, pos = self._playbin.query_position(Gst.Format.TIME)
        return pos // Gst.MSECOND if ok else 0

    def get_duration_ms(self) -> int:
        """Return track duration in milliseconds, or 0."""
        # Once about-to-finish queues the next file, playbin3 answers with
        # that file's duration while this one is still playing out.
        if self._gapless_pending and self._last_duration_ms:
            return self._last_duration_ms
        ok, dur = self._playbin.query_duration(Gst.Format.TIME)
        self._last_duration_ms = dur // Gst.MSECOND if ok else 0
        return self._last_duration_ms

    def set_playback_rate(self, rate: float) -> None:
        """Set playback speed (0.5–2.0). Uses GStreamer seek with rate."""
        rate = max(0.5, min(2.0, rate))
        self._playback_rate = rate
        if self._state == PlaybackState.STOPPED:
            return
        pos = self.get_position_ms()
        self._playbin.seek(
            rate,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            pos * Gst.MSECOND,
            Gst.SeekType.NONE,
            0,
        )
        self.flush_subtitle_text()

    def get_playback_rate(self) -> float:
        return self._playback_rate

    # -- Stream selection (audio tracks, subtitles) -----------------------

    # -- Stream collection (playbin3 API) ------------------------------------

    def _get_stream_collection(self) -> list:
        """Return the current GstStreamCollection's streams, or []."""
        if not hasattr(self, "_stream_collection") or self._stream_collection is None:
            return []
        coll = self._stream_collection
        return [coll.get_stream(i) for i in range(coll.get_size())]

    def _streams_by_type(self, stream_type) -> list:
        return [s for s in self._get_stream_collection()
                if s.get_stream_type() == stream_type]

    def _select_streams(self, stream_ids: list[str]) -> None:
        """Send a SELECT_STREAMS event to playbin3.

        This is the correct playbin3 API — NOT emit("select-streams").
        The event propagates upstream and playbin3 reconfigures decoders.
        """
        event = Gst.Event.new_select_streams(stream_ids)
        ok = self._playbin.send_event(event)
        if not ok:
            log.warning("SELECT_STREAMS event not handled: %s", stream_ids)

    def _can_decode(self, stream) -> bool:
        """True if this installation can handle *stream*'s caps.

        Selecting a stream with no decoder (E-AC3 on Fedora's ffmpeg-free,
        say) leaves decodebin3 with nothing to link, and the pipeline hangs
        on its last frame with no error — and stays hung after switching
        back.  Callers check this before offering or performing a switch.
        """
        caps = stream.get_caps()
        if caps is None:
            return True  # Unknown — let GStreamer try.
        uridecodebin = self._playbin.get_by_name("uridecodebin3")
        raw_caps = uridecodebin.get_property("caps") if uridecodebin else None
        if raw_caps is not None and raw_caps.can_intersect(caps):
            return True  # Passed through undecoded (raw audio, ASS, PGS…).
        decoders = Gst.ElementFactory.list_get_elements(
            Gst.ELEMENT_FACTORY_TYPE_DECODER, Gst.Rank.MARGINAL
        )
        return bool(
            Gst.ElementFactory.list_filter(decoders, caps, Gst.PadDirection.SINK, False)
        )

    def _decodable(self, streams: list) -> list[bool]:
        return [self._can_decode(s) for s in streams]

    def get_audio_track_support(self) -> list[bool]:
        """Per-audio-track decodability, index-aligned with get_audio_tracks()."""
        return self._decodable(self._streams_by_type(Gst.StreamType.AUDIO))

    def get_subtitle_track_support(self) -> list[bool]:
        """Per-subtitle-track decodability, index-aligned with get_subtitle_tracks()."""
        return self._decodable(self._streams_by_type(Gst.StreamType.TEXT))

    def _reject_undecodable(self, stream, label: str) -> bool:
        """Report and refuse a switch to an undecodable stream."""
        if self._can_decode(stream):
            return False
        caps = stream.get_caps()
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else "unknown"
        log.warning("No decoder for %s stream (%s) — refusing switch", label, name)
        if self.on_error:
            self.on_error(f"No decoder installed for this {label} track ({name})")
        return True

    def _replace_selection(self, stream_type, new_id: str) -> None:
        """Select *new_id* for *stream_type*, keeping one stream of each other type.

        playsink has a single pad per type, so a SELECT_STREAMS carrying two
        audio (or text) streams errors with "playsink pad already used" and
        the pipeline stops on its last frame.  If the current selection is
        unknown for a type (e.g. a new collection arrived but STREAMS_SELECTED
        has not yet), fall back to that type's first stream rather than
        silently deselecting video or audio.
        """
        selected = [new_id]
        for other in (Gst.StreamType.VIDEO, Gst.StreamType.AUDIO, Gst.StreamType.TEXT):
            if other == stream_type:
                continue
            streams = self._streams_by_type(other)
            current = [s.get_stream_id() for s in streams
                       if s.get_stream_id() in self._selected_streams]
            if current:
                selected.append(current[0])
            elif streams and other != Gst.StreamType.TEXT:
                selected.append(streams[0].get_stream_id())
        self._selected_streams = set(selected)
        self._select_streams(selected)

    def get_audio_tracks(self) -> list[tuple[int, str]]:
        """Return [(index, language/label)] for available audio streams."""
        tracks = []
        for i, stream in enumerate(self._streams_by_type(Gst.StreamType.AUDIO)):
            label = self._stream_label(stream, f"Track {i + 1}")
            tracks.append((i, label))
        return self._deduplicate_labels(tracks)

    def get_current_audio(self) -> int:
        """Return the current audio stream index."""
        audio_streams = self._streams_by_type(Gst.StreamType.AUDIO)
        for i, s in enumerate(audio_streams):
            if s.get_stream_id() in self._selected_streams:
                return i
        return 0

    def set_current_audio(self, index: int) -> None:
        """Switch to audio stream *index*."""
        audio_streams = self._streams_by_type(Gst.StreamType.AUDIO)
        if 0 <= index < len(audio_streams):
            if self._reject_undecodable(audio_streams[index], "audio"):
                return
            self._replace_selection(
                Gst.StreamType.AUDIO, audio_streams[index].get_stream_id()
            )

    def get_subtitle_tracks(self) -> list[tuple[int, str]]:
        """Return [(index, language/label)] for available subtitle streams."""
        tracks = []
        for i, stream in enumerate(self._streams_by_type(Gst.StreamType.TEXT)):
            label = self._stream_label(stream, f"Subtitle {i + 1}")
            tracks.append((i, label))
        return self._deduplicate_labels(tracks)

    def get_current_subtitle(self) -> int:
        """Return the current subtitle stream index (-1 = off)."""
        if not self._subtitles_enabled:
            return -1
        if self._current_sub_index < 0:
            # None chosen yet: report the stream playbin3 picked itself.
            for i, s in enumerate(self._streams_by_type(Gst.StreamType.TEXT)):
                if s.get_stream_id() in self._selected_streams:
                    return i
        return self._current_sub_index

    def get_subtitle_serial(self) -> int:
        """A number that changes when a different subtitle stream is chosen.

        The overlay compares it between polls to drop a replaced stream's
        rendered state.  Turning subtitles off and on again, or choosing
        the stream playbin3 picked itself, doesn't change it.
        """
        return self._sub_serial

    def set_current_subtitle(self, index: int) -> None:
        """Switch to subtitle stream *index*.  Implicitly enables subtitles."""
        text_streams = self._streams_by_type(Gst.StreamType.TEXT)
        if 0 <= index < len(text_streams) and self._reject_undecodable(
            text_streams[index], "subtitle"
        ):
            return
        self._subtitles_enabled = True
        self._current_sub_index = index
        if 0 <= index < len(text_streams):
            stream_id = text_streams[index].get_stream_id()
            if stream_id != self._sub_stream_id:
                self._sub_stream_id = stream_id
                self._sub_serial += 1
        # Reset ASS state for the new track.
        self._ass_header = None
        self._ass_header_extracted = False
        self._ass_chunks.clear()
        self._subtitle_queue.clear()
        # Send SELECT_STREAMS to playbin3.
        if 0 <= index < len(text_streams):
            self._replace_selection(
                Gst.StreamType.TEXT, text_streams[index].get_stream_id()
            )

    def get_subtitle_flags(self) -> bool:
        """Return True if subtitles are enabled."""
        return self._subtitles_enabled

    def set_subtitle_enabled(self, enabled: bool) -> None:
        """Enable or disable subtitle rendering."""
        self._subtitles_enabled = enabled
        if not enabled:
            self._current_sub_index = -1
            self._subtitle_queue.clear()
            self._ass_chunks.clear()
        else:
            if self._current_sub_index < 0:
                text_streams = self._streams_by_type(Gst.StreamType.TEXT)
                if text_streams:
                    self.set_current_subtitle(0)

    # Common ISO 639-1/2 codes → human-readable names.  GStreamer's
    # language-name tag is often missing; this table prevents bare "en"
    # or "ja" from appearing in stream selection menus.
    _LANG_NAMES: dict[str, str] = {
        "aa": "Afar", "ab": "Abkhazian", "af": "Afrikaans", "am": "Amharic",
        "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
        "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian",
        "bn": "Bengali", "bo": "Tibetan", "br": "Breton", "bs": "Bosnian",
        "ca": "Catalan", "ce": "Chechen", "ch": "Chamorro", "co": "Corsican",
        "cs": "Czech", "cy": "Welsh",
        "da": "Danish", "de": "German", "dz": "Dzongkha",
        "el": "Greek", "en": "English", "eo": "Esperanto", "es": "Spanish",
        "et": "Estonian", "eu": "Basque",
        "fa": "Persian", "fi": "Finnish", "fj": "Fijian", "fo": "Faroese",
        "fr": "French", "fy": "Western Frisian",
        "ga": "Irish", "gd": "Scottish Gaelic", "gl": "Galician",
        "gn": "Guarani", "gu": "Gujarati",
        "ha": "Hausa", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian",
        "hu": "Hungarian", "hy": "Armenian",
        "id": "Indonesian", "ig": "Igbo", "is": "Icelandic", "it": "Italian",
        "ja": "Japanese", "jv": "Javanese",
        "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "kn": "Kannada",
        "ko": "Korean", "ku": "Kurdish", "ky": "Kyrgyz",
        "la": "Latin", "lb": "Luxembourgish", "lo": "Lao", "lt": "Lithuanian",
        "lv": "Latvian",
        "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian", "ml": "Malayalam",
        "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
        "my": "Burmese",
        "nb": "Norwegian Bokmål", "ne": "Nepali", "nl": "Dutch",
        "nn": "Norwegian Nynorsk", "no": "Norwegian",
        "oc": "Occitan", "or": "Odia",
        "pa": "Punjabi", "pl": "Polish", "ps": "Pashto", "pt": "Portuguese",
        "qu": "Quechua",
        "rm": "Romansh", "ro": "Romanian", "ru": "Russian", "rw": "Kinyarwanda",
        "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala", "sk": "Slovak",
        "sl": "Slovenian", "sm": "Samoan", "sn": "Shona", "so": "Somali",
        "sq": "Albanian", "sr": "Serbian", "ss": "Swati", "st": "Southern Sotho",
        "su": "Sundanese", "sv": "Swedish", "sw": "Swahili",
        "ta": "Tamil", "te": "Telugu", "tg": "Tajik", "th": "Thai",
        "ti": "Tigrinya", "tk": "Turkmen", "tl": "Tagalog", "tn": "Tswana",
        "to": "Tongan", "tr": "Turkish", "ts": "Tsonga", "tt": "Tatar",
        "tw": "Twi",
        "ug": "Uyghur", "uk": "Ukrainian", "un": "Undetermined", "ur": "Urdu",
        "uz": "Uzbek",
        "vi": "Vietnamese", "vo": "Volapük",
        "wo": "Wolof",
        "xh": "Xhosa",
        "yi": "Yiddish", "yo": "Yoruba",
        "za": "Zhuang", "zh": "Chinese", "zu": "Zulu",
        # ISO 639-2 three-letter codes (common in MKV/Matroska).
        "ara": "Arabic", "bul": "Bulgarian", "cat": "Catalan",
        "ces": "Czech", "cze": "Czech",
        "chi": "Chinese", "zho": "Chinese",
        "dan": "Danish", "deu": "German", "ger": "German",
        "dut": "Dutch", "nld": "Dutch",
        "ell": "Greek", "gre": "Greek",
        "eng": "English", "epo": "Esperanto", "est": "Estonian",
        "eus": "Basque", "baq": "Basque",
        "fas": "Persian", "per": "Persian",
        "fin": "Finnish", "fra": "French", "fre": "French",
        "gle": "Irish", "glg": "Galician",
        "heb": "Hebrew", "hin": "Hindi", "hrv": "Croatian",
        "hun": "Hungarian", "hye": "Armenian", "arm": "Armenian",
        "ind": "Indonesian", "isl": "Icelandic", "ice": "Icelandic",
        "ita": "Italian",
        "jpn": "Japanese", "jav": "Javanese",
        "kat": "Georgian", "geo": "Georgian",
        "kaz": "Kazakh", "khm": "Khmer", "kor": "Korean", "kur": "Kurdish",
        "lao": "Lao", "lat": "Latin", "lav": "Latvian", "lit": "Lithuanian",
        "mal": "Malayalam", "mar": "Marathi", "mkd": "Macedonian",
        "mac": "Macedonian", "mon": "Mongolian", "msa": "Malay",
        "may": "Malay", "mya": "Burmese", "bur": "Burmese",
        "nep": "Nepali", "nor": "Norwegian", "nob": "Norwegian Bokmål",
        "nno": "Norwegian Nynorsk",
        "pan": "Punjabi", "pol": "Polish", "por": "Portuguese",
        "ron": "Romanian", "rum": "Romanian", "rus": "Russian",
        "sin": "Sinhala", "slk": "Slovak", "slo": "Slovak",
        "slv": "Slovenian", "som": "Somali", "spa": "Spanish",
        "sqi": "Albanian", "alb": "Albanian", "srp": "Serbian",
        "swe": "Swedish", "swa": "Swahili",
        "tam": "Tamil", "tel": "Telugu", "tgl": "Tagalog", "tha": "Thai",
        "tur": "Turkish",
        "ukr": "Ukrainian", "urd": "Urdu", "uzb": "Uzbek",
        "vie": "Vietnamese",
        "yid": "Yiddish",
        "und": "Undetermined",
    }

    @staticmethod
    def _stream_label(stream, fallback: str) -> str:
        """Build a human-readable label from a GstStream's tags.

        Composes all available info rather than returning the first match:
        ``"Title (Language)"`` when both exist, bare language or title
        when only one is set, resolved ISO code when only a code exists.
        """
        tags = stream.get_tags()
        if not tags:
            log.debug("Stream %s: no tags", fallback)
            return fallback

        # Log ALL available tags so we can debug label issues.  As one
        # string: get_string on a tag that isn't one (a bitrate, say)
        # makes GLib print a g_value_get_string assertion to stderr.
        log.debug("Stream %s tags: %s", fallback, tags.to_string())

        # Gather what we have.
        ok, title = tags.get_string("title")
        title = title.strip() if ok and title else ""

        ok, lang_name = tags.get_string("language-name")
        lang_name = lang_name.strip() if ok and lang_name else ""

        ok, lang_code = tags.get_string("language-code")
        lang_code = lang_code.strip() if ok and lang_code else ""

        # Resolve bare ISO code to a readable name if language-name is missing.
        language = lang_name
        if not language and lang_code:
            language = AudioEngine._LANG_NAMES.get(lang_code, lang_code)

        # Compose: prefer "Title (Language)", fall back gracefully.
        if title and language:
            # Avoid redundancy — if title already contains the language name
            # (e.g. title="English SDH"), don't append "(English)".
            if language.lower() in title.lower():
                return title
            return f"{title} ({language})"
        if title:
            return title
        if language:
            return language
        return fallback

    @staticmethod
    def _deduplicate_labels(tracks: list[tuple[int, str]]) -> list[tuple[int, str]]:
        """Append '#N' suffixes when multiple tracks share the same label."""
        counts: dict[str, int] = {}
        for _, label in tracks:
            counts[label] = counts.get(label, 0) + 1
        if not any(c > 1 for c in counts.values()):
            return tracks
        seen: dict[str, int] = {}
        result = []
        for idx, label in tracks:
            if counts[label] > 1:
                seen[label] = seen.get(label, 0) + 1
                result.append((idx, f"{label} #{seen[label]}"))
            else:
                result.append((idx, label))
        return result

    def set_next_uri(self, path: str | Path | None) -> None:
        """Queue the next track for gapless playback.

        Call this at any time — the value is consumed when GStreamer
        fires about-to-finish.  Also safe to call *inside* the
        on_about_to_finish callback.
        """
        if path is None:
            self._next_uri = None
        else:
            s = str(path)
            self._next_uri = s if s.startswith(("http://", "https://")) else self._path_to_uri(path)

    def shutdown(self) -> None:
        """Release all GStreamer resources."""
        self._stop_pipeline()
        self._loop.quit()

    # ------------------------------------------------------------------
    # Video frame delivery (appsink)
    # ------------------------------------------------------------------

    def _setup_video_sink(self) -> None:
        """Create the appsink video pipeline: videoconvert → appsink.

        Called once from __init__.  The appsink delivers decoded RGBA
        frames that the UI pulls via ``pull_video_frame()``.
        """
        vconv = Gst.ElementFactory.make("videoconvert", "vconv")
        appsink = Gst.ElementFactory.make("appsink", "vappsink")

        if vconv and appsink:
            caps = Gst.Caps.from_string("video/x-raw,format=RGBA")
            appsink.set_property("caps", caps)
            appsink.set_property("emit-signals", True)
            appsink.set_property("drop", True)
            appsink.set_property("max-buffers", 2)
            appsink.connect("new-sample", self._on_new_video_sample)

            video_bin = Gst.Bin.new("video-sink-bin")
            video_bin.add(vconv)
            video_bin.add(appsink)
            vconv.link(appsink)

            ghost = Gst.GhostPad.new("sink", vconv.get_static_pad("sink"))
            video_bin.add_pad(ghost)

            self._playbin.set_property("video-sink", video_bin)
            self._video_appsink = appsink
            log.info("Video sink: appsink (RGBA frame delivery)")
        else:
            # Fallback: discard video frames.
            fakesink = Gst.ElementFactory.make("fakesink", "fakevideo")
            if fakesink:
                self._playbin.set_property("video-sink", fakesink)
            log.warning("Could not create appsink video pipeline, using fakesink")

    def _setup_text_sink(self) -> None:
        """Create a text appsink for subtitle data.

        No caps filter — accepts both raw ASS/SSA (application/x-ass) and
        decoded text (text/x-raw).  The callback routes each format to the
        appropriate deque: raw ASS → _ass_chunks (for libass), text → _subtitle_queue.
        """
        appsink = Gst.ElementFactory.make("appsink", "tappsink")
        if appsink:
            appsink.set_property("emit-signals", True)
            appsink.set_property("drop", False)   # Every subtitle matters
            appsink.set_property("max-buffers", 16)
            appsink.set_property("sync", True)     # Hold until PTS
            appsink.connect("new-sample", self._on_new_subtitle_sample)

            text_bin = Gst.Bin.new("text-sink-bin")
            text_bin.add(appsink)
            ghost = Gst.GhostPad.new("sink", appsink.get_static_pad("sink"))
            text_bin.add_pad(ghost)

            self._playbin.set_property("text-sink", text_bin)
            self._text_appsink = appsink
            self._pass_through_ass()
            log.info("Text sink: appsink (dual-path: raw ASS + decoded text)")
        else:
            log.warning("Could not create text appsink for subtitles")

    def _pass_through_ass(self) -> None:
        """Make decodebin3 stop at ASS/SSA instead of looking for a decoder.

        decodebin3 only emits streams whose caps are "raw"; application/x-ass
        is not in its default raw set.  With ssaparse de-ranked it finds no
        decoder, posts missing-plugin and drops the subtitle stream — which
        (a) means embedded ASS never reaches libass, and (b) races playsink:
        if playsink reconfigures before the text pad is released,
        streamsynchronizer waits forever for the dropped text stream and
        the pipeline hangs in READY→PLAYING with no error.  Affects any MKV
        with embedded ASS/SSA subtitles.
        """
        uridecodebin = self._playbin.get_by_name("uridecodebin3")
        if uridecodebin is None:
            log.warning("uridecodebin3 not found — embedded ASS subtitles may stall playback")
            return
        caps = uridecodebin.get_property("caps").copy()
        caps.append(Gst.Caps.from_string("application/x-ass; application/x-ssa"))
        uridecodebin.set_property("caps", caps)

    # -- GStreamer new-sample callbacks (run on streaming thread) ------
    # These copy buffer data while it's guaranteed valid, avoiding the
    # race where a QTimer poll reads memory freed during gapless switch.

    def _on_new_video_sample(self, sink) -> Gst.FlowReturn:
        """Copy decoded RGBA frame data on the GStreamer thread."""
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if not buf or not caps:
            return Gst.FlowReturn.OK
        struct = caps.get_structure(0)
        width = struct.get_value("width")
        height = struct.get_value("height")
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(map_info.data)
        buf.unmap(map_info)
        self._latest_video_frame = (data, width, height)
        return Gst.FlowReturn.OK

    def _on_new_subtitle_sample(self, sink) -> Gst.FlowReturn:
        """Route subtitle data to the correct deque on the GStreamer thread.

        Raw ASS/SSA → _ass_chunks (rendered by libass on the Qt thread).
        text/x-raw  → _subtitle_queue (rendered by QPainter).

        Wrapped in try/except so a malformed buffer can never crash the
        callback and permanently break the subtitle pipeline.
        """
        try:
            sample = sink.emit("pull-sample")
            if not sample:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            if not buf:
                return Gst.FlowReturn.OK

            caps = sample.get_caps()
            caps_name = ""
            if caps and caps.get_size() > 0:
                caps_name = caps.get_structure(0).get_name()

            is_ass = "ass" in caps_name or "ssa" in caps_name

            ok, map_info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK
            raw = bytes(map_info.data)
            buf.unmap(map_info)

            if is_ass:
                # --- RAW ASS PATH ---
                # Extract codec_data (ASS header) from caps on first sample.
                if not self._ass_header_extracted:
                    self._ass_header_extracted = True
                    self._extract_ass_header(caps)

                # Queue the raw chunk for libass on the Qt thread.
                pts_ms = buf.pts // Gst.MSECOND if buf.pts != Gst.CLOCK_TIME_NONE else 0
                dur_ms = buf.duration // Gst.MSECOND if buf.duration != Gst.CLOCK_TIME_NONE else 5000
                self._ass_chunks.append((raw, int(pts_ms), int(dur_ms)))
            else:
                # --- TEXT PATH (SRT, VTT, pango-markup) ---
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return Gst.FlowReturn.OK
                text = text.replace("\x00", "").strip()
                if not text:
                    return Gst.FlowReturn.OK

                duration_ms = 3000
                if buf.duration != Gst.CLOCK_TIME_NONE:
                    duration_ms = buf.duration // Gst.MSECOND
                self._subtitle_queue.append((text, int(duration_ms)))

        except Exception:
            log.debug("subtitle sample error", exc_info=True)
        return Gst.FlowReturn.OK

    def _extract_ass_header(self, caps) -> None:
        """Extract the ASS codec_data (script info + styles) from caps."""
        try:
            struct = caps.get_structure(0)
            codec_data = struct.get_value("codec_data")
            if codec_data and hasattr(codec_data, "map"):
                ok, map_info = codec_data.map(Gst.MapFlags.READ)
                if ok:
                    self._ass_header = bytes(map_info.data)
                    codec_data.unmap(map_info)
                    log.info("Extracted ASS header (%d bytes)", len(self._ass_header))
        except Exception:
            log.debug("Could not extract ASS codec_data from caps", exc_info=True)

    # -- Public pull methods (called from Qt main thread) ---------------
    # These return pre-copied Python data — no GStreamer memory access.

    def pull_video_frame(self) -> tuple[bytes, int, int] | None:
        """Return the latest decoded video frame as (rgba_data, width, height).

        Non-blocking — returns None if no frame is available.
        The data was already copied on the GStreamer thread by the
        new-sample callback, so this is safe during pipeline transitions.
        """
        frame = self._latest_video_frame
        self._latest_video_frame = None
        return frame

    def pull_subtitle_text(self) -> tuple[str, int] | None:
        """Return the latest decoded subtitle as ``(text, duration_ms)``.

        For the text/x-raw path (SRT, VTT).  Drains the queue and returns
        the most recent entry.  Returns None when disabled or empty.
        """
        if not self._subtitles_enabled:
            self._subtitle_queue.clear()
            return None
        latest = None
        try:
            while True:
                latest = self._subtitle_queue.popleft()
        except IndexError:
            pass
        return latest

    def pull_ass_chunks(self) -> list[tuple[bytes, int, int]]:
        """Return all queued raw ASS chunks as ``[(data, pts_ms, dur_ms)]``.

        Called from the Qt thread.  The overlay feeds these to libass.
        """
        if not self._subtitles_enabled:
            self._ass_chunks.clear()
            return []
        result = []
        try:
            while True:
                result.append(self._ass_chunks.popleft())
        except IndexError:
            pass
        return result

    def get_ass_header(self) -> bytes | None:
        """Return the ASS codec_data (script info + styles), or None."""
        return self._ass_header

    def flush_subtitle_text(self) -> None:
        """Clear all pending subtitle data (call after seek or stream switch)."""
        self._subtitle_queue.clear()
        self._ass_chunks.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _stop_pipeline(self) -> None:
        self._latest_video_frame = None
        self._subtitle_queue.clear()
        self._ass_chunks.clear()
        self._ass_header = None
        self._ass_header_extracted = False
        self._playbin.set_state(Gst.State.NULL)
        # Block until the pipeline is fully in NULL state.  Without this,
        # rapid track switches can start a new PLAYING transition while
        # playsink is still tearing down the previous one — causing a
        # gst_play_sink_do_reconfigure assertion (SIGABRT).
        self._playbin.get_state(5 * Gst.SECOND)
        self._current_uri = None
        self._gapless_pending = False
        self._last_duration_ms = 0

    def _set_state(self, new_state: PlaybackState) -> None:
        self._state = new_state
        if self.on_state_changed:
            self.on_state_changed(new_state)

    def _on_about_to_finish(self, playbin: Gst.Element) -> None:
        """Called from a GStreamer thread — do NOT touch Qt here.

        If a next URI has been queued, set it on playbin for gapless
        transition.  The consumer's callback can also set it just-in-time.
        """
        if self.on_about_to_finish:
            self.on_about_to_finish()

        if self._next_uri:
            playbin.set_property("uri", self._next_uri)
            self._current_uri = self._next_uri
            self._next_uri = None
            self._gapless_pending = True

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        msg_type = message.type

        if msg_type == Gst.MessageType.EOS:
            if self._gapless_pending:
                # Gapless transition — pipeline continues, just reset flag.
                self._gapless_pending = False
            else:
                self._playbin.set_state(Gst.State.NULL)
                self._set_state(PlaybackState.STOPPED)
                if self.on_track_finished:
                    self.on_track_finished()

        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self._playbin.set_state(Gst.State.NULL)
            self._set_state(PlaybackState.STOPPED)
            if self.on_error:
                self.on_error(f"{err.message} ({debug})")

        elif msg_type == Gst.MessageType.STREAM_COLLECTION:
            self._stream_collection = message.parse_stream_collection()
            # Seed selected streams from the collection's initial state.
            self._selected_streams.clear()
            self._sub_stream_id = None

        elif msg_type == Gst.MessageType.STREAMS_SELECTED:
            # parse_streams_selected() returns the WHOLE collection; the
            # selected subset is only reachable via streams_selected_get_*.
            self._selected_streams = {
                message.streams_selected_get_stream(i).get_stream_id()
                for i in range(message.streams_selected_get_size())
            }
            if self._sub_stream_id is None:
                # playbin3's own pick: choosing it from the menu later
                # isn't a change of stream.
                for s in self._streams_by_type(Gst.StreamType.TEXT):
                    if s.get_stream_id() in self._selected_streams:
                        self._sub_stream_id = s.get_stream_id()

        elif msg_type == Gst.MessageType.STREAM_START:
            # Fires when a new stream actually starts (including gapless).
            if self._gapless_pending:
                self._gapless_pending = False
                self._set_state(PlaybackState.PLAYING)
                if self.on_gapless_transition:
                    self.on_gapless_transition()

    # Subtitle file extensions, in priority order.
    _SUB_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")

    @staticmethod
    def _find_external_subtitle(video_path: Path) -> Path | None:
        """Find an external subtitle file matching *video_path*.

        Looks for files like ``video.srt``, ``video.en.srt``, etc.
        in the same directory.  Returns the best match or None.
        """
        video_path = video_path.resolve()
        stem = video_path.stem  # e.g. "Episode 01"
        parent = video_path.parent
        if not parent.is_dir():
            return None

        # Exact stem match first (video.srt), then language variants (video.en.srt).
        best: Path | None = None
        best_priority = len(AudioEngine._SUB_EXTENSIONS) + 1
        try:
            for f in parent.iterdir():
                if not f.is_file():
                    continue
                name_lower = f.name.lower()
                # Must start with the video's stem.
                if not name_lower.startswith(stem.lower()):
                    continue
                for pri, ext in enumerate(AudioEngine._SUB_EXTENSIONS):
                    if name_lower.endswith(ext) and pri < best_priority:
                        # Prefer exact match (video.srt) over lang variant (video.en.srt).
                        remainder = f.stem[len(stem):]  # e.g. "" or ".en"
                        if remainder == "":
                            return f  # Exact match — can't do better.
                        if best is None or pri < best_priority:
                            best = f
                            best_priority = pri
                        break
        except OSError:
            pass
        return best

    @staticmethod
    def _path_to_uri(path: str | Path) -> str:
        p = Path(path).resolve()
        return p.as_uri()
