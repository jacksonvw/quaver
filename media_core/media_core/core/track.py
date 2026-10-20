"""Track dataclass + drag-and-drop MIME types — Qt-free, library-free.

The :class:`Track` is the playable item used by both the library shell
(Quaver) and the lightweight player shell (Minim).  No Qt imports, no
SQLite imports, no scraper imports — pure data.

Library-only IDs (``track.id == -1``) are valid: that's how transient
tracks (podcast episodes via RSS, files opened ad-hoc, Minim's whole
queue) are represented.  Code that touches a database must guard on
``track.id >= 0`` before doing a DB lookup.
"""

from __future__ import annotations

from dataclasses import dataclass


# Shared MIME types for drag-and-drop between widgets.  The "x-quaver-"
# prefix is historical — these MIMEs originated in Quaver and the
# QueueWidget that consumes them lives in media_core (used by both apps).
TRACK_IDS_MIME = "application/x-quaver-track-ids"
EPISODE_IDS_MIME = "application/x-quaver-episode-ids"
PLAYLIST_IDS_MIME = "application/x-quaver-playlist-ids"
VIDEO_IDS_MIME = "application/x-quaver-video-ids"

# Cross-instance queue drop: carries fully-serialised Track objects so a
# queue in window/process B can reconstruct what was dragged out of
# window/process A without sharing memory or a database.  Used for
# "consolidate two queues" — drag rows from one Minim window into
# another's queue.
QUEUE_TRACKS_MIME = "application/x-quaver-queue-tracks"


@dataclass
class Track:
    id: int
    filepath: str
    title: str
    artist: str
    album: str
    album_artist: str
    year: int | None
    genre: str
    track_number: int | None
    disc_number: int | None
    duration_ms: int
    bitrate: int
    format: str
    replay_gain_track: float | None
    replay_gain_album: float | None
    play_count: int
    last_played: str | None
    date_added: str
    rating: int
    fingerprint: str | None
    media_type: str = "audio"
    show_id: int | None = None
