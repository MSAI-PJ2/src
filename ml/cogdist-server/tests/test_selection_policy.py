import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.selection_policy import normalize_multilabel_selection


def test_exclusive_primary_selects_only_itself():
    labels = [
        {"label": "불충분", "score": 0.5244, "selected": False},
        {"label": "과잉 일반화", "score": 0.49, "selected": True},
        {"label": "정상", "score": 0.1, "selected": False},
    ]
    result = normalize_multilabel_selection(labels, "불충분", 0.55)
    assert [x for x in result if x["selected"]] == [{"label": "불충분", "score": 0.5244, "selected": True}]


def test_distortion_primary_excludes_routing_labels():
    labels = [
        {"label": "불충분", "score": 0.90, "selected": False},
        {"label": "과잉 일반화", "score": 0.40, "selected": False},
        {"label": "정상", "score": 0.80, "selected": False},
        {"label": "흑백 사고", "score": 0.70, "selected": False},
    ]
    result = normalize_multilabel_selection(labels, "과잉 일반화", 0.55)
    selected = {x["label"] for x in result if x["selected"]}
    assert "과잉 일반화" in selected
    assert "흑백 사고" in selected
    assert "불충분" not in selected
    assert "정상" not in selected

