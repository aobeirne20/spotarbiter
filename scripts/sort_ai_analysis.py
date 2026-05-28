"""LLM initial analysis for the manual sort GUI (bucket names + per-track suggestions)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable

from llm_client import call_llm, default_model, get_openai_client
from playlist_plan import (
    assigned_indices,
    dedupe_compact_assignments,
    indexed_tracks_for_prompt,
    merge_compact_assignments,
    missing_indices,
)

ASSIGN_BATCH_SIZE = 80
MAX_REPAIR_PASSES = 2
# Full library in the name pass exceeds model context (~1.6k+ tracks).
NAME_PROMPT_MAX_TRACKS = 350
NAME_PROMPT_SAMPLE_SEED = 42

NAME_RAW_PATH = Path(__file__).resolve().parents[1] / "data" / "last_gui_name_suggestions.txt"
ASSIGN_RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "gui_assign_batches"

NAME_SYSTEM_PROMPT = """\
You are helping a user organize Liked Songs into Spotify playlist buckets in a sorting UI.

Some buckets already have names from Spotify. Other buckets are blank and need your suggestions.
Study the track library and propose a coherent name and one-sentence description for every
blank bucket. Keep named buckets as-is (do not rename them).

Return ONLY JSON:
{
  "summary": "brief note on how you themed the blank buckets",
  "bucket_suggestions": [
    {"bucket": "playlist_id", "name": "evocative playlist name", "description": "what belongs here"}
  ]
}

Rules:
- Include ONLY entries for buckets where is_blank is true in the prompt.
- Use exact bucket ids from the prompt (Spotify playlist ids).
- Suggested names must be unique and must not duplicate any user_name in the prompt.
- Do NOT assign tracks yet.
"""

ASSIGN_SYSTEM_PROMPT = """\
Assign tracks to playlist buckets in a sorting UI.

Tracks use "index" (0-based) within this batch only. Reference tracks by index only.

Return ONLY JSON:
{
  "assignments": [{"bucket": "playlist_id", "track_index": 0}],
  "unassigned_indices": []
}

Rules:
- bucket must be an exact bucket id from suggested_playlists.
- Every track index in this batch (0 through track_count-1) must appear exactly once in
  assignments or unassigned_indices.
- Use unassigned_indices when a track does not fit any playlist well.
"""

REPAIR_SYSTEM_PROMPT = """\
Complete a partial track-to-bucket assignment for one batch.

Return ONLY JSON:
{
  "assignments": [{"bucket": "3", "track_index": 0}],
  "unassigned_indices": []
}

