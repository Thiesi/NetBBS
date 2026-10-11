"""External API example: announce events using only their host-owned tokens."""
import json
import sys

json.loads(sys.stdin.readline())
print(json.dumps({"type": "ready", "version": 1}), flush=True)
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] in {"post", "file"}:
        assert "body" not in event and "request" not in event
        # Neither a replacement title nor a different destination is accepted.
        print(json.dumps({"id": "announce", "action": "announce", "event": event["event"],
                          "channel": "elsewhere", "text": "forged announcement"}), flush=True)
