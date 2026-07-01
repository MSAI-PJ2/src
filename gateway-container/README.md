# 게이트웨이 컨테이너

이 폴더는 Azure Container Apps `api-gateway` 배포에 필요한 게이트웨이 컨테이너 소스입니다.
리포지토리 루트에는 다른 팀 코드가 추가될 수 있으므로, 게이트웨이 Docker 빌드 컨텍스트는 `gateway-container/`로 제한합니다.

## 폴더 구조

```text
gateway-container/
|-- .dockerignore
|-- .env.example
|-- API_CONTRACT.md
|-- README.md
|-- README_GATEWAY_SHARE.md
|-- docker-compose.yml
|-- scripts/
`-- services/
    |-- api-gateway/
    |   |-- Dockerfile
    |   |-- requirements.txt
    |   `-- app/
    |-- common/
    `-- retrieve/
```

## 사용 기술

```text
Framework: FastAPI + Uvicorn
Container: Azure Container Apps / Azure Container Registry
Auth: 임시 x-api-key
Classifier: internal cogdistmodel Container App
Safety: Azure AI Content Safety + keyword fallback
RAG: Azure AI Search
LLM: Azure OpenAI gpt-4.1-mini
Speech: Azure Speech STT/TTS
Session store: memory 또는 Azure Cosmos DB NoSQL
```

## 주요 파일

```text
services/api-gateway/app/main.py                         FastAPI 라우트 진입점
services/api-gateway/app/dag.py                          respond 오케스트레이션
services/api-gateway/app/adapters.py                     외부 서비스 어댑터 경계
services/api-gateway/app/request_context.py              /v1/respond 요청 정규화
services/api-gateway/app/repositories/session_repository.py  memory/Cosmos 세션 저장소
services/api-gateway/app/payloads.py                     SSE/API 페이로드 빌더
services/api-gateway/app/turns.py                        세션 턴 빌더
services/api-gateway/app/prompts.py                      LLM 메시지 빌더
services/api-gateway/app/events.py                       SSE 직렬화
services/api-gateway/app/safety.py                       Content Safety + 키워드 fallback
services/api-gateway/app/tts.py                          TTS 이벤트 페이로드 빌더
services/api-gateway/app/ranking.py                      RAG 후보 재정렬
services/common/llm_client.py                            Azure OpenAI/local LLM 클라이언트
services/common/retrieve_client.py                       Azure AI Search 저수준 클라이언트
services/common/speech_client.py                         Azure Speech STT/TTS 클라이언트
services/retrieve/client.py                              Retriever provider 래퍼
scripts/                                                 Azure 회귀 테스트 스크립트
API_CONTRACT.md                                          프론트엔드/테스트 API 계약서
```

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

Document Intelligence OCR은 별도 브랜치 작업입니다.
향후 예상 입력은 `/v1/respond`의 `input_type=document`이며, OCR 성공 후 기존 text DAG로 연결합니다.

