import asyncio
from common.llm_client import LLMClient
from common.speech_client import transcribe_audio_input_detailed
from retrieve.client import get_retriever

from . import clients, crisis
from .events import sse
from .payloads import (
    INPUT_REQUIRED_STT_MESSAGE,
    INPUT_REQUIRED_TEXT_MESSAGE,
    chunks_payload,
    done_payload,
    input_required_payload,
    meta_payload,
    stt_processing_payload,
    stt_result_payload,
    token_payload,
    tts_payload,
)
from .prompts import build_llm_messages
from .ranking import rerank
from .repositories import session_repository
from .request_context import RespondRequestContext, default_text_input_meta
from .safety import safety_check
from .tts import synthesize_tts
from .turns import assistant_turn, crisis_turn, input_pending_turn, stt_failed_turn, user_turn

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
    context = RespondRequestContext(session_id, None, input_meta or {}, tts)
    session = session_repository.ensure(context.session_id)
    session_id = session["session_id"]
    context = RespondRequestContext(session_id, context.text, context.input_meta, context.tts)

    yield sse(stt_processing_payload(session_id, context.stt_provider, context.language))

    result = await asyncio.to_thread(transcribe_audio_input_detailed, context.audio)

    if result.get("status") != "completed" or not result.get("transcript"):
        session_repository.append_turn(session_id, stt_failed_turn(context.input_meta, result, context.tts))
        yield sse(stt_result_payload(session_id, result))
        yield sse(
            input_required_payload(
                session_id,
                result.get("status") or "stt_failed",
                INPUT_REQUIRED_STT_MESSAGE,
            )
        )
        yield sse(done_payload(session_id))
        return

    context = context.with_transcript(result)
    yield sse(stt_result_payload(session_id, result))

    async for event in respond_stream(context.text or "", session_id, context.input_meta, context.tts):
        yield event


async def input_pending_stream(
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
):
    """Accept future STT/TTS payloads even before an STT provider is wired."""
    session = session_repository.ensure(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    session_repository.append_turn(session_id, input_pending_turn(input_meta, tts))
    turn_count = session_repository.snapshot(session_id)["turn_count"]

    yield sse(meta_payload(session_id, turn_count, input_meta, tts))
    yield sse(input_required_payload(session_id, "text_required", INPUT_REQUIRED_TEXT_MESSAGE))
    yield sse(done_payload(session_id))


async def respond_stream(
    text: str,
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
):
    session = session_repository.ensure(session_id)
    session_id = session["session_id"]
    prior_messages = session_repository.recent_llm_messages(session_id)
    input_meta = default_text_input_meta(input_meta)

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

    session_repository.append_turn(session_id, user_turn(text, primary, safety, input_meta, tts))
    turn_count = session_repository.snapshot(session_id)["turn_count"]
    yield sse(meta_payload(session_id, turn_count, input_meta, tts, cls))

    if not safety["safe"]:
        payload = crisis.crisis_payload(reason=safety.get("reason"))
        yield sse(payload)
        session_repository.append_turn(session_id, crisis_turn(payload))
        if tts and tts.get("enabled"):
            tts_event = await synthesize_tts(payload.get("message", ""), tts)
            yield sse(tts_payload(session_id, tts_event))
        yield sse(done_payload(session_id))
        return

    chunks = rerank(cands, primary, confidence)
    yield sse(chunks_payload(session_id, chunks))

    messages = build_llm_messages(primary, chunks, prior_messages, text)
    assistant_parts: list[str] = []
    # NOTE: single-user skeleton: chat_stream is a sync generator; iterating here blocks the loop.
    # For concurrency switch to an async OpenAI client later.
    for tok in respond_stream._llm.chat_stream(messages):
        assistant_parts.append(tok)
        yield sse(token_payload(session_id, tok))

    assistant_text = "".join(assistant_parts).strip()
    if assistant_text:
        session_repository.append_turn(session_id, assistant_turn(assistant_text, primary, chunks))

    if tts and tts.get("enabled"):
        tts_event = await synthesize_tts(assistant_text, tts)
        yield sse(tts_payload(session_id, tts_event))

    yield sse(done_payload(session_id))

respond_stream._llm = LLMClient()
