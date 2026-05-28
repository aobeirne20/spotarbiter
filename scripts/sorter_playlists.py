"""Load playlist buckets for the sort GUI from a Spotify playlist folder."""

from __future__ import annotations

from typing import Any

from spotify_playlist_folders import playlist_ids_in_folder
from spotipy import Spotify


def fetch_folder_playlists(
    spotify: Spotify,
    folder_name: str = "Sorter",
    *,
    account: str | None = None,
) -> list[dict[str, str]]:
    """Return {id, name, description} for each playlist in the named folder."""
    playlist_ids = playlist_ids_in_folder(folder_name, account=account)
    playlists: list[dict[str, str]] = []
    for playlist_id in playlist_ids:
        detail = spotify.playlist(playlist_id, fields="id,name,description")
        playlists.append(
            {
                "id": playlist_id,
                "name": str(detail.get("name") or "Untitled playlist"),
                "description": str(detail.get("description") or ""),
            }
        )
    return playlists


def _playlist_entry_track_id(entry: dict) -> str | None:
    """Track id from a playlist item (legacy `track` or newer `item` shape)."""
    track = entry.get("track")
    if isinstance(track, dict):
        track_id = track.get("id")
        if track_id:
            return str(track_id)
    item = entry.get("item")
    if isinstance(item, dict) and item.get("type") == "track":
        track_id = item.get("id")
        if track_id:
            return str(track_id)
    return None


def fetch_playlist_track_ids(spotify: Spotify, playlist_id: str) -> list[str]:
    """All track Spotify IDs in a playlist, in playlist order."""
    ids: list[str] = []
    offset = 0
    limit = 100
    while True:
        page = spotify.playlist_tracks(playlist_id, offset=offset, limit=limit)
        for entry in page.get("items", []):
            if not isinstance(entry, dict):
                continue
            track_id = _playlist_entry_track_id(entry)
            if track_id:
                ids.append(track_id)
        if not page.get("next"):
            break
        offset += limit
    return ids


def merge_bucket_tracks(
    spotify_ids: list[str],
    local_ids: list[str],
    export_ids: set[str],
) -> list[str]:
    """Spotify order first, then local-only assignments; only tracks in the export."""
    seen: set[str] = set()
    merged: list[str] = []
    for track_id in (*spotify_ids, *local_ids):
        if track_id not in export_ids or track_id in seen:
            continue
        seen.add(track_id)
        merged.append(track_id)
    return merged


def import_playlist_tracks(
    sort_data: dict[str, Any],
    playlists: list[dict[str, str]],
    spotify: Spotify,
    export_track_ids: set[str],
) -> int:
    """Merge each playlist's Spotify tracks into sort buckets. Returns total imported."""
    buckets = sort_data.setdefault("buckets", {})
    total = 0
    for playlist in playlists:
        playlist_id = playlist["id"]
        spotify_ids = fetch_playlist_track_ids(spotify, playlist_id)
        local_ids = buckets.get(playlist_id, [])
        merged = merge_bucket_tracks(spotify_ids, local_ids, export_track_ids)
        buckets[playlist_id] = merged
        total += len(merged)
    return total


def sync_sort_playlists(sort_data: dict[str, Any], playlists: list[dict[str, str]]) -> list[str]:
    """Merge folder playlists into sort_data; return bucket keys in folder order."""
    ordered_ids = [p["id"] for p in playlists]
    meta = sort_data.setdefault("playlists", {})
    old_buckets = sort_data.get("buckets", {})
    new_buckets: dict[str, list[str]] = {}
    for playlist in playlists:
        playlist_id = playlist["id"]
        meta[playlist_id] = {
            "name": playlist["name"],
            "description": playlist.get("description") or "",
        }
        new_buckets[playlist_id] = list(old_buckets.get(playlist_id, []))
    sort_data["playlists"] = meta
    sort_data["buckets"] = new_buckets
    sort_data["playlist_order"] = ordered_ids
    sort_data.pop("names", None)
    sort_data.pop("bucket_count", None)
    if "containment" in sort_data.get("buckets", {}):
        del sort_data["buckets"]["containment"]
    return ordered_ids
