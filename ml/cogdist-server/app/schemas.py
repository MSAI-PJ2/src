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


class ExplainRequest(BaseModel):
    text: str
    label: str | None = None          # 생략하면 primary 라벨 기준으로 설명
    max_evals: int | None = None      # 생략하면 settings.SHAP_MAX_EVALS


class TokenContribution(BaseModel):
    token: str
    shap_value: float


class ExplainResult(BaseModel):
    text: str
    label: str            # 실제로 설명에 사용된 라벨 (label 생략 시 primary 로 채워짐)
    primary: str           # 참고용 — 이 문장의 분류기 대표 라벨
    base_value: float      # SHAP 기준값 — logit 공간 (tests/SHAP/shap_visual.py 와 동일 관례)
    tokens: list[TokenContribution]  # shap_value 도 logit 공간: + 는 해당 라벨 쪽으로 강화, − 는 약화
