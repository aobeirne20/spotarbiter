#!/usr/bin/env python3
"""Validate a playlist plan against a liked-tracks export and config."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playlist_config import DEFAULT_CONFIG_PATH, load_playlist_config
from playlist_plan import known_track_ids, load_export, load_plan, validate_plan
from spotify_auth import ROOT

DEFAULT_PLAN = ROOT / "data" / "playlist_plan.json"
DEFAULT_EXPORT = ROOT / "data" / "liked_tracks.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    if not args.plan.is_file() or not args.export.is_file():
        print("Missing plan or export file.")
        return 1

    config = None
    if args.config.is_file():
        try:
            config = load_playlist_config(args.config)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"Config error: {exc}")
            return 1

    export = load_export(args.export)
    plan = load_plan(args.plan)
    errors = validate_plan(plan, known_track_ids(export), config)

    if errors:
        print("Invalid plan:")
        for error in errors:
            print(f"  - {error}")
        return 1

    assigned = sum(len(e.get("track_ids", [])) for e in plan.get("playlists", {}).values())
    print("Plan is valid.")
    print(f"  Playlists: {len(plan.get('playlists', {}))}")
    if config:
        print(f"  Target range: {config.min_playlists}–{config.max_playlists}")
    print(f"  Assigned:   {assigned}")
    print(f"  Unassigned: {len(plan.get('unassigned', []))}")
    print(f"  Summary:    {plan.get('summary', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
