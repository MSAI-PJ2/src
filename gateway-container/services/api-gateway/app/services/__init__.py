"""services — 외부 서비스 어댑터 경계. 외부 컴포넌트 하나당 파일 하나.

오케스트레이터는 SDK/HTTP 세부를 모르고 이 어댑터들만 호출한다.
테스트에서는 services 싱글톤의 각 어댑터를 가짜로 교체한다 (tests/ 참고).

    classifier  내부 cogdist Container App (인지왜곡 분류)
    safety      Azure Content Safety + 키워드 fallback
    retriever   Azure AI Search / 로컬 stub
    llm         Azure OpenAI / 로컬 OpenAI 호환 서버
    speech      Azure Speech STT/TTS
"""
from .classifier import ClassifierAdapter
from .content_safety import SafetyAdapter
from .llm import LlmAdapter
from .retriever import RetrieverAdapter
from .speech import SpeechAdapter


class GatewayServiceAdapters:
    def __init__(self):
        self.classifier = ClassifierAdapter()
        self.safety = SafetyAdapter()
        self.retriever = RetrieverAdapter()
        self.llm = LlmAdapter()
        self.speech = SpeechAdapter()


services = GatewayServiceAdapters()

__all__ = ["GatewayServiceAdapters", "services"]
