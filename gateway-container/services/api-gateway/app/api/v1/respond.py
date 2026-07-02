from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ...contracts.requests import RespondIn
from ...core.auth import require_api_key
from ...orchestrator import respond_flow
from ...orchestrator.request_context import RespondRequestContext

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/respond")
async def respond(body: RespondIn):
    context = RespondRequestContext.from_body(body)

    if context.requires_stt:
        # STT 를 스트리밍 경로 안에서 수행해 stt processing/completed/error 이벤트를
        # 클라이언트가 그대로 받게 한다 (조용한 fallback 금지).
        return StreamingResponse(
            respond_flow.stt_then_respond_stream(context.session_id, context.input_meta, context.tts, context.llm),
            media_type="text/event-stream",
        )

    if not context.has_text:
        return StreamingResponse(
            respond_flow.input_pending_stream(context.session_id, context.input_meta, context.tts),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        respond_flow.respond_stream(context.text or "", context.session_id, context.input_meta, context.tts, context.llm),
        media_type="text/event-stream",
    )
