"""/v1/respond 오케스트레이션 — 상담 응답의 전체 흐름.

respond_stream 한 턴의 순서:
    1. 세션 확보 + 최근 대화 로드
    2. safety / classify / retrieve 3-레인 병렬 실행 (asyncio.gather)
    3. context_policy.resolve → 이번 턴의 응답 정책 결정
    4. 위기면 crisis 고정 메시지 출력 후 종료 (LLM 우회)
    5. 정책에 따라 RAG 재정렬 → 프롬프트 구성 → LLM 토큰 스트리밍
    6. (옵션) TTS 합성 → 세션 저장 → done

이 파일은 흐름만 기술한다. 외부 서비스 호출 방법은 services/, 정책은
context_policy.py, 프롬프트 내용은 llm/prompts.py 를 본다.
"""
import asyncio

from ..llm.prompts import build_llm_messages
from ..rag.ranking import rerank
from ..services import services
from ..session import session_repository
from ..session.turns import assistant_turn, crisis_turn, input_pending_turn, stt_failed_turn, user_turn
from ..streaming.events import (
    INPUT_REQUIRED_STT_MESSAGE,
    INPUT_REQUIRED_TEXT_MESSAGE,
    chunks_event,
    done_event,
    input_required_event,
    meta_event,
    stt_processing_event,
    stt_result_event,
    token_event,
    tts_event,
)
from ..streaming.sse import sse
from . import context_policy, crisis
from .request_context import RespondRequestContext, default_text_input_meta


async def stt_then_respond_stream(
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
    llm: dict | None = None,
):
    """오디오 입력: STT 수행 → stt 이벤트 출력 → 성공 시 respond_stream 으로 계속."""
    context = RespondRequestContext(session_id, None, input_meta or {}, tts, llm)
    session = await session_repository.ensure(context.session_id)
    session_id = session["session_id"]
    context = RespondRequestContext(session_id, context.text, context.input_meta, context.tts, context.llm)

    yield sse(stt_processing_event(session_id, context.stt_provider, context.language))

    result = await services.speech.transcribe_audio(context.audio)

    if result.get("status") != "completed" or not result.get("transcript"):
        # STT 실패는 조용히 넘어가지 않는다 — 실패 이벤트와 재입력 요청을 명시적으로 보낸다.
        await session_repository.append_turn(session_id, stt_failed_turn(context.input_meta, result, context.tts))
        yield sse(stt_result_event(session_id, result))
        yield sse(
            input_required_event(
                session_id,
                result.get("status") or "stt_failed",
                INPUT_REQUIRED_STT_MESSAGE,
            )
        )
        yield sse(done_event(session_id))
        return

    context = context.with_transcript(result)
    yield sse(stt_result_event(session_id, result))

    async for event in respond_stream(context.text or "", session_id, context.input_meta, context.tts, context.llm):
        yield event


async def input_pending_stream(
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
):
    """텍스트도 오디오도 없는 요청: 입력을 요청하는 이벤트만 보내고 종료."""
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    await session_repository.append_turn(session_id, input_pending_turn(input_meta, tts))
    snap = await session_repository.snapshot(session_id)

    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts))
    yield sse(input_required_event(session_id, "text_required", INPUT_REQUIRED_TEXT_MESSAGE))
    yield sse(done_event(session_id))


async def respond_stream(
    text: str,
    session_id: str | None = None,
    input_meta: dict | None = None,
    tts: dict | None = None,
    llm: dict | None = None,
):
    # 1. 세션 확보 + 최근 대화 로드
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    prior_messages = await session_repository.recent_llm_messages(session_id)
    input_meta = default_text_input_meta(input_meta)

    # 2. safety / classify / retrieve 3-레인 병렬 실행
    safety, cls, cands = await asyncio.gather(
        services.safety.check(text),
        services.classifier.classify_one(text),
        services.retriever.retrieve(text),
    )

    primary = cls["primary"]
    confidence = max(
        (label["score"] for label in cls["labels"] if label["label"] == primary),
        default=0.0,
    )

    # 3. 컨텍스트 정책 결정 (라벨별 응답 전략 — context_policy.py 에서 편집)
    policy = context_policy.resolve(safety, cls)

    await session_repository.append_turn(session_id, user_turn(text, primary, safety, input_meta, tts))
    snap = await session_repository.snapshot(session_id)
    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts, cls))

    # 4. 위기 분기: LLM 을 호출하지 않고 고정 메시지 + 핫라인 출력 후 종료
    if policy.is_crisis:
        payload = crisis.crisis_payload(reason=safety.get("reason"))
        yield sse(payload)
        await session_repository.append_turn(session_id, crisis_turn(payload))
        if tts and tts.get("enabled"):
            tts_result = await services.speech.synthesize_tts(payload.get("message", ""), tts)
            yield sse(tts_event(session_id, tts_result))
        yield sse(done_event(session_id))
        return

    # 5. 정책에 따른 RAG 재정렬 + 프롬프트 구성 + LLM 스트리밍
    chunks = rerank(cands, primary, confidence, top_n=policy.rag_top_n) if policy.use_rag else []
    yield sse(chunks_event(session_id, chunks))

    messages = build_llm_messages(policy.prompt_strategy, primary, chunks, prior_messages, text)
    assistant_parts: list[str] = []
    async for tok in services.llm.chat_stream_async(messages, llm):
        assistant_parts.append(tok)
        yield sse(token_event(session_id, tok))

    assistant_text = "".join(assistant_parts).strip()
    if assistant_text:
        await session_repository.append_turn(
            session_id,
            assistant_turn(assistant_text, primary, chunks, policy=policy.as_metadata()),
        )

    # 6. (옵션) TTS — 완성된 문장이 있어야 자연스럽게 합성되므로 스트리밍 종료 후 수행
    if tts and tts.get("enabled"):
        tts_result = await services.speech.synthesize_tts(assistant_text, tts)
        yield sse(tts_event(session_id, tts_result))

    yield sse(done_event(session_id))
