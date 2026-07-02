#!/usr/bin/env python3
"""배포된 게이트웨이 회귀 테스트 — 모드 하나로 5개 시나리오를 통합.

사용법:
    export GW_FQDN=$(az containerapp show -g $RG -n api-gateway --query properties.configuration.ingress.fqdn -o tsv)
    read -s -p 'Gateway API key: ' API_KEY_VALUE; echo; export API_KEY_VALUE
    python gateway_live_test.py text|transcript|tts|audio|image|session|all

audio 모드는 WAV 파일 경로를 TEST_AUDIO_FILE 로 지정 (기본 없음 → 스킵).
로컬 계약 테스트(외부 서비스 불필요)는 services/api-gateway/tests/ 를 사용.
"""
import base64
import json
import os
import sys
import urllib.request

GW = os.environ.get("GW_FQDN") or sys.exit("ERROR: set GW_FQDN")
KEY = os.environ.get("API_KEY_VALUE") or sys.exit("ERROR: set API_KEY_VALUE")
TEXT = os.environ.get("TEST_TEXT", "사람들 앞에 서면 다 망칠 것 같아요")


def post_sse(payload: dict) -> list[dict]:
    """POST /v1/respond → SSE 이벤트 리스트."""
    req = urllib.request.Request(
        f"https://{GW}/v1/respond", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": KEY})
    events = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def get(path: str) -> dict:
    req = urllib.request.Request(f"https://{GW}{path}", headers={"x-api-key": KEY})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def check(name: str, events: list[dict], required_types: list[str]) -> bool:
    types = [e["type"] for e in events]
    ok = all(t in types for t in required_types) and types[-1] == "done"
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {types}")
    if not ok:
        print(json.dumps(events, ensure_ascii=False, indent=2)[:2000])
    return ok


def run(mode: str) -> bool:
    sid = os.environ.get("TEST_SESSION_ID", f"live-test-{mode}")
    if mode == "text":
        return check("text", post_sse({"session_id": sid, "text": TEXT}), ["meta", "chunks", "token"])
    if mode == "transcript":
        return check("transcript", post_sse({"session_id": sid, "stt": {"transcript": TEXT}}),
                     ["meta", "token"])
    if mode == "tts":
        events = post_sse({"session_id": sid, "text": "안녕하세요", "tts": {"enabled": True}})
        ok = check("tts", events, ["token", "tts"])
        tts = next((e for e in events if e["type"] == "tts"), {})
        return ok and tts.get("status") == "completed"
    if mode == "audio":
        path = os.environ.get("TEST_AUDIO_FILE")
        if not path:
            print("[SKIP] audio: set TEST_AUDIO_FILE=<wav path>")
            return True
        data = base64.b64encode(open(path, "rb").read()).decode()
        return check("audio", post_sse({"session_id": sid, "audio": {
            "kind": "base64", "data": data, "mime_type": "audio/wav"}}), ["stt"])
    if mode == "image":
        path = os.environ.get("TEST_IMAGE_FILE")  # jpeg/png (카톡 캡쳐 예: di/di_test_image.jpeg)
        if not path:
            print("[SKIP] image: set TEST_IMAGE_FILE=<jpeg/png path>")
            return True
        profile = os.environ.get("TEST_OCR_PROFILE", "kakao")  # kakao | generic
        names = [n for n in os.environ.get("TEST_SENDER_NAMES", "").split(",") if n]
        data = base64.b64encode(open(path, "rb").read()).decode()
        return check(f"image({profile})", post_sse({
            "session_id": sid,
            "image": {"kind": "base64", "data": data, "mime_type": "image/jpeg"},
            "ocr": {"profile": profile, "sender_names": names}}), ["ocr"])
    if mode == "session":
        post_sse({"session_id": sid, "text": TEXT})
        snap = get(f"/v1/sessions/{sid}")
        ok = snap.get("turn_count", 0) >= 2
        print(f"[{'PASS' if ok else 'FAIL'}] session: turn_count={snap.get('turn_count')}")
        return ok
    sys.exit(f"unknown mode: {mode} (text|transcript|tts|audio|image|session|all)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    modes = ["text", "transcript", "tts", "audio", "image", "session"] if mode == "all" else [mode]
    sys.exit(0 if all(run(m) for m in modes) else 1)
