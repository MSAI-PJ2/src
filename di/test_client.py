"""
카카오톡 이미지 OCR 테스트 클라이언트

사용법:
  # 카톡 이미지 분석
  python test_client.py --doc 카톡캡쳐.jpg

  # 텍스트로 직접 입력
  python test_client.py "요즘 뭘 해도 안 될 것 같아"

  # 서버 주소가 다를 경우
  python test_client.py --doc 카톡캡쳐.jpg --url http://서버주소:8000/v1/respond

  # API 키가 설정된 서버일 경우
  python test_client.py --doc 카톡캡쳐.jpg --api-key 여기에키입력

  # 이전 대화 이어가기 (멀티턴)
  python test_client.py "추가 질문" --session 세션ID
"""
from __future__ import annotations

import argparse
import base64
import json

import httpx


# ── 인지왜곡 라벨 한국어 설명 ────────────────────────────────
LABEL_DESC = {
    "불충분":           "문맥이 짧아 판단하기 어려운 발화",
    "정상":             "인지왜곡 없는 건강한 생각",
    "'해야 한다' 진술": "지나치게 엄격한 기준을 자신에게 부과",
    "감정적 추론":       "감정을 사실처럼 받아들임",
    "개인화":           "모든 일의 원인을 자신에게 돌림",
    "과잉 일반화":       "한 번의 일로 항상/절대 등 일반화",
    "긍정 축소화":       "좋은 일을 작게 보거나 무시",
    "낙인찍기":          "자신/타인에게 부정적 꼬리표를 붙임",
    "부정적 편향":       "부정적인 면만 보는 경향",
    "성급한 판단":       "근거 없이 결론을 내림",
    "확대와 축소":       "나쁜 점은 크게, 좋은 점은 작게 봄",
    "흑백 사고":         "극단적으로 흑백만 보는 사고방식",
}


def label_display(primary: str) -> str:
    """라벨명 + 설명을 사용자 친화적으로 반환."""
    desc = LABEL_DESC.get(primary, "")
    return f"{primary} — {desc}" if desc else primary


def score_bar(score: float, width: int = 15) -> str:
    """확률을 시각적 막대로 표현."""
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


