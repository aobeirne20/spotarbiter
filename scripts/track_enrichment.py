"""Fetch genre/mood/BPM metadata from services Spotify Dev Mode does not expose."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / ".spotify" / "enrichment"
ARTIST_CACHE_DIR = CACHE_DIR / "artists"
RECCOBEATS_URL = "https://api.reccobeats.com/v1/audio-features"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "spotarbiter/0.1 (local dev)"
RECCOBEATS_BATCH = 40
LASTFM_DELAY_SEC = 0.2
PROGRESS_EVERY = 25
BULK_LIBRARY_THRESHOLD = 150

MOOD_KEYWORDS = frozenset({
    "chill", "sad", "happy", "energetic", "dark", "upbeat", "mellow", "aggressive",
    "romantic", "party", "sleep", "focus", "relaxing", "calm", "melancholy",
    "depressive", "angry", "emotional", "intense", "dreamy", "atmospheric",
    "summer", "winter", "night", "morning", "workout", "study",
})

KEY_NAMES = ("C", "C♯/D♭", "D", "D♯/E♭", "E", "F", "F♯/G♭", "G", "G♯/A♭", "A", "A♯/B♭", "B")


def _http_get_json(url: str, params: dict[str, str] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    full_url = f"{url}?{query}" if query else url
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def _lastfm_call(api_key: str, method: str, **params: str) -> dict[str, Any]:
    payload = _http_get_json(
        LASTFM_URL,
        {
            "method": method,
            "api_key": api_key,
            "format": "json",
            "autocorrect": "1",
            **params,
        },
    )
    if "error" in payload:
        raise RuntimeError(f"Last.fm error {payload['error']}: {payload.get('message', '')}")
    return payload


def _parse_tags(tag_payload: Any) -> list[str]:
    if not tag_payload:
        return []
    tags = tag_payload if isinstance(tag_payload, list) else [tag_payload]
    return [tag["name"] for tag in tags if isinstance(tag, dict) and tag.get("name")]


def _lastfm_track_tags(api_key: str, artist: str, title: str) -> list[str]:
    payload = _lastfm_call(api_key, "track.getInfo", artist=artist, track=title)
    return _parse_tags(payload.get("track", {}).get("toptags", {}).get("tag"))


def _lastfm_artist_tags(api_key: str, artist: str) -> list[str]:
    payload = _lastfm_call(api_key, "artist.getTopTags", artist=artist)
    return _parse_tags(payload.get("toptags", {}).get("tag"))


def _artist_cache_path(artist: str) -> Path:
    safe = urllib.parse.quote(artist.lower(), safe="")
    return ARTIST_CACHE_DIR / f"{safe[:120]}.json"


def _load_artist_tags(artist: str) -> list[str] | None:
    path = _artist_cache_path(artist)
    if path.exists():
        data = json.loads(path.read_text())
        return data.get("tags")
    return None


def _save_artist_tags(artist: str, tags: list[str]) -> None:
    ARTIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _artist_cache_path(artist).write_text(json.dumps({"tags": tags}, indent=2))


def fetch_artist_tags(api_key: str, artist: str, memory_cache: dict[str, list[str]]) -> list[str]:
    key = artist.lower()
    if key in memory_cache:
        return memory_cache[key]
    disk = _load_artist_tags(artist)
    if disk is not None:
        memory_cache[key] = disk
        return disk
    try:
        tags = _lastfm_artist_tags(api_key, artist)[:10]
    except (urllib.error.HTTPError, RuntimeError):
        tags = []
    time.sleep(LASTFM_DELAY_SEC)
    memory_cache[key] = tags
    _save_artist_tags(artist, tags)
    return tags


def fetch_lastfm_tags(
    artists: list[str],
    title: str,
    api_key: str,
    *,
    mode: str,
    artist_memory: dict[str, list[str]],
) -> list[str]:
    if not api_key or mode == "off":
        return []

    seen: set[str] = set()
    merged: list[str] = []

    def add_tags(tags: list[str]) -> None:
        for tag in tags:
            key = tag.strip().lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(tag)

    if mode == "artist-only":
        for artist in artists:
            add_tags(fetch_artist_tags(api_key, artist, artist_memory))
            if merged:
                return merged[:10]
        return merged

    # full: track tags first, then artist fallback
    for artist in artists:
        try:
            add_tags(_lastfm_track_tags(api_key, artist, title))
        except (urllib.error.HTTPError, RuntimeError):
            pass
        if merged:
            return merged[:10]
        time.sleep(LASTFM_DELAY_SEC)

    for artist in artists:
        add_tags(fetch_artist_tags(api_key, artist, artist_memory))
        if merged:
            return merged[:10]
    return merged


def _cache_path(track_id: str) -> Path:
    return CACHE_DIR / f"{track_id}.json"


def _load_cache(track_id: str) -> dict[str, Any] | None:
    path = _cache_path(track_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_cache(track_id: str, data: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(track_id).write_text(json.dumps(data, indent=2))


def _needs_tag_refresh(cached: dict[str, Any], lastfm_api_key: str) -> bool:
    if not lastfm_api_key:
        return False
    return cached.get("tags_source") != "last.fm"


def fetch_reccobeats_batch(
    track_ids: list[str],
    *,
    on_progress: Optional[Callable[..., None]] = None,
) -> dict[str, dict[str, Any]]:
    if not track_ids:
        return {}

    features: dict[str, dict[str, Any]] = {}
    total_batches = (len(track_ids) + RECCOBEATS_BATCH - 1) // RECCOBEATS_BATCH
    for batch_num, start in enumerate(range(0, len(track_ids), RECCOBEATS_BATCH), start=1):
        batch = track_ids[start : start + RECCOBEATS_BATCH]
        try:
            payload = _http_get_json(RECCOBEATS_URL, {"ids": ",".join(batch)})
        except urllib.error.HTTPError:
            pass
        else:
            for row in payload.get("content", []):
                href = row.get("href", "")
                spotify_id = href.rstrip("/").split("/")[-1] if href else None
                if spotify_id:
                    features[spotify_id] = row
        if on_progress:
            on_progress("audio", batch_num, total_batches, len(features))
    return features


def split_tags(tags: list[str]) -> tuple[list[str], list[str]]:
    genres: list[str] = []
    moods: list[str] = []
    for tag in tags:
        normalized = tag.strip().lower()
        if normalized in MOOD_KEYWORDS or any(word in normalized for word in MOOD_KEYWORDS):
            moods.append(tag)
        else:
            genres.append(tag)
    return genres, moods


def format_key(key: int | None, mode: int | None) -> str | None:
    if key is None or key < 0:
        return None
    name = KEY_NAMES[key] if 0 <= key < len(KEY_NAMES) else str(key)
    quality = "major" if mode == 1 else "minor" if mode == 0 else None
    return f"{name} {quality}" if quality else name


def infer_mood_from_audio(energy: float | None, valence: float | None) -> str | None:
    if energy is None or valence is None:
        return None
    if valence >= 0.5 and energy >= 0.5:
        return "happy / energetic"
    if valence >= 0.5:
        return "chill / positive"
    if energy >= 0.5:
        return "tense / aggressive"
    return "sad / mellow"


def _build_enriched(
    audio: dict[str, Any] | None,
    tags: list[str],
) -> dict[str, Any]:
    genres, mood_tags = split_tags(tags)
    mood = ", ".join(mood_tags) if mood_tags else infer_mood_from_audio(
        audio.get("energy") if audio else None,
        audio.get("valence") if audio else None,
    )
    return {
        "genres": genres,
        "mood_tags": mood_tags,
        "mood": mood,
        "bpm": round(audio["tempo"], 1) if audio and audio.get("tempo") is not None else None,
        "key": format_key(audio.get("key") if audio else None, audio.get("mode") if audio else None),
        "danceability": audio.get("danceability") if audio else None,
        "energy": audio.get("energy") if audio else None,
        "valence": audio.get("valence") if audio else None,
        "acousticness": audio.get("acousticness") if audio else None,
        "instrumentalness": audio.get("instrumentalness") if audio else None,
        "liveness": audio.get("liveness") if audio else None,
        "speechiness": audio.get("speechiness") if audio else None,
        "loudness_db": audio.get("loudness") if audio else None,
        "audio_source": "reccobeats" if audio else None,
        "tags_source": "last.fm" if tags else None,
    }


def enrich_tracks(
    tracks: list[dict[str, Any]],
    *,
    lastfm_api_key: str | None = None,
    refresh_tags: bool = False,
    skip_lastfm: bool = False,
    skip_audio: bool = False,
    lastfm_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    track_ids = [t["id"] for t in tracks if t.get("id")]
    total = len(tracks)
    api_key = "" if skip_lastfm else (lastfm_api_key or os.getenv("LASTFM_API_KEY", ""))

    if lastfm_mode is None:
        if skip_lastfm or not api_key:
            lastfm_mode = "off"
        elif total > BULK_LIBRARY_THRESHOLD:
            lastfm_mode = "artist-only"
            print(
                f"  Large library ({total} tracks): using fast Last.fm artist tags only. "
                "Use --lastfm-mode full for per-track tags (much slower)."
            )
        else:
            lastfm_mode = "full"

    if refresh_tags and api_key:
        for path in CACHE_DIR.glob("*.json"):
            path.unlink()

    def progress(phase: str, current: int, total_steps: int, extra: int = 0) -> None:
        if phase == "audio":
            print(f"  Audio features: batch {current}/{total_steps} ({extra} tracks matched)")
        elif current % PROGRESS_EVERY == 0 or current == total_steps:
            print(f"  Tags/metadata: {current}/{total_steps}")

    audio_by_id: dict[str, dict[str, Any]] = {}
    if not skip_audio:
        print("  Fetching audio features (ReccoBeats)...")
        audio_by_id = fetch_reccobeats_batch(track_ids, on_progress=progress)

    artist_memory: dict[str, list[str]] = {}
    results: dict[str, dict[str, Any]] = {}
    cached_count = 0

    for index, track in enumerate(tracks, start=1):
        track_id = track.get("id")
        if not track_id:
            continue

        cached = _load_cache(track_id) if not refresh_tags else None
        audio = None if skip_audio else audio_by_id.get(track_id)

        if cached and not _needs_tag_refresh(cached, api_key):
            if skip_audio or cached.get("audio_source") or not audio:
                results[track_id] = cached
                cached_count += 1
                progress("tags", index, total)
                continue

        artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
        title = track.get("name", "")
        tags = fetch_lastfm_tags(
            artists,
            title,
            api_key,
            mode=lastfm_mode,
            artist_memory=artist_memory,
        )
        enriched = _build_enriched(audio, tags)
        _save_cache(track_id, enriched)
        results[track_id] = enriched
        progress("tags", index, total)

    print(f"  Done. {cached_count} from cache, {len(artist_memory)} artists looked up on Last.fm.")
    return results
