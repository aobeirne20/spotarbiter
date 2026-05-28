#!/usr/bin/env python3
"""Manual song sorting — track on the left, flexible playlist buckets on the right."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

from preview_lookup import resolve_preview as lookup_preview_url
from sort_ai_analysis import run_initial_analysis
from sorter_playlists import (
    fetch_folder_playlists,
    import_playlist_tracks,
    sync_sort_playlists,
)
from spotify_auth import configure_spotipy_env, get_spotify_client

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DEFAULT_EXPORT = ROOT / "data" / "liked_tracks.json"
DEFAULT_SORT = ROOT / "data" / "manual_sort.json"
DEFAULT_PORT = 8765
DEFAULT_SORTER_FOLDER = "Sorter"
PREVIEW_CACHE_PATH = ROOT / ".spotify" / "preview_cache.json"
PERSIST_DEBOUNCE_S = 0.3
PREVIEW_WARM_AHEAD = 8


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def save_sort(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def playlist_keys_from_data(data: dict) -> list[str]:
    """Playlist bucket keys in folder order."""
    order = data.get("playlist_order")
    if isinstance(order, list) and order:
        return [str(key) for key in order]
    playlists = data.get("playlists", {})
    if isinstance(playlists, dict) and playlists:
        return list(playlists.keys())
    return [key for key in data.get("buckets", {}) if key != "containment"]


def empty_sort() -> dict:
    return {
        "playlists": {},
        "playlist_order": [],
        "buckets": {},
        "unassigned": [],
    }


def load_or_create_sort(path: Path) -> dict:
    if not path.is_file():
        return empty_sort()
    data = load_json(path)
    data.setdefault("playlists", {})
    data.setdefault("playlist_order", [])
    data.setdefault("buckets", {})
    data.setdefault("unassigned", [])
    for key in playlist_keys_from_data(data):
        data["buckets"].setdefault(key, [])
    return data


def all_track_ids(export: dict) -> list[str]:
    return [t["spotify_id"] for t in export.get("tracks", [])]


def assigned_ids(data: dict) -> set[str]:
    seen: set[str] = set()
    for ids in data.get("buckets", {}).values():
        seen.update(ids)
    return seen


def build_queue(data: dict, export: dict) -> list[str]:
    assigned = assigned_ids(data)
    return [tid for tid in all_track_ids(export) if tid not in assigned]


def format_artists(track: dict) -> str:
    artists = track.get("artists") or []
    return ", ".join(artists) if artists else "Unknown artist"


def load_preview_cache_file() -> dict[str, str | None]:
    if not PREVIEW_CACHE_PATH.is_file():
        return {}
    try:
        raw = json.loads(PREVIEW_CACHE_PATH.read_text())
        return {str(k): (v if v else None) for k, v in raw.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def save_preview_cache_file(cache: dict[str, str | None]) -> None:
    PREVIEW_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n")


class SortSession:
    def __init__(
        self,
        export: dict,
        sort_data: dict,
        sort_path: Path,
        queue: list[str],
    ) -> None:
        self.export = export
        self.sort_data = sort_data
        self.sort_path = sort_path
        self.queue = queue
        self.track_by_id = {t["spotify_id"]: t for t in export.get("tracks", [])}
        self.history: list[tuple[str, str | None]] = []
        self._sync_bucket_keys()
        self._preview_cache = load_preview_cache_file()
        self._preview_lock = threading.Lock()
        self._preview_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="preview"
        )
        self._persist_lock = threading.Lock()
        self._persist_timer: threading.Timer | None = None
        self._cache_save_timer: threading.Timer | None = None
        self._ai_lock = threading.Lock()
        self._ai_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ai"
        )
        self._ensure_ai_state()

    def _ensure_ai_state(self) -> None:
        ai = self.sort_data.setdefault("ai", {})
        ai.setdefault("status", "idle")
        ai.setdefault("summary", "")
        ai.setdefault("bucket_hints", {})
        ai.setdefault("track_suggestions", {})
        ai.setdefault("error", None)

    def ai_status(self) -> str:
        return str(self.sort_data.get("ai", {}).get("status", "idle"))

    def ai_error(self) -> str | None:
        err = self.sort_data.get("ai", {}).get("error")
        return str(err) if err else None

    def playlist_meta(self, key: str) -> dict[str, str]:
        entry = self.sort_data.get("playlists", {}).get(key, {})
        if not isinstance(entry, dict):
            return {"name": "", "description": ""}
        return {
            "name": str(entry.get("name", "")).strip(),
            "description": str(entry.get("description", "")).strip(),
        }

    def bucket_hint(self, key: str) -> dict[str, str] | None:
        meta = self.playlist_meta(key)
        if meta["name"]:
            return None
        hint = self.sort_data.get("ai", {}).get("bucket_hints", {}).get(key)
        if not isinstance(hint, dict):
            return None
        name = str(hint.get("name", "")).strip()
        if not name:
            return None
        return {
            "name": name,
            "description": str(hint.get("description", "")).strip(),
        }

    def track_suggestion_bucket(self, track_id: str | None) -> str | None:
        if not track_id:
            return None
        bucket = self.sort_data.get("ai", {}).get("track_suggestions", {}).get(track_id)
        return str(bucket) if bucket else None

    def _suggestion_label(self, bucket: str) -> str:
        meta = self.playlist_meta(bucket)
        if meta["name"]:
            return meta["name"]
        hint = self.sort_data.get("ai", {}).get("bucket_hints", {}).get(bucket, {})
        if isinstance(hint, dict) and hint.get("name"):
            return str(hint["name"])
        return bucket

    def start_initial_analysis(self) -> bool:
        with self._ai_lock:
            if self.ai_status() == "running":
                return False
            self.sort_data["ai"] = {
                "status": "running",
                "summary": "",
                "bucket_hints": {},
                "track_suggestions": {},
                "error": None,
            }
        self._persist()
        self._ai_executor.submit(self._run_initial_analysis)
        return True

    def _merge_ai_progress(self, partial: dict[str, Any]) -> None:
        with self._ai_lock:
            ai = self.sort_data.setdefault("ai", {})
            ai["status"] = "running"
            if "bucket_hints" in partial:
                ai["bucket_hints"] = partial["bucket_hints"]
            if "summary" in partial:
                ai["summary"] = partial["summary"]
            if "track_suggestions" in partial:
                ai["track_suggestions"] = partial["track_suggestions"]
            if "assign_progress" in partial:
                ai["assign_progress"] = partial["assign_progress"]
        self._persist()

    def _run_initial_analysis(self) -> None:
        try:
            result = run_initial_analysis(
                self.export,
                self.sort_data,
                self.bucket_keys,
                list(self.queue),
                on_progress=self._merge_ai_progress,
            )
            with self._ai_lock:
                self.sort_data["ai"] = result
        except Exception as exc:
            print(f"  AI analysis failed: {exc}", flush=True)
            with self._ai_lock:
                previous = self.sort_data.get("ai", {})
                self.sort_data["ai"] = {
                    "status": "error",
                    "summary": previous.get("summary", ""),
                    "bucket_hints": previous.get("bucket_hints", {}),
                    "track_suggestions": previous.get("track_suggestions", {}),
                    "error": str(exc),
                }
        self._persist()

    def _ai_payload(self) -> dict[str, Any]:
        ai = self.sort_data.get("ai", {})
        payload: dict[str, Any] = {
            "status": ai.get("status", "idle"),
            "error": ai.get("error"),
            "summary": ai.get("summary", ""),
        }
        if ai.get("assign_progress"):
            payload["assign_progress"] = ai["assign_progress"]
        return payload

    def preview_status(self, track_id: str) -> bool | None:
        with self._preview_lock:
            if track_id not in self._preview_cache:
                return None
            return self._preview_cache[track_id] is not None

    def resolve_preview_url(self, track_id: str) -> str | None:
        with self._preview_lock:
            if track_id in self._preview_cache:
                return self._preview_cache[track_id]

        track = self.track_by_id.get(track_id)
        if not track:
            with self._preview_lock:
                self._preview_cache[track_id] = None
            return None

        url = lookup_preview_url(track)
        with self._preview_lock:
            self._preview_cache[track_id] = url
            if url:
                track["preview_url"] = url
        self._schedule_cache_save()
        return url

    def warm_previews(self, track_ids: list[str]) -> None:
        to_warm: list[str] = []
        with self._preview_lock:
            for track_id in track_ids:
                if track_id and track_id not in self._preview_cache:
                    to_warm.append(track_id)
        for track_id in to_warm:
            self._preview_executor.submit(self.resolve_preview_url, track_id)

    def _schedule_cache_save(self) -> None:
        with self._persist_lock:
            if self._cache_save_timer is not None:
                self._cache_save_timer.cancel()
            self._cache_save_timer = threading.Timer(2.0, self._flush_preview_cache)
            self._cache_save_timer.daemon = True
            self._cache_save_timer.start()

    def _flush_preview_cache(self) -> None:
        with self._preview_lock:
            snapshot = dict(self._preview_cache)
        save_preview_cache_file(snapshot)

    def _track_payload(self, track: dict[str, Any], meta: list[str]) -> dict[str, Any]:
        track_id = track["spotify_id"]
        return {
            "spotify_id": track_id,
            "title": track.get("title") or "Unknown title",
            "artists": format_artists(track),
            "meta": meta,
            "has_preview": self.preview_status(track_id),
        }

    def current_track_id(self) -> str | None:
        return self.queue[0] if self.queue else None

    def _remove_track(self, track_id: str) -> str | None:
        unassigned = self.sort_data.setdefault("unassigned", [])
        if track_id in unassigned:
            unassigned.remove(track_id)
            return "__unassigned__"
        for key, ids in self.sort_data.get("buckets", {}).items():
            if track_id in ids:
                ids.remove(track_id)
                return key
        return None

    def _persist(self) -> None:
        with self._persist_lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
            self._persist_timer = threading.Timer(PERSIST_DEBOUNCE_S, self._flush_persist)
            self._persist_timer.daemon = True
            self._persist_timer.start()

    def _flush_persist(self) -> None:
        save_sort(self.sort_path, self.sort_data)

    def assign(self, bucket: str) -> None:
        track_id = self.current_track_id()
        if not track_id or bucket not in self.sort_data.get("buckets", {}):
            return
        previous = self._remove_track(track_id)
        self.sort_data["buckets"][bucket].append(track_id)
        self.history.append((track_id, previous))
        self.queue.pop(0)
        self._persist()

    def skip(self) -> None:
        track_id = self.current_track_id()
        if not track_id:
            return
        previous = self._remove_track(track_id)
        self.history.append((track_id, previous))
        self.queue.pop(0)
        self.queue.append(track_id)
        self._persist()

    def undo(self) -> None:
        if not self.history:
            return
        track_id, previous_bucket = self.history.pop()
        if track_id in self.queue:
            self.queue.remove(track_id)
        self._remove_track(track_id)
        if previous_bucket and previous_bucket != "__unassigned__":
            self.sort_data["buckets"][previous_bucket].append(track_id)
        elif previous_bucket == "__unassigned__":
            self.sort_data.setdefault("unassigned", []).append(track_id)
        self.queue.insert(0, track_id)
        self._persist()

    def reset_assignments(self) -> None:
        """Clear all track assignments and rebuild the queue from the export."""
        for ids in self.sort_data.get("buckets", {}).values():
            ids.clear()
        self.sort_data["unassigned"] = []
        self.history.clear()
        self.queue = build_queue(self.sort_data, self.export)
        self.sort_data["ai"] = {
            "status": "idle",
            "summary": "",
            "bucket_hints": {},
            "track_suggestions": {},
            "error": None,
        }
        self._persist()

    def _sync_bucket_keys(self) -> None:
        self.bucket_keys = playlist_keys_from_data(self.sort_data)

    def _recent_for_ids(self, ids: list[str]) -> list[dict[str, str]]:
        recent: list[dict[str, str]] = []
        for track_id in ids[-3:]:
            track = self.track_by_id.get(track_id)
            if track:
                recent.append(
                    {
                        "title": track.get("title") or "Unknown title",
                        "artists": format_artists(track),
                    }
                )
        return recent

    def _bucket_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for key in self.bucket_keys:
            ids = self.sort_data["buckets"].get(key, [])
            meta = self.playlist_meta(key)
            entry: dict[str, Any] = {
                "type": "playlist",
                "key": key,
                "name": meta["name"],
                "description": meta["description"],
                "count": len(ids),
                "recent": self._recent_for_ids(ids),
            }
            hint = self.bucket_hint(key)
            if hint:
                entry["ai_hint"] = hint
            entries.append(entry)
        return entries

    def state(self) -> dict[str, Any]:
        total = len(self.export.get("tracks", []))
        remaining = len(self.queue)
        done = total - remaining
        track_id = self.current_track_id()
        buckets = self._bucket_entries()

        if not track_id:
            return {
                "done": True,
                "progress": f"Done — {done} of {total} tracks processed",
                "track": None,
                "buckets": buckets,
                "can_undo": bool(self.history),
                "ai": self._ai_payload(),
                "suggested_bucket": None,
            }

        track = self.track_by_id.get(track_id)
        if not track:
            self.queue.pop(0)
            self._persist()
            return self.state()

        meta: list[str] = []
        if album := track.get("album"):
            meta.append(f"Album: {album}")
        if mood := track.get("mood"):
            meta.append(f"Mood: {mood}")
        if bpm := track.get("bpm"):
            meta.append(f"BPM: {bpm:g}")
        if key_sig := track.get("key"):
            meta.append(f"Key: {key_sig}")
        genres = track.get("genres") or []
        if genres:
            meta.append(f"Genres: {', '.join(genres[:8])}")
        audio = track.get("audio") or {}
        bits = []
        for label, field in (
            ("energy", "energy"),
            ("valence", "valence"),
            ("dance", "danceability"),
        ):
            val = audio.get(field)
            if val is not None:
                bits.append(f"{label} {val:.2f}")
        if bits:
            meta.append(" · ".join(bits))

        self.warm_previews([track_id, *self.queue[1:PREVIEW_WARM_AHEAD]])

        suggested = self.track_suggestion_bucket(track_id)
        payload: dict[str, Any] = {
            "done": False,
            "progress": f"Track {done + 1} of {total} · {remaining} remaining",
            "track": self._track_payload(track, meta),
            "buckets": buckets,
            "can_undo": bool(self.history),
            "ai": self._ai_payload(),
            "suggested_bucket": suggested,
        }
        if suggested:
            payload["suggestion_label"] = self._suggestion_label(suggested)
        return payload


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spotarbiter — sort songs</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f12;
      color: #e8e8ec;
      min-height: 100vh;
    }
    .wrap {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0;
      min-height: 100vh;
    }
    @media (max-width: 800px) {
      .wrap { grid-template-columns: 1fr; }
    }
    .track-panel {
      padding: 2rem;
      border-right: 1px solid #2a2a32;
      display: flex;
      flex-direction: column;
    }
    .progress { color: #888; font-size: 0.9rem; margin-bottom: 1.5rem; }
    .title-row {
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
      margin-bottom: 0.35rem;
    }
    .title-row h1 {
      font-size: 1.75rem;
      margin: 0;
      line-height: 1.2;
      flex: 1;
    }
    .btn-play {
      flex-shrink: 0;
      width: 2.5rem;
      height: 2.5rem;
      padding: 0;
      border-radius: 50%;
      background: #1db954;
      color: #0f0f12;
      font-size: 0.95rem;
      line-height: 1;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .btn-play:hover:not(:disabled) { background: #1ed760; }
    .btn-play:disabled { background: #2a2a32; color: #555; cursor: default; }
    .btn-play.playing { background: #1a1a22; color: #1db954; border: 1px solid #1db954; }
    .track-suggestion {
      font-size: 0.95rem;
      color: #7a8a99;
      margin: -0.75rem 0 1.25rem;
      padding: 0.5rem 0.75rem;
      background: #141820;
      border-radius: 8px;
      border: 1px solid #2a3540;
    }
    .track-suggestion strong { color: #9ab4c8; font-weight: 600; }
    .artists { font-size: 1.1rem; color: #aaa; margin-bottom: 1.5rem; }
    .meta { color: #999; line-height: 1.6; font-size: 0.95rem; }
    .meta p { margin: 0.25rem 0; }
    .actions {
      margin-top: auto;
      padding-top: 2rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
    }
    button {
      font: inherit;
      cursor: pointer;
      border: none;
      border-radius: 8px;
      padding: 0.6rem 1rem;
    }
    .btn-secondary { background: #2a2a32; color: #ccc; }
    .btn-secondary:hover { background: #35353f; }
    .btn-secondary:disabled { opacity: 0.4; cursor: default; }
    .btn-danger { background: #3a1a1a; color: #e8a0a0; }
    .btn-danger:hover { background: #4a2222; color: #f0b0b0; }
    .btn-danger.armed {
      background: #5a2020;
      color: #fff;
      outline: 2px solid #e84545;
      outline-offset: 2px;
    }
    .buckets-panel {
      padding: 1.5rem;
      overflow-y: auto;
      max-height: 100vh;
    }
    .buckets-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 1rem;
      flex-wrap: wrap;
    }
    .buckets-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }
    .buckets-panel h2 {
      margin: 0;
      font-size: 1rem;
      color: #aaa;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .btn-analysis {
      background: #1a2a3a;
      color: #9ab4c8;
      padding: 0.45rem 0.75rem;
      font-size: 0.85rem;
    }
    .btn-analysis:hover:not(:disabled) { background: #243548; color: #c8dae8; }
    .btn-analysis:disabled { opacity: 0.55; cursor: default; }
    .ai-status {
      margin: -0.5rem 0 1rem;
      padding: 0.6rem 0.75rem;
      border-radius: 8px;
      font-size: 0.85rem;
      line-height: 1.45;
    }
    .ai-status.running {
      background: #141a22;
      border: 1px solid #3a5a7a;
      color: #9ab4c8;
    }
    .ai-status.ready {
      background: #141a16;
      border: 1px solid #1db95455;
      color: #8fd4a8;
    }
    .ai-status.error {
      background: #221418;
      border: 1px solid #8b2e2e;
      color: #e8a0a0;
    }
    .ai-hint {
      margin: 0.35rem 0 0;
      font-size: 0.8rem;
      color: #666;
      line-height: 1.4;
    }
    .ai-hint .hint-name { color: #888; font-weight: 500; }
    .bucket-row.ai-suggested {
      border-color: #3a5a7a;
      background: #141a22;
      box-shadow: 0 0 0 1px #3a5a7a40;
    }
    .bucket-list { display: flex; flex-direction: column; gap: 0.5rem; }
    .bucket-row {
      display: block;
      width: 100%;
      text-align: left;
      background: #1a1a22;
      color: #e8e8ec;
      border: 1px solid #2a2a32;
      border-radius: 10px;
      padding: 0.85rem 1rem;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
    }
    .bucket-row:hover {
      border-color: #1db954;
      background: #141a16;
    }
    .row-header {
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
    }
    .playlist-copy {
      flex: 1;
      min-width: 0;
    }
    .playlist-name {
      font-size: 1rem;
      font-weight: 600;
      line-height: 1.3;
    }
    .playlist-desc {
      margin: 0.35rem 0 0;
      font-size: 0.82rem;
      color: #888;
      line-height: 1.45;
    }
    .bucket-row .count {
      font-size: 0.85rem;
      color: #1db954;
      flex-shrink: 0;
      min-width: 2rem;
      text-align: right;
    }
    .recent {
      margin: 0.5rem 0 0;
      padding: 0;
      list-style: none;
      font-size: 0.8rem;
      color: #888;
      line-height: 1.45;
    }
    .recent li { margin: 0.15rem 0; }
    .hint {
      position: fixed;
      bottom: 0.75rem;
      left: 50%;
      transform: translateX(-50%);
      font-size: 0.75rem;
      color: #555;
    }
    .done h1 { color: #1db954; }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="track-panel" id="track-panel">
      <div class="progress" id="progress">Loading…</div>
      <div class="title-row">
        <button type="button" class="btn-play" id="play-btn" hidden title="Play preview">▶</button>
        <h1 id="title">—</h1>
      </div>
      <audio id="preview-audio" preload="none"></audio>
      <div class="artists" id="artists"></div>
      <p class="track-suggestion" id="track-suggestion" hidden></p>
      <div class="meta" id="meta"></div>
      <div class="actions">
        <button class="btn-secondary" id="skip-btn">Skip (S)</button>
        <button class="btn-secondary" id="undo-btn" disabled>Undo (U)</button>
        <button type="button" class="btn-danger" id="reset-btn">Reset sort</button>
      </div>
    </section>
    <section class="buckets-panel">
      <div class="buckets-header">
        <h2>Playlists</h2>
        <div class="buckets-actions">
          <button type="button" class="btn-analysis" id="initial-analysis-btn">
            Initial Analysis
          </button>
        </div>
      </div>
      <p id="ai-status" class="ai-status" hidden></p>
      <div class="bucket-list" id="buckets"></div>
    </section>
  </div>
  <p class="hint" id="keyboard-hint">Keys: 1–9 assign · S skip · U undo</p>
  <script>
    async function api(path, method = "GET") {
      const r = await fetch(path, { method });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    }

    const previewAudio = document.getElementById("preview-audio");
    const playBtn = document.getElementById("play-btn");
    let currentTrackId = null;
    let playlistKeys = [];
    let suggestedBucket = null;
    let aiPollTimer = null;
    let resetConfirmPending = false;
    let resetConfirmTimer = null;
    let lastAiErrorShown = null;

    function stopPreview() {
      previewAudio.pause();
      previewAudio.removeAttribute("src");
      playBtn.textContent = "▶";
      playBtn.classList.remove("playing");
    }

    async function warmPreview(trackId) {
      try {
        const r = await api(
          "/api/preview?track_id=" + encodeURIComponent(trackId)
        );
        if (trackId !== currentTrackId) return;
        playBtn.disabled = !r.has_preview;
        playBtn.title = r.has_preview
          ? "Play 30s preview"
          : "No preview found for this track";
      } catch (_) {
        /* ignore background warm failures */
      }
    }

    function updatePreviewUi(track) {
      if (!track) {
        stopPreview();
        playBtn.hidden = true;
        currentTrackId = null;
        return;
      }
      if (track.spotify_id !== currentTrackId) {
        stopPreview();
        currentTrackId = track.spotify_id;
      }
      playBtn.hidden = false;
      if (track.has_preview === null) {
        playBtn.disabled = false;
        playBtn.title = "Play 30s preview";
        warmPreview(track.spotify_id);
      } else {
        playBtn.disabled = !track.has_preview;
        playBtn.title = track.has_preview
          ? "Play 30s preview"
          : "No preview found for this track";
      }
    }

    playBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!currentTrackId || playBtn.disabled) return;
      if (!previewAudio.paused && previewAudio.src) {
        stopPreview();
        return;
      }
      playBtn.disabled = true;
      playBtn.textContent = "…";
      const audioUrl =
        "/api/preview/audio?track_id=" +
        encodeURIComponent(currentTrackId) +
        "&_=" +
        Date.now();
      previewAudio.src = audioUrl;
      try {
        await previewAudio.play();
        playBtn.textContent = "⏸";
        playBtn.classList.add("playing");
        playBtn.disabled = false;
      } catch (err) {
        console.error("Preview playback failed:", err);
        stopPreview();
        playBtn.disabled = true;
        playBtn.title = "Could not play preview";
      }
    });

    previewAudio.addEventListener("ended", () => {
      stopPreview();
    });

    previewAudio.addEventListener("error", () => {
      console.error("Preview audio error");
      stopPreview();
      playBtn.disabled = true;
      playBtn.title = "Could not load preview";
    });

    function shortenAiError(message) {
      if (!message) return "Analysis failed";
      const text = String(message);
      const match = text.match(/'message':\s*"([^"]+)"/);
      if (match) return match[1];
      return text.length > 220 ? text.slice(0, 217) + "…" : text;
    }

    function updateAiStatusBanner(ai) {
      const el = document.getElementById("ai-status");
      if (!el) return;
      if (!ai || ai.status === "idle") {
        el.hidden = true;
        el.textContent = "";
        el.className = "ai-status";
        return;
      }
      el.hidden = false;
      el.className = "ai-status " + ai.status;
      if (ai.status === "running") {
        const batchNote = ai.assign_progress
          ? " Assign batch " + ai.assign_progress + "."
          : "";
        el.textContent =
          "Analysis running — assigning tracks to your Spotify playlists in batches." +
          batchNote +
          " Watch the terminal for LLM progress (each batch may take 1–3 minutes).";
        return;
      }
      if (ai.status === "ready") {
        el.textContent = ai.summary
          ? "Analysis ready — " + ai.summary
          : "Analysis ready — track suggestions shown for the current song.";
        return;
      }
      if (ai.status === "error") {
        el.textContent = shortenAiError(ai.error);
      }
    }

    function updateAnalysisUi(ai) {
      const btn = document.getElementById("initial-analysis-btn");
      if (!btn) return;
      updateAiStatusBanner(ai);
      if (!ai || ai.status === "idle") {
        btn.disabled = false;
        btn.textContent = "Initial Analysis";
        lastAiErrorShown = null;
        stopAiPoll();
        return;
      }
      if (ai.status === "running") {
        btn.disabled = true;
        btn.textContent = "Analyzing…";
        startAiPoll();
        return;
      }
      stopAiPoll();
      btn.disabled = false;
      if (ai.status === "ready") {
        btn.textContent = "Re-run Analysis";
        return;
      }
      if (ai.status === "error") {
        btn.textContent = "Retry Analysis";
        if (ai.error) {
          console.error("Analysis failed:", ai.error);
        }
      }
    }

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function render(data) {
      document.getElementById("progress").textContent = data.progress;
      const panel = document.getElementById("track-panel");
      panel.classList.toggle("done", data.done);

      const suggestionEl = document.getElementById("track-suggestion");
      if (data.done || !data.track) {
        document.getElementById("title").textContent =
          data.done ? "All tracks sorted" : "—";
        document.getElementById("artists").textContent = "";
        suggestionEl.hidden = true;
        document.getElementById("meta").innerHTML = "";
        document.getElementById("skip-btn").disabled = true;
        updatePreviewUi(null);
      } else {
        document.getElementById("title").textContent = data.track.title;
        document.getElementById("artists").textContent = data.track.artists;
        if (data.suggested_bucket && data.suggestion_label) {
          suggestionEl.hidden = false;
          suggestionEl.innerHTML =
            "Suggested: <strong>" +
            escapeHtml(data.suggestion_label) +
            "</strong>";
        } else {
          suggestionEl.hidden = true;
        }
        document.getElementById("meta").innerHTML = (data.track.meta || [])
          .map((line) => `<p>${escapeHtml(line)}</p>`).join("");
        document.getElementById("skip-btn").disabled = false;
        updatePreviewUi(data.track);
      }

      document.getElementById("undo-btn").disabled = !data.can_undo;
      updateAnalysisUi(data.ai);

      suggestedBucket = data.suggested_bucket || null;
      const playlistBuckets = (data.buckets || []).filter((b) => b.type === "playlist");
      playlistKeys = playlistBuckets.map((b) => b.key);
      updateKeyboardHint(playlistBuckets);
      renderBuckets(data.buckets || [], suggestedBucket);
    }

    function startAiPoll() {
      if (aiPollTimer) return;
      aiPollTimer = setInterval(() => refresh().catch(console.error), 2500);
    }

    function stopAiPoll() {
      if (!aiPollTimer) return;
      clearInterval(aiPollTimer);
      aiPollTimer = null;
    }

    function updateKeyboardHint(buckets) {
      const parts = [];
      for (let i = 0; i < Math.min(9, buckets.length); i++) {
        const label = buckets[i].name || "playlist";
        parts.push(`${i + 1} = ${label}`);
      }
      if (buckets.length >= 10) {
        const label = buckets[9].name || "playlist";
        parts.push(`0 = ${label}`);
      }
      const shortcuts = parts.length ? parts.join(" · ") : "—";
      document.getElementById("keyboard-hint").textContent =
        `Keys: ${shortcuts} · S skip · U undo`;
    }

    function createBucketRow(b) {
      const row = document.createElement("div");
      row.className = "bucket-row";
      row.dataset.key = b.key;
      row.dataset.type = b.type;

      const header = document.createElement("div");
      header.className = "row-header";

      const count = document.createElement("span");
      count.className = "count";
      count.textContent = b.count ?? 0;

      const copy = document.createElement("div");
      copy.className = "playlist-copy";

      const name = document.createElement("div");
      name.className = "playlist-name";
      name.textContent = b.name || "Untitled playlist";
      copy.appendChild(name);

      if (b.description) {
        const desc = document.createElement("p");
        desc.className = "playlist-desc";
        desc.textContent = b.description;
        copy.appendChild(desc);
      }

      header.appendChild(copy);
      header.appendChild(count);

      row.appendChild(header);

      const hint = document.createElement("div");
      hint.className = "ai-hint";
      hint.hidden = true;
      row.appendChild(hint);

      row.addEventListener("click", () => assign(row.dataset.key));
      updateAiHint(row, b);
      return row;
    }

    function updateAiHint(row, b) {
      const hint = row.querySelector(".ai-hint");
      if (!hint) return;
      if (b.ai_hint && b.ai_hint.name) {
        hint.hidden = false;
        const desc = b.ai_hint.description
          ? " — " + escapeHtml(b.ai_hint.description)
          : "";
        hint.innerHTML =
          '<span class="hint-name">' +
          escapeHtml(b.ai_hint.name) +
          "</span>" +
          desc;
      } else {
        hint.hidden = true;
        hint.textContent = "";
      }
    }

    function updateBucketRow(row, b, suggestedKey) {
      row.dataset.key = b.key;
      row.dataset.type = b.type;
      const base = "bucket-row";
      row.className =
        suggestedKey && b.key === suggestedKey ? base + " ai-suggested" : base;
      row.querySelector(".count").textContent = b.count;
      row.querySelector(".playlist-name").textContent = b.name || "Untitled playlist";
      let desc = row.querySelector(".playlist-desc");
      if (b.description) {
        if (!desc) {
          desc = document.createElement("p");
          desc.className = "playlist-desc";
          row.querySelector(".playlist-copy").appendChild(desc);
        }
        desc.textContent = b.description;
      } else if (desc) {
        desc.remove();
      }

      let ul = row.querySelector(".recent");
      if (b.recent && b.recent.length) {
        if (!ul) {
          ul = document.createElement("ul");
          ul.className = "recent";
          row.appendChild(ul);
        }
        ul.innerHTML = b.recent
          .map((t) => `<li>${escapeHtml(t.title)} — ${escapeHtml(t.artists)}</li>`)
          .join("");
      } else if (ul) {
        ul.remove();
      }
      updateAiHint(row, b);
    }

    function renderBuckets(buckets, suggestedKey) {
      const list = document.getElementById("buckets");

      while (list.children.length < buckets.length) {
        list.appendChild(createBucketRow(buckets[list.children.length]));
      }
      while (list.children.length > buckets.length) {
        list.lastChild.remove();
      }

      buckets.forEach((b, index) => {
        let row = list.children[index];
        if (!row || row.dataset.type !== b.type) {
          row = createBucketRow(b);
          if (list.children[index]) {
            list.replaceChild(row, list.children[index]);
          } else {
            list.appendChild(row);
          }
        } else {
          updateBucketRow(row, b, suggestedKey);
        }
      });
    }

    async function refresh() {
      try {
        render(await api("/api/state"));
      } catch (err) {
        console.error("Failed to load state:", err);
        const progress = document.getElementById("progress");
        if (progress) {
          progress.textContent = "Failed to load — is the sort server running?";
        }
        const statusEl = document.getElementById("ai-status");
        if (statusEl) {
          statusEl.hidden = false;
          statusEl.className = "ai-status error";
          statusEl.textContent = err.message || String(err);
        }
      }
    }

    async function assign(key) {
      await api("/api/assign?bucket=" + encodeURIComponent(key), "POST");
      await refresh();
    }

    async function skip() {
      await api("/api/skip", "POST");
      await refresh();
    }

    async function undo() {
      await api("/api/undo", "POST");
      await refresh();
    }

    async function runInitialAnalysis() {
      const btn = document.getElementById("initial-analysis-btn");
      btn.disabled = true;
      btn.textContent = "Analyzing…";
      try {
        await api("/api/initial-analysis", "POST");
        await refresh();
      } catch (err) {
        console.error(err);
        alert(err.message || "Analysis failed");
        btn.disabled = false;
        btn.textContent = "Initial Analysis";
      }
    }

    function disarmResetButton() {
      const btn = document.getElementById("reset-btn");
      resetConfirmPending = false;
      if (resetConfirmTimer) {
        clearTimeout(resetConfirmTimer);
        resetConfirmTimer = null;
      }
      btn.textContent = "Reset sort";
      btn.classList.remove("armed");
    }

    async function resetSort() {
      const btn = document.getElementById("reset-btn");
      if (!resetConfirmPending) {
        resetConfirmPending = true;
        btn.textContent = "Click again to confirm";
        btn.classList.add("armed");
        if (resetConfirmTimer) clearTimeout(resetConfirmTimer);
        resetConfirmTimer = setTimeout(disarmResetButton, 5000);
        return;
      }
      disarmResetButton();
      btn.disabled = true;
      btn.textContent = "Resetting…";
      try {
        await api("/api/reset", "POST");
        await refresh();
      } catch (err) {
        console.error(err);
        alert(err.message || "Reset failed");
      } finally {
        btn.disabled = false;
        btn.textContent = "Reset sort";
      }
    }

    document.getElementById("skip-btn").addEventListener("click", skip);
    document.getElementById("undo-btn").addEventListener("click", undo);
    document.getElementById("reset-btn").addEventListener("click", resetSort);
    document
      .getElementById("initial-analysis-btn")
      .addEventListener("click", runInitialAnalysis);

    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, textarea")) return;
      if (e.key === "s" || e.key === "S") { e.preventDefault(); skip(); }
      if (e.key === "u" || e.key === "U") { e.preventDefault(); undo(); }
      if (e.key >= "1" && e.key <= "9") {
        const idx = parseInt(e.key, 10) - 1;
        if (idx < playlistKeys.length) {
          e.preventDefault();
          assign(playlistKeys[idx]);
        }
      }
      if (e.key === "0" && playlistKeys.length >= 10) {
        e.preventDefault();
        assign(playlistKeys[9]);
      }
    });

    refresh().catch(console.error);
  </script>
</body>
</html>"""


