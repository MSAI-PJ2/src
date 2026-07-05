"""CogDist v2 모델 서버.

실제 모델 파일은 기본적으로 Azure Files에서 `/models/cogdist`에 마운트된다.
`ml/outputs/multi_large/best` 모델의 label map과 threshold.json 구조를 기준으로 한다.
"""
from __future__ import annotations

import json
import os
from typing import Any

import numpy as np
import shap
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
    # 4자리 반올림: 점수도 4자리로 반올림해 비교하므로(_score_one) 기준값도 같은
    # 정밀도로 맞춘다. 튜닝 산출물의 부동소수점 노이즈(예: 0.5500000000000002)가
    # 경계 밴드 [0.55, 0.55005) 라벨을 조용히 탈락시키던 문제의 수정 (2026-07-04 검수).
    return round(float(data.get("threshold", default)), 4)




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
        # SHAP 설명(연산 과정 보기)용 — 모델 토크나이저 기준으로 텍스트를 마스킹한다.
        # 무거운 객체가 아니라 요청마다 새로 만들지 않고 1회 생성해 재사용한다.
        self._shap_masker = shap.maskers.Text(self.tokenizer)

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

    # ------------------------------------------------------------------
    # SHAP 설명 ("연산 과정 보기") — 캐싱 없이 호출마다 새로 계산한다.
    # (tests/SHAP/shap_visual.py)과 동일한 방식: output_names 멀티아웃풋 + logit 공간.
    # 라벨 12개를 한 번에 계산한 뒤 타깃 라벨 열만 추출한다 — 라벨 하나만 계산하던
    # 이전 방식보다 코드가 단순하면서 계산 비용도 늘지 않는다(마스킹 횟수가 비용을 결정).
    # ------------------------------------------------------------------

    def _label_index(self, label: str) -> int:
        try:
            return self.labels.index(label)
        except ValueError:
            raise ValueError(f"unknown label: {label}") from None

    def _predict_logits(self, texts: list[str]) -> np.ndarray:
        """texts(list[str]) -> 전체 12개 라벨의 raw logits(np.ndarray, shape (n, 12)).

        (shap_visual.py)과 동일하게 sigmoid 전 logit 공간에서 SHAP을 계산한다.
        """
        texts = ["" if t is None else str(t) for t in texts]
        return self._logits(texts) if texts else np.zeros((0, len(self.labels)))

    def explain(self, text: str, label: str | None = None, max_evals: int | None = None) -> dict[str, Any]:
        """text 한 문장에 대해, 지정한 라벨(생략 시 primary) 기준 토큰별 SHAP 기여도(logit 공간)를 구한다."""
        base = self.predict(text)
        target_label = label or base["primary"]
        label_idx = self._label_index(target_label)

        explainer = shap.Explainer(self._predict_logits, self._shap_masker, output_names=self.labels)
        evals = max_evals or settings.SHAP_MAX_EVALS
        sv = explainer([text], max_evals=evals, batch_size=16)

        tokens_raw = list(sv.data[0])
        values_all = np.array(sv.values[0])            # (num_tokens, num_labels)
        values_raw = [float(v) for v in values_all[:, label_idx]]
        base_values_all = np.atleast_1d(sv.base_values[0])
        base_value = float(base_values_all[label_idx]) if base_values_all.ndim else float(base_values_all)

        tokens = [
            {"token": tok, "shap_value": round(val, 5)}
            for tok, val in zip(tokens_raw, values_raw)
            if tok is not None and str(tok).strip()
        ]

        return {
            "text": text,
            "label": target_label,
            "primary": base["primary"],
            "base_value": round(base_value, 5),
            "tokens": tokens,
        }
