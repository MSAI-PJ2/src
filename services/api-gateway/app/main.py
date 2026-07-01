import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse


from . import clients, dag, settings
from .repositories import session_repository
from .request_context import RespondRequestContext
from .schemas import BatchClassifyIn, ClassifyIn, RespondIn, SessionCreateIn

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
    return session_repository.create(body.session_id if body else None)


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_key)])
async def get_session(session_id: str):
    state = session_repository.snapshot(session_id)
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
    context = RespondRequestContext.from_body(body)

    if context.requires_stt:
        # Keep STT inside the streaming path so clients can receive explicit
        # stt processing/completed/error events instead of a silent fallback.
        return StreamingResponse(
            dag.stt_then_respond_stream(context.session_id, context.input_meta, context.tts, context.llm),
            media_type="text/event-stream",
        )

    if not context.has_text:
        return StreamingResponse(
            dag.input_pending_stream(context.session_id, context.input_meta, context.tts),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        dag.respond_stream(context.text or "", context.session_id, context.input_meta, context.tts, context.llm),
        media_type="text/event-stream",
    )
