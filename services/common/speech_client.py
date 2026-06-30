"""Azure AI Speech helper for gateway STT/TTS integration.

TTS returns audio bytes as base64 for the current MVP. STT supports short-audio
REST recognition for URL/base64 payloads. For production, large audio should be
uploaded to Blob and processed through a dedicated upload/batch pipeline.
"""
from __future__ import annotations

import base64
import html
import os
from urllib.parse import urlencode
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
    stt_timeout: float = 45.0
    stt_max_bytes: int = 10 * 1024 * 1024

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
            stt_timeout=float(os.getenv("AZURE_SPEECH_STT_TIMEOUT", "45")),
            stt_max_bytes=int(os.getenv("AZURE_SPEECH_STT_MAX_BYTES", str(10 * 1024 * 1024))),
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


    def _stt_url(self, language: str, *, detailed: bool = True) -> str:
        # Short-audio REST endpoint for conversational speech recognition.
        base = os.getenv(
            "AZURE_SPEECH_STT_ENDPOINT",
            f"https://{self.config.region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
        )
        query = urlencode({"language": language, "format": "detailed" if detailed else "simple"})
        return f"{base}?{query}"

    def _stt_content_type(self, mime_type: str | None) -> str:
        mime = (mime_type or "audio/wav").lower().strip()
        if "wav" in mime or "wave" in mime:
            sample_rate = os.getenv("AZURE_SPEECH_STT_SAMPLE_RATE", "16000")
            return f"audio/wav; codecs=audio/pcm; samplerate={sample_rate}"
        if "ogg" in mime or "opus" in mime:
            return "audio/ogg; codecs=opus"
        # Azure short-audio REST is strict; reject mp3 rather than sending a
        # request that usually fails with an unclear service error.
        if "mp3" in mime or "mpeg" in mime:
            raise ValueError("stt_unsupported_audio_format: use WAV PCM 16k mono or OGG Opus for short-audio STT")
        return mime

    async def _load_audio_bytes(self, audio: dict[str, Any]) -> bytes:
        kind = (audio.get("kind") or "url").lower()
        if kind == "base64":
            data = audio.get("data") or ""
            if not data:
                raise ValueError("audio_base64_missing")
            if "," in data and data.strip().startswith("data:"):
                data = data.split(",", 1)[1]
            blob = base64.b64decode(data)
        elif kind == "url":
            url = audio.get("url") or ""
            if not url:
                raise ValueError("audio_url_missing")
            async with httpx.AsyncClient(timeout=self.config.stt_timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                blob = resp.content
        elif kind == "blob_ref":
            raise ValueError("stt_blob_ref_not_supported_yet")
        else:
            raise ValueError(f"unsupported_audio_kind:{kind}")

        if not blob:
            raise ValueError("audio_empty")
        if len(blob) > self.config.stt_max_bytes:
            raise ValueError(f"audio_too_large:{len(blob)}>{self.config.stt_max_bytes}")
        return blob

    async def transcribe_stt(self, audio: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        if not self.enabled:
            return {"status": "pending_provider", "reason": "speech_not_configured"}

        language = options.get("language") or audio.get("language") or self.config.language
        audio_bytes = await self._load_audio_bytes(audio)
        content_type = self._stt_content_type(audio.get("mime_type"))

        headers = {
            "Ocp-Apim-Subscription-Key": self.config.key,
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "team3-api-gateway",
        }
        async with httpx.AsyncClient(timeout=self.config.stt_timeout) as client:
            resp = await client.post(self._stt_url(language), headers=headers, content=audio_bytes)
            resp.raise_for_status()
            payload = resp.json()

        status = payload.get("RecognitionStatus") or payload.get("recognitionStatus")
        nbest = payload.get("NBest") or []
        best = nbest[0] if nbest else {}
        transcript = (best.get("Display") or payload.get("DisplayText") or "").strip()
        lexical = (best.get("Lexical") or "").strip() or None
        confidence = best.get("Confidence")

        if not transcript:
            return {
                "status": "no_match" if status in ("NoMatch", "InitialSilenceTimeout") else "empty_transcript",
                "provider": "azure",
                "language": language,
                "recognition_status": status,
                "raw": payload,
            }

        return {
            "status": "completed",
            "provider": "azure",
            "language": language,
            "transcript": transcript,
            "lexical": lexical,
            "confidence": confidence,
            "recognition_status": status,
            "duration_ms": audio.get("duration_ms"),
            "sample_rate_hz": audio.get("sample_rate_hz"),
        }

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
