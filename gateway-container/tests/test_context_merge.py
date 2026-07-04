# -*- coding: utf-8 -*-
"""컨텍스트 병합 재분류(twopass) · novelty 게이트 · 완화 사다리 계약 테스트.

검증 대상 (설계 근거: B:\\teamprojectshare\\gateway_policy_lab_2026-07-04 실측):
  1. 순수 함수 단위: novelty 판별 / 병합문 길이 규칙 / 연속 불충분 카운트
  2. e2e 계약: '불충분' → 병합 재분류로 라벨 회복 (meta.analysis 관측 필드 포함)
  3. e2e 계약: 화제 전환은 novelty 게이트가 병합을 기각 (오염 방지)
  4. e2e 계약: 연속 불충분 사다리 1→4차 (4차 = 수용·동행 모드 전환)
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.respond import context_merge

# ---------------------------------------------------------------------------
# 1) 순수 함수 단위 테스트 — 서버 없이 규칙만 검증
# ---------------------------------------------------------------------------

ANCHOR = "이번 발표 하나 망쳤으니 저는 회사에서 완전히 실패자예요."


def test_novelty_topic_shift_detected():
    """화제 전환(새 내용어 도입)은 반드시 잡아야 한다 — 병합 오염 방지의 핵심."""
    assert context_merge.novelty("아 점심에 김치찌개 먹었어요.", [ANCHOR])
    assert context_merge.novelty("주말에 본가 다녀왔어요.", [ANCHOR])


def test_novelty_continuation_passes():
    """조응·기능어뿐인 이어말하기는 새 내용어 0개여야 한다 — 병합 회복의 전제."""
    for frag in ("네 그냥 그래요.", "어제도 그랬어요.", "비슷한 일이 또 있었어요.",
                 "딱히 이유는 모르겠어요.", "이번에도요."):
        assert context_merge.novelty(frag, [ANCHOR]) == [], frag


def test_novelty_same_topic_word_passes():
    """맥락에 이미 있는 내용어(발표)는 조사가 바뀌어도 '새' 내용어가 아니다."""
    assert context_merge.novelty("발표만 생각하면 그래요.", [ANCHOR]) == []


def test_novelty_two_char_nouns_not_destroyed():
    """리뷰 지적: 2글자 명사가 한 글자 꼬리('다·가·과')에 잘려 소멸하면 전환을 놓친다."""
    for shift in ("어제 바다 갔어요.", "포도 먹었어요.", "요가했어요.", "사과했어요."):
        assert context_merge.novelty(shift, [ANCHOR]), shift


def test_novelty_ack_and_jamo_are_not_content():
    """리뷰 지적: 'ㅇㅇ·ㅠㅠ·네네' 같은 수긍·이모티콘은 새 내용어가 아니다 (사다리 오탈출 방지)."""
    for ack in ("ㅇㅇ", "ㅠㅠ 그냥요", "네네.", "네에 맞아요."):
        assert context_merge.novelty(ack, [ANCHOR]) == [], ack


def test_novelty_state_reversal_rejected():
    """리뷰 지적: '이제 괜찮아요'(상태 반전)는 이어말하기가 아니다 — 병합하면 앵커 왜곡에 오염."""
    assert context_merge.novelty("오늘 기분 진짜 괜찮아요.", [ANCHOR])


def test_merge_text_keeps_current_and_trims_oldest():
    """길이 상한 초과 시 오래된 맥락부터 버리고, 현재 발화는 절대 자르지 않는다."""
    old, recent, current = "가" * 100, "나" * 100, "다" * 50
    merged = context_merge.merge_text([old, recent], current, max_turns=3, max_chars=180)
    assert merged.endswith(current)          # 현재 발화 온전
    assert old not in merged                 # 가장 오래된 맥락 탈락
    assert recent in merged and len(merged) <= 180


def test_merge_text_single_context_head_cut():
    """맥락이 1개뿐인데도 넘치면 맥락의 머리를 잘라 꼬리(현재와 가까운 쪽)만 남긴다."""
    ctx = "가" * 300
    merged = context_merge.merge_text([ctx], "다" * 50, max_turns=3, max_chars=180)
    assert merged.endswith("다" * 50) and len(merged) <= 180


def test_merge_candidate_zero_turns_disables():
    """리뷰 지적: list[-0:] 함정 — max_turns=0 은 '전체 병합'이 아니라 '병합 없음'이어야 한다."""
    merged, included = context_merge.merge_candidate(["가가 하나", "나나 둘"], "다다", 0, 180)
    assert merged == "다다" and included == []


def test_merge_candidate_gate_window_matches_trim():
    """리뷰 지적: 길이 초과로 앵커가 잘리면 novelty 게이트 기준(included)도 같이 줄어야 한다."""
    anchor = "발" * 170
    merged, included = context_merge.merge_candidate([anchor, "그냥요.", "몰라요."], "네 그래요.", 3, 180)
    assert anchor not in included and merged.endswith("네 그래요.")


def test_trailing_insufficient_event_turn_breaks_chain():
    """리뷰 지적: STT/OCR 실패(빈 텍스트 이벤트 턴)는 발화 '시도'다 — 불충분 연쇄를 끊어야 한다."""
    turns = [{"role": "user", "text": "a", "primary": "불충분", "safety": "safe"},
             {"role": "user", "text": "b", "primary": "불충분", "safety": "safe"},
             {"role": "user", "text": "", "event": "stt_failed"},
             ]
    assert context_merge.trailing_insufficient(turns) == 0
    assert context_merge.trailing_insufficient(turns[:2]) == 2


def test_trailing_insufficient_counts_and_breaks():
    turns = [
        {"role": "user", "text": "a", "primary": "흑백 사고", "safety": "safe"},
        {"role": "assistant", "text": "r1"},
        {"role": "user", "text": "b", "primary": "불충분", "safety": "safe"},
        {"role": "assistant", "text": "r2"},
        {"role": "user", "text": "c", "primary": "불충분", "safety": "safe"},
    ]
    assert context_merge.trailing_insufficient(turns) == 2
    # 왜곡 턴이 맨 뒤면 연쇄 0, 차단 턴은 연쇄를 끊는다
    assert context_merge.trailing_insufficient(turns[:1]) == 0
    blocked = turns + [{"role": "user", "text": "d", "primary": "불충분", "safety": "blocked"},
                       {"role": "user", "text": "e", "primary": "불충분", "safety": "safe"}]
    assert context_merge.trailing_insufficient(blocked) == 1


# ---------------------------------------------------------------------------
# 2) e2e 계약 — 순서 지정 가짜 분류기로 twopass/게이트/사다리 흐름 검증
# ---------------------------------------------------------------------------

def _cls(primary, score=0.9):
    return {"text": "", "mode": "single_label", "model": "fake", "model_version": "t",
            "threshold": 0.5, "primary": primary,
            "labels": [{"label": primary, "score": score, "selected": True}]}


class SeqClassifier:
    """호출 순서대로 미리 정한 라벨을 돌려주는 가짜 분류기. calls 로 입력문도 검증한다."""

    def __init__(self, primaries):
        self.queue = list(primaries)
        self.calls: list[str] = []

    async def classify_one(self, text, threshold=None):
        self.calls.append(text)
        primary = self.queue.pop(0) if self.queue else "불충분"
        return {**_cls(primary), "text": text}


class FakeSafety:
    async def check(self, text):
        return {"safe": True, "reason": None, "source": "fake"}


class FakeRetriever:
    async def retrieve(self, text):
        return []


class FakeLlm:
    async def chat_stream_async(self, messages, options=None):
        yield "네, "
        yield "함께 볼게요."


@pytest.fixture()
def harness(monkeypatch):
    """SeqClassifier 를 주입할 수 있는 TestClient 팩토리."""
    from app.services import services
    monkeypatch.setattr(services, "safety", FakeSafety())
    monkeypatch.setattr(services, "retriever", FakeRetriever())
    monkeypatch.setattr(services, "llm", FakeLlm())

    from app.main import app

    def make(primaries):
        clf = SeqClassifier(primaries)
        monkeypatch.setattr(services, "classifier", clf)
        return TestClient(app), clf

    return make


def sse_events(response):
    return [json.loads(f[len("data: "):]) for f in response.text.split("\n\n")
            if f.strip().startswith("data: ")]


def _meta(events):
    return next(e for e in events if e["type"] == "meta")


def _respond(client, sid, text):
    r = client.post("/v1/respond", json={"text": text, "session_id": sid})
    assert r.status_code == 200
    return sse_events(r)


def test_twopass_recovers_label_with_merge(harness):
    """1턴 왜곡 → 2턴 이어말하기 파편: 단독 '불충분'이 병합 재분류로 왜곡을 되찾는다."""
    client, clf = harness(["흑백 사고", "불충분", "흑백 사고"])
    sid = f"merge-{uuid.uuid4()}"

    ev1 = _respond(client, sid, ANCHOR)
    assert _meta(ev1)["primary"] == "흑백 사고"
    assert _meta(ev1)["analysis"] == {"context_merged": False, "merge_rejected_by": None,
                                      "ladder_step": 0}

    ev2 = _respond(client, sid, "네 그냥 그래요.")
    meta = _meta(ev2)
    assert meta["primary"] == "흑백 사고"            # 병합 재분류 결과가 최종
    assert meta["analysis"]["context_merged"] is True
    assert meta["analysis"]["ladder_step"] == 0      # 최종 라벨이 불충분이 아니므로 0
    # 3번째 분류기 호출 입력 = "직전 발화 + 현재 발화" 병합문
    assert clf.calls[-1] == f"{ANCHOR} 네 그냥 그래요."

    # 세션 기록: 사용자 턴 primary 도 최종(회복된) 라벨로 저장돼야 다음 턴 사다리가 맞다
    snap = client.get(f"/v1/sessions/{sid}").json()
    user_turns = [t for t in snap["turns"] if t["role"] == "user"]
    assert user_turns[-1]["primary"] == "흑백 사고"


def test_novelty_gate_rejects_topic_shift(harness):
    """화제 전환 발화는 병합을 기각하고 '불충분'(명확화)을 유지한다 — 오염 방지."""
    client, clf = harness(["흑백 사고", "불충분"])
    sid = f"shift-{uuid.uuid4()}"

    _respond(client, sid, ANCHOR)
    ev2 = _respond(client, sid, "아 점심에 김치찌개 먹었어요.")
    meta = _meta(ev2)
    assert meta["primary"] == "불충분"               # 직전 왜곡으로 끌려가지 않음
    assert meta["analysis"]["merge_rejected_by"] == "novelty"
    assert meta["analysis"]["context_merged"] is False
    assert len(clf.calls) == 2                       # 재분류 호출 자체가 없었다 (비용 절감)


def test_insufficient_ladder_to_accompany(harness):
    """연속 '불충분' 4턴: 정책이 clarify → alt → light → 수용·동행으로 계단을 오른다."""
    client, _ = harness(["불충분"])                   # 큐 소진 후엔 계속 '불충분'
    sid = f"ladder-{uuid.uuid4()}"

    steps = []
    for text in ("몰라요.", "그냥요.", "글쎄요.", "됐어요."):
        meta = _meta(_respond(client, sid, text))
        steps.append(meta["analysis"]["ladder_step"])
    assert steps == [1, 2, 3, 4]

    snap = client.get(f"/v1/sessions/{sid}").json()
    names = [t["policy"]["name"] for t in snap["turns"]
             if t["role"] == "assistant" and t.get("policy")]
    assert names == ["insufficient_clarify", "insufficient_clarify_alt",
                     "insufficient_clarify_light", "insufficient_accompany"]
    # 저장된 policy 메타에 관측 필드가 함께 남는다 (운영 집계용)
    last = [t for t in snap["turns"] if t["role"] == "assistant"][-1]
    assert last["policy"]["ladder_step"] == 4


def test_normal_turn_resets_ladder(harness):
    """중간에 정상/왜곡 턴이 나오면 연쇄가 끊기고 사다리가 1부터 다시 시작한다."""
    # 큐 순서 주의: 2턴은 단독+병합 재분류로 분류기를 2번 부른다 (불충분 2개 소비)
    client, _ = harness(["불충분", "불충분", "불충분", "정상"])
    sid = f"reset-{uuid.uuid4()}"

    _respond(client, sid, "몰라요.")                  # step 1 (호출 1회)
    _respond(client, sid, "그냥요.")                  # step 2 (단독 불충분 + 병합도 불충분)
    meta3 = _meta(_respond(client, sid, "오늘은 산책을 다녀와서 기분이 좀 나아요."))
    assert meta3["analysis"]["ladder_step"] == 0      # 정상 턴 — 사다리 밖
    meta4 = _meta(_respond(client, sid, "다시 모르겠어요."))
    assert meta4["analysis"]["ladder_step"] == 1      # 연쇄 리셋 후 1부터


# ---------------------------------------------------------------------------
# 3) 노브 비기본값 계약 — 스위치가 문서대로 동작하는지 고정 (리뷰 지적: 기본값만
#    테스트하면 "끄는" 경로가 무방비다. 두 기능의 스위치는 분리돼 있다.)
# ---------------------------------------------------------------------------

def test_knob_retry_off_disables_merge_only(harness, monkeypatch):
    """CLASSIFY_RETRY_ON_INSUFFICIENT=false: 병합 재분류만 꺼진다 — 사다리는 계속 동작."""
    from app import settings as app_settings
    monkeypatch.setattr(app_settings, "CLASSIFY_RETRY_ON_INSUFFICIENT", False)
    client, clf = harness(["불충분"])
    sid = f"knob-retry-{uuid.uuid4()}"

    m1 = _meta(_respond(client, sid, "몰라요."))
    m2 = _meta(_respond(client, sid, "그냥요."))
    assert len(clf.calls) == 2                        # 턴당 1회 — 재분류 호출 없음
    assert m2["analysis"]["context_merged"] is False
    assert m2["analysis"]["ladder_step"] == 2         # 사다리는 별도 스위치라 계속 오른다
    assert m1["analysis"]["ladder_step"] == 1


def test_knob_ladder_off(harness, monkeypatch):
    """INSUFFICIENT_ESCAPE_AFTER=0: 사다리 전체 끔 — 항상 1차 clarify, step 은 0 유지."""
    from app import settings as app_settings
    monkeypatch.setattr(app_settings, "INSUFFICIENT_ESCAPE_AFTER", 0)
    client, _ = harness(["불충분"])
    sid = f"knob-ladder-{uuid.uuid4()}"

    for text in ("몰라요.", "그냥요.", "글쎄요.", "됐어요."):
        meta = _meta(_respond(client, sid, text))
        assert meta["analysis"]["ladder_step"] == 0
    snap = client.get(f"/v1/sessions/{sid}").json()
    names = {t["policy"]["name"] for t in snap["turns"]
             if t["role"] == "assistant" and t.get("policy")}
    assert names == {"insufficient_clarify"}          # alt/light/accompany 미발동


def test_knob_max_turns_zero_disables_merge(harness, monkeypatch):
    """CLASSIFY_CONTEXT_MAX_TURNS=0: list[-0:] 함정 없이 '병합 끔'으로 동작해야 한다."""
    from app import settings as app_settings
    monkeypatch.setattr(app_settings, "CLASSIFY_CONTEXT_MAX_TURNS", 0)
    client, clf = harness(["흑백 사고", "불충분"])
    sid = f"knob-turns-{uuid.uuid4()}"

    _respond(client, sid, ANCHOR)
    m2 = _meta(_respond(client, sid, "네 그냥 그래요."))
    assert len(clf.calls) == 2                        # 병합 재분류 시도 없음
    assert m2["primary"] == "불충분"
    assert m2["analysis"]["context_merged"] is False and m2["analysis"]["merge_rejected_by"] is None
