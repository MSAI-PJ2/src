"""SSE payload builders for the gateway API contract."""

INPUT_REQUIRED_STT_MESSAGE = (
    "audio payload was accepted, but STT did not produce a transcript. "
    "Check stt event error/reason, or send text/stt.transcript."
)

INPUT_REQUIRED_TEXT_MESSAGE = (
    "No text or transcript was provided. Send text, stt.transcript, or an audio payload."
)


def stt_processing_payload(session_id: str, provider: str, language: str) -> dict:
    return {
        "type": "stt",
        "session_id": session_id,
        "status": "processing",
        "provider": provider,
        "language": language,
    }


def stt_result_payload(session_id: str, result: dict) -> dict:
    return {"type": "stt", "session_id": session_id, **result}


def input_required_payload(session_id: str, reason: str, message: str) -> dict:
    return {
        "type": "input_required",
        "session_id": session_id,
        "reason": reason,
        "message": message,
    }


def meta_payload(
    session_id: str,
    turn_count: int,
    input_meta: dict,
    tts: dict | None,
    cls: dict | None = None,
) -> dict:
    payload = {
        "type": "meta",
        "session_id": session_id,
        "turn_count": turn_count,
        "input": input_meta,
        "tts": tts,
    }
    if cls:
        payload.update(
            {
                "primary": cls["primary"],
                "mode": cls["mode"],
                "labels": cls["labels"],
            }
        )
    return payload


def chunks_payload(session_id: str, chunks: list[dict]) -> dict:
    return {
        "type": "chunks",
        "session_id": session_id,
        "chunks": [{"id": chunk["id"], "content": chunk["content"]} for chunk in chunks],
    }


def token_payload(session_id: str, text: str) -> dict:
    return {"type": "token", "session_id": session_id, "text": text}


def tts_payload(session_id: str, tts_event: dict) -> dict:
    return {"type": "tts", "session_id": session_id, **tts_event}


def done_payload(session_id: str) -> dict:
    return {"type": "done", "session_id": session_id}
