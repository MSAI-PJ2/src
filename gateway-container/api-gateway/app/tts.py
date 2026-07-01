"""TTS orchestration helpers."""

import asyncio

from common.speech_client import synthesize_speech_base64


async def synthesize_tts(text: str, tts_options: dict | None) -> dict:
    """Build a TTS SSE payload without blocking the response flow.

    Canonical contract:
      type=tts, status=completed|error, provider=azure,
      audio={kind,data,mime_type}, audio_base64(deprecated alias).
    """
    voice = (tts_options or {}).get("voice")
    try:
        audio_b64 = await asyncio.to_thread(synthesize_speech_base64, text, voice)
        return {
            "status": "completed",
            "provider": "azure",
            "text": text,
            "mime_type": "audio/wav",
            "format": "wav",
            "audio": {"kind": "base64", "data": audio_b64, "mime_type": "audio/wav"},
            "audio_base64": audio_b64,
            "options": tts_options,
        }
    except Exception as exc:
        return {
            "status": "error",
            "provider": "azure",
            "text": text,
            "error": str(exc)[:300],
            "options": tts_options,
        }
