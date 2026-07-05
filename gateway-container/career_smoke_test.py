# -*- coding: utf-8 -*-
"""[원샷 테스트] career 기능 6개를 순서대로 전부 테스트하고 결과를 파일로 저장한다.

사용법 (서버가 떠 있는 상태에서):
    로컬:  python career_smoke_test.py
    Azure: python career_smoke_test.py --base https://<컨테이너앱주소> --api-key <키>

하는 일:
    1. /healthz            서버 생존 확인
    2. POST profile        샘플 프로필 저장
    3. POST analyze        샘플 공고 → 공고분석+키워드+적합도  → test_results/analyze.json
    4. POST resume         공고 맞춤 이력서 초안               → test_results/resume.json
    5. POST cover-letter   자소서 초안 (SSE 스트리밍)          → test_results/cover_letter.txt
    6. POST review         자소서 첨삭 (SSE 스트리밍)          → test_results/review.txt

샘플 데이터는 이 파일 안에 전부 내장돼 있다 (아래 [샘플 데이터] 구획).
Swagger(/docs)에서 손으로 테스트하고 싶으면 그 구획의 내용을 복사해 쓰면 된다.
※ 공고·회사는 가상의 예시다. 실제 테스트 후에는 사람인/원티드의 진짜 공고로 바꿔볼 것.
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

# ══════════════════════════════════════════════════════════════════════════
# [샘플 데이터] — Swagger 수동 테스트 시 여기서 복사
# ══════════════════════════════════════════════════════════════════════════

# 1) 프로필: 팀 상황(MS AI School 수강생)에 맞춘 예시. 본인 것으로 바꿔서 써도 됨.
SAMPLE_PROFILE = {
    "education": "가온누리대학교(가상) 통계학과 졸업, Microsoft AI School 10기 수료",
    "target_role": "데이터 분석가",
    "skills": ["SQL", "Python(pandas)", "Power BI", "Excel", "통계 분석(가설검정, 회귀)"],
    "experiences": [
        {"title": "공공데이터 분석 공모전 장려상",
         "detail": "가상의 지자체 대중교통 이용 데이터를 분석해 노선 개편안 제안. "
                   "pandas 로 200만 행 전처리, 시간대별 수요 예측에 회귀 모델 적용."},
        {"title": "MS AI School 10기 팀 프로젝트 — 소상공인 매출 대시보드",
         "detail": "카드 매출 공공데이터를 Azure SQL 에 적재하고 Power BI 대시보드 제작. "
                   "업종·상권별 매출 추이 시각화와 이상치 탐지 담당."},
        {"title": "대학 축제 운영 데이터 관리",
         "detail": "부스별 매출·방문 데이터를 Excel/구글시트로 집계하고 "
                   "운영진 의사결정용 일일 리포트 작성."},
    ],
}

# 2) 채용공고: 가상 회사의 예시 공고 (실전 테스트 땐 진짜 공고로 교체)
SAMPLE_POSTING = """(주)장터인사이트(가상 회사) — 데이터 분석가 (신입/주니어) 채용

[주요 업무]
- 커머스 판매·고객 데이터 분석 및 인사이트 도출
- SQL 기반 데이터 추출·정제 및 분석용 데이터마트 관리
- Power BI 등 BI 도구를 활용한 대시보드 구축·운영
- A/B 테스트 설계 및 성과 분석 리포트 작성

[자격 요건]
- SQL 을 활용한 데이터 추출·가공 능력
- Python 또는 R 을 활용한 데이터 분석 경험
- 통계적 가설검정에 대한 이해
- 분석 결과를 비개발 직군에게 전달하는 커뮤니케이션 능력

[우대 사항]
- BI 도구(Power BI, Tableau 등) 대시보드 구축 경험
- 클라우드(Azure, AWS 등) 데이터 서비스 사용 경험
- 대용량 데이터 전처리 경험
- 공모전·프로젝트 등 실데이터 분석 경험

[근무 조건]
- 근무지: 성남 판교 / 채용 형태: 정규직 (수습 3개월)
"""

# 3) 자소서 문항 + 초안: 첨삭(review) 테스트용. 일부러 흔한 약점을 넣어둠
#    (추상적 표현, 성과 없는 나열, 그리고 프로필에 없는 주장 1개 → 사실검증이 잡아내는지 확인)
SAMPLE_QUESTION = "지원 동기와 입사 후 포부를 기술하시오. (공백 포함 1,000자 이내)"
SAMPLE_DRAFT = """저는 평소 숫자를 다루는 일에 흥미가 많았고, 꼼꼼한 성격으로 어떤 일이든 \
차분하게 처리하는 편입니다. 대학에서 통계학을 전공하며 데이터의 중요성을 배웠고, 여러 활동을 \
통해 분석 역량을 키웠습니다.

