"""Data models for the Pinch plugin."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TranslatorOptions:
    """Options for configuring a Pinch translation session.

    Args:
        source_language: BCP-47 language code for the input audio (e.g. "en-US").
        target_language: BCP-47 language code for the output audio (e.g. "es-ES").
        voice_type: Voice clone mode. One of "clone", "female", or "male".
    """

    source_language: str
    target_language: str
    voice_type: str = field(default="clone")

    def __post_init__(self) -> None:
        allowed = {"clone", "female", "male"}
        if self.voice_type not in allowed:
            raise ValueError(f"voice_type must be one of {allowed!r}, got {self.voice_type!r}.")


@dataclass
class TranscriptEvent:

    type: str
    text: str
    is_final: bool
    language_detected: str
    timestamp: float
    confidence: float = field(default=0.0)

    @property
    def is_original(self) -> bool:
        """True when this event carries the original (pre-translation) transcript."""
        return self.type == "original_transcript"

    @property
    def is_translated(self) -> bool:
        """True when this event carries the translated transcript."""
        return self.type == "translated_transcript"
