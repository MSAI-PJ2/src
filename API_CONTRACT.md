# API Gateway API 계약서

이 문서는 `main` 기준의 Gateway API 계약 정리본이다.
Azure Speech SDK/ffmpeg 기반 STT/TTS 구조와 SSE 이벤트 계약을 기준으로 작성한다.

## 1. 기준 상태

```text
Repository: https://github.com/MSAI-PJ2/src.git
Git main: b8b6084
Gateway base URL: https://api-gateway.icybush-95bf9b25.koreacentral.azurecontainerapps.io
Auth: x-api-key 임시 필수
Runtime: FastAPI + Uvicorn
Container: Azure Container Apps
Azure revision: api-gateway--0000016
Classifier: cogdistmodel internal
Safety: Azure Content Safety
RAG: Azure AI Search cbt-rag-index
LLM: Azure OpenAI gpt-4.1-mini
Speech: Azure Speech SDK + pydub + ffmpeg
```

검증 상태:

```text
healthz PASS
auth 401 PASS
classify PASS
respond text PASS
crisis PASS
TTS PASS
audio STT success PASS
audio STT failure PASS
```

## 1.1 내부 모듈 구조

```text
services/api-gateway/app/main.py      FastAPI route entrypoint
services/api-gateway/app/dag.py       respond orchestration / STT to DAG flow
services/api-gateway/app/events.py    SSE serialization
services/api-gateway/app/safety.py    Azure Content Safety + keyword fallback
services/api-gateway/app/tts.py       TTS SSE payload builder
services/api-gateway/app/ranking.py   RAG candidate rerank
services/common/speech_client.py      Azure Speech STT/TTS client
```

설계 메모:

```text
- dag.py는 요청 orchestration 중심으로 유지한다.
- safety/tts/ranking/events는 독립 보조 모듈로 분리되어 있다.
- API 계약은 SSE event type과 payload field를 기준으로 유지한다.
- 내부 모듈 분리는 프론트 호출 계약을 바꾸지 않는다.
```

문서 표기 규칙:

```text
- 예시 JSON 블록은 실제 요청/응답에 가까운 형태로 작성한다.
- JSON 내부에는 주석을 넣지 않는다. 대신 각 예시 아래의 "필드 주석"에서 의미와 주의점을 설명한다.
- 프론트 구현은 필드 주석의 "권장 사용" 항목을 우선 따른다.
```

## 2. 공통 헤더

```http
Content-Type: application/json
x-api-key: <GATEWAY_API_KEY>
```

`/healthz`를 제외한 주요 API는 `x-api-key`가 없으면 401을 반환한다.

## 3. Health

```http
GET /healthz
```

응답:

```json
{"status":"ok"}
```

필드 주석:

```text
status: Gateway 프로세스가 HTTP 요청을 받을 수 있는지만 확인한다.
주의: 내부 cogdist/RAG/LLM/Speech까지 모두 정상이라는 의미는 아니다.
```

## 4. Classify

```http
POST /v1/classify
```

요청:

```json
{
  "text": "사람들 앞에 서면 다 망칠 것 같아요"
}
```

응답 구조:

```json
{
  "text": "사람들 앞에 서면 다 망칠 것 같아요",
  "mode": "multi_label",
  "model": "klue/roberta-base",
  "model_version": "multi-label-12c-mldistill",
  "threshold": 0.5,
  "primary": "불충분",
  "labels": [
    {"label":"불충분", "score":0.5244, "selected":true}
  ]
}
```

모델 교체 후 `primary` 값은 달라질 수 있으나, `primary`, `labels[]`, `score`, `selected` 구조는 유지한다.

필드 주석:

```text
primary: Gateway가 후속 RAG/LLM 프롬프트에 넘기는 대표 인지왜곡 라벨.
labels: 전체 라벨별 점수 목록. UI에서 상세 분석을 보여줄 때 사용한다.
selected: threshold 기준으로 선택된 라벨 여부.
주의: 모델 교체 시 점수와 primary는 달라질 수 있으므로, 프론트는 특정 라벨값을 하드코딩하지 않는다.
```

## 5. Batch Classify

```http
POST /v1/batch-classify
```

요청:

```json
{
  "texts": [
    "사람들 앞에 서면 다 망칠 것 같아요",
    "나는 늘 실패해요"
  ]
}
```

응답은 단일 classify 응답 배열이다.

필드 주석:

```text
texts: 비어 있지 않은 문자열 배열.
권장 사용: 대량 분석/관리자 도구용. 실시간 채팅 UI는 /v1/respond 사용을 우선한다.
```

