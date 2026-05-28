#!/usr/bin/env python3
"""Run both steps: suggest playlists, then assign tracks."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from spotify_auth import ROOT

SCRIPTS = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Or run suggest_playlists.py and assign_playlist_tracks.py separately.",
    )
    parser.add_argument(
        "--suggest-only",
        action="store_true",
        help="Only run step 1 (playlist suggestions)",
    )
    parser.add_argument(
        "--assign-only",
        action="store_true",
        help="Only run step 2 (requires existing playlist_suggestions.json)",
    )
    args, extra = parser.parse_known_args()

    python = sys.executable
    steps: list[list[str]] = []

    if args.assign_only:
        steps.append([python, str(SCRIPTS / "assign_playlist_tracks.py"), *extra])
    elif args.suggest_only:
        steps.append([python, str(SCRIPTS / "suggest_playlists.py"), *extra])
    else:
        steps.append([python, str(SCRIPTS / "suggest_playlists.py"), *extra])
        steps.append([python, str(SCRIPTS / "assign_playlist_tracks.py"), *extra])

    for command in steps:
        print(f"\n>> {' '.join(command)}\n")
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
