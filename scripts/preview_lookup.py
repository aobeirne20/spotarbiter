"""Resolve 30-second preview URLs (Spotify previews are largely unavailable since 2024)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

ITUNES_TIMEOUT = 6
USER_AGENT = "spotarbiter/1.0"


def _itunes_get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=ITUNES_TIMEOUT) as response:
        return json.loads(response.read())


def lookup_itunes_isrc(isrc: str) -> str | None:
    encoded = urllib.parse.quote(isrc.strip())
    url = f"https://itunes.apple.com/lookup?isrc={encoded}&entity=song&limit=1"
    data = _itunes_get(url)
    for item in data.get("results", []):
        preview = item.get("previewUrl")
        if preview:
            return str(preview)
    return None


def lookup_itunes_search(artist: str, title: str) -> str | None:
    term = urllib.parse.quote(f"{artist.strip()} {title.strip()}")
    url = f"https://itunes.apple.com/search?term={term}&entity=song&limit=5"
    data = _itunes_get(url)
    for item in data.get("results", []):
        preview = item.get("previewUrl")
        if preview:
            return str(preview)
    return None


def resolve_preview(track: dict[str, Any]) -> str | None:
    """Find a playable preview URL for a track record from liked_tracks.json."""
    existing = track.get("preview_url")
    if existing:
        return str(existing)

    isrc = track.get("isrc")
    if isinstance(isrc, str) and isrc.strip():
        url = lookup_itunes_isrc(isrc)
        if url:
            return url

    artists = track.get("artists") or []
    title = track.get("title") or ""
    if artists and title:
        return lookup_itunes_search(str(artists[0]), str(title))

    return None