def main() -> None:
    ap = argparse.ArgumentParser(description="카카오톡 OCR + 인지왜곡 분석 테스트")
    ap.add_argument("text", nargs="?", default=None, help="직접 입력할 텍스트")
    ap.add_argument("--doc",  help="카톡 캡쳐 이미지 파일 경로 (.jpg / .png)")
    ap.add_argument("--audio", help="음성 파일 경로 (.wav)")
    ap.add_argument("--url",  default="http://localhost:8000/v1/respond", help="서버 주소")
    ap.add_argument("--session", default=None, help="이전 대화 이어가기용 세션 ID")
    ap.add_argument("--tts",  action="store_true", help="음성 응답 켜기")
    ap.add_argument("--api-key", default=None, help="서버 API 키 (필요한 경우)")
    args = ap.parse_args()

    # ── 요청 본문 구성 ──────────────────────────────────────
    body: dict = {}
    if args.session:
        body["session_id"] = args.session
    if args.tts:
        body["tts"] = {"enabled": True}

    if args.audio:
        with open(args.audio, "rb") as f:
            body["audio"] = {
                "kind": "base64",
                "data": base64.b64encode(f.read()).decode(),
                "mime_type": "audio/wav",
            }
    elif args.doc:
        with open(args.doc, "rb") as f:
            body["document"] = {
                "kind": "base64",
                "data": base64.b64encode(f.read()).decode(),
            }
    else:
        body["text"] = args.text or "요즘 뭘 해도 안 될 것 같고 다 내 잘못인 것 같아"

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["x-api-key"] = args.api_key

    # ── 요청 시작 안내 ──────────────────────────────────────
    if args.doc:
        print(f"\n📸  이미지 파일: {args.doc}")
    elif args.audio:
        print(f"\n🎤  음성 파일: {args.audio}")
    else:
        print(f"\n💬  입력 텍스트: {body.get('text')}")
    print(f"🌐  서버: {args.url}")
    print("=" * 55)

    # ── SSE 스트리밍 수신 ───────────────────────────────────
    session_id = None
    with httpx.stream("POST", args.url, json=body, headers=headers, timeout=180) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            t = ev.get("type")

            # ── 이미지 OCR 처리 ──
            if t == "doc":
                if ev.get("status") == "processing":
                    print("\n⏳  카톡 대화 인식 중...", flush=True)
                elif ev.get("status") == "completed":
                    conversation = ev.get("conversation", [])
                    turns = len(conversation)
                    print(f"✅  인식 완료 — 총 {turns}개 메시지\n")

                    # 인식된 대화 말풍선 형태로 출력
                    print("┌─ 인식된 카톡 대화 " + "─" * 35)
                    for turn in conversation:
                        speaker = turn.get("speaker", "")
                        content = turn.get("content", "")
                        time    = turn.get("time", "")
                        if speaker == "나":
                            # 오른쪽 정렬 (내 메시지)
                            line_str = f"  {content}  [{time}]"
                            print(f"│{line_str:>52}")
                        else:
                            # 왼쪽 정렬 (상대방 메시지)
                            print(f"│  [{time}]  {speaker}: {content}")
                    print("└" + "─" * 53 + "\n")

            # ── 음성 → 텍스트 ──
            elif t == "stt":
                if ev.get("status") == "processing":
                    print("\n⏳  음성 인식 중...", flush=True)
                elif ev.get("status") == "completed":
                    print(f"✅  음성 인식 완료\n💬  \"{ev.get('transcript')}\"\n")

            # ── 인지왜곡 분류 결과 + AI 응답 ──
            elif t == "meta":
                primary = ev.get("primary", "")
                labels  = ev.get("labels", [])

                print("┌─ 인지왜곡 분석 결과 " + "─" * 33)
                print(f"│  주요 판정: {label_display(primary)}")
                print("│")
                print("│  전체 라벨 확률:")
                # 확률 높은 순으로 상위 5개만 표시
                top5 = sorted(labels, key=lambda x: x.get("score", 0), reverse=True)[:5]
                for item in top5:
                    lbl   = item.get("label", "")
                    score = item.get("score", 0)
                    bar   = score_bar(score)
                    pct   = f"{score * 100:.1f}%"
                    print(f"│    {lbl:<14} {bar}  {pct}")
                print("└" + "─" * 53 + "\n")
                print("🤖  AI 응답:\n")

            # ── AI 응답 토큰 스트리밍 ──
            elif t == "token":
                print(ev["text"], end="", flush=True)

            # ── 위기 감지 ──
            elif t == "crisis":
                print("\n⚠️   위기 신호가 감지되었습니다.")
                print(f"\n{ev.get('message', '')}\n")
                resources = ev.get("resources", [])
                if resources:
                    print("📞  도움받을 수 있는 곳:")
                    for res in resources:
                        print(f"    · {res.get('name')}  {res.get('phone')}  ({res.get('hours')})")

            # ── TTS ──
            elif t == "tts":
                if ev.get("status") == "completed":
                    print("\n\n🔊  음성 응답이 생성되었습니다.")

            # ── 에러 ──
            elif t == "error":
                stage  = ev.get("stage", "")
                detail = ev.get("detail", "")
                error  = ev.get("error", "")
                stage_kor = {
                    "stt":      "음성 인식",
                    "document": "이미지 OCR",
                    "input":    "입력",
                    "safety":   "안전 검사",
                    "classify": "인지왜곡 분류",
                    "retrieve": "정보 검색",
                    "llm":      "AI 응답 생성",
                }.get(stage, stage)
                print(f"\n❌  오류 발생 [{stage_kor}]: {detail}")
                if error:
                    print(f"    상세: {error}")

            # ── 완료 ──
            elif t == "done":
                session_id = ev.get("session_id")
                print(f"\n\n{'=' * 55}")
                print(f"✔   완료  |  세션 ID: {session_id}")
                print(f"    다음 대화를 이어가려면:")
                print(f"    python test_client.py \"추가 질문\" --session {session_id}")
                print(f"{'=' * 55}\n")


if __name__ == "__main__":
    main()