"""Session turn payload builders.

These helpers keep the in-memory session shape explicit and make a future
Cosmos DB persistence adapter easier to introduce.
"""


def stt_failed_turn(input_meta: dict, result: dict, tts: dict | None) -> dict:
    return {
        "role": "user",
        "text": "",
        "event": "stt_failed",
        "input": input_meta,
        "stt_result": result,
        "tts": tts,
    }


def input_pending_turn(input_meta: dict, tts: dict | None) -> dict:
    return {
        "role": "user",
        "text": "",
        "event": "input_pending",
        "input": input_meta,
        "tts": tts,
    }


def user_turn(text: str, primary: str, safety: dict, input_meta: dict, tts: dict | None) -> dict:
    return {
        "role": "user",
        "text": text,
        "primary": primary,
        "safety": "safe" if safety.get("safe") else "blocked",
        "safety_reason": safety.get("reason"),
        "input": input_meta,
        "tts": tts,
    }


def crisis_turn(payload: dict) -> dict:
    return {
        "role": "assistant",
        "text": payload.get("message", ""),
        "event": "crisis",
        "blocked": True,
        "reason": payload.get("reason"),
    }


def assistant_turn(text: str, primary: str, chunks: list[dict]) -> dict:
    return {
        "role": "assistant",
        "text": text,
        "event": "respond",
        "primary": primary,
        "rag_chunk_ids": [chunk["id"] for chunk in chunks],
    }
