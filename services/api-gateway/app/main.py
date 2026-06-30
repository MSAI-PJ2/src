import asyncio
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from common.speech_client import transcribe_audio_input

from . import clients, dag, sessions, settings
from .schemas import BatchClassifyIn, ClassifyIn, RespondIn, SessionCreateIn, SttIn

app = FastAPI(title="mlnode-api-gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


async def require_key(x_api_key: str | None = Header(default=None)):
    if settings.API_KEY_REQUIRED and x_api_key != settings.API_KEY:
        raise HTTPException(401, "invalid api key")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/v1/sessions", dependencies=[Depends(require_key)])
async def create_session(body: SessionCreateIn | None = None):
    return sessions.create_session(body.session_id if body else None)


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_key)])
async def get_session(session_id: str):
    state = sessions.snapshot(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return state


@app.post("/v1/classify", dependencies=[Depends(require_key)])
async def classify(body: ClassifyIn):
    return await clients.classify_one(body.text, body.threshold)


@app.post("/v1/batch-classify", dependencies=[Depends(require_key)])
async def batch_classify(body: BatchClassifyIn):
    return await clients.classify_batch(body.texts, body.threshold)


@app.post("/v1/respond", dependencies=[Depends(require_key)])
async def respond(body: RespondIn):
    # ── STT: 오디오만 왔고 transcript가 없으면 여기서 텍스트로 변환 ──
    # (text가 이미 있거나 stt.transcript가 이미 채워져 있으면 건너뜀 —
    #  effective_text()가 그 경우 그대로 처리하던 기존 동작을 유지)
    if body.audio and not (body.stt and body.stt.transcript):
        try:
            transcript, ok = await asyncio.to_thread(
                transcribe_audio_input, body.audio.model_dump(exclude_none=True)
            )
        except Exception:
            transcript, ok = "", False

        if ok and transcript:
            if body.stt is None:
                body.stt = SttIn(
                    transcript=transcript,
                    provider="azure",
                    language=body.audio.language or "ko-KR",
                )
            else:
                body.stt.transcript = transcript
        # 실패 시 그대로 두면 effective_text()가 None을 반환 →
        # 기존처럼 input_pending_stream으로 빠짐 (동작 변경 없음)

    text = body.effective_text()
    input_meta = body.input_meta()
    tts = body.tts.model_dump(exclude_none=True) if body.tts else None

    if not text:
        # Accept STT/TTS-shaped payloads now, but fail gracefully until STT is wired.
        return StreamingResponse(
            dag.input_pending_stream(body.session_id, input_meta, tts),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        dag.respond_stream(text, body.session_id, input_meta, tts),
        media_type="text/event-stream",
    )
