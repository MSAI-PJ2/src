"""LLM prompt/message builders."""


def build_system_prompt(primary: str, chunks: list[dict]) -> str:
    system = (
        "당신은 한국어로 응답하는 인지행동상담(CBT) 보조자입니다. "
        "사용자의 생각을 단정하거나 진단하지 말고, 공감적으로 반응한 뒤 "
        "검색된 참고자료와 분류 결과를 바탕으로 안전하고 실천 가능한 단계별 도움을 제공합니다. "
        "주요 인지왜곡 유형: " + primary
    )
    context = "\n".join(f"- {chunk['content']}" for chunk in chunks)
    return system + "\n[참고 자료]\n" + context


def build_llm_messages(
    primary: str,
    chunks: list[dict],
    prior_messages: list[dict],
    user_text: str,
) -> list[dict]:
    return [
        {"role": "system", "content": build_system_prompt(primary, chunks)},
        *prior_messages,
        {"role": "user", "content": user_text},
    ]
