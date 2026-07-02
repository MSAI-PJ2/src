# 게이트웨이 컨테이너

Azure Container Apps `api-gateway` 배포용 소스. Docker 빌드 컨텍스트는 `gateway-container/`로 제한한다
(리포지토리 루트의 다른 팀 코드가 이미지에 포함되지 않게).

## 사용 기술

```text
FastAPI + Uvicorn / Azure Container Apps + ACR
Auth: x-api-key (기본) 또는 Entra External ID JWT — 코드 구현 완료, .env 로 전환 (api/v1.py 구획 2)
Classifier: internal cogdist Container App | Safety: Azure Content Safety + keyword fallback
RAG: Azure AI Search | LLM: Azure OpenAI gpt-4.1-mini | Speech: Azure Speech STT/TTS
OCR: Azure Document Intelligence — ocr.profile 로 분기: generic(일반 이미지, 기본) | kakao(카톡 캡쳐 화자분리)
Session: memory(개발) 또는 Azure Cosmos DB
```

## 폴더 구조 — 기능 도메인별 병합 (파일 내부는 [구획 N] 배너로 분리)

```text
gateway-container/
|-- Dockerfile · requirements.txt
|-- API_CONTRACT.md            프론트/테스트 API 계약서 (기준 문서)
|-- docker-compose.yml         로컬 실행
|-- scripts/gateway_live_test.py   배포본 회귀 테스트 (text|transcript|tts|audio|image|session)
|-- tests/                     v1 계약 테스트 (외부 서비스·키 없이 실행)
`-- app/
    |-- main.py            앱 생성 + 미들웨어 + 라우터 등록
    |-- settings.py        환경변수 (모든 조정값)
    |-- session.py         세션 — 저장소 규격 + memory/Cosmos 구현 + 턴 빌더
    |-- api/
    |   `-- v1.py          [HTTP 경계]  구획: ①요청 모델 ②인증(+Entra 가이드) ③라우트
    |-- respond/
    |   |-- policy.py      [사람 편집]  구획: ①정책 테이블 ②시스템 프롬프트 ③위기 대응
    |   `-- flow.py        [기계장치]  구획: ①SSE 이벤트 ②요청 정리 ③RAG 재정렬 ④진입 스트림 ⑤핵심 흐름
    `-- services/          [외부 창구] 컴포넌트당 파일 하나 (어댑터+클라이언트 통합)
        |-- classifier.py · content_safety.py · document_ocr.py
        `-- llm.py · retriever.py · speech.py
