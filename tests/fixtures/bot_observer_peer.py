"""Independent API client retaining a bounded sample of live observations."""
import json
import sys

hello = json.loads(sys.stdin.readline())
print(json.dumps({"type": "ready", "version": 1}), flush=True)
events = []
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] in {"channel", "presence"}:
        assert "request" not in event
        events = (events + [event])[-10:]
        print(json.dumps({"id": "observe", "action": "state.set", "value": {"events": events}}), flush=True)
    elif event["type"] == "message":
        print(json.dumps({"id": "reply", "action": "reply", "request": event["request"], "text": "observer ready"}), flush=True)
