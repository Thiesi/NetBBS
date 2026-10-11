"""Bundled rule-based bot. Standalone stdlib program using only BOT API v1.

Run by filename so it also works from a source checkout without an installed
package. It has no database imports, filesystem access, or special host grants.
"""

import json
import re
import sys
import time
from collections import deque

VERSION = 1


class Baud:
    def __init__(self, hello, emit):
        self.emit = emit
        self.temperament = hello.get("temperament", "friendly")
        self.state = hello.get("state") or {"next": 1, "reminders": []}
        if not isinstance(self.state, dict) or not isinstance(self.state.get("reminders", []), list):
            raise ValueError("Invalid Baud reminder state")
        self.state.setdefault("next", 1)
        self.state.setdefault("reminders", [])
        self.sequence = 0
        self.waiting = None
        self.backlog = deque(maxlen=32)
        self.retry_at = 0

    def action(self, action, **fields):
        self.sequence += 1
        identity = str(self.sequence)
        self.emit({"id": identity, "action": action, **fields})
        return identity

    def reply(self, event, text):
        self.action("reply", request=event["request"], text=f"{event['caller']['name']}: {text}"[:500])

    def store(self, state, event=None, text=None):
        identity = self.action("state.set", value=state)
        self.waiting = (identity, "store", state, event, text)

    def handle(self, event):
        if event["type"] == "result":
            if self.waiting and event["id"] == self.waiting[0]:
                _, kind, value, request, text = self.waiting
                self.waiting = None
                if kind == "store":
                    if event["ok"]:
                        self.state = value
                    if request:
                        self.reply(request, text if event["ok"] else "Could not save that reminder; ask the SysOp.")
                elif event["ok"]:
                    state = {**self.state, "reminders": [r for r in self.state["reminders"] if r["id"] != value]}
                    self.store(state)
                else:
                    self.retry_at = time.time() + 60
                while not self.waiting and self.backlog:
                    self.handle(self.backlog.popleft())
            return
        if self.waiting:
            if event["type"] == "message":
                if len(self.backlog) == self.backlog.maxlen:
                    self.reply(event, "Busy saving reminders; try again shortly.")
                else:
                    self.backlog.append(event)
            return
        if event["type"] == "tick":
            if event["time"] < self.retry_at:
                return
            for reminder in self.state["reminders"]:
                if reminder["due"] <= event["time"]:
                    identity = self.action("say", channel=reminder["channel"],
                                           text=f"{reminder['name']}: reminder #{reminder['id']}: {reminder['text']}")
                    self.waiting = (identity, "deliver", reminder["id"], None, None)
                    break
            return
        if event["type"] != "message":
            return
        command, _, rest = event["text"].strip().lstrip("!").partition(" ")
        command = command.lower()
        own = [r for r in self.state["reminders"] if r["user"] == event["caller"]["id"]]
        if command in {"", "help"}:
            greeting = {"friendly": "Happy to help! ", "terse": "", "retro": "Carrier detected. "}[self.temperament]
            self.reply(event, greeting + "help; remind <1s-30d> <text>; reminders; cancel <id>. "
                       "Reminders appear publicly in this channel, including after a restart. No language model is enabled.")
        elif command == "reminders":
            # Do not reveal reminder text from a different, possibly private channel.
            here = [r for r in own if r["channel"] == event["channel"]]
            self.reply(event, "; ".join(f"#{r['id']}: {r['text'][:35]}" for r in here) or "No reminders here.")
        elif command == "cancel":
            match = next((r for r in own if str(r["id"]) == rest.strip() and r["channel"] == event["channel"]), None)
            if match is None:
                self.reply(event, "No reminder with that id belongs to you in this channel.")
            else:
                self.store({**self.state, "reminders": [r for r in self.state["reminders"] if r != match]},
                           event, f"Cancelled reminder #{match['id']}.")
        elif command == "remind":
            duration, _, text = rest.strip().partition(" ")
            match = re.fullmatch(r"([1-9][0-9]{0,6})([smhd])", duration.lower())
            seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]] if match else 0
            if not 1 <= seconds <= 30 * 86400 or not 1 <= len(text.strip()) <= 160:
                self.reply(event, "Use remind <1s-30d> <text of 1-160 characters>.")
            elif len(own) >= 8 or len(self.state["reminders"]) >= 48:
                self.reply(event, "Reminder limit reached (8 per caller, 48 per bot). Cancel an old reminder first.")
            else:
                ordinal = self.state["next"]
                reminder = {"id": ordinal, "user": event["caller"]["id"], "name": event["caller"]["name"],
                            "channel": event["channel"], "due": time.time() + seconds, "text": text.strip()}
                self.store({"next": ordinal + 1, "reminders": [*self.state["reminders"], reminder]},
                           event, f"Saved reminder #{ordinal}; it will appear publicly here.")
        else:
            self.reply(event, "I know help, remind, reminders and cancel. Conversation and other abilities are not enabled.")


def main():
    def emit(value):
        print(json.dumps(value, ensure_ascii=True), flush=True)

    hello = json.loads(sys.stdin.readline())
    if hello.get("type") != "hello" or hello.get("version") != VERSION:
        raise ValueError("Unsupported bot API")
    bot = Baud(hello, emit)
    emit({"type": "ready", "version": VERSION})
    for line in sys.stdin:
        bot.handle(json.loads(line))


if __name__ == "__main__":
    main()
