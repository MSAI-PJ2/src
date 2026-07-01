#!/usr/bin/env python3
"""Collect /v1/respond SSE events for text input.

Required env:
- GW_FQDN: api-gateway FQDN without https://
- API_KEY_VALUE: temporary gateway API key
"""
import json
import os
import sys
import urllib.request
from pathlib import Path


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing env {name}", file=sys.stderr)
        print("Set it first, for example:", file=sys.stderr)
        if name == "GW_FQDN":
            print(
                "  export GW_FQDN=$(az containerapp show -g $RG -n api-gateway "
                "--query properties.configuration.ingress.fqdn -o tsv)",
                file=sys.stderr,
            )
        else:
            print(
                "  read -s -p 'Gateway API key: ' API_KEY_VALUE; echo; export API_KEY_VALUE",
                file=sys.stderr,
            )
        sys.exit(2)
    return value


def main() -> int:
    gw = require_env("GW_FQDN")
    key = require_env("API_KEY_VALUE")
    url = f"https://{gw}/v1/respond"

    payload = {
        "session_id": os.environ.get("TEST_SESSION_ID", "api-contract-text-1"),
        "text": os.environ.get("TEST_TEXT", "사람들 앞에 서면 다 망칠 것 같아요"),
    }
    max_completion_tokens = os.environ.get("TEST_MAX_COMPLETION_TOKENS")
    if max_completion_tokens:
        payload["llm"] = {"max_completion_tokens": int(max_completion_tokens)}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "x-api-key": key},
        method="POST",
    )

    events = []
    answer_parts = []
    token_count = 0

    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue

            event = json.loads(line[6:])
            events.append(event)
            typ = event.get("type")

            if typ != "token":
                print("EVENT =", typ)

            if typ == "meta":
                print("  primary =", event.get("primary"))
                print("  turn_count =", event.get("turn_count"))
            elif typ == "chunks":
                print("  chunks =", [c.get("id") for c in event.get("chunks", [])])
            elif typ == "token":
                token_count += 1
                answer_parts.append(event.get("text", ""))
            elif typ == "crisis":
                print("  crisis reason =", event.get("reason"))
            elif typ == "input_required":
                print("  input_required reason =", event.get("reason"))
            elif typ == "done":
                break

    answer = "".join(answer_parts)
    print("TOKEN events =", token_count)
    print("ANSWER chars =", len(answer))
    print("ANSWER preview =")
    print(answer[:800])

    Path("api_contract_text_events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("api_contract_text_answer.txt").write_text(answer, encoding="utf-8")
    print("SAVED api_contract_text_events.json")
    print("SAVED api_contract_text_answer.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
