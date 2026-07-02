"""Azure Speech STT/TTS 어댑터 (common/speech_client.py).

Speech SDK 는 블로킹(동기)이라 모든 호출을 asyncio.to_thread 로 오프로딩한다.
TTS 는 토큰 단위가 아니라 완성된 문장으로 합성한다 — respond_flow 가
LLM 스트리밍 종료 후 호출한다.
"""
import asyncio

from common.speech_client import synthesize_speech_base64, transcribe_audio_input_detailed


class SpeechAdapter:
    async def transcribe_audio(self, audio: dict | None) -> dict:
        return await asyncio.to_thread(transcribe_audio_input_detailed, audio)

    async def synthesize_tts(self, text: str, tts_options: dict | None) -> dict:
        """TTS SSE payload 를 만든다. 실패해도 스트림이 끊기지 않도록 error payload 로 반환."""
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
                # 과거 프론트 호환용 별칭 (신규 코드는 audio.data 사용)
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
