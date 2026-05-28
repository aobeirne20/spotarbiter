#!/usr/bin/env python3
"""List every track in your Spotify Liked Songs library with enriched metadata."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from dotenv import load_dotenv
from spotipy.exceptions import SpotifyException

from liked_library import fetch_liked_items
from spotify_auth import ROOT, configure_spotipy_env, get_spotify_client
from track_enrichment import enrich_tracks

load_dotenv(ROOT / ".env")


def format_duration(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "?"
    seconds, _ = divmod(duration_ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}" if minutes else f"0:{seconds:02d}"


def format_optional(value: Any, *, digits: int | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and digits is not None:
        return f"{value:.{digits}f}"
    if isinstance(value, list):
        return ", ".join(value) if value else None
    return str(value)


def print_field(label: str, value: str | None) -> None:
    if value is not None:
        print(f"    {label:<22} {value}")


def print_track(index: int, item: dict[str, Any], enriched: dict[str, Any]) -> None:
    track = item.get("track") or {}
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    album = track.get("album", {})

    print(f"{index:3}. {track.get('name', '?')} — {artists}")
    print_field("Liked at", item.get("added_at"))
    print_field("Album", album.get("name"))
    print_field("Release", album.get("release_date"))
    print_field("Duration", format_duration(track.get("duration_ms")))
    print_field("Explicit", str(track.get("explicit")) if "explicit" in track else None)
    print_field("ISRC", (track.get("external_ids") or {}).get("isrc"))
    print_field("Genres", format_optional(enriched.get("genres")))
    print_field("Mood tags", format_optional(enriched.get("mood_tags")))
    print_field("Mood", enriched.get("mood"))
    print_field("BPM", format_optional(enriched.get("bpm"), digits=1))
    print_field("Key", enriched.get("key"))
    print_field("Danceability", format_optional(enriched.get("danceability"), digits=2))
    print_field("Energy", format_optional(enriched.get("energy"), digits=2))
    print_field("Valence", format_optional(enriched.get("valence"), digits=2))
    print_field("Acousticness", format_optional(enriched.get("acousticness"), digits=2))
    print_field("Instrumentalness", format_optional(enriched.get("instrumentalness"), digits=2))
    print_field("Liveness", format_optional(enriched.get("liveness"), digits=2))
    print_field("Speechiness", format_optional(enriched.get("speechiness"), digits=2))
    print_field("Loudness (dB)", format_optional(enriched.get("loudness_db"), digits=1))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-tags",
        action="store_true",
        help="Re-fetch Last.fm genre tags (use after adding LASTFM_API_KEY)",
    )
    args = parser.parse_args()

    configure_spotipy_env()

    try:
        spotify = get_spotify_client(scopes="user-library-read")
    except RuntimeError:
        return 1

    try:
        items = fetch_liked_items(spotify)
    except SpotifyException as exc:
        print(f"Spotify API error ({exc.http_status}): {exc.msg}")
        return 1

    tracks = [item["track"] for item in items if item.get("track")]
    lastfm_key = os.getenv("LASTFM_API_KEY", "").strip()

    if lastfm_key:
        print("Enriching tracks (ReccoBeats + Last.fm)...")
    else:
        print("Enriching tracks (ReccoBeats)...")
        print(
            "Tip: Add LASTFM_API_KEY to .env for genre tags "
            "(free: https://www.last.fm/api/account/create)"
        )
    enriched_by_id = enrich_tracks(
        tracks,
        lastfm_api_key=lastfm_key,
        refresh_tags=args.refresh_tags,
    )
    print()

    print(f"Liked Songs ({len(items)} tracks)\n")
    for index, item in enumerate(items, start=1):
        track = item.get("track")
        if not track:
            continue
        enriched = enriched_by_id.get(track["id"], {})
        print_track(index, item, enriched)

    return 0


if __name__ == "__main__":
    sys.exit(main())
