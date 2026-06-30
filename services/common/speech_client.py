"""Azure AI Speech helper for gateway STT/TTS integration.

Current implementation wires TTS first because it can be tested from the LLM
response text without an audio upload pipeline. STT is intentionally left as a
future method so the gateway can keep returning input_required for audio-only
payloads until storage/upload semantics are settled.
"""
from __future__ import annotations

import base64
import html
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class SpeechConfig:
    provider: str = "disabled"
    key: str = ""
    region: str = "koreacentral"
    endpoint: str = ""
    language: str = "ko-KR"
    voice: str = "ko-KR-SunHiNeural"
    output_format: str = "audio-16khz-32kbitrate-mono-mp3"
    timeout: float = 30.0
    max_chars: int = 2500

    @classmethod
    def from_env(cls) -> "SpeechConfig":
        return cls(
            provider=(os.getenv("SPEECH_PROVIDER") or "disabled").strip().lower(),
            key=os.getenv("AZURE_SPEECH_KEY") or "",
            region=os.getenv("AZURE_SPEECH_REGION", "koreacentral"),
            endpoint=(os.getenv("AZURE_SPEECH_ENDPOINT") or "").rstrip("/"),
            language=os.getenv("AZURE_SPEECH_LANGUAGE", "ko-KR"),
            voice=os.getenv("AZURE_SPEECH_VOICE", "ko-KR-SunHiNeural"),
            output_format=os.getenv("AZURE_SPEECH_OUTPUT_FORMAT", "audio-16khz-32kbitrate-mono-mp3"),
            timeout=float(os.getenv("AZURE_SPEECH_TIMEOUT", "30")),
            max_chars=int(os.getenv("AZURE_SPEECH_MAX_CHARS", "2500")),
        )


class SpeechClient:
    def __init__(self, config: SpeechConfig | None = None):
        self.config = config or SpeechConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self.config.provider == "azure" and bool(self.config.key)

    def _tts_url(self) -> str:
        # Azure Speech TTS REST uses the regional tts endpoint even when the
        # resource endpoint is the generic cognitiveservices endpoint.
        return os.getenv(
            "AZURE_SPEECH_TTS_ENDPOINT",
            f"https://{self.config.region}.tts.speech.microsoft.com/cognitiveservices/v1",
        )

    def _ssml(self, text: str, *, voice: str, language: str) -> str:
        safe_text = html.escape(text[: self.config.max_chars], quote=False)
        safe_voice = html.escape(voice, quote=True)
        safe_language = html.escape(language, quote=True)
        return (
            f'<speak version="1.0" xml:lang="{safe_language}">'
            f'<voice xml:lang="{safe_language}" name="{safe_voice}">{safe_text}</voice>'
            "</speak>"
        )

    async def synthesize_tts(self, text: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        if not text.strip():
            return {"status": "skipped", "reason": "empty_text"}
        if not self.enabled:
            return {"status": "pending_provider", "reason": "speech_not_configured"}

        voice = options.get("voice") or self.config.voice
        language = options.get("language") or self.config.language
        output_format = options.get("output_format") or self.config.output_format
        requested_format = (options.get("format") or "mp3").lower()
        mime_type = "audio/mpeg" if "mp3" in requested_format or "mp3" in output_format else "audio/wav"

        headers = {
            "Ocp-Apim-Subscription-Key": self.config.key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": output_format,
            "User-Agent": "team3-api-gateway",
        }
        ssml = self._ssml(text, voice=voice, language=language)
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(self._tts_url(), headers=headers, content=ssml.encode("utf-8"))
            resp.raise_for_status()
            audio = resp.content

        return {
            "status": "completed",
            "provider": "azure",
            "mime_type": mime_type,
            "format": "mp3" if mime_type == "audio/mpeg" else requested_format,
            "voice": voice,
            "language": language,
            "size_bytes": len(audio),
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        }


def get_speech_client() -> SpeechClient:
    return SpeechClient()
