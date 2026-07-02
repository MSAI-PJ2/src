"""[핵심 흐름] 상담 한 턴이 처음부터 끝까지 진행되는 순서가 이 파일에 있다.

respond_stream 한 턴의 순서:
    1. 세션(대화방) 확보 + 최근 대화 기록 로드
    2. 안전검사 / 인지왜곡 분류 / 참고자료 검색 — 3가지를 동시에 실행
    3. 분류 결과로 응답 정책 결정 (context_policy.py)
    4. 위기 발화면: LLM 을 부르지 않고 고정 위기 메시지 + 핫라인 출력 후 종료
    5. 평상시: 참고자료 정렬 → 프롬프트 구성 → LLM 답변을 글자 단위로 스트리밍
    6. (옵션) 답변을 음성으로 합성 → 대화 기록 저장 → done

읽는 법 — 이 파일의 함수들은 "제너레이터"다:
    yield sse(...) = "이벤트 하나를 프론트로 지금 내보내라". return 처럼 끝나지 않고
    다음 줄로 계속 진행하므로, 위에서 아래로 읽으면 프론트가 받는 이벤트 순서와 같다.
    await = Azure 응답을 기다리는 동안 다른 요청 처리를 양보한다는 표시.
"""
import asyncio

from ..events import (
    INPUT_REQUIRED_STT_MESSAGE, INPUT_REQUIRED_TEXT_MESSAGE,
    chunks_event, done_event, input_required_event, meta_event, sse,
    stt_processing_event, stt_result_event, token_event, tts_event,
)
from ..prompts import build_llm_messages
from ..ranking import rerank
from ..services import services
from ..session import session_repository
from ..session.turns import assistant_turn, crisis_turn, input_pending_turn, stt_failed_turn, user_turn
from . import context_policy, crisis
from .request_context import RespondRequestContext, default_text_input_meta


async def stt_then_respond_stream(session_id=None, input_meta=None, tts=None, llm=None):
    """오디오 입력 흐름: 음성→텍스트(STT) 변환 후, 성공하면 일반 상담 흐름으로 넘어간다."""
    context = RespondRequestContext(session_id, None, input_meta or {}, tts, llm)
    session = await session_repository.ensure(context.session_id)  # 세션이 없으면 새로 만든다
    session_id = session["session_id"]
    context = RespondRequestContext(session_id, None, context.input_meta, tts, llm)

    # "인식 시작" 알림을 먼저 보내고, Azure Speech 로 음성을 텍스트로 변환
    yield sse(stt_processing_event(session_id, context.stt_provider, context.language))
    result = await services.speech.transcribe_audio(context.audio)

    if result.get("status") != "completed" or not result.get("transcript"):
        # STT 실패: 조용히 넘어가지 않고 실패 이벤트 + "다시 입력해달라" 요청을 명시적으로 보낸다
        await session_repository.append_turn(session_id, stt_failed_turn(context.input_meta, result, tts))
        yield sse(stt_result_event(session_id, result))
        yield sse(input_required_event(session_id, result.get("status") or "stt_failed",
                                       INPUT_REQUIRED_STT_MESSAGE))
        yield sse(done_event(session_id))
        return

    # STT 성공: 전사문을 반영한 컨텍스트로 바꾸고 일반 상담 흐름을 이어서 실행
    context = context.with_transcript(result)
    yield sse(stt_result_event(session_id, result))
    async for event in respond_stream(context.text or "", session_id, context.input_meta, tts, llm):
        yield event  # respond_stream 이 내보내는 이벤트를 그대로 통과시킨다


async def input_pending_stream(session_id=None, input_meta=None, tts=None):
    """텍스트도 오디오도 없는 요청: "입력을 보내달라"는 안내만 보내고 끝낸다."""
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    await session_repository.append_turn(session_id, input_pending_turn(input_meta, tts))
    snap = await session_repository.snapshot(session_id)

    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts))
    yield sse(input_required_event(session_id, "text_required", INPUT_REQUIRED_TEXT_MESSAGE))
    yield sse(done_event(session_id))


