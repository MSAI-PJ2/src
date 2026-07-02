"""pytest 공용 설정 — 컨테이너와 동일한 import 배치를 로컬에서 재현한다.

컨테이너(/app)에는 app/, common/, retrieve/ 가 나란히 복사된다(Dockerfile 참고).
로컬 테스트에서는 services/ 와 services/api-gateway/ 를 sys.path 에 추가해
같은 import 구조(`app.*`, `common.*`, `retrieve.*`)를 만든다.

실행 방법 (gateway-container/services/api-gateway 에서):
    python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_API_GATEWAY_DIR = Path(__file__).resolve().parents[1]   # services/api-gateway
_SERVICES_DIR = _API_GATEWAY_DIR.parent                   # services

for path in (str(_SERVICES_DIR), str(_API_GATEWAY_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# App Insights(OpenTelemetry) 배포 의존성은 로컬 테스트에 불필요하므로 no-op 스텁으로 대체.
# 실제 계측은 APPLICATIONINSIGHTS_CONNECTION_STRING 이 설정된 배포 환경에서만 동작한다.
_fake_otel = types.ModuleType("azure.monitor.opentelemetry")
_fake_otel.configure_azure_monitor = lambda **kwargs: None
_fake_monitor = types.ModuleType("azure.monitor")
_fake_monitor.opentelemetry = _fake_otel
sys.modules.setdefault("azure.monitor", _fake_monitor)
sys.modules.setdefault("azure.monitor.opentelemetry", _fake_otel)