def make_handler(session: SortSession):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json_response(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _query_params(self) -> dict[str, str]:
            parsed = urlparse(self.path)
            params: dict[str, str] = {}
            if parsed.query:
                for part in parsed.query.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[unquote(k)] = unquote(v)
            return params

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            params = self._query_params()
            if path == "/api/preview":
                track_id = params.get("track_id", "")
                url = session.resolve_preview_url(track_id) if track_id else None
                self._json_response({"preview_url": url, "has_preview": url is not None})
                return
            if path == "/api/preview/audio":
                track_id = params.get("track_id", "")
                preview_url = session.resolve_preview_url(track_id) if track_id else None
                if not preview_url:
                    self.send_error(404, "No preview for this track")
                    return
                request = urllib.request.Request(
                    preview_url,
                    headers={"User-Agent": "spotarbiter/1.0"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=15) as remote:
                        data = remote.read()
                        content_type = remote.headers.get("Content-Type", "audio/mpeg")
                except Exception:
                    self.send_error(502, "Preview source unavailable")
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/state":
                self._json_response(session.state())
                return
            if path in ("/", "/index.html"):
                body = HTML_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            params: dict[str, str] = {}
            if parsed.query:
                for part in parsed.query.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[unquote(k)] = unquote(v)

            if parsed.path == "/api/assign":
                bucket = params.get("bucket")
                if bucket:
                    session.assign(bucket)
                self._json_response(session.state())
                return
            if parsed.path == "/api/skip":
                session.skip()
                self._json_response(session.state())
                return
            if parsed.path == "/api/undo":
                session.undo()
                self._json_response(session.state())
                return
            if parsed.path == "/api/initial-analysis":
                if not session.start_initial_analysis():
                    self._json_response(
                        {"ok": False, "message": "Analysis already running"},
                        status=409,
                    )
                    return
                self._json_response(session.state())
                return
            if parsed.path == "/api/reset":
                session.reset_assignments()
                self._json_response(session.state())
                return
            self.send_error(404)

    return Handler




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--sort", type=Path, default=DEFAULT_SORT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear all assignments before starting",
    )
    parser.add_argument(
        "--folder",
        default=DEFAULT_SORTER_FOLDER,
        help="Spotify playlist folder to load buckets from (default: Sorter)",
    )
    parser.add_argument(
        "--spotify-user",
        default=None,
        help="Spotify account id when multiple accounts exist in the local cache",
    )
    args = parser.parse_args()

    if not args.export.is_file():
        print(f"Missing export: {args.export}", file=sys.stderr)
        return 1

    configure_spotipy_env()
    try:
        spotify = get_spotify_client()
        folder_playlists = fetch_folder_playlists(
            spotify, args.folder, account=args.spotify_user
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    export = load_json(args.export)
    sort_data = load_or_create_sort(args.sort)
    bucket_keys = sync_sort_playlists(sort_data, folder_playlists)
    if not bucket_keys:
        print(
            f'No playlists found in folder "{args.folder}". Add playlists in Spotify and try again.',
            file=sys.stderr,
        )
        return 1

    if args.reset:
        for ids in sort_data.get("buckets", {}).values():
            ids.clear()
        sort_data["unassigned"] = []
        sort_data["ai"] = {
            "status": "idle",
            "summary": "",
            "bucket_hints": {},
            "track_suggestions": {},
            "error": None,
        }
    else:
        export_ids = set(all_track_ids(export))
        imported = import_playlist_tracks(
            sort_data, folder_playlists, spotify, export_ids
        )
        if imported:
            print(f"Loaded {imported} tracks already in your Spotify playlists.")
    save_sort(args.sort, sort_data)

    queue = build_queue(sort_data, export)
    if not queue:
        print("No tracks left to sort.")
        return 0

    session = SortSession(export, sort_data, args.sort, queue)
    url = f"http://127.0.0.1:{args.port}/"
    server = HTTPServer(("127.0.0.1", args.port), make_handler(session))
    print(f"Sorting UI: {url}")
    print(
        f'{len(bucket_keys)} playlists from folder "{args.folder}" — '
        f"{len(queue)} tracks in queue — saves to {args.sort}"
    )
    print("Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        session._flush_persist()
        session._flush_preview_cache()
        session._preview_executor.shutdown(wait=False)
        session._ai_executor.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
