import asyncio
import json

import httpx

from common.llm_client import LLMClient
from common.speech_client import synthesize_speech_base64
from retrieve.client import get_retriever

from . import clients, crisis, sessions, settings

_retriever = get_retriever()


def sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# 위험 신호 키워드 (interim stub — 추후 Azure AI Content Safety로 교체)
_RISK_KEYWORDS = (
    "자살", "죽고싶", "자해", "끝내고싶", "사라지고싶",
    "살이유가없", "살이유없", "목숨", "뛰어내리", "죽어버",
)


def _keyword_check(text: str) -> dict:
    # 공백 제거 후 키워드 매칭 (Content Safety 폴백/오프라인용)
    flat = text.replace(" ", "")
    matched = [k for k in _RISK_KEYWORDS if k in flat]
    if matched:
        return {"safe": False, "reason": "self_harm_signal", "matched": matched}
    return {"safe": True, "reason": None}


async def safety_check(text: str) -> dict:
    """안전 게이트. Content Safety 연동 시 실호출, 미설정/장애 시 키워드 폴백(fail-safe)."""
    if settings.CONTENT_SAFETY_ENABLED and settings.CONTENT_SAFETY_ENDPOINT and settings.CONTENT_SAFETY_KEY:
        url = settings.CONTENT_SAFETY_ENDPOINT.rstrip("/") + "/contentsafety/text:analyze?api-version=2024-09-01"
        try:
            async with httpx.AsyncClient(timeout=settings.CONTENT_SAFETY_TIMEOUT) as c:
                resp = await c.post(url, json={"text": text},
                                    headers={"Ocp-Apim-Subscription-Key": settings.CONTENT_SAFETY_KEY})
                resp.raise_for_status()
                cats = {x["category"]: x["severity"] for x in resp.json().get("categoriesAnalysis", [])}
            flagged = {k: v for k, v in cats.items() if v >= settings.CONTENT_SAFETY_THRESHOLD}
            if flagged:
                reason = "self_harm" if "SelfHarm" in flagged else max(flagged, key=flagged.get).lower()
                return {"safe": False, "reason": reason, "categories": cats, "source": "content_safety"}
            return {"safe": True, "reason": None, "categories": cats, "source": "content_safety"}
        except Exception as e:
            # Content Safety 장애 → 키워드 폴백(명백한 위기 누락 방지)
            r = _keyword_check(text)
            r["source"] = "keyword_fallback"
            r["cs_error"] = str(e)[:140]
            return r
    # 비활성/미설정 → 키워드 stub
    r = _keyword_check(text)
    r["source"] = "keyword"
    return r


async def classify(text: str) -> dict:
    return await clients.classify_one(text)


async def retrieve(text: str) -> list[dict]:
    # 연결 구조만: RETRIEVE_PROVIDER=local(stub) / azure(Azure AI Search — 팀원 구현).
    # 동기 retriever를 스레드로 돌려 gather 동시성을 유지한다.
    return await asyncio.to_thread(_retriever.retrieve, text)


async def synthesize_tts(text: str, tts_options: dict | None) -> dict:
    """
    TTS 실행 후 SSE에 실어보낼 이벤트 페이로드를 만든다.
    실패해도 본문 응답 흐름을 막지 않도록 status: error로 감싸 반환한다.
    """
    voice = (tts_options or {}).get("voice")
    try:
        audio_b64 = await asyncio.to_thread(synthesize_speech_base64, text, voice)
        return {
            "status": "ready",
            "text": text,
            "audio": {"kind": "base64", "data": audio_b64, "mime_type": "audio/wav"},
            "options": tts_options,
        }
    except Exception as exc:
        return {
            "status": "error",
            "text": text,
            "error": str(exc)[:200],
            "options": tts_options,
        }


def rerank(
    candidates: list[dict],
    primary: str,
    confidence: float,
    top_n: int | None = None,
) -> list[dict]:
    top_n = top_n or settings.RERANK_TOP_N
    if not candidates:
        return []

    scores = [float(c.get("score", 0.0)) for c in candidates]
    min_score = min(scores)
    max_score = max(scores)
    span = max_score - min_score

    use_bias = primary not in ("정상", "불충분") and confidence >= 0.5
    deduped: dict[str, dict] = {}

    for candidate in candidates:
        raw_score = float(candidate.get("score", 0.0))
        normalized = 1.0 if span == 0 else (raw_score - min_score) / span
        distortions = candidate.get("metadata", {}).get("distortions", [])
        final_score = normalized + (0.3 if use_bias and primary in distortions else 0.0)

        ranked = {**candidate, "score": final_score}
        candidate_id = ranked.get("id")

        if candidate_id not in deduped or final_score > deduped[candidate_id]["score"]:
            deduped[candidate_id] = ranked

    return sorted(deduped.values(), key=lambda c: c["score"], reverse=True)[:top_n]


