# CogDist v2 Container

`ml/outputs/multi_large/best`의 RoBERTa-large multi-label 모델을 서비스하기 위한 cogdist API 컨테이너입니다.

## 핵심 변경

multi-label 모드에서도 `정상` / `불충분`을 배타 라벨로 처리합니다.

규칙:

1. `primary` 라벨은 항상 `selected=true`
2. `primary`가 `정상` 또는 `불충분`이면 해당 라벨 하나만 `selected=true`
3. `primary`가 인지왜곡 라벨이면 `정상` / `불충분`은 `selected=false`

## API 계약

기존 게이트웨이가 호출하던 계약을 유지합니다.

```text
GET  /healthz
GET  /readyz
POST /v1/predict
POST /v1/batch-predict
```

`POST /v1/predict` 요청:

```json
{"text":"사람들 앞에 서면 다 망칠 것 같아요", "threshold":0.55}
```

응답은 기존 게이트웨이 호환을 위해 전체 라벨 배열을 유지합니다.
`selected` 값만 정책에 맞게 정리됩니다.

```json
{
  "text": "...",
  "mode": "multi_label",
  "model": "klue/roberta-large",
  "model_version": "multi_large_v2",
  "threshold": 0.55,
  "primary": "불충분",
  "labels": [
    {"label":"'해야 한다' 진술", "score":0.0015, "selected":false},
    {"label":"감정적 추론", "score":0.0046, "selected":false},
    {"label":"불충분", "score":0.5244, "selected":true},
    {"label":"흑백 사고", "score":0.0155, "selected":false}
  ]
}
```

## Azure Container Apps 배포 방식

현재 운영 구조처럼 Azure Files를 `/models/cogdist`에 mount하는 nobake 방식을 기본으로 합니다.
컨테이너 이미지에는 모델 파일을 굽지 않습니다. 따라서 빌드 컨텍스트는 `ml/cogdist-server/`만 사용합니다.

필수 환경변수:

```text
MODEL_PATH=/models/cogdist
MODEL_ID=klue/roberta-large
MODEL_VERSION=multi_large_v2
CLASSIFY_MODE=multi_label
DEFAULT_THRESHOLD=0.55
MAX_LENGTH=160
```

## ACR 빌드 예시

repo root에서 실행:

```bash
TAG=cogdist-v2-exclusive-20260702
az acr build \
  -r "$ACR" \
  -t cogdist:$TAG \
  ml/cogdist-server
```

또는 `ml/cogdist-server/`로 이동 후:

```bash
cd ml/cogdist-server
az acr build -r "$ACR" -t cogdist:$TAG .
```

## ACA 업데이트 예시

```bash
az containerapp update \
  -g "$RG" \
  -n cogdistmodel \
  --image "$ACR.azurecr.io/cogdist:$TAG" \
  --set-env-vars \
    MODEL_PATH=/models/cogdist \
    MODEL_ID=klue/roberta-large \
    MODEL_VERSION=multi_large_v2 \
    CLASSIFY_MODE=multi_label \
    DEFAULT_THRESHOLD=0.55 \
    MAX_LENGTH=160
```

모델 파일은 기존처럼 Azure Files 볼륨 `modelstore`, subPath `v2`, mountPath `/models/cogdist`를 유지합니다.

## API 호환성 확인 포인트

게이트웨이의 `ClassifierAdapter`는 cogdist 응답에서 아래 필드를 사용합니다.
따라서 이 컨테이너는 기존 API 계약을 유지합니다.

```text
text
mode
model
model_version
threshold
primary
labels[].label
labels[].score
labels[].selected
```

특히 게이트웨이의 `/v1/classify`, `/v1/respond`는 `primary`와 `labels`를 그대로 사용하므로,
배포 후 아래를 확인해야 합니다.

1. HTTP 200 응답
2. `primary` 존재
3. `labels`가 비어 있지 않음
4. `primary`와 같은 라벨의 `selected=true`
5. `primary`가 `정상` 또는 `불충분`이면 선택 라벨이 1개만 존재
6. `primary`가 인지왜곡 라벨이면 `정상` / `불충분`은 `selected=false`

## Azure 배포 준비 명령

Cloud Shell에서 repo root(`~/src`) 기준:

```bash
RG=10ai_2nd_team3
APP=cogdistmodel
ACR=$(az acr list -g "$RG" --query "[0].name" -o tsv)
TAG=cogdist-v2-exclusive-20260702

az acr build \
  -r "$ACR" \
  -t cogdist:$TAG \
  ml/cogdist-server
```

배포:

```bash
az containerapp update \
  -g "$RG" \
  -n "$APP" \
  --image "$ACR.azurecr.io/cogdist:$TAG" \
  --set-env-vars \
    MODEL_PATH=/models/cogdist \
    MODEL_ID=klue/roberta-large \
    MODEL_VERSION=multi_large_v2 \
    CLASSIFY_MODE=multi_label \
    DEFAULT_THRESHOLD=0.55 \
    MAX_LENGTH=160
```

배포 상태 확인:

```bash
az containerapp revision list \
  -g "$RG" \
  -n "$APP" \
  -o table

az containerapp show \
  -g "$RG" \
  -n "$APP" \
  --query "{image:properties.template.containers[0].image, revision:properties.latestRevisionName, state:properties.provisioningState}" \
  -o yaml
```

## 게이트웨이 경유 호환성 테스트

cogdistmodel은 내부 서비스이므로 외부에서 직접 호출하지 않고 게이트웨이로 확인합니다.

```bash
GW_FQDN=$(az containerapp show \
  -g "$RG" \
  -n api-gateway \
  --query "properties.configuration.ingress.fqdn" -o tsv)

curl -sS --max-time 90 \
  -X POST "https://$GW_FQDN/v1/classify" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $API_KEY_VALUE" \
  -d '{"text":"사람들 앞에 서면 다 망칠 것 같아요"}' \
  | python -m json.tool
```

예상 확인값:

```text
model = klue/roberta-large
model_version = multi_large_v2
threshold = 0.55
primary 존재
labels 배열 존재
```

내부 직접 확인이 필요하면 `api-gateway` 컨테이너에서 internal FQDN으로 호출합니다.

```bash
az containerapp exec \
  -g "$RG" \
  -n api-gateway \
  --command "python -c \"import json,urllib.request; url='https://cogdistmodel.internal.icybush-95bf9b25.koreacentral.azurecontainerapps.io/v1/predict'; payload=json.dumps({'text':'사람들 앞에 서면 다 망칠 것 같아요'}).encode(); req=urllib.request.Request(url,data=payload,headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req,timeout=90).read().decode())\""
```

## 롤백 참고

문제가 있으면 직전 정상 이미지로 되돌립니다. 현재 운영에서 확인된 이전 계열 이미지는 예시입니다.
실제 롤백 전에는 `az containerapp revision list`와 `az containerapp show`로 이미지 태그를 확인하세요.

```bash
az containerapp update \
  -g "$RG" \
  -n cogdistmodel \
  --image "$ACR.azurecr.io/cogdist:nobake"
```
