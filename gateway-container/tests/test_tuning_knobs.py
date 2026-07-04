"""튜닝 노브(POLICY_MIN_CONFIDENCE)와 RAG 선별(select_chunks) 동작 검증.

기본값이 현행 동작과 동일한지(끄면 아무것도 안 바뀜), 그리고 각 노브가
문서대로 동작하는지를 라우팅/선별 단위에서 고정한다.
※ 라벨 가산점(rerank) 노브는 2026-07 제거 — 실코퍼스(82청크) 전수 분석에서
  가산점이 참조하는 metadata.distortions 가 전부 비어 있어 죽은 코드로 확인.
  선별(중복 제거 + top_n)은 select_chunks 로 남아 아래에서 특성을 고정한다.
"""
import pytest

from app import settings
from app.respond import policy as context_policy
from app.respond.flow import select_chunks


def _cls(primary: str, score: float, selected: bool):
    return {"primary": primary,
            "labels": [{"label": primary, "score": score, "selected": selected},
                       {"label": "정상", "score": 0.01, "selected": False}]}


# --- ① RAG 선별 (select_chunks) ---

def test_select_chunks_orders_by_score_and_limits():
    cands = [{"id": f"d{i}", "content": "c", "score": s}
             for i, s in enumerate([0.2, 0.9, 0.5, 0.7, 0.1])]
    result = select_chunks(cands, top_n=3)
    assert [c["score"] for c in result] == [0.9, 0.7, 0.5]


def test_select_chunks_dedups_keeping_higher_score():
    cands = [{"id": "d1", "content": "old", "score": 0.4},
             {"id": "d1", "content": "new", "score": 0.8},
             {"id": "d2", "content": "c", "score": 0.6}]
    result = select_chunks(cands, top_n=4)
    d1 = next(c for c in result if c["id"] == "d1")
    assert d1["score"] == 0.8 and len(result) == 2


def test_select_chunks_empty_and_default_top_n(monkeypatch):
    assert select_chunks([]) == []
    monkeypatch.setattr(settings, "RAG_TOP_N", 2)
    cands = [{"id": f"d{i}", "content": "c", "score": i / 10} for i in range(5)]
    assert len(select_chunks(cands)) == 2  # top_n 생략 시 settings.RAG_TOP_N


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