## 6. Respond 공통

```http
POST /v1/respond
```

### 6.1 Optional LLM generation control

`/v1/respond` 요청 body에는 선택적으로 `llm` 옵션을 넣어 응답 생성 길이를 제어할 수 있다.
이 옵션은 일반 안전 응답 생성 경로의 LLM 호출에만 적용된다.

요청 예시:

```json
{
  "session_id": "long-answer-test-1",
  "text": "자세히 단계별로 설명해 주세요.",
  "llm": {
    "max_completion_tokens": 2048
  }
}
```

필드 주석:

```text
llm.max_completion_tokens:
  Azure OpenAI Chat Completions의 max_completion_tokens로 전달된다.
  요청별 응답 길이 조절용이며, 서버 상한을 초과할 수 없다.

llm.temperature:
  선택 값. 지정하지 않으면 서버 기본값을 사용한다.
```

서버 적용 규칙:

```text
기본값: AZURE_OPENAI_MAX_COMPLETION_TOKENS       # MVP 권장 1200
상한값: AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT # Gateway 권장 상한 12000
요청값: llm.max_completion_tokens
최종 적용값: min(요청값 또는 기본값, 상한값)
```

주의:

```text
토큰 상한을 크게 올리면 응답 시간이 길어질 수 있다.
위기/핫라인 분기에서는 llm 옵션과 무관하게 LLM 생성을 차단한다.
```


응답 타입:

```text
text/event-stream
```

즉, 일반 JSON 1개가 아니라 아래 형식의 SSE 이벤트가 순서대로 온다.

```text
data: {"type":"meta", ...}

data: {"type":"token", ...}

data: {"type":"done", ...}
```

필드 주석:

```text
type: 이벤트 종류. 프론트는 type 기준으로 분기한다.
session_id: 같은 대화 흐름을 묶는 임시 세션 식별자.
주의: SSE는 이벤트가 여러 번 도착하므로, 마지막 done 전까지 연결을 유지한다.
```

## 7. Respond - 텍스트 입력

요청:

```json
{
  "session_id": "text-test-1",
  "text": "사람들 앞에 서면 다 망칠 것 같아요"
}
```

정상 이벤트 순서:

```text
meta -> chunks -> token... -> done
```

`meta` 예시:

```json
{
  "type": "meta",
  "session_id": "text-test-1",
  "turn_count": 1,
  "primary": "불충분",
  "mode": "multi_label",
  "labels": [],
  "input": {"input_type":"text"},
  "tts": null
}
```

`chunks` 예시:

```json
{
  "type": "chunks",
  "session_id": "text-test-1",
  "chunks": [
    {"id":"exercise-reframing-3", "content":"..."}
  ]
}
```

`token` 예시:

```json
{
  "type": "token",
  "session_id": "text-test-1",
  "text": "응답 텍스트 조각"
}
```

`done` 예시:

```json
{
  "type": "done",
  "session_id": "text-test-1"
}
```

필드 주석:

```text
meta: 분류 결과와 입력 메타데이터를 담는다.
chunks: RAG 검색 결과. UI에서 출처/근거 패널로 표시 가능하다.
token: LLM 응답 조각. 여러 token.text를 이어 붙여 최종 답변을 만든다.
done: 스트림 종료 신호. done 수신 후 UI 로딩 상태를 해제한다.
```

## 8. Respond - transcript 입력

브라우저 또는 별도 STT가 이미 transcript를 만든 경우 사용한다.

요청:

```json
{
  "session_id": "transcript-test-1",
  "input_type": "transcript",
  "stt": {
    "provider": "mock",
    "language": "ko-KR",
    "transcript": "사람들 앞에 서면 다 망칠 것 같아요",
    "confidence": 0.93
  }
}
```

이 경우 Gateway는 `stt.transcript`를 텍스트 입력처럼 처리한다.

이벤트 순서:

```text
meta -> chunks -> token... -> done
```

필드 주석:

```text
input_type=transcript: 이미 STT가 끝난 텍스트라는 의미.
stt.provider: mock, browser, azure 등 transcript 출처 표시용.
stt.confidence: 제공 가능한 경우에만 넣는다. 없으면 생략 가능하다.
권장 사용: 브라우저 Web Speech API 또는 별도 음성 서비스가 먼저 transcript를 만든 경우.
```

## 9. Respond - audio 입력/STT

지원 입력 형태:

