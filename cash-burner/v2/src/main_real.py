# src/main_real.py
from __future__ import annotations

import os
import time
import threading
from typing import List

from scanner_company_rank import build_watchlist
from quote_basic import ensure_prev_close
from ws_sub_manager import write_watchlist
from ws_capture_live import WSCapture
from runner_live import run_live

IN_FILE = os.getenv("IN_FILE", os.path.join("data", "ws_dump.log"))
LIVE_POLL_SEC = float(os.getenv("LIVE_POLL_SEC", "0.2"))
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "10"))
WATCHLIST_DEBUG = os.getenv("WATCHLIST_DEBUG", os.path.join("data", "watchlist_debug.log"))

def _log(msg: str):
    try:
        d = os.path.dirname(WATCHLIST_DEBUG)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(WATCHLIST_DEBUG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg}\n")
    except Exception:
        pass

def scanner_loop():
    _log("START scanner_company_rank mode")

    last_watch: List[str] = []
    while True:
        try:
            watch = build_watchlist()
            _log(f"rank watchlist n={len(watch)} head={watch[:10]}")
            if watch:
                # prev_close cache needed for +12% block and limitup-gap take
                ensure_prev_close(watch)
                if watch != last_watch:
                    write_watchlist(watch)
                    last_watch = watch
                    _log("watchlist updated")
                else:
                    _log("watchlist unchanged")
            else:
                _log("WARN empty watchlist (rank api returned none) - keep previous")
        except Exception as e:
            _log(f"ERR {type(e).__name__}: {e}")
        time.sleep(SCAN_INTERVAL_SEC)

def main():
    threading.Thread(target=scanner_loop, daemon=True).start()
    cap = WSCapture()
    threading.Thread(target=cap.start, daemon=True).start()
    run_live(IN_FILE, poll=LIVE_POLL_SEC)

if __name__ == "__main__":
    main()
