from pydantic import BaseModel


class LabelScore(BaseModel):
    label: str
    score: float
    selected: bool


class PredictRequest(BaseModel):
    text: str
    threshold: float | None = None


class BatchPredictRequest(BaseModel):
    texts: list[str]
    threshold: float | None = None


class ClassifyResult(BaseModel):
    text: str
    mode: str
    model: str
    model_version: str
    threshold: float
    primary: str
    labels: list[LabelScore]


class BatchItem(BaseModel):
    index: int
    ok: bool
    result: ClassifyResult | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    results: list[BatchItem]
