"""Application Insights(OpenTelemetry) 계측 설정.

APPLICATIONINSIGHTS_CONNECTION_STRING 이 설정된 환경(ACA 배포)에서만 활성화한다.
로컬 개발/테스트에서는 아무 것도 하지 않으므로 연결 문자열 없이도 앱이 뜬다.
"""
import logging
import os

logger = logging.getLogger(__name__)


def setup() -> None:
    if not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        logger.info("App Insights 연결 문자열 없음 — 계측 비활성화 (로컬 모드)")
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor()
        logger.info("App Insights 계측 활성화")
    except Exception as exc:
        # 계측 실패가 서비스 기동을 막으면 안 된다. 원인은 로그로만 남긴다.
        logger.warning("App Insights 계측 초기화 실패(서비스는 계속 기동): %s", exc)
