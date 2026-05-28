"""User configuration for playlist generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spotify_auth import ROOT

DEFAULT_CONFIG_PATH = ROOT / "data" / "playlist_config.json"
EXAMPLE_CONFIG_PATH = ROOT / "data" / "playlist_config.example.json"


@dataclass
class RequiredPlaylist:
    description: str


@dataclass
class PlaylistConfig:
    min_playlists: int = 5
    max_playlists: int = 10
    required_playlists: list[RequiredPlaylist] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


def load_playlist_config(path: Path) -> PlaylistConfig:
    if not path.is_file():
        if path == DEFAULT_CONFIG_PATH and EXAMPLE_CONFIG_PATH.is_file():
            print(f"Config not found at {path}; using {EXAMPLE_CONFIG_PATH}")
            path = EXAMPLE_CONFIG_PATH
        else:
            raise FileNotFoundError(
                f"Playlist config not found: {path}\n"
                f"Copy {EXAMPLE_CONFIG_PATH} to {DEFAULT_CONFIG_PATH} and edit it."
            )

    raw = json.loads(path.read_text())
    count = raw.get("playlist_count", {})

    required: list[RequiredPlaylist] = []
    for entry in raw.get("required_playlists", []):
        if isinstance(entry, str):
            required.append(RequiredPlaylist(description=entry))
        elif isinstance(entry, dict) and entry.get("description"):
            required.append(RequiredPlaylist(description=str(entry["description"])))

    rules = [str(line) for line in raw.get("rules", []) if str(line).strip()]

    min_playlists = int(count.get("min", 5))
    max_playlists = int(count.get("max", 10))
    if min_playlists < 1:
        raise ValueError("playlist_count.min must be at least 1")
    if max_playlists < min_playlists:
        raise ValueError("playlist_count.max must be >= playlist_count.min")
    if len(required) > max_playlists:
        raise ValueError(
            f"You have {len(required)} required playlists but max is {max_playlists}"
        )

    return PlaylistConfig(
        min_playlists=min_playlists,
        max_playlists=max_playlists,
        required_playlists=required,
        rules=rules,
    )


def config_for_prompt(config: PlaylistConfig) -> dict[str, Any]:
    return {
        "playlist_count": {
            "min": config.min_playlists,
            "max": config.max_playlists,
        },
        "required_playlists": [
            {"description": item.description} for item in config.required_playlists
        ],
        "rules": config.rules,
    }
