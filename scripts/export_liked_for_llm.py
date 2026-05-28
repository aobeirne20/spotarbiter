#!/usr/bin/env python3
"""Export Liked Songs with enriched metadata to JSON for LLM playlist sorting."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from spotipy.exceptions import SpotifyException

from liked_library import build_track_record, fetch_liked_items
from spotify_auth import ROOT, configure_spotipy_env, get_spotify_client
from track_enrichment import enrich_tracks

load_dotenv(ROOT / ".env")

DEFAULT_OUTPUT = ROOT / "data" / "liked_tracks.json"


def build_export_payload(items: list[dict[str, Any]], enriched_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tracks: list[dict[str, Any]] = []
    for item in items:
        track = item.get("track")
        if not track or not track.get("id"):
            continue
        enriched = enriched_by_id.get(track["id"], {})
        tracks.append(build_track_record(item, enriched))

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": "spotify_liked_songs",
        "track_count": len(tracks),
        "tracks": tracks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--refresh-tags",
        action="store_true",
        help="Ignore cached enrichment and re-fetch",
    )
    parser.add_argument(
        "--skip-lastfm",
        action="store_true",
        help="Skip Last.fm genre tags (fastest)",
    )
    parser.add_argument(
        "--skip-audio",
        action="store_true",
        help="Skip ReccoBeats audio features (BPM, energy, etc.)",
    )
    parser.add_argument(
        "--lastfm-mode",
        choices=("off", "artist-only", "full"),
        help="Last.fm lookup: artist-only is much faster for large libraries",
    )
    args = parser.parse_args()

    configure_spotipy_env()

    try:
        spotify = get_spotify_client(scopes="user-library-read")
    except RuntimeError:
        return 1

    print("Fetching liked songs from Spotify...")
    try:
        items = fetch_liked_items(spotify)
    except SpotifyException as exc:
        print(f"Spotify API error ({exc.http_status}): {exc.msg}")
        return 1

    spotify_tracks = [item["track"] for item in items if item.get("track")]
    lastfm_key = "" if args.skip_lastfm else os.getenv("LASTFM_API_KEY", "").strip()

    print(f"Enriching {len(spotify_tracks)} tracks...")
    enriched_by_id = enrich_tracks(
        spotify_tracks,
        lastfm_api_key=lastfm_key,
        refresh_tags=args.refresh_tags,
        skip_lastfm=args.skip_lastfm,
        skip_audio=args.skip_audio,
        lastfm_mode=args.lastfm_mode,
    )

    payload = build_export_payload(items, enriched_by_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"Exported {payload['track_count']} tracks to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