async def input_pending_stream(
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
):
    """Accept future STT/TTS payloads even before an STT provider is wired."""
    session = sessions.ensure_session(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    sessions.append_turn(
        session_id,
        {
            "role": "user",
            "text": "",
            "event": "input_pending",
            "input": input_meta,
            "tts": tts,
        },
    )
    yield sse(
        {
            "type": "meta",
            "session_id": session_id,
            "turn_count": sessions.snapshot(session_id)["turn_count"],
            "input": input_meta,
            "tts": tts,
        }
    )
    yield sse(
        {
            "type": "input_required",
            "session_id": session_id,
            "reason": "stt_not_configured",
            "message": "audio-only payload was accepted, but STT is not wired yet. Send text or stt.transcript for now.",
        }
    )
    yield sse({"type": "done", "session_id": session_id})


async def respond_stream(
    text: str,
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
):
    session = sessions.ensure_session(session_id)
    session_id = session["session_id"]
    prior_messages = sessions.recent_llm_messages(session_id)
    input_meta = input_meta or {"input_type": "text"}

    safety, cls, cands = await asyncio.gather(
        safety_check(text),
        classify(text),
        retrieve(text),
    )

    primary = cls["primary"]
    confidence = max(
        (label["score"] for label in cls["labels"] if label["label"] == primary),
        default=0.0,
    )

    sessions.append_turn(
        session_id,
        {
            "role": "user",
            "text": text,
            "primary": primary,
            "safety": "safe" if safety.get("safe") else "blocked",
            "safety_reason": safety.get("reason"),
            "input": input_meta,
            "tts": tts,
        },
    )

    yield sse(
        {
            "type": "meta",
            "session_id": session_id,
            "turn_count": sessions.snapshot(session_id)["turn_count"],
            "primary": primary,
            "mode": cls["mode"],
            "labels": cls["labels"],
            "input": input_meta,
            "tts": tts,
        }
    )

    if not safety["safe"]:
        payload = crisis.crisis_payload(reason=safety.get("reason"))
        yield sse(payload)
        sessions.append_turn(
            session_id,
            {
                "role": "assistant",
                "text": payload.get("message", ""),
                "event": "crisis",
                "blocked": True,
                "reason": payload.get("reason"),
            },
        )
        if tts and tts.get("enabled"):
            tts_event = await synthesize_tts(payload.get("message", ""), tts)
            yield sse({"type": "tts", "session_id": session_id, **tts_event})
        yield sse({"type": "done", "session_id": session_id})
        return

    chunks = rerank(cands, primary, confidence)
    chunk_refs = [{"id": c["id"], "content": c["content"]} for c in chunks]
    yield sse({"type": "chunks", "session_id": session_id, "chunks": chunk_refs})

    sys = (
        "??? ???? ??? ???? ??? ??? ?? ??????. "
        "?? ?? ?? ??? ?? ?? ??? ????, ???? ??? ???? ?? "
        "????? ????. ??? ???? ??: " + primary
    )
    ctx = "\n".join(f"- {c['content']}" for c in chunks)
    messages = [
        {"role": "system", "content": sys + "\n[?? ?? ??]\n" + ctx},
        *prior_messages,
        {"role": "user", "content": text},
    ]

    assistant_parts: list[str] = []
    # NOTE: single-user skeleton: chat_stream is a sync generator; iterating here blocks the loop.
    # For concurrency switch to an async OpenAI client later.
    for tok in respond_stream._llm.chat_stream(messages):
        assistant_parts.append(tok)
        yield sse({"type": "token", "session_id": session_id, "text": tok})

    assistant_text = "".join(assistant_parts).strip()
    if assistant_text:
        sessions.append_turn(
            session_id,
            {
                "role": "assistant",
                "text": assistant_text,
                "event": "respond",
                "primary": primary,
                "rag_chunk_ids": [c["id"] for c in chunks],
            },
        )

    if tts and tts.get("enabled"):
        tts_event = await synthesize_tts(assistant_text, tts)
        yield sse({"type": "tts", "session_id": session_id, **tts_event})

    yield sse({"type": "done", "session_id": session_id})

respond_stream._llm = LLMClient()