```json
{
  "session_id": "stt-audio-test-1",
  "input_type": "audio",
  "audio": {
    "kind": "base64",
    "data": "<AUDIO_BASE64>",
    "mime_type": "audio/wav",
    "language": "ko-KR"
  },
  "stt": {
    "provider": "azure",
    "language": "ko-KR"
  }
}
```

`audio.kind`:

```text
base64: audio.data 사용
url: audio.url 사용
```

`pydub + ffmpeg` 변환 경로는 유지한다. 따라서 `audio/wav`뿐 아니라 브라우저 녹음 포맷도 테스트 대상이다.

필드 주석:

```text
audio.kind=base64: 프론트가 오디오 파일을 base64로 직접 실어 보낼 때 사용한다.
audio.kind=url: Gateway 컨테이너가 접근 가능한 URL을 넘길 때 사용한다.
audio.mime_type: 변환/인식 경로 판단에 사용한다. 가능하면 정확히 넣는다.
audio.language: 기본값은 ko-KR. 다국어 입력이 필요하면 명시한다.
주의: base64 payload는 커질 수 있으므로, 장기적으로는 Blob/SAS URL 방식이 더 적합하다.
```

### 9.1 STT 성공 이벤트 순서

```text
stt(processing) -> stt(completed) -> meta -> chunks -> token... -> done
```

`stt processing`:

```json
{
  "type": "stt",
  "session_id": "stt-audio-test-1",
  "status": "processing",
  "provider": "azure",
  "language": "ko-KR"
}
```

`stt completed`:

```json
{
  "type": "stt",
  "session_id": "stt-audio-test-1",
  "status": "completed",
  "provider": "azure",
  "language": "ko-KR",
  "mime_type": "audio/wav",
  "kind": "base64",
  "transcript": "사람들 앞에 서면 다 망칠 것 같아요.",
  "confidence": null,
  "recognition_status": "RecognizedSpeech"
}
```

필드 주석:

```text
status=processing: 서버가 STT 처리를 시작했다는 신호.
status=completed: transcript가 생성되어 이후 classify/RAG/LLM 단계로 이어진다는 신호.
transcript: 이후 DAG에 실제 입력으로 들어가는 텍스트.
recognition_status: Azure Speech SDK 인식 상태를 프론트/로그에서 확인하기 위한 디버깅 필드.
confidence: SDK/경로에 따라 없을 수 있으므로 null 허용.
```

### 9.2 STT 실패 이벤트 순서

```text
stt(processing) -> stt(error|no_match) -> input_required -> done
```

`stt error`:

```json
{
  "type": "stt",
  "session_id": "stt-audio-test-1",
  "status": "error",
  "provider": "azure",
  "language": "ko-KR",
  "mime_type": "audio/webm",
  "kind": "base64",
  "transcript": "",
  "error": "구체적 실패 사유"
}
```

`input_required`:

```json
{
  "type": "input_required",
  "session_id": "stt-audio-test-1",
  "reason": "error",
  "message": "audio payload was accepted, but STT did not produce a transcript. Check stt event error/reason, or send text/stt.transcript."
}
```

필드 주석:

```text
status=no_match: 음성은 처리했지만 인식 가능한 발화가 없다는 의미.
status=error: 키/권한/포맷/ffmpeg/Azure Speech 호출 등 처리 오류.
error: 개발/테스트 디버깅용 메시지. 운영 UI에서는 그대로 노출하지 않는 것이 좋다.
input_required: 프론트가 사용자에게 텍스트 재입력 또는 음성 재녹음을 요청할 때 사용한다.
```

## 10. Respond - TTS

요청:

```json
{
  "session_id": "tts-test-1",
  "text": "사람들 앞에 서면 다 망칠 것 같아요",
  "tts": {
    "enabled": true,
    "provider": "azure",
    "voice": "ko-KR-SunHiNeural",
    "format": "wav"
  }
}
```

이벤트 순서:

```text
meta -> chunks -> token... -> tts -> done
```

TTS 이벤트 계약:

```json
{
  "type": "tts",
  "session_id": "tts-test-1",
  "status": "completed",
  "provider": "azure",
  "text": "최종 LLM 응답 텍스트",
  "mime_type": "audio/wav",
  "format": "wav",
  "audio": {
    "kind": "base64",
    "data": "<AUDIO_BASE64>",
    "mime_type": "audio/wav"
  },
  "audio_base64": "<AUDIO_BASE64>",
  "options": {
    "enabled": true,
    "provider": "azure",
    "voice": "ko-KR-SunHiNeural",
    "format": "wav"
  }
}
```

