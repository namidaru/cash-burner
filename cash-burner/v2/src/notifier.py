from __future__ import annotations

import os
import time
from typing import List

import requests


class DiscordNotifier:
    def __init__(self):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.enabled = bool(self.webhook_url)
        self.username = self._resolve_username()
        self.timeout = float(os.getenv("DISCORD_TIMEOUT_SEC", "4"))

    def _resolve_username(self) -> str:
        fixed_name = "파쇄기"
        env_name = (os.getenv("DISCORD_BOT_NAME", "") or "").strip()
        if not env_name:
            return fixed_name

        # 기본은 고정 이름(파쇄기) 사용: Windows cmd 인코딩(cp949/utf-8) 꼬임으로
        # 한글 환경변수가 깨지는 경우가 빈번해서, env 이름은 명시 opt-in일 때만 사용.
        use_env_name = os.getenv("DISCORD_USE_ENV_BOT_NAME", "0").strip() == "1"
        if not use_env_name:
            return fixed_name

        if self._looks_garbled(env_name):
            return fixed_name
        return env_name

    def _looks_garbled(self, name: str) -> bool:
        if "?" in name or "�" in name:
            return True
        hangul_or_ascii = 0
        for ch in name:
            o = ord(ch)
            if (0x20 <= o <= 0x7E) or (0xAC00 <= o <= 0xD7A3):
                hangul_or_ascii += 1
        return hangul_or_ascii < max(1, int(len(name) * 0.6))

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
        attempt = 0
        while attempt < 3:
            try:
                r = requests.post(self.webhook_url, json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    retry_after = float((r.json() or {}).get("retry_after", 1.0))
                    time.sleep(min(retry_after, 10.0))
                    continue  # BUG-036: attempt 증가 없이 재시도
                # BUG-035: 429 외 HTTP 오류도 stderr에 기록
                if r.status_code >= 400:
                    import sys
                    print(f"[notifier] HTTP {r.status_code} title={title!r:.80} body={r.text[:120]}", file=sys.stderr)
                return
            except Exception:
                time.sleep(0.5)
            attempt += 1
