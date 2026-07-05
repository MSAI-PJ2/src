"""[분류기 창구] 인지왜곡 분류 모델(cogdist 컨테이너)에 문장을 보내고 결과를 받는다.

분류기 응답의 정식 형식(계약):
    {text, mode, model, model_version, threshold, primary,
     labels: [{label, score, selected}]}
primary = 대표 라벨(예: "흑백 사고"), labels = 라벨별 점수 목록.
형식이 다르면 오류를 낸다 — 게이트웨이에서 억지로 맞춰주지 않고 분류기를 고친다.
"""
from typing import Any

import httpx

from .. import settings

# HTTP 연결을 재사용하기 위한 공용 클라이언트 (요청마다 새로 연결하면 매번
# TLS 핸드셰이크 비용이 들어 느려진다). 첫 사용 시 한 번만 만든다.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT_SECONDS)
    return _client


def _as_float(value: Any, default: float) -> float:
    """숫자로 변환, 실패하면 기본값 (분류기가 문자열 숫자를 보내는 경우 대비)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_result(data: dict[str, Any], *, fallback_text: str = "",
                 threshold: float | None = None) -> dict[str, Any]:
    """분류기 응답을 검증하고 게이트웨이 표준 형태로 정리한다. 계약 위반이면 즉시 오류."""
    if not isinstance(data, dict) or "primary" not in data:
        raise ValueError("classifier response missing 'primary'")
    if not isinstance(data.get("labels"), list) or not data["labels"]:
        raise ValueError("classifier response missing 'labels'")

    labels = [
        {"label": str(item["label"]),
         "score": round(_as_float(item.get("score", 1.0), 1.0), 4),
         "selected": bool(item.get("selected", False))}
        for item in data["labels"]
    ]
    return {
        "text": str(data.get("text") or fallback_text or ""),
        "mode": str(data.get("mode") or "single"),
        "model": str(data.get("model") or "unknown"),
        "model_version": str(data.get("model_version") or "unknown"),
        "threshold": _as_float(data.get("threshold", threshold if threshold is not None else 0.5), 0.5),
        "primary": str(data["primary"]),
        "labels": labels,
    }


class ClassifierAdapter:
    async def classify_one(self, text: str, threshold: float | None = None) -> dict:
        """문장 1개 분류: cogdist 의 /v1/predict 호출 → 검증 → 표준 형태로 반환."""
        response = await _http().post(f"{settings.KLUE_API_URL}/v1/predict",
                                      json={"text": text, "threshold": threshold})
        response.raise_for_status()  # HTTP 오류(4xx/5xx)면 여기서 예외 발생
        return parse_result(response.json(), fallback_text=text, threshold=threshold)

    async def explain_text(self, text: str, label: str | None = None) -> dict:
        """문장 1개의 SHAP 토큰 기여도: cogdist 의 /v1/explain 호출. 캐싱 없이 호출마다 새로 계산한다.

        SHAP 계산은 /v1/predict 보다 훨씬 느릴 수 있어 전용 타임아웃(SHAP_REQUEST_TIMEOUT_SECONDS)을 쓴다.
        """
        response = await _http().post(f"{settings.KLUE_API_URL}/v1/explain",
                                      json={"text": text, "label": label},
                                      timeout=settings.SHAP_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    async def classify_batch(self, texts: list[str], threshold: float | None = None) -> dict:
        """문장 여러 개 분류. 항목별로 성공(ok=True)/실패를 구분해서 돌려준다."""
        response = await _http().post(f"{settings.KLUE_API_URL}/v1/batch-predict",
                                      json={"texts": texts, "threshold": threshold})
        response.raise_for_status()
        data = response.json()

        items = []
        for i, item in enumerate(data.get("results", []) if isinstance(data, dict) else []):
            if isinstance(item, dict) and item.get("ok", True) and item.get("result") is not None:
                idx = int(item.get("index", i))
                items.append({"index": idx, "ok": True, "error": None,
                              "result": parse_result(item["result"],
                                                     fallback_text=texts[idx] if idx < len(texts) else "",
                                                     threshold=threshold)})
            else:
                items.append({"index": int(item.get("index", i)) if isinstance(item, dict) else i,
                              "ok": False, "result": None,
                              "error": (item or {}).get("error", "batch item failed") if isinstance(item, dict) else "invalid batch item"})
        return {"results": items}
