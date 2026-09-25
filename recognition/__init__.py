"""Recognising food in a photo — the interface only.

Nothing here calls an AI service yet. To switch recognition on later:

  1. implement FoodRecognizer in one class (see the marked spot in get_recognizer),
  2. set FOOD_AI_ENABLED=true and ANTHROPIC_API_KEY in .env.

Everything around it already works: the job table, the upload and polling endpoints, matching
the recognised names against the catalogue, and the confirmation sheet in the frontend, which
never logs anything the user has not confirmed.
"""

import os
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable


@dataclass
class RecognizedItem:
    name: str                              # in Czech, as it would be searched for: "rýže vařená"
    grams_estimate: float
    confidence: float                      # 0–1
    matched_food_ref: str | None = None    # filled in by the backend, never by the recognizer

    def to_dict(self) -> dict:
        return asdict(self)


class RecognitionDisabled(RuntimeError):
    pass


@runtime_checkable
class FoodRecognizer(Protocol):
    available: bool

    def recognize(self, image_bytes: bytes) -> list[RecognizedItem]:
        """Items visible on the plate. May take seconds; runs outside the request."""
        ...


class DisabledRecognizer:
    available = False

    def recognize(self, image_bytes: bytes) -> list[RecognizedItem]:
        raise RecognitionDisabled("food_ai_disabled")


def flag_enabled() -> bool:
    return os.environ.get("FOOD_AI_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def get_recognizer() -> FoodRecognizer:
    if not flag_enabled():
        return DisabledRecognizer()
    # ── ClaudeRecognizer goes here ──────────────────────────────────────────
    # A class with `available = True` whose recognize() sends the JPEG to the model named by
    # FOOD_AI_MODEL (default claude-haiku-4-5) with ANTHROPIC_API_KEY and returns
    # RecognizedItem(name=<Czech food name>, grams_estimate=..., confidence=...) per item.
    # Until it exists, the flag alone changes nothing and the feature stays off.
    return DisabledRecognizer()
