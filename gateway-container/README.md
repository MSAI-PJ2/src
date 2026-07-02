# 게이트웨이 컨테이너

이 폴더는 Azure Container Apps `api-gateway` 배포에 필요한 게이트웨이 컨테이너 소스입니다.
리포지토리 루트에는 다른 팀 코드가 추가될 수 있으므로, 게이트웨이 Docker 빌드 컨텍스트는 `gateway-container/`로 제한합니다.

## 사용 기술

```text
Framework: FastAPI + Uvicorn
Container: Azure Container Apps / Azure Container Registry
Auth: 임시 x-api-key (Entra External ID 도입 예정 — app/core/auth.py 가이드)
Classifier: internal cogdistmodel Container App
Safety: Azure AI Content Safety + keyword fallback
RAG: Azure AI Search
LLM: Azure OpenAI gpt-4.1-mini (로컬 개발은 LLM_PROVIDER=local)
Speech: Azure Speech STT/TTS
Session store: memory 또는 Azure Cosmos DB NoSQL
```

## 폴더 구조

```text
gateway-container/
|-- API_CONTRACT.md            프론트엔드/테스트 API 계약서 (v1 기준 문서)
|-- docker-compose.yml         로컬 실행
|-- scripts/                   Azure 배포본 회귀 테스트 스크립트
`-- services/
    |-- api-gateway/
    |   |-- Dockerfile
    |   |-- requirements.txt
    |   |-- tests/             v1 계약 테스트 (외부 서비스 없이 로컬 실행)
    |   `-- app/
    |       |-- main.py        앱 생성 + 라우터 등록만
    |       |-- api/v1/        FastAPI 라우터 (HTTP 만 담당)
    |       |-- orchestrator/  상담 응답 흐름 + 컨텍스트 정책 + 위기 분기
    |       |-- services/      외부 서비스 어댑터 (컴포넌트당 파일 하나)
    |       |-- session/       세션 저장소 (memory/Cosmos) + 턴 빌더
    |       |-- streaming/     SSE 직렬화 + 이벤트 payload
    |       |-- contracts/     요청 Pydantic 모델
    |       |-- rag/           검색 후보 재정렬
    |       |-- llm/           프롬프트 (사람 편집용)
    |       `-- core/          설정 / 인증 / 계측
    |-- common/                LLM/Speech 클라이언트 (+ *_legacy = 프로토타입 보관)
    `-- retrieve/              Retriever provider (local stub / Azure AI Search)
```

## 처음 읽는 사람의 읽는 순서

```text
1. API_CONTRACT.md                          외부 계약(엔드포인트/SSE 이벤트) 파악
2. app/main.py → app/api/v1/                어떤 엔드포인트가 있는지
3. app/orchestrator/respond_flow.py         상담 한 턴의 전체 흐름 (핵심)
4. app/orchestrator/context_policy.py       라벨별 응답 정책 (사람 편집용)
5. app/llm/prompts.py                       시스템 프롬프트/답변 스타일 (사람 편집용)
6. app/services/                            외부 서비스 호출 세부
7. app/session/, app/streaming/             저장/이벤트 형태
```

## 사람이 편집하도록 만든 지점

```text
답변 스타일/프롬프트     app/llm/prompts.py          (PERSONA, STYLE_RULES, LABEL_GUIDANCE)
라벨별 응답 정책         app/orchestrator/context_policy.py  (POLICIES 테이블)
위기 메시지/핫라인       app/orchestrator/crisis.py   (+ 위치 기반 DB 조회 작업 가이드)
로그인(Entra) 도입       app/core/auth.py             (단계별 작업 가이드 주석)
```

## 테스트

외부 서비스 없이 v1 계약(SSE 이벤트 순서/형태)을 검증합니다. 리팩토링 후 반드시 실행:

```bash
cd gateway-container/services/api-gateway
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Azure 배포본에 대한 회귀 테스트는 `scripts/`(실 endpoint 대상)를 사용합니다.

## ACR 빌드

리포지토리 루트에서 실행합니다.

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f services/api-gateway/Dockerfile \
  gateway-container
```

중요: 빌드 컨텍스트는 `.`가 아니라 `gateway-container`입니다. 이렇게 해야 리포지토리 루트의 다른 팀 코드가 게이트웨이 이미지에 포함되지 않습니다.

## 로컬 실행

```bash
docker compose -f gateway-container/docker-compose.yml up --build api-gateway
```

## 보안 메모

- 실제 키와 비밀값은 커밋하지 않습니다.
- 공유 가능한 템플릿은 `gateway-container/.env.example`입니다.
- Azure Container Apps의 실제 키는 SecretRef로 관리합니다.
- 프론트엔드 코드에는 게이트웨이 API 키를 하드코딩하지 않습니다.

## 현재 검증 상태

- `/healthz` PASS
- `x-api-key` 미포함 요청 401 PASS
- `/v1/classify` PASS
- `/v1/respond` text/transcript/audio STT/TTS PASS
- crisis branch PASS
- Cosmos session persistence PASS
- `tests/` 12건 (v1 계약 characterization) PASS — 리팩토링 전/후 동일 통과

Document Intelligence OCR은 별도 브랜치 작업입니다.
향후 예상 입력은 `/v1/respond`의 `input_type=document`이며, OCR 성공 후 기존 text DAG로 연결합니다.
