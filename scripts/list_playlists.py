#!/usr/bin/env python3
"""List Spotify playlists in the Sorter playlist folder (desktop app cache)."""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from spotipy.exceptions import SpotifyException

from sorter_playlists import fetch_folder_playlists
from spotify_auth import ROOT, configure_spotipy_env, get_spotify_client

load_dotenv(ROOT / ".env")

FOLDER_NAME = "Sorter"


def main() -> int:
    configure_spotipy_env()

    try:
        spotify = get_spotify_client()
    except RuntimeError:
        return 1

    print("Authenticating (browser opens on first run; token is cached after that)...")

    try:
        profile = spotify.current_user()
    except SpotifyException as exc:
        print(f"Spotify API error ({exc.http_status}): {exc.msg}")
        return 1

    display_name = profile.get("display_name") or profile.get("id")
    print(f'\nPlaylists in "{FOLDER_NAME}" ({display_name}):\n')

    try:
        playlists = fetch_folder_playlists(spotify, FOLDER_NAME)
    except RuntimeError as exc:
        print(exc)
        return 1
    except SpotifyException as exc:
        print(f"Spotify API error ({exc.http_status}): {exc.msg}")
        return 1

    if not playlists:
        print("  (no playlists in folder)")
        return 0

    for index, playlist in enumerate(playlists, start=1):
        name = playlist["name"]
        playlist_id = playlist["id"]
        description = (playlist.get("description") or "").strip()
        url = f"https://open.spotify.com/playlist/{playlist_id}"
        print(f"{index:3}. {name}")
        if description:
            print(f"     {description}")
        print(f"     {url}")

    print(f"\n{len(playlists)} playlist(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
