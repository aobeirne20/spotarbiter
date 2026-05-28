"""Load and validate LLM playlist sort plans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playlist_config import PlaylistConfig

PLAN_VERSION = 2
SUGGESTIONS_VERSION = 1


def strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(strip_json_fences(path.read_text()))


def parse_llm_json(text: str) -> dict[str, Any]:
    return json.loads(strip_json_fences(text))


def load_export(path: Path) -> dict[str, Any]:
    return load_json_file(path)


def load_plan(path: Path) -> dict[str, Any]:
    return load_json_file(path)


def load_suggestions(path: Path) -> dict[str, Any]:
    return load_json_file(path)


def validate_suggestions(
    suggestions: dict[str, Any],
    config: PlaylistConfig,
) -> list[str]:
    errors: list[str] = []

    if suggestions.get("version") not in (SUGGESTIONS_VERSION, None):
        errors.append(
            f"Unsupported suggestions version: {suggestions.get('version')}"
        )

    playlists = suggestions.get("playlists")
    if not isinstance(playlists, list) or not playlists:
        errors.append("'playlists' must be a non-empty array")
        return errors

    names: set[str] = set()
    for index, entry in enumerate(playlists):
        if not isinstance(entry, dict):
            errors.append(f"playlists[{index}] must be an object")
            continue
        name = normalize_playlist_name(str(entry.get("name", "")))
        description = (entry.get("description") or "").strip()
        if not name:
            errors.append(f"playlists[{index}]: name is required")
        elif name.lower() in names:
            errors.append(f"Duplicate playlist name: {name}")
        else:
            names.add(name.lower())
        if not description:
            errors.append(f"playlists[{index}] ({name or '?'}): description is required")

    count = len([p for p in playlists if isinstance(p, dict) and p.get("name")])
    if count < config.min_playlists:
        errors.append(f"Only {count} playlists; minimum is {config.min_playlists}")
    if count > config.max_playlists:
        errors.append(f"{count} playlists; maximum is {config.max_playlists}")
    if count < len(config.required_playlists):
        errors.append(
            f"Only {count} playlists but {len(config.required_playlists)} required briefs"
        )

    return errors


def suggestions_as_definitions(suggestions: dict[str, Any]) -> list[dict[str, str]]:
    definitions: list[dict[str, str]] = []
    for entry in suggestions.get("playlists", []):
        if not isinstance(entry, dict):
            continue
        name = normalize_playlist_name(str(entry.get("name", "")))
        if name:
            definitions.append(
                {
                    "name": name,
                    "description": str(entry.get("description") or ""),
                }
            )
    return definitions


def known_track_ids(export: dict[str, Any]) -> set[str]:
    return {
        track["spotify_id"]
        for track in export.get("tracks", [])
        if track.get("spotify_id")
    }


def indexed_tracks_for_prompt(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tracks for the LLM prompt using numeric index instead of spotify_id in assignments."""
    slim = slim_tracks_for_prompt(tracks)
    for index, row in enumerate(slim):
        row["index"] = index
        row.pop("spotify_id", None)
    return slim


