#!/usr/bin/env python3
"""Step 2: Assign every liked track to the suggested playlists."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from llm_client import call_llm, default_model, get_openai_client
from playlist_config import DEFAULT_CONFIG_PATH, load_playlist_config
from playlist_plan import (
    PLAN_VERSION,
    dedupe_compact_assignments,
    fill_uncategorized,
    indexed_tracks_for_prompt,
    known_track_ids,
    load_export,
    load_suggestions,
    merge_compact_assignments,
    missing_indices,
    normalize_llm_response,
    parse_llm_json,
    playlist_names_from_compact,
    save_plan,
    suggestions_as_definitions,
    validate_plan,
)
from spotify_auth import ROOT

load_dotenv(ROOT / ".env")

DEFAULT_EXPORT = ROOT / "data" / "liked_tracks.json"
DEFAULT_SUGGESTIONS = ROOT / "data" / "playlist_suggestions.json"
DEFAULT_OUTPUT = ROOT / "data" / "playlist_plan.json"
RAW_RESPONSE_PATH = ROOT / "data" / "last_llm_response.txt"
MAX_REPAIR_PASSES = 2

ASSIGN_SYSTEM_PROMPT = f"""\
You are assigning tracks to playlists that are already defined.

Tracks use "index" (0-based). Reference tracks by index only.

Return ONLY JSON:
{{
  "version": {PLAN_VERSION},
  "summary": "brief note on how you distributed tracks",
  "assignments": [
    {{"playlist": "exact name from suggested_playlists", "track_index": 0}}
  ],
  "unassigned_indices": []
}}

Rules:
- Use ONLY playlist names from suggested_playlists.
- Every track index from 0 through track_count-1 must appear exactly once in
  assignments or unassigned_indices.
- Prefer unassigned over a poor fit.
"""

REPAIR_SYSTEM_PROMPT = """\
Complete a partial track assignment.

Return ONLY JSON:
{
  "assignments": [{"playlist": "existing playlist name", "track_index": 0}],
  "unassigned_indices": []
}

Rules:
- Assign ONLY indices listed in missing_indices.
- Use only playlist names from existing_playlists.
"""


def build_assign_prompt(
    tracks: list[dict[str, Any]],
    suggestions: dict[str, Any],
) -> str:
    definitions = suggestions_as_definitions(suggestions)
    payload = {
        "track_count": len(tracks),
        "suggested_playlists": definitions,
        "tracks": indexed_tracks_for_prompt(tracks),
    }
    return (
        "Assign each track to one suggested playlist.\n\n"
        f"{json.dumps(payload, separators=(',', ':'))}"
    )


def build_repair_prompt(
    tracks: list[dict[str, Any]],
    compact: dict[str, Any],
    missing: list[int],
) -> str:
    indexed = indexed_tracks_for_prompt(tracks)
    return json.dumps(
        {
            "existing_playlists": playlist_names_from_compact(compact),
            "missing_indices": missing,
            "missing_tracks": [indexed[i] for i in missing],
        },
        separators=(",", ":"),
    )


def compact_from_assignment(
    raw: dict[str, Any],
    suggestions: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": PLAN_VERSION,
        "summary": raw.get("summary", suggestions.get("summary", "")),
        "playlist_count": len(suggestions_as_definitions(suggestions)),
        "required_playlists_fulfilled": suggestions.get("required_playlists_fulfilled", []),
        "playlist_definitions": suggestions_as_definitions(suggestions),
        "assignments": raw.get("assignments", []),
        "unassigned_indices": raw.get("unassigned_indices", []),
    }


def repair_missing(
    client,
    *,
    model: str,
    tracks: list[dict[str, Any]],
    compact: dict[str, Any],
) -> dict[str, Any]:
    missing = missing_indices(compact, len(tracks))
    if not missing:
        return compact
    print(f"  Repairing {len(missing)} unassigned track(s)...")
    patch = call_llm(
        client,
        model=model,
        system=REPAIR_SYSTEM_PROMPT,
        user=build_repair_prompt(tracks, compact, missing),
        raw_path=None,
    )
    skipped = merge_compact_assignments(compact, patch)
    if skipped:
        print(f"  Ignored {skipped} duplicate assignment(s) from repair")
    return compact


def complete_assignments(
    client,
    *,
    model: str,
    tracks: list[dict[str, Any]],
    compact: dict[str, Any],
) -> dict[str, Any]:
    for pass_num in range(1, MAX_REPAIR_PASSES + 1):
        missing = missing_indices(compact, len(tracks))
        if not missing:
            break
        print(f"Pass {pass_num}: {len(missing)} track(s) still unassigned")
        compact = repair_missing(client, model=model, tracks=tracks, compact=compact)

    missing = missing_indices(compact, len(tracks))
    if missing:
        print(f"  Adding {len(missing)} track(s) to 'Uncategorized'")
        fill_uncategorized(compact, missing)

    removed = dedupe_compact_assignments(compact)
    if removed:
        print(f"  Removed {removed} duplicate assignment(s)")
    return compact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--suggestions", type=Path, default=DEFAULT_SUGGESTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--model", default=default_model())
    parser.add_argument(
        "--from-raw",
        type=Path,
        help="Parse a saved assignment response instead of calling the API",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"Export not found: {args.input}")
        return 1
    if not args.suggestions.is_file():
        print(f"Suggestions not found: {args.suggestions}")
        print("Run: .venv/bin/python scripts/suggest_playlists.py")
        return 1

    export = load_export(args.input)
    tracks = export.get("tracks", [])
    track_ids = known_track_ids(export)
    suggestions = load_suggestions(args.suggestions)
    definitions = suggestions_as_definitions(suggestions)

    if not definitions:
        print("No playlists in suggestions file.")
        return 1

    try:
        client = get_openai_client()
    except RuntimeError as exc:
        print(exc)
        return 1

    if args.from_raw:
        raw = parse_llm_json(args.from_raw.read_text())
    else:
        print(f"Assigning {len(tracks)} tracks to {len(definitions)} playlists...")
        raw = call_llm(
            client,
            model=args.model,
            system=ASSIGN_SYSTEM_PROMPT,
            user=build_assign_prompt(tracks, suggestions),
            raw_path=RAW_RESPONSE_PATH,
        )

    compact = compact_from_assignment(raw, suggestions)
    compact = complete_assignments(client, model=args.model, tracks=tracks, compact=compact)
    plan = normalize_llm_response(compact, tracks)

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
        save_plan(plan, args.output)
        return 1

    save_plan(plan, args.output)
    print(f"Plan saved to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
