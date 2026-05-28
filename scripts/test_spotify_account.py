#!/usr/bin/env python3
"""Probe the authenticated Spotify account (Authorization Code with PKCE)."""

from __future__ import annotations

import sys

from spotipy.exceptions import SpotifyException

from spotify_auth import configure_spotipy_env, get_spotify_client


def main() -> int:
    configure_spotipy_env()

    try:
        spotify = get_spotify_client()
    except RuntimeError:
        return 1

    print("Authenticating (browser opens on first run; token is cached after that)...")

    try:
        profile = spotify.current_user()
        playlists = spotify.current_user_playlists(limit=5)
        saved = spotify.current_user_saved_tracks(limit=1)
    except SpotifyException as exc:
        print(f"Spotify API error ({exc.http_status}): {exc.msg}")
        return 1

    display_name = profile.get("display_name") or profile.get("id")
    product = profile.get("product", "unknown")
    followers = profile.get("followers", {}).get("total", 0)
    playlist_total = playlists.get("total", 0)
    saved_total = saved.get("total", 0)

    print()
    print("Spotify account probe OK.")
    print(f"  User:      {display_name} ({profile['id']})")
    print(f"  Plan:      {product}")
    print(f"  Followers: {followers}")
    print(f"  Playlists: {playlist_total}")
    print(f"  Liked:     {saved_total} saved tracks")

    items = playlists.get("items", [])
    if items:
        print()
        print("Recent playlists:")
        for entry in items:
            playlist = entry.get("playlist") or entry
            name = playlist.get("name", "?")
            track_count = playlist.get("tracks", {}).get("total", "?")
            print(f"  - {name} ({track_count} tracks)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
