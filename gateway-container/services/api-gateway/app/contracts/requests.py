"""/v1 요청 모델. 필드 의미는 API_CONTRACT.md 가 기준이다."""
from typing import Any, Literal

from pydantic import BaseModel


class ClassifyIn(BaseModel):
    text: str
    threshold: float | None = None


class BatchClassifyIn(BaseModel):
    texts: list[str]
    threshold: float | None = None


class AudioIn(BaseModel):
    # 큰 오디오는 url/blob_ref 권장. base64 는 소용량 테스트용.
    kind: Literal["url", "base64", "blob_ref"] = "url"
    url: str | None = None
    data: str | None = None
    blob_ref: str | None = None
    mime_type: str | None = None
    duration_ms: int | None = None
    sample_rate_hz: int | None = None
    language: str | None = "ko-KR"


class SttIn(BaseModel):
    # transcript 가 이미 있으면(클라이언트측 STT) 게이트웨이는 바로 텍스트 흐름으로 진행한다.
    provider: str | None = None
    language: str | None = "ko-KR"
    transcript: str | None = None
    confidence: float | None = None


class TtsIn(BaseModel):
    enabled: bool = False
    provider: str | None = None
    voice: str | None = None
    format: str | None = "mp3"
    speed: float | None = None


class LlmIn(BaseModel):
    # 요청 단위 생성 옵션. 서버측 상한(AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT)이 항상 우선한다.
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
        """text > stt.transcript 우선순위로 실제 처리할 텍스트를 고른다."""
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
