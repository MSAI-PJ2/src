from __future__ import annotations

from typing import Any

EXCLUSIVE_LABELS = {"정상", "불충분"}


def normalize_multilabel_selection(
    labels: list[dict[str, Any]],
    primary: str,
    threshold: float,
) -> list[dict[str, Any]]:
    """multi-label 결과의 selected 값을 API 계약에 맞게 정리한다.

    정상/불충분은 라우팅 라벨이므로 인지왜곡 다중 선택과 배타적으로 취급한다.
    이 함수는 게이트웨이 방어코드 없이도 `primary`와 `selected`가 어긋나지 않게 한다.
    """
    if primary in EXCLUSIVE_LABELS:
        for item in labels:
            item["selected"] = item["label"] == primary
        return labels

    any_distortion_selected = False
    for item in labels:
        label = item["label"]
        if label in EXCLUSIVE_LABELS:
            item["selected"] = False
        elif label == primary:
            item["selected"] = True
            any_distortion_selected = True
        else:
            item["selected"] = float(item["score"]) >= threshold
            any_distortion_selected = any_distortion_selected or item["selected"]

    if not any_distortion_selected:
        for item in labels:
            item["selected"] = item["label"] == primary
    return labels
