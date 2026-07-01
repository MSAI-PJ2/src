"""Azure Speech STT/TTS client for the gateway.

llm_client.py, retrieve/client.py와 같은 패턴: 단순한 모듈 함수 +
환경변수 설정. Speech SDK 호출은 블로킹(동기)이라서, 호출하는 쪽
(main.py, dag.py)에서 asyncio.to_thread로 감싸서 씁니다.

연결 위치:
    STT — main.py의 /v1/respond 핸들러, body.effective_text() 호출 전
    TTS — dag.py의 respond_stream() (그리고 crisis 분기), 전체
          assistant_text가 완성된 뒤 — TTS는 토큰 단위가 아니라
          완성된 문장이 있어야 자연스럽게 합성됩니다.

스키마 참고: AudioIn.kind는 "url" | "base64" | "blob_ref" 중 하나
(이 프로젝트는 blob_ref 미사용). kind == "base64"일 때, 실제 바이트는
AudioIn.data 필드에 base64로 인코딩되어 들어옵니다.

필요 환경변수:
    AZURE_SPEECH_KEY
    AZURE_SPEECH_REGION (기본값 koreacentral)
    AZURE_SPEECH_DEFAULT_VOICE (기본값 ko-KR-SunHiNeural)
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import tempfile

import azure.cognitiveservices.speech as speechsdk
import httpx

logger = logging.getLogger(__name__)

DEFAULT_VOICE = os.getenv("AZURE_SPEECH_DEFAULT_VOICE", "ko-KR-SunHiNeural")


def _speech_config(voice_name: str | None = None) -> speechsdk.SpeechConfig:
    cfg = speechsdk.SpeechConfig(
        subscription=os.environ["AZURE_SPEECH_KEY"],
        region=os.environ.get("AZURE_SPEECH_REGION", "koreacentral"),
    )
    cfg.speech_recognition_language = "ko-KR"
    cfg.speech_synthesis_voice_name = voice_name or DEFAULT_VOICE
    cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
    )
    return cfg


def _resolve_audio_bytes(audio: dict) -> bytes:
    """AudioIn.model_dump() 결과(dict)에서 실제 오디오 바이트를 꺼냅니다."""
    kind = audio.get("kind")

    if kind == "base64":
        data = audio.get("data")
        if not data:
            raise ValueError("audio.data is required when audio.kind='base64'")
        if isinstance(data, str) and data.strip().startswith("data:") and "," in data:
            data = data.split(",", 1)[1]
        return base64.b64decode(data)

    if kind == "url":
        url = audio.get("url")
        if not url:
            raise ValueError("audio.url is required when audio.kind='url'")
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        return resp.content

    raise ValueError(
        f"unsupported audio.kind: {kind!r} "
        "(이 프로젝트는 base64 / url만 지원, blob_ref 미사용)"
    )


def _to_wav(raw: bytes, mime_type: str | None) -> bytes:
    """WebM/OGG 등을 16kHz 모노 16bit WAV로 변환 (pydub + ffmpeg 필요)."""
    if mime_type and "wav" in mime_type:
        return raw
    try:
        from pydub import AudioSegment

        fmt_map = {"webm": "webm", "ogg": "ogg", "mp4": "mp4", "m4a": "mp4"}
        fmt = next((v for k, v in fmt_map.items() if k in (mime_type or "")), "webm")

        seg = AudioSegment.from_file(io.BytesIO(raw), format=fmt)
        seg = seg.set_frame_rate(16_000).set_channels(1).set_sample_width(2)

        buf = io.BytesIO()
        seg.export(buf, format="wav")
        return buf.getvalue()
    except Exception as exc:
        logger.warning("오디오 포맷 변환 실패 (%s), 원본 바이트로 시도", exc)
        return raw


def transcribe_audio_input(audio: dict) -> tuple[str, bool]:
    """
    audio (AudioIn.model_dump(exclude_none=True) 결과) → (transcript, success).
    블로킹 호출입니다 — asyncio.to_thread로 감싸서 쓰세요.
    """
    raw = _resolve_audio_bytes(audio)
    wav = _to_wav(raw, audio.get("mime_type"))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav)
        tmp_path = f.name

    try:
        audio_cfg = speechsdk.audio.AudioConfig(filename=tmp_path)
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=_speech_config(), audio_config=audio_cfg
        )
        result = recognizer.recognize_once_async().get()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if result.reason == speechsdk.ResultReason.RecognizedSpeech:
        return result.text.strip(), True
    if result.reason == speechsdk.ResultReason.NoMatch:
        logger.warning("STT NoMatch: %s", result.no_match_details)
        return "", False

    cancel = speechsdk.CancellationDetails.from_result(result)
    raise RuntimeError(f"STT canceled: {cancel.reason} / {cancel.error_details}")


def transcribe_audio_input_detailed(audio: dict) -> dict:
    """Return a detailed STT result for SSE/debugging.

    This keeps the old transcribe_audio_input() API available while giving the
    gateway a stable contract for `stt` SSE events.
    """
    try:
        raw = _resolve_audio_bytes(audio)
        wav = _to_wav(raw, audio.get("mime_type"))

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            tmp_path = f.name

        try:
            audio_cfg = speechsdk.audio.AudioConfig(filename=tmp_path)
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=_speech_config(), audio_config=audio_cfg
            )
            result = recognizer.recognize_once_async().get()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        base = {
            "provider": "azure",
            "language": audio.get("language") or "ko-KR",
            "mime_type": audio.get("mime_type"),
            "kind": audio.get("kind"),
        }

        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            return {
                **base,
                "status": "completed",
                "transcript": result.text.strip(),
                "confidence": None,
                "recognition_status": "RecognizedSpeech",
            }

        if result.reason == speechsdk.ResultReason.NoMatch:
            return {
                **base,
                "status": "no_match",
                "transcript": "",
                "confidence": None,
                "recognition_status": "NoMatch",
                "reason": str(result.no_match_details),
            }

        cancel = speechsdk.CancellationDetails.from_result(result)
        return {
            **base,
            "status": "error",
            "transcript": "",
            "recognition_status": "Canceled",
            "reason": str(cancel.reason),
            "error": str(cancel.error_details),
        }
    except Exception as exc:
        return {
            "status": "error",
            "provider": "azure",
            "language": audio.get("language") or "ko-KR",
            "mime_type": audio.get("mime_type"),
            "kind": audio.get("kind"),
            "transcript": "",
            "error": str(exc),
        }


def synthesize_speech_base64(text: str, voice_name: str | None = None) -> str:
    """
    text → base64 인코딩된 WAV 오디오 문자열.
    블로킹 호출입니다 — asyncio.to_thread로 감싸서 쓰세요.
    """
    clean = _strip_markdown(text)
    synth = speechsdk.SpeechSynthesizer(
        speech_config=_speech_config(voice_name), audio_config=None
    )
    result = synth.speak_text_async(clean).get()

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return base64.b64encode(result.audio_data).decode("ascii")

    cancel = speechsdk.CancellationDetails.from_result(result)
    raise RuntimeError(f"TTS canceled: {cancel.reason} / {cancel.error_details}")


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"#{1,6}\s*", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(
        "[\U00010000-\U0010ffff"
        "\U0001F300-\U0001F9FF"
        "\U00002700-\U000027BF"
        "\U0000FE00-\U0000FE0F]+",
        "",
        text,
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()
