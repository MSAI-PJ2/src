"""RAG candidate ranking helpers."""

from . import settings


def rerank(
    candidates: list[dict],
    primary: str,
    confidence: float,
    top_n: int | None = None,
) -> list[dict]:
    top_n = top_n or settings.RERANK_TOP_N
    if not candidates:
        return []

    scores = [float(candidate.get("score", 0.0)) for candidate in candidates]
    min_score = min(scores)
    max_score = max(scores)
    span = max_score - min_score

    use_bias = primary not in ("정상", "불충분") and confidence >= 0.5
    deduped: dict[str, dict] = {}

    for candidate in candidates:
        raw_score = float(candidate.get("score", 0.0))
        normalized = 1.0 if span == 0 else (raw_score - min_score) / span
        distortions = candidate.get("metadata", {}).get("distortions", [])
        final_score = normalized + (0.3 if use_bias and primary in distortions else 0.0)

        ranked = {**candidate, "score": final_score}
        candidate_id = ranked.get("id")

        if candidate_id not in deduped or final_score > deduped[candidate_id]["score"]:
            deduped[candidate_id] = ranked

    return sorted(deduped.values(), key=lambda candidate: candidate["score"], reverse=True)[:top_n]
