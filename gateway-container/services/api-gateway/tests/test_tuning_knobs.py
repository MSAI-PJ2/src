"""튜닝 노브(RERANK_BIAS_* / POLICY_MIN_CONFIDENCE) 동작 검증.

기본값이 현행 동작과 동일한지(끄면 아무것도 안 바뀜), 그리고 각 노브가
문서대로 동작하는지를 라우팅/재정렬 단위에서 고정한다.
"""
import pytest

from app import settings
from app.orchestrator import context_policy
from app.ranking import rerank

CANDS = [
    {"id": "d1", "content": "라벨 일치 문서", "score": 0.8, "metadata": {"distortions": ["흑백 사고"]}},
    {"id": "d2", "content": "무관 문서", "score": 0.9, "metadata": {}},
]


def _cls(primary: str, score: float, selected: bool):
    return {"primary": primary,
            "labels": [{"label": primary, "score": score, "selected": selected},
                       {"label": "정상", "score": 0.01, "selected": False}]}


def _bias_fired(result) -> bool:
    # d1 은 raw 점수가 최저(0.8 < 0.9)라 정규화 후 0.0 — 가산점이 붙었을 때만 0 보다 커진다
    return next(c for c in result if c["id"] == "d1")["score"] > 0.0


# --- ① rerank 가산점 노브 ---

@pytest.mark.parametrize("source,score,selected,expected", [
    ("score", 0.62, False, True),      # 현행: 확신 0.5 이상이면 발동
    ("score", 0.07, True, False),      # 현행: sigmoid 저점수는 selected 여도 미발동
    ("selected", 0.07, True, True),    # multi 권장: 서버 selected 판정으로 발동
    ("selected", 0.62, False, False),  # selected 소스에서는 점수만으로 발동 안 함
    ("either", 0.07, True, True),
    ("either", 0.62, False, True),
])
def test_rerank_bias_source(monkeypatch, source, score, selected, expected):
    monkeypatch.setattr(settings, "RERANK_BIAS_SOURCE", source)
    cls = _cls("흑백 사고", score, selected)
    result = rerank([dict(c) for c in CANDS], "흑백 사고", score, cls_labels=cls["labels"])
    assert _bias_fired(result) is expected


def test_rerank_bias_weight_knob(monkeypatch):
    monkeypatch.setattr(settings, "RERANK_BIAS_WEIGHT", 0.9)
    result = rerank([dict(c) for c in CANDS], "흑백 사고", 0.8,
                    cls_labels=_cls("흑백 사고", 0.8, True)["labels"])
    d1 = next(c for c in result if c["id"] == "d1")
    assert d1["score"] == pytest.approx(0.9)  # 정규화 0.0 + 가산점(WEIGHT) 0.9


def test_rerank_normal_label_never_biased(monkeypatch):
    monkeypatch.setattr(settings, "RERANK_BIAS_SOURCE", "either")
    result = rerank([dict(c) for c in CANDS], "정상", 0.99,
                    cls_labels=_cls("정상", 0.99, True)["labels"])
    assert not _bias_fired(result)


# --- ③ 저확신 강등 노브 ---

@pytest.mark.parametrize("floor,score,expected_policy", [
    (0.0, 0.07, "cbt_label_guided"),          # 기본값(꺼짐) = 현행 동작
    (0.3, 0.07, "low_confidence_clarify"),    # 하한 미만 → clarify 강등
    (0.3, 0.62, "cbt_label_guided"),          # 하한 이상 → 정상 라우팅
    (0.5, 0.436, "low_confidence_clarify"),   # 실측값(multi 낙인찍기 0.436) 기준
])
def test_policy_min_confidence(monkeypatch, floor, score, expected_policy):
    monkeypatch.setattr(settings, "POLICY_MIN_CONFIDENCE", floor)
    policy = context_policy.resolve({"safe": True}, _cls("낙인찍기", score, False))
    assert policy.name == expected_policy


def test_floor_does_not_touch_normal_and_insufficient(monkeypatch):
    """정상/불충분은 하한과 무관하게 자기 정책을 유지한다."""
    monkeypatch.setattr(settings, "POLICY_MIN_CONFIDENCE", 0.99)
    assert context_policy.resolve({"safe": True}, _cls("정상", 0.1, False)).name == "normal_supportive"
    assert context_policy.resolve({"safe": True}, _cls("불충분", 0.1, False)).name == "insufficient_clarify"


def test_crisis_overrides_floor(monkeypatch):
    """위기 판정은 어떤 노브 설정보다 우선한다."""
    monkeypatch.setattr(settings, "POLICY_MIN_CONFIDENCE", 0.99)
    policy = context_policy.resolve({"safe": False, "reason": "self_harm"}, _cls("낙인찍기", 0.01, False))
    assert policy.is_crisis
