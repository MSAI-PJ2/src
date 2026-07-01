"""Normalized request context for /v1/respond orchestration.

This module keeps transport/input-shape decisions out of dag.py. The public API
contract stays in schemas.py; this file turns that contract into the internal
shape used by orchestration and can later be extended for auth/profile context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import RespondIn


DEFAULT_LANGUAGE = "ko-KR"


@dataclass(frozen=True)
class RespondRequestContext:
    """Internal normalized request context for the gateway response DAG."""

    session_id: str | None
    text: str | None
    input_meta: dict[str, Any]
    tts: dict[str, Any] | None = None

    @classmethod
    def from_body(cls, body: RespondIn) -> "RespondRequestContext":
        return cls(
            session_id=body.session_id,
            text=body.effective_text(),
            input_meta=body.input_meta(),
            tts=body.tts.model_dump(exclude_none=True) if body.tts else None,
        )

    @property
    def has_text(self) -> bool:
        return bool((self.text or "").strip())

    @property
    def requires_stt(self) -> bool:
        return bool(self.input_meta.get("audio")) and not self.has_text

    @property
    def stt_options(self) -> dict[str, Any]:
        return dict(self.input_meta.get("stt") or {})

    @property
    def audio(self) -> dict[str, Any]:
        return dict(self.input_meta.get("audio") or {})

    @property
    def language(self) -> str:
        return self.stt_options.get("language") or self.audio.get("language") or DEFAULT_LANGUAGE

    @property
    def stt_provider(self) -> str:
        return self.stt_options.get("provider") or "azure"

    def with_transcript(self, result: dict[str, Any]) -> "RespondRequestContext":
        """Return a new context after STT successfully produced a transcript."""
        language = result.get("language") or self.language
        stt_options = self.stt_options
        input_meta = {
            **self.input_meta,
            "input_type": "transcript",
            "stt": {
                **stt_options,
                "provider": result.get("provider"),
                "language": language,
                "transcript": result.get("transcript"),
                "confidence": result.get("confidence"),
                "recognition_status": result.get("recognition_status"),
            },
        }
        return RespondRequestContext(
            session_id=self.session_id,
            text=result.get("transcript"),
            input_meta=input_meta,
            tts=self.tts,
        )


def default_text_input_meta(input_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the legacy default shape used by /v1/respond text requests."""
    return input_meta or {"input_type": "text"}
