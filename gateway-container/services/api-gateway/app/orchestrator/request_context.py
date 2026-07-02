"""/v1/respond 요청 정규화 컨텍스트.

전송 형태(text/transcript/audio) 판단을 respond_flow 밖으로 분리한다.
외부 계약은 contracts/requests.py, 이 파일은 그 계약을 오케스트레이션이 쓰는
내부 형태로 바꾼다. 로그인 도입 시 user_id 필드를 여기에 추가하면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts.requests import RespondIn

DEFAULT_LANGUAGE = "ko-KR"


@dataclass(frozen=True)
class RespondRequestContext:
    session_id: str | None
    text: str | None
    input_meta: dict[str, Any]
    tts: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None

    @classmethod
    def from_body(cls, body: RespondIn) -> "RespondRequestContext":
        return cls(
            session_id=body.session_id,
            text=body.effective_text(),
            input_meta=body.input_meta(),
            tts=body.tts.model_dump(exclude_none=True) if body.tts else None,
            llm=body.llm.model_dump(exclude_none=True) if body.llm else None,
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
        """STT 성공 후 transcript 를 반영한 새 컨텍스트를 만든다."""
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
            llm=self.llm,
        )


def default_text_input_meta(input_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """/v1/respond 텍스트 요청의 기존 기본 input 형태를 유지한다."""
    return input_meta or {"input_type": "text"}
