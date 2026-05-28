#!/usr/bin/env python3
"""Smoke-test Spotify Web API credentials (Client Credentials flow)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
from spotipy.exceptions import SpotifyException

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def main() -> int:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        print("Missing environment variables:", ", ".join(missing))
        print(f"Copy {ROOT / '.env.example'} to {ROOT / '.env'} and fill in your app credentials.")
        return 1

    auth = SpotifyClientCredentials(
        client_id=client_id,
        client_secret=client_secret,
    )
    spotify = Spotify(auth_manager=auth)

    try:
        result = spotify.search(q="spotify", type="track", limit=1)
    except SpotifyException as exc:
        print(f"Spotify API error ({exc.http_status}): {exc.msg}")
        return 1

    tracks = result.get("tracks", {}).get("items", [])
    if not tracks:
        print("Connected, but search returned no tracks.")
        return 0

    track = tracks[0]
    artists = ", ".join(a["name"] for a in track["artists"])
    print("Spotify Web API connection OK.")
    print(f"Sample track: {track['name']} — {artists}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
