from typing import Any, Literal

from pydantic import BaseModel


class ClassifyIn(BaseModel):
    text: str
    threshold: float | None = None


class BatchClassifyIn(BaseModel):
    texts: list[str]
    threshold: float | None = None


class AudioIn(BaseModel):
    # For future STT. Prefer url/blob_ref for large audio; base64 is only for tiny tests.
    kind: Literal["url", "base64", "blob_ref"] = "url"
    url: str | None = None
    data: str | None = None
    blob_ref: str | None = None
    mime_type: str | None = None
    duration_ms: int | None = None
    sample_rate_hz: int | None = None
    language: str | None = "ko-KR"


class SttIn(BaseModel):
    # If transcript is already produced by the client/STT layer, gateway can proceed today.
    # If only audio is provided, gateway accepts the payload but returns an input_required event
    # until an STT provider is wired.
    provider: str | None = None
    language: str | None = "ko-KR"
    transcript: str | None = None
    confidence: float | None = None


class TtsIn(BaseModel):
    # Future TTS request options. Current gateway only echoes a tts pending event.
    enabled: bool = False
    provider: str | None = None
    voice: str | None = None
    format: str | None = "mp3"
    speed: float | None = None


class LlmIn(BaseModel):
    # Optional per-request generation controls. Server-side env limits still
    # cap these values to prevent accidental cost/latency spikes.
    max_completion_tokens: int | None = None
    temperature: float | None = None


class RespondIn(BaseModel):
    text: str | None = None
    session_id: str | None = None
    input_type: Literal["text", "audio", "transcript"] | None = None
    audio: AudioIn | None = None
    stt: SttIn | None = None
    tts: TtsIn | None = None
    llm: LlmIn | None = None
    client: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def effective_text(self) -> str | None:
        text = (self.text or "").strip()
        if text:
            return text
        transcript = (self.stt.transcript if self.stt else None) or ""
        transcript = transcript.strip()
        return transcript or None

    def input_meta(self) -> dict[str, Any]:
        return {
            "input_type": self.input_type or ("audio" if self.audio else "text"),
            "audio": self.audio.model_dump(exclude_none=True) if self.audio else None,
            "stt": self.stt.model_dump(exclude_none=True) if self.stt else None,
            "client": self.client,
            "metadata": self.metadata,
        }


class SessionCreateIn(BaseModel):
    session_id: str | None = None
