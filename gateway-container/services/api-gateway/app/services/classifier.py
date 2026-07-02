"""인지왜곡 분류기(cogdist Container App) 클라이언트.

운영 계약(strict) — cogdist /v1/predict 응답:
    {text, mode, model, model_version, threshold, primary,
     labels: [{label, score, selected}]}

과거 프로토타입 분류기들의 다양한 응답 형태(primary_label/top_label/predictions 등)는
legacy 정규화(_normalize_legacy)로만 받아준다. legacy 경로를 타면 warning 로그를
남긴다 — fallback 은 허용하되 조용히 지나가지 않게 한다.
CLASSIFIER_RESPONSE_MODE=strict 로 두면 legacy 응답은 즉시 오류가 된다.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..core import settings

logger = logging.getLogger(__name__)

# 커넥션 풀을 재사용하는 공용 클라이언트 (요청마다 새로 만들면 TLS 핸드셰이크 낭비)
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT_SECONDS)
    return _client


class ClassifierAdapter:
    async def classify_one(self, text: str, threshold: float | None = None) -> dict:
        payload = {"text": text, "threshold": threshold}
        response = await _http().post(f"{settings.KLUE_API_URL}/v1/predict", json=payload)
        response.raise_for_status()
        return _parse_result(response.json(), fallback_text=text, threshold=threshold)

    async def classify_batch(self, texts: list[str], threshold: float | None = None) -> dict:
        payload = {"texts": texts, "threshold": threshold}
        response = await _http().post(f"{settings.KLUE_API_URL}/v1/batch-predict", json=payload)
        response.raise_for_status()
        data = response.json()

        # 정식 배치 형태: {results:[{index, ok, result, error}]}
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            normalized_items = []
            for i, item in enumerate(data["results"]):
                if not isinstance(item, dict):
                    normalized_items.append({"index": i, "ok": False, "result": None, "error": "invalid batch item"})
                    continue
                if item.get("ok", True) and item.get("result") is not None:
                    idx = int(item.get("index", i))
                    normalized_items.append(
                        {
                            "index": idx,
                            "ok": True,
                            "result": _parse_result(
                                item["result"],
                                fallback_text=texts[idx] if idx < len(texts) else "",
                                threshold=threshold,
                            ),
                            "error": None,
                        }
                    )
                else:
                    normalized_items.append(
                        {"index": int(item.get("index", i)), "ok": False, "result": None,
                         "error": item.get("error", "batch item failed")}
                    )
            return {"results": normalized_items}

        # 대체 배치 형태: 예측 dict 의 raw 리스트
        if isinstance(data, list):
            return {
                "results": [
                    {
                        "index": i,
                        "ok": True,
                        "result": _parse_result(item, fallback_text=texts[i] if i < len(texts) else "",
                                                threshold=threshold),
                        "error": None,
                    }
                    for i, item in enumerate(data)
                    if isinstance(item, dict)
                ]
            }

        return data


# ---------------------------------------------------------------------------
# strict 파서 (운영 경로)
# ---------------------------------------------------------------------------

def _parse_result(data: dict[str, Any], *, fallback_text: str = "", threshold: float | None = None) -> dict[str, Any]:
    try:
        return _parse_strict(data, fallback_text=fallback_text, threshold=threshold)
    except ValueError as exc:
        if settings.CLASSIFIER_RESPONSE_MODE == "strict":
            raise
        logger.warning("classifier legacy normalization used (%s) — 응답 형태 확인 필요", exc)
        return _normalize_legacy(data, fallback_text=fallback_text, threshold=threshold)


def _parse_strict(data: dict[str, Any], *, fallback_text: str = "", threshold: float | None = None) -> dict[str, Any]:
    """정식 계약만 허용: primary 와 labels[{label, score, selected}] 필수."""
    if not isinstance(data, dict):
        raise ValueError("classification response must be a JSON object")
    if "primary" not in data:
        raise ValueError("classifier response missing 'primary'")
    if not isinstance(data.get("labels"), list) or not data["labels"]:
        raise ValueError("classifier response missing 'labels'")

    labels = []
    for item in data["labels"]:
        if not isinstance(item, dict) or "label" not in item:
            raise ValueError("labels item must be {label, score, selected}")
        labels.append(
            {
                "label": str(item["label"]),
                "score": round(_as_float(item.get("score", 1.0), 1.0), 4),
                "selected": bool(item.get("selected", False)),
            }
        )

    return {
        "text": str(data.get("text") or fallback_text or ""),
        "mode": str(data.get("mode") or "single"),
        "model": str(data.get("model") or "unknown"),
        "model_version": str(data.get("model_version") or "unknown"),
        "threshold": _as_float(data.get("threshold", threshold if threshold is not None else 0.5), 0.5),
        "primary": str(data["primary"]),
        "labels": labels,
    }


# ---------------------------------------------------------------------------
# legacy 정규화 (과거 프로토타입 분류기 호환 — 신규 코드에서 의존 금지)
# ---------------------------------------------------------------------------

def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick_first(data: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None:
            return value
    return default


def _label_item(label: str, score: Any = 1.0, selected: bool = False) -> dict[str, Any]:
    return {"label": str(label), "score": round(_as_float(score, 1.0), 4), "selected": bool(selected)}


def _labels_from_any(raw: Any, primary: str | None = None) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    if raw is None:
        return labels

    if isinstance(raw, dict):
        # {"labelA": 0.7, "labelB": 0.2}
        for label, score in raw.items():
            labels.append(_label_item(label, score, primary is not None and str(label) == primary))
        return labels

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                labels.append(_label_item(item, 1.0, primary is not None and item == primary))
                continue
            if not isinstance(item, dict):
                continue
            label = _pick_first(item, ("label", "name", "class", "category", "value"))
            if label is None:
                label = _pick_first(item, ("primary", "primary_label", "sub", "sub_label"))
            if label is None:
                continue
            score = _pick_first(item, ("score", "prob", "probability", "confidence"), 1.0)
            selected = bool(item.get("selected", False)) or (primary is not None and str(label) == primary)
            labels.append(_label_item(str(label), score, selected))
    return labels


def _normalize_legacy(data: dict[str, Any], *, fallback_text: str = "", threshold: float | None = None) -> dict[str, Any]:
    """과거 테스트 분류기들의 여러 응답 형태를 정식 계약으로 정규화한다."""
    if not isinstance(data, dict):
        raise ValueError("classification response must be a JSON object")

    text = str(data.get("text") or fallback_text or "")
    primary = _pick_first(
        data,
        ("primary", "primary_label", "top_label", "label", "category", "class", "prediction"),
    )

    label_source = _pick_first(data, ("labels", "predictions", "classes", "scores", "results"))
    labels = _labels_from_any(label_source, str(primary) if primary is not None else None)

    sub_source = _pick_first(data, ("sub_labels", "sub_label", "secondary", "secondary_labels", "sub"))
    sub_labels = _labels_from_any(sub_source, None)

    existing = {item["label"] for item in labels}
    for item in sub_labels:
        if item["label"] not in existing:
            item["selected"] = True
            labels.append(item)
            existing.add(item["label"])

    if primary is None and labels:
        selected = [item for item in labels if item.get("selected")]
        primary = (selected or sorted(labels, key=lambda x: x.get("score", 0), reverse=True))[0]["label"]

    if primary is None:
        primary = "불충분"

    if not labels:
        labels = [_label_item(str(primary), data.get("score", data.get("confidence", 1.0)), True)]

    if str(primary) not in {item["label"] for item in labels}:
        labels.insert(0, _label_item(str(primary), data.get("score", data.get("confidence", 1.0)), True))
    else:
        for item in labels:
            if item["label"] == str(primary):
                item["selected"] = True

    return {
        "text": text,
        "mode": str(data.get("mode") or data.get("task") or "normalized"),
        "model": str(data.get("model") or data.get("model_id") or "unknown"),
        "model_version": str(data.get("model_version") or data.get("version") or "unknown"),
        "threshold": _as_float(data.get("threshold", threshold if threshold is not None else 0.5), 0.5),
        "primary": str(primary),
        "labels": labels,
    }
