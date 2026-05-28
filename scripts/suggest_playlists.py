#!/usr/bin/env python3
"""Step 1: LLM proposes playlist names and descriptions from your liked tracks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from llm_client import call_llm, default_model, get_openai_client
from playlist_config import (
    DEFAULT_CONFIG_PATH,
    PlaylistConfig,
    config_for_prompt,
    load_playlist_config,
)
from playlist_plan import (
    SUGGESTIONS_VERSION,
    indexed_tracks_for_prompt,
    load_export,
    save_plan,
    validate_suggestions,
)
from spotify_auth import ROOT

load_dotenv(ROOT / ".env")

DEFAULT_EXPORT = ROOT / "data" / "liked_tracks.json"
DEFAULT_OUTPUT = ROOT / "data" / "playlist_suggestions.json"
RAW_RESPONSE_PATH = ROOT / "data" / "last_playlist_suggestions.txt"

SYSTEM_PROMPT = f"""\
You are a music librarian designing new Spotify playlists for a user's Liked Songs library.

The user wants brand-new playlists — do NOT reference or reuse their existing Spotify
playlist names. Study the track list and propose a coherent set of playlists.

Return ONLY JSON (no markdown):
{{
  "version": {SUGGESTIONS_VERSION},
  "summary": "why you chose this set of playlists for this library",
  "playlist_count": <number of playlists you are proposing>,
  "required_playlists_fulfilled": [
    {{
      "requested": "user's required playlist description",
      "playlist_name": "the name you chose for it"
    }}
  ],
  "playlists": [
    {{
      "name": "specific evocative playlist name",
      "description": "one sentence describing vibe and what belongs here"
    }}
  ]
}}

Rules:
- Propose between playlist_count.min and playlist_count.max playlists (inclusive).
- You MUST include one playlist for every entry in required_playlists (pick the name).
- You MAY add more playlists within the range to cover the library's diversity.
- Do NOT assign tracks yet — names and descriptions only.
- Names must be unique, specific, and memorable (not "Playlist 1").
- Descriptions should help decide which tracks belong later.
"""


def build_user_prompt(tracks: list[dict[str, Any]], config: PlaylistConfig) -> str:
    payload = {
        "constraints": config_for_prompt(config),
        "track_count": len(tracks),
        "tracks": indexed_tracks_for_prompt(tracks),
    }
    return (
        "Based on this liked-songs library, propose playlists to organize it.\n\n"
        f"{json.dumps(payload, separators=(',', ':'))}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--min-playlists", type=int)
    parser.add_argument("--max-playlists", type=int)
    parser.add_argument("--model", default=default_model())
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"Export not found: {args.input}")
        print("Run: .venv/bin/python scripts/export_liked_for_llm.py")
        return 1

    try:
        config = load_playlist_config(args.config)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(exc)
        return 1

    if args.min_playlists is not None:
        config.min_playlists = args.min_playlists
    if args.max_playlists is not None:
        config.max_playlists = args.max_playlists

    export = load_export(args.input)
    tracks = export.get("tracks", [])

    print(
        f"Suggesting {config.min_playlists}–{config.max_playlists} playlists "
        f"from {len(tracks)} tracks..."
    )

    try:
        client = get_openai_client()
        suggestions = call_llm(
            client,
            model=args.model,
            system=SYSTEM_PROMPT,
            user=build_user_prompt(tracks, config),
            raw_path=RAW_RESPONSE_PATH,
        )
    except Exception as exc:
        print(f"LLM error: {exc}")
        return 1

    errors = validate_suggestions(suggestions, config)
    if errors:
        print("Suggestions validation failed:")
        for error in errors:
            print(f"  - {error}")
        save_plan(suggestions, args.output)
        return 1

    save_plan(suggestions, args.output)
    print(f"Saved {len(suggestions.get('playlists', []))} playlist suggestions to {args.output}")
    print(suggestions.get("summary", ""))
    print("\nPlaylists:")
    for entry in suggestions.get("playlists", []):
        print(f"  - {entry.get('name')}: {entry.get('description')}")
    print(f"\nNext: .venv/bin/python scripts/assign_playlist_tracks.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
