# src/ws_sub_manager.py
from __future__ import annotations

import os
from typing import List

WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))

def _ensure_dir(path: str):
    d=os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def write_watchlist(symbols: List[str]):
    _ensure_dir(WATCHLIST_FILE)
    # BUG-026: 원자적 쓰기로 부분 쓰기 방지
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in symbols:
            f.write(s.strip() + "\n")
    os.replace(tmp, WATCHLIST_FILE)
