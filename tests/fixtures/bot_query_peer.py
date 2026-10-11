"""Independent BOT API v1 client; deliberately asks without trusting hello grants."""
import json
import sys


def send(**value):
    print(json.dumps(value), flush=True)


hello = json.loads(sys.stdin.readline())
send(type="ready", version=1)
pending = {}
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] == "message":
        key = event["request"]
        pending[key] = event
        send(id=key, action="query", query="stats", request=key)
    elif event["type"] == "result" and event["id"] in pending:
        request = pending.pop(event["id"])
        send(id="reply-" + event["id"], action="reply", request=request["request"],
             text=event["value"]["text"] if event["ok"] else event["error"])
