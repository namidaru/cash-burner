# src/ws_sub_manager.py
from __future__ import annotations

import os, time
from typing import List

WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", r"data\watchlist.txt")

def _ensure_dir(path: str):
    d=os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def write_watchlist(symbols: List[str]):
    _ensure_dir(WATCHLIST_FILE)
    with open(WATCHLIST_FILE,"w",encoding="utf-8") as f:
        for s in symbols:
            f.write(s.strip()+"\n")
