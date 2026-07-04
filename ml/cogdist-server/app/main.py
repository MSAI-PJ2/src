import logging

from fastapi import FastAPI, HTTPException, Response, status

from app.model import Classifier
from app.schemas import BatchItem, BatchPredictRequest, BatchResponse, ClassifyResult, PredictRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="cogdist classifier", version="2.0.0")

classifier: Classifier | None = None
ready = False


@app.on_event("startup")
def startup() -> None:
    global classifier, ready
    try:
        classifier = Classifier()
        ready = True
        logger.info("Classifier loaded: mode=%s labels=%s", classifier.mode, classifier.labels)
    except Exception:
        ready = False
        logger.exception("Failed to load classifier")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response) -> dict:
    if ready and classifier is not None:
        return {"status": "ready", "mode": classifier.mode, "labels": classifier.labels}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "loading"}


@app.post("/v1/predict", response_model=ClassifyResult)
def predict(req: PredictRequest) -> dict:
    if not ready or classifier is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"status": "loading"})
    # 배치 경로(model.batch_predict)와 동일한 계약: 빈 문자열에 허구 분류를 만들지 않는다
    # (CLS/SEP 만으로도 sigmoid 는 12개 점수를 뱉는다 — 2026-07-04 검수에서 확인된 모순 수정)
    if not isinstance(req.text, str) or not req.text.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="text must be a non-empty string")
    return classifier.predict(req.text, req.threshold)


@app.post("/v1/batch-predict", response_model=BatchResponse)
def batch_predict(req: BatchPredictRequest) -> BatchResponse:
    if not ready or classifier is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"status": "loading"})
    raw = classifier.batch_predict(req.texts, req.threshold)
    return BatchResponse(
        results=[
            BatchItem(index=i, ok=item.get("ok", False), result=item.get("result"), error=item.get("error"))
            for i, item in enumerate(raw)
        ]
    )
