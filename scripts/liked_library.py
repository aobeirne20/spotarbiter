"""Fetch and normalize Spotify Liked Songs for listing and export."""

from __future__ import annotations

from typing import Any

PAGE_SIZE = 50


def fetch_liked_items(spotify) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = spotify.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset)
        items.extend(page.get("items", []))
        if page.get("next") is None:
            break
        offset += PAGE_SIZE
    return items


def build_track_record(item: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    track = item.get("track") or {}
    album = track.get("album") or {}
    duration_ms = track.get("duration_ms")

    record: dict[str, Any] = {
        "spotify_id": track.get("id"),
        "uri": track.get("uri"),
        "preview_url": track.get("preview_url"),
        "title": track.get("name"),
        "artists": [a["name"] for a in track.get("artists", []) if a.get("name")],
        "album": album.get("name"),
        "album_type": album.get("album_type"),
        "release_date": album.get("release_date"),
        "duration_ms": duration_ms,
        "explicit": track.get("explicit"),
        "isrc": (track.get("external_ids") or {}).get("isrc"),
        "liked_at": item.get("added_at"),
        "genres": enriched.get("genres") or [],
        "mood_tags": enriched.get("mood_tags") or [],
        "mood": enriched.get("mood"),
        "bpm": enriched.get("bpm"),
        "key": enriched.get("key"),
    }

    audio_fields = (
        "danceability",
        "energy",
        "valence",
        "acousticness",
        "instrumentalness",
        "liveness",
        "speechiness",
    )
    audio = {
        name: enriched.get(name)
        for name in audio_fields
        if enriched.get(name) is not None
    }
    if enriched.get("loudness_db") is not None:
        audio["loudness_db"] = enriched.get("loudness_db")
    if audio:
        record["audio"] = audio

    return record
