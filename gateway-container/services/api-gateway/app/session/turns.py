"""DB(세션)에 저장하는 턴 payload 빌더.

SSE 로 나가는 이벤트는 streaming/events.py — 여기와 섞지 않는다.
필드를 바꾸면 GET /v1/sessions/{id} 응답이 바뀌므로 API_CONTRACT.md 도 갱신한다.
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


def assistant_turn(text: str, primary: str, chunks: list[dict], policy: dict | None = None) -> dict:
    turn = {
        "role": "assistant",
        "text": text,
        "event": "respond",
        "primary": primary,
        "rag_chunk_ids": [chunk["id"] for chunk in chunks],
    }
    if policy:
        # 어떤 컨텍스트 정책/프롬프트 전략으로 생성했는지 기록 (orchestrator/context_policy.py)
        turn["policy"] = policy
    return turn