프론트 권장 처리:

```python
audio_data = event.get("audio", {}).get("data") or event.get("audio_base64")
mime_type = event.get("audio", {}).get("mime_type") or event.get("mime_type")
```

`event.audio.data`가 canonical이다. `event.audio_base64`는 구버전 호환 alias다.

필드 주석:

```text
status=completed: TTS 오디오 생성 성공.
status=error: LLM 응답은 생성됐지만 TTS 변환만 실패.
audio.data: 권장 base64 오디오 필드.
audio_base64: 기존 클라이언트 호환용 alias. 신규 구현은 audio.data를 우선 사용한다.
mime_type: 현재 SDK 경로는 audio/wav 기준. mp3가 필요하면 별도 변환/출력 설정을 확정해야 한다.
주의: TTS 실패가 전체 응답 실패를 의미하지는 않는다. token 응답은 그대로 사용할 수 있다.
```

## 11. Crisis 분기

자해/자살 위험 신호가 감지되면 LLM 응답 대신 crisis 이벤트를 반환한다.

이벤트 순서:

```text
meta -> crisis -> done
```

`crisis` 예시:

```json
{
  "type": "crisis",
  "blocked": true,
  "reason": "self_harm",
  "message": "지금 많이 힘들고 고통스러우신 것 같아요...",
  "resources": [
    {"name":"자살예방상담전화", "phone":"1393", "hours":"24시간"}
  ]
}
```

필드 주석:

```text
blocked=true: 안전 배리어가 일반 LLM 응답을 차단했다는 의미.
reason: self_harm 등 차단 사유.
message/resources: UI가 사용자에게 표시할 안전 안내.
주의: crisis 이벤트에서는 일반 token 응답을 기대하지 않는다.
```

## 12. Session

```http
POST /v1/sessions
GET /v1/sessions/{session_id}
```

현재 세션은 in-memory다.

주의:

```text
Container App 재시작, revision 변경, replica 변경 시 세션이 사라질 수 있다.
Cosmos DB 연동 전까지 운영 세션 저장소로 보면 안 된다.
```

필드 주석:

```text
session_id: 없으면 Gateway가 생성할 수 있으나, 프론트 테스트에서는 명시하는 편이 추적하기 쉽다.
turn_count: 현재 세션 내 턴 수.
주의: 현재 세션은 임시 메모리 저장소다. 개인정보/상담기록의 영구 저장 정책은 Cosmos DB 설계 후 확정한다.
```

## 13. 프론트 SSE 처리 예시

```python
import json
import requests

resp = requests.post(
    f"{BASE}/v1/respond",
    headers={"Content-Type":"application/json", "x-api-key": GATEWAY_API_KEY},
    json={"session_id":"ui-test-1", "text":"사람들 앞에 서면 다 망칠 것 같아요"},
    stream=True,
    timeout=120,
)

answer_parts = []
for raw in resp.iter_lines(decode_unicode=True):
    if not raw or not raw.startswith("data: "):
        continue

    event = json.loads(raw[6:])
    typ = event.get("type")

    if typ == "stt":
        stt_status = event.get("status")
        transcript = event.get("transcript")
        stt_error = event.get("error") or event.get("reason")
    elif typ == "meta":
        primary = event.get("primary")
    elif typ == "chunks":
        chunks = event.get("chunks", [])
    elif typ == "token":
        answer_parts.append(event.get("text", ""))
    elif typ == "tts":
        audio_data = event.get("audio", {}).get("data") or event.get("audio_base64")
        mime_type = event.get("audio", {}).get("mime_type") or event.get("mime_type")
    elif typ == "crisis":
        crisis_message = event.get("message")
    elif typ == "input_required":
        input_required_reason = event.get("reason")
    elif typ == "done":
        break

answer = "".join(answer_parts)
```

## 14. Azure 테스트 체크리스트

배포 후 아래 순서로 검증한다.

```text
1. /healthz 200
2. /v1/classify 200
3. /v1/respond text: meta/chunks/token/done
4. /v1/respond crisis: meta/crisis/done
5. /v1/respond transcript: meta/chunks/token/done
6. /v1/respond audio wav: stt processing/completed/meta/chunks/token/done
7. /v1/respond tts: tts status=completed, audio.data 존재
8. STT 실패 샘플: stt error 또는 no_match가 노출되는지 확인
```

이 체크리스트 통과 후에만 구조 리팩터링 브랜치를 시작한다.
