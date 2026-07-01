import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GW_FQDN", "").strip()
KEY = os.environ.get("API_KEY_VALUE", "").strip()
SESSION_ID = os.environ.get("TEST_SESSION_ID", "cosmos-session-smoke-1")
TEXT = os.environ.get("TEST_TEXT", "사람들 앞에 서면 다 망칠 것 같아요")

if not BASE or not KEY:
    print("ERROR: GW_FQDN/API_KEY_VALUE missing", file=sys.stderr)
    sys.exit(2)

base_url = BASE if BASE.startswith("http") else f"https://{BASE}"
headers = {"Content-Type": "application/json", "x-api-key": KEY}


def request_json(method: str, path: str, payload=None, timeout=120):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def post_sse(path: str, payload: dict, timeout=180):
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    events = []
    answer_parts = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            typ = event.get("type")
            if typ == "token":
                answer_parts.append(event.get("text", ""))
            elif typ in {"meta", "chunks", "crisis", "input_required", "done"}:
                print("EVENT =", typ)
                if typ == "meta":
                    print("  session_id =", event.get("session_id"))
                    print("  turn_count =", event.get("turn_count"))
                    print("  primary =", event.get("primary"))
                if typ == "chunks":
                    print("  chunks =", [c.get("id") for c in event.get("chunks", [])])
                if typ == "crisis":
                    print("  reason =", event.get("reason"))
                if typ == "input_required":
                    print("  reason =", event.get("reason"))
    return events, "".join(answer_parts)


print("BASE =", base_url)
print("SESSION_ID =", SESSION_ID)

before = request_json("GET", f"/v1/sessions/{SESSION_ID}", timeout=30)
print("BEFORE exists =", before is not None)
if before:
    print("BEFORE turn_count =", before.get("turn_count"))

payload = {"session_id": SESSION_ID, "text": TEXT, "llm": {"max_completion_tokens": 512}}
events, answer = post_sse("/v1/respond", payload)

after = request_json("GET", f"/v1/sessions/{SESSION_ID}", timeout=30)
print("AFTER turn_count =", after.get("turn_count") if after else None)
print("ANSWER chars =", len(answer))
print("ANSWER preview =")
print(answer[:700])

open("api_contract_cosmos_session_events.json", "w", encoding="utf-8").write(
    json.dumps(events, ensure_ascii=False, indent=2)
)
open("api_contract_cosmos_session_snapshot.json", "w", encoding="utf-8").write(
    json.dumps(after, ensure_ascii=False, indent=2)
)
open("api_contract_cosmos_session_answer.txt", "w", encoding="utf-8").write(answer)
print("SAVED api_contract_cosmos_session_events.json")
print("SAVED api_contract_cosmos_session_snapshot.json")
print("SAVED api_contract_cosmos_session_answer.txt")