Rules:
- Assign ONLY indices listed in missing_indices.
- Use only bucket ids from existing_playlists.
"""


def build_bucket_catalog(
    sort_data: dict[str, Any],
    bucket_keys: list[str],
) -> dict[str, Any]:
    playlists = sort_data.get("playlists", {})
    rows: list[dict[str, Any]] = []
    for key in bucket_keys:
        entry = playlists.get(key, {}) if isinstance(playlists, dict) else {}
        user_name = str(entry.get("name", "")).strip() if isinstance(entry, dict) else ""
        description = (
            str(entry.get("description", "")).strip() if isinstance(entry, dict) else ""
        )
        rows.append(
            {
                "bucket": key,
                "user_name": user_name or None,
                "description": description or None,
                "is_blank": not user_name,
            }
        )
    blank_count = sum(1 for row in rows if row["is_blank"])
    return {
        "playlist_slots": rows,
        "blank_slot_count": blank_count,
        "named_slot_count": len(rows) - blank_count,
        "total_playlist_slots": len(rows),
    }


def sample_tracks_for_name_prompt(
    tracks: list[dict[str, Any]],
    *,
    max_tracks: int = NAME_PROMPT_MAX_TRACKS,
) -> tuple[list[dict[str, Any]], bool]:
    """Return a representative subset when the library is too large for one prompt."""
    if len(tracks) <= max_tracks:
        return tracks, False
    rng = random.Random(NAME_PROMPT_SAMPLE_SEED)
    return rng.sample(tracks, max_tracks), True


def build_name_user_prompt(
    tracks: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> str:
    sample, is_sample = sample_tracks_for_name_prompt(tracks)
    payload = {
        "playlist_slots": catalog["playlist_slots"],
        "blank_slot_count": catalog["blank_slot_count"],
        "named_slot_count": catalog["named_slot_count"],
        "track_count": len(tracks),
        "tracks_are_sample": is_sample,
        "sample_size": len(sample),
        "tracks": indexed_tracks_for_prompt(sample),
    }
    note = (
        " (tracks below are a random sample of the library; theme blank slots for the "
        "full collection)"
        if is_sample
        else ""
    )
    return (
        f"Propose playlist names and descriptions for every blank slot.{note}\n\n"
        f"{json.dumps(payload, separators=(',', ':'))}"
    )


def normalize_bucket_suggestions(
    raw: dict[str, Any],
    bucket_keys: list[str],
    sort_data: dict[str, Any],
) -> dict[str, dict[str, str]]:
    playlists = sort_data.get("playlists", {})
    used_names = set()
    for key in bucket_keys:
        entry = playlists.get(key, {}) if isinstance(playlists, dict) else {}
        name = str(entry.get("name", "")).strip() if isinstance(entry, dict) else ""
        if name:
            used_names.add(name.lower())
    hints: dict[str, dict[str, str]] = {}
    valid_keys = set(bucket_keys)

    for entry in raw.get("bucket_suggestions", []):
        if not isinstance(entry, dict):
            continue
        bucket = str(entry.get("bucket", "")).strip()
        if bucket not in valid_keys:
            continue
        entry = playlists.get(bucket, {}) if isinstance(playlists, dict) else {}
        if str(entry.get("name", "")).strip() if isinstance(entry, dict) else "":
            continue
        name = str(entry.get("name", "")).strip()
        description = str(entry.get("description", "")).strip()
        if not name:
            continue
        lower = name.lower()
        if lower in used_names:
            continue
        used_names.add(lower)
        hints[bucket] = {"name": name, "description": description}
    return hints


def playlist_definitions_for_assign(
    sort_data: dict[str, Any],
    bucket_keys: list[str],
    bucket_hints: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    playlists = sort_data.get("playlists", {})
    definitions: list[dict[str, str]] = []
    for key in bucket_keys:
        entry = playlists.get(key, {}) if isinstance(playlists, dict) else {}
        user_name = str(entry.get("name", "")).strip() if isinstance(entry, dict) else ""
        user_description = (
            str(entry.get("description", "")).strip() if isinstance(entry, dict) else ""
        )
        hint = bucket_hints.get(key, {})
        name = user_name or str(hint.get("name", "")).strip()
        description = user_description or str(hint.get("description", "")).strip()
        if name and not description:
            description = f"Playlist: {name}"
        definitions.append(
            {
                "bucket": key,
                "name": name,
                "description": description,
            }
        )
    return definitions


def build_assign_user_prompt(
    batch_tracks: list[dict[str, Any]],
    definitions: list[dict[str, str]],
) -> str:
    payload = {
        "track_count": len(batch_tracks),
        "suggested_playlists": definitions,
        "tracks": indexed_tracks_for_prompt(batch_tracks),
    }
    return (
        "Assign each track in this batch to one bucket.\n\n"
        f"{json.dumps(payload, separators=(',', ':'))}"
    )


def build_repair_user_prompt(
    batch_tracks: list[dict[str, Any]],
    definitions: list[dict[str, str]],
    missing: list[int],
) -> str:
    indexed = indexed_tracks_for_prompt(batch_tracks)
    return json.dumps(
        {
            "existing_playlists": definitions,
            "missing_indices": missing,
            "missing_tracks": [indexed[i] for i in missing],
        },
        separators=(",", ":"),
    )


def valid_bucket_ids(definitions: list[dict[str, str]]) -> set[str]:
    return {
        str(defn["bucket"])
        for defn in definitions
        if isinstance(defn, dict) and defn.get("bucket")
    }


def apply_batch_assignments(
    raw: dict[str, Any],
    batch_tracks: list[dict[str, Any]],
    allowed_buckets: set[str],
) -> dict[str, str]:
    suggestions: dict[str, str] = {}
    batch_ids = [t["spotify_id"] for t in batch_tracks]
    for assignment in raw.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        index = assignment.get("track_index")
        bucket = str(assignment.get("bucket", "")).strip()
        if not isinstance(index, int) or index < 0 or index >= len(batch_ids):
            continue
        if bucket not in allowed_buckets:
            continue
        suggestions[batch_ids[index]] = bucket
    return suggestions


def repair_batch(
    client,
    *,
    model: str,
    batch_tracks: list[dict[str, Any]],
    definitions: list[dict[str, str]],
    compact: dict[str, Any],
    raw_path: Path | None,
) -> dict[str, Any]:
    missing = missing_indices(compact, len(batch_tracks))
    if not missing:
        return compact
    patch = call_llm(
        client,
        model=model,
        system=REPAIR_SYSTEM_PROMPT,
        user=build_repair_user_prompt(batch_tracks, definitions, missing),
        raw_path=raw_path,
        label="repair",
    )
    merge_compact_assignments(compact, patch)
    return compact


def assign_batch(
    client,
    *,
    model: str,
    batch_tracks: list[dict[str, Any]],
    definitions: list[dict[str, str]],
    raw_path: Path | None,
) -> dict[str, str]:
    allowed = valid_bucket_ids(definitions)
    label = raw_path.stem if raw_path else "assign"
    raw = call_llm(
        client,
        model=model,
        system=ASSIGN_SYSTEM_PROMPT,
        user=build_assign_user_prompt(batch_tracks, definitions),
        raw_path=raw_path,
        label=label,
    )
    compact: dict[str, Any] = {
        "playlist_definitions": definitions,
        "assignments": raw.get("assignments", []),
        "unassigned_indices": raw.get("unassigned_indices", []),
    }
    for repair_pass in range(1, MAX_REPAIR_PASSES + 1):
        if not missing_indices(compact, len(batch_tracks)):
            break
        missing_count = len(missing_indices(compact, len(batch_tracks)))
        print(
            f"    repair pass {repair_pass}/{MAX_REPAIR_PASSES} "
            f"({missing_count} tracks unassigned)…",
            flush=True,
        )
        compact = repair_batch(
            client,
            model=model,
            batch_tracks=batch_tracks,
            definitions=definitions,
            compact=compact,
            raw_path=None,
        )
    dedupe_compact_assignments(compact)
    suggestions = apply_batch_assignments(compact, batch_tracks, allowed)
    missing = missing_indices(compact, len(batch_tracks))
    if missing:
        fallback = str(definitions[0]["bucket"]) if definitions else ""
        batch_ids = [t["spotify_id"] for t in batch_tracks]
        for index in missing:
            if fallback and 0 <= index < len(batch_ids):
                suggestions.setdefault(batch_ids[index], fallback)
    return suggestions


def assign_all_tracks(
    client,
    *,
    model: str,
    tracks: list[dict[str, Any]],
    definitions: list[dict[str, str]],
    on_batch_complete: Callable[[int, int, dict[str, str]], None] | None = None,
) -> dict[str, str]:
    ASSIGN_RAW_DIR.mkdir(parents=True, exist_ok=True)
    all_suggestions: dict[str, str] = {}
    total_batches = (len(tracks) + ASSIGN_BATCH_SIZE - 1) // ASSIGN_BATCH_SIZE
    for batch_num, start in enumerate(range(0, len(tracks), ASSIGN_BATCH_SIZE)):
        batch = tracks[start : start + ASSIGN_BATCH_SIZE]
        print(
            f"  AI assign batch {batch_num + 1}/{total_batches} ({len(batch)} tracks)…",
            flush=True,
        )
        raw_path = ASSIGN_RAW_DIR / f"batch_{batch_num:03d}.txt"
        batch_suggestions = assign_batch(
            client,
            model=model,
            batch_tracks=batch,
            definitions=definitions,
            raw_path=raw_path,
        )
        all_suggestions.update(batch_suggestions)
        print(
            f"  AI assign batch {batch_num + 1}/{total_batches} done "
            f"({len(all_suggestions)} suggestions so far)",
            flush=True,
        )
        if on_batch_complete:
            on_batch_complete(batch_num + 1, total_batches, dict(all_suggestions))
    return all_suggestions


def run_initial_analysis(
    export: dict[str, Any],
    sort_data: dict[str, Any],
    bucket_keys: list[str],
    queue_track_ids: list[str],
    *,
    model: str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run name + assign LLM passes; return ai payload for sort_data['ai']."""
    model = model or default_model()
    client = get_openai_client()
    tracks = export.get("tracks", [])
    track_by_id = {t["spotify_id"]: t for t in tracks if t.get("spotify_id")}

    catalog = build_bucket_catalog(sort_data, bucket_keys)
    bucket_hints: dict[str, dict[str, str]] = {}
    summary = ""
    if catalog["blank_slot_count"] > 0:
        print(
            f"  AI naming pass ({len(tracks)} tracks in library, "
            f"{catalog['blank_slot_count']} blank buckets)…",
            flush=True,
        )
        name_raw = call_llm(
            client,
            model=model,
            system=NAME_SYSTEM_PROMPT,
            user=build_name_user_prompt(tracks, catalog),
            raw_path=NAME_RAW_PATH,
            label="playlist names",
        )
        bucket_hints = normalize_bucket_suggestions(name_raw, bucket_keys, sort_data)
        summary = str(name_raw.get("summary", ""))
        if on_progress:
            on_progress({"bucket_hints": bucket_hints, "summary": summary})
    else:
        print("  Skipping AI naming pass — all playlists loaded from Spotify.", flush=True)

    definitions = playlist_definitions_for_assign(
        sort_data,
        bucket_keys,
        bucket_hints,
    )

    queue_tracks = [track_by_id[tid] for tid in queue_track_ids if tid in track_by_id]
    def on_batch_complete(done: int, total: int, partial: dict[str, str]) -> None:
        if on_progress:
            on_progress({"track_suggestions": partial, "assign_progress": f"{done}/{total}"})

    track_suggestions = assign_all_tracks(
        client,
        model=model,
        tracks=queue_tracks,
        definitions=definitions,
        on_batch_complete=on_batch_complete,
    )

    return {
        "status": "ready",
        "summary": summary,
        "bucket_hints": bucket_hints,
        "track_suggestions": track_suggestions,
        "error": None,
    }
