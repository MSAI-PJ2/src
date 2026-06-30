"""HTTP clients and response adapters for backend model services."""
from __future__ import annotations

from typing import Any

import httpx

from . import settings


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
                # Two-column style: {"primary":"...", "sub":"..."}
                label = _pick_first(item, ("primary", "primary_label", "sub", "sub_label"))
            if label is None:
                continue
            score = _pick_first(item, ("score", "prob", "probability", "confidence"), 1.0)
            selected = bool(item.get("selected", False)) or (primary is not None and str(label) == primary)
            labels.append(_label_item(str(label), score, selected))
    return labels


def normalize_classify_result(data: dict[str, Any], *, fallback_text: str = "", threshold: float | None = None) -> dict[str, Any]:
    """Normalize several likely classifier shapes to gateway's canonical contract.

    Canonical contract expected downstream:
    {
      text, mode, model, model_version, threshold, primary,
      labels: [{label, score, selected}]
    }

    Accepted alternate shapes include:
    - {primary_label, sub_labels}
    - {primary, sub_label}
    - {label/category, score}
    - {predictions: [{label, score}, ...]}
    - {scores: {label: score, ...}}
    """
    if not isinstance(data, dict):
        raise ValueError("classification response must be a JSON object")

    text = str(data.get("text") or fallback_text or "")
    primary = _pick_first(
        data,
        (
            "primary",
            "primary_label",
            "top_label",
            "label",
            "category",
            "class",
            "prediction",
        ),
    )

    label_source = _pick_first(data, ("labels", "predictions", "classes", "scores", "results"))
    labels = _labels_from_any(label_source, str(primary) if primary is not None else None)

    sub_source = _pick_first(data, ("sub_labels", "sub_label", "secondary", "secondary_labels", "sub"))
    sub_labels = _labels_from_any(sub_source, None)

    # If sub labels were supplied separately, include them as selected context labels.
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

    # Ensure primary is represented and selected.
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


async def classify_one(text: str, threshold: float | None = None) -> dict:
    payload = {"text": text, "threshold": threshold}
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(f"{settings.KLUE_API_URL}/v1/predict", json=payload)
        response.raise_for_status()
        return normalize_classify_result(response.json(), fallback_text=text, threshold=threshold)


async def classify_batch(texts: list[str], threshold: float | None = None) -> dict:
    payload = {"texts": texts, "threshold": threshold}
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(f"{settings.KLUE_API_URL}/v1/batch-predict", json=payload)
        response.raise_for_status()
        data = response.json()

    # Canonical batch shape: {results:[{index, ok, result, error}]}
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
                        "result": normalize_classify_result(
                            item["result"], fallback_text=texts[idx] if idx < len(texts) else "", threshold=threshold
                        ),
                        "error": None,
                    }
                )
            else:
                normalized_items.append(
                    {"index": int(item.get("index", i)), "ok": False, "result": None, "error": item.get("error", "batch item failed")}
                )
        return {"results": normalized_items}

    # Alternate batch shape: raw list of predictions.
    if isinstance(data, list):
        return {
            "results": [
                {
                    "index": i,
                    "ok": True,
                    "result": normalize_classify_result(item, fallback_text=texts[i] if i < len(texts) else "", threshold=threshold),
                    "error": None,
                }
                for i, item in enumerate(data)
                if isinstance(item, dict)
            ]
        }

    return data
