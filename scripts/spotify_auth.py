"""Shared Spotify PKCE auth for scripts in this project."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from spotipy import Spotify
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyPKCE

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

CACHE_PATH = ROOT / ".spotify" / "token"
LEGACY_CACHE_FILE = ROOT / ".cache"

DEFAULT_SCOPES = (
    "user-read-private",
    "playlist-read-private",
    "user-library-read",
)

PLAYLIST_SCOPES = (
    "user-read-private",
    "playlist-read-private",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
)


def _env(name: str, fallback: str | None = None) -> str | None:
    return os.getenv(name) or fallback


def configure_spotipy_env() -> None:
    client_id = _env("SPOTIFY_CLIENT_ID") or _env("SPOTIPY_CLIENT_ID")
    redirect_uri = _env("SPOTIPY_REDIRECT_URI")
    if client_id:
        os.environ.setdefault("SPOTIPY_CLIENT_ID", client_id)
    if redirect_uri:
        os.environ.setdefault("SPOTIPY_REDIRECT_URI", redirect_uri)


def require_config() -> tuple[str, str] | None:
    client_id = _env("SPOTIFY_CLIENT_ID") or _env("SPOTIPY_CLIENT_ID")
    redirect_uri = _env("SPOTIPY_REDIRECT_URI")

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIPY_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        print("Missing environment variables:", ", ".join(missing))
        print(f"Fill in {ROOT / '.env'} (see {ROOT / '.env.example'}).")
        return None

    return client_id, redirect_uri


def prepare_cache_path() -> None:
    if LEGACY_CACHE_FILE.is_file() and not CACHE_PATH.exists():
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_CACHE_FILE.rename(CACHE_PATH)
    else:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_spotify_client(scopes: str | tuple[str, ...] = DEFAULT_SCOPES) -> Spotify:
    prepare_cache_path()
    config = require_config()
    if config is None:
        raise RuntimeError("Missing Spotify configuration in .env")
    client_id, redirect_uri = config
    scope = scopes if isinstance(scopes, str) else ",".join(scopes)
    auth = SpotifyPKCE(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_handler=CacheFileHandler(cache_path=str(CACHE_PATH)),
        open_browser=True,
    )
    return Spotify(auth_manager=auth)
