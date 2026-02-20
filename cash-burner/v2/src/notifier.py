from __future__ import annotations

import os
import time
from typing import List

import requests


class DiscordNotifier:
    def __init__(self):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.enabled = bool(self.webhook_url)
        self.username = os.getenv("DISCORD_BOT_NAME", "파세경보기")
        self.timeout = float(os.getenv("DISCORD_TIMEOUT_SEC", "4"))

    def send(self, title: str, lines: List[str], color: int = 0x5865F2):
        if not self.enabled:
            return
        desc = "\n".join(lines)
        payload = {
            "username": self.username,
            "embeds": [
                {
                    "title": title,
                    "description": desc[:3800],
                    "color": color,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                }
            ],
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=self.timeout)
        except Exception:
            return