```

각 병합 파일 상단 docstring 에 **구획 목차**가 있다 — `[구획` 으로 Ctrl+F 하면 원하는
영역으로 바로 이동한다. "무엇을 답할지"는 respond/policy.py, "어떻게 흘러가는지"는
respond/flow.py 로 역할이 갈린다.

### OCR 파이프라인 출처

`app/services/document_ocr.py` 는 **DI 담당 팀원이 작업한 리포 루트 `di/kakao_ocr_pipeline.py` 의
복제·개조본**이다 (병합 재정리와 무관하게 이 파일은 그대로 유지). 원본은 포트폴리오·독립 실행 목적으로 무수정 보존하며, 알고리즘 설명
(라인 분류 · 좌우 화자 판별 · y좌표 타임스탬프 매칭)은 `di/README.md` 가 기준 문서다.
게이트웨이 쪽 개조는 입출력 포장뿐이다: bytes 입력, dict 반환, dotenv 제거.

## 용어 미니 사전 (비전공자용)

```text
게이트웨이     프론트엔드와 여러 Azure 서비스 사이에서 교통정리를 하는 중간 서버
엔드포인트     서버가 받는 요청 주소 (예: POST /v1/respond). 전체 목록은 app/api/v1.py 구획 3
SSE/스트리밍   답변을 완성 후 한 번에 주지 않고 생성되는 대로 조각조각 보내는 방식
이벤트         스트리밍으로 보내는 메시지 조각. type 필드로 구분 (meta/token/done 등)
세션 / 턴      대화방 하나 / 그 안의 발화 하나 (사용자 또는 AI)
RAG            답변 전에 관련 자료를 검색해 프롬프트에 넣어주는 기법
시스템 프롬프트 AI 에게 답변 전에 주는 지시문 (말투·역할·금지사항) — app/respond/policy.py 구획 2
어댑터         외부 서비스의 복잡한 호출법을 감추는 중간층 — app/services/
async/await    기다리는 동안 다른 요청을 처리할 수 있게 하는 파이썬 문법
yield          함수가 끝나지 않은 채 값을 하나씩 내보내는 문법 (스트리밍의 재료)
환경변수(env)  코드 밖에서 주입하는 설정값 (키·주소 등) — app/settings.py 에서 읽음
```

모든 파일 상단에 `[역할] 설명` docstring 이 있고, 논리 단계마다 한국어 주석이 있다.

## 읽는 순서

```text
1. API_CONTRACT.md              외부 계약(엔드포인트/SSE 이벤트)
2. app/api/v1.py [구획 3]       엔드포인트 목록
3. app/respond/flow.py [구획 5]  상담 한 턴의 핵심 6단계
4. app/respond/policy.py        정책·프롬프트·위기 (사람 편집 영역 전부)
```

## 사람이 편집하는 지점 — respond/policy.py 한 파일

```text
라벨별 응답 정책       respond/policy.py [구획 1]   POLICIES 테이블 (+ 저확신 강등 노브)
답변 스타일/프롬프트   respond/policy.py [구획 2]   PERSONA·STYLE_RULES·LABEL_GUIDANCE
위기 메시지/핫라인     respond/policy.py [구획 3]   + 위치 기반 DB 조회 작업 가이드
로그인(Entra) 켜기     .env (AUTH_MODE=entra + ENTRA_CLIENT_ID + ENTRA_TENANT_ID/ISSUER)
튜닝 노브             .env (RERANK_BIAS_* · POLICY_MIN_CONFIDENCE — settings.py 참고)
```

## 테스트

```bash
# 로컬 계약 테스트 (키 불필요) — 게이트웨이 수정 후 필수
cd gateway-container
pip install -r requirements-dev.txt
python -m pytest tests/ -q

# 배포본 회귀 테스트
python scripts/gateway_live_test.py all   # GW_FQDN, API_KEY_VALUE 필요
```

## 빌드·실행

```bash
# ACR 빌드 (리포지토리 루트에서, 컨텍스트는 반드시 gateway-container)
# ※ 구조 평탄화로 -f 경로가 바뀌었다 (이전: services/api-gateway/Dockerfile)
az acr build -r "$ACR" -t gateway:<TAG> -f Dockerfile gateway-container

# 로컬 실행 (.env 는 .env.example 참고)
docker compose -f gateway-container/docker-compose.yml up --build api-gateway
```

## 보안 메모

- 실제 키·비밀값 커밋 금지. 템플릿은 `.env.example`, 운영 키는 ACA SecretRef.
- 프론트엔드 코드에 게이트웨이 API 키 하드코딩 금지.

## 상태

- `/healthz`·`/v1/classify`·`/v1/respond`(text/transcript/audio/TTS)·crisis·Cosmos 세션 검증 PASS
- 이미지 입력(`input_type=image`) 통합됨 — `ocr.profile`: generic(일반)/kakao(카톡 캡쳐).
  실이미지 배포 검증 대기
- Entra External ID JWT 인증 구현 완료(잠금 상태) — `AUTH_MODE=entra` + ENTRA_* 3개로 즉시 활성화.
  세션을 user_id 로 스코프하는 것은 다음 단계(api/v1.py 구획 2 주석에 훅 명시)
- 프로토타입 경로(로컬 LLM, GPT-5 responses, 로컬 retriever stub 등)는 삭제됨
  — 필요하면 git 히스토리(070c799 이전)에서 복원
- 계약 테스트 32건 통과 — 구조 평탄화(A+B+C) 전후 동일