def slim_tracks_for_prompt(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slim: list[dict[str, Any]] = []
    for track in tracks:
        row: dict[str, Any] = {
            "spotify_id": track["spotify_id"],
            "title": track["title"],
            "artists": track.get("artists", []),
        }
        if track.get("genres"):
            row["genres"] = track["genres"][:8]
        if track.get("mood_tags"):
            row["mood_tags"] = track["mood_tags"][:5]
        if track.get("mood"):
            row["mood"] = track["mood"]
        if track.get("bpm") is not None:
            row["bpm"] = track["bpm"]
        audio = track.get("audio") or {}
        for key in ("energy", "valence", "danceability", "acousticness"):
            if audio.get(key) is not None:
                row.setdefault("audio", {})[key] = audio[key]
        slim.append(row)
    return slim


def normalize_playlist_name(name: str) -> str:
    return name.strip()


def expand_compact_plan(
    compact: dict[str, Any],
    tracks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert index-based LLM output into the full plan format used by apply/validate."""
    spotify_ids = [t["spotify_id"] for t in tracks]
    track_count = len(spotify_ids)

    playlists: dict[str, dict[str, Any]] = {}
    for definition in compact.get("playlist_definitions", []):
        if not isinstance(definition, dict):
            continue
        name = normalize_playlist_name(str(definition.get("name", "")))
        if not name:
            continue
        playlists[name] = {
            "description": definition.get("description") or "",
            "track_ids": [],
            "action": "create",
        }

    for assignment in compact.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        name = normalize_playlist_name(str(assignment.get("playlist", "")))
        index = assignment.get("track_index")
        if not name or not isinstance(index, int):
            continue
        if index < 0 or index >= track_count:
            raise ValueError(f"Invalid track_index {index} (library has {track_count} tracks)")
        playlists.setdefault(
            name,
            {"description": "", "track_ids": [], "action": "create"},
        )
        playlists[name]["track_ids"].append(spotify_ids[index])

    unassigned: list[str] = []
    for index in compact.get("unassigned_indices", []):
        if not isinstance(index, int):
            continue
        if index < 0 or index >= track_count:
            raise ValueError(f"Invalid unassigned index {index}")
        unassigned.append(spotify_ids[index])

    return {
        "version": PLAN_VERSION,
        "summary": compact.get("summary", ""),
        "playlist_count": compact.get("playlist_count", len(playlists)),
        "required_playlists_fulfilled": compact.get("required_playlists_fulfilled", []),
        "playlists": playlists,
        "unassigned": unassigned,
    }


def normalize_llm_response(raw: dict[str, Any], tracks: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept compact (index) or legacy (spotify_id) LLM shapes."""
    if "playlist_definitions" in raw and "assignments" in raw:
        return expand_compact_plan(raw, tracks)

    plan = raw
    for name, entry in plan.get("playlists", {}).items():
        if isinstance(entry, dict):
            entry.setdefault("action", "create")
    return plan


def assigned_indices(compact: dict[str, Any]) -> set[int]:
    seen: set[int] = set()
    for assignment in compact.get("assignments", []):
        if isinstance(assignment, dict) and isinstance(assignment.get("track_index"), int):
            seen.add(assignment["track_index"])
    for index in compact.get("unassigned_indices", []):
        if isinstance(index, int):
            seen.add(index)
    return seen


def missing_indices(compact: dict[str, Any], track_count: int) -> list[int]:
    seen = assigned_indices(compact)
    return [index for index in range(track_count) if index not in seen]


def playlist_names_from_compact(compact: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for definition in compact.get("playlist_definitions", []):
        if isinstance(definition, dict):
            name = normalize_playlist_name(str(definition.get("name", "")))
            if name:
                names.append(name)
    return names


def dedupe_compact_assignments(compact: dict[str, Any]) -> int:
    """Keep the first assignment per track index. Returns duplicate count removed."""
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    removed = 0
    for assignment in compact.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        index = assignment.get("track_index")
        if not isinstance(index, int) or index in seen:
            removed += 1
            continue
        seen.add(index)
        unique.append(assignment)
    compact["assignments"] = unique

    unique_unassigned: list[int] = []
    for index in compact.get("unassigned_indices", []):
        if isinstance(index, int) and index not in seen:
            unique_unassigned.append(index)
            seen.add(index)
    compact["unassigned_indices"] = unique_unassigned
    return removed


def merge_compact_assignments(compact: dict[str, Any], patch: dict[str, Any]) -> int:
    """Merge repair output; skip indices already assigned. Returns skipped count."""
    compact.setdefault("assignments", [])
    compact.setdefault("unassigned_indices", [])
    already = assigned_indices(compact)
    skipped = 0

    for assignment in patch.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        index = assignment.get("track_index")
        if not isinstance(index, int) or index in already:
            skipped += 1
            continue
        compact["assignments"].append(assignment)
        already.add(index)

    for index in patch.get("unassigned_indices", []):
        if not isinstance(index, int) or index in already:
            skipped += 1
            continue
        compact["unassigned_indices"].append(index)
        already.add(index)

    return skipped


def fill_uncategorized(compact: dict[str, Any], indices: list[int]) -> None:
    if not indices:
        return
    name = "Uncategorized"
    definitions = compact.setdefault("playlist_definitions", [])
    if name not in playlist_names_from_compact(compact):
        definitions.append(
            {
                "name": name,
                "description": "Tracks the model did not assign in the first pass.",
            }
        )
    for index in indices:
        compact.setdefault("assignments", []).append(
            {"playlist": name, "track_index": index}
        )


def validate_plan(
    plan: dict[str, Any],
    track_ids: set[str],
    config: PlaylistConfig | None = None,
) -> list[str]:
    errors: list[str] = []

    version = plan.get("version")
    if version not in (PLAN_VERSION, None):
        errors.append(f"Unsupported plan version: {version} (expected {PLAN_VERSION})")

    playlists = plan.get("playlists")
    if not isinstance(playlists, dict) or not playlists:
        errors.append("Plan must include a non-empty 'playlists' object")
    elif config:
        count = len(playlists)
        if count < config.min_playlists:
            errors.append(
                f"Plan has {count} playlists; minimum is {config.min_playlists}"
            )
        if count > config.max_playlists:
            errors.append(
                f"Plan has {count} playlists; maximum is {config.max_playlists}"
            )
        if len(config.required_playlists) > count:
            errors.append(
                "Plan has fewer playlists than required_playlists entries in config"
            )

    seen: set[str] = set()
    if isinstance(playlists, dict):
        for name, entry in playlists.items():
            if not name.strip():
                errors.append("Playlist names must not be empty")
                continue
            if not isinstance(entry, dict):
                errors.append(f"Playlist entry for {name!r} must be an object")
                continue
            action = entry.get("action", "create")
            if action not in ("create", "use_existing"):
                errors.append(f"{name}: unsupported action {action!r}")
            ids = entry.get("track_ids")
            if not isinstance(ids, list):
                errors.append(f"{name}: track_ids must be a list")
                continue
            for track_id in ids:
                if not isinstance(track_id, str):
                    errors.append(f"{name}: invalid track id {track_id!r}")
                    continue
                if track_id not in track_ids:
                    errors.append(f"{name}: unknown track id {track_id}")
                if track_id in seen:
                    errors.append(f"Duplicate assignment for track id {track_id}")
                seen.add(track_id)

    unassigned = plan.get("unassigned", [])
    if not isinstance(unassigned, list):
        errors.append("'unassigned' must be a list")
    else:
        for track_id in unassigned:
            if track_id not in track_ids:
                errors.append(f"unassigned: unknown track id {track_id}")
            if track_id in seen:
                errors.append(f"Duplicate assignment for track id {track_id}")
            seen.add(track_id)

    missing = track_ids - seen
    if missing:
        errors.append(f"{len(missing)} track(s) not assigned to any playlist")

    return errors


def save_plan(plan: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2) + "\n")
