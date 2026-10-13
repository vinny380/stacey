"""A named invocation opens one turn; playback opens a brief follow-up window."""

from __future__ import annotations

import re
import time

INVOCATION = re.compile(r"^\W*(?:(?:hey|hi|hello|ok|okay|yo)\W+)?stac(?:e)?y\b", re.I)


class InvocationWindow:
    def __init__(self, *, followup_seconds=5.0, clock=time.monotonic):
        self.clock = clock
        self.followup_seconds = followup_seconds
        self.listen_until = float("-inf")
        self.onsets: dict[str, bool] = {}

    def speech_started(self, item_id: str) -> None:
        self.onsets[item_id] = self.clock() < self.listen_until
        if len(self.onsets) > 128:
            self.onsets.pop(next(iter(self.onsets)))

    def accept(self, item_id: str, text: str) -> str | None:
        in_window = self.onsets.pop(item_id, self.clock() < self.listen_until)
        reason = "invocation" if INVOCATION.match(text) else "followup" if in_window else None
        if reason is not None and text.strip():
            # Consume the window immediately, including while a tool/model is busy.
            self.listen_until = float("-inf")
            return reason
        return None

    def playback_started(self) -> None:
        self.listen_until = float("-inf")

    def playback_stopped(self) -> None:
        self.listen_until = self.clock() + self.followup_seconds
