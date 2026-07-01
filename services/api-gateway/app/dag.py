import asyncio
from common.llm_client import LLMClient
from common.speech_client import transcribe_audio_input_detailed
from retrieve.client import get_retriever

from . import clients, crisis, sessions
from .events import sse
from .ranking import rerank
from .safety import safety_check
from .tts import synthesize_tts

_retriever = get_retriever()



async def classify(text: str) -> dict:
    return await clients.classify_one(text)


async def retrieve(text: str) -> list[dict]:
    # 연결 구조만: RETRIEVE_PROVIDER=local(stub) / azure(Azure AI Search).
    # 동기 retriever를 스레드로 돌려 gather 동시성을 유지한다.
    return await asyncio.to_thread(_retriever.retrieve, text)


async def stt_then_respond_stream(
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
):
    """Transcribe audio, emit STT debug events, then continue the existing DAG."""
    session = sessions.ensure_session(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    audio = input_meta.get("audio") or {}
    stt_options = input_meta.get("stt") or {}
    language = stt_options.get("language") or audio.get("language") or "ko-KR"

    yield sse({
        "type": "stt",
        "session_id": session_id,
        "status": "processing",
        "provider": stt_options.get("provider") or "azure",
        "language": language,
    })

    result = await asyncio.to_thread(transcribe_audio_input_detailed, audio)

    if result.get("status") != "completed" or not result.get("transcript"):
        sessions.append_turn(
            session_id,
            {
                "role": "user",
                "text": "",
                "event": "stt_failed",
                "input": input_meta,
                "stt_result": result,
                "tts": tts,
            },
        )
        yield sse({"type": "stt", "session_id": session_id, **result})
        yield sse({
            "type": "input_required",
            "session_id": session_id,
            "reason": result.get("status") or "stt_failed",
            "message": "audio payload was accepted, but STT did not produce a transcript. Check stt event error/reason, or send text/stt.transcript.",
        })
        yield sse({"type": "done", "session_id": session_id})
        return

    input_meta = {
        **input_meta,
        "input_type": "transcript",
        "stt": {
            **stt_options,
            "provider": result.get("provider"),
            "language": result.get("language") or language,
            "transcript": result.get("transcript"),
            "confidence": result.get("confidence"),
            "recognition_status": result.get("recognition_status"),
        },
    }
    yield sse({"type": "stt", "session_id": session_id, **result})

    async for event in respond_stream(result["transcript"], session_id, input_meta, tts):
        yield event

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
            "reason": "text_required",
            "message": "No text or transcript was provided. Send text, stt.transcript, or an audio payload.",
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
