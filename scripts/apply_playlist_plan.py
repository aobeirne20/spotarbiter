#!/usr/bin/env python3
"""Validate and apply a playlist plan to Spotify (creates new playlists)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from spotipy.exceptions import SpotifyException

from playlist_config import DEFAULT_CONFIG_PATH, load_playlist_config
from playlist_plan import load_export, load_plan, normalize_playlist_name, validate_plan
from spotify_auth import PLAYLIST_SCOPES, ROOT, configure_spotipy_env, get_spotify_client

load_dotenv(ROOT / ".env")

DEFAULT_PLAN = ROOT / "data" / "playlist_plan.json"
DEFAULT_EXPORT = ROOT / "data" / "liked_tracks.json"
BATCH_SIZE = 100


def track_uris(track_ids: list[str]) -> list[str]:
    return [f"spotify:track:{track_id}" for track_id in track_ids]


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def apply_playlist(
    spotify,
    playlist_id: str,
    track_ids: list[str],
    *,
    replace: bool,
    dry_run: bool,
) -> None:
    uris = track_uris(track_ids)
    if not uris or dry_run:
        return
    if replace:
        spotify.playlist_replace_items(playlist_id, uris[:BATCH_SIZE])
        for batch in chunked(uris[BATCH_SIZE:], BATCH_SIZE):
            spotify.playlist_add_items(playlist_id, batch)
    else:
        for batch in chunked(uris, BATCH_SIZE):
            spotify.playlist_add_items(playlist_id, batch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print actions without changing Spotify",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace playlist contents (only relevant if re-using an existing id)",
    )
    args = parser.parse_args()

    configure_spotipy_env()

    if not args.plan.is_file():
        print(f"Plan not found: {args.plan}")
        return 1
    if not args.export.is_file():
        print(f"Export not found: {args.export}")
        return 1

    export = load_export(args.export)
    plan = load_plan(args.plan)
    track_ids = {t["spotify_id"] for t in export.get("tracks", []) if t.get("spotify_id")}

    config = None
    if args.config.is_file():
        try:
            config = load_playlist_config(args.config)
        except (ValueError, json.JSONDecodeError):
            pass

    errors = validate_plan(plan, track_ids, config)
    if errors:
        print("Plan validation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    try:
        spotify = get_spotify_client(scopes=PLAYLIST_SCOPES)
    except RuntimeError:
        return 1

    if args.dry_run:
        print("Dry run — no changes will be made.\n")

    created = 0
    for raw_name, entry in plan.get("playlists", {}).items():
        name = normalize_playlist_name(raw_name)
        ids = entry.get("track_ids", [])
        description = entry.get("description") or ""

        if args.dry_run:
            print(f"CREATE {name!r} — {len(ids)} tracks")
            if description:
                print(f"         {description}")
            continue

        try:
            playlist = spotify.current_user_playlist_create(
                name,
                public=False,
                description=description,
            )
        except SpotifyException as exc:
            print(f"Failed to create {name!r}: {exc.msg}")
            return 1

        playlist_id = playlist["id"]
        try:
            apply_playlist(
                spotify,
                playlist_id,
                ids,
                replace=args.replace,
                dry_run=False,
            )
        except SpotifyException as exc:
            print(f"Failed to add tracks to {name!r}: {exc.msg}")
            return 1

        created += 1
        print(f"Created {name!r} ({len(ids)} tracks)")

    unassigned = plan.get("unassigned", [])
    if unassigned:
        print(f"\n{len(unassigned)} track(s) remain in Liked Songs only.")

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to create playlists.")
    else:
        print(f"\nDone. Created {created} new playlist(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