async def respond_stream(text: str, session_id=None, input_meta=None, tts=None, llm=None):
    """일반 상담 흐름 — 이 서비스의 심장. 위 모듈 docstring 의 1~6 단계와 대응한다."""
    # 1) 세션 확보 + 최근 대화 로드 (LLM 이 맥락을 이어가도록 이전 발화들을 가져온다)
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    prior_messages = await session_repository.recent_llm_messages(session_id)
    input_meta = default_text_input_meta(input_meta)

    # 2) 세 가지 분석을 "동시에" 실행 — gather 는 병렬 실행 후 셋 다 끝나면 결과를 준다.
    #    순서대로 하면 3번 기다려야 할 것을 1번 기다리는 시간으로 줄이는 것.
    safety, cls, cands = await asyncio.gather(
        services.safety.check(text),        # 위험(자살/자해) 발화인지
        services.classifier.classify_one(text),   # 인지왜곡 12분류 중 무엇인지
        services.retriever.retrieve(text),  # 관련 상담기법 자료 검색(RAG)
    )
    primary = cls["primary"]  # 대표 라벨 (예: "흑백 사고")
    # 대표 라벨의 확신 점수를 찾는다 (라벨 목록에서 primary 와 같은 항목의 score)
    confidence = max((l["score"] for l in cls["labels"] if l["label"] == primary), default=0.0)

    # 3) 이번 턴을 어떻게 응답할지 정책 결정 — 규칙은 context_policy.py 에서 편집
    policy = context_policy.resolve(safety, cls)

    # 사용자 발화를 대화 기록에 저장하고, 분류 결과를 meta 이벤트로 프론트에 먼저 알린다
    await session_repository.append_turn(session_id, user_turn(text, primary, safety, input_meta, tts))
    snap = await session_repository.snapshot(session_id)
    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts, cls))

    # 4) 위기 분기: LLM 답변 생성 없이 고정 메시지 + 상담 핫라인을 즉시 출력하고 종료
    if policy.is_crisis:
        payload = crisis.crisis_payload(reason=safety.get("reason"))
        yield sse(payload)
        await session_repository.append_turn(session_id, crisis_turn(payload))
        if tts and tts.get("enabled"):
            yield sse(tts_event(session_id, await services.speech.synthesize_tts(payload.get("message", ""), tts)))
        yield sse(done_event(session_id))
        return

    # 5) 참고자료 정렬 → 프롬프트 구성 → LLM 스트리밍
    #    정책이 RAG 를 끄면(chunks=[]) 참고자료 없이 답변한다
    #    cls_labels: multi_label 모델의 selected 판정을 가산점 조건으로 쓸 수 있게 전달
    chunks = rerank(cands, primary, confidence, top_n=policy.rag_top_n,
                    cls_labels=cls["labels"]) if policy.use_rag else []
    yield sse(chunks_event(session_id, chunks))

    # 시스템 프롬프트(상담 스타일·라벨 지침) + 이전 대화 + 이번 발화 → LLM 입력 메시지
    messages = build_llm_messages(policy.prompt_strategy, primary, chunks, prior_messages, text)
    assistant_parts: list[str] = []
    # LLM 이 글자를 생성하는 대로 token 이벤트로 즉시 내보낸다 (타자 치듯 보이는 효과)
    async for tok in services.llm.chat_stream_async(messages, llm):
        assistant_parts.append(tok)
        yield sse(token_event(session_id, tok))

    # 조각들을 합쳐 완성된 답변을 만들고, 어떤 정책·확신으로 생성했는지와 함께 저장
    # (confidence 를 남겨야 운영 후 "저확신 강등이 몇 번 일어났나"를 DB 에서 집계할 수 있다)
    assistant_text = "".join(assistant_parts).strip()
    if assistant_text:
        policy_meta = {**policy.as_metadata(), "confidence": round(confidence, 4)}
        await session_repository.append_turn(
            session_id, assistant_turn(assistant_text, primary, chunks, policy=policy_meta))

    # 6) (옵션) 음성 합성 — 문장이 완성된 뒤에 해야 자연스러워서 스트리밍이 끝난 후 수행
    if tts and tts.get("enabled"):
        yield sse(tts_event(session_id, await services.speech.synthesize_tts(assistant_text, tts)))

    yield sse(done_event(session_id))
