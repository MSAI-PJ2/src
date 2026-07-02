"""CogDist v2 모델 서버.

실제 모델 파일은 기본적으로 Azure Files에서 `/models/cogdist`에 마운트된다.
`ml/outputs/multi_large/best` 모델의 label map과 threshold.json 구조를 기준으로 한다.
"""
from __future__ import annotations

import json
import os
from typing import Any

import numpy as np
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from app import settings
from app.selection_policy import normalize_multilabel_selection

def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -500, 500)))


def softmax(x: np.ndarray) -> np.ndarray:
    z = x - np.max(x)
    exp = np.exp(z)
    return exp / np.sum(exp)


def load_threshold(model_dir: str, default: float) -> float:
    path = os.path.join(model_dir, "threshold.json")
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return float(data.get("threshold", default))




class Classifier:
    def __init__(self) -> None:
        self.tokenizer = AutoTokenizer.from_pretrained(settings.MODEL_PATH)
        self.model = AutoModelForSequenceClassification.from_pretrained(settings.MODEL_PATH).eval()
        self.labels = [self.model.config.id2label[i] for i in range(self.model.config.num_labels)]
        self.threshold = load_threshold(settings.MODEL_PATH, settings.DEFAULT_THRESHOLD)

        problem_type = getattr(self.model.config, "problem_type", None) or "multi_label_classification"
        self.mode = (
            {
                "single_label_classification": "single_label",
                "multi_label_classification": "multi_label",
            }.get(problem_type, "multi_label")
            if settings.CLASSIFY_MODE == "auto"
            else settings.CLASSIFY_MODE
        )

    def _logits(self, texts: list[str]) -> np.ndarray:
        enc = self.tokenizer(
            texts,
            truncation=True,
            max_length=settings.MAX_LENGTH,
            padding=True,
            return_tensors="pt",
        )
        with torch.no_grad():
            return self.model(**enc).logits.cpu().numpy()

    def _score_one(self, logits_row: np.ndarray, threshold: float | None = None) -> dict[str, Any]:
        effective_threshold = float(threshold if threshold is not None else self.threshold)

        if self.mode == "single_label":
            probs = softmax(logits_row)
            primary_idx = int(probs.argmax())
            primary = self.labels[primary_idx]
            labels = [
                {
                    "label": self.labels[i],
                    "score": round(float(probs[i]), 4),
                    "selected": i == primary_idx,
                }
                for i in range(len(self.labels))
            ]
        else:
            probs = sigmoid(logits_row)
            primary_idx = int(probs.argmax())
            primary = self.labels[primary_idx]
            labels = [
                {
                    "label": self.labels[i],
                    "score": round(float(probs[i]), 4),
                    "selected": False,
                }
                for i in range(len(self.labels))
            ]
            labels = normalize_multilabel_selection(labels, primary, effective_threshold)

        return {
            "mode": self.mode,
            "model": settings.MODEL_ID,
            "model_version": settings.MODEL_VERSION,
            "threshold": effective_threshold,
            "primary": primary,
            "labels": labels,
        }

    def predict(self, text: str, threshold: float | None = None) -> dict[str, Any]:
        result = self._score_one(self._logits([text])[0], threshold)
        result["text"] = text
        return result

    def batch_predict(self, texts: list[str], threshold: float | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(texts)

        for start in range(0, len(texts), settings.MAX_BATCH_SIZE):
            end = min(start + settings.MAX_BATCH_SIZE, len(texts))
            chunk = texts[start:end]
            valid: list[tuple[int, str]] = []

            for offset, text in enumerate(chunk):
                idx = start + offset
                if isinstance(text, str) and text.strip():
                    valid.append((idx, text))
                else:
                    results[idx] = {"ok": False, "error": "text must be a non-empty string"}

            if not valid:
                continue

            try:
                logits = self._logits([text for _, text in valid])
                for row_idx, (idx, text) in enumerate(valid):
                    result = self._score_one(logits[row_idx], threshold)
                    result["text"] = text
                    results[idx] = {"ok": True, "result": result}
            except Exception as exc:
                for idx, _ in valid:
                    results[idx] = {"ok": False, "error": str(exc)}

        return [item or {"ok": False, "error": "unknown batch error"} for item in results]