특히 공공데이터 공모전에 참가해 대중교통 데이터를 분석하며 데이터가 실제 의사결정에 쓰이는 \
과정을 경험했습니다. 또한 Microsoft AI School 에서 팀 프로젝트로 대시보드를 만들며 협업의 \
중요성을 깨달았습니다. 글로벌 컨설팅사에서 데이터 분석 인턴으로 근무하며 실무 감각도 익혔습니다.

귀사는 데이터 기반 의사결정을 선도하는 기업으로서 저의 열정을 펼치기에 최적의 환경이라고 \
생각합니다. 입사 후에는 맡은 바 업무에 최선을 다하고, 끊임없이 배우는 자세로 회사의 발전에 \
기여하는 인재가 되겠습니다."""


# ══════════════════════════════════════════════════════════════════════════
# 테스트 실행부 — 여기부터는 손댈 필요 없음
# ══════════════════════════════════════════════════════════════════════════

OUT = Path("test_results")
GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}[성공]{RESET} {msg}")


def fail(msg: str, hint: str = "") -> None:
    print(f"{RED}[실패]{RESET} {msg}")
    if hint:
        print(f"       힌트: {hint}")
    sys.exit(1)


def consume_sse(client: httpx.Client, url: str, body: dict, headers: dict, save_to: Path) -> str:
    """SSE 응답을 받아 token 조각들을 이어 붙이고, 전체 텍스트를 파일로 저장한다."""
    tokens: list[str] = []
    events = 0
    with client.stream("POST", url, json=body, headers=headers, timeout=300) as r:
        if r.status_code != 200:
            r.read()
            fail(f"{url} → HTTP {r.status_code}: {r.text[:300]}")
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            events += 1
            evt = json.loads(line[len("data: "):])
            if evt.get("type") == "token":
                tokens.append(evt.get("text", ""))
                print(".", end="", flush=True)   # 진행 표시
            elif evt.get("type") == "error":
                print()
                fail(f"서버가 error 이벤트를 보냄: {evt.get('message')}",
                     "Azure OpenAI 키/배포이름(.env)을 확인하세요.")
    print()
    text = "".join(tokens)
    if not text.strip():
        fail(f"{url} 에서 token 이벤트가 비어 있음 (이벤트 {events}개)",
             "LLM 설정(.env 의 AZURE_OPENAI_*)을 확인하세요.")
    save_to.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description="career 기능 원샷 테스트")
    ap.add_argument("--base", default="http://127.0.0.1:8080",
                    help="서버 주소 (기본: 로컬). Azure 는 https://<컨테이너앱주소>")
    ap.add_argument("--api-key", default="", help="배포 환경의 x-api-key (로컬은 보통 불필요)")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    headers = {"x-api-key": args.api_key} if args.api_key else {}
    OUT.mkdir(exist_ok=True)
    # trust_env=False: PC 에 프록시 환경변수가 있어도 무시하고 직접 연결
    # (회사/학교 프록시 때문에 localhost 연결이 막히는 흔한 문제를 예방)
    client = httpx.Client(timeout=120, trust_env=False)

    print(f"\n대상 서버: {base}\n" + "=" * 60)

    # ── 1. 서버 생존 확인 ──────────────────────────────────────────────
    try:
        r = client.get(f"{base}/healthz")
    except httpx.ConnectError:
        fail(f"{base} 에 연결할 수 없음",
             "서버가 떠 있나요? 로컬이면: uvicorn app.main:app --reload --port 8080")
    if r.status_code != 200:
        fail(f"/healthz → HTTP {r.status_code}")
    ok("1/6 서버 살아있음 (/healthz)")

    # ── 2. 프로필 저장 ────────────────────────────────────────────────
    r = client.post(f"{base}/v1/career/profile", json=SAMPLE_PROFILE, headers=headers)
    if r.status_code == 401:
        fail("인증 실패(401)", "--api-key <키> 를 붙여서 다시 실행하세요.")
    if r.status_code != 200:
        fail(f"profile → HTTP {r.status_code}: {r.text[:300]}")
    pid = r.json()["profile_id"]
    ok(f"2/6 프로필 저장 (profile_id={pid}, 경험 {r.json()['experiences']}개)")

    # ── 2.5. 설정 진단 — LLM 을 부르기 전에 환경변수부터 확인 ──────────
    r = client.get(f"{base}/v1/career/diag", headers=headers)
    if r.status_code == 200:
        d = r.json()
        missing = [k for k, v in d.items() if k.startswith("AZURE_") and not v]
        if missing:
            fail(f"서버에 Azure OpenAI 설정이 없음: {', '.join(missing)}",
                 "서버 창을 끄고(Ctrl+C) 이렇게 다시 켜세요: "
                 "uvicorn app.main:app --reload --port 8080 --env-file .env "
                 "(.env 파일이 gateway-container 폴더에 있어야 함)")
        ver = d.get("career_version")
        if ver != "5":
            fail(f"서버가 옛 career.py 를 쓰고 있음 (버전: {ver}, 필요: 5)",
                 "새 career.py 를 app 폴더에 덮어썼는지, 저장 후 서버 로그에 "
                 "'Reloading...' 이 떴는지 확인. 안 떴으면 서버를 껐다 켜세요.")
        ok(f"2.5/6 서버 설정 확인 (AZURE_OPENAI_* 3개 존재, career v{ver})")

    # ── 3. 공고 분석 + 키워드 + 적합도 ─────────────────────────────────
    print("     ... LLM 호출 중 (수십 초 걸릴 수 있음)")
    r = client.post(f"{base}/v1/career/analyze",
                    json={"posting": SAMPLE_POSTING, "profile_id": pid}, headers=headers)
    if r.status_code != 200:
        fail(f"analyze → HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    (OUT / "analyze.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if "parse_error" in data:
        raw_preview = (data.get("raw") or "")[:300].replace("\n", " ")
        print(f"     [원문 미리보기] {raw_preview}")
        fail("analyze 응답에서 JSON 을 찾지 못함 (전체는 test_results/analyze.json)",
             "위 미리보기 화면을 캡처해서 공유해 주세요.")
    if "_repaired" in data:
        print("     [알림] 응답이 잘려서 자동 복구됨 — .env 에 "
              "AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT=12000 이 있는지 확인하면 좋음")
    ok(f"3/6 공고 분석 — 적합도 {data.get('fit_score')}점, "
       f"키워드 {len(data.get('keywords', []))}개, 추천: {data.get('recommendation')}")
    print(f"     근거 문장(reframe_evidence): {data.get('reframe_evidence')}")

    # ── 4. 공고 맞춤 이력서 ────────────────────────────────────────────
    print("     ... LLM 호출 중")
    r = client.post(f"{base}/v1/career/resume",
                    json={"posting": SAMPLE_POSTING, "profile_id": pid}, headers=headers)
    if r.status_code != 200:
        fail(f"resume → HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    (OUT / "resume.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ok(f"4/6 이력서 초안 — headline: {data.get('headline')}")
    print(f"     채워야 할 정보(missing_info): {data.get('missing_info')}")

    # ── 5. 자소서 초안 (SSE) ───────────────────────────────────────────
    print("     ... 자소서 초안 스트리밍 수신 중 ", end="")
    text = consume_sse(client, f"{base}/v1/career/cover-letter",
                       {"posting": SAMPLE_POSTING, "question": SAMPLE_QUESTION,
                        "max_chars": 1000, "profile_id": pid},
                       headers, OUT / "cover_letter.txt")
    ok(f"5/6 자소서 초안 — {len(text)}자 수신 → test_results/cover_letter.txt")

    # ── 6. 자소서 첨삭 (SSE) ───────────────────────────────────────────
    print("     ... 첨삭 스트리밍 수신 중 ", end="")
    text = consume_sse(client, f"{base}/v1/career/review",
                       {"draft": SAMPLE_DRAFT, "posting": SAMPLE_POSTING,
                        "question": SAMPLE_QUESTION, "max_chars": 1000, "profile_id": pid},
                       headers, OUT / "review.txt")
    ok(f"6/6 자소서 첨삭 — {len(text)}자 수신 → test_results/review.txt")

    print("=" * 60)
    print(f"{GREEN}전부 통과!{RESET} 결과 파일: test_results/ 폴더")
    print("확인 포인트: review.txt 에서 '글로벌 컨설팅사 인턴' 부분을 지적했는지 보세요.")
    print("(초안에 일부러 넣어둔, 프로필에 없는 주장 — 사실검증이 작동하는지 확인용)")


if __name__ == "__main__":
    main()
