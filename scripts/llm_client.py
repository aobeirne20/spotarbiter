"""Shared OpenAI helpers for playlist scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from playlist_plan import parse_llm_json
from spotify_auth import ROOT

MAX_COMPLETION_TOKENS = 16_384
MAX_ATTEMPTS = 3
DEFAULT_REQUEST_TIMEOUT_S = 240.0


def llm_request_timeout_s() -> float:
    raw = os.getenv("OPENAI_REQUEST_TIMEOUT_S", "").strip()
    if raw:
        return float(raw)
    return DEFAULT_REQUEST_TIMEOUT_S


def get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing OPENAI_API_KEY in .env (https://platform.openai.com/api-keys)"
        )
    return OpenAI(api_key=api_key, timeout=llm_request_timeout_s())


def call_llm(
    client: OpenAI,
    *,
    model: str,
    system: str,
    user: str,
    raw_path: Path | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if label:
            retry = f" (retry {attempt}/{MAX_ATTEMPTS})" if attempt > 1 else ""
            print(f"    LLM {label}{retry}…", flush=True)
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        if raw_path:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(content)

        if choice.finish_reason == "length":
            last_error = RuntimeError("LLM response was truncated (hit token limit).")
            continue

        try:
            return parse_llm_json(content)
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                print(f"  JSON parse failed (attempt {attempt}/{MAX_ATTEMPTS}), retrying...")
            continue

    hint = f"\nRaw response saved to {raw_path}" if raw_path else ""
    raise RuntimeError(
        f"Could not parse LLM JSON after {MAX_ATTEMPTS} attempts: {last_error}{hint}"
    ) from last_error


def default_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")
