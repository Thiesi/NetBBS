"""An independent third-party-style API client for real-pipe tests."""
import json
import os
import sys


def send(**value):
    print(json.dumps(value), flush=True)


hello = json.loads(sys.stdin.readline())
os.close(2)  # Closing the optional diagnostic pipe must not end the session.
send(type="ready", version=1)
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] == "message":
        # The first three actions are deliberately unauthorized. Results must
        # be explicit and never broadcast, and this process must keep serving.
        send(id="forged", action="reply", request="someone-elses-request", text="forged")
        send(id="query", action="stats")
        send(id="wrong-channel", action="say", channel="private", text="leaked")
        send(id="echo", action="reply", request=event["request"], text="External reply: " + event["text"])
    elif event["type"] == "result" and event["id"] == "wrong-channel":
        send(id="state", action="state.set", value={"refused": not event["ok"]})
